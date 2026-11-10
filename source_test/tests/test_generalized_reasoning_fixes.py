from __future__ import annotations

import copy
import csv
import json
import sqlite3
import time
from decimal import Decimal

import pytest
from pydantic import ValidationError

from bizreason.config import Settings
from bizreason.contracts import OutputSpec, ProbeRequest, QueryPlan, Question
from bizreason.data.ingestion import BusinessBuilder
from bizreason.data.vehicles import vehicle_parts
from bizreason.execution import execute_sql, register_helpers
from bizreason.llm import Usage
from bizreason.pipeline import Solver
from bizreason.plan_input import canonicalize_plan_input
from bizreason.planning import validate_plan
from bizreason.planning import Planner
from bizreason.prompt_budget import bounded_observations, join_reference
from bizreason.semantic_checks import semantic_plan_errors
from bizreason.temporal import TemporalResolver
from bizreason.utils import normalize_lookup


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)


class RecordedModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.usage = Usage()
        self.prompts = []

    def generate_json(self, system, prompt, *, timeout):
        self.usage.calls += 1
        self.prompts.append(json.loads(prompt))
        return copy.deepcopy(next(self.responses))


@pytest.mark.parametrize("model", ["Vision", "Winner X", "Future"])
def test_vehicle_identity_is_independent_of_model_spelling(model, workspace_tmp, rules):
    directory = workspace_tmp / "random_garage"
    directory.mkdir()
    write_csv(directory / "events.csv", [
        ["vehicle_description", "event_at", "line_amount"],
        [f"{model} 70A1-111.22", "2027-05-01", 200],
        [f"{model.lower().replace(' ', '')} 70 A1 111 22", "2027-05-02", 0],
        [f"{model} 71B2-222.33", "2027-05-02", 100],
        ["Another model 72C3-333.44", "2027-05-02", 100],
    ])
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    table, = context.tables
    assert table.role("vehicle_plate")
    result = execute_sql(context.database,
        f'SELECT COUNT(DISTINCT {table.role("vehicle_plate")}) FROM "{table.name}" WHERE {table.role("vehicle_model") }=?',
        [model.lower().replace(" ", "")], [table.name])
    assert result.rows == [[2]]  # Includes the zero-price visit; does not count display aliases twice.
    plan = QueryPlan(metric="generic_sql", tables=[table.name], rule_refs=[rules.records[0].id],
        sql=f'SELECT COUNT(DISTINCT vehicle_description) FROM "{table.name}"', output=OutputSpec(type="number"))
    question = Question(id="unseen", business_id=context.business_id,
        question=f"Có bao nhiêu chiếc xe {model} khác nhau?", as_of_date="2027-05-03")
    assert any(e.startswith("ENTITY_IDENTITY_REQUIRED") for e in validate_plan(plan, context, rules, question))
    for query in [f'SELECT COUNT(DISTINCT LOWER(TRIM(vehicle_description))) FROM "{table.name}"',
                  f'SELECT COUNT(DISTINCT t."vehicle_description") FROM "{table.name}" t']:
        other = plan.model_copy(update={"sql": query})
        abbreviated = question.model_copy(update={"question": f"Tháng này bn chiếc {model} khác nhau?"})
        assert any(e.startswith("ENTITY_IDENTITY_REQUIRED") for e in semantic_plan_errors(other, context, abbreviated))


@pytest.mark.parametrize("value", [None, "unknown", "Vision 70A1-111.22 / 71B2-222.33", "Vision 70A1-111.222"])
def test_vehicle_parser_does_not_guess_missing_or_ambiguous_identity(value):
    assert vehicle_parts(value) == (None, None)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("language", ["vi", "en"])
