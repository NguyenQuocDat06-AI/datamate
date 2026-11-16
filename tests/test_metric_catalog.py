"""Executable metric formulas are fixed by registration, not question-side JSON."""
from copy import deepcopy
from dataclasses import replace
from datetime import date
import sqlite3

import pytest

from bizreason.contracts import AnswerType
from bizreason.semantic.evidence import extract_definitions
from bizreason.semantic.metric_catalog import (Catalog, CatalogError, MetricSpec, QuestionContract,
                                               with_derived_dependencies)

TODAY = date(2024, 8, 31)
MONTH = (date(2024, 8, 1), date(2024, 9, 1))
DOC = "- **Staff reward**: Sum revenue multiplied by each employee's rate.\n"


def db():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE orders (order_id TEXT,date TEXT,status TEXT,revenue REAL, "
                       "employee TEXT,xn_rate REAL,xd_paid TEXT)")
    connection.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", [
        ("a", "2024-08-01", "completed", 100, "Ada", .1, "2024-09-01"),
        ("b", "2024-08-02", "completed", 300, "Ben", .2, "2024-08-15"),
        ("c", "2024-08-03", "cancelled", 500, "Ada", .5, "2024-08-20"),
    ])
    return connection


def raw(doc=DOC, **changes):
    evidence = extract_definitions(doc)
    definition_id = next(d.definition_id for d in evidence.definitions if d.term == "Staff reward")
    spec = {"metric_id": "reward", "terms": ["Staff reward"], "evidence_ids": [definition_id],
            "source": "orders", "grain": "order", "entity": "staff", "date_axis": "orders.date",
            "population": "valid", "unit": "money", "dependencies": ["revenue", "xn_rate"],
            "plan": {"source": "orders", "scope": "valid", "period": "question", "measure": {
                "agg": "sum", "expr": {"op": "*", "left": "revenue", "right": "xn_rate"}}}}
    spec.update(changes)
    return spec


def contract(**changes):
    result = {"metric_id": "reward", "grain": "order", "entity": "staff", "date_axis": "orders.date",
              "population": "valid", "period": "question", "output": "scalar"}
    result.update(changes)
    return result


def catalog(connection, spec=None, doc=DOC, evidence_docs=None):
    return Catalog.from_reply({"metrics": [spec or raw(doc)]}, {"rulebook": doc}, connection,
                              evidence_docs=evidence_docs)


def test_weighted_metric_uses_row_expression_and_evidence():
    connection = db()
    metrics = catalog(connection)
    assert not metrics.errors
    result = metrics.execute(contract(), connection, TODAY, MONTH, question="Staff reward this month")
    assert (result.type, result.answer) == (AnswerType.NUMBER, 70)
    assert result.evidence[0].evidence[0].validate(DOC)
    assert metrics.select("staff REWARD per month").metric_id == "reward"
    assert metrics.select("unrelated question") is None
    assert '"date_axis": "orders.date"' in metrics.prompt()


def test_compact_proposal_derives_all_consumed_references_including_filters():
    spec = raw()
    spec.pop('dependencies')
    spec['plan']['filters'] = [{'column':'status','op':'eq','value':'completed'}]
    reply = with_derived_dependencies({'metrics':[spec]})
    assert reply['metrics'][0]['dependencies'] == ['orders.revenue','orders.status','orders.xn_rate']
    assert 'dependencies' not in spec
    assert not Catalog.from_reply(reply, {'rulebook':DOC}, db()).errors


def test_derived_dependencies_do_not_replace_an_explicit_contradictory_manifest():
    spec = raw(dependencies=['revenue'])
    reply = with_derived_dependencies({'metrics':[spec]})
    assert reply['metrics'][0]['dependencies'] == ['revenue']
    assert Catalog.from_reply(reply, {'rulebook':DOC}, db()).errors


def test_dependency_derivation_cannot_make_an_unknown_column_valid():
    spec = raw()
    spec.pop('dependencies')
    spec['plan']['measure'] = {'agg':'sum','expr':'unknown_column'}
    reply = with_derived_dependencies({'metrics':[spec]})
    assert Catalog.from_reply(reply, {'rulebook':DOC}, db()).errors


