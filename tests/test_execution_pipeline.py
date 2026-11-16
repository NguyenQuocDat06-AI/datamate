from __future__ import annotations

import copy
import json
import time

import pytest

from bizreason.compilation import TemplateCompiler
from bizreason.config import Settings
from bizreason.contracts import Candidate, ExecutionStatus, Question
from bizreason.decision import DecisionGate, UnsupportedEvidence, verify_unsupported
from bizreason.execution import execute_sql
from bizreason.llm import Usage
from bizreason.pipeline import Solver
from bizreason.planning import validate_plan
from bizreason.temporal import TemporalResolver
from bizreason.utils import digest, file_hash
from bizreason.verification import verify_candidate


def execute_plan(plan, business, rules):
    program = TemplateCompiler(rules).compile(plan, business)
    result = execute_sql(
        business.database, program.sql, program.parameters, plan.tables, timeout=10
    )
    candidate = Candidate(
        id="fixture",
        plan_hash=digest(plan),
        code_hash=digest(program),
        source_revision=business.revision,
        program=program,
        execution=result,
        output=plan.output,
    )
    return verify_candidate(candidate, plan)


@pytest.mark.parametrize(
    "metric,kind,shape,expected",
    [
        ("sales_revenue", "number", "scalar", 380000),
        ("cancellation_rate", "percent", "ratio", 20),
        ("top_products", "list", "list", ["B", "A", "C"]),
        ("gross_profit", "number", "scalar", 111000),
        ("debt_total", "number", "scalar", 125000),
    ],
)
def test_metric_templates_against_independent_gold(
    metric, kind, shape, expected, business, rules, plan_factory
):
    plan = plan_factory(metric, output_type=kind, shape=shape)
    question = Question(
        id="fixture",
        business_id=business.business_id,
        question="tháng này",
        as_of_date="2026-03-04",
    )
    assert validate_plan(plan, business, rules, question) == []
    candidate = execute_plan(plan, business, rules)
    assert candidate.verified, candidate.model_dump()
    assert candidate.value == expected


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM {table}",
        "DROP TABLE {table}",
        "PRAGMA user_version=10",
        "ATTACH DATABASE ':memory:' AS external",
        "SELECT load_extension('evil')",
        "SELECT name FROM sqlite_master",
        "SELECT __raw_json FROM {table}",
        "SELECT * FROM {table}; DELETE FROM {table}",
    ],
)
def test_sql_policy_denies_mutation_and_hidden_sources(sql, business):
    table = business.tables[0].name
    before = file_hash(business.database)
    result = execute_sql(
        business.database, sql.format(table='"' + table + '"'), [], [table], timeout=10
    )
    assert result.status in {ExecutionStatus.POLICY_DENIED, ExecutionStatus.EXECUTION_ERROR}
    assert file_hash(business.database) == before


def test_empty_success_is_not_an_error_or_unexecuted(business):
    table = business.tables[0].name
    result = execute_sql(
        business.database, f'SELECT * FROM "{table}" WHERE 0', [], [table], timeout=10
    )
    # SELECT * includes hidden provenance, so explicitly select a public column.
    if result.status == ExecutionStatus.POLICY_DENIED:
        column = business.tables[0].columns[0].name
        result = execute_sql(
            business.database, f'SELECT "{column}" FROM "{table}" WHERE 0', [], [table], timeout=10
        )
    assert result.status == ExecutionStatus.EMPTY_SUCCESS
    assert result.rows == []


def test_result_limit_never_returns_a_truncated_answer(business):
    table = business.tables[0]
    result = execute_sql(
        business.database,
        f'SELECT "{table.columns[0].name}" FROM "{table.name}"',
        [],
        [table.name],
        timeout=10,
        max_rows=1,
    )
    assert result.status == ExecutionStatus.RESULT_LIMIT
    assert result.rows == []


def test_parent_watchdog_bounds_the_entire_process(business):
    table = next(table for table in business.tables if table.grain == "order_line")
    query = "SELECT COUNT(*) FROM " + " CROSS JOIN ".join(f'"{table.name}" t{i}' for i in range(14))
    start = time.monotonic()
    result = execute_sql(business.database, query, [], [table.name], timeout=0.15)
    assert result.status == ExecutionStatus.TIMEOUT
    assert time.monotonic() - start < 3


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.usage = Usage()
        self.prompts = []

    def generate_json(self, system, prompt, *, timeout):
        self.usage.calls += 1
        self.usage.tokens += 10
        self.prompts.append(prompt)
        return copy.deepcopy(self.responses.pop(0))


def test_controller_repairs_latest_candidate_and_never_reads_gold(business, rules, plan_factory):
    table = next(table for table in business.tables if table.grain == "order_line")
    first = plan_factory(
        "generic_sql",
        rule_refs=[rules.records[0].id],
        sql=f'SELECT missing_field FROM "{table.name}"',
    )
    second = first.model_copy(
        update={"sql": f'SELECT COUNT(DISTINCT "{table.role("order_id")}") FROM "{table.name}"'}
    )
    model = FakeModel([first.model_dump(mode="json"), second.model_dump(mode="json")])
    question = Question(
        id="fixture",
        business_id=business.business_id,
        question="bao nhiêu đơn",
        as_of_date="2026-03-04",
    )
    prediction, trace = Solver(model, rules, TemporalResolver(), Settings(sql_timeout=10)).solve(
        question, business
    )
    assert prediction.answer == 5
    assert len(trace.candidates) == 2
    assert trace.candidates[1].parent_id == trace.candidates[0].id
    assert trace.candidates[0].code_hash != trace.candidates[1].code_hash
    assert (
        json.loads(model.prompts[1])["feedback_on_latest_candidate"]["latest_candidate"]["program"][
            "sql"
        ]
        == trace.candidates[0].program.sql
    )
    assert "answers.jsonl" not in "".join(model.prompts)


def test_no_backend_abstains_instead_of_claiming_missing_data(business, rules):
    question = Question(
        id="fixture",
        business_id=business.business_id,
        question="doanh thu",
        as_of_date="2026-03-04",
    )
    prediction, trace = Solver(None, rules, TemporalResolver(), Settings()).solve(
        question, business
    )
    assert prediction.type == "abstain"
    assert trace.decision.p_value is None


def test_signature_preserves_list_order(business, rules, plan_factory):
    first = execute_plan(
        plan_factory("top_products", output_type="list", shape="list"), business, rules
    )
    assert first.value == ["B", "A", "C"]
    assert first.signature != digest(
        ["list", list(reversed(first.value)), first.program.policy_refs, business.revision]
    )


def test_uncalibrated_gate_does_not_invent_a_probability(business, rules, plan_factory):
    candidate = execute_plan(plan_factory(), business, rules)
    decision = DecisionGate().choose(candidate, UnsupportedEvidence(False, "unknown"))
    assert decision.action == "value"
    assert decision.p_value is None


def test_missing_column_name_alone_does_not_prove_unanswerable(business, rules, plan_factory):
    from bizreason.contracts import UnsupportedClaim

    plan = plan_factory(
        "unsupported",
        output_type="unanswerable",
        unsupported=UnsupportedClaim(
            kind="missing_field",
            required=["supplier_id"],
            explanation="not found",
            source_refs=["catalog"],
        ),
    )
    assert not verify_unsupported(plan, business).verified
