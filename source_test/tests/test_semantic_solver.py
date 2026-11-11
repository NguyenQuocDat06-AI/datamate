from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

import pytest

from bizreason.contracts import AnswerType, Question, TimeWindow
from bizreason.semantic import solver as solver_module
from bizreason.semantic.compiler import Answer, Intent, Unsupported
from bizreason.semantic.solver import SemanticSolver, _ungrounded_dim, filter_choices
from bizreason.utils import normalize_lookup


@dataclass
class ParseResult:
    intent: Intent | None
    reason: str = ""


class FakeParser:
    def __init__(self, result: ParseResult):
        self.result = result
        self.choices: dict[str, list[str]] | None = None

    def parse(self, question: str, choices: dict[str, list[str]] | None = None) -> ParseResult:
        self.choices = choices
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


def solve(parser_result: ParseResult, temporal: FakeTemporal, conn: sqlite3.Connection | None = None,
          text: str = "q"):
    conn = conn or make_db()
    solver = SemanticSolver(FakeParser(parser_result), temporal, conn)
    return solver.solve(question(text))


def add_facts(conn: sqlite3.Connection, **facts: object) -> None:
    conn.execute("CREATE TABLE shop_facts (key TEXT, value TEXT)")
    conn.executemany("INSERT INTO shop_facts VALUES (?, ?)", [(k, str(v)) for k, v in facts.items()])


class RecordingCompiler:
    """Stands in for the compiler; records that the solver reached it and answers with a fixed value."""

    reached: list[str] = []

    def __init__(self, connection):
        pass

    def answer(self, intent, windows):
        RecordingCompiler.reached.append(intent.measure)
        raise Unsupported("reached compiler")


