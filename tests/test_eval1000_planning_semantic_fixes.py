from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from bizreason.contracts import (
    AnswerType,
    BusinessContext,
    Candidate,
    ColumnMeta,
    ExecutionResult,
    ExecutionStatus,
    OutputSpec,
    Program,
    QueryPlan,
    Question,
    TableMeta,
    TimeWindow,
)
from bizreason.execution import execute_sql
from bizreason.pipeline import Solver, _recover_list_execution
from bizreason.planning import (
    NATIVE_SQL_SYSTEM_PROMPT,
    Planner,
    _build_native_sql_ddl_prompt,
    auto_heal_plan,
    validate_plan,
)
from bizreason.rules import RuleRegistry
from bizreason.semantic_checks import semantic_plan_errors
from bizreason.verification import verify_candidate


def _make_dummy_context(db_path: Path, tables: list[TableMeta]) -> BusinessContext:
    return BusinessContext(
        business_id="biz_test",
        revision="rev_1",
        database=db_path,
        readme="Business notes",
        tables=tables,
        manifest={},
    )


def test_native_sql_prompt_contains_rules():
    assert "11. Customer Debt & Inventory Stock Ledger:" in NATIVE_SQL_SYSTEM_PROMPT
    assert "12. List & Entity Selection:" in NATIVE_SQL_SYSTEM_PROMPT
    assert "Revenue Grain:" in NATIVE_SQL_SYSTEM_PROMPT
    assert "giá nhập bình quân" in NATIVE_SQL_SYSTEM_PROMPT
    assert "ROW_NUMBER() OVER (PARTITION BY" in NATIVE_SQL_SYSTEM_PROMPT


def test_active_partition_comment_in_ddl_prompt(tmp_path):
    db_file = tmp_path / "test.db"
    col_date = ColumnMeta(name="ngay", original_name="ngay", dtype="TEXT", semantic_role="ordered_at")
    col_amt = ColumnMeta(name="tien", original_name="tien", dtype="INTEGER", semantic_role="line_amount")
    
    t1 = TableMeta(
        name="sales_2026_01",
        sources=[],
        columns=[col_date, col_amt],
        grain="order_line",
        row_count=100,
        raw_row_count=100,
        coverage={"ngay": ("2026-01-01", "2026-01-31")},
    )
    t2 = TableMeta(
        name="sales_2026_02",
        sources=[],
        columns=[col_date, col_amt],
        grain="order_line",
        row_count=100,
        raw_row_count=100,
        coverage={"ngay": ("2026-02-01", "2026-02-28")},
    )
    t3 = TableMeta(
        name="sales_2026_03",
        sources=[],
        columns=[col_date, col_amt],
        grain="order_line",
        row_count=100,
        raw_row_count=100,
        coverage={"ngay": ("2026-03-01", "2026-03-31")},
    )
    ctx = _make_dummy_context(db_file, [t1, t2, t3])
    q = Question(id="q1", business_id="biz_test", question="Doanh thu 2 tháng đầu năm 2026", as_of_date=date(2026, 3, 1))
    windows = [TimeWindow(start=date(2026, 1, 1), end_exclusive=date(2026, 3, 1), role="target")]
    
    sys_prompt, user_prompt = _build_native_sql_ddl_prompt(
        context=ctx,
        tables=[t1, t2, t3],
        question=q,
        windows=windows,
        entities=[],
        feedback=None,
        probe_results=None,
        budget=16000,
    )
    assert "-- Active Partition Tables matching requested date range:" in user_prompt
    assert "sales_2026_02" in user_prompt


