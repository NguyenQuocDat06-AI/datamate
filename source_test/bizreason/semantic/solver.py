"""Decide one question: parse it, resolve its time windows, check coverage, then compile the answer."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Protocol

from bizreason.contracts import AnswerType, Prediction, Question, TimeWindow
from bizreason.data.vocabulary import AGE_BUCKET_WORD, AGE_OPEN_BUCKET_WORDS, PRICE_RISE_RE
from bizreason.semantic.compiler import DEBT_MEASURES, Compiler, Intent, NotRecorded, Unsupported
from bizreason.semantic import rule_terms, rulebook_params
from bizreason.semantic.quality import QualityFlags, assess
from bizreason.semantic.vocabulary import (
    LARGE_UNIT_LOOSE_CUE, LARGE_UNIT_WHOLE_CUES, LOT_MEASURES, QUOTED_DIMENSIONS, STOCK_MEASURES, TABLE_ASK_CUES,
    CAUSE_WORD, DELIVERY_WORDS, MOST_ORDERS_CUE, PERCENT_CUES, TRIP_WORDS, UNDEFINED_JUDGEMENT_WORDS,
)
from bizreason.temporal import TemporalResolver
from bizreason.utils import normalize_lookup

POINT_IN_TIME = {
    "debt_outstanding", "debtor_count", "inventory_qty", "inventory_value", "out_of_stock_count",
    "expiry_lot", "expiring_lot_value",
}
ORDER_MEASURES = {
    "no_sale_count", "orders_containing_product",
    "revenue", "order_count", "cancelled_count", "cancel_rate", "avg_per_order",
    "qty_base_sold", "distinct_customers", "gross_profit",
}
# Which data-quality flags an intent depends on (see quality.py). Unlisted measures depend on none of them.
QUANTITY_MEASURES = {
    "qty_base_sold", "gross_profit", "unit_sale_price_on_date", "no_sale_count",
    "orders_containing_product", "unit_purchase_price",
}
QUANTITY_FILTERS = {"unit", "size"}
MONEY_MEASURES = {"revenue", "avg_per_order", "gross_profit", "purchase_amount", "order_total_due"} | DEBT_MEASURES
ORDER_BASED_MEASURES = ORDER_MEASURES | DEBT_MEASURES
FLAG_NAMES = (
    "qty_unreliable", "money_unreliable", "order_structure_broken", "status_unreliable", "payments_unlinked",
    "customer_group_incomplete",
)
PERIOD_SHAPES = {"breakdown", "argmax_period", "trend_check"}
# Stored values offered to the parser, per filter dimension: (table, column).
CHOICE_COLUMNS = {
    "channel": ("orders", "channel"),
    "customer_group": ("orders", "customer_group"),
    "payment_method": ("orders", "payment_method"),
    "product_group": ("lines", "category"),
}
CHOICE_LIMIT = 40
# A model failure on a catalog or auxiliary table blocks only the questions that read what that table feeds into
# the layer: {table kind: the intent features it can change}. A feature is a filter dimension, a group_by
# dimension, a measure, or a threshold subject (see _intent_features). Built from layer.py: _fill_from_catalogs,
# _load_product_facts, _load_product_attrs, _load_unit_factors, _load_customer_groups, _map_employees and
# _load_catalog_keys (which picks id columns by the catalog ids). A kind mapped to None cannot be decided, so
# every question abstains. A kind missing from this dict is not a catalog: any failure on it blocks everything.
_PRODUCT_FEATURES = frozenset({
    "product", "product_group", "size", "unit", "prescription", "active_ingredient", "product_kind",
    "gross_profit", "unit_sale_price_on_date", "unit_purchase_price", "qty_base_sold",
    "orders_containing_product", "no_sale_count",
})
CATALOG_FEATURES: dict[str, frozenset[str] | None] = {
    # category and base unit of products, prescription, active ingredient, base units per large unit (qty_base)
    "product_catalog": _PRODUCT_FEATURES,
    # base units per large unit, the same product features as the catalog
    "unit_conversion": _PRODUCT_FEATURES,
    # alias names resolve to full product names, and product kind
    "alias": _PRODUCT_FEATURES,
    # the layer does not read promotion rows, so no answer depends on them
    "promotion": frozenset(),
    # the layer does not read price rows; the price measures are the ones a price table would feed
    "price_history": frozenset({"gross_profit", "unit_sale_price_on_date", "unit_purchase_price"}),
    # customer group of an order when the order has none, and the id column choice for customers
    "customer_catalog": frozenset({"customer", "customer_group", "distinct_customers"}),
    # employee spellings are matched to the full names of this catalog
    "employee_catalog": frozenset({"employee"}),
    # "other" covers any table, so the features it can change cannot be bounded: every question abstains
    "other": None,
}


# Abstain reasons that mean the typed intent cannot express the question (not: a name not found, no time window, a
# data-quality gate, a model failure, two readings that disagree). Only these go to the SQL fallback.
FALLBACK_REASONS = (
    "outside known measures", "measure ", "ranking of ", "group by ", "dimension ", "shape ", "unknown filter dimension",
    "second measure on shape", "table with a second measure", "threshold on shape", "threshold metric",
    "average per weekday", "location filter", "delivery trips with a filter", "purchase amount with filter",
    "debt measure with filter", "breakdown names", "invalid measure",
)
_FALLBACK: Any = None


def set_fallback(fallback: Any) -> None:
    """The SqlFallback the solvers use (None disables it). Set once per run, before questions are solved."""
    global _FALLBACK
    _FALLBACK = fallback


MIN_NAME_CHARS = 3


def _implausible(kind: str, value: Any) -> str | None:
    """A reason when an answer looks like a placeholder rather than a real value, else None.

    A named entity (a customer, product, employee) of two letters or fewer is a placeholder or a cut-off name
    ("lẻ", "kl"), not a real name (short given names like "Lan" stay); on train no correct answer was that short; on
    the public test every such answer was wrong."""
    names = [value] if kind == "string" else (value if kind == "list" else [])
    for name in names:
        if isinstance(name, str) and not any(ch.isdigit() for ch in name) and len(normalize_lookup(name)) < MIN_NAME_CHARS:
            return f"name too short: {name!r}"
    return None


def undefined_judgement(question: str) -> str | None:
    """The first judgement word the question uses that the active rulebook never mentions, or None. Without a
    rulebook text nothing is reported."""
    text = rulebook_params.rulebook_text()
    if text is None:
        return None
    asked = f" {normalize_lookup(question)} "
    for word in UNDEFINED_JUDGEMENT_WORDS:
        if f" {word} " in asked and f" {word} " not in text:
            return word
    return None


def age_buckets(question: str) -> list[tuple[int, int | None]]:
    """Debt age buckets the question names, in order: "a–b ngày" pairs and a final open "trên c ngày".

    Only read when the question speaks of debt age; [] otherwise or when the buckets are not contiguous."""
    # A dash between two numbers is a range; normalize_lookup would turn it into a space like a date's slash.
    text = normalize_lookup(re.sub(r"(?<=\d)\s*[-–—]\s*(?=\d)", " den ", question))
    if not re.search(rf"\b{AGE_BUCKET_WORD}\b", text):
        return []
    buckets: list[tuple[int, int | None]] = []
    for low, high, open_low in re.findall(
        rf"(?<!\d)(\d{{1,3}}) (?:den|toi) (\d{{1,3}})(?!\d)|\b{AGE_OPEN_BUCKET_WORDS} (\d{{1,3}})\b", text
    ):
        if open_low:
            buckets.append((int(open_low) + 1, None))
        else:
            buckets.append((int(low), int(high)))
    if len(buckets) < 2 or buckets[-1][1] is not None:
        return []
    for (low, high), (next_low, _) in zip(buckets, buckets[1:]):
        if high is None or next_low != high + 1 or low > high:
            return []
    return buckets


class ParseResult(Protocol):
    @property
    def intent(self) -> Intent | None: ...

    @property
    def reason(self) -> str: ...


class IntentParser(Protocol):
    def parse(self, question: str, choices: dict[str, list[str]] | None = None) -> ParseResult: ...


@dataclass
class SolveTrace:
    question_id: str
    intent: Intent | None
    windows: list[TimeWindow]
    decision: str  # "value" | "unanswerable" | "abstain"
    reason: str
    # Names of the data-quality flags that fired for this question, when they were checked.
    quality: list[str] = field(default_factory=list)


def filter_choices(connection: sqlite3.Connection) -> dict[str, list[str]]:
    """Distinct stored values for each filter dimension; a dimension whose table or column is missing is left out."""
    choices: dict[str, list[str]] = {}
    for dim, (table, column) in CHOICE_COLUMNS.items():
        try:
            rows = connection.execute(
                f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL "
                f"ORDER BY {column} LIMIT ?",
                (CHOICE_LIMIT,),
            ).fetchall()
        except sqlite3.Error:
            continue
        values = [str(row[0]) for row in rows if str(row[0]).strip()]
        if values:
            choices[dim] = values
    return choices


def _flag(facts: dict[str, Any], key: str) -> int | None:
    """A shop fact read as a whole number, or None when it is absent or not a number."""
    try:
        return int(str(facts[key]).strip())
    except (KeyError, TypeError, ValueError):
        return None


def _outside(window: TimeWindow, low: date, high: date) -> bool:
    return window.end_exclusive <= low or window.start > high


def _required_flags(intent: Intent) -> list[str]:
    """The data-quality flags whose failure would make this intent's answer wrong."""
    required = []
    filter_dims = {item.get("dim") for item in intent.filters}
    if intent.measure in QUANTITY_MEASURES or filter_dims & QUANTITY_FILTERS:
        required.append("qty_unreliable")
    # A threshold reads its own metric, whatever the measure says.
    threshold_metric = (intent.threshold or {}).get("metric")
    if threshold_metric == "qty":
        required.append("qty_unreliable")
    if threshold_metric == "revenue":
        required.append("money_unreliable")
    if intent.measure in MONEY_MEASURES:
        required.append("money_unreliable")
    if intent.measure in ORDER_BASED_MEASURES:
        required.extend(["order_structure_broken", "status_unreliable"])
    if intent.measure in DEBT_MEASURES:
        required.append("payments_unlinked")
    if intent.group_by == "customer_group" or "customer_group" in filter_dims:
        required.append("customer_group_incomplete")
    return required