def test_installment_ledger_uses_keys_and_typed_money_not_row_order(reverse, language, workspace_tmp, rules):
    directory = workspace_tmp / "another_ledger"
    directory.mkdir()
    headers = (["STT", "Ngày", "Giá", "Trả trước", "Ngày góp", "Đã góp"] if language == "vi" else
               ["contract_id", "contract_date", "contract_amount", "down_payment", "payment_date", "payment_amount"])
    rows = [
        ["B", None, None, None, "2027-06-01", 200],
        ["A", "2027-04-01", "2,5tr", "500k", None, None],
        ["B", "2027-04-03", 3000, 1000, "2027-05-05", 500],
        ["A", None, None, None, "2027-05-03", "500.000đ"],
        ["A", None, None, None, "2027-05-03", "500.000đ"],
        ["C", "2027-08-01", 9_000_000, 1_000_000, None, None],
    ]
    write_csv(directory / "contracts.csv", [headers, *(list(reversed(rows)) if reverse else rows)])
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    table, = context.tables
    assert table.grain == "installment_ledger"
    assert table.row_count == 6  # Identical non-order payment events are both retained.
    assert not any(c.parse_errors for c in table.columns)
    key, bought, amount, upfront, paid_at, paid_amount = [table.role(r) for r in
        ["contract_id", "contract_at", "contract_amount", "upfront_amount", "paid_at", "paid_amount"]]
    sql = f'''WITH h AS (
      SELECT "{key}" AS k,"{amount}" AS amount,"{upfront}" AS upfront
      FROM "{table.name}" WHERE "{bought}" IS NOT NULL AND "{bought}" < ?
    ), p AS (
      SELECT "{key}" AS k,decimal_sum("{paid_amount}") AS paid
      FROM "{table.name}" WHERE "{paid_at}" < ? GROUP BY "{key}"
    ) SELECT decimal_sum(decimal_positive(decimal_sub(decimal_sub(h.amount,h.upfront),COALESCE(p.paid,0))))
      FROM h LEFT JOIN p ON h.k=p.k'''
    result = execute_sql(context.database, sql, ["2027-06-01", "2027-06-01"], [table.name])
    assert Decimal(result.rows[0][0]) == 1_001_500
    plan = QueryPlan(metric="generic_sql", tables=[table.name], rule_refs=[rules.records[0].id],
        output=OutputSpec(type="number"), sql=f'SELECT ROW_NUMBER() OVER () FROM "{table.name}"')
    question = Question(id="different", business_id=context.business_id, question="Còn nợ?", as_of_date="2027-05-31")
    assert semantic_plan_errors(plan, context, question) == [
        "UNSTABLE_LEDGER_GROUPING:use recorded contract_id, not arbitrary row ordering"]


def test_redundant_entity_json_is_canonicalized_without_changing_sql(business, plan_factory):
    table = next(t for t in business.tables if t.role("product_name"))
    raw = plan_factory("generic_sql").model_dump(mode="json")
    raw["entity_constraints"] = [{"field": f"{table.name}.{table.role('product_name')}",
        "value": "A", "lookup_normalized": True}]
    raw["output"]["column_types"] = None
    before = copy.deepcopy(raw)
    result = canonicalize_plan_input(raw, business)
    assert raw == before
    constraint = result["entity_constraints"][0]
    assert constraint["table"] == table.name
    assert constraint["column"] == table.role("product_name")
    assert constraint["lookup_normalized"] == "a"
    assert result["sql"] == raw["sql"] and result["parameters"] == raw["parameters"]
    QueryPlan.model_validate(result)
    raw["entity_constraints"][0]["table"] = "different_table"
    with pytest.raises(ValueError, match="Conflicting"):
        canonicalize_plan_input(raw, business)


def test_ambiguous_entity_values_are_not_flattened(business, plan_factory):
    raw = plan_factory("generic_sql").model_dump(mode="json")
    raw["entity_constraints"] = [{"table": business.tables[0].name, "column": "x",
        "lookup_normalized": ["one", "two"]}]
    with pytest.raises(ValidationError):
        QueryPlan.model_validate(canonicalize_plan_input(raw, business))


