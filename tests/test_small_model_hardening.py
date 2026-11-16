from __future__ import annotations

from datetime import date
from pathlib import Path
import pytest

from bizreason.contracts import (
    AnswerType,
    BusinessContext,
    ColumnMeta,
    OutputSpec,
    QueryPlan,
    Question,
    SourceRef,
    TableMeta,
)
from bizreason.llm import (
    ModelError,
    _repair_json_syntax,
    build_chat_messages,
    parse_json_object,
)
from bizreason.plan_input import canonicalize_plan_input
from bizreason.semantic_checks import semantic_plan_errors


# ==============================================================================
# 1. LLM: Gemma-4 Thought Stripping & Small-Model JSON Repair Tests
# ==============================================================================

def test_gemma4_thought_stripping_with_turn_tag():
    text = (
        "<|thought>\n"
        "Need to calculate total revenue for march 2026.\n"
        "Filter status = 'completed'.\n"
        "<|turn>\n"
        '{"metric": "generic_sql", "sql": "SELECT 1"}'
    )
    res = parse_json_object(text)
    assert res == {"metric": "generic_sql", "sql": "SELECT 1"}


def test_gemma4_thought_stripping_leading_lookahead():
    text = (
        "<|thought>\n"
        "Model thought block without turn tag ending immediately before JSON object.\n"
        '{"metric": "generic_sql", "sql": "SELECT 42"}'
    )
    res = parse_json_object(text)
    assert res == {"metric": "generic_sql", "sql": "SELECT 42"}


def test_standard_think_and_thought_tags_stripped():
    text1 = '<think>some reasoning</think>{"key": "val"}'
    assert parse_json_object(text1) == {"key": "val"}

    text2 = '<thought>some reasoning</thought>{"key": "val2"}'
    assert parse_json_object(text2) == {"key": "val2"}


def test_multiline_sql_literal_newlines_repaired():
    raw_payload = (
        '{\n'
        '  "metric": "generic_sql",\n'
        '  "sql": "SELECT order_id,\n'
        '         SUM(amount) AS total\n'
        '  FROM orders\n'
        '  WHERE status = \'completed\'"\n'
        '}'
    )
    res = parse_json_object(raw_payload)
    assert res["metric"] == "generic_sql"
    assert "SELECT order_id,\n" in res["sql"]
    assert "WHERE status = 'completed'" in res["sql"]


def test_repair_json_syntax_comments_and_trailing_commas():
    raw = (
        '{\n'
        '  // Single line JS comment outside string\n'
        '  "metric": "generic_sql",\n'
        '  "tables": ["orders", "items",], // trailing comma in list\n'
        '  "sql": "SELECT * // this is not a comment inside string\nFROM orders",\n'
        '}'
    )
    repaired = _repair_json_syntax(raw)
    assert "// Single line JS comment" not in repaired
    assert "this is not a comment inside string" in repaired
    res = parse_json_object(raw)
    assert res["metric"] == "generic_sql"
    assert res["tables"] == ["orders", "items"]


def test_parse_json_object_strict_dict_enforcement():
    with pytest.raises(ModelError, match="Model output must be a JSON object"):
        parse_json_object("[1, 2, 3]")

    with pytest.raises(ModelError, match="Model output must be a JSON object"):
        parse_json_object("12345")

    with pytest.raises(ModelError, match="Model output must be a JSON object"):
        parse_json_object('"just a string"')

    with pytest.raises(ModelError, match="Model returned invalid JSON"):
        parse_json_object('{"a":')

    with pytest.raises(ModelError, match="Model returned invalid JSON"):
        parse_json_object('{"a": 1} {"b": 2}')


def test_gemma4_chat_messages_keeps_standard_turns():
    g4_msg = build_chat_messages("Shaleen123/gemma4-12b-sql", "System prompt", "User prompt")
    assert g4_msg == [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "User prompt"},
    ]

    g4_alias = build_chat_messages("gemma-4", "System prompt", "User prompt")
    assert g4_alias == [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "User prompt"},
    ]

    # Gemma 2 & 3 must still merge system into user
    g3_msg = build_chat_messages("google/gemma-3-12b-it", "System prompt", "User prompt")
    assert g3_msg == [{"role": "user", "content": "System prompt\n\nUser prompt"}]

    g2_msg = build_chat_messages("google/gemma-2-9b-it", "System prompt", "User prompt")
    assert g2_msg == [{"role": "user", "content": "System prompt\n\nUser prompt"}]


