"""Decide one question: parse it, resolve its time windows, check coverage, then compile the answer."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

from bizreason.contracts import AnswerType, Prediction, Question, TimeWindow
from bizreason.semantic.compiler import DEBT_MEASURES, Compiler, Intent, Unsupported
from bizreason.semantic.quality import QualityFlags, assess
from bizreason.semantic.vocabulary import QUOTED_DIMENSIONS, TABLE_ASK_CUES
from bizreason.temporal import TemporalResolver
from bizreason.utils import normalize_lookup

POINT_IN_TIME = {"debt_outstanding", "debtor_count", "inventory_qty", "inventory_value", "out_of_stock_count"}
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

        wants_table = intent.group_by is not None and asks_entity_values(question.question)
        try:
            compiler = Compiler(self.connection)
            if wants_table and intent.shape == "top_k" and (intent.top_k or 0) >= 2:
                answer = compiler.table(intent, windows)
            else:
                answer = compiler.answer(intent, windows)
                if wants_table and answer.type == "list":
                    # A plain list cannot carry the value each entity was asked with; abstaining is safer than a wrong list.
                    raise Unsupported("answer shape: table expected")
        except Unsupported as exc:
            return self._finish(question, trace, "abstain", str(exc) or "unsupported")
        except Exception as exc:
            return self._finish(question, trace, "abstain", type(exc).__name__)

        prediction = Prediction(id=question.id, type=AnswerType(answer.type), answer=answer.value)
        trace.decision = "value"
        return prediction, trace

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
        trace.decision = decision
        trace.reason = reason
        if decision == "unanswerable":
            prediction = Prediction(id=question.id, type=AnswerType.UNANSWERABLE, reason=reason)
        else:
            prediction = Prediction(id=question.id, type=AnswerType.ABSTAIN, reason=reason)
        return prediction, trace
