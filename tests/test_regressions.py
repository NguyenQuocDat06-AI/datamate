from __future__ import annotations

import sqlite3
from contextlib import closing
from decimal import Decimal

import pytest

from bizreason.config import Settings
from bizreason.contracts import OutputSpec, Question, TimeWindow
from bizreason.data.ingestion import BusinessBuilder, infer_locale
from bizreason.data.parsing import ParseStatus, parse_date, parse_number, parse_status
from bizreason.data.readers import sqlite_readonly
from bizreason.decision import DecisionGate, UnsupportedEvidence
from bizreason.pipeline import Solver
from bizreason.planning import Planner
from bizreason.temporal import TemporalResolver
from test_execution_pipeline import FakeModel, execute_plan


@pytest.mark.parametrize(
    "raw,unit,expected",
    [
        ("1tr270", "VND", 1270000),
        ("1,274tr", "VND", 1274000),
        ("332,5972tr", "VND", 332597200),
        ("1.190.000đ", "VND", 1190000),
        ("2tr760đ", "VND", 2760000),
        ("-1tr250", "VND", -1250000),
        ("2 bao 25kg", "bao 25kg", 2),
        ("1 hộp 30", "hộp 30", 1),
        ("3 xe 4m3", "xe 4m3", 3),
        ("1 m3", "m3", 1),
    ],
)
def test_observed_export_formats_are_generic(raw, unit, expected):
    result = parse_number(raw, unit=unit)
    assert result.status == ParseStatus.OK
    assert result.value == Decimal(expected)


def test_excel_serial_and_vietnamese_dates():
    assert parse_date("45292").value == "2024-01-01"
    assert parse_date("1 thg 1 2025").value == "2025-01-01"
    assert parse_date("ngày 4 tháng 3 năm 2026").value == "2026-03-04"


def test_locale_uses_a_column_profile_not_one_ambiguous_cell():
    assert infer_locale(["1.234"], "") == "unknown"
    assert infer_locale(["1,000", "2,000", "3,000", "4,000"] * 6, "", currency=True) == "en"
    assert parse_number("1.234").status == ParseStatus.AMBIGUOUS


def test_purchase_and_customer_status_do_not_share_order_assumptions():
    assert parse_status("Đã nhập hàng", domain="purchase").value == "received"
    assert parse_status("Đã nhập hàng").status == ParseStatus.INVALID
    assert parse_status("đang gửi").value == "shipping"
    assert parse_status("Chờ giao").value == "shipping"


def test_converter_uses_product_specific_factors_and_keeps_sale_quantity(workspace_tmp, rules):
    # The unit-default rule is supplied at runtime, not keyed to a business ID.
    from bizreason.rules import RuleRegistry

    runtime = RuleRegistry(
        rules.text + "\nHệ số quy đổi; đơn vị cơ bản, hệ số 1 khi không có dòng quy đổi.\n"
    )
    directory = workspace_tmp / "units"
    directory.mkdir()
    (directory / "lines.csv").write_text(
        "order_id,product_id,qty_sale,unit\nO1,P1,2,box\nO2,P2,2,box\n", encoding="utf-8"
    )
    (directory / "conversion.csv").write_text(
        "product_id,conversion_unit,conversion_factor,base_unit\nP1,box,6,item\nP2,box,24,item\n",
        encoding="utf-8",
    )
    context = BusinessBuilder(
        workspace_tmp / "cache", rule_hash=runtime.revision, rules=runtime
    ).prepare(directory)
    table = next(table for table in context.tables if table.grain == "order_line")
    assert table.role("qty_base") == "derived_qty_base"
    with closing(sqlite_readonly(context.database)) as connection:
        values = connection.execute(
            f'SELECT qty_sale,derived_qty_base FROM "{table.name}" ORDER BY product_id'
        ).fetchall()
    assert values == [(2, 12), (2, 48)]


def test_debt_includes_unpaid_cash_and_anonymous_orders(business, rules, plan_factory):
    orders = next(table for table in business.tables if table.grain == "order_header")
    with closing(sqlite3.connect(business.database)) as connection:
        connection.execute(
            f"UPDATE \"{orders.name}\" SET payment_method='cash' WHERE order_id='O4'"
        )
        connection.commit()
    assert execute_plan(plan_factory("debt_total"), business, rules).value == 125000


def test_debt_cutoff_does_not_include_a_future_payment(business, rules, plan_factory):
    first = execute_plan(plan_factory("debt_total"), business, rules)
    later = execute_plan(
        plan_factory(
            "debt_total", windows=[TimeWindow(start="2026-03-01", end_exclusive="2026-03-08")]
        ),
        business,
        rules,
    )
    assert (first.value, later.value) == (125000, 75000)


