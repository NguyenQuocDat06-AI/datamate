from __future__ import annotations

import csv
from datetime import date

import pytest

from bizreason.config import Settings
from bizreason.contracts import Diagnostic, SourceRef, OutputSpec, PlanFilter, QueryPlan, Question, TimeWindow
from bizreason.data.ingestion import BusinessBuilder
from bizreason.execution import execute_sql
from bizreason.pipeline import Solver
from bizreason.planning import validate_plan
from bizreason.profit import bind_net_profit
from bizreason.rules import RuleRegistry
from bizreason.temporal import TemporalResolver
from bizreason.schema_context import compact_diagnostics
from scripts.review_public_next20 import worker_alias
from bizreason.llm import Usage


def write(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows(rows)


@pytest.fixture
def profit_case(workspace_tmp, rules):
    rules = RuleRegistry(rules.text.replace("## 7.", "Lãi ròng trừ mọi khoản ghi trong sổ chi phí.\n## 7."))
    directory = workspace_tmp / "profit"
    directory.mkdir()
    write(directory / "sales.csv", [
        ["order_id", "ordered_at", "status", "product_name", "qty_sale", "cost_sale", "line_amount"],
        ["A", "2025-10-02", "completed", "X", 2, 100, 1000],
        ["B", "2025-10-03", "cancelled", "X", 1, 100, 500],
        ["C", "2025-10-04", "returned", "X", 1, 100, 300],
    ])
    write(directory / "expenses.csv", [["Ngày mua", "Tiền chi"], ["5 thg 10 2025", 200], ["01/11/2025", 999]])
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    sales = next(t for t in context.tables if t.grain == "order_line")
    expense = next(t for t in context.tables if t.role("expense_amount"))
    plan = QueryPlan(metric="generic_sql", tables=[sales.name, expense.name],
        windows=[TimeWindow(start="2025-10-01", end_exclusive="2025-11-01", field=f"{sales.name}.ordered_at")],
        rule_refs=rules.required_refs("gross_profit"), output=OutputSpec(type="number", rounding="money"),
        filters=[PlanFilter(field=f"{sales.name}.status", operator="not_in", values=["Đã hủy đơn", "Khách trả"])],
        sql=f'SELECT SUM(line_amount) FROM "{sales.name}"')
    question = Question(id="arbitrary", business_id=context.business_id,
                        question="Lãi ròng của quán trong 10/2025 là bao nhiêu?", as_of_date="2026-01-02")
    return context, rules, plan, question


def test_profit_binding_excludes_ineligible_orders_and_handles_source_dates(profit_case):
    context, rules, plan, question = profit_case
    bound = bind_net_profit(plan, question, context, rules)
    assert bound is not None
    assert validate_plan(bound.plan, context, rules, question) == []
    result = execute_sql(context.database, bound.program.sql, bound.program.parameters, bound.plan.tables)
    assert result.rows == [["600"]]  # 1000 - 2*100 - 200, not 1800 gross receipts.
    assert result.status == "success"


def test_raw_status_metadata_is_rejected_before_execution(profit_case):
    context, rules, plan, question = profit_case
    assert any(e.startswith("NON_CANONICAL_STATUS_FILTER:") for e in validate_plan(plan, context, rules, question))


def test_profit_binding_does_not_drop_an_additional_scope(profit_case):
    context, rules, plan, question = profit_case
    question = question.model_copy(update={"question": "Lãi ròng nhóm cà phê tháng 10/2025?"})
    assert bind_net_profit(plan, question, context, rules) is None


def test_solver_binds_a_recorded_plan_without_needing_a_repair(profit_case):
    context, rules, plan, question = profit_case
    class Recorded:
        def __init__(self):
            self.usage = Usage()
        def generate_json(self, *args, **kwargs):
            self.usage.calls += 1
            return plan.model_dump(mode="json")
    prediction, trace = Solver(Recorded(), rules, TemporalResolver(), Settings()).solve(question, context)
    assert prediction.answer == 600 and prediction.type == "number"
    assert trace.repair_count == 0 and trace.candidates[0].program.backend == "template"


@pytest.mark.parametrize("raw", ["An", "a An", "A.An", "A. An", "anh An"])
def test_independent_audit_handles_recorded_worker_aliases(raw):
    assert worker_alias(raw) == "an"


def test_independent_audit_does_not_match_an_inside_another_name():
    assert worker_alias("anh Đức") == "duc"
    assert worker_alias("A.Đạt") == "dat"


def test_numeric_month_does_not_replace_a_quarter():
    resolver = TemporalResolver()
    month = resolver.resolve("Lãi ròng trong 10/2025?", date(2026, 1, 2))[0]
    assert (month.start.isoformat(), month.end_exclusive.isoformat()) == ("2025-10-01", "2025-11-01")
    quarter = resolver.resolve("Cả quý 4/2025 bán được bao nhiêu tiền?", date(2026, 1, 2))[0]
    assert (quarter.start.isoformat(), quarter.end_exclusive.isoformat()) == ("2025-10-01", "2026-01-01")


def test_repeated_diagnostics_fit_prompt_without_losing_conditions():
    items=[Diagnostic(code="EXCLUDED_SUMMARY_ROW",message="Confirmed summary",severity="info",
        source=SourceRef(path="sales.xlsx",table="Jan",row=i)) for i in range(400)]
    items.append(Diagnostic(code="SOURCE_READ_ERROR",message="Unread source",severity="error",
        source=SourceRef(path="other.xlsx")))
    compact=compact_diagnostics(items)
    assert len(compact)==2 and compact[0]["occurrences"]==400
    assert len(compact[0]["source_samples"])==3
    assert compact[1]["severity"]=="error" and compact[1]["path"]=="other.xlsx"
    assert len(items)==401  # Source diagnostics are unchanged.