# ==============================================================================
# 2. Plan Input: Wrappers, Echoed Keys, Shorthand SQL & Unsupported
# ==============================================================================

def _col(name: str, role: str | None = None, dtype: str = "string") -> ColumnMeta:
    return ColumnMeta(name=name, original_name=name, dtype=dtype, semantic_role=role)


def _tbl(name: str, grain: str, columns: list[ColumnMeta]) -> TableMeta:
    return TableMeta(
        name=name,
        grain=grain,
        sources=[SourceRef(path=f"{name}.csv")],
        row_count=10,
        raw_row_count=10,
        columns=columns,
    )


@pytest.fixture
def mock_context() -> BusinessContext:
    t_orders = _tbl(
        name="orders",
        grain="order_header",
        columns=[
            _col("order_id", "order_id"),
            _col("customer_id", "customer_id"),
            _col("order_date", "order_date", "date"),
            _col("status", "status"),
            _col("total_amount", "total_amount", "numeric"),
        ],
    )
    t_lines = _tbl(
        name="order_lines",
        grain="order_line",
        columns=[
            _col("order_id", "order_id"),
            _col("product_id", "product_id"),
            _col("qty_sale", "qty_sale", "numeric"),
            _col("line_amount", "line_amount", "numeric"),
        ],
    )
    t_conv = _tbl(
        name="don_vi_quy_doi",
        grain="unit_conversion",
        columns=[
            _col("product_id", "product_id"),
            _col("ty_le_quy_doi", "conversion_rate", "numeric"),
        ],
    )
    return BusinessContext(
        version="1",
        business_id="biz_mock",
        revision="rev1",
        database=Path("mock.db"),
        manifest={},
        tables=[t_orders, t_lines, t_conv],
        readme="Business test readme",
    )


def test_unwrap_plan_field_guide_wrapper(mock_context: BusinessContext):
    wrapped = {
        "plan_field_guide": {
            "metric": "generic_sql",
            "sql": "SELECT count(*) FROM orders",
            "tables": ["orders"],
            "rule_refs": [],
        }
    }
    canon = canonicalize_plan_input(wrapped, mock_context)
    assert canon["metric"] == "generic_sql"
    assert canon["sql"] == "SELECT count(*) FROM orders"
    assert canon["tables"] == ["orders"]
    assert "plan_field_guide" not in canon


def test_unwrap_query_plan_and_plan_wrappers(mock_context: BusinessContext):
    qp_wrapped = {
        "query_plan": {
            "metric": "generic_sql",
            "sql": "SELECT 1",
            "tables": ["orders"],
        }
    }
    canon = canonicalize_plan_input(qp_wrapped, mock_context)
    assert canon["metric"] == "generic_sql"
    assert canon["sql"] == "SELECT 1"

    p_wrapped = {
        "plan": {
            "metric": "generic_sql",
            "sql": "SELECT 2",
            "tables": ["orders"],
        }
    }
    canon2 = canonicalize_plan_input(p_wrapped, mock_context)
    assert canon2["metric"] == "generic_sql"
    assert canon2["sql"] == "SELECT 2"


def test_strip_echoed_prompt_keys(mock_context: BusinessContext):
    raw = {
        "runtime_rules": {"rule1": "desc"},
        "business_readme": "Text...",
        "catalog": ["orders", "order_lines"],
        "declared_data_end": "2026-03-31",
        "nested_field_definitions_not_output_fields": {},
        "required_rule_refs_by_metric": {},
        "joins_verified_on_full_data": [],
        "context_diagnostics": {},
        "temporal_hints_not_a_substitute_for_reading_question": {},
        "entity_retrieval_not_probabilities": {},
        "controller_budget": {},
        "probe_results": {},
        "feedback_on_latest_candidate": {},
        "context_compression": {},
        "optional_field_details": {},
        "metric": "generic_sql",
        "sql": "SELECT count(*) FROM orders",
        "tables": ["orders"],
        "rule_refs": [],
    }
    canon = canonicalize_plan_input(raw, mock_context)
    plan = QueryPlan.model_validate(canon)
    assert plan.metric == "generic_sql"
    assert plan.sql == "SELECT count(*) FROM orders"


