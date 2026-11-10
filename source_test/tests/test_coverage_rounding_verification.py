from __future__ import annotations

import json

import pytest

from bizreason.contracts import (
    AnswerType,
    Candidate,
    ColumnMeta,
    ExecutionResult,
    ExecutionStatus,
    OutputSpec,
    Prediction,
    Program,
    Question,
    TableMeta,
    TimeWindow,
    UnsupportedClaim,
)
from bizreason.decision import (
    NON_COVERAGE_ROLES,
    DecisionGate,
    UnsupportedEvidence,
    assess_metric_coverage,
    verify_unsupported,
)
from bizreason.formatting import SubmissionWriter
from bizreason.semantic_checks import semantic_plan_errors
from bizreason.verification import verify_candidate


def test_contracts_rounding_literals_and_prediction_defaults():
    spec = OutputSpec(type=AnswerType.NUMBER, rounding="integer")
    assert spec.rounding == "integer"
    spec_money = OutputSpec(type=AnswerType.NUMBER, rounding="money")
    assert spec_money.rounding == "money"
    spec_two = OutputSpec(type=AnswerType.NUMBER, rounding="two_decimals")
    assert spec_two.rounding == "two_decimals"
    spec_none = OutputSpec(type=AnswerType.NUMBER, rounding="none")
    assert spec_none.rounding == "none"

    prediction = Prediction(id="q1", type=AnswerType.NUMBER, answer=100)
    assert prediction.rounding == "none"
    prediction_custom = Prediction(
        id="q2", type=AnswerType.NUMBER, answer=100, rounding="money"
    )
    assert prediction_custom.rounding == "money"


def test_generic_sql_outside_coverage_detection(business, rules, plan_factory):
    lines = next(t for t in business.tables if t.grain == "order_line")
    plan = plan_factory(
        metric="generic_sql",
        tables=[lines.name],
        sql=f'SELECT SUM(line_amount) FROM "{lines.name}"',
        windows=[
            TimeWindow(
                role="target",
                mode="period",
                start="2035-01-01",
                end_exclusive="2035-02-01",
                field=f"{lines.name}.ordered_at",
            )
        ],
    )
    evidence = assess_metric_coverage(plan, business, rules.text)
    assert evidence.verified
    assert "outside released coverage" in evidence.reason


def test_generic_sql_future_date_and_cumulative_never_flagged_outside_coverage(
    business, rules, plan_factory
):
    lines = next(t for t in business.tables if t.grain == "order_line")

    # Cumulative window should NEVER trigger outside_coverage
    plan_cumulative = plan_factory(
        metric="generic_sql",
        tables=[lines.name],
        sql=f'SELECT SUM(line_amount) FROM "{lines.name}"',
        windows=[
            TimeWindow(
                role="target",
                mode="cumulative",
                start="2026-01-01",
                end_exclusive="2035-02-01",
                field=f"{lines.name}.ordered_at",
            )
        ],
    )
    evidence_cumulative = assess_metric_coverage(plan_cumulative, business, rules.text)
    assert not evidence_cumulative.verified

    # Query referencing due_at column must NOT trigger outside_coverage
    plan_future_sql = plan_factory(
        metric="generic_sql",
        tables=[lines.name],
        sql=f'SELECT due_at FROM "{lines.name}"',
        windows=[
            TimeWindow(
                role="target",
                mode="period",
                start="2035-01-01",
                end_exclusive="2035-02-01",
                field=f"{lines.name}.ordered_at",
            )
        ],
    )
    evidence_future_sql = assess_metric_coverage(plan_future_sql, business, rules.text)
    assert not evidence_future_sql.verified

    # Non-coverage role in claim must NEVER be verified as outside_coverage
    for role in NON_COVERAGE_ROLES:
        claim = UnsupportedClaim(
            kind="outside_coverage",
            required=[role],
            explanation="future date",
            source_refs=["catalog"],
        )
        evidence_role = verify_unsupported(
            plan_cumulative.model_copy(update={"unsupported": claim}), business
        )
        assert not evidence_role.verified