@pytest.fixture
def reached(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    RecordingCompiler.reached = []
    monkeypatch.setattr(solver_module, "Compiler", RecordingCompiler)
    return RecordingCompiler.reached


def test_parser_receives_stored_choices_from_the_database():
    parser = FakeParser(ParseResult(None, "x"))
    conn = make_db()
    conn.execute("ALTER TABLE orders ADD COLUMN channel TEXT")
    conn.execute("UPDATE orders SET channel = 'shop'")
    conn.commit()
    SemanticSolver(parser, FakeTemporal(), conn).solve(question())
    assert parser.choices == {"channel": ["shop"]}


def test_filter_choices_lists_distinct_values_per_dimension():
    conn = make_db()
    conn.execute("ALTER TABLE orders ADD COLUMN channel TEXT")
    conn.execute("ALTER TABLE orders ADD COLUMN customer_group TEXT")
    conn.execute("ALTER TABLE orders ADD COLUMN payment_method TEXT")
    conn.execute("UPDATE orders SET channel = 'shop', customer_group = 'vip', payment_method = 'cash' WHERE order_id = 'o1'")
    conn.execute("UPDATE orders SET channel = 'shop', payment_method = 'card' WHERE order_id = 'o2'")
    conn.execute("INSERT INTO lines VALUES ('o1', 'p', 'name', 'drinks', 'u', 1, 1, 1, 0)")
    conn.execute("INSERT INTO lines VALUES ('o1', 'p2', 'name2', NULL, 'u', 1, 1, 1, 0)")
    conn.commit()
    assert filter_choices(conn) == {
        "channel": ["shop"],
        "customer_group": ["vip"],
        "payment_method": ["card", "cash"],
        "product_group": ["drinks"],
    }


def test_filter_choices_is_capped_at_forty_values():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE orders (order_id TEXT, channel TEXT)")
    conn.executemany("INSERT INTO orders VALUES (?, ?)", [(f"o{i}", f"ch{i:02d}") for i in range(50)])
    values = filter_choices(conn)["channel"]
    assert len(values) == 40
    assert values[0] == "ch00"
    assert values[-1] == "ch39"


def test_filter_choices_tolerates_missing_tables_and_columns():
    empty = sqlite3.connect(":memory:")
    assert filter_choices(empty) == {}

    partial = sqlite3.connect(":memory:")
    partial.execute("CREATE TABLE orders (order_id TEXT, date TEXT, channel TEXT)")
    partial.execute("INSERT INTO orders VALUES ('o1', '2026-01-05', 'shop')")
    assert filter_choices(partial) == {"channel": ["shop"]}


def test_location_without_address_on_a_shop_with_orders_is_unanswerable():
    conn = make_db()
    add_facts(conn, has_address=0, has_orders=1)
    prediction, trace = solve(ParseResult(Intent(measure="order_count", location="Main street")),
                              FakeTemporal(single=[win("2026-01-01", "2026-02-01")]), conn)
    assert prediction.type == AnswerType.UNANSWERABLE
    assert prediction.reason == "no address recorded"
    assert trace.decision == "unanswerable"


def test_location_with_address_recorded_abstains_as_unsupported(reached: list[str]):
    conn = make_db()
    add_facts(conn, has_address=1, has_orders=1)
    prediction, _ = solve(ParseResult(Intent(measure="revenue", location="Main street")),
                          FakeTemporal(single=[win("2026-01-01", "2026-02-01")]), conn)
    assert prediction.type == AnswerType.ABSTAIN
    assert prediction.reason == "location filter unsupported"
    assert reached == []


def test_location_with_missing_shop_facts_abstains(reached: list[str]):
    prediction, _ = solve(ParseResult(Intent(measure="revenue", location="Main street")),
                          FakeTemporal(single=[win("2026-01-01", "2026-02-01")]))
    assert prediction.type == AnswerType.ABSTAIN
    assert prediction.reason == "location filter unsupported"
    assert reached == []


def test_comparison_wholly_outside_coverage_is_unanswerable(reached: list[str]):
    conn = make_db()
    add_facts(conn, has_orders=1)
    temporal = FakeTemporal(single=[
        win("2026-01-01", "2026-02-01"),
        win("2025-01-01", "2025-02-01", role="comparison"),
    ])
    prediction, trace = solve(ParseResult(Intent(measure="revenue", shape="compare_periods")), temporal, conn)
    assert prediction.type == AnswerType.UNANSWERABLE
    assert "comparison" in prediction.reason
    assert trace.decision == "unanswerable"
    assert reached == []


def test_comparison_outside_coverage_is_unanswerable_when_shop_facts_is_missing(reached: list[str]):
    temporal = FakeTemporal(single=[
        win("2026-01-01", "2026-02-01"),
        win("2025-01-01", "2025-02-01", role="comparison"),
    ])
    prediction, _ = solve(ParseResult(Intent(measure="revenue", shape="compare_periods")), temporal)
    assert prediction.type == AnswerType.UNANSWERABLE


def test_comparison_outside_coverage_is_not_unanswerable_when_orders_are_not_recorded(reached: list[str]):
    conn = make_db()
    add_facts(conn, has_orders=0)
    temporal = FakeTemporal(single=[
        win("2026-01-01", "2026-02-01"),
        win("2025-01-01", "2025-02-01", role="comparison"),
    ])
    prediction, _ = solve(ParseResult(Intent(measure="revenue", shape="compare_periods")), temporal, conn)
    assert prediction.type != AnswerType.UNANSWERABLE
    assert reached == ["revenue"]


def test_comparison_partly_inside_coverage_is_not_unanswerable(reached: list[str]):
    temporal = FakeTemporal(single=[
        win("2026-01-01", "2026-02-01"),
        win("2025-12-01", "2026-01-10", role="comparison"),
    ])
    solve(ParseResult(Intent(measure="revenue", shape="compare_periods")), temporal)
    assert reached == ["revenue"]


def test_customer_group_filter_not_recorded_is_unanswerable(reached: list[str]):
    conn = make_db()
    add_facts(conn, has_customer_group=0, has_orders=1)
    intent = Intent(measure="revenue", filters=[{"dim": "customer_group", "value": "vip", "exclude": False}])
    prediction, trace = solve(ParseResult(intent), FakeTemporal(single=[win("2026-01-01", "2026-02-01")]), conn,
                              text="doanh thu khach vip")
    assert prediction.type == AnswerType.UNANSWERABLE
    assert prediction.reason == "customer group not recorded"
    assert trace.decision == "unanswerable"
    assert reached == []


def test_customer_group_breakdown_not_recorded_is_unanswerable(reached: list[str]):
    conn = make_db()
    add_facts(conn, has_customer_group=0, has_orders=1)
    intent = Intent(measure="revenue", shape="breakdown", group_by="customer_group")
    prediction, _ = solve(ParseResult(intent), FakeTemporal(single=[win("2026-01-01", "2026-02-01")]), conn)
    assert prediction.type == AnswerType.UNANSWERABLE
    assert prediction.reason == "customer group not recorded"


def test_customer_group_recorded_reaches_compiler(reached: list[str]):
    conn = make_db()
    add_facts(conn, has_customer_group=1, has_orders=1)
    intent = Intent(measure="revenue", filters=[{"dim": "customer_group", "value": "vip", "exclude": False}])
    solve(ParseResult(intent), FakeTemporal(single=[win("2026-01-01", "2026-02-01")]), conn,
          text="doanh thu khach vip")
    assert reached == ["revenue"]


def test_customer_group_rule_needs_orders_recorded(reached: list[str]):
    conn = make_db()
    add_facts(conn, has_customer_group=0, has_orders=0)
    intent = Intent(measure="revenue", filters=[{"dim": "customer_group", "value": "vip", "exclude": False}])
    solve(ParseResult(intent), FakeTemporal(single=[win("2026-01-01", "2026-02-01")]), conn,
          text="doanh thu khach vip")
    assert reached == ["revenue"]


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


def add_stats(conn: sqlite3.Connection, **stats: object) -> None:
    conn.execute("CREATE TABLE build_stats (key TEXT, value TEXT)")
    conn.executemany("INSERT INTO build_stats VALUES (?, ?)", [(k, str(v)) for k, v in stats.items()])


def test_broken_quantity_abstains_for_a_quantity_measure(reached: list[str]):
    conn = make_db()
    add_stats(conn, qty_cells_nonblank=1000, qty_cells_unparsed=50)
    prediction, trace = solve(ParseResult(Intent(measure="qty_base_sold")),
                              FakeTemporal(single=[win("2026-01-01", "2026-02-01")]), conn)
    assert prediction.type == AnswerType.ABSTAIN
    assert prediction.reason.startswith("data quality: qty_unreliable:")
    assert trace.decision == "abstain"
    assert trace.quality == ["qty_unreliable"]
    assert reached == []


def test_broken_quantity_abstains_for_a_unit_filter(reached: list[str]):
    conn = make_db()
    add_stats(conn, qty_cells_nonblank=1000, qty_cells_unparsed=50)
    intent = Intent(measure="order_count", filters=[{"dim": "unit", "value": "box", "exclude": False}])
    prediction, _ = solve(ParseResult(intent), FakeTemporal(single=[win("2026-01-01", "2026-02-01")]), conn,
                          text="so don box")
    assert prediction.type == AnswerType.ABSTAIN
    assert prediction.reason.startswith("data quality: qty_unreliable:")
    assert reached == []


def test_broken_quantity_does_not_stop_a_measure_that_does_not_use_quantity():
    conn = make_db()
    add_stats(conn, qty_cells_nonblank=1000, qty_cells_unparsed=50)
    prediction, trace = solve(ParseResult(Intent(measure="revenue")),
                              FakeTemporal(single=[win("2026-01-01", "2026-02-01")]), conn)
    assert prediction.type == AnswerType.NUMBER
    assert prediction.answer == 150
    assert trace.quality == ["qty_unreliable"]


def test_broken_money_does_not_stop_an_order_count():
    conn = make_db()
    add_stats(conn, money_cells_nonblank=200, money_cells_unparsed=20)
    prediction, _ = solve(ParseResult(Intent(measure="order_count")),
                          FakeTemporal(single=[win("2026-01-01", "2026-02-01")]), conn)
    assert prediction.type == AnswerType.NUMBER
    assert prediction.answer == 2


def test_broken_money_abstains_for_a_money_measure(reached: list[str]):
    conn = make_db()
    add_stats(conn, money_cells_nonblank=200, money_cells_unparsed=20)
    prediction, _ = solve(ParseResult(Intent(measure="revenue")),
                          FakeTemporal(single=[win("2026-01-01", "2026-02-01")]), conn)
    assert prediction.type == AnswerType.ABSTAIN
    assert prediction.reason.startswith("data quality: money_unreliable:")
    assert reached == []


def test_broken_order_structure_abstains_for_an_order_measure(reached: list[str]):
    conn = make_db()
    add_stats(conn, product_rows_without_order=3)
    prediction, trace = solve(ParseResult(Intent(measure="revenue")),
                              FakeTemporal(single=[win("2026-01-01", "2026-02-01")]), conn)
    assert prediction.type == AnswerType.ABSTAIN
    assert prediction.reason.startswith("data quality: order_structure_broken:")
    assert trace.quality == ["order_structure_broken"]
    assert reached == []


def test_unlinked_payments_abstain_for_a_debt_measure(reached: list[str]):
    conn = make_db()
    conn.execute("CREATE TABLE payments (order_id TEXT, date TEXT, amount REAL, method TEXT)")
    conn.execute("INSERT INTO payments (order_id, amount) VALUES ('o1', 10)")
    prediction, trace = solve(ParseResult(Intent(measure="debt_outstanding")), FakeTemporal(), conn)
    assert prediction.type == AnswerType.ABSTAIN
    assert prediction.reason.startswith("data quality: payments_unlinked:")
    assert trace.quality == ["payments_unlinked"]
    assert reached == []


def test_quality_is_assessed_once_per_solver(monkeypatch: pytest.MonkeyPatch):
    calls: list[int] = []
    real_assess = solver_module.assess

    def counting_assess(connection):
        calls.append(1)
        return real_assess(connection)

    monkeypatch.setattr(solver_module, "assess", counting_assess)
    solver = SemanticSolver(FakeParser(ParseResult(Intent(measure="revenue"))),
                            FakeTemporal(single=[win("2026-01-01", "2026-02-01")]), make_db())
    solver.solve(question())
    solver.solve(question())
    assert len(calls) == 1


def test_missing_build_stats_leaves_answers_unchanged():
    prediction, trace = solve(ParseResult(Intent(measure="revenue")),
                              FakeTemporal(single=[win("2026-01-01", "2026-02-01")]))
    assert prediction.type == AnswerType.NUMBER
    assert prediction.answer == 150
    assert trace.quality == []


def test_invented_customer_group_gives_abstain_end_to_end(reached: list[str]):
    conn = make_db()
    add_facts(conn, has_customer_group=1, has_orders=1)
    intent = Intent(measure="revenue", filters=[
        {"dim": "size", "value": "m", "exclude": False},
        {"dim": "customer_group", "value": "si", "exclude": False},
    ])
    prediction, trace = solve(ParseResult(intent), FakeTemporal(single=[win("2026-01-01", "2026-02-01")]), conn,
                              text="quan ban dc bnhiu ly size m trong thang 10/2025")
    assert prediction.type == AnswerType.ABSTAIN
    assert trace.decision == "abstain"
    assert trace.reason == "filter not in question: customer_group"
    assert trace.intent is intent
    assert reached == []


def test_ungrounded_quoted_filter_is_named():
    filters = [{"dim": "size", "value": "m", "exclude": False}, {"dim": "customer_group", "value": "si", "exclude": False}]
    assert _ungrounded_dim("quan ban dc bnhiu ly size m trong thang 10/2025", filters) == "customer_group"


def test_quoted_filter_matches_despite_diacritics():
    filters = [{"dim": "customer_group", "value": "sỉ", "exclude": False}]
    assert _ungrounded_dim("Doanh thu khách sỉ tháng 3", filters) is None


def test_canonical_shift_value_is_not_checked():
    filters = [{"dim": "shift", "value": "afternoon", "exclude": False}]
    assert _ungrounded_dim("doanh thu ca chiều", filters) is None


def test_canonical_channel_value_is_not_checked():
    filters = [{"dim": "channel", "value": "phone", "exclude": False}]
    assert _ungrounded_dim("dthu don qua dien thoai", filters) is None


def test_product_punctuation_and_diacritics_are_ignored():
    filters = [{"dim": "product", "value": "Cà phê sữa (L)", "exclude": False}]
    assert _ungrounded_dim("so ly Ca phe sua (L) ban dc", filters) is None


def test_excluded_quoted_filter_is_checked_too():
    filters = [{"dim": "brand", "value": "acme", "exclude": True}]
    assert _ungrounded_dim("doanh thu tháng 3", filters) == "brand"


def _record_model_failures(conn: sqlite3.Connection, *stages: str) -> None:
    conn.execute("CREATE TABLE model_failures (stage TEXT)")
    conn.executemany("INSERT INTO model_failures VALUES (?)", [(stage,) for stage in stages])


def test_model_failure_in_layer_abstains_before_any_answer(reached: list[str]):
    conn = make_db()
    _record_model_failures(conn, "line_kinds", "status")
    prediction, trace = solve(ParseResult(Intent(measure="revenue", shape="scalar")),
                              FakeTemporal(single=[win("2026-01-01", "2026-02-01")]), conn)
    assert prediction.type == AnswerType.ABSTAIN
    assert prediction.reason == "model stage failed: line_kinds"
    assert trace.decision == "abstain"
    assert reached == []


def test_model_failure_does_not_turn_an_unanswerable_question_into_unanswerable(reached: list[str]):
    conn = make_db()
    add_facts(conn, has_address=0, has_orders=1)
    _record_model_failures(conn, "value_groups:channel")
    prediction, _ = solve(ParseResult(Intent(measure="order_count", location="Main street")),
                          FakeTemporal(single=[win("2026-01-01", "2026-02-01")]), conn)
    assert prediction.type == AnswerType.ABSTAIN
    assert prediction.reason == "model stage failed: value_groups:channel"


def test_empty_model_failures_table_leaves_answers_unchanged():
    conn = make_db()
    _record_model_failures(conn)
    prediction, trace = solve(ParseResult(Intent(measure="revenue", shape="scalar")),
                              FakeTemporal(single=[win("2026-01-01", "2026-02-01")]), conn)
    assert prediction.type == AnswerType.NUMBER
    assert prediction.answer == 150
    assert trace.decision == "value"


# Table cues: wording that asks for a value beside each entity of a ranked list.
@pytest.mark.parametrize("text", [
    "Top 3 khách hàng có công nợ lớn nhất và số nợ của từng người?",
    "Liệt kê 5 khách nợ nhiều nhất, kèm số tiền còn nợ.",
    "5 khách nào đang nợ nhiều nhất, mỗi người còn nợ bao nhiêu?",
    "3 khách nợ nhiều nhất, moi nguoi con no bnhiu",
    "Cho bảng 3 khách còn nợ, kèm số tiền",
])
def test_table_cue_is_found_in_entity_value_questions(text: str) -> None:
    assert solver_module.asks_entity_values(text)


@pytest.mark.parametrize("text", [
    "Doanh thu từng tháng của 3 tháng 11/2025 lần lượt là bao nhiêu?",
    "Top 3 khách hàng nợ nhiều nhất tính đến hết ngày 14/08/2026",
    "Doanh thu tháng này là bao nhiêu?",
    "Số lượng đơn hủy là bao nhiêu, kể cả đơn trả?",
])
def test_table_cue_is_not_found_in_other_questions(text: str) -> None:
    assert not solver_module.asks_entity_values(text)


def test_list_answer_with_table_cue_abstains_for_answer_shape(monkeypatch: pytest.MonkeyPatch):
    # A breakdown by group answers a list of values; with a cue asking each entity's value it must abstain.
    class ListCompiler:
        def __init__(self, connection):
            pass

        def answer(self, intent, windows):
            return Answer("list", [120, 80])

    monkeypatch.setattr(solver_module, "Compiler", ListCompiler)
    intent = Intent(measure="revenue", shape="breakdown", group_by="channel")
    prediction, trace = solve(
        ParseResult(intent), FakeTemporal(single=[win("2026-01-01", "2026-04-01")]),
        text="Doanh thu của kênh dine và take, kèm số tiền mỗi người",
    )
    assert prediction.type == AnswerType.ABSTAIN
    assert prediction.reason == "answer shape: table expected"
    assert trace.decision == "abstain"


def test_top_k_with_table_cue_abstains_when_no_table_can_be_built(monkeypatch: pytest.MonkeyPatch):
    class NoTableCompiler:
        def __init__(self, connection):
            pass

        def table(self, intent, windows):
            raise Unsupported("ranked entity has no name")

    monkeypatch.setattr(solver_module, "Compiler", NoTableCompiler)
    intent = Intent(measure="revenue", shape="top_k", group_by="customer", top_k=3)
    prediction, trace = solve(
        ParseResult(intent), FakeTemporal(single=[win("2026-01-01", "2026-04-01")]),
        text="Top 3 khách mua nhiều nhất, kèm số tiền",
    )
    assert prediction.type == AnswerType.ABSTAIN
    assert prediction.reason == "ranked entity has no name"


def test_list_answer_without_table_cue_is_returned_unchanged(monkeypatch: pytest.MonkeyPatch):
    class ListCompiler:
        def __init__(self, connection):
            pass

        def answer(self, intent, windows):
            return Answer("list", ["Lan", "Minh", "Hoa"])

    monkeypatch.setattr(solver_module, "Compiler", ListCompiler)
    intent = Intent(measure="revenue", shape="top_k", group_by="customer", top_k=3)
    prediction, _ = solve(
        ParseResult(intent), FakeTemporal(single=[win("2026-01-01", "2026-04-01")]),
        text="Top 3 khách mua nhiều nhất trong quý",
    )
    assert prediction.type == AnswerType.LIST
    assert prediction.answer == ["Lan", "Minh", "Hoa"]


def test_top_k_with_table_cue_returns_the_table_from_the_compiler(monkeypatch: pytest.MonkeyPatch):
    table = {"columns": ["name", "value"], "rows": [["Lan", 150], ["Minh", 110]]}

    class TableCompiler:
        def __init__(self, connection):
            pass

        def table(self, intent, windows):
            return Answer("table", table)

        def answer(self, intent, windows):
            raise AssertionError("a table question must not take the list path")

    monkeypatch.setattr(solver_module, "Compiler", TableCompiler)
    intent = Intent(measure="revenue", shape="top_k", group_by="customer", top_k=2)
    prediction, trace = solve(
        ParseResult(intent), FakeTemporal(single=[win("2026-01-01", "2026-04-01")]),
        text="Top 2 khách mua nhiều nhất, kèm số tiền",
    )
    assert prediction.type == AnswerType.TABLE
    assert prediction.answer == table
    assert trace.decision == "value"