def test_recover_sql_shorthand_and_infer_tables(mock_context: BusinessContext):
    raw = {
        "query": "SELECT o.order_id, l.qty_sale FROM orders o JOIN order_lines l ON o.order_id = l.order_id"
    }
    canon = canonicalize_plan_input(raw, mock_context)
    assert canon["metric"] == "generic_sql"
    assert "SELECT o.order_id" in canon["sql"]
    assert set(canon["tables"]) == {"orders", "order_lines"}
    assert canon["rule_refs"] == []
    plan = QueryPlan.model_validate(canon)
    assert plan.metric == "generic_sql"
    assert set(plan.tables) == {"orders", "order_lines"}


def test_recover_conversational_unsupported_plan(mock_context: BusinessContext):
    raw = {
        "type": "unanswerable",
        "reason": "Catalog contains no table for customer credit score",
    }
    canon = canonicalize_plan_input(raw, mock_context)
    assert canon["metric"] == "unsupported"
    assert canon["tables"] == []
    assert canon["rule_refs"] == []
    assert canon["output"]["type"] == "unanswerable"
    assert canon["unsupported"]["kind"] == "missing_field"
    assert canon["unsupported"]["explanation"] == "Catalog contains no table for customer credit score"
    plan = QueryPlan.model_validate(canon)
    assert plan.metric == "unsupported"
    assert plan.unsupported.kind == "missing_field"


# ==============================================================================
# 3. Semantic Checks: Percent Scale, Date on Table, Unit Conversion Join
# ==============================================================================

def test_semantic_guard_percent_missing_100_scale(mock_context: BusinessContext):
    q = Question(
        id="q_pct",
        business_id="biz_mock",
        question="Tỷ lệ đơn hàng bị hủy là bao nhiêu %?",
        as_of_date=date(2026, 3, 10),
    )
    plan_unscaled = QueryPlan(
        metric="generic_sql",
        tables=["orders"],
        sql="SELECT count(case when status = 'cancelled' then 1 end) * 1.0 / count(*) FROM orders",
        output=OutputSpec(type=AnswerType.PERCENT, shape="scalar", rounding="two_decimals"),
        rule_refs=[],
    )
    errors = semantic_plan_errors(plan_unscaled, mock_context, q)
    assert any("PERCENT_MISSING_100_SCALE" in err for err in errors)

    plan_scaled = QueryPlan(
        metric="generic_sql",
        tables=["orders"],
        sql="SELECT 100.0 * count(case when status = 'cancelled' then 1 end) / count(*) FROM orders",
        output=OutputSpec(type=AnswerType.PERCENT, shape="scalar", rounding="two_decimals"),
        rule_refs=[],
    )
    errors_scaled = semantic_plan_errors(plan_scaled, mock_context, q)
    assert not any("PERCENT_MISSING_100_SCALE" in err for err in errors_scaled)


def test_semantic_guard_output_type_must_be_date_on_table(mock_context: BusinessContext):
    q_singular = Question(
        id="q_date",
        business_id="biz_mock",
        question="Ngày nào có doanh thu cao nhất?",
        as_of_date=date(2026, 3, 10),
    )
    plan_table = QueryPlan(
        metric="generic_sql",
        tables=["orders"],
        sql="SELECT order_date, sum(total_amount) FROM orders GROUP BY order_date ORDER BY sum(total_amount) DESC",
        output=OutputSpec(type=AnswerType.TABLE, shape="table"),
        rule_refs=[],
    )
    errors = semantic_plan_errors(plan_table, mock_context, q_singular)
    assert any("OUTPUT_TYPE_MUST_BE_DATE" in err for err in errors)

    # Question asking for list/breakdown should not trigger this error
    q_list = Question(
        id="q_list",
        business_id="biz_mock",
        question="Bảng doanh thu các ngày trong tháng 3",
        as_of_date=date(2026, 3, 10),
    )
    errors_list = semantic_plan_errors(plan_table, mock_context, q_list)
    assert not any("OUTPUT_TYPE_MUST_BE_DATE" in err for err in errors_list)