def _intent_features(intent: Intent) -> set[str]:
    """Every filter dimension, group_by, measure and threshold subject that the intent reads."""
    features = {item.get("dim") for item in intent.filters}
    features |= {intent.group_by, intent.measure, intent.measure2}
    subject = (intent.threshold or {}).get("subject")
    if subject is not None:
        features.add(subject)
    features.discard(None)
    return {str(feature) for feature in features}


def _blocking_failure(failures: list[tuple[str, str | None, str | None]], intent: Intent | None) -> str | None:
    """The stage of the first model failure that makes this question's answer untrusted, or None.

    A shop-wide stage, a table of a kind that is not a catalog, a table of unknown kind, and every failure when the
    intent is unknown all block. A catalog failure blocks only an intent that reads a feature its kind feeds.
    """
    for stage, table, kind in failures:
        if table is None or intent is None:
            return stage
        if kind not in CATALOG_FEATURES:
            return stage
        features = CATALOG_FEATURES[kind]
        if features is None or features & _intent_features(intent):
            return stage
    return None


def _ungrounded_dim(question_text: str, filters: list[dict[str, Any]]) -> str | None:
    """The first quoted-dimension filter whose value shares no whole word with the question, or None."""
    question_words = set(normalize_lookup(question_text).split())
    for item in filters:
        dim = item.get("dim")
        if dim not in QUOTED_DIMENSIONS:
            continue
        value_words = normalize_lookup(str(item.get("value") or "")).split()
        if not any(word in question_words for word in value_words):
            return dim
    return None


