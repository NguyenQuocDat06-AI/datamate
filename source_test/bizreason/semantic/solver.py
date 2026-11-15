"""Decide one question: parse it, resolve its time windows, check coverage, then compile the answer."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Protocol

from bizreason.contracts import AnswerType, Prediction, Question, TimeWindow
from bizreason.data.vocabulary import (
    AGE_BUCKET_WORD, AGE_OPEN_BUCKET_WORDS, DAYS_BEFORE_RE, DMY_DATE_RE, NON_WEEKDAY_PHRASE_RE, PRICE_RISE_CUE_RE,
    CLOSED_VALUE_WORDS, MEMBER_ASK_RE, MEMBER_LIST_CUES, MEMBER_LIST_NEGATION_CUES, MONEY_QUESTION_WORDS, PRICE_RISE_RE, PRICE_THOUSAND_UNITS, REGIONAL_WORD_VARIANTS,
)
from bizreason.semantic.compiler import DEBT_MEASURES, Answer, Compiler, Intent, NotRecorded, Unsupported
from bizreason.semantic import rule_terms, rulebook_params, stock_sources
from bizreason.semantic.quality import QualityFlags, assess
from bizreason.semantic.vocabulary import DIMENSIONS
from bizreason.semantic.vocabulary import (
    LARGE_UNIT_LOOSE_CUE, LARGE_UNIT_WHOLE_CUES, LOT_MEASURES, QUOTED_DIMENSIONS, STOCK_MEASURES, TABLE_ASK_CUES,
    CAUSE_WORD, DELIVERY_WORDS, MOST_ORDERS_CUE, PURCHASE_MINUS_SOLD_RE, RULE_DELIVERY_TRIPS, RULE_TICKET_VISITS, PERCENT_CUES, TRIP_WORDS, UNDEFINED_JUDGEMENT_WORDS,
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
# Quantity measures read from the sale lines: never the answer to how much the delivery book shows was delivered.
DELIVERED_QTY_GUARD = {"qty_base_sold", "qty_sold"}
# Measures that are only ever an amount of money; asked without any money word, the parse chose the wrong measure.
MONEY_ONLY_MEASURES = {"expiring_lot_value"}
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
_CONTRACT_RUNTIME: Any = None
_TIME_READER: Any = None
# Phrase-triggered repairs learned from seen questions (most-orders ranking, cancellation share and cause, delivery
# trips for an order count). Switched off by run_v2 --phrase-rules off for ablation.
PHRASE_RULES = True


def set_fallback(fallback: Any) -> None:
    """The SqlFallback the solvers use (None disables it). Set once per run, before questions are solved."""
    global _FALLBACK
    _FALLBACK = fallback


def set_time_reader(reader: Any) -> None:
    """The TimeReader used only when the temporal rules give no window (None disables it)."""
    global _TIME_READER
    _TIME_READER = reader


def set_contract_runtime(runtime: Any) -> None:
    """Set the experimental evidence-backed path; None preserves legacy routing."""
    global _CONTRACT_RUNTIME
    _CONTRACT_RUNTIME = runtime


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


def _has_list_cue(asked: str) -> bool:
    """True when the padded, normalized question holds a list cue; one mistyped letter in the cue's last word is
    tolerated (questions are typed fast), the first word must be exact."""
    words = asked.split()
    for cue in MEMBER_LIST_CUES:
        head, *rest = cue.split()
        for i, word in enumerate(words):
            if word != head or i + len(rest) >= len(words):
                continue
            nxt = words[i + 1:i + 1 + len(rest)]
            if all(a == b or (len(a) == len(b) and len(a) >= 4 and sum(x != y for x, y in zip(a, b)) <= 1)
                   for a, b in zip(nxt, rest)):
                return True
    return False


def _named_closed_members(asked: str, intent: Intent) -> Intent:
    """Members of a closed dimension (payment method, channel) in the order the question writes them.

    When the question writes two or more known words of one such dimension and the intent filters only that dimension
    (or names fewer members than the question), the filters become the written members, each as its canonical value.
    The member must still resolve in the shop's data afterwards. Intents with filters on other closed dimensions,
    or with a member the words do not explain, are left as they are."""
    for dim, codes in CLOSED_VALUE_WORDS.items():
        found: list[tuple[int, str]] = []
        for code, words in codes.items():
            hits = [asked.find(f" {w} ") for w in words if f" {w} " in asked]
            if hits:
                found.append((min(hits), code))
        if len(found) < 2:
            continue
        mine = [f for f in intent.filters if f.get("dim") == dim]
        if len(mine) >= 2:
            return intent  # the intent already names members; the written words do not replace them
        if any(f.get("dim") in CLOSED_VALUE_WORDS and f.get("dim") != dim for f in intent.filters):
            return intent
        if intent.group_by not in (None, dim) or not mine and intent.group_by != dim:
            return intent
        ordered = [code for _, code in sorted(found)]
        rest = [f for f in intent.filters if f.get("dim") != dim]
        return replace(intent, filters=[*rest, *({"dim": dim, "value": c, "exclude": False} for c in ordered)])
    return intent


def _weekday_from_question(question: str, intent: Intent) -> dict[str, Any] | None:
    """A weekday filter for an 'average per weekday' intent that has none, when the question names one weekday."""
    from bizreason.semantic.compiler import WEEKDAY_TOKENS, _asks_weekday_average
    if not _asks_weekday_average(intent) or any(f.get("dim") == "weekday" for f in intent.filters):
        return None
    padded = f" {re.sub(NON_WEEKDAY_PHRASE_RE, ' ', normalize_lookup(question))} "
    found = {day: token for token, day in WEEKDAY_TOKENS.items() if f" {token} " in padded}
    if len(found) != 1:
        return None
    return {"dim": "weekday", "value": next(iter(found.values())), "exclude": False}


def _trim_added_words(question: str, intent: Intent) -> Intent | None:
    """When a product filter carries words the question does not have, but starts with a phrase of two or more
    words that the question does write, use that phrase. None when nothing changes."""
    asked = set(normalize_lookup(question).split())
    changed, filters = False, []
    for item in intent.filters:
        value = normalize_lookup(str(item.get("value", "")))
        words = value.split()
        if item.get("dim") == "product" and words and not set(words) <= asked:
            prefix = []
            for word in words:
                if word not in asked:
                    break
                prefix.append(word)
            if len(prefix) >= 2:
                filters.append({**item, "value": " ".join(prefix)})
                changed = True
                continue
        filters.append(item)
    return replace(intent, filters=filters) if changed else None


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
    semantic_contract: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None


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
            if _CONTRACT_RUNTIME is not None:
                decision = _CONTRACT_RUNTIME.answer(question, self.connection)
                if decision.handled:
                    trace.semantic_contract = decision.trace
                    trace.reason = decision.reason
                    if decision.type is not None:
                        trace.decision = "value"
                        return Prediction(id=question.id, type=decision.type, answer=decision.answer), trace
                    # A matched but ungrounded definition cannot escape through a legacy formula or fallback.
                    trace.decision = "abstain"
                    return Prediction(id=question.id, type=AnswerType.ABSTAIN, reason=decision.reason), trace
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
        if PHRASE_RULES and intent.measure == "cancelled_count" and any(f" {cue} " in asked for cue in PERCENT_CUES) or (PHRASE_RULES and
                intent.measure == "cancelled_count" and "%" in question.question):
            # "đơn huỷ chiếm mấy %" asks the cancellation rate, not the count.
            intent = replace(intent, measure="cancel_rate")
            trace.intent = intent
        if (PHRASE_RULES and intent.shape in ("argmax_entity", "top_k") and f" {MOST_ORDERS_CUE} " in asked
                and intent.measure in ("revenue", "qty_base_sold")):
            # "ai mua nhiều đơn nhất" ranks by orders, whatever money or quantity the parse chose.
            intent = replace(intent, measure="order_count")
            trace.intent = intent
        threshold = intent.threshold or {}
        if (intent.shape in ("threshold_count", "ratio_of_counts") and threshold.get("subject") in ("order", None)
                and any(f" {w} " in asked for w in DELIVERY_WORDS) and any(f" {w} " in asked for w in TRIP_WORDS)
                and rulebook_params.mentions(*RULE_DELIVERY_TRIPS)):
            # "mấy đơn đã được giao từ 2 đợt trở lên": a threshold on each order's delivery trips.
            intent = replace(intent, threshold={**threshold, "subject": "order", "metric": "delivery_trips"})
            trace.intent = intent
        if PHRASE_RULES and intent.measure == "order_count" and any(f" {w} " in asked for w in DELIVERY_WORDS) and any(
                f" {w} " in asked for w in TRIP_WORDS) and rulebook_params.mentions(*RULE_DELIVERY_TRIPS):
            # "xe đã giao mấy đợt hàng" counts delivery trips, not orders (rulebook: giao nhiều đợt).
            intent = replace(intent, measure="delivery_trip_count")
            trace.intent = intent
        if (intent.measure in DELIVERED_QTY_GUARD and any(f" {w} " in asked for w in DELIVERY_WORDS)
                and self._has_rows("deliveries")):
            # "đã chở đi / đã giao bao nhiêu thùng" asks what the delivery book shows up to today, which the sold
            # quantity of the orders is not (orders may be delivered later or in part). No measure reads delivered
            # quantities, so answering with the sold quantity would be a confident wrong answer.
            return self._finish(question, trace, "abstain", "delivered quantity is not modelled")
        if intent.measure in MONEY_ONLY_MEASURES and not any(f" {w} " in asked for w in MONEY_QUESTION_WORDS):
            # A money measure for a question that names no money ("còn mấy lô sắp hết hạn") asks something else.
            return self._finish(question, trace, "abstain", f"{intent.measure} for a question about no money")
        if re.search(PURCHASE_MINUS_SOLD_RE, asked) and any(f.get("dim") == "product" for f in intent.filters):
            # "số nhập trừ số bán" / "chênh lệch nhập trừ bán" (rulebook §5): received minus sold, one product.
            intent = replace(intent, measure="purchase_minus_sold", measure2=None, shape="scalar", group_by=None)
            trace.intent = intent
        if (intent.measure == "delivery_trip_count" and not self._has_rows("deliveries")
                and rulebook_params.mentions(*RULE_TICKET_VISITS)):
            # No delivery book: "lượt xe" in a repair book is one ticket per visit (rulebook, sửa xe).
            intent = replace(intent, measure="service_ticket_count")
            trace.intent = intent
        cause_at = re.search(rf" {CAUSE_WORD} (?!x |\d)(\S+(?: \S+){{0,2}})", asked)  # not "vỉ x 4", "vỉ 10"
        if PHRASE_RULES and intent.measure in ("cancelled_count", "cancel_rate") and cause_at:
            cause = cause_at.group(1).split()
            if cause and not self._notes_mention(cause):
                # A cancellation or return "vì <reason>": no file records the reason (rulebook §7).
                return self._finish(question, trace, "unanswerable", "reason for cancellation not recorded")
            return self._finish(question, trace, "abstain", "reason for cancellation")
        moved = self._columns_from_filters(intent)
        if moved is not None:
            intent = moved
            trace.intent = intent
        weekday = _weekday_from_question(question.question, intent)
        if weekday is not None:
            intent = replace(intent, filters=[*intent.filters, weekday])
            trace.intent = intent
        variant = self._product_variants(intent)
        if variant is not None:
            intent = variant
            trace.intent = intent
        unit_free = self._drop_redundant_units(intent)
        if unit_free is not None:
            intent = unit_free
            trace.intent = intent
        trimmed = _trim_added_words(question.question, intent)
        if trimmed is not None:
            # The parse added words the asker did not write (a stored variant copied from the value list): keep the
            # asker's own phrase so every product it names is counted.
            intent = trimmed
            trace.intent = intent
        grounded = None if intent.column_measure else self._closed_value_named(question.question, intent)
        if grounded is not None:
            intent = replace(intent, filters=[*intent.filters, grounded])
            trace.intent = intent
        named = self._named_person(question.question, intent)
        if named is not None:
            # The question names one stored employee or customer in full that the parse left out (names typed without
            # accents are often missed): without the filter the answer would cover the whole shop.
            intent = replace(intent, filters=[*intent.filters, named])
            trace.intent = intent
        ungrounded = _ungrounded_dim(question.question, intent.filters)
        if ungrounded is not None:
            return self._finish(question, trace, "abstain", f"filter not in question: {ungrounded}")
        if intent.measure == "outside" and not intent.column_measure:
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
        if (not windows and _TIME_READER is not None and intent.measure not in POINT_IN_TIME
                and not intent.column_measure and re.search(r"\d", question.question)):
            # Only a question that writes some number (a day, a month, N days) can name a period the rules missed; a
            # question with no time phrase keeps the rulebook defaults below (a balance at the end of today).
            # The rules read nothing: the model reads the periods and code keeps them only if every boundary is grounded.
            windows = _TIME_READER.read(question.question, question.as_of_date) or windows
        if intent.shape == "compare_periods":
            # A price-rise comparison fixes both windows itself, whatever single window the text alone gave.
            windows = self._price_rise_windows(question.question, intent) or windows
        relative = {"after_today", "before_today", "on_or_before_today", "on_or_after_today"}
        if intent.column_measure and any(f.get("op") in relative for f in intent.column_filters):
            # A condition measured against today ("ngày phải trả sau hôm nay") reads everything up to today.
            windows = []
        if not windows and intent.column_measure:
            # "tới hôm nay ..." on a kept column: everything recorded up to the end of today.
            windows = [TimeWindow(role="target", start=date(1900, 1, 1),
                                  end_exclusive=question.as_of_date + timedelta(days=1), evidence=["as_of_date"])]
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
        intent = self._months_not_weekdays(question.question, intent, windows)
        trace.intent = intent
        wants_table = intent.group_by is not None and asks_entity_values(question.question)
        try:
            compiler = Compiler(self.connection)
            compiler.as_of = question.as_of_date
            listed = self._member_list(compiler, question.question, intent, windows)
            if listed is not None:
                answer = listed
            elif intent.measure == "purchase_minus_sold":
                target = [w for w in windows if w.role != "comparison"]
                if len(target) != 1:
                    raise Unsupported("purchase minus sold needs one window")
                diff = compiler.purchase_minus_sold(intent, target[0])
                answer = Answer("number", int(round(diff)) if diff is not None and abs(diff - round(diff)) < 1e-9
                                else diff)
            elif wants_table and intent.shape == "top_k" and (intent.top_k or 0) >= 2:
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

    @staticmethod
    def _months_not_weekdays(text: str, intent: Intent, windows: list[TimeWindow]) -> Intent:
        """Several whole calendar months with no weekday written is a list per month: the parse that groups the same
        question by weekday (an abbreviated month read as a weekday) is dropped."""
        from bizreason.semantic.compiler import WEEKDAY_TOKENS
        if intent.group_by != "weekday" or intent.shape != "breakdown" or any(f.get("dim") == "weekday" for f in intent.filters):
            return intent
        target = [w for w in windows if w.role != "comparison"]
        if len(target) < 2 or len(target) != len(windows):
            return intent
        months = {(w.start.year, w.start.month) for w in target}
        if len(months) != len(target) or any(
                w.start.day != 1 or (w.end_exclusive - timedelta(days=1)).month != w.start.month
                or w.end_exclusive.day != 1 for w in target):
            return intent
        padded = f" {re.sub(NON_WEEKDAY_PHRASE_RE, ' ', normalize_lookup(text))} "
        if any(f" {token} " in padded for token in WEEKDAY_TOKENS):
            return intent
        return replace(intent, group_by=None, compare_as="percent")

    @staticmethod
    def _member_list(compiler: Compiler, text: str, intent: Intent, windows: list[TimeWindow]) -> Answer | None:
        """'A và B lần lượt bao nhiêu': one value per member of one dimension, in the order the intent names them.

        Only when the question says so (a list cue, no exclusion word), the intent names two or more members of exactly
        one dimension, and one window applies. The parse often marks a member as an exclusion or adds a second measure;
        here every named member is listed. Each member must resolve in the shop's own data: a member of a closed
        dimension the shop does not record makes the whole question unanswerable (the compiler raises NotRecorded)."""
        asked = f" {normalize_lookup(text)} "
        if not (_has_list_cue(asked) or len(re.findall(MEMBER_ASK_RE, asked)) >= 2) or any(cue in asked for cue in MEMBER_LIST_NEGATION_CUES):
            return None
        if (intent.column_measure or intent.column_filters or intent.top_k is not None or intent.threshold is not None
                or intent.location is not None or intent.age_days is not None):
            return None
        if intent.measure2 is not None and intent.measure2 != intent.measure:
            return None
        if intent.shape not in ("breakdown", "compare_periods", "scalar"):
            return None
        target = [w for w in windows if w.role != "comparison"]
        if len(target) != 1 or len(target) != len(windows):
            return None
        intent = _named_closed_members(asked, intent)
        positions: dict[Any, list[int]] = {}
        for index, item in enumerate(intent.filters):
            positions.setdefault(item.get("dim"), []).append(index)
        repeated = {dim: at for dim, at in positions.items() if len(at) > 1}
        if len(repeated) != 1:
            return None
        dim, at = next(iter(repeated.items()))
        if intent.group_by not in (None, dim):
            return None
        if len({normalize_lookup(str(intent.filters[i].get("value"))) for i in at}) != len(at):
            return None
        others = [f for i, f in enumerate(intent.filters) if i not in at]
        values: list[Any] = []
        for i in at:
            member = {**intent.filters[i], "exclude": False}
            member_intent = replace(intent, filters=[*others, member], shape="scalar", measure2=None, group_by=None,
                                    compare_as="percent")
            answer = compiler.answer(member_intent, windows)
            if answer.type != "number" or answer.value is None:
                raise Unsupported("member value is not a number")
            values.append(answer.value)
        return Answer("list", values)

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
        target_windows = [w for w in windows if w.role != "comparison"]
        if len(target_windows) == 1 and stock_sources.is_expiry_range_list(
                intent, target_windows[0].start, target_windows[0].end_exclusive):
            return self._decide_lots(question, trace, replace(intent, measure="expiry_lot"), windows)
        split = asks_unit_split(question.question) and intent.measure == "inventory_qty"
        if split and intent.threshold is None:
            # A two-unit split reads its units from the question text (stock_split), not from the intent's measure2.
            intent = replace(intent, measure2=None, group_by=None, shape="scalar")
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
            value = Compiler(self.connection).stock(intent, target[0], split=split, question_text=question.question)
        except NotRecorded as exc:
            return self._finish(question, trace, "unanswerable", str(exc))
        except Unsupported as exc:
            return self._finish(question, trace, "abstain", str(exc) or "unsupported")
        except Exception as exc:
            return self._finish(question, trace, "abstain", type(exc).__name__)
        trace.decision = "value"
        return Prediction(id=question.id, type=AnswerType(value.type), answer=value.value), trace

    def _product_variants(self, intent: Intent) -> Intent | None:
        """A product the question writes with one regional word ("lạc") that the shop stores with its variant
        ("đậu phộng"): when the written name matches nothing and exactly one variant spelling matches, use it."""
        compiler = Compiler(self.connection)
        changed, filters = False, []
        for item in intent.filters:
            if item.get("dim") == "product" and not item.get("exclude"):
                try:
                    compiler.resolve_filter("product", item["value"])
                except Unsupported as exc:
                    if str(exc).startswith("unresolved product"):
                        alt = self._variant_spelling(compiler, str(item["value"]))
                        if alt is not None:
                            filters.append({**item, "value": alt})
                            changed = True
                            continue
                except (NotRecorded, sqlite3.Error):
                    pass
            filters.append(item)
        return replace(intent, filters=filters) if changed else None

    @staticmethod
    def _variant_spelling(compiler: Compiler, value: str) -> str | None:
        padded = f" {normalize_lookup(value)} "
        found = set()
        for pair in REGIONAL_WORD_VARIANTS:
            for word, other in (pair, pair[::-1]):
                if f" {word} " in padded:
                    candidate = padded.replace(f" {word} ", f" {other} ").strip()
                    try:
                        compiler.resolve_filter("product", candidate)
                    except (Unsupported, NotRecorded, sqlite3.Error):
                        continue
                    found.add(candidate)
        return found.pop() if len(found) == 1 else None

    def _drop_redundant_units(self, intent: Intent) -> Intent | None:
        """A unit filter on a quantity in base units that every line of the named product already satisfies ("số bình"
        for a product only ever sold by the bình) changes no row, so it is dropped. A unit that differs for any line
        of the product stays a filter. Only for a comparison of periods: a scalar quantity may carry a meaning the unit
        filter alone does not (e.g. what was delivered), which this does not model. None when nothing changes."""
        if intent.measure != "qty_base_sold" or intent.shape != "compare_periods":
            return None
        products = [f for f in intent.filters if f.get("dim") == "product" and not f.get("exclude")]
        units = [f for f in intent.filters if f.get("dim") == "unit" and not f.get("exclude")]
        if len(products) != 1 or not units:
            return None
        compiler = Compiler(self.connection)
        try:
            sql, args, _ = compiler.resolve_filter("product", products[0]["value"])
            base = "base_unit" if "base_unit" in compiler.line_columns else "NULL"
            rows = self.connection.execute(f"SELECT DISTINCT unit, {base} FROM lines WHERE {sql}", args).fetchall()
        except (Unsupported, NotRecorded, sqlite3.Error):
            return None
        counted = {normalize_lookup(base_unit if base_unit is not None else unit) for unit, base_unit in rows}
        if not rows or len(counted) != 1:
            return None
        only = next(iter(counted))
        drop = [f for f in units if normalize_lookup(str(f.get("value", ""))) == only]
        if not drop:
            return None
        return replace(intent, filters=[f for f in intent.filters if not any(f is d for d in drop)])

    def _price_rise_windows(self, text: str, intent: Intent) -> list[TimeWindow]:
        """Rulebook §5 (hiệu ứng tăng giá): the day D of the rise, then N days from D (D included) against the N days
        before D, as [target, comparison]. [] when the question is not one, or D or N is not certain.

        D is the day the question writes (one day, with its year) when it gives one: the question wins over the price
        list (rulebook: a question that says otherwise is followed). Otherwise D is the first price-list day the
        product's price rose to the new price the question names. N is the one number of days the question writes."""
        normalized = normalize_lookup(text)
        found = re.findall(PRICE_RISE_RE, normalized)
        prices = {int(digits.replace(" ", "")) * (1000 if unit in PRICE_THOUSAND_UNITS else 1)
                  for digits, unit in found}
        days = set(re.findall(r"\b(\d{1,3}) ngay\b", normalized))
        if len(days) != 1 or not (found or re.search(PRICE_RISE_CUE_RE, normalized)):
            return []
        n = int(days.pop())
        if n < 1:
            return []
        written = self._question_days(text)
        before = set(re.findall(DAYS_BEFORE_RE, normalized))
        evidence = ["question"]
        start = None
        if len(written) == 1 and before == {str(n)}:
            start = next(iter(written))
        table_day = self._price_rise_day(intent, prices.pop()) if len(prices) == 1 else None
        if start is None:
            start = table_day
        elif table_day is not None and table_day != start:
            evidence.append("question_overrides_price_changes")
        if start is None:
            return []
        if table_day is not None:
            evidence.append("price_changes")
        return [
            TimeWindow(role="target", start=start, end_exclusive=start + timedelta(days=n), evidence=evidence),
            TimeWindow(role="comparison", start=start - timedelta(days=n), end_exclusive=start, evidence=evidence),
        ]

    @staticmethod
    def _question_days(text: str) -> set[date]:
        """The distinct calendar days a question writes day-first with a year; impossible dates are ignored."""
        days = set()
        for day, month, year in re.findall(DMY_DATE_RE, text):
            try:
                days.add(date(int(year) + (2000 if len(year) == 2 else 0), int(month), int(day)))
            except ValueError:
                continue
        return days

    def _price_rise_day(self, intent: Intent, price: int) -> date | None:
        """The first day the one product the intent names rose to `price` in the price list, else None."""
        products = [item for item in intent.filters if item.get("dim") == "product" and not item.get("exclude")]
        if len(products) != 1:
            return None
        try:
            rows = self.connection.execute(
                "SELECT product_id, product_name, date, old_price, new_price FROM price_changes "
                "WHERE date IS NOT NULL ORDER BY date").fetchall()
            ids_to_names: dict[Any, set[str]] = {}
            if any(row[1] is None and row[0] is not None for row in rows):
                # A price list with codes only: the names the sales lines write for each code.
                for code, name in self.connection.execute(
                        "SELECT DISTINCT product_id, product_name FROM lines "
                        "WHERE product_id IS NOT NULL AND product_name IS NOT NULL"):
                    ids_to_names.setdefault(code, set()).add(name)
        except sqlite3.Error:
            return None
        names_of = [({row[1]} if row[1] is not None else ids_to_names.get(row[0], set())) for row in rows]
        every = sorted(set().union(*names_of)) if names_of else []
        chosen = set(Compiler._match("product", every, normalize_lookup(str(products[0].get("value", ""))))) \
            if every else set()
        if not chosen:
            return None
        history = [(row, names) for row, names in zip(rows, names_of) if names & chosen]
        owners = {(row[0] if row[0] is not None else tuple(sorted(normalize_lookup(x) for x in names)))
                  for row, names in history}
        if len(owners) != 1:
            return None
        previous = None
        for (_, _, day, old_price, new_price), _names in history:
            before = old_price if old_price is not None else previous
            if new_price is not None and abs(new_price - price) < 0.5 and before is not None and new_price > before:
                return date.fromisoformat(str(day)[:10])
            previous = new_price if new_price is not None else previous
        return None

    def _catalog(self) -> dict[str, dict[str, Any]]:
        try:
            rows = self.connection.execute(
                "SELECT table_name, column_name, source_header, value_type FROM column_catalog").fetchall()
        except sqlite3.Error:
            return {}
        return {c: {"table": t, "header": h, "type": k} for t, c, h, k in rows}

    def _columns_from_filters(self, intent: Intent) -> Intent | None:
        """Filters the parse put on a kept column name (not a dimension) become column conditions: a date column is
        'in the question's period', anything else 'contains'. A date condition with an order count and no column
        measure counts the rows of that column's table. Filters that only repeat words of the measured column's
        header ('máy cũ' in 'thu máy cũ') are dropped."""
        catalog = self._catalog()
        if not catalog:
            return None
        keep, moved = [], []
        for item in intent.filters:
            dim = item.get("dim")
            if dim not in DIMENSIONS and dim in catalog:
                op = "in_period" if catalog[dim]["type"] == "date" else "contains"
                moved.append({"column": dim, "op": op, "value": item.get("value")})
            else:
                keep.append(item)
        measured = (intent.column_measure or {}).get("column")
        if measured in catalog:
            header = f" {normalize_lookup(str(catalog[measured]['header'] or ''))} "
            keep = [f for f in keep if f" {normalize_lookup(str(f.get('value', '')))} " not in header]
        if not moved and len(keep) == len(intent.filters):
            return None
        column_measure = intent.column_measure
        if column_measure is None and moved and intent.measure in ("order_count", "outside"):
            column_measure = {"op": "count", "column": moved[0]["column"]}
        if column_measure is None and moved:
            return None
        return replace(intent, filters=keep, column_filters=[*intent.column_filters, *moved],
                       column_measure=column_measure, shape="scalar" if column_measure else intent.shape)

    def _closed_value_named(self, text: str, intent: Intent) -> dict[str, Any] | None:
        """A filter for the one stored value of a short closed list (channel, payment method, customer group, product
        kind, a rulebook shift) that the question writes verbatim but the intent neither filters nor groups by."""
        asked = f" {normalize_lookup(text)} "
        lists = {"shift": list(rulebook_params.active().shift_windows)}
        for dim, table, column in (("channel", "orders", "channel"), ("payment_method", "orders", "payment_method"),
                                   ("customer_group", "orders", "customer_group"),
                                   ("product_kind", "lines", "product_kind")):
            try:
                lists[dim] = [r[0] for r in self.connection.execute(
                    f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL LIMIT 50")]
            except sqlite3.Error:
                continue
        for dim, values in lists.items():
            if intent.group_by == dim or any(item.get("dim") == dim for item in intent.filters):
                continue
            hits = {normalize_lookup(v) for v in values if v is not None and len(normalize_lookup(v)) >= 4
                    and len(normalize_lookup(v).split()) >= 2 and f" {normalize_lookup(v)} " in asked}
            if len(hits) == 1:
                return {"dim": dim, "value": hits.pop(), "exclude": False}
        return None

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

    def _has_rows(self, table: str) -> bool:
        try:
            return self.connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
        except sqlite3.Error:
            return False

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
        if decision == "abstain" and _FALLBACK is not None and reason.startswith(
                getattr(_FALLBACK, "reasons", FALLBACK_REASONS)):
            # The configured fallback validates its own representation before returning an answer.
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
            if getattr(_FALLBACK, "last_diagnostics", None):
                trace.plan = dict(_FALLBACK.last_diagnostics)
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