def test_verify_unsupported_paid_at_historical_coverage(business, plan_factory):
    payments = next(t for t in business.tables if t.grain == "payment_event")
    claim = UnsupportedClaim(
        kind="outside_coverage",
        required=["paid_at"],
        explanation="paid_at outside coverage",
        source_refs=["catalog:paid_at"],
    )
    plan_out = plan_factory(
        metric="generic_sql",
        tables=[payments.name],
        unsupported=claim,
        windows=[
            TimeWindow(
                role="target",
                mode="period",
                start="2035-01-01",
                end_exclusive="2035-02-01",
                field=f"{payments.name}.paid_at",
            )
        ],
    )
    evidence = verify_unsupported(plan_out, business)
    assert evidence.verified
    assert "Requested paid_at period is outside released coverage" in evidence.reason


def test_rounding_policy_money_and_non_money_averages(business, rules, plan_factory):
    lines = next(t for t in business.tables if t.grain == "order_line")

    # Money average: Decimal 125430.6666667 with required_fields=["lines.line_amount"]
    plan_money = plan_factory(
        metric="generic_sql",
        tables=[lines.name],
        required_fields=[f"{lines.name}.line_amount"],
        sql=f'SELECT AVG(line_amount) FROM "{lines.name}"',
        output=OutputSpec(type=AnswerType.NUMBER, rounding="none"),
    )
    program_money = Program(
        sql=plan_money.sql,
        parameters=[],
        backend="generated_sql",
        policy_refs=[],
    )
    exec_money = ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        accessed_tables=[lines.name],
        columns=["avg_line"],
        rows=[[125430.6666667]],
        row_count=1,
    )
    candidate_money = Candidate(
        id="c1",
        plan_hash="h1",
        code_hash="h2",
        source_revision="rev1",
        program=program_money,
        execution=exec_money,
        output=plan_money.output,
    )
    verified_money = verify_candidate(candidate_money, plan_money)
    assert verified_money.verified
    assert verified_money.value == 125431
    assert type(verified_money.value) is int
    assert verified_money.output.rounding == "money"

    # Non-money average: Decimal 12.3456 with required_fields=["lines.qty_sale"]
    plan_qty = plan_factory(
        metric="generic_sql",
        tables=[lines.name],
        required_fields=[f"{lines.name}.qty_sale"],
        sql=f'SELECT AVG(qty_sale) FROM "{lines.name}"',
        output=OutputSpec(type=AnswerType.NUMBER, rounding="none"),
    )
    program_qty = Program(
        sql=plan_qty.sql,
        parameters=[],
        backend="generated_sql",
        policy_refs=[],
    )
    exec_qty = ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        accessed_tables=[lines.name],
        columns=["avg_qty"],
        rows=[[12.3456]],
        row_count=1,
    )
    candidate_qty = Candidate(
        id="c2",
        plan_hash="h1",
        code_hash="h2",
        source_revision="rev1",
        program=program_qty,
        execution=exec_qty,
        output=plan_qty.output,
    )
    verified_qty = verify_candidate(candidate_qty, plan_qty)
    assert verified_qty.verified
    assert verified_qty.value == 12.35
    assert verified_qty.output.rounding == "two_decimals"


def test_count_integer_rounding_and_fractional_rejection(business, plan_factory):
    lines = next(t for t in business.tables if t.grain == "order_line")
    plan_count = plan_factory(
        metric="generic_sql",
        tables=[lines.name],
        sql=f'SELECT COUNT(*) FROM "{lines.name}"',
        output=OutputSpec(type=AnswerType.NUMBER, rounding="none"),
    )
    program = Program(
        sql=plan_count.sql,
        parameters=[],
        backend="generated_sql",
        policy_refs=[],
    )

    # Valid integer count
    exec_valid = ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        accessed_tables=[lines.name],
        columns=["cnt"],
        rows=[[5]],
        row_count=1,
    )
    candidate_valid = Candidate(
        id="c_valid",
        plan_hash="h1",
        code_hash="h2",
        source_revision="rev1",
        program=program,
        execution=exec_valid,
        output=plan_count.output,
    )
    verified_valid = verify_candidate(candidate_valid, plan_count)
    assert verified_valid.verified
    assert verified_valid.value == 5
    assert verified_valid.output.rounding == "integer"

    # Fractional count must be rejected
    exec_fractional = ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        accessed_tables=[lines.name],
        columns=["cnt"],
        rows=[[5.5]],
        row_count=1,
    )
    candidate_fractional = Candidate(
        id="c_frac",
        plan_hash="h1",
        code_hash="h2",
        source_revision="rev1",
        program=program,
        execution=exec_fractional,
        output=plan_count.output,
    )
    verified_frac = verify_candidate(candidate_fractional, plan_count)
    assert not verified_frac.verified
    assert any("fractional" in c for c in verified_frac.checks)


