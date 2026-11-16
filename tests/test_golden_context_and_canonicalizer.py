from __future__ import annotations

import copy
import time
from datetime import date
from types import SimpleNamespace

from bizreason.config import Settings
from bizreason.contracts import (
    AnswerType,
    QueryPlan,
    Question,
)
from bizreason.llm import ProviderError
from bizreason.plan_input import _scale_scalar_percent_sql, canonicalize_plan_input
from bizreason.planning import (
    COMPACT_SYSTEM_PROMPT,
    Planner,
    SYSTEM_PROMPT,
    _build_compact_sql_prompt,
)
from bizreason.temporal import TemporalResolver


# ------------------------------------------------------------------------------
# 1. Golden Context Budget Tests
# ------------------------------------------------------------------------------

def test_golden_context_30k_keeps_system_prompt_and_rules(business, rules):
    q = Question(
        id="q1",
        business_id=business.business_id,
        question="Doanh thu hom nay",
        as_of_date=date(2026, 3, 1),
    )
    sys_prompt, user_prompt = _build_compact_sql_prompt(
        rules=rules,
        context=business,
        question=q,
        windows=[],
        entities=[],
        feedback=None,
        probe_results=None,
        budget=30000,
    )
    assert sys_prompt == SYSTEM_PROMPT
    assert "runtime_rules" in user_prompt
    assert len(sys_prompt) + len(user_prompt) <= 30000


def test_planner_make_plan_http_500_fallback_above_18k(business, rules):
    calls = []

    class MockModel:
        def generate_json(self, sys_p, user_p, *, timeout):
            calls.append((sys_p, user_p))
            if len(calls) == 1:
                raise ProviderError("Provider HTTP 500: CUDA out of memory")
            return {
                "metric": "generic_sql",
                "tables": [business.tables[0].name],
                "sql": "SELECT 1",
                "parameters": [],
                "rule_refs": [],
            }

    settings = Settings(context_characters=30000)
    planner = Planner(MockModel(), rules, TemporalResolver(), settings)
    q = Question(
        id="q_fallback",
        business_id=business.business_id,
        question="Doanh thu",
        as_of_date=date(2026, 3, 1),
    )
    plan = planner.make_plan(q, business, deadline=time.monotonic() + 30)
    assert plan.metric == "generic_sql"
    assert len(calls) == 2
    # Verify retry used compact budget and COMPACT_SYSTEM_PROMPT
    assert calls[1][0] == COMPACT_SYSTEM_PROMPT
    assert len(calls[1][0]) + len(calls[1][1]) <= 18000


# ------------------------------------------------------------------------------
# 2. Schema Canonicalization Tests
# ------------------------------------------------------------------------------

def test_canonicalize_singular_table_and_extra_forbidden(business):
    tbl_name = business.tables[0].name
    raw = {
        "metric": "generic_sql",
        "table": tbl_name,
        "sql": f"SELECT 1 FROM {tbl_name}",
        "model_type": "gemma4-12b",
        "query_planner": "active",
        "explanation": "Calculates revenue",
        "output": {
            "type": "number",
            "shape": "scalar",
            "rounding": "none",
            "extra_output_field": 42,
        },
    }
    canon = canonicalize_plan_input(raw, business)
    assert canon["tables"] == [tbl_name]
    assert "table" not in canon
    assert "model_type" not in canon
    assert "query_planner" not in canon
    assert "explanation" not in canon
    assert "extra_output_field" not in canon["output"]

    # Validates cleanly without extra_forbidden
    plan = QueryPlan.model_validate(canon)
    assert plan.tables == [tbl_name]


def test_canonicalize_dicts_inside_parameters(business):
    tbl_name = business.tables[0].name
    raw = {
        "metric": "generic_sql",
        "tables": [tbl_name],
        "sql": f"SELECT 1 FROM {tbl_name} WHERE status = ?",
        "parameters": [
            {"name": "status", "value": "cancelled"},
            {"val_dict": 100},
            "regular_str",
            None,
        ],
    }
    canon = canonicalize_plan_input(raw, business)
    assert canon["parameters"] == ["cancelled", 100, "regular_str"]
    assert all(isinstance(x, (str, int, float)) for x in canon["parameters"])
    plan = QueryPlan.model_validate(canon)
    assert plan.parameters == ["cancelled", 100, "regular_str"]