@pytest.mark.parametrize("keep_unresolved", [False, True])
def test_final_repair_with_probe_requests_does_not_starve_sql_execution(keep_unresolved, business, rules, plan_factory):
    table = next(t for t in business.tables if t.grain == "order_line")
    first = plan_factory("generic_sql", rule_refs=[rules.records[0].id], sql=f'SELECT missing FROM "{table.name}"')
    repaired = first.model_copy(update={
        "sql": f'SELECT COUNT(DISTINCT "{table.role("order_id")}") FROM "{table.name}"',
        "probe_requests": [ProbeRequest(purpose="optional extra fact", sql=f'SELECT COUNT(*) FROM "{table.name}"')],
        "unresolved": ["Necessary condition still unknown"] if keep_unresolved else [],
    })
    model = RecordedModel([first.model_dump(mode="json"), repaired.model_dump(mode="json")])
    question = Question(id="not_a_benchmark_id", business_id=business.business_id,
                        question="bao nhiêu đơn?", as_of_date="2026-03-04")
    prediction, trace = Solver(model, rules, TemporalResolver(), Settings(max_model_calls=2, max_repairs=1)).solve(question, business)
    assert model.usage.calls == 2
    assert model.prompts[-1]["controller_budget"]["must_finalize"] is True
    assert not trace.probes
    if keep_unresolved:
        assert prediction.type == "abstain"
    else:
        assert prediction.answer == 5
        assert len(trace.candidates) == 2 and trace.candidates[-1].verified


def test_money_helper_rejects_ambiguous_text_and_preserves_nulls():
    db = sqlite3.connect(":memory:")
    try:
        register_helpers(db)
        assert db.execute("SELECT money_vnd('2,5tr'),money_vnd('500k'),iso_date(NULL)").fetchone() == ("2500000.0", "500000", None)
        assert db.execute("SELECT money_vnd('1,234','en')").fetchone() == ("1234",)
        with pytest.raises(sqlite3.OperationalError):
            db.execute("SELECT money_vnd('1,234')").fetchone()
    finally:
        db.close()


@pytest.mark.parametrize("customer", ["Nghiêm Minh Ánh", "Bùi Khánh Ngọc", "Lâm Hoàng Quân"])
def test_customer_debt_contract_handles_other_names_dates_and_payment_events(customer, workspace_tmp, rules):
    directory = workspace_tmp / "new_customer_shop"
    directory.mkdir()
    write_csv(directory / "customers.csv", [["customer_id", "customer_name"], ["K9", customer], ["K8", "Other person"]])
    write_csv(directory / "orders.csv", [
        ["order_id", "customer_id", "ordered_at", "status", "payment_method", "total_due"],
        ["A", "K9", "2028-02-10", "completed", "debt", 50000],
        ["B", "K9", "2028-01-20", "returned", "debt", 30000],
        ["C", "K9", "2028-02-11", "completed", "cash", 90000],
        ["D", "K9", "2028-02-12", "cancelled", "debt", 40000],
        ["E", "K8", "2028-02-12", "completed", "debt", 40000],
    ])
    write_csv(directory / "payments.csv", [
        ["order_id", "paid_at", "paid_amount"],
        ["A", "2028-02-10", 10000], ["A", "2028-02-13", "7k"],
        ["A", "2028-03-01", 9000], ["B", "2028-02-15", 5000],
        ["C", "2028-02-11", 90000], ["D", "2028-02-20", 1000],
    ])
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    customers = next(t for t in context.tables if t.role("customer_name"))
    orders = next(t for t in context.tables if t.grain == "order_header")
    payments = next(t for t in context.tables if t.grain == "payment_event")
    sql = f'''WITH selected AS (
      SELECT o.* FROM "{orders.name}" o JOIN "{customers.name}" c ON o.customer_id=c.customer_id
      WHERE lookup(c.customer_name)=lookup(?) AND o.status<>'cancelled' AND o.payment_method='debt'
    ), upfront AS (
      SELECT o.order_id,COALESCE(SUM(p.paid_amount),0) AS paid FROM selected o
      LEFT JOIN "{payments.name}" p ON o.order_id=p.order_id AND p.paid_at=o.ordered_at GROUP BY o.order_id
    ), incurred AS (
      SELECT COALESCE(SUM(MAX(o.total_due-u.paid,0)),0) AS value FROM selected o JOIN upfront u USING(order_id)
      WHERE o.ordered_at>='2028-02-01' AND o.ordered_at<'2028-03-01'
    ), repaid AS (
      SELECT COALESCE(SUM(p.paid_amount),0) AS value FROM selected o JOIN "{payments.name}" p USING(order_id)
      WHERE p.paid_at>o.ordered_at AND p.paid_at>='2028-02-01' AND p.paid_at<'2028-03-01'
    ) SELECT value FROM (SELECT 1 AS ordering,value FROM incurred UNION ALL SELECT 2,value FROM repaid) ORDER BY ordering'''
    raw = QueryPlan(metric="generic_sql", tables=[t.name for t in context.tables],
        rule_refs=[rules.records[0].id], sql=sql, parameters=[normalize_lookup(customer)],
        output=OutputSpec(type="list", shape="list", element_type="number", rounding="money")).model_dump(mode="json")
    raw["entity_constraints"] = [{"field": f"{customers.name}.customer_name", "value": customer, "lookup_normalized": True}]
    raw["output"]["column_types"] = None
    question = Question(id="a_new_question", business_id=context.business_id,
        question=f"{customer} tháng 2/2028 nợ thêm, trả nợ bao nhiêu?", as_of_date="2028-03-02")
    model = RecordedModel([raw])
    prediction, trace = Solver(model, rules, TemporalResolver(), Settings()).solve(question, context)
    assert prediction.type == "list" and prediction.answer == [40000, 12000]
    assert not trace.errors and model.usage.calls == 1