def test_semantic_guard_inner_join_on_unit_conversion(mock_context: BusinessContext):
    q = Question(
        id="q_uc",
        business_id="biz_mock",
        question="Tổng số lượng sản phẩm đã bán",
        as_of_date=date(2026, 3, 10),
    )
    plan_inner = QueryPlan(
        metric="generic_sql",
        tables=["order_lines", "don_vi_quy_doi"],
        sql=(
            "SELECT sum(l.qty_sale * coalesce(u.ty_le_quy_doi, 1)) "
            "FROM order_lines l JOIN don_vi_quy_doi u ON l.product_id = u.product_id"
        ),
        output=OutputSpec(type=AnswerType.NUMBER, shape="scalar"),
        rule_refs=[],
    )
    errors = semantic_plan_errors(plan_inner, mock_context, q)
    assert any("INNER_JOIN_ON_UNIT_CONVERSION" in err for err in errors)
    assert "don_vi_quy_doi" in next(e for e in errors if "INNER_JOIN_ON_UNIT_CONVERSION" in e)

    plan_left = QueryPlan(
        metric="generic_sql",
        tables=["order_lines", "don_vi_quy_doi"],
        sql=(
            "SELECT sum(l.qty_sale * coalesce(u.ty_le_quy_doi, 1)) "
            "FROM order_lines l LEFT JOIN don_vi_quy_doi u ON l.product_id = u.product_id"
        ),
        output=OutputSpec(type=AnswerType.NUMBER, shape="scalar"),
        rule_refs=[],
    )
    errors_left = semantic_plan_errors(plan_left, mock_context, q)
    assert not any("INNER_JOIN_ON_UNIT_CONVERSION" in err for err in errors_left)


def test_invalid_metric_still_fails_validation(mock_context: BusinessContext):
    raw = {
        "metric": "fake_invented_metric",
        "tables": ["orders"],
        "sql": "SELECT 1",
    }
    canon = canonicalize_plan_input(raw, mock_context)
    with pytest.raises(Exception):
        QueryPlan.model_validate(canon)


def test_repair_json_syntax_tabs_and_newlines_in_strings():
    raw = '{\n  "query": "SELECT\tcol1,\r\n\tcol2\nFROM table"\n}'
    repaired = _repair_json_syntax(raw)
    parsed = parse_json_object(repaired)
    assert "SELECT\tcol1" in parsed["query"]
    assert "FROM table" in parsed["query"]


def test_semantic_guard_quoted_and_inner_join_unit_conversion(mock_context: BusinessContext):
    q = Question(
        id="q_uc2",
        business_id="biz_mock",
        question="Tổng sản phẩm",
        as_of_date=date(2026, 3, 10),
    )
    # Explicit INNER JOIN
    plan_inner = QueryPlan(
        metric="generic_sql",
        tables=["order_lines", "don_vi_quy_doi"],
        sql="SELECT 1 FROM order_lines INNER JOIN don_vi_quy_doi ON 1=1",
        output=OutputSpec(type=AnswerType.NUMBER, shape="scalar"),
        rule_refs=[],
    )
    errors = semantic_plan_errors(plan_inner, mock_context, q)
    assert any("INNER_JOIN_ON_UNIT_CONVERSION" in err for err in errors)

    # Quoted identifier JOIN "don_vi_quy_doi"
    plan_quoted = QueryPlan(
        metric="generic_sql",
        tables=["order_lines", "don_vi_quy_doi"],
        sql='SELECT 1 FROM order_lines JOIN "don_vi_quy_doi" ON 1=1',
        output=OutputSpec(type=AnswerType.NUMBER, shape="scalar"),
        rule_refs=[],
    )
    errors_quoted = semantic_plan_errors(plan_quoted, mock_context, q)
    assert any("INNER_JOIN_ON_UNIT_CONVERSION" in err for err in errors_quoted)


def test_parse_json_object_raw_sql_recovery():
    raw_sql_output = "```sql\nSELECT SUM(x) FROM tbl;\n```"
    res = parse_json_object(raw_sql_output)
    assert res == {"sql": "SELECT SUM(x) FROM tbl"}


def test_parse_json_object_non_sql_text_still_raises_model_error():
    with pytest.raises(ModelError, match="Model returned invalid JSON"):
        parse_json_object("With respect to your request, invalid")


# ==============================================================================
# 5. Compact SQL Prompt Mode & Constrained-VRAM Hardening Tests
# ==============================================================================

