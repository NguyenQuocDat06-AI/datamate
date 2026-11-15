"""The time reader hook in the real solver path (synthetic data, fake reader; no model)."""
from __future__ import annotations

from datetime import date

import pytest

from bizreason.contracts import Question, TimeWindow
from bizreason.semantic import solver as solver_module
from bizreason.semantic.compiler import Intent
from bizreason.semantic.solver import SemanticSolver

from tests.test_semantic_solver import FakeParser, FakeTemporal, ParseResult, make_db


class FakeReader:
    def __init__(self, windows):
        self.windows, self.calls = windows, 0

    def read(self, question, as_of):
        self.calls += 1
        return self.windows


@pytest.fixture
def reader(monkeypatch):
    fake = FakeReader([TimeWindow(role="target", start=date(2026, 1, 1), end_exclusive=date(2026, 2, 1),
                                  evidence=["question", "as_of_date", "model_time"])])
    monkeypatch.setattr(solver_module, "_TIME_READER", fake)
    return fake


def _solve(text, rule_windows, measure="revenue"):
    solver = SemanticSolver(FakeParser(ParseResult(Intent(measure=measure, shape="scalar"))),
                            FakeTemporal(rule_windows), make_db())
    return solver.solve(Question(id="q1", business_id="b1", question=text, as_of_date=date(2026, 3, 1)))


def test_reader_is_used_when_the_rules_give_no_window(reader):
    prediction, trace = _solve("doanh thu đợt 1/2026 là bao nhiêu", [])
    assert reader.calls == 1
    assert trace.windows and "model_time" in trace.windows[0].evidence
    assert prediction.answer == 150  # o1 + o2 in January, the cancelled February order is out


def test_reader_is_not_called_when_the_rules_give_a_window_counterexample(reader):
    window = TimeWindow(role="target", start=date(2026, 1, 1), end_exclusive=date(2026, 1, 10))
    _, trace = _solve("doanh thu tháng 1/2026", [window])
    assert reader.calls == 0
    assert trace.windows == [window]


def test_reader_is_not_called_for_a_question_with_no_number_counterexample(reader):
    _solve("tổng doanh thu là bao nhiêu", [])
    assert reader.calls == 0


def test_reader_is_not_called_for_a_balance_measure_counterexample(reader):
    measure = next(iter(solver_module.POINT_IN_TIME))
    _solve("còn bao nhiêu tới 1/3/2026", [], measure=measure)
    assert reader.calls == 0