@pytest.mark.parametrize("measure", [
    {"agg": "sum", "expr": "revenue"},
    {"op": "*", "left": {"agg": "sum", "expr": "revenue"},
     "right": {"agg": "avg", "expr": "xn_rate"}},
])
def test_question_cannot_substitute_another_valid_formula(measure):
    connection = db()
    with pytest.raises(CatalogError, match="unknown"):
        catalog(connection).execute(contract(measure=measure), connection, TODAY, MONTH)


def test_dependency_intervention_changes_reward_without_changing_revenue():
    connection = db()
    metrics = catalog(connection)
    before = metrics.execute(contract(), connection, TODAY, MONTH).answer
    connection.execute("UPDATE orders SET xn_rate=xn_rate*2")
    after = metrics.execute(contract(), connection, TODAY, MONTH).answer
    assert (before, after) == (70, 140)
    assert connection.execute("SELECT SUM(revenue) FROM orders WHERE status='completed'").fetchone()[0] == 400


@pytest.mark.parametrize("changes", [
    {"dependencies": ["revenue"]}, {"dependencies": ["revenue", "xn_rate", "employee"]},
    {"sql": "SELECT 1"}, {"unit": "arbitrary"}, {"source": "lines"},
    {"date_axis": "orders.revenue"}, {"evidence_ids": ["invented"]},
    {"terms": ["sales"]},
])
def test_invalid_registration_returns_structured_issues(changes):
    connection = db()
    metrics = catalog(connection, raw(**changes))
    assert metrics.metrics == {}
    assert metrics.errors[0].code == "invalid_metric"


def test_unknown_column_rejected_without_executing_metric():
    connection = db()
    spec = raw(dependencies=["revenue", "missing"])
    spec["plan"]["measure"]["expr"]["right"] = "missing"
    metrics = catalog(connection, spec)
    assert not metrics.metrics
    assert "column" in metrics.errors[0].message


def test_conflicting_documents_do_not_silently_select_one_formula():
    connection = db()
    alternate = "- **Staff reward**: Sum revenue only.\n"
    # Conflicts across separately supplied documents also need checking.
    metrics = Catalog.from_reply({"metrics": [raw()]}, {"rulebook": DOC, "readme": alternate}, connection)
    assert not metrics.metrics


def test_same_document_conflict_rejects_metric():
    connection = db()
    text = DOC + "- **Staff reward**: Sum revenue only.\n"
    assert not catalog(connection, raw(text), text).metrics


def test_forged_span_or_definition_is_rejected():
    connection = db()
    evidence = extract_definitions(DOC)
    definition = evidence.definitions[0]
    forged = replace(definition.evidence[0], quote="invented")
    with pytest.raises(CatalogError, match="span"):
        catalog(connection, evidence_docs=[replace(evidence, definitions=(
            replace(definition, evidence=(forged,)),))])
    with pytest.raises(CatalogError, match="definition"):
        catalog(connection, evidence_docs=[replace(evidence, definitions=(
            replace(definition, body="Sum revenue only"),))])


@pytest.mark.parametrize("changes", [
    {"population": "all"}, {"date_axis": "orders.xd_paid"}, {"grain": "line"}, {"entity": "vehicle"},
    {"metric_id": "unknown"},
])
def test_contract_cannot_change_definition_semantics(changes):
    connection = db()
    with pytest.raises(CatalogError):
        catalog(connection).execute(contract(**changes), connection, TODAY, MONTH)


def test_date_axis_applies_period_to_paid_date_instead_of_order_date():
    connection = db()
    metrics = catalog(connection, raw(date_axis="orders.xd_paid"))
    assert metrics.execute(contract(date_axis="orders.xd_paid"), connection, TODAY, MONTH).answer == 60


def test_question_period_and_named_values_cannot_be_ignored():
    connection = db()
    metrics = catalog(connection)
    with pytest.raises(CatalogError, match="period"):
        metrics.execute(contract(period="none"), connection, TODAY, MONTH)
    with pytest.raises(CatalogError, match="ignores"):
        metrics.execute(contract(), connection, TODAY, MONTH, named=["Ada"])
    assert metrics.execute(contract(filters=[{"column": "employee", "op": "eq", "value": "Ada"}]),
                           connection, TODAY, MONTH, named=["Ada"]).answer == 10