def test_canonicalize_none_and_non_list_in_list_fields(business):
    tbl_name = business.tables[0].name
    raw = {
        "metric": "generic_sql",
        "tables": tbl_name,
        "sql": f"SELECT 1 FROM {tbl_name}",
        "probe_requests": None,
        "filters": None,
        "group_by": None,
        "windows": None,
        "parameters": None,
        "rule_refs": "rb:test",
    }
    canon = canonicalize_plan_input(raw, business)
    assert canon["tables"] == [tbl_name]
    assert canon["probe_requests"] == []
    assert canon["filters"] == []
    assert canon["group_by"] == []
    assert canon["windows"] == []
    assert canon["parameters"] == []
    assert canon["rule_refs"] == ["rb:test"]
    QueryPlan.model_validate(canon)


def test_canonicalize_lingering_probe_requests_cleared_on_executable_sql(business):
    tbl_name = business.tables[0].name
    raw = {
        "metric": "generic_sql",
        "tables": [tbl_name],
        "sql": f"SELECT 1 FROM {tbl_name}",
        "probe_requests": [
            {"purpose": "Check max date", "sql": f"SELECT MAX(ordered_at) FROM {tbl_name}"}
        ],
        "unresolved": [],
    }
    canon = canonicalize_plan_input(raw, business)
    assert canon["probe_requests"] == []


def test_canonicalize_invented_metric_names(business, rules):
    planner = Planner(None, rules, TemporalResolver(), Settings())
    q = Question(
        id="q_test",
        business_id=business.business_id,
        question="Tong so don hang",
        as_of_date=date(2026, 3, 1),
    )
    raw = {
        "metric": "order_count",
        "tables": [business.tables[0].name],
        "sql": f"SELECT COUNT(*) FROM {business.tables[0].name}",
    }
    planner.model = SimpleNamespace(generate_json=lambda *a, **k: copy.deepcopy(raw))
    plan = planner.make_plan(q, business, deadline=time.monotonic() + 30)
    assert plan.metric == "generic_sql"


def test_canonicalize_invalid_time_field_immunity_on_executable_sql(business, rules):
    planner = Planner(None, rules, TemporalResolver(), Settings())
    q = Question(
        id="q_test",
        business_id=business.business_id,
        question="Doanh thu",
        as_of_date=date(2026, 3, 1),
    )
    tbl_name = business.tables[0].name
    raw = {
        "metric": "generic_sql",
        "tables": [tbl_name],
        "sql": f"SELECT SUM(total_due) FROM {tbl_name}",
        "windows": [
            {
                "role": "invalid_role",
                "mode": "invalid_mode",
                "start": "2026-03-01",
                "end_exclusive": "2026-04-01",
                "field": "unrelated_tbl.non_existent_time",
            }
        ],
    }
    planner.model = SimpleNamespace(generate_json=lambda *a, **k: copy.deepcopy(raw))
    plan = planner.make_plan(q, business, deadline=time.monotonic() + 30)
    assert len(plan.windows) == 1
    assert plan.windows[0].role == "target"
    assert plan.windows[0].mode == "period"
    assert plan.windows[0].field == f"{tbl_name}.ordered_at"


def test_percentage_scaling_for_decimal_div():
    sql = (
        "SELECT decimal_div(COUNT(DISTINCT CASE WHEN trang_thai = 'cancelled' THEN ma_don_hang END), "
        "COUNT(DISTINCT ma_don_hang)) AS cancellation_rate FROM don_hang"
    )
    scaled = _scale_scalar_percent_sql(sql)
    assert "100.0 * CAST((decimal_div" in scaled
    assert "AS cancellation_rate" in scaled


def test_canonicalize_cancellation_rate_sets_percent_and_scales(business):
    tbl_name = business.tables[0].name
    raw = {
        "metric": "cancellation_rate",
        "tables": [tbl_name],
        "sql": f"SELECT decimal_div(1, 4) AS cancellation_rate FROM {tbl_name}",
        "output": {"type": "number", "shape": "scalar", "rounding": "none"},
    }
    canon = canonicalize_plan_input(raw, business)
    assert canon["output"]["type"] == "percent"
    assert canon["output"]["rounding"] == "two_decimals"
    assert "100.0 * CAST((decimal_div" in canon["sql"]
    plan = QueryPlan.model_validate(canon)
    assert plan.output.type == AnswerType.PERCENT
