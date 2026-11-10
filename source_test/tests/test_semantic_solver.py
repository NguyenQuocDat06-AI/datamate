from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

import pytest

from bizreason.contracts import AnswerType, Question, TimeWindow
from bizreason.semantic import solver as solver_module
from bizreason.semantic.compiler import Intent, Unsupported
from bizreason.semantic.solver import SemanticSolver
from bizreason.utils import normalize_lookup


@dataclass
class ParseResult:
    intent: Intent | None
    reason: str = ""


class FakeParser:
    def __init__(self, result: ParseResult):
        self.result = result

    def parse(self, question: str) -> ParseResult:
        return self.result


class FakeTemporal:
    def __init__(self, single: list[TimeWindow] | None = None, periods: list[TimeWindow] | None = None):
        self.single = single or []
        self.periods = periods or []
        self.calls: list[str] = []

    def resolve(self, question: str, as_of: date) -> list[TimeWindow]:
        self.calls.append("resolve")
        return list(self.single)

    def resolve_periods(self, question: str, as_of: date) -> list[TimeWindow]:
        self.calls.append("resolve_periods")
        return list(self.periods)


def win(start: str, end: str, role: str = "target") -> TimeWindow:
    return TimeWindow(role=role, start=date.fromisoformat(start), end_exclusive=date.fromisoformat(end))


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.create_function("lookup", 1, lambda v: normalize_lookup(v) if v is not None else None)
    conn.execute("CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, revenue REAL, customer_key TEXT)")
    conn.execute(
        "CREATE TABLE lines (order_id TEXT, product_key TEXT, product_name TEXT, category TEXT, "
        "unit TEXT, qty REAL, qty_base REAL, amount REAL, line_cost REAL)"
    )
    conn.executemany(
        "INSERT INTO orders VALUES (?, ?, ?, ?, ?)",
        [
            ("o1", "2026-01-05", "completed", 100.0, "c1"),
            ("o2", "2026-01-20", "completed", 50.0, "c2"),
            ("o3", "2026-02-03", "cancelled", 30.0, "c1"),
        ],
    )
    conn.commit()
    return conn


def question(text: str = "q") -> Question:
    return Question(id="q1", business_id="b1", question=text, as_of_date=date(2026, 3, 1))


def solve(parser_result: ParseResult, temporal: FakeTemporal, conn: sqlite3.Connection | None = None):
    conn = conn or make_db()
    solver = SemanticSolver(FakeParser(parser_result), temporal, conn)
    return solver.solve(question())


def test_value_answer_for_scalar_revenue():
    intent = Intent(measure="revenue", shape="scalar")
    temporal = FakeTemporal(single=[win("2026-01-01", "2026-02-01")])
    prediction, trace = solve(ParseResult(intent), temporal)
    assert prediction.type == AnswerType.NUMBER
    assert prediction.answer == 150
    assert trace.decision == "value"
    assert trace.intent == intent


def test_parser_none_gives_abstain_with_parser_reason():
    prediction, trace = solve(ParseResult(None, "not understood"), FakeTemporal())
    assert prediction.type == AnswerType.ABSTAIN
    assert prediction.reason == "not understood"
    assert trace.decision == "abstain"


def test_outside_measure_gives_abstain():
    temporal = FakeTemporal(single=[win("2026-01-01", "2026-02-01")])
    prediction, trace = solve(ParseResult(Intent(measure="outside")), temporal)
    assert prediction.type == AnswerType.ABSTAIN
    assert prediction.reason == "outside known measures"
    assert trace.decision == "abstain"


def test_no_window_for_period_measure_gives_abstain():
    prediction, _ = solve(ParseResult(Intent(measure="revenue")), FakeTemporal())
    assert prediction.type == AnswerType.ABSTAIN
    assert prediction.reason == "no time window"


def test_point_in_time_measure_skips_window_rule(monkeypatch: pytest.MonkeyPatch):
    reached: list[str] = []

    class RecordingCompiler:
        def __init__(self, connection):
            pass

        def answer(self, intent, windows):
            reached.append(intent.measure)
            raise Unsupported("point in time")

    monkeypatch.setattr(solver_module, "Compiler", RecordingCompiler)
    prediction, _ = solve(ParseResult(Intent(measure="debt_outstanding")), FakeTemporal())
    assert reached == ["debt_outstanding"]
    assert prediction.type == AnswerType.ABSTAIN
    assert prediction.reason == "point in time"


