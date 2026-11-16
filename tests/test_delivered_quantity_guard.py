"""A sold quantity is not the answer to how much the delivery book shows was delivered (synthetic data only)."""
from __future__ import annotations

from datetime import date

from bizreason.contracts import Question, TimeWindow
from bizreason.semantic.compiler import Intent
from bizreason.semantic.solver import SemanticSolver

from tests.test_semantic_solver import FakeParser, FakeTemporal, ParseResult, make_db


def _intent(measure: str = "qty_base_sold") -> Intent:
    return Intent(measure=measure, shape="scalar", filters=[{"dim": "product", "value": "Paint A", "exclude": False}])


def _solve(text: str, with_deliveries: bool, measure: str = "qty_base_sold"):
    conn = make_db()
    conn.execute("INSERT INTO lines VALUES ('o1','p1','Paint A','paint','can',5,5,100,NULL)")
    conn.execute("CREATE TABLE deliveries (order_id TEXT, date TEXT, qty REAL, trip INTEGER)")
    if with_deliveries:
        conn.execute("INSERT INTO deliveries VALUES ('o1','2026-01-06',3,1)")
    window = TimeWindow(role="target", start=date(2026, 1, 1), end_exclusive=date(2026, 2, 1))
    solver = SemanticSolver(FakeParser(ParseResult(_intent(measure))), FakeTemporal([window]), conn)
    return solver.solve(Question(id="q1", business_id="b1", question=text, as_of_date=date(2026, 3, 1)))


def test_delivered_quantity_question_abstains_when_the_shop_keeps_a_delivery_book():
    prediction, trace = _solve("Các đơn đặt tháng 1/2026, tới hôm nay đã chở đi bao nhiêu can Paint A?", True)
    assert prediction.type.value == "abstain"
    assert trace.reason == "delivered quantity is not modelled"


def test_sold_quantity_question_is_not_blocked_counterexample():
    prediction, trace = _solve("Tháng 1/2026 bán được bao nhiêu can Paint A?", True)
    assert trace.reason != "delivered quantity is not modelled"


def test_shop_without_delivery_rows_is_not_blocked_counterexample():
    prediction, trace = _solve("Tháng 1/2026 đã giao bao nhiêu can Paint A?", False)
    assert trace.reason != "delivered quantity is not modelled"


def test_other_measures_with_delivery_words_are_not_blocked_counterexample():
    prediction, trace = _solve("Tháng 1/2026 đơn giao tận nơi thu bao nhiêu tiền?", True, measure="revenue")
    assert trace.reason != "delivered quantity is not modelled"


def _solve_lots(text: str):
    conn = make_db()
    window = TimeWindow(role="target", start=date(2026, 3, 1), end_exclusive=date(2026, 7, 1))
    solver = SemanticSolver(FakeParser(ParseResult(Intent(measure="expiring_lot_value", shape="scalar"))),
                            FakeTemporal([window]), conn)
    return solver.solve(Question(id="q1", business_id="b1", question=text, as_of_date=date(2026, 3, 1)))


def test_money_measure_for_a_count_question_abstains():
    _, trace = _solve_lots("Hiện còn mấy lô chưa bán hết mà hạn dùng rơi vào 120 ngày tới?")
    assert trace.reason == "expiring_lot_value for a question about no money"


def test_money_measure_for_a_money_question_is_not_blocked_counterexample():
    _, trace = _solve_lots("Mấy lô còn tồn hết hạn trong 120 ngày tới đang đọng bao nhiêu tiền vốn?")
    assert trace.reason != "expiring_lot_value for a question about no money"