def test_build_slim_catalog_strips_metadata_and_limits_examples():
    from bizreason.planning import _build_slim_catalog

    col1 = ColumnMeta(
        name="col_same",
        original_name="col_same",
        dtype="TEXT",
        examples=["ex1", "ex2", "ex3"],
        null_count=5,
        parse_errors=2,
    )
    col2 = ColumnMeta(
        name="col_diff",
        original_name="Tên khác",
        dtype="TEXT",
        examples=["exA", "exB"],
        semantic_role="status",
    )
    tbl1 = TableMeta(
        name="tbl_p1",
        grain="order_line",
        sources=[SourceRef(path="p1.csv")],
        row_count=100,
        raw_row_count=100,
        coverage={"ordered_at": ("2026-03-01T00:00:00", "2026-03-31T23:59:59")},
        columns=[col1, col2],
    )
    tbl2 = TableMeta(
        name="tbl_p2",
        grain="order_line",
        sources=[SourceRef(path="p2.csv")],
        row_count=200,
        raw_row_count=200,
        coverage={"ordered_at": ("2026-04-01T00:00:00", "2026-04-30T23:59:59")},
        columns=[col1, col2],
    )

    slim = _build_slim_catalog([tbl1, tbl2], max_chars=10000)
    assert len(slim) == 1
    group = slim[0]
    assert "physical_tables" in group
    assert len(group["physical_tables"]) == 2
    # Verify sources and note are stripped
    assert "sources" not in group
    assert "note" not in group
    # Coverage compacted to 10-char dates
    cov = group["physical_tables"][0]["coverage"]["ordered_at"]
    assert cov == ["2026-03-01", "2026-03-31"]

    cols = group["shared_columns"]
    # col_same stripped original_name because original_name == name
    c0 = next(c for c in cols if c["name"] == "col_same")
    assert "original_name" not in c0
    assert "null_count_across_partitions" not in c0
    assert "parse_errors_across_partitions" not in c0
    assert len(c0["examples"]) == 1
    assert c0["examples"] == ["ex1"]

    # col_diff preserved original_name because original_name != name
    c1 = next(c for c in cols if c["name"] == "col_diff")
    assert c1["original_name"] == "Tên khác"
    assert c1["semantic_role"] == "status"
    assert len(c1["examples"]) == 1


def test_compact_sql_prompt_activation_under_budget(business: BusinessContext, rules):
    import json
    import time
    from bizreason.planning import Planner, COMPACT_SYSTEM_PROMPT
    from bizreason.config import Settings
    from bizreason.temporal import TemporalResolver

    calls = []

    class MockModel:
        def generate_json(self, system, prompt, *, timeout):
            calls.append((system, prompt))
            return {
                "metric": "sales_revenue",
                "tables": [t.name for t in business.tables],
                "sql": "SELECT 100",
                "parameters": [],
                "rule_refs": rules.required_refs("sales_revenue"),
            }

    model = MockModel()
    settings = Settings(context_characters=11000)
    planner = Planner(model, rules, TemporalResolver(), settings)

    q = Question(
        id="q_compact_test",
        business_id=business.business_id,
        question="Doanh thu tháng 3/2026",
        as_of_date=date(2026, 4, 1),
    )
    plan = planner.make_plan(q, business, deadline=time.monotonic() + 30)

    assert plan.metric == "sales_revenue"
    assert len(calls) == 1
    active_sys, active_prompt = calls[0]
    # Activated COMPACT_SYSTEM_PROMPT
    assert active_sys == COMPACT_SYSTEM_PROMPT
    assert len(active_sys) + len(active_prompt) <= 11000

    parsed_payload = json.loads(active_prompt)
    assert "runtime_rules" not in parsed_payload
    assert "plan_field_guide" not in parsed_payload
    assert "required_rule_refs_by_metric" in parsed_payload
    assert "catalog" in parsed_payload
    assert "question" in parsed_payload


def test_compact_sql_prompt_budget_above_20k_preserves_system_prompt_and_rules(business: BusinessContext, rules):
    import json
    from bizreason.planning import _build_compact_sql_prompt, SYSTEM_PROMPT

    q = Question(
        id="q_compact_20k_test",
        business_id=business.business_id,
        question="Doanh thu tháng 3/2026",
        as_of_date=date(2026, 4, 1),
    )
    active_sys, active_prompt = _build_compact_sql_prompt(
        rules=rules,
        context=business,
        question=q,
        windows=[],
        entities=[],
        feedback=None,
        probe_results=None,
        budget=25000,
    )

    assert active_sys == SYSTEM_PROMPT
    assert len(active_sys) + len(active_prompt) <= 25000
    parsed = json.loads(active_prompt)
    assert "runtime_rules" in parsed
    assert parsed["runtime_rules"] == rules.render()