def test_join_references_restore_only_verified_edges(business, plan_factory):
    edge = business.joins[0]
    raw = plan_factory("generic_sql").model_dump(mode="json")
    raw["joins"] = [{"join_id": join_reference(edge)}]
    result = canonicalize_plan_input(raw, business)
    assert result["joins"] == [edge.model_dump(mode="json")]
    raw["joins"][0]["left"] = "invented_table"
    with pytest.raises(ValueError, match="Conflicting join"):
        canonicalize_plan_input(raw, business)
    raw["joins"] = [{"join_id": "unknown"}]
    with pytest.raises(ValueError, match="Unknown join"):
        canonicalize_plan_input(raw, business)


def test_ordinal_column_does_not_invent_order_identity(workspace_tmp, rules):
    directory = workspace_tmp / "inventory_sheet"
    directory.mkdir()
    write_csv(directory / "stock.csv", [["STT", "Ngày", "Hàng", "SL"],
        [1, "2028-01-01", "A", 20], [2, "2028-01-01", "B", 40]])
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    assert all(t.role("order_id") is None and t.grain != "order_line" for t in context.tables)


def test_large_observation_preview_does_not_destroy_repair_budget(business, rules, plan_factory):
    response = plan_factory("generic_sql", sql="SELECT 1").model_dump(mode="json")
    model = RecordedModel([response])
    probes = [{"purpose": "inspect data", "status": "success", "columns": ["wide_text"],
        "preview": [["X" * 2000] for _ in range(20)], "row_count": 100,
        "full_result_hash": "immutable_hash", "errors": []}]
    before = copy.deepcopy(probes)
    planner = Planner(model, rules, TemporalResolver(), Settings(context_characters=20000))
    planner.make_plan(Question(id="new", business_id=business.business_id,
        question="count orders", as_of_date="2026-03-04"), business,
        time.monotonic() + 30, probe_results=probes)
    assert probes == before
    sent = model.prompts[-1]["probe_results"][0]
    assert sent["preview_rows_omitted"] == 20
    assert sent["row_count"] == 100 and sent["full_result_hash"] == "immutable_hash"
    assert sent["preview_complete"] is False
    assert bounded_observations(probes)[0]["columns"] == ["wide_text"]