def test_null_scalar_and_zero_denominator_rejection(business, plan_factory):
    lines = next(t for t in business.tables if t.grain == "order_line")
    plan = plan_factory(
        metric="generic_sql",
        tables=[lines.name],
        sql=f'SELECT AVG(line_amount) FROM "{lines.name}" WHERE 0',
        output=OutputSpec(type=AnswerType.NUMBER),
    )
    program = Program(
        sql=plan.sql,
        parameters=[],
        backend="generated_sql",
        policy_refs=[],
    )
    exec_null = ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        accessed_tables=[lines.name],
        columns=["avg_line"],
        rows=[[None]],
        row_count=1,
    )
    candidate = Candidate(
        id="c_null",
        plan_hash="h1",
        code_hash="h2",
        source_revision="rev1",
        program=program,
        execution=exec_null,
        output=plan.output,
    )
    verified = verify_candidate(candidate, plan)
    assert not verified.verified
    diagnostic = verified.checks[-1]
    assert "Scalar SQL returned NULL (empty aggregation)" in diagnostic
    assert "COALESCE(SUM(...), 0) ONLY for valid covered periods" in diagnostic

    # Ratio with 0 denominator
    plan_ratio = plan_factory(
        metric="generic_sql",
        tables=[lines.name],
        output=OutputSpec(type=AnswerType.PERCENT, shape="ratio"),
    )
    exec_ratio_zero = ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        accessed_tables=[lines.name],
        columns=["num", "den"],
        rows=[[10, 0]],
        row_count=1,
    )
    cand_ratio = Candidate(
        id="c_ratio",
        plan_hash="h1",
        code_hash="h2",
        source_revision="rev1",
        program=program,
        execution=exec_ratio_zero,
        output=plan_ratio.output,
    )
    verified_ratio = verify_candidate(cand_ratio, plan_ratio)
    assert not verified_ratio.verified
    assert any("nonzero denominator" in c for c in verified_ratio.checks)


def test_submission_writer_preserves_rounding_and_validates_reason(workspace_tmp):
    target = workspace_tmp / "submission_rounding.jsonl"
    with SubmissionWriter(target) as writer:
        writer.write(
            Prediction(
                id="q_money",
                type=AnswerType.NUMBER,
                answer=125430.6666667,
                rounding="money",
            )
        )
        writer.write(
            Prediction(
                id="q_two",
                type=AnswerType.NUMBER,
                answer=12.3456,
                rounding="two_decimals",
            )
        )
        writer.write(
            Prediction(
                id="q_unanswerable",
                type=AnswerType.UNANSWERABLE,
                reason="Data outside coverage",
            )
        )

    lines = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert lines[0] == {"id": "q_money", "type": "number", "answer": 125431}
    assert lines[1] == {"id": "q_two", "type": "number", "answer": 12.35}
    assert lines[2] == {
        "id": "q_unanswerable",
        "type": "unanswerable",
        "reason": "Data outside coverage",
    }

    # Non-string reason rejection
    with pytest.raises(ValueError, match="Reason must be a string"):
        bad_pred = Prediction(id="q_bad", type=AnswerType.ABSTAIN)
        object.__setattr__(bad_pred, "reason", 12345)
        with SubmissionWriter(workspace_tmp / "bad_reason.jsonl") as bad_writer:
            bad_writer.write(bad_pred)