def test_compact_sql_fallback_on_http_500_provider_error(business: BusinessContext, rules):
    import time
    from bizreason.planning import Planner, COMPACT_SYSTEM_PROMPT
    from bizreason.config import Settings
    from bizreason.temporal import TemporalResolver
    from bizreason.llm import ProviderError

    calls = []

    class MockModel500:
        def generate_json(self, system, prompt, *, timeout):
            calls.append((system, prompt))
            if len(calls) == 1:
                # Trigger HTTP 500 CUDA OOM on first large call
                raise ProviderError("Provider HTTP 500: CUDA out of memory during prefill")
            return {
                "metric": "generic_sql",
                "tables": [t.name for t in business.tables],
                "sql": "SELECT 1",
                "parameters": [],
                "rule_refs": [],
            }

    model = MockModel500()
    # Large budget so first call uses default SYSTEM_PROMPT (>11500 chars)
    settings = Settings(context_characters=65000)
    planner = Planner(model, rules, TemporalResolver(), settings)

    q = Question(
        id="q_fallback_500",
        business_id=business.business_id,
        question="Doanh thu",
        as_of_date=date(2026, 4, 1),
    )
    plan = planner.make_plan(q, business, deadline=time.monotonic() + 30)

    assert plan.metric == "generic_sql"
    assert len(calls) == 2
    # First call failed with 500 on large prompt
    assert len(calls[0][0]) + len(calls[0][1]) > 11500
    # Second call retried with compact system prompt and budget <= 11000
    assert calls[1][0] == COMPACT_SYSTEM_PROMPT
    assert len(calls[1][0]) + len(calls[1][1]) <= 11000


def test_compact_sql_budget_too_small_raises_model_error(business: BusinessContext, rules):
    import time
    from bizreason.planning import Planner
    from bizreason.config import Settings
    from bizreason.temporal import TemporalResolver

    class MockModel:
        def generate_json(self, system, prompt, *, timeout):
            return {"metric": "generic_sql", "sql": "SELECT 1"}

    model = MockModel()
    settings = Settings(context_characters=500)
    planner = Planner(model, rules, TemporalResolver(), settings)

    q = Question(
        id="q_tiny_budget",
        business_id=business.business_id,
        question="Doanh thu",
        as_of_date=date(2026, 4, 1),
    )
    with pytest.raises(ModelError, match="Mandatory context exceeds configured character budget; no silent truncation"):
        planner.make_plan(q, business, deadline=time.monotonic() + 30)


# ==============================================================================
# IMP-GEMMA4-SQL-PLAN-CANONICALIZATION Tests
# ==============================================================================

def test_canonicalize_plan_input_dict_objects_in_output_columns(mock_context: BusinessContext):
    raw_plan = {
        "metric": "generic_sql",
        "tables": ["orders"],
        "sql": "SELECT SUM(money_vnd(can_tra_ncc)) as total_purchase_value FROM orders",
        "output": {
            "columns": [
                {"expression": "SUM(money_vnd(can_tra_ncc))", "title": "Tổng giá trị phiếu nhập hàng"},
                {"column": "col_b"},
                {"name": "col_c"},
                {"alias": "col_d"},
                1234,
            ],
            "type": "number",
            "shape": "scalar",
            "rounding": "money",
        },
        "rule_refs": [],
    }
    canon = canonicalize_plan_input(raw_plan, mock_context)
    plan = QueryPlan.model_validate(canon)
    assert plan.output.columns == ["Tổng giá trị phiếu nhập hàng", "col_b", "col_c", "col_d", "1234"]


def test_canonicalize_plan_input_sql_expression_in_group_by_and_required_fields(mock_context: BusinessContext, rules):
    raw_plan = {
        "metric": "sales_revenue",
        "tables": ["orders"],
        "sql": "SELECT SUM(total_amount) AS sales_revenue FROM orders WHERE status = 'completed' GROUP BY strftime('%Y-%m', order_date)",
        "group_by": ["strftime('%Y-%m', order_date)"],
        "required_fields": ["iso_date(order_date)"],
        "output": {"type": "number", "shape": "scalar", "rounding": "money", "columns": ["sales_revenue"]},
        "rule_refs": [],
    }
    canon = canonicalize_plan_input(raw_plan, mock_context)
    plan = QueryPlan.model_validate(canon)
    assert plan.group_by == ["orders.order_date"]
    assert "orders.order_date" in plan.required_fields

    from bizreason.planning import validate_plan
    q = Question(id="q_rev", business_id="biz_mock", question="Doanh thu?", as_of_date=date(2026, 3, 10))
    errors = validate_plan(plan, mock_context, rules, q)
    assert not any("INVALID_FILTER_OR_GROUP_FIELD" in e for e in errors)
    assert not any("MISSING_REQUIRED_FIELD" in e for e in errors)