def test_window_wholly_outside_coverage_gives_unanswerable():
    temporal = FakeTemporal(single=[win("2025-01-01", "2025-02-01")])
    prediction, trace = solve(ParseResult(Intent(measure="revenue")), temporal)
    assert prediction.type == AnswerType.UNANSWERABLE
    assert prediction.reason
    assert trace.decision == "unanswerable"


def test_window_partly_inside_coverage_gives_value():
    # Coverage runs from 2026-01-05 to 2026-02-03; this window starts before and ends inside it.
    temporal = FakeTemporal(single=[win("2025-12-01", "2026-01-10")])
    prediction, trace = solve(ParseResult(Intent(measure="revenue")), temporal)
    assert prediction.type == AnswerType.NUMBER
    assert prediction.answer == 100
    assert trace.decision == "value"


def test_empty_orders_abstains_because_the_build_failed():
    conn = sqlite3.connect(":memory:")
    conn.create_function("lookup", 1, lambda v: normalize_lookup(v) if v is not None else None)
    conn.execute("CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, revenue REAL, customer_key TEXT)")
    conn.execute(
        "CREATE TABLE lines (order_id TEXT, product_key TEXT, product_name TEXT, category TEXT, "
        "unit TEXT, qty REAL, qty_base REAL, amount REAL, line_cost REAL)"
    )
    temporal = FakeTemporal(single=[win("2026-01-01", "2026-02-01")])
    prediction, _ = solve(ParseResult(Intent(measure="revenue")), temporal, conn)
    assert prediction.type == AnswerType.ABSTAIN


def test_unsupported_from_compiler_gives_abstain(monkeypatch: pytest.MonkeyPatch):
    class RejectingCompiler:
        def __init__(self, connection):
            pass

        def answer(self, intent, windows):
            raise Unsupported("no route for this intent")

    monkeypatch.setattr(solver_module, "Compiler", RejectingCompiler)
    temporal = FakeTemporal(single=[win("2026-01-01", "2026-02-01")])
    prediction, trace = solve(ParseResult(Intent(measure="revenue")), temporal)
    assert prediction.type == AnswerType.ABSTAIN
    assert prediction.reason == "no route for this intent"
    assert trace.decision == "abstain"


def test_unexpected_exception_gives_abstain_with_type_name(monkeypatch: pytest.MonkeyPatch):
    class BrokenCompiler:
        def __init__(self, connection):
            pass

        def answer(self, intent, windows):
            raise ZeroDivisionError("boom")

    monkeypatch.setattr(solver_module, "Compiler", BrokenCompiler)
    temporal = FakeTemporal(single=[win("2026-01-01", "2026-02-01")])
    prediction, trace = solve(ParseResult(Intent(measure="revenue")), temporal)
    assert prediction.type == AnswerType.ABSTAIN
    assert prediction.reason == "ZeroDivisionError"
    assert trace.decision == "abstain"


def test_multi_period_shape_calls_resolve_periods_first():
    intent = Intent(measure="revenue", shape="breakdown")
    temporal = FakeTemporal(
        single=[win("2026-01-01", "2026-02-01")],
        periods=[win("2026-01-01", "2026-01-15"), win("2026-01-15", "2026-02-01")],
    )
    prediction, trace = solve(ParseResult(intent), temporal)
    assert temporal.calls == ["resolve_periods"]
    assert len(trace.windows) == 2
    assert prediction.type == AnswerType.LIST
    assert prediction.answer == [100, 50]


def test_multi_period_shape_falls_back_to_resolve_when_no_periods():
    intent = Intent(measure="revenue", shape="argmax_period")
    temporal = FakeTemporal(single=[win("2026-01-01", "2026-02-01")])
    solve(ParseResult(intent), temporal)
    assert temporal.calls == ["resolve_periods", "resolve"]