_TABLE_CUES = tuple(normalize_lookup(cue) for cue in TABLE_ASK_CUES)


def asks_entity_values(question_text: str) -> bool:
    """True when the question asks for a value beside each entity, by whole-word match of TABLE_ASK_CUES."""
    text = f" {normalize_lookup(question_text)} "
    return any(f" {cue} " in text for cue in _TABLE_CUES)


def asks_unit_split(question_text: str) -> bool:
    """True when the question asks a stock quantity as whole large units and loose base units (whole-word cues)."""
    words = set(normalize_lookup(question_text).split())
    return LARGE_UNIT_LOOSE_CUE in words and any(cue in words for cue in LARGE_UNIT_WHOLE_CUES)


def _quality_reason(flags: QualityFlags, name: str) -> str:
    """The first reason the assessment gives for one flag; reasons are written as 'flag name: detail'."""
    return next(reason for reason in flags.reasons if reason.startswith(f"{name}:"))


class SemanticSolver:
    def __init__(self, parser: IntentParser, temporal: TemporalResolver, connection: sqlite3.Connection):
        self.parser = parser
        self.temporal = temporal
        self.connection = connection
        self._flags: QualityFlags | None = None

    def _quality(self) -> QualityFlags:
        """The data-quality flags of this database, assessed once per solver."""
        if self._flags is None:
            self._flags = assess(self.connection)
        return self._flags

    def solve(self, question: Question) -> tuple[Prediction, SolveTrace]:
        trace = SolveTrace(question_id=question.id, intent=None, windows=[], decision="abstain", reason="")
        try:
            return self._decide(question, trace)
        except Exception as exc:  # one bad question must never crash the run
            return self._finish(question, trace, "abstain", type(exc).__name__)

    def _decide(self, question: Question, trace: SolveTrace) -> tuple[Prediction, SolveTrace]:
        judgement = undefined_judgement(question.question)
        if judgement:
            # A personal judgement the rulebook does not define is not recorded in any shop file (rulebook §7).
            return self._finish(question, trace, "unanswerable", f"judgement not defined by the rulebook: {judgement}")
        parsed = self.parser.parse(question.question, choices=filter_choices(self.connection))
        intent = parsed.intent
        trace.intent = intent
        blocked = _blocking_failure(self._model_failures(), intent)
        if blocked is not None:
            # The layer was built with a model fallback that this question reads, so no answer from it is trusted;
            # this runs before the unanswerable paths so that a degraded shop is never reported as unanswerable.
            return self._finish(question, trace, "abstain", f"model stage failed: {blocked}")
        if intent is None:
            return self._finish(question, trace, "abstain", parsed.reason)
        # A ranking term the test set's rulebook defines decides what is ranked, not the parser's guess.
        intent, _term = rule_terms.apply(intent, question.question)
        trace.intent = intent
        asked = f" {normalize_lookup(question.question)} "
        if intent.measure == "cancelled_count" and any(f" {cue} " in asked for cue in PERCENT_CUES) or (
                intent.measure == "cancelled_count" and "%" in question.question):
            # "đơn huỷ chiếm mấy %" asks the cancellation rate, not the count.
            intent = replace(intent, measure="cancel_rate")
            trace.intent = intent
        if (intent.shape in ("argmax_entity", "top_k") and f" {MOST_ORDERS_CUE} " in asked
                and intent.measure in ("revenue", "qty_base_sold")):
            # "ai mua nhiều đơn nhất" ranks by orders, whatever money or quantity the parse chose.
            intent = replace(intent, measure="order_count")
            trace.intent = intent
        if intent.measure == "order_count" and any(f" {w} " in asked for w in DELIVERY_WORDS) and any(
                f" {w} " in asked for w in TRIP_WORDS):
            # "xe đã giao mấy đợt hàng" counts delivery trips, not orders (rulebook: giao nhiều đợt).
            intent = replace(intent, measure="delivery_trip_count")
            trace.intent = intent
        cause_at = re.search(rf" {CAUSE_WORD} (?!x |\d)(\S+(?: \S+){{0,2}})", asked)  # not "vỉ x 4", "vỉ 10"
        if intent.measure in ("cancelled_count", "cancel_rate") and cause_at:
            cause = cause_at.group(1).split()
            if cause and not self._notes_mention(cause):
                # A cancellation or return "vì <reason>": no file records the reason (rulebook §7).
                return self._finish(question, trace, "unanswerable", "reason for cancellation not recorded")
            return self._finish(question, trace, "abstain", "reason for cancellation")
        named = self._named_person(question.question, intent)
        if named is not None:
            # The question names one stored employee or customer in full that the parse left out (names typed without
            # accents are often missed): without the filter the answer would cover the whole shop.
            intent = replace(intent, filters=[*intent.filters, named])
            trace.intent = intent
        ungrounded = _ungrounded_dim(question.question, intent.filters)
        if ungrounded is not None:
            return self._finish(question, trace, "abstain", f"filter not in question: {ungrounded}")
        if intent.measure == "outside":
            return self._finish(question, trace, "abstain", "outside known measures")

        facts = self._shop_facts()
        if intent.location is not None:
            if facts is not None and _flag(facts, "has_address") == 0 and _flag(facts, "has_orders") == 1:
                return self._finish(question, trace, "unanswerable", "no address recorded")
            return self._finish(question, trace, "abstain", "location filter unsupported")

        uses_customer_group = intent.group_by == "customer_group" or any(
            f.get("dim") == "customer_group" for f in intent.filters
        )
        if (
            uses_customer_group and facts is not None
            and _flag(facts, "has_customer_group") == 0 and _flag(facts, "has_orders") == 1
        ):
            return self._finish(question, trace, "unanswerable", "customer group not recorded")

        windows: list[TimeWindow] = []
        if intent.shape in PERIOD_SHAPES:
            windows = self.temporal.resolve_periods(question.question, question.as_of_date)
        if not windows:
            windows = self.temporal.resolve(question.question, question.as_of_date)
        if not windows and intent.shape == "compare_periods":
            windows = self._price_rise_windows(question.question, intent)
        if not windows and intent.measure in POINT_IN_TIME:
            # Rulebook §2: a balance with no stated day is the position at the end of today.
            windows = [
                TimeWindow(
                    role="target", mode="cumulative", start=date.min,
                    end_exclusive=question.as_of_date + timedelta(days=1), evidence=["as_of_date"],
                )
            ]
        trace.windows = windows

        if intent.measure not in POINT_IN_TIME and not windows:
            return self._finish(question, trace, "abstain", "no time window")

        if intent.measure in ORDER_MEASURES:
            span = self._orders_range()
            if span is None:
                # An empty layer means the build failed, which is not evidence that the data is absent.
                return self._finish(question, trace, "abstain", "orders table has no dated rows")
            low, high = span
            targets = [w for w in windows if w.role != "comparison"]
            if targets and all(_outside(w, low, high) for w in targets):
                return self._finish(
                    question, trace, "unanswerable",
                    f"requested period lies wholly outside the shop's data range {low.isoformat()} to {high.isoformat()}",
                )
            comparisons = [w for w in windows if w.role == "comparison"]
            orders_recorded = facts is None or _flag(facts, "has_orders") == 1
            if comparisons and orders_recorded and all(_outside(w, low, high) for w in comparisons):
                return self._finish(
                    question, trace, "unanswerable",
                    f"comparison period lies wholly outside the shop's data range {low.isoformat()} to {high.isoformat()}",
                )

        flags = self._quality()
        trace.quality = [name for name in FLAG_NAMES if getattr(flags, name)]
        for name in _required_flags(intent):
            if getattr(flags, name):
                return self._finish(question, trace, "abstain", f"data quality: {_quality_reason(flags, name)}")

        if intent.measure in STOCK_MEASURES | LOT_MEASURES:
            return self._decide_stock(question, trace, intent, windows)
        wants_table = intent.group_by is not None and asks_entity_values(question.question)
        try:
            compiler = Compiler(self.connection)
            if wants_table and intent.shape == "top_k" and (intent.top_k or 0) >= 2:
                answer = compiler.table(intent, windows)
            elif (buckets := age_buckets(question.question)) and intent.measure == "debt_outstanding" \
                    and intent.shape == "breakdown" and intent.group_by is None:
                target = [w for w in windows if w.role != "comparison"]
                if len(target) != 1:
                    raise Unsupported("debt aging needs one window")
                answer = compiler.debt_aging(intent, target[0], buckets)
            else:
                answer = compiler.answer(intent, windows)
                if wants_table and answer.type == "list":
                    # A plain list cannot carry the value each entity was asked with; abstaining is safer than a wrong list.
                    raise Unsupported("answer shape: table expected")
        except NotRecorded as exc:
            return self._finish(question, trace, "unanswerable", str(exc))
        except Unsupported as exc:
            return self._finish(question, trace, "abstain", str(exc) or "unsupported")
        except Exception as exc:
            return self._finish(question, trace, "abstain", type(exc).__name__)

        implausible = _implausible(answer.type, answer.value)
        if implausible:
            return self._finish(question, trace, "abstain", f"implausible answer: {implausible}")
        prediction = Prediction(id=question.id, type=AnswerType(answer.type), answer=answer.value)
        trace.decision = "value"
        return prediction, trace

    def _decide_lots(self, question: Question, trace: SolveTrace, intent: Intent,
                     windows: list[TimeWindow]) -> tuple[Prediction, SolveTrace]:
        """An expiry question: the lots still in stock today, by expiry date (see Compiler.lots)."""
        if intent.measure2 is not None or intent.group_by is not None or intent.threshold is not None:
            return self._finish(question, trace, "abstain", "expiry question with a second measure or grouping")
        if intent.shape not in ("scalar", "argmax_date", "list_filter"):
            return self._finish(question, trace, "abstain", f"expiry shape {intent.shape}")
        target = [w for w in windows if w.role != "comparison"]
        if len(target) != 1:
            return self._finish(question, trace, "abstain", "expiry needs one window")
        try:
            value = Compiler(self.connection).lots(intent, target[0], question.as_of_date)
        except NotRecorded as exc:
            return self._finish(question, trace, "unanswerable", str(exc))
        except Unsupported as exc:
            return self._finish(question, trace, "abstain", str(exc) or "unsupported")
        except Exception as exc:
            return self._finish(question, trace, "abstain", type(exc).__name__)
        trace.decision = "value"
        return Prediction(id=question.id, type=AnswerType(value.type), answer=value.value), trace

    def _decide_stock(self, question: Question, trace: SolveTrace, intent: Intent,
                      windows: list[TimeWindow]) -> tuple[Prediction, SolveTrace]:
        """A stock question: one product's quantity (plain or as whole large units), the stock value, or the count of
        bought-in products at zero. Any other shape or a second measure is not answered."""
        if intent.measure in LOT_MEASURES:
            return self._decide_lots(question, trace, intent, windows)
        split = asks_unit_split(question.question) and intent.measure == "inventory_qty"
        if intent.measure2 is not None or intent.group_by is not None or intent.threshold is not None:
            return self._finish(question, trace, "abstain", "stock question with a second measure or grouping")
        if intent.shape == "breakdown" and not split:
            return self._finish(question, trace, "abstain", "stock breakdown without large units")
        if intent.shape not in ("scalar", "breakdown"):
            return self._finish(question, trace, "abstain", f"stock shape {intent.shape}")
        target = [w for w in windows if w.role != "comparison"]
        if len(target) != 1:
            return self._finish(question, trace, "abstain", "stock needs one day")
        try:
            value = Compiler(self.connection).stock(intent, target[0], split=split)
        except NotRecorded as exc:
            return self._finish(question, trace, "unanswerable", str(exc))
        except Unsupported as exc:
            return self._finish(question, trace, "abstain", str(exc) or "unsupported")
        except Exception as exc:
            return self._finish(question, trace, "abstain", type(exc).__name__)
        trace.decision = "value"
        return Prediction(id=question.id, type=AnswerType(value.type), answer=value.value), trace

    def _price_rise_windows(self, text: str, intent: Intent) -> list[TimeWindow]:
        """Rulebook §5 (hiệu ứng tăng giá) when the question names the new price but not the day: the day D is the
        first price-list day the product's price rose to that price; compare N days from D with the N days before.

        [] unless there is exactly one product filter, one new price, one N, and a matching rise."""
        normalized = normalize_lookup(text)
        prices = {int(p.replace(" ", "")) for p in re.findall(PRICE_RISE_RE, normalized)}
        days = set(re.findall(r"\b(\d{1,3}) ngay\b", normalized))
        products = [item for item in intent.filters if item.get("dim") == "product" and not item.get("exclude")]
        if len(prices) != 1 or len(days) != 1 or len(products) != 1:
            return []
        price, n = prices.pop(), int(days.pop())
        try:
            rows = self.connection.execute(
                "SELECT product_name, date, old_price, new_price FROM price_changes "
                "WHERE product_name IS NOT NULL AND date IS NOT NULL ORDER BY date").fetchall()
        except sqlite3.Error:
            return []
        names = sorted({row[0] for row in rows})
        chosen = set(Compiler._match("product", names, normalize_lookup(str(products[0].get("value", "")))))
        if not chosen or n < 1:
            return []
        keys = {normalize_lookup(name) for name in chosen}
        history = [row for row in rows if normalize_lookup(row[0]) in keys]
        previous = None
        for _, day, old_price, new_price in history:
            before = old_price if old_price is not None else previous
            if new_price is not None and abs(new_price - price) < 0.5 and before is not None and new_price > before:
                start = date.fromisoformat(str(day)[:10])
                evidence = ["question", "price_changes"]
                return [
                    TimeWindow(role="target", start=start, end_exclusive=start + timedelta(days=n), evidence=evidence),
                    TimeWindow(role="comparison", start=start - timedelta(days=n), end_exclusive=start,
                               evidence=evidence),
                ]
            previous = new_price if new_price is not None else previous
        return []

    def _named_person(self, text: str, intent: Intent) -> dict[str, Any] | None:
        """A filter for the one employee or customer whose full stored name (two words or more) the question writes
        and the intent neither filters nor groups by; None otherwise."""
        asked = f" {normalize_lookup(text)} "
        for dim, column in (("employee", "employee"), ("customer", "customer_name")):
            if intent.group_by == dim or any(item.get("dim") == dim for item in intent.filters):
                continue
            try:
                names = [r[0] for r in self.connection.execute(
                    f"SELECT DISTINCT {column} FROM orders WHERE {column} IS NOT NULL")]
            except sqlite3.Error:
                continue
            hits = {normalize_lookup(n) for n in names if len(normalize_lookup(n).split()) >= 2
                    and f" {normalize_lookup(n)} " in asked}
            if len(hits) == 1:
                return {"dim": dim, "value": hits.pop(), "exclude": False}
        return None

    def _notes_mention(self, words: list[str]) -> bool:
        """True when some order note contains all these words (the shop may record why an order was cancelled)."""
        try:
            cols = {r[1] for r in self.connection.execute("PRAGMA table_info(orders)")}
            note = next((c for c in ("note", "order_note", "status_raw") if c in cols), None)
            if note is None:
                return False
            for (text,) in self.connection.execute(f"SELECT DISTINCT {note} FROM orders WHERE {note} IS NOT NULL LIMIT 5000"):
                padded = f" {normalize_lookup(str(text))} "
                if all(f" {w} " in padded for w in words):
                    return True
        except sqlite3.Error:
            return False
        return False

    def _shop_facts(self) -> dict[str, Any] | None:
        """Key-value facts the shop records about itself; None when the shop_facts table is missing."""
        exists = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'shop_facts'"
        ).fetchone()
        if exists is None:
            return None
        return {str(key): value for key, value in self.connection.execute("SELECT key, value FROM shop_facts")}

    def _model_failures(self) -> list[tuple[str, str | None, str | None]]:
        """(stage, table_name, kind) of each model failure recorded in the layer. A table with only a stage column
        (built before failures named their table) gives shop-wide failures. Empty when the table is missing."""
        exists = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'model_failures'"
        ).fetchone()
        if exists is None:
            return []
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(model_failures)")}
        if {"table_name", "kind"} <= columns:
            rows = self.connection.execute("SELECT stage, table_name, kind FROM model_failures ORDER BY rowid").fetchall()
        else:
            rows = [(row[0], None, None) for row in self.connection.execute(
                "SELECT stage FROM model_failures ORDER BY rowid").fetchall()]
        return [
            ("unknown" if stage is None else str(stage),
             None if table is None else str(table),
             None if kind is None else str(kind))
            for stage, table, kind in rows
        ]

    def _orders_range(self) -> tuple[date, date] | None:
        row = self.connection.execute("SELECT MIN(date), MAX(date) FROM orders").fetchone()
        if row is None or row[0] is None or row[1] is None:
            return None
        return date.fromisoformat(str(row[0])[:10]), date.fromisoformat(str(row[1])[:10])

    def _finish(
        self, question: Question, trace: SolveTrace, decision: str, reason: str
    ) -> tuple[Prediction, SolveTrace]:
        if decision == "abstain" and _FALLBACK is not None and reason.startswith(FALLBACK_REASONS):
            # The typed intent cannot express the question; let two independent SQL readings try (they must agree).
            result = None
            try:
                # Its own read-only connection: the fallback sets an authorizer and a progress handler, which must not
                # touch the connection other questions of this shop are using in other threads.
                path = self.connection.execute("PRAGMA database_list").fetchone()[2]
                own = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True) if path else None
                if own is not None:
                    try:
                        result = _FALLBACK.answer(question.question, own, question.as_of_date)
                    finally:
                        own.close()
            except Exception:
                result = None
            if result is not None:
                trace.decision = "value"
                trace.reason = f"sql fallback after: {reason}"
                return Prediction(id=question.id, type=result.type, answer=result.answer), trace
            reason = f"{reason}; sql fallback: {getattr(_FALLBACK, 'last_reason', '')}"[:300]
        trace.decision = decision
        trace.reason = reason
        if decision == "unanswerable":
            prediction = Prediction(id=question.id, type=AnswerType.UNANSWERABLE, reason=reason)
        else:
            prediction = Prediction(id=question.id, type=AnswerType.ABSTAIN, reason=reason)
        return prediction, trace
