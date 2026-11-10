"""Decide one question: parse it, resolve its time windows, check coverage, then compile the answer."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from bizreason.contracts import AnswerType, Prediction, Question, TimeWindow
from bizreason.semantic.compiler import Compiler, Intent, Unsupported
from bizreason.temporal import TemporalResolver

POINT_IN_TIME = {"debt_outstanding", "debtor_count", "inventory_qty", "inventory_value", "out_of_stock_count"}
ORDER_MEASURES = {
    "no_sale_count", "orders_containing_product",
    "revenue", "order_count", "cancelled_count", "cancel_rate", "avg_per_order",
    "qty_base_sold", "distinct_customers", "gross_profit",
}
PERIOD_SHAPES = {"breakdown", "argmax_period", "trend_check"}


class ParseResult(Protocol):
    @property
    def intent(self) -> Intent | None: ...

    @property
    def reason(self) -> str: ...


class IntentParser(Protocol):
    def parse(self, question: str) -> ParseResult: ...


@dataclass
class SolveTrace:
    question_id: str
    intent: Intent | None
    windows: list[TimeWindow]
    decision: str  # "value" | "unanswerable" | "abstain"
    reason: str


class SemanticSolver:
    def __init__(self, parser: IntentParser, temporal: TemporalResolver, connection: sqlite3.Connection):
        self.parser = parser
        self.temporal = temporal
        self.connection = connection

    def solve(self, question: Question) -> tuple[Prediction, SolveTrace]:
        trace = SolveTrace(question_id=question.id, intent=None, windows=[], decision="abstain", reason="")
        try:
            return self._decide(question, trace)
        except Exception as exc:  # one bad question must never crash the run
            return self._finish(question, trace, "abstain", type(exc).__name__)

    def _decide(self, question: Question, trace: SolveTrace) -> tuple[Prediction, SolveTrace]:
        parsed = self.parser.parse(question.question)
        intent = parsed.intent
        trace.intent = intent
        if intent is None:
            return self._finish(question, trace, "abstain", parsed.reason)
        if intent.measure == "outside":
            return self._finish(question, trace, "abstain", "outside known measures")

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
            if targets and all(w.end_exclusive <= low or w.start > high for w in targets):
                return self._finish(
                    question, trace, "unanswerable",
                    f"requested period lies wholly outside the shop's data range {low.isoformat()} to {high.isoformat()}",
                )

        try:
            answer = Compiler(self.connection).answer(intent, windows)
        except Unsupported as exc:
            return self._finish(question, trace, "abstain", str(exc) or "unsupported")
        except Exception as exc:
            return self._finish(question, trace, "abstain", type(exc).__name__)

        prediction = Prediction(id=question.id, type=AnswerType(answer.type), answer=answer.value)
        trace.decision = "value"
        return prediction, trace

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