def test_unused_filter_values_cannot_spoof_question_grounding():
    connection = db()
    with pytest.raises(CatalogError, match="unused"):
        catalog(connection).execute(contract(filters=[{
            "column": "date", "op": "not_empty", "value": "Ada"}]), connection, TODAY, MONTH, named=["Ada"])


def test_zero_survives_and_null_results_are_rejected():
    connection = db()
    metrics = catalog(connection)
    connection.execute("UPDATE orders SET xn_rate=0")
    assert metrics.execute(contract(), connection, TODAY, MONTH).answer == 0
    connection.execute("UPDATE orders SET xn_rate=NULL")
    with pytest.raises(Exception, match="empty"):
        metrics.execute(contract(), connection, TODAY, MONTH)


def test_explicit_money_rounds_without_turning_arbitrary_numbers_into_money():
    connection = db()
    connection.execute("UPDATE orders SET revenue=100.5,xn_rate=1 WHERE order_id='a'")
    money = catalog(connection)
    q = contract(filters=[{"column": "order_id", "op": "eq", "value": "a"}])
    assert money.execute(q, connection, TODAY, MONTH).answer == 101
    number = catalog(connection, raw(unit="number"))
    assert number.execute(q, connection, TODAY, MONTH).answer == 100.5


def test_duplicate_metric_ids_and_ambiguous_terms_are_not_first_wins():
    connection = db()
    metrics = Catalog.from_reply({"metrics": [raw(), raw()]}, {"rulebook": DOC}, connection)
    assert not metrics.metrics
    another = raw(metric_id="other")
    metrics = Catalog.from_reply({"metrics": [raw(), another]}, {"rulebook": DOC}, connection)
    with pytest.raises(CatalogError, match="ambiguous"):
        metrics.select("Staff reward")


def test_registered_formula_cannot_be_mutated_via_returned_plan():
    spec = MetricSpec.from_json(raw())
    original = spec.plan_json
    plan = spec.plan
    plan["measure"] = {"agg": "sum", "expr": "revenue"}
    assert spec.plan_json == original


def test_metric_ids_are_case_sensitive_ascii_names_not_sql_identifiers():
    connection = db()
    metrics = catalog(connection, raw(metric_id="Reward_V2"))
    assert not metrics.errors
    assert metrics.execute(contract(metric_id="Reward_V2"), connection, TODAY, MONTH).answer == 70


def test_scalar_null_order_has_no_semantic_effect_but_rankings_require_order():
    connection = db()
    assert catalog(connection).execute(contract(order=None), connection, TODAY, MONTH).answer == 70
    with pytest.raises(CatalogError, match="order"):
        QuestionContract.from_json(contract(output="ranking", group_by="employee", order=None))


def test_unused_having_operand_cannot_pretend_to_constrain_a_metric():
    spec = raw()
    spec["plan"]["per"] = {"by": "employee", "agg": "count", "having": {
        "op": "gt", "value": 0, "filters": [{"column": "employee", "op": "eq", "value": "Ada"}]}}
    spec["dependencies"].append("employee")
    with pytest.raises(CatalogError, match="unused"):
        MetricSpec.from_json(spec)


def test_grouped_count_unit_cannot_round_fractional_money_into_counts():
    connection = db()
    spec = raw(unit="count")
    connection.execute("UPDATE orders SET xn_rate=0.101 WHERE order_id='a'")
    with pytest.raises(Exception, match="fractional count"):
        catalog(connection, spec).execute(contract(output="table", group_by="employee"),
                                          connection, TODAY, MONTH)


def test_contract_grouping_is_a_column_reference_not_an_arbitrary_object():
    with pytest.raises(CatalogError, match="group_by"):
        QuestionContract.from_json(contract(group_by={"month": "date"}))


def test_contract_cannot_invent_numeric_filter_to_change_the_population():
    connection = db()
    with pytest.raises(CatalogError, match="numeric bound"):
        catalog(connection).execute(contract(filters=[{"column": "revenue", "op": "gt", "value": 99}]),
                                    connection, TODAY, MONTH, question="Staff reward this month")


def test_contract_parsing_is_strict_and_requires_all_semantic_slots():
    value = contract()
    del value["entity"]
    with pytest.raises(CatalogError, match="missing"):
        QuestionContract.from_json(value)
    for key, value in [("limit", True), ("output", "share"), ("order", "random")]:
        with pytest.raises(CatalogError):
            QuestionContract.from_json(contract(**{key: value}))