def test_multi_partition_union_all_expansion(tmp_path, rules):
    db_file = tmp_path / "partitions.db"
    con = sqlite3.connect(db_file)
    con.execute('CREATE TABLE "pos_order_202601" (order_id TEXT, amount REAL, ordered_at TEXT)')
    con.execute('CREATE TABLE "pos_order_202602" (order_id TEXT, amount REAL, ordered_at TEXT)')
    con.execute("INSERT INTO pos_order_202601 VALUES ('DH01', 100.0, '2026-01-15')")
    con.execute("INSERT INTO pos_order_202602 VALUES ('DH02', 200.0, '2026-02-15')")
    con.commit()
    con.close()

    col_id = ColumnMeta(name="order_id", original_name="order_id", dtype="TEXT")
    col_amt = ColumnMeta(name="amount", original_name="amount", dtype="REAL", semantic_role="line_amount")
    col_date = ColumnMeta(name="ordered_at", original_name="ordered_at", dtype="TEXT", semantic_role="ordered_at")

    t1 = TableMeta(
        name="pos_order_202601",
        sources=[],
        columns=[col_id, col_amt, col_date],
        grain="order_line",
        row_count=1,
        raw_row_count=1,
        coverage={"ordered_at": ("2026-01-01", "2026-01-31")},
    )
    t2 = TableMeta(
        name="pos_order_202602",
        sources=[],
        columns=[col_id, col_amt, col_date],
        grain="order_line",
        row_count=1,
        raw_row_count=1,
        coverage={"ordered_at": ("2026-02-01", "2026-02-28")},
    )
    ctx = _make_dummy_context(db_file, [t1, t2])
    q = Question(id="q_multi", business_id="biz_test", question="Tổng doanh thu tháng 1 và 2 năm 2026", as_of_date=date(2026, 3, 1))
    window = TimeWindow(start=date(2026, 1, 1), end_exclusive=date(2026, 3, 1))

    # Query references only pos_order_202601
    initial_plan = QueryPlan(
        metric="generic_sql",
        tables=["pos_order_202601"],
        rule_refs=[next(iter(rules.by_id))],
        windows=[window],
        sql='SELECT SUM(amount) FROM "pos_order_202601"',
        output=OutputSpec(type=AnswerType.NUMBER),
    )

    # Initial plan would fail validate_plan with MISSING_PARTITION
    unhealed_errors = validate_plan(initial_plan, ctx, rules, q)
    assert any("MISSING_PARTITION:pos_order_202602" in e for e in unhealed_errors)

    # auto_heal_plan deterministically expands the partition
    healed_plan = auto_heal_plan(initial_plan, ctx, rules, q)
    assert "pos_order_202602" in healed_plan.tables
    assert "UNION ALL" in healed_plan.sql
    assert validate_plan(healed_plan, ctx, rules, q) == []

    # Executing the healed query sums across both partitions
    exec_res = execute_sql(db_file, healed_plan.sql, [], healed_plan.tables, timeout=5.0, max_rows=100)
    assert exec_res.status == ExecutionStatus.SUCCESS
    assert exec_res.rows[0][0] == 300.0


def test_customer_identity_grouping_auto_healing(tmp_path, rules):
    db_file = tmp_path / "customers.db"
    col_cid = ColumnMeta(name="ma_kh", original_name="ma_kh", dtype="TEXT", semantic_role="customer_id")
    col_cname = ColumnMeta(name="ten_kh", original_name="ten_kh", dtype="TEXT", semantic_role="customer_name")
    col_amt = ColumnMeta(name="thanh_tien", original_name="thanh_tien", dtype="REAL", semantic_role="line_amount")

    t = TableMeta(
        name="don_hang",
        sources=[],
        columns=[col_cid, col_cname, col_amt],
        grain="order_line",
        row_count=10,
        raw_row_count=10,
    )
    ctx = _make_dummy_context(db_file, [t])
    q = Question(id="q_cust", business_id="biz_test", question="Top khách hàng chi tiêu nhiều nhất", as_of_date=date(2026, 3, 1))

    hazard_plan = QueryPlan(
        metric="generic_sql",
        tables=["don_hang"],
        rule_refs=[next(iter(rules.by_id))],
        group_by=["don_hang.ten_kh"],
        sql="SELECT ten_kh, SUM(thanh_tien) AS total FROM don_hang GROUP BY ten_kh ORDER BY total DESC LIMIT 3",
        output=OutputSpec(type=AnswerType.LIST),
    )

    unhealed_errors = semantic_plan_errors(hazard_plan, ctx, q)
    assert any("CUSTOMER_IDENTITY_GROUPING_REQUIRED" in e for e in unhealed_errors)

    healed_plan = auto_heal_plan(hazard_plan, ctx, rules, q)
    assert "ma_kh" in healed_plan.sql
    assert "don_hang.ma_kh" in healed_plan.group_by
    assert not any("CUSTOMER_IDENTITY_GROUPING_REQUIRED" in e for e in semantic_plan_errors(healed_plan, ctx, q))


def test_percent_missing_100_scale_auto_healing(tmp_path, rules):
    db_file = tmp_path / "percent.db"
    ctx = _make_dummy_context(db_file, [])
    q = Question(id="q_pct", business_id="biz_test", question="Tăng trưởng bao nhiêu phần trăm?", as_of_date=date(2026, 3, 1))

    hazard_plan = QueryPlan(
        metric="generic_sql",
        tables=[],
        rule_refs=[next(iter(rules.by_id))],
        sql="WITH cte AS (SELECT 120.0 AS curr, 90.0 AS prev) SELECT (curr - prev) / prev AS growth FROM cte",
        output=OutputSpec(type=AnswerType.PERCENT),
    )

    unhealed_errors = semantic_plan_errors(hazard_plan, ctx, q)
    assert any("PERCENT_MISSING_100_SCALE" in e for e in unhealed_errors)

    healed_plan = auto_heal_plan(hazard_plan, ctx, rules, q)
    assert "100.0 *" in healed_plan.sql
    assert not any("PERCENT_MISSING_100_SCALE" in e for e in semantic_plan_errors(healed_plan, ctx, q))


