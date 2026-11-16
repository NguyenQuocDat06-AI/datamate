from __future__ import annotations

import copy
import csv
import sqlite3

import pytest

from bizreason.compilation import TemplateCompiler
from bizreason.contracts import OutputSpec, QueryPlan, Question, TimeWindow
from bizreason.data.ingestion import BusinessBuilder
from bizreason.decision import assess_metric_coverage
from bizreason.execution import execute_sql, register_helpers
from bizreason.plan_input import canonicalize_plan_input
from bizreason.planning import validate_plan
from bizreason.sql_parameters import ParameterBindingError, bind_lookup_parameters
from bizreason.utils import file_hash, normalize_lookup


def write(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows(rows)


def test_header_dedup_keeps_other_orders_scopes_and_payment_events(workspace_tmp, rules):
    directory = workspace_tmp / "invoice_export"
    directory.mkdir()
    write(directory / "invoices.csv", [
        ["scope", "order_id", "ordered_at", "status", "total_due", "order_revenue"],
        ["north", "A", "2029-05-05", "completed", 25000, 23300],
        ["north", "A", "2029-05-05", "completed", 25000, 23300],
        ["north", "B", "2029-05-05", "completed", 25000, 23300],
        ["south", "A", "2029-05-05", "completed", 25000, 23300],
    ])
    write(directory / "payments.csv", [["order_id", "paid_at", "paid_amount"],
        ["A", "2029-05-06", 100], ["A", "2029-05-06", 100]])
    before = file_hash(directory / "invoices.csv")
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    header = next(t for t in context.tables if t.grain == "order_header")
    payments = next(t for t in context.tables if t.grain == "payment_event")
    assert header.row_count == 3 and header.duplicate_rows == 1
    assert header.raw_row_count == header.row_count + header.duplicate_rows + header.excluded_rows
    assert payments.row_count == 2 and payments.duplicate_rows == 0
    result = execute_sql(context.database, f'SELECT SUM(order_revenue) FROM "{header.name}"', [], [header.name])
    assert result.rows == [[69900]]
    evidence = [d for d in context.diagnostics if d.code == "DUPLICATE_ORDER_ROW"]
    assert len(evidence) == 1 and evidence[0].source.row == 3
    assert file_hash(directory / "invoices.csv") == before


def test_different_header_values_are_not_silently_collapsed(workspace_tmp, rules):
    directory = workspace_tmp / "different_headers"
    directory.mkdir()
    write(directory / "invoices.csv", [["order_id", "ordered_at", "total_due"],
        ["A", "2029-01-01", 100], ["A", "2029-01-01", 200]])
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    assert context.tables[0].row_count == 2 and context.tables[0].duplicate_rows == 0


def test_documented_header_revenue_is_typed_separately_from_total_due(workspace_tmp, rules):
    directory = workspace_tmp / "described_invoice"
    directory.mkdir()
    (directory / "README.md").write_text(
        "## Cột trong `orders.csv`\n- **Merchandise**: tiền hàng của đơn: cộng thành tiền các mặt hàng, trước giảm giá cả đơn, chưa gồm phí giao.\n",
        encoding="utf-8")
    write(directory / "orders.csv", [["order_id", "ordered_at", "Merchandise", "total_due", "due_date"],
        ["A", "2030-01-01", "2,7tr", "2800k", "12/01/2030"]])
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    table = context.tables[0]
    assert table.role("order_revenue") == "merchandise"
    assert table.role("due_at") == "due_date"
    result = execute_sql(context.database, f'SELECT merchandise,total_due,due_date FROM "{table.name}"', [], [table.name])
    assert result.rows == [[2700000, 2800000, "2030-01-12"]]


@pytest.mark.parametrize("name", ["Trà đào [L]", "Sữa tươi 1,5L", "Bánh-mì (nhỏ)"])
def test_compiler_binds_equality_value_with_the_same_lookup(name, business, rules, plan_factory):
    sql = "SELECT ? AS date_value WHERE lookup(?) = ? AND ? > 0"
    parameters = ["2032-03-04", name, name, 17.25]
    plan = plan_factory("generic_sql", sql=sql, parameters=parameters)
    compiled = TemplateCompiler(rules).compile(plan, business)
    assert compiled.sql == sql and plan.parameters == parameters
    assert compiled.parameters == [parameters[0], name, normalize_lookup(name), 17.25]
    db = sqlite3.connect(":memory:")
    try:
        register_helpers(db)
        assert db.execute(compiled.sql, compiled.parameters).fetchone() == ("2032-03-04",)
    finally:
        db.close()


@pytest.mark.parametrize("sql", ["SELECT 1 WHERE ? = (lookup(name))", "SELECT 1 WHERE ((lookup(name))) == ?", "SELECT 1 WHERE lookup(name) <> ?"])
def test_lookup_binding_supports_reverse_parentheses_and_inequality(sql):
    assert bind_lookup_parameters(sql, ["Tên [X]"]) == ["ten x"]


def test_parameter_positions_ignore_strings_identifiers_and_comments():
    sql = '''SELECT '?' AS "?", 'lookup(x)=?' AS text FROM t -- lookup(x)=?
      WHERE stamp >= ? AND lookup(label)=? /* ? */ AND price > ?'''
    assert bind_lookup_parameters(sql, ["2033-01-02", "Áo (M)", 23.5]) == ["2033-01-02", "ao m", 23.5]


def test_lookup_in_normalizes_whole_values_but_not_nested_function_arguments():
    sql = "SELECT 1 WHERE lookup(name) IN (?, coalesce(?,?),lookup(?),?)"
    original = ["TÊN A", "Raw A", "Raw B", "TÊN C", "Tên D"]
    assert bind_lookup_parameters(sql, original) == ["ten a", "Raw A", "Raw B", "TÊN C", "ten d"]
    assert bind_lookup_parameters("SELECT 1 WHERE lookup(name) IN (SELECT ? FROM t)", ["Raw A"]) == ["Raw A"]


def test_raw_equality_like_patterns_and_ranges_remain_exact():
    original = ["%Áo_%", "Tên (M)", "2031-12-31"]
    sql = "SELECT 1 WHERE lookup(name) LIKE ? AND label=? AND stamp<?"
    assert bind_lookup_parameters(sql, original) == original


def test_shared_numbered_parameter_never_changes_a_raw_comparison():
    with pytest.raises(ParameterBindingError, match="mixes normalized and raw"):
        bind_lookup_parameters("SELECT 1 WHERE lookup(name)=?1 OR exact_code=?1", ["Áo (M)"])
    assert bind_lookup_parameters("SELECT 1 WHERE lookup(a)=?1 OR lookup(b)=lookup(?1)", ["Áo (M)"]) == ["ao m"]
    assert bind_lookup_parameters("SELECT 1 WHERE lookup(a)=?2 AND x=?1", [13, "Áo (M)"]) == [13, "ao m"]


def test_complex_lookup_expression_and_missing_bindings_fail_closed():
    with pytest.raises(ParameterBindingError, match="Complex lookup"):
        bind_lookup_parameters("SELECT x + lookup(name) = ?", ["Áo (M)"])
    with pytest.raises(ParameterBindingError, match="expects 2"):
        bind_lookup_parameters("SELECT ? UNION ALL SELECT ?", [1])


@pytest.fixture
def partitions(workspace_tmp, rules):
    directory = workspace_tmp / "partition_family"
    directory.mkdir()
    header = ["order_id", "ordered_at", "status", "product_name", "qty_sale", "line_amount"]
    write(directory / "history.csv", [header, ["OLD", "2024-01-01", "completed", "A", 1, 20]])
    write(directory / "recent.csv", [header, ["NEW", "2029-05-05", "completed", "A", 1, 30]])
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    old = next(t for t in context.tables if t.coverage["ordered_at"][0].startswith("2024"))
    recent = next(t for t in context.tables if t is not old)
    return context, old, recent


def test_shared_date_metadata_does_not_require_unrelated_partitions(partitions, rules):
    context, old, recent = partitions
    raw = QueryPlan(metric="generic_sql", tables=[recent.name], rule_refs=[rules.records[0].id],
        windows=[TimeWindow(start="2029-05-01", end_exclusive="2029-06-01", field="ordered_at")],
        sql=f'SELECT COUNT(*) FROM "{recent.name}"', output=OutputSpec(type="number")).model_dump(mode="json")
    before = copy.deepcopy(raw)
    plan = QueryPlan.model_validate(canonicalize_plan_input(raw, context))
    assert raw == before and plan.sql == raw["sql"]
    assert plan.windows[0].field == f"{recent.name}.ordered_at"
    question = Question(id="unseen", business_id=context.business_id, question="tháng 5/2029", as_of_date="2029-06-01")
    assert not validate_plan(plan, context, rules, question)
    cumulative = plan.model_copy(update={"windows": [plan.windows[0].model_copy(update={"mode": "cumulative"})]})
    assert "MISSING_PARTITION:" + old.name in validate_plan(cumulative, context, rules, question)


def test_metadata_uses_only_real_aliases_or_documented_source_names(partitions, rules):
    context, _, recent = partitions
    raw = QueryPlan(metric="generic_sql", tables=[recent.name], rule_refs=[rules.records[0].id],
        required_fields=["r.ordered_at"], sql=f'SELECT * FROM "{recent.name}" AS r',
        output=OutputSpec(type="number"), windows=[TimeWindow(start="2029-05-01", end_exclusive="2029-06-01", field="recent.ordered_at")]).model_dump(mode="json")
    result = canonicalize_plan_input(raw, context)
    assert result["required_fields"] == [f"{recent.name}.ordered_at"]
    assert result["windows"][0]["field"] == f"{recent.name}.ordered_at"
    raw["windows"][0]["field"] = "invented.ordered_at"
    assert canonicalize_plan_input(raw, context)["windows"][0]["field"] == "invented.ordered_at"


def test_missing_time_field_is_not_guessed_between_sales_and_payment_dates(business, rules):
    sale = next(t for t in business.tables if t.role("ordered_at"))
    payment = next(t for t in business.tables if t.role("paid_at"))
    raw = QueryPlan(metric="generic_sql", tables=[sale.name, payment.name], rule_refs=[rules.records[0].id],
        required_fields=[f"{sale.name}.ordered_at", f"{payment.name}.paid_at"],
        windows=[TimeWindow(start="2026-03-01", end_exclusive="2026-04-01")],
        output=OutputSpec(type="number"), sql="SELECT 1").model_dump(mode="json")
    assert canonicalize_plan_input(raw, business)["windows"][0]["field"] is None
    # Even if the model lists just one date in required_fields, another date
    # domain among selected relations makes an omitted time field ambiguous.
    raw["required_fields"] = [f"{payment.name}.paid_at"]
    assert canonicalize_plan_input(raw, business)["windows"][0]["field"] is None


def test_cumulative_coverage_does_not_discard_available_history(business, rules, plan_factory):
    plan = plan_factory("sales_revenue", windows=[TimeWindow(start="2030-01-01", end_exclusive="2030-01-02", mode="cumulative")], sql="SELECT 1")
    assert assess_metric_coverage(plan, business, rules.text).verified is False
    assert TemplateCompiler(rules).compile(plan, business).backend == "generated_sql"