def test_canonicalize_plan_input_unmatched_expression_in_group_by_dropped(mock_context: BusinessContext):
    raw_plan = {
        "metric": "generic_sql",
        "tables": ["orders"],
        "sql": "SELECT strftime('%Y', 'now'), count(*) FROM orders GROUP BY strftime('%Y', 'now')",
        "group_by": ["strftime('%Y', 'now')"],
        "rule_refs": [],
    }
    canon = canonicalize_plan_input(raw_plan, mock_context)
    plan = QueryPlan.model_validate(canon)
    assert plan.group_by == []


def test_canonicalize_plan_input_unscaled_percent_ratio_scaled(mock_context: BusinessContext):
    raw_plan = {
        "metric": "cancellation_rate",
        "tables": ["orders"],
        "sql": "SELECT CAST(COUNT(CASE WHEN status = 'cancelled' THEN 1 END) AS FLOAT) / COUNT(*) AS cancellation_rate FROM orders",
        "output": {"type": "percent", "shape": "scalar", "rounding": "two_decimals", "columns": ["cancellation_rate"]},
        "rule_refs": [],
    }
    canon = canonicalize_plan_input(raw_plan, mock_context)
    plan = QueryPlan.model_validate(canon)
    assert plan.sql.startswith("SELECT 100.0 * (")
    assert "cancellation_rate" in plan.sql

    q = Question(
        id="q_cancel_rate",
        business_id="biz_mock",
        question="Tỷ lệ đơn hàng bị hủy là bao nhiêu %?",
        as_of_date=date(2026, 3, 10),
    )
    errors = semantic_plan_errors(plan, mock_context, q)
    assert not any("PERCENT_MISSING_100_SCALE" in err for err in errors)


def test_canonicalize_plan_input_already_scaled_percent_sql_preserved(mock_context: BusinessContext):
    raw_plan = {
        "metric": "cancellation_rate",
        "tables": ["orders"],
        "sql": "SELECT 100.0 * COUNT(CASE WHEN status = 'cancelled' THEN 1 END) / COUNT(*) AS cancellation_rate FROM orders",
        "output": {"type": "percent", "shape": "scalar", "rounding": "two_decimals", "columns": ["cancellation_rate"]},
        "rule_refs": [],
    }
    canon = canonicalize_plan_input(raw_plan, mock_context)
    plan = QueryPlan.model_validate(canon)
    assert plan.sql == raw_plan["sql"]


def test_repair_json_syntax_truncated_at_max_tokens():
    raw_truncated_a = (
        "<|thought>\n"
        "Let's write a query for sales revenue\n"
        "<|turn>\n"
        '{"metric": "sales_revenue", "sql": "SELECT SUM(amount) FROM orders", "tables": ["orders"], "windows": [{"role": "target", "start": "2026-01-01"'
    )
    parsed_a = parse_json_object(raw_truncated_a)
    assert parsed_a["metric"] == "sales_revenue"
    assert parsed_a["sql"] == "SELECT SUM(amount) FROM orders"
    assert parsed_a["tables"] == ["orders"]
    assert parsed_a["windows"] == [{"role": "target", "start": "2026-01-01"}]

    raw_truncated_b = '{"metric": "generic_sql", "sql": "SELECT * FROM orders WHERE status = \'completed'
    parsed_b = parse_json_object(raw_truncated_b)
    assert parsed_b["metric"] == "generic_sql"
    assert parsed_b["sql"] == "SELECT * FROM orders WHERE status = 'completed"

    raw_truncated_c = '{"metric": "generic_sql", "sql": "SELECT 1", "tables": ["orders"], "windows'
    parsed_c = parse_json_object(raw_truncated_c)
    assert parsed_c["metric"] == "generic_sql"
    assert parsed_c["sql"] == "SELECT 1"
    assert parsed_c["tables"] == ["orders"]

    raw_truncated_d = '{"metric": "generic_sql", "sql": "SELECT 1", "tables": '
    parsed_d = parse_json_object(raw_truncated_d)
    assert parsed_d["metric"] == "generic_sql"
    assert parsed_d["sql"] == "SELECT 1"
    assert parsed_d["tables"] is None

    raw_truncated_e = '{"metric": "generic_sql", "sql": "SELECT 1", '
    parsed_e = parse_json_object(raw_truncated_e)
    assert parsed_e["metric"] == "generic_sql"
    assert parsed_e["sql"] == "SELECT 1"