def test_list_two_column_metric_recovery():
    plan = QueryPlan(
        metric="generic_sql",
        tables=["san_pham"],
        rule_refs=["rb:test"],
        output=OutputSpec(type=AnswerType.LIST),
    )
    q = Question(id="q_list", business_id="biz_test", question="Top 3 sản phẩm bán chạy nhất", as_of_date=date(2026, 3, 1))
    raw_exec = ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        columns=["ten_sp", "total_qty"],
        rows=[["Cà phê sữa", 150], ["Trà đào", 120], ["Bạc xỉu", 90]],
        row_count=3,
        accessed_tables=["san_pham"],
    )
    recovered = _recover_list_execution(raw_exec, plan, q)
    assert recovered.rows == [["Cà phê sữa"], ["Trà đào"], ["Bạc xỉu"]]

    prog = Program(sql="SELECT ten_sp, total FROM ...", parameters=[], backend="generated_sql", policy_refs=[])
    cand = Candidate(
        id="c1",
        plan_hash="h1",
        code_hash="c1",
        source_revision="r1",
        program=prog,
        execution=recovered,
        output=plan.output,
    )
    verified = verify_candidate(cand, plan, question=q)
    assert verified.verified is True
    assert verified.value == ["Cà phê sữa", "Trà đào", "Bạc xỉu"]


def test_list_two_column_id_name_recovery():
    plan = QueryPlan(
        metric="generic_sql",
        tables=["san_pham"],
        rule_refs=["rb:test"],
        output=OutputSpec(type=AnswerType.LIST),
    )
    q = Question(id="q_list2", business_id="biz_test", question="Danh sách các món ăn phổ biến", as_of_date=date(2026, 3, 1))
    raw_exec = ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        columns=["ma_sp", "ten_sp"],
        rows=[["SP001", "Bún bò Huế"], ["SP002", "Phở bò"], ["SP003", "Cơm tấm"]],
        row_count=3,
        accessed_tables=["san_pham"],
    )
    recovered = _recover_list_execution(raw_exec, plan, q)
    assert recovered.rows == [["Bún bò Huế"], ["Phở bò"], ["Cơm tấm"]]


def test_list_comma_separated_single_row_recovery():
    plan = QueryPlan(
        metric="generic_sql",
        tables=["san_pham"],
        rule_refs=["rb:test"],
        output=OutputSpec(type=AnswerType.LIST),
    )
    q = Question(id="q_list3", business_id="biz_test", question="Những mặt hàng nào được bán?", as_of_date=date(2026, 3, 1))
    raw_exec = ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        columns=["items"],
        rows=[["Bia Saigon, Nước ngọt Pepsi, Trà xanh C2"]],
        row_count=1,
        accessed_tables=["san_pham"],
    )
    recovered = _recover_list_execution(raw_exec, plan, q)
    assert recovered.rows == [["Bia Saigon"], ["Nước ngọt Pepsi"], ["Trà xanh C2"]]


def test_pipeline_solver_end_to_end_list_and_healing(tmp_path, rules):
    from bizreason.config import Settings
    from bizreason.temporal import TemporalResolver

    db_file = tmp_path / "e2e.db"
    con = sqlite3.connect(db_file)
    con.execute("CREATE TABLE products (product_name TEXT, revenue REAL)")
    con.execute("INSERT INTO products VALUES ('Product Alpha', 500), ('Product Beta', 300)")
    con.commit()
    con.close()

    col_name = ColumnMeta(name="product_name", original_name="product_name", dtype="TEXT")
    col_rev = ColumnMeta(name="revenue", original_name="revenue", dtype="REAL", semantic_role="line_amount")
    t = TableMeta(
        name="products",
        sources=[],
        columns=[col_name, col_rev],
        grain="order_line",
        row_count=2,
        raw_row_count=2,
    )
    ctx = _make_dummy_context(db_file, [t])
    q = Question(id="q_e2e", business_id="biz_test", question="Top sản phẩm có doanh thu cao nhất", as_of_date=date(2026, 3, 1))

    # Mock model returns 2 columns in SQL for a list question
    model_response = {
        "metric": "generic_sql",
        "tables": ["products"],
        "rule_refs": [next(iter(rules.by_id))],
        "sql": "SELECT product_name, SUM(revenue) AS total FROM products GROUP BY product_name ORDER BY total DESC",
        "output": {"type": "list", "shape": "list"},
    }

    class MockModel:
        def __init__(self, resp):
            self.resp = resp
            self.usage = type("Usage", (), {"calls": 0, "tokens": 0})()

        def generate_json(self, system, prompt, timeout=60.0):
            self.usage.calls += 1
            return dict(self.resp)

    model = MockModel(model_response)
    settings = Settings(model="mock-model", max_model_calls=1, max_repairs=0)
    solver = Solver(model=model, rules=rules, temporal=TemporalResolver(), settings=settings)

    pred, trace = solver.solve(q, ctx)
    assert pred.type == AnswerType.LIST
    assert pred.answer == ["Product Alpha", "Product Beta"]
    assert trace.decision.action == "value"