def test_customer_display_name_grouping_hazard(business, plan_factory):
    # Construct context table that has both customer_id and customer_name
    order_tbl = next(t for t in business.tables if t.grain == "order_header")
    updated_cols = list(order_tbl.columns) + [
        ColumnMeta(
            name="customer_name",
            original_name="Tên khách",
            dtype="string",
            semantic_role="customer_name",
        )
    ]
    updated_tbl = TableMeta(
        name=order_tbl.name,
        sources=order_tbl.sources,
        columns=updated_cols,
        grain=order_tbl.grain,
        row_count=order_tbl.row_count,
        raw_row_count=order_tbl.raw_row_count,
        coverage=order_tbl.coverage,
    )
    context = business.model_copy(
        update={"tables": [updated_tbl if t.name == order_tbl.name else t for t in business.tables]}
    )
    question = Question(
        id="q_cust",
        business_id=business.business_id,
        question="Doanh thu theo từng khách hàng",
        as_of_date="2026-03-04",
    )

    # Group by customer_name WITHOUT customer_id
    plan_hazard = plan_factory(
        metric="generic_sql",
        tables=[order_tbl.name],
        group_by=[f"{order_tbl.name}.customer_name"],
        sql=f'SELECT customer_name, SUM(total_due) FROM "{order_tbl.name}" GROUP BY customer_name',
    )
    errors = semantic_plan_errors(plan_hazard, context, question)
    assert any("CUSTOMER_IDENTITY_GROUPING_REQUIRED" in err for err in errors)

    # Group by BOTH customer_id and customer_name -> NO error
    plan_safe = plan_factory(
        metric="generic_sql",
        tables=[order_tbl.name],
        group_by=[f"{order_tbl.name}.customer_id", f"{order_tbl.name}.customer_name"],
        sql=f'SELECT customer_id, customer_name, SUM(total_due) FROM "{order_tbl.name}" GROUP BY customer_id, customer_name',
    )
    errors_safe = semantic_plan_errors(plan_safe, context, question)
    assert not any("CUSTOMER_IDENTITY_GROUPING_REQUIRED" in err for err in errors_safe)


def test_direct_multi_child_fanout_join_hazard(business, plan_factory):
    lines = next(t for t in business.tables if t.grain == "order_line")
    payments = next(t for t in business.tables if t.grain == "payment_event")
    question = Question(
        id="q_fanout",
        business_id=business.business_id,
        question="Tổng tiền dòng hàng và tiền thanh toán",
        as_of_date="2026-03-04",
    )

    # Flat join between order_line and payment_event -> Hazard
    plan_flat = plan_factory(
        metric="generic_sql",
        tables=[lines.name, payments.name],
        sql=f'SELECT SUM(l.line_amount), SUM(p.paid_amount) FROM "{lines.name}" l JOIN "{payments.name}" p ON l.order_id = p.order_id',
    )
    errors = semantic_plan_errors(plan_flat, business, question)
    assert any("MULTI_CHILD_FANOUT_JOIN" in err for err in errors)

    # CTE join -> Safe
    plan_cte = plan_factory(
        metric="generic_sql",
        tables=[lines.name, payments.name],
        sql=(
            f'WITH pay AS (SELECT order_id, SUM(paid_amount) AS total_paid FROM "{payments.name}" GROUP BY order_id) '
            f'SELECT SUM(l.line_amount), SUM(pay.total_paid) FROM "{lines.name}" l JOIN pay ON l.order_id = pay.order_id'
        ),
    )
    errors_cte = semantic_plan_errors(plan_cte, business, question)
    assert not any("MULTI_CHILD_FANOUT_JOIN" in err for err in errors_cte)

    # Subquery join -> Safe
    plan_subquery = plan_factory(
        metric="generic_sql",
        tables=[lines.name, payments.name],
        sql=(
            f'SELECT SUM(l.line_amount), SUM(pay.total_paid) FROM "{lines.name}" l JOIN '
            f'(SELECT order_id, SUM(paid_amount) AS total_paid FROM "{payments.name}" GROUP BY order_id) pay '
            f'ON l.order_id = pay.order_id'
        ),
    )
    errors_subquery = semantic_plan_errors(plan_subquery, business, question)
    assert not any("MULTI_CHILD_FANOUT_JOIN" in err for err in errors_subquery)


def test_decision_gate_accepts_semantic_score(business, rules, plan_factory):
    candidate = Candidate(
        id="fixture",
        plan_hash="h1",
        code_hash="h2",
        source_revision=business.revision,
        program=Program(sql="SELECT 1", parameters=[], backend="template", policy_refs=[]),
        execution=ExecutionResult(
            status=ExecutionStatus.SUCCESS,
            columns=["c"],
            rows=[[1]],
            row_count=1,
            accessed_tables=[business.tables[0].name],
        ),
        output=OutputSpec(type=AnswerType.NUMBER),
        value=1,
        verified=True,
    )
    gate = DecisionGate("verified")
    decision = gate.choose(
        candidate,
        UnsupportedEvidence(False, "unknown"),
        repair_count=0,
        semantic_score=0.92,
    )
    assert decision.action == "value"
