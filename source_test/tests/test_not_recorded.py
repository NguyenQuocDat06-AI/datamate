import sqlite3

import pytest

from bizreason.semantic.compiler import Intent, NotRecorded, Unsupported, _uses_customers


def test_not_recorded_is_an_unsupported_kind():
    assert issubclass(NotRecorded, Unsupported)


@pytest.mark.parametrize("intent, expected", [
    (Intent(measure="distinct_customers"), True),
    (Intent(measure="revenue", shape="top_k", group_by="customer", top_k=3), True),
    (Intent(measure="revenue", filters=[{"dim": "customer", "value": "x", "exclude": False}]), True),
    (Intent(measure="distinct_customers", shape="threshold_count",
            threshold={"subject": "customer", "metric": "order_count", "op": "gte", "value": 3}), True),
    (Intent(measure="revenue"), False),
])
def test_uses_customers(intent, expected):
    assert _uses_customers(intent) is expected


def test_undefined_judgement_is_unanswerable_only_when_the_rulebook_is_silent():
    from bizreason.semantic import rulebook_params
    from bizreason.semantic.solver import undefined_judgement
    try:
        rulebook_params.set_rulebook_text(None)
        assert undefined_judgement("Ai là thợ nhiệt tình nhất?") is None
        rulebook_params.set_rulebook_text("| Doanh thu | tổng tiền |")
        assert undefined_judgement("Ai là thợ nhiệt tình nhất?") == "nhiet tinh"
        assert undefined_judgement("Khách nào mua nhiều nhất?") is None
        rulebook_params.set_rulebook_text("| Thợ nhiệt tình nhất | nhiều phiếu nhất |")
        assert undefined_judgement("Ai là thợ nhiệt tình nhất?") is None
    finally:
        rulebook_params.set_rulebook_text(None)


def test_cancellation_reason_and_rate_rules():
    import sqlite3
    from datetime import date
    from bizreason.contracts import AnswerType, Question
    from bizreason.semantic.compiler import Intent
    from bizreason.semantic.solver import SemanticSolver

    class P:
        def __init__(self, intent):
            self.intent, self.reason = intent, ""

    class Parser:
        def __init__(self, intent):
            self.result = P(intent)

        def parse(self, question, choices=None):
            return self.result

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE orders (order_id TEXT, date TEXT, note TEXT)")
    solver = SemanticSolver(Parser(Intent(measure="cancelled_count")), None, con)
    q = lambda text: Question(id="q", business_id="b", question=text, as_of_date=date(2026, 1, 1))
    pred, trace = solver.solve(q("Th3/2025 có mấy đơn bị huỷ vì giao trễ?"))
    assert pred.type == AnswerType.UNANSWERABLE
    pred, trace = solver.solve(q("Th3/2025 có mấy đơn hộp 25 vỉ x 4 viên bị huỷ?"))
    assert trace.reason != "reason for cancellation not recorded"