def test_compact_system_prompt_guidance():
    from bizreason.planning import COMPACT_SYSTEM_PROMPT
    assert "For output.type='percent', always multiply by 100.0 in SQL" in COMPACT_SYSTEM_PROMPT
    assert "Do not wrap column names in SQL functions inside group_by or required_fields" in COMPACT_SYSTEM_PROMPT
    assert "output.columns must be a list of strings" in COMPACT_SYSTEM_PROMPT


def test_parse_json_object_fallback_a_regex_sql():
    text = (
        'Model returned garbage around json: '
        '{"metric": "generic_sql", "sql": "SELECT SUM(total_amount) FROM orders WHERE status = \\"completed\\"", "broken_key": {'
    )
    res = parse_json_object(text)
    assert res["sql"] == 'SELECT SUM(total_amount) FROM orders WHERE status = "completed"'
    assert res["metric"] == "generic_sql"


def test_parse_json_object_unescaped_double_quotes_in_sql():
    raw = (
        '{\n'
        '  "metric": "sales_revenue",\n'
        '  "sql": "SELECT SUM(tong_tien) AS "tong_doanh_thu" FROM orders WHERE ten LIKE "%Natri clorid%"",\n'
        '  "type": "number"\n'
        '}'
    )
    res = parse_json_object(raw)
    assert res["sql"] == 'SELECT SUM(tong_tien) AS "tong_doanh_thu" FROM orders WHERE ten LIKE "%Natri clorid%"'
    assert res["metric"] == "sales_revenue"
    assert res.get("output") == {"type": "number"}

    raw_final = (
        '{"metric": "generic_sql", "sql": "SELECT SUM(tong_tien) AS "tong_doanh_thu" FROM orders WHERE ten LIKE "%Natri clorid%""}'
    )
    res_final = parse_json_object(raw_final)
    assert res_final["sql"] == 'SELECT SUM(tong_tien) AS "tong_doanh_thu" FROM orders WHERE ten LIKE "%Natri clorid%"'
    assert res_final["metric"] == "generic_sql"


def test_parse_json_object_truncated_ending_mid_sql():
    raw_trunc = (
        '{"metric": "sales_revenue", "sql": "SELECT SUM(tong_tien) AS "tong_doanh_thu" FROM orders WHERE ten LIKE "%Natri'
    )
    res_trunc = parse_json_object(raw_trunc)
    assert res_trunc["sql"] == 'SELECT SUM(tong_tien) AS "tong_doanh_thu" FROM orders WHERE ten LIKE "%Natri'
    assert res_trunc["metric"] == "sales_revenue"


def test_parse_json_object_fallback_b_standalone_sql():
    text = (
        "Dưới đây là câu lệnh SQL để truy vấn doanh thu:\n\n"
        "SELECT order_id, total_amount FROM orders WHERE status = 'completed';\n\n"
        "Chúc bạn thành công!"
    )
    res = parse_json_object(text)
    assert res["sql"] == "SELECT order_id, total_amount FROM orders WHERE status = 'completed'"


def test_canonicalize_plan_input_executable_sql_auto_fills_required_and_cleans_metadata(mock_context: BusinessContext, rules):
    from bizreason.planning import validate_plan

    raw_plan = {
        "sql": "SELECT order_id, total_amount FROM orders WHERE status = 'completed'",
        "group_by": ["unresolvable_expr(1)"],
        "filters": [{"field": "unresolvable_col", "operator": "eq", "values": ["x"]}],
    }
    canon = canonicalize_plan_input(raw_plan, mock_context)
    plan = QueryPlan.model_validate(canon)
    assert plan.tables == ["orders"]
    assert "orders.order_id" in plan.required_fields
    assert "orders.total_amount" in plan.required_fields
    assert plan.group_by == []
    assert plan.filters == []

    q = Question(id="q_sql_only", business_id="biz_mock", question="Doanh thu?", as_of_date=date(2026, 3, 10))
    errors = validate_plan(plan, mock_context, rules, q)
    assert not any("INVALID_FILTER_OR_GROUP_FIELD" in e for e in errors)
    assert not any("MISSING_REQUIRED_FIELD" in e for e in errors)