def test_profit_template_enforces_whole_dong_rounding(business, rules, plan_factory):
    table = next(table for table in business.tables if table.grain == "order_line")
    with closing(sqlite3.connect(business.database)) as connection:
        connection.execute(
            f"UPDATE \"{table.name}\" SET cost_sale=7000.25 WHERE order_id='O1' AND product_id='P1'"
        )
        connection.commit()
    candidate = execute_plan(plan_factory("gross_profit"), business, rules)
    assert candidate.value == 111000
    assert type(candidate.value) is int
    assert "vnd_half_up_rounding" in candidate.checks


def test_missing_redundant_tables_field_is_derived_from_real_sql(business, rules, plan_factory):
    table = next(table for table in business.tables if table.grain == "order_line")
    raw = plan_factory(
        "generic_sql", rule_refs=[rules.records[0].id], sql=f'SELECT COUNT(*) FROM "{table.name}"'
    ).model_dump(mode="json")
    del raw["tables"]
    model = FakeModel([raw])
    planner = Planner(model, rules, TemporalResolver(), Settings())
    import time

    plan = planner.make_plan(
        Question(
            id="q", business_id=business.business_id, question="count", as_of_date="2026-03-04"
        ),
        business,
        time.monotonic() + 20,
    )
    assert plan.tables == [table.name]
    assert plan.derivation_notes


def test_probe_then_execute_is_bounded_and_traceable(business, rules, plan_factory):
    from bizreason.contracts import ProbeRequest

    table = next(table for table in business.tables if table.grain == "order_line")
    final = plan_factory(
        "generic_sql", rule_refs=[rules.records[0].id], sql=f'SELECT COUNT(*) FROM "{table.name}"'
    )
    initial = final.model_copy(
        update={
            "unresolved": ["inspect row count"],
            "probe_requests": [ProbeRequest(purpose="row count", sql=final.sql)],
        }
    )
    model = FakeModel([initial.model_dump(mode="json"), final.model_dump(mode="json")])
    prediction, trace = Solver(model, rules, TemporalResolver(), Settings()).solve(
        Question(
            id="q", business_id=business.business_id, question="count", as_of_date="2026-03-04"
        ),
        business,
    )
    assert prediction.answer == 6
    assert len(trace.probes) == 1 and trace.model_calls == 2


def test_numeric_decimal_list_is_serialized_as_numbers(business, rules, plan_factory):
    table = next(table for table in business.tables if table.grain == "order_line")
    plan = plan_factory(
        "generic_sql",
        output_type="list",
        shape="list",
        rule_refs=[rules.records[0].id],
        sql=f'SELECT decimal_sum(qty_sale) FROM "{table.name}"',
        output=OutputSpec(type="list", shape="list", element_type="number"),
    )
    assert execute_plan(plan, business, rules).value == [23]


def test_calibrated_gate_abstains_at_exact_half_probability(business, rules, plan_factory):
    from bizreason.decision import FEATURE_NAMES

    candidate = execute_plan(plan_factory(), business, rules)
    neutral = {
        "mean": [0] * len(FEATURE_NAMES),
        "scale": [1] * len(FEATURE_NAMES),
        "coefficients": [0] * len(FEATURE_NAMES),
        "intercept": 0,
    }
    calibration = {
        "feature_names": FEATURE_NAMES,
        "value_model": neutral,
        "unanswerable_model": neutral,
    }
    assert (
        DecisionGate("calibrated", calibration)
        .choose(candidate, UnsupportedEvidence(False, "not established"))
        .action
        == "abstain"
    )


@pytest.mark.parametrize(
    "start,end,expected_action,expected_value",
    [
        ("2026-02-25", "2026-02-26", "number", 0),
        ("2024-01-01", "2024-02-01", "unanswerable", None),
    ],
)
def test_empty_covered_period_differs_from_outside_coverage(
    start, end, expected_action, expected_value, business, rules, plan_factory
):
    plan = plan_factory(windows=[TimeWindow(start=start, end_exclusive=end)])
    model = FakeModel([plan.model_dump(mode="json")])
    prediction, trace = Solver(model, rules, TemporalResolver(), Settings()).solve(
        Question(
            id="q", business_id=business.business_id, question="doanh thu", as_of_date="2026-03-04"
        ),
        business,
    )
    assert prediction.type == expected_action
    assert prediction.answer == expected_value


def test_missing_comparison_period_is_not_silently_zero(business, rules, plan_factory):
    from bizreason.decision import assess_metric_coverage

    plan = plan_factory(
        windows=[
            TimeWindow(start="2026-03-01", end_exclusive="2026-03-05"),
            TimeWindow(role="comparison", start="2024-03-01", end_exclusive="2024-03-05"),
        ]
    )
    assert assess_metric_coverage(plan, business, rules.text).verified
