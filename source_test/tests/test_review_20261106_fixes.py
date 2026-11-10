from __future__ import annotations

import copy
import json
import struct
import time
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bizreason.compilation import TemplateCompiler
from bizreason.config import Settings
from bizreason.planning import Planner, partition_is_needed, validate_plan
from bizreason.contracts import (
    AnswerType,
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
    UnsupportedClaim,
    ProbeRequest,
)
from bizreason.data.notes_facets import classify_note_status
from bizreason.decision import DecisionGate, UnsupportedEvidence, verify_unsupported, _canonical_required_role
from bizreason.plan_input import canonicalize_plan_input
from bizreason.execution import _SqliteWorkerPool, _sql_iso_date, execute_sql, register_helpers, shutdown_sql_pool
from bizreason.model_ledger import (
    count_safetensors_parameters,
    inspect_safetensors_parameters,
    verify_model_ledger,
)
from bizreason.pipeline import Solver
from bizreason.rules import RuleRegistry
from bizreason.semantic_checks import semantic_plan_errors
from bizreason.temporal import TemporalResolver
from bizreason.triage import (
    DeterministicSlotGuard,
    LLMSemanticCritic,
    parse_fast_path_slots,
)
from bizreason.utils import digest
from bizreason.verification import verify_candidate


# ---------------------------------------------------------------------------
# Helper models and candidate builders
# ---------------------------------------------------------------------------

class OfflineModel:
    """Mock model that returns a fixed plan on first call and critique thereafter."""

    def __init__(self, plan: QueryPlan):
        self.plan = plan
        self.usage = SimpleNamespace(calls=0, tokens=0)

    def generate_json(self, *_args, **_kwargs):
        self.usage.calls += 1
        if self.usage.calls == 1:
            return self.plan.model_dump(mode="json")
        return {"verdict": "policy_violation", "score": 0.0, "reason": "Offline test rejection"}


def _model_entry(identity: str, parameters: int, **extra) -> dict:
    return {
        "model_id": identity,
        "weights_revision": "fixture_rev",
        "weights_sha256": identity * 64,
        "release_date": "2025-01-01",
        "licence": "apache-2.0",
        "total_parameters_including_moe": parameters,
        "quantization": "none",
        "runtime": "vllm",
        "purpose": "test",
        **extra,
    }


def _make_candidate(plan: QueryPlan, rows: list[list], columns: list[str]) -> Candidate:
    program = Program(
        backend="generated_sql",
        sql=plan.sql or "SELECT 1",
        parameters=[],
        policy_refs=plan.rule_refs,
    )
    return Candidate(
        id="test_candidate",
        plan_hash=digest(plan),
        code_hash=digest(program),
        source_revision="test_rev",
        program=program,
        output=plan.output,
        execution=ExecutionResult(
            status=ExecutionStatus.SUCCESS,
            rows=rows,
            columns=columns,
            accessed_tables=plan.tables,
        ),
    )


# ---------------------------------------------------------------------------
# R01: DeterministicSlotGuard & parse_fast_path_slots
# ---------------------------------------------------------------------------

class TestR01FastPathSlotGuard:
    def test_parse_fast_path_slots_positive_and_residuals(self):
        # 1. Clean sales revenue question routes properly
        res = parse_fast_path_slots("Doanh thu thang 3/2026")
        assert res is not None
        assert res["metric"] == "sales_revenue"
        assert res["top_k"] == 1

        # 2. Residual modifier 'online' causes unconsumed tokens -> returns None
        assert parse_fast_path_slots("Doanh thu online thang 3/2026") is None

        # 3. Residual time-of-day modifier 'truoc 10 gio' -> returns None
        assert parse_fast_path_slots("Doanh thu truoc 10 gio hom qua") is None

        # 4. Top 5 products question parses metric and top_k
        res_top = parse_fast_path_slots("Top 5 san pham ban chay thang 3/2026")
        assert res_top is not None
        assert res_top["metric"] == "top_products"
        assert res_top["top_k"] == 5

    def test_deterministic_slot_guard_routing(self, business, rules):
        guard = DeterministicSlotGuard()
        temporal = TemporalResolver()

        # 1. "Doanh thu thang 3/2026" routes to sales_revenue plan
        question_rev = Question(
            id="test_r01_rev",
            business_id=business.business_id,
            question="Doanh thu thang 3/2026",
            as_of_date="2026-03-05",
        )
        plan_rev = guard.route(question_rev, business, rules, temporal)
        assert plan_rev is not None
        assert plan_rev.metric == "sales_revenue"
        assert plan_rev.output.type == AnswerType.NUMBER
        assert plan_rev.output.rounding == "money"

        # 2. "Doanh thu online thang 3/2026" returns None
        question_online = Question(
            id="test_r01_online",
            business_id=business.business_id,
            question="Doanh thu online thang 3/2026",
            as_of_date="2026-03-05",
        )
        assert guard.route(question_online, business, rules, temporal) is None

        # 3. "Doanh thu truoc 10 gio hom qua" returns None
        question_time = Question(
            id="test_r01_time",
            business_id=business.business_id,
            question="Doanh thu truoc 10 gio hom qua",
            as_of_date="2026-03-05",
        )
        assert guard.route(question_time, business, rules, temporal) is None

        # 4. "Top 5 san pham ban chay thang 3/2026" routes to top_products list
        question_top = Question(
            id="test_r01_top",
            business_id=business.business_id,
            question="Top 5 san pham ban chay thang 3/2026",
            as_of_date="2026-03-05",
        )
        plan_top = guard.route(question_top, business, rules, temporal)
        assert plan_top is not None
        assert plan_top.metric == "top_products"
        assert plan_top.output.type == AnswerType.LIST
        assert plan_top.output.shape == "list"
        assert plan_top.output.element_type == "string"
        assert plan_top.limit == 5

        # Compile and verify against business fixture
        program = TemplateCompiler(rules).compile(plan_top, business)
        exec_res = execute_sql(
            business.database,
            program.sql,
            program.parameters,
            plan_top.tables,
            timeout=10,
        )
        cand = Candidate(
            id="test_top_cand",
            plan_hash=digest(plan_top),
            code_hash=digest(program),
            source_revision=business.revision,
            program=program,
            output=plan_top.output,
            execution=exec_res,
        )
        verified_cand = verify_candidate(cand, plan_top)
        assert verified_cand.verified is True
        assert isinstance(verified_cand.value, list)
        assert len(verified_cand.value) > 0
        assert all(isinstance(p, str) for p in verified_cand.value)


# ---------------------------------------------------------------------------
# R02: classify_note_status & derive_status_from_notes
# ---------------------------------------------------------------------------

class TestR02NotesClassification:
    @pytest.mark.parametrize(
        "note_text",
        [
            "khach tra no",
            "da thu no",
            "khach tra tien mat",
            "chuyen khoan thanh toan",
        ],
    )
    def test_payment_notes_classify_as_completed(self, note_text):
        status, is_err = classify_note_status(note_text)
        assert (status, is_err) == ("completed", False)

    @pytest.mark.parametrize(
        "note_text",
        [
            "khach mang tra",
            "tra hang",
            "khach gui tra sp",
        ],
    )
    def test_return_notes_classify_as_returned(self, note_text):
        status, is_err = classify_note_status(note_text)
        assert (status, is_err) == ("returned", False)

    @pytest.mark.parametrize(
        "note_text",
        [
            "khong huy",
            "khach bao khong huy",
            "khong doi tra",
        ],
    )
    def test_negated_notes_classify_as_completed(self, note_text):
        status, is_err = classify_note_status(note_text)
        assert (status, is_err) == ("completed", False)

    @pytest.mark.parametrize(
        "note_text",
        [
            "chua giao hang",
            "dang giao",
        ],
    )
    def test_shipping_notes_classify_as_shipping(self, note_text):
        status, is_err = classify_note_status(note_text)
        assert (status, is_err) == ("shipping", False)

    @pytest.mark.parametrize(
        "note_text",
        [
            "da huy",
            "khach huy don",
        ],
    )
    def test_cancelled_notes_classify_as_cancelled(self, note_text):
        status, is_err = classify_note_status(note_text)
        assert (status, is_err) == ("cancelled", False)

    def test_conflicting_note_flags_diagnostic_error(self):
        status, is_err = classify_note_status("da huy nhung da giao hoan thanh")
        assert (status, is_err) == (None, True)


# ---------------------------------------------------------------------------
# R03: Model ledger order invariance, decomposition, and safetensors
# ---------------------------------------------------------------------------

class TestR03ModelLedgerAndSafetensors:
    def test_order_invariance_base_adapter(self):
        base = _model_entry("base_m", 8_000_000_000)
        adapter = _model_entry(
            "adapter_m",
            8_100_000_000,
            shared_base_sha256=base["weights_sha256"],
            base_parameters=8_000_000_000,
            adapter_parameters=100_000_000,
        )
        # Both base-then-adapter and adapter-then-base must yield [] violations
        assert verify_model_ledger({"offline_models": [base, adapter]}) == []
        assert verify_model_ledger({"offline_models": [adapter, base]}) == []

    def test_inconsistent_parameter_decomposition(self):
        inconsistent = _model_entry(
            "inconsistent_m",
            20_000_000_000,
            shared_base_sha256="d" * 64,
            base_parameters=0,
            adapter_parameters=1,
        )
        violations = verify_model_ledger({"offline_models": [inconsistent]})
        assert any("INCONSISTENT_PARAMETER_DECOMPOSITION" in v for v in violations)
        assert any("EXCEEDS_MAX_PARAMETERS" in v for v in violations)

    def test_base_parameter_mismatch(self):
        base = _model_entry("base_stand", 8_000_000_000, weights_sha256="s" * 64)
        adapter = _model_entry(
            "adapter_other",
            7_100_000_000,
            weights_sha256="ad" * 32,
            shared_base_sha256="s" * 64,
            base_parameters=7_000_000_000,
            adapter_parameters=100_000_000,
        )
        violations = verify_model_ledger({"offline_models": [base, adapter]})
        assert any("BASE_PARAMETER_MISMATCH" in v for v in violations)

    def test_packed_awq_safetensors_parameter_count(self, tmp_path):
        packed_path = tmp_path / "awq_packed.safetensors"
        # Synthetic AWQ packing: layer.qweight [8, 8] I32, layer.qzeros [1, 8] I32, layer.scales [1, 64] F16
        # Logical weight matrix: 8 * 64 = 512 parameters
        # Stored elements: 8*8 + 1*8 + 1*64 = 64 + 8 + 64 = 136 elements
        header = {
            "layer.qweight": {"dtype": "I32", "shape": [8, 8], "data_offsets": [0, 256]},
            "layer.qzeros": {"dtype": "I32", "shape": [1, 8], "data_offsets": [256, 288]},
            "layer.scales": {"dtype": "F16", "shape": [1, 64], "data_offsets": [288, 416]},
        }
        encoded = json.dumps(header).encode("utf-8")
        encoded += b" " * (-len(encoded) % 8)
        packed_path.write_bytes(struct.pack("<Q", len(encoded)) + encoded + b"\0" * 416)

        inspection = inspect_safetensors_parameters(packed_path)
        assert inspection["logical_parameters"] == 512
        assert inspection["stored_tensor_elements"] == 136
        assert count_safetensors_parameters(packed_path) == 512


# ---------------------------------------------------------------------------
# R04: semantic_plan_errors CTE fanout & SQL GROUP BY customer identity
# ---------------------------------------------------------------------------

class TestR04SemanticChecks:
    def test_cte_fanout_detection(self, business, rules):
        lines_t = next(t for t in business.tables if t.grain == "order_line")
        pay_t = next(t for t in business.tables if t.grain == "payment_event")
        question = Question(
            id="test_fanout_q",
            business_id=business.business_id,
            question="Tong tien da thanh toan",
            as_of_date="2026-03-05",
        )

        # 1. Unrelated CTE preceding raw join still flags MULTI_CHILD_FANOUT_JOIN
        raw_join_sql = (
            f'WITH unrelated AS (SELECT 1) '
            f'SELECT SUM(p.paid_amount) '
            f'FROM "{lines_t.name}" l JOIN "{pay_t.name}" p ON l.order_id = p.order_id'
        )
        plan_bad = QueryPlan(
            metric="generic_sql",
            tables=[lines_t.name, pay_t.name],
            rule_refs=rules.required_refs("sales_revenue"),
            sql=raw_join_sql,
            output=OutputSpec(type="number"),
        )
        errors_bad = semantic_plan_errors(plan_bad, business, question)
        assert any("MULTI_CHILD_FANOUT_JOIN" in e for e in errors_bad)

        # 2. Properly pre-aggregated CTE has NO MULTI_CHILD_FANOUT_JOIN
        agg_sql = (
            f'WITH o AS (SELECT order_id, SUM(line_amount) AS due FROM "{lines_t.name}" GROUP BY order_id), '
            f'p AS (SELECT order_id, SUM(paid_amount) AS paid FROM "{pay_t.name}" GROUP BY order_id) '
            f'SELECT SUM(o.due - COALESCE(p.paid, 0)) FROM o LEFT JOIN p ON o.order_id = p.order_id'
        )
        plan_good = QueryPlan(
            metric="generic_sql",
            tables=[lines_t.name, pay_t.name],
            rule_refs=rules.required_refs("sales_revenue"),
            sql=agg_sql,
            output=OutputSpec(type="number"),
        )
        errors_good = semantic_plan_errors(plan_good, business, question)
        assert not any("MULTI_CHILD_FANOUT_JOIN" in e for e in errors_good)

    def test_sql_group_by_customer_identity_without_plan_metadata(self, business, rules):
        orders_t = next(t for t in business.tables if t.grain == "order_header")
        updated_cols = list(orders_t.columns) + [
            ColumnMeta(
                name="customer_name",
                original_name="Ten khach",
                dtype="string",
                semantic_role="customer_name",
            )
        ]
        updated_tbl = TableMeta(
            name=orders_t.name,
            sources=orders_t.sources,
            columns=updated_cols,
            grain=orders_t.grain,
            row_count=orders_t.row_count,
            raw_row_count=orders_t.raw_row_count,
            coverage=orders_t.coverage,
        )
        context = business.model_copy(
            update={"tables": [updated_tbl if t.name == orders_t.name else t for t in business.tables]}
        )
        question = Question(
            id="test_cust_q",
            business_id=business.business_id,
            question="So don theo tung khach hang",
            as_of_date="2026-03-05",
        )
        # SQL groups by customer_name without customer_id; plan.group_by is left empty
        sql = f'SELECT "customer_name", COUNT(*) FROM "{orders_t.name}" GROUP BY "customer_name"'
        plan = QueryPlan(
            metric="generic_sql",
            tables=[orders_t.name],
            rule_refs=rules.required_refs("sales_revenue"),
            sql=sql,
            group_by=[],  # empty in plan metadata
            output=OutputSpec(type="table"),
        )
        errors = semantic_plan_errors(plan, context, question)
        assert any("CUSTOMER_IDENTITY_GROUPING_REQUIRED" in e for e in errors)


# ---------------------------------------------------------------------------
# R05: verify_candidate value-independent money rounding & mixed table columns
# ---------------------------------------------------------------------------

class TestR05VerificationRoundingAndTables:
    def test_value_independent_money_rounding(self, business, rules):
        lines_t = next(t for t in business.tables if t.grain == "order_line")
        plan = QueryPlan(
            metric="generic_sql",
            tables=[lines_t.name],
            rule_refs=rules.required_refs("sales_revenue"),
            required_fields=[f"{lines_t.name}.line_amount"],
            sql=f'SELECT AVG(line_amount) FROM "{lines_t.name}"',
            output=OutputSpec(type="number"),
        )
        # Both 123.45 (2 decimals) and 123.456 (3 decimals) must produce rounding="money" and value=123
        for test_val in [123.45, 123.456]:
            candidate = _make_candidate(plan, [[test_val]], ["average"])
            verified = verify_candidate(candidate, plan)
            assert verified.verified is True
            assert verified.output.rounding == "money"
            assert verified.value == 123

    def test_mixed_table_columns_preserves_int_and_float(self, business, rules):
        lines_t = next(t for t in business.tables if t.grain == "order_line")
        plan = QueryPlan(
            metric="generic_sql",
            tables=[lines_t.name],
            rule_refs=rules.required_refs("sales_revenue"),
            sql=f'SELECT product_name, COUNT(*), SUM(qty_sale) FROM "{lines_t.name}" GROUP BY product_name',
            output=OutputSpec(
                type="table",
                columns=["product", "orders", "kg"],
                column_types=["string", "number", "number"],
            ),
        )
        candidate = _make_candidate(plan, [["A", 1, 2.5]], ["product", "orders", "kg"])
        verified = verify_candidate(candidate, plan)
        assert verified.verified is True
        row = verified.value["rows"][0]
        assert row[0] == "A"
        assert row[1] == 1
        assert isinstance(row[1], int)
        assert float(row[2]) == 2.5

    def test_fractional_count_rejected_in_pure_count_column(self, business, rules):
        lines_t = next(t for t in business.tables if t.grain == "order_line")
        plan = QueryPlan(
            metric="generic_sql",
            tables=[lines_t.name],
            rule_refs=rules.required_refs("sales_revenue"),
            sql=f'SELECT product_name, COUNT(*), SUM(qty_sale) FROM "{lines_t.name}" GROUP BY product_name',
            output=OutputSpec(
                type="table",
                columns=["product", "orders", "kg"],
                column_types=["string", "number", "number"],
            ),
        )
        # 1.5 in pure COUNT(*) column must be rejected
        candidate = _make_candidate(plan, [["A", 1.5, 2.5]], ["product", "orders", "kg"])
        verified = verify_candidate(candidate, plan)
        assert verified.verified is False
        assert any("Count/integer output cannot be fractional" in c for c in verified.checks)


# ---------------------------------------------------------------------------
# R06: DecisionGate critic veto & Solver critic budget cap
# ---------------------------------------------------------------------------

class TestR06DecisionGateAndCriticBudget:
    def test_decision_gate_critic_veto(self, business, rules):
        lines_t = next(t for t in business.tables if t.grain == "order_line")
        plan = QueryPlan(
            metric="generic_sql",
            tables=[lines_t.name],
            rule_refs=rules.required_refs("sales_revenue"),
            sql=f'SELECT SUM(line_amount) FROM "{lines_t.name}"',
            output=OutputSpec(type="number", rounding="money"),
        )
        candidate = verify_candidate(_make_candidate(plan, [[1000]], ["total"]), plan)
        evidence = UnsupportedEvidence(False, "test_evidence")

        # semantic_score = 0.0 vetoes candidate -> action="abstain"
        dec_veto = DecisionGate().choose(candidate, evidence, semantic_score=0.0)
        assert dec_veto.action == "abstain"

        # semantic_score = 1.0 accepts candidate -> action="value"
        dec_acc = DecisionGate().choose(candidate, evidence, semantic_score=1.0)
        assert dec_acc.action == "value"

    def test_solver_critic_budget_cap(self, business, rules):
        lines_t = next(t for t in business.tables if t.grain == "order_line")
        temporal = TemporalResolver()
        plan = QueryPlan(
            metric="generic_sql",
            tables=[lines_t.name],
            rule_refs=rules.required_refs("sales_revenue"),
            sql=f'SELECT SUM(line_amount) FROM "{lines_t.name}"',
            output=OutputSpec(type="number", rounding="money"),
        )
        model = OfflineModel(plan)
        question = Question(
            id="test_budget_q",
            business_id=business.business_id,
            question="Tong tien",
            as_of_date="2026-03-05",
        )
        solver = Solver(
            model=model,
            rules=rules,
            temporal=temporal,
            settings=Settings(max_model_calls=1, max_probe_calls=0),
            semantic_critic=LLMSemanticCritic(model),
        )
        prediction, trace = solver.solve(question, business)

        # Max model calls is 1: model usage is 1, trace model calls is 1, and critic is skipped
        assert model.usage.calls == 1
        assert trace.model_calls == 1
        assert any("CRITIC_SKIPPED_BUDGET_EXHAUSTED" in e for e in trace.errors)


# ---------------------------------------------------------------------------
# R07 & R10: _SqliteWorkerPool hard process cap
# ---------------------------------------------------------------------------

class TestR07WorkerPoolHardCap:
    def test_pool_leasing_respects_hard_max_workers_cap(self):
        pool = _SqliteWorkerPool(max_workers=1)
        leased = []
        try:
            # Lease twice simultaneously with pool limit 1
            leased.append(pool.lease_worker(timeout=0.1))
            leased.append(pool.lease_worker(timeout=0.1))
            # At most 1 worker process can be alive
            live_count = sum(w.is_alive() for w in leased)
            assert live_count == 1
        finally:
            for w in leased:
                pool.discard_worker(w)
            pool.shutdown()
        shutdown_sql_pool()


# ---------------------------------------------------------------------------
# R08: Fix 1 - Placed-Order Cohort in cancellation_rate
# ---------------------------------------------------------------------------

class TestR08PlacedOrderCohortCancellationRate:
    def test_cancellation_rate_excludes_negative_quantity_return_lines(self, business, rules, plan_factory):
        import sqlite3
        # 1. Compile cancellation_rate on order_line grain
        lines_t = next(t for t in business.tables if t.grain == "order_line")
        plan = plan_factory(
            metric="cancellation_rate",
            tables=[lines_t.name],
            windows=[TimeWindow(start=date(2026, 3, 1), end_exclusive=date(2026, 3, 31))],
            output_type=AnswerType.PERCENT,
        )
        program = TemplateCompiler(rules).compile(plan, business)
        assert program.backend == "template"
        # Verify CAST(qty_col AS REAL) > 0 is present in SQL
        qty_col = lines_t.role("qty_sale")
        assert f'CAST("{qty_col}" AS REAL) > 0' in program.sql

        # 2. Insert a prior-period return line with negative qty directly into SQLite database
        conn = sqlite3.connect(business.database)
        try:
            conn.execute(
                f'INSERT INTO "{lines_t.name}" ("order_id", "ordered_at", "status", "product_id", "product_name", "{qty_col}", "qty_base", "line_amount", "cost_sale") '
                f'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                ["RET_PRIOR", "2026-03-02", "returned", "P1", "A", -5, -5, -50000, 7000],
            )
            conn.commit()
        finally:
            conn.close()

        res = execute_sql(
            business.database,
            program.sql,
            program.parameters,
            plan.tables,
            timeout=10,
        )
        assert res.status == ExecutionStatus.SUCCESS
        cancelled_orders, placed_orders = res.rows[0]
        # RET_PRIOR with qty=-5 must be excluded from placed_orders cohort
        raw_distinct = execute_sql(
            business.database,
            f'SELECT COUNT(DISTINCT order_id) FROM "{lines_t.name}" WHERE ordered_at >= ? AND ordered_at < ?',
            ["2026-03-01", "2026-03-31"],
            [lines_t.name],
        )
        assert raw_distinct.rows[0][0] == placed_orders + 1
        shutdown_sql_pool()

    def test_semantic_plan_errors_flags_unfiltered_return_lines(self, business, rules, plan_factory):
        lines_t = next(t for t in business.tables if t.grain == "order_line")
        qty_col = lines_t.role("qty_sale")
        # Context where README mentions negative quantities
        readme_context = business.model_copy(
            update={"readme": "Cửa hàng ghi nhận hàng trả lại bằng số lượng âm trên sổ bán hàng."}
        )
        question = Question(
            id="test_cancel_q",
            business_id=business.business_id,
            question="Tỉ lệ đơn huỷ trong tháng 3/2026 là bao nhiêu?",
            as_of_date=date(2026, 3, 5),
        )
        # SQL with COUNT(DISTINCT order_id) but without qty_col filter
        bad_plan = plan_factory(
            metric="generic_sql",
            tables=[lines_t.name],
            sql=f'SELECT COUNT(DISTINCT CASE WHEN status=\'cancelled\' THEN order_id END) * 100.0 / COUNT(DISTINCT order_id) FROM "{lines_t.name}"',
            template_eligible=False,
            output_type=AnswerType.PERCENT,
        )
        errs = semantic_plan_errors(bad_plan, readme_context, question)
        assert any("RETURN_LINE_IN_ORDER_COHORT" in e for e in errs)

        # SQL with qty_col filter
        good_plan = plan_factory(
            metric="generic_sql",
            tables=[lines_t.name],
            sql=f'SELECT COUNT(DISTINCT CASE WHEN status=\'cancelled\' THEN order_id END) * 100.0 / COUNT(DISTINCT order_id) FROM "{lines_t.name}" WHERE CAST({qty_col} AS REAL) > 0',
            template_eligible=False,
            output_type=AnswerType.PERCENT,
        )
        good_errs = semantic_plan_errors(good_plan, readme_context, question)
        assert not any("RETURN_LINE_IN_ORDER_COHORT" in e for e in good_errs)


# ---------------------------------------------------------------------------
# R09: Fix 2 - Canonical Role Synonyms & Auxiliary Headers in verify_unsupported
# ---------------------------------------------------------------------------

class TestR09CanonicalRolesAndAuxiliaryHeadersUnsupported:
    def test_verify_unsupported_canonical_synonyms_and_harmless_headers(self, business, plan_factory):
        source = next(iter(business.manifest))
        orders_t = next(t for t in business.tables if t.grain == "order_header")
        # Add auxiliary headers to orders_t that are in harmless
        aux_columns = [
            ColumnMeta(name="c_ghi_chu", original_name="Ghi chú", dtype="string"),
            ColumnMeta(name="c_stt", original_name="Số thứ tự", dtype="number"),
            ColumnMeta(name="c_bao_hanh", original_name="Bảo hành", dtype="string"),
            ColumnMeta(name="c_nhom_hang", original_name="Nhóm hàng hoá", dtype="string"),
        ]
        updated_orders = orders_t.model_copy(
            update={"columns": list(orders_t.columns) + aux_columns}
        )
        context = business.model_copy(
            update={
                "tables": [updated_orders if t.name == orders_t.name else t for t in business.tables],
                "schema_complete": True,
            }
        )

        # 1. required=["expiry_date"] maps to expires_at -> verified True
        plan_exp = plan_factory(
            "unsupported",
            output_type="unanswerable",
            unsupported=UnsupportedClaim(
                kind="missing_field",
                required=["expiry_date"],
                explanation="Expiry date is not recorded in sales data",
                source_refs=[source],
            ),
        )
        evidence_exp = verify_unsupported(plan_exp, context)
        assert evidence_exp.verified is True

        # 2. required=["expiry_date", "inventory_balance"] maps to expires_at and inventory_qty -> verified True
        plan_both = plan_factory(
            "unsupported",
            output_type="unanswerable",
            unsupported=UnsupportedClaim(
                kind="missing_field",
                required=["expiry_date", "inventory_balance"],
                explanation="Expiry date and inventory balance are not recorded in sales data",
                source_refs=[source],
            ),
        )
        evidence_both = verify_unsupported(plan_both, context)
        assert evidence_both.verified is True

        # 3. An unmapped column NOT in harmless prevents unsupported verification
        unmapped_non_harmless = ColumnMeta(name="c_unknown", original_name="Mã vận đơn mới", dtype="string")
        context_incomplete = context.model_copy(
            update={
                "tables": [
                    updated_orders.model_copy(update={"columns": list(updated_orders.columns) + [unmapped_non_harmless]})
                    if t.name == updated_orders.name else t
                    for t in context.tables
                ]
            }
        )
        assert not verify_unsupported(plan_exp, context_incomplete).verified

        # 4. expired_at synonym and noi dung in harmless
        plan_expired = plan_factory(
            "unsupported",
            output_type="unanswerable",
            unsupported=UnsupportedClaim(
                kind="missing_field",
                required=["expired_at"],
                explanation="Expired at is missing",
                source_refs=[source],
            ),
        )
        noi_dung_col = ColumnMeta(name="c_noidung", original_name="Nội dung", dtype="string")
        context_noi_dung = context.model_copy(
            update={
                "tables": [
                    updated_orders.model_copy(update={"columns": list(updated_orders.columns) + [noi_dung_col]})
                    if t.name == updated_orders.name else t
                    for t in context.tables
                ]
            }
        )
        assert verify_unsupported(plan_expired, context_noi_dung).verified

        # 5. README documented column descriptions
        doc_col = ColumnMeta(name="c_custom_desc", original_name="Thong tin phu", dtype="string")
        context_readme_doc = context.model_copy(
            update={
                "tables": [
                    updated_orders.model_copy(update={"columns": list(updated_orders.columns) + [doc_col]})
                    if t.name == updated_orders.name else t
                    for t in context.tables
                ],
                "readme": "- **Thong tin phu**: Chi tiet them ve van chuyen va dong goi",
            }
        )
        assert verify_unsupported(plan_exp, context_readme_doc).verified

        # If README documentation of the unmapped column contains a missing alias, it cannot prove absence
        context_readme_alias_conflict = context.model_copy(
            update={
                "tables": [
                    updated_orders.model_copy(update={"columns": list(updated_orders.columns) + [doc_col]})
                    if t.name == updated_orders.name else t
                    for t in context.tables
                ],
                "readme": "- **Thong tin phu**: Ghi chu han dung cua san pham",
            }
        )
        assert not verify_unsupported(plan_exp, context_readme_alias_conflict).verified


# ---------------------------------------------------------------------------
# R10: Fix 3 & Fast Path - DeterministicSlotGuard on Partitioned Tables
# ---------------------------------------------------------------------------

class TestR10PartitionedMonthlyFastPathSlotGuard:
    def test_fast_path_parses_cancellation_polite_and_synonyms(self):
        # 1. Question with polite particle and % đơn bị huỷ
        res1 = parse_fast_path_slots("Tháng 2/2026 cửa hàng có bao nhiêu % đơn bị huỷ ạ?")
        assert res1 is not None
        assert res1["metric"] == "cancellation_rate"
        assert res1["top_k"] == 1

        # 2. Question with space/dash separated month
        res2 = parse_fast_path_slots("Tỉ lệ huỷ đơn tháng 02-2026 là bao nhiêu %?")
        assert res2 is not None
        assert res2["metric"] == "cancellation_rate"
        assert res2["top_k"] == 1

    def test_partitioned_monthly_tables_single_needed_partition_routes(self, business, rules):
        import sqlite3
        guard = DeterministicSlotGuard()
        temporal = TemporalResolver()
        lines_t = next(t for t in business.tables if t.grain == "order_line")

        # Create backing views in database
        conn = sqlite3.connect(business.database)
        try:
            conn.execute(f'CREATE VIEW "sobanhang_thang_01_2026" AS SELECT * FROM "{lines_t.name}" WHERE 0')
            conn.execute(f'CREATE VIEW "sobanhang_thang_02_2026" AS SELECT * FROM "{lines_t.name}"')
            conn.commit()
        finally:
            conn.close()

        try:
            # Create two monthly partitions with distinct coverage
            part1 = lines_t.model_copy(
                update={
                    "name": "sobanhang_thang_01_2026",
                    "coverage": {lines_t.role("ordered_at"): ["2026-01-01", "2026-01-31"]},
                }
            )
            part2 = lines_t.model_copy(
                update={
                    "name": "sobanhang_thang_02_2026",
                    "coverage": {lines_t.role("ordered_at"): ["2026-02-01", "2026-02-28"]},
                }
            )
            # Context without order_header so it falls back to order_line grain partitions
            non_header_tables = [t for t in business.tables if t.grain != "order_header" and t.grain != "order_line"]
            context = business.model_copy(
                update={"tables": [*non_header_tables, part1, part2]}
            )

            question = Question(
                id="test_part_q",
                business_id=business.business_id,
                question="Tỉ lệ huỷ đơn tháng 02-2026 là bao nhiêu %?",
                as_of_date=date(2026, 3, 1),
            )
            plan = guard.route(question, context, rules, temporal)
            assert plan is not None
            assert plan.metric == "cancellation_rate"
            assert plan.tables == ["sobanhang_thang_02_2026"]
        finally:
            drop_conn = sqlite3.connect(business.database)
            try:
                drop_conn.execute('DROP VIEW IF EXISTS "sobanhang_thang_01_2026"')
                drop_conn.execute('DROP VIEW IF EXISTS "sobanhang_thang_02_2026"')
                drop_conn.commit()
            finally:
                drop_conn.close()
            shutdown_sql_pool()


# ---------------------------------------------------------------------------
# Forensic Audit 100-Question Generalized Fixes (IMP-FORENSIC-100)
# ---------------------------------------------------------------------------

class TestForensicAuditFixes:
    def test_verify_unsupported_customer_address(self, business):
        # 1. Context initially has no address column, and has unmapped populated auxiliary POS columns
        # like "Mã barcode" and "Tồn lớn nhất" -> verify_unsupported returns True
        tbl0 = business.tables[0]
        col_barcode = ColumnMeta(
            name="ma_barcode",
            original_name="Mã barcode",
            dtype="TEXT",
            semantic_role=None,
            null_count=0,
        )
        col_max_stock = ColumnMeta(
            name="ton_lon_nhat",
            original_name="Tồn lớn nhất",
            dtype="REAL",
            semantic_role=None,
            null_count=0,
        )
        tbl0_with_aux = tbl0.model_copy(
            update={"columns": [*tbl0.columns, col_barcode, col_max_stock]}
        )
        business_with_aux = business.model_copy(
            update={"tables": [tbl0_with_aux, *business.tables[1:]]}
        )

        claim = UnsupportedClaim(
            kind="missing_field",
            required=["customer_address"],
            explanation="Missing customer address",
            source_refs=["README.md"],
        )
        plan = QueryPlan(
            metric="unsupported",
            tables=[business_with_aux.tables[0].name],
            rule_refs=[],
            output=OutputSpec(type=AnswerType.UNANSWERABLE),
            unsupported=claim,
        )
        evidence = verify_unsupported(plan, business_with_aux)
        assert evidence.verified is True

        # 2. Add an unmapped column named "Địa chỉ" -> present -> verified False
        col_addr = ColumnMeta(
            name="dia_chi",
            original_name="Địa chỉ",
            dtype="TEXT",
            semantic_role=None,
            null_count=0,
        )
        tbl0_with_addr = tbl0_with_aux.model_copy(update={"columns": [*tbl0_with_aux.columns, col_addr]})
        business_with_addr = business.model_copy(
            update={"tables": [tbl0_with_addr, *business.tables[1:]]}
        )
        evidence_with_addr = verify_unsupported(plan, business_with_addr)
        assert evidence_with_addr.verified is False
        assert "Required field absence was not established" in evidence_with_addr.reason

    def test_semantic_plan_errors_address_filter_on_customer_name(self, business):
        tbl = next(t for t in business.tables if t.grain == "order_header")
        col_cname = ColumnMeta(
            name="ten_khach_hang",
            original_name="Tên khách hàng",
            dtype="TEXT",
            semantic_role="customer_name",
        )
        tbl_with_cname = tbl.model_copy(update={"columns": [*tbl.columns, col_cname]})
        context_no_addr = business.model_copy(
            update={"tables": [tbl_with_cname, *[t for t in business.tables if t.name != tbl.name]]}
        )

        q = Question(
            id="test_q_addr",
            business_id=business.business_id,
            question="khách hàng ở đường Nguyễn Văn Linh",
            as_of_date=date(2026, 3, 1),
        )
        plan = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[],
            sql=f'SELECT * FROM "{tbl.name}" WHERE lookup(ten_khach_hang) LIKE lookup(?)',
            parameters=["%Nguyen Van Linh%"],
            output=OutputSpec(type=AnswerType.LIST),
        )
        errors = semantic_plan_errors(plan, context_no_addr, q)
        assert any("ADDRESS_FILTER_ON_CUSTOMER_NAME" in err for err in errors)

        # When context has an address column, semantic check does not flag error
        col_addr = ColumnMeta(
            name="dia_chi",
            original_name="Địa chỉ",
            dtype="TEXT",
            semantic_role="customer_address",
        )
        tbl_with_addr = tbl_with_cname.model_copy(update={"columns": [*tbl_with_cname.columns, col_addr]})
        context_with_addr = business.model_copy(
            update={"tables": [tbl_with_addr, *[t for t in business.tables if t.name != tbl.name]]}
        )
        errors_with_addr = semantic_plan_errors(plan, context_with_addr, q)
        assert not any("ADDRESS_FILTER_ON_CUSTOMER_NAME" in err for err in errors_with_addr)

    def test_sqlite_lookup_preserves_wildcards(self):
        import sqlite3

        conn = sqlite3.connect(":memory:")
        register_helpers(conn)
        cursor = conn.cursor()

        res = cursor.execute("SELECT lookup(?)", ["%Cetirizin 10mg Domesco%"]).fetchone()[0]
        assert res == "%cetirizin 10mg domesco%"

        res_plain = cursor.execute("SELECT lookup(?)", ["Cetirizin 10mg Domesco"]).fetchone()[0]
        assert res_plain == "cetirizin 10mg domesco"

        match = cursor.execute(
            "SELECT lookup(?) LIKE lookup(?)",
            ["Cetirizin 10mg Domesco hộp 10 vỉ x 10 viên", "%Cetirizin 10mg Domesco%"],
        ).fetchone()[0]
        assert match == 1

    def test_solver_probe_requests_cross_table_scope(self, business, rules):
        t_lines = next(t for t in business.tables if t.grain == "order_line")
        t_orders = next(t for t in business.tables if t.grain == "order_header")

        # Turn 1 plan includes probe_requests that access t_orders (not in plan.tables)
        plan1 = QueryPlan(
            metric="sales_revenue",
            tables=[t_lines.name],
            windows=[TimeWindow(start=date(2026, 3, 1), end_exclusive=date(2026, 3, 5))],
            rule_refs=rules.required_refs("sales_revenue"),
            template_eligible=False,
            status_policy="revenue_eligible_recorded_status",
            output=OutputSpec(type=AnswerType.NUMBER),
            probe_requests=[
                ProbeRequest(
                    purpose="Inspect orders header table",
                    sql=f'SELECT * FROM "{t_orders.name}" LIMIT 2',
                    parameters=[],
                )
            ],
        )
        # Turn 2 plan is final executable
        plan2 = QueryPlan(
            metric="sales_revenue",
            tables=[t_lines.name],
            windows=[TimeWindow(start=date(2026, 3, 1), end_exclusive=date(2026, 3, 5))],
            rule_refs=rules.required_refs("sales_revenue"),
            template_eligible=True,
            status_policy="revenue_eligible_recorded_status",
            output=OutputSpec(type=AnswerType.NUMBER),
            probe_requests=[],
        )

        class MultiTurnMockModel:
            def __init__(self, plans):
                self.plans = plans
                self.turn = 0
                self.usage = SimpleNamespace(calls=0, tokens=0)

            def generate_json(self, *_args, **_kwargs):
                self.usage.calls += 1
                idx = min(self.turn, len(self.plans) - 1)
                self.turn += 1
                return self.plans[idx].model_dump(mode="json")

        model = MultiTurnMockModel([plan1, plan2])
        temporal = TemporalResolver()
        settings = Settings(
            max_probe_calls=2,
            max_model_calls=3,
            use_sql_worker_pool=False,
        )
        solver = Solver(model=model, rules=rules, temporal=temporal, settings=settings)
        question = Question(
            id="test_probe_q",
            business_id=business.business_id,
            question="Doanh thu tu ngay 01/03/2026 den 04/03/2026",
            as_of_date=date(2026, 3, 10),
        )
        pred, trace = solver.solve(question, business)
        assert len(trace.probes) == 1
        assert trace.probes[0]["status"] == "success"
        assert trace.probes[0]["row_count"] > 0
        assert trace.decision.action == "value"

    def test_solver_executes_candidate_sql_when_planner_finishes_in_grace_window(self, business, rules):
        t_lines = next(t for t in business.tables if t.grain == "order_line")
        plan = QueryPlan(
            metric="sales_revenue",
            tables=[t_lines.name],
            windows=[TimeWindow(start=date(2026, 3, 1), end_exclusive=date(2026, 3, 5))],
            rule_refs=rules.required_refs("sales_revenue"),
            template_eligible=True,
            status_policy="revenue_eligible_recorded_status",
            output=OutputSpec(type=AnswerType.NUMBER),
        )

        class GraceWindowMockModel:
            def __init__(self, plan_to_return: QueryPlan, delay: float = 0.08):
                self.plan = plan_to_return
                self.delay = delay
                self.usage = SimpleNamespace(calls=0, tokens=0)

            def generate_json(self, *_args, **_kwargs):
                time.sleep(self.delay)
                self.usage.calls += 1
                return self.plan.model_dump(mode="json")

        model = GraceWindowMockModel(plan, delay=0.08)
        temporal = TemporalResolver()
        settings = Settings(
            question_timeout=0.05,
            sql_timeout=5.0,
            use_sql_worker_pool=False,
        )
        solver = Solver(model=model, rules=rules, temporal=temporal, settings=settings)
        question = Question(
            id="test_grace_window_q",
            business_id=business.business_id,
            question="Doanh thu tu ngay 01/03/2026 den 04/03/2026",
            as_of_date=date(2026, 3, 10),
        )
        pred, trace = solver.solve(question, business)
        assert trace.decision.action == "value"
        assert pred.answer is not None
        assert len(trace.candidates) == 1
        cand_exec = trace.candidates[0].execution
        assert cand_exec.status == ExecutionStatus.SUCCESS
        assert cand_exec.error_code is None

    def test_status_column_indexed_during_ingestion(self, business):
        import sqlite3

        conn = sqlite3.connect(str(business.database))
        try:
            cursor = conn.cursor()
            rows = cursor.execute(
                "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index'"
            ).fetchall()
            status_indexes = [sql for _, _, sql in rows if sql and "status" in sql.lower()]
            assert len(status_indexes) > 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Open-Weights General Resilience Test Suite
# ---------------------------------------------------------------------------

class TestOpenWeightsGeneralResilience:
    def test_status_policy_and_source_refs_defaulting(self, business, rules):
        # 1. status_policy: None defaulting
        for metric, expected_policy in [
            ("sales_revenue", "revenue_eligible_recorded_status"),
            ("top_products", "revenue_eligible_recorded_status"),
            ("gross_profit", "revenue_eligible_recorded_status"),
            ("cancellation_rate", "all_orders"),
            ("debt_total", "debt_eligible_recorded_status"),
            ("generic_sql", "custom"),
            ("unsupported", "custom"),
        ]:
            raw = {
                "metric": metric,
                "status_policy": None,
                "tables": [business.tables[0].name],
                "rule_refs": [rules.records[0].id],
                "sql": "SELECT 1" if metric != "unsupported" else None,
                "output": {"type": "number", "shape": "scalar"},
            }
            res = canonicalize_plan_input(raw, business)
            assert res["status_policy"] == expected_policy

        # 2. unsupported.source_refs: [] defaulting
        raw_unsup = {
            "metric": "unsupported",
            "status_policy": None,
            "tables": [],
            "rule_refs": rules.required_refs("unsupported"),
            "sql": None,
            "output": {"type": "unanswerable", "shape": "scalar"},
            "unsupported": {
                "kind": "missing_field",
                "required": ["customer_address"],
                "explanation": "No customer address recorded",
                "source_refs": [],
            },
        }
        res_unsup = canonicalize_plan_input(raw_unsup, business)
        assert res_unsup["unsupported"]["source_refs"] == ["README.md"]

        # 3. Tolerant join resolution when join_id is omitted
        edge = business.joins[0]
        raw_join = {
            "metric": "generic_sql",
            "tables": [edge.left, edge.right],
            "joins": [{"left": edge.left, "right": edge.right}],
            "sql": "SELECT 1",
            "output": {"type": "number", "shape": "scalar"},
        }
        res_join = canonicalize_plan_input(raw_join, business)
        assert res_join["joins"] == [edge.model_dump(mode="json")]

    def test_semantic_role_metadata_field_resolution(self, business):
        tbl = next(t for t in business.tables if t.grain == "order_line")
        amount_col = tbl.role("line_amount")
        status_col = tbl.role("status")
        assert amount_col is not None
        assert status_col is not None

        raw = {
            "metric": "generic_sql",
            "tables": [tbl.name],
            "required_fields": [f"{tbl.name}.line_amount"],
            "filters": [{"field": "status", "operator": "eq", "values": ["completed"]}],
            "sql": f'SELECT "{amount_col}" FROM "{tbl.name}"',
            "output": {"type": "number", "shape": "scalar"},
        }
        res = canonicalize_plan_input(raw, business)
        assert res["required_fields"] == [f"{tbl.name}.{amount_col}"]
        assert res["filters"][0]["field"] == f"{tbl.name}.{status_col}"

    def test_semantic_guardrails_percent_date_and_quantity(self, business, rules):
        tbl = next(t for t in business.tables if t.grain == "order_line")
        amount_col = tbl.role("line_amount")

        # 1. OUTPUT_TYPE_MUST_BE_PERCENT
        plan_num = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[rules.records[0].id],
            sql=f'SELECT 1 FROM "{tbl.name}"',
            output=OutputSpec(type=AnswerType.NUMBER, shape="scalar"),
        )
        q_pct = Question(
            id="test_pct",
            business_id=business.business_id,
            question="Tỷ lệ đơn hàng bị huỷ là bao nhiêu %?",
            as_of_date=date(2026, 3, 4),
        )
        errs_pct = semantic_plan_errors(plan_num, business, q_pct)
        assert any(e.startswith("OUTPUT_TYPE_MUST_BE_PERCENT") for e in errs_pct)

        # 2. OUTPUT_TYPE_MUST_BE_DATE
        plan_str = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[rules.records[0].id],
            sql=f'SELECT "2026-03-01" FROM "{tbl.name}"',
            output=OutputSpec(type=AnswerType.STRING, shape="scalar"),
        )
        q_date = Question(
            id="test_date",
            business_id=business.business_id,
            question="Ngày nào có doanh thu cao nhất?",
            as_of_date=date(2026, 3, 4),
        )
        errs_date = semantic_plan_errors(plan_str, business, q_date)
        assert any(e.startswith("OUTPUT_TYPE_MUST_BE_DATE") for e in errs_date)

        # 3. QUANTITY_QUESTION_SUMS_MONEY_COLUMN
        plan_sum_money = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[rules.records[0].id],
            sql=f'SELECT SUM("{amount_col}") FROM "{tbl.name}"',
            output=OutputSpec(type=AnswerType.NUMBER, shape="scalar"),
        )
        q_qty = Question(
            id="test_qty",
            business_id=business.business_id,
            question="Bao nhiêu ly trà sữa đã bán?",
            as_of_date=date(2026, 3, 4),
        )
        errs_qty = semantic_plan_errors(plan_sum_money, business, q_qty)
        assert any(e.startswith("QUANTITY_QUESTION_SUMS_MONEY_COLUMN") for e in errs_qty)

        # 4. Phrasing "bao nhiêu % đơn bị huỷ" triggers RETURN_LINE_IN_ORDER_COHORT
        context_neg = business.model_copy(update={"readme": "Có số lượng âm cho hàng trả lại"})
        plan_cancel = QueryPlan(
            metric="cancellation_rate",
            tables=[tbl.name],
            rule_refs=rules.required_refs("cancellation_rate"),
            sql=f'SELECT COUNT(DISTINCT "{tbl.role("order_id")}") FROM "{tbl.name}"',
            output=OutputSpec(type=AnswerType.PERCENT, shape="scalar"),
        )
        q_cancel = Question(
            id="test_cancel",
            business_id=business.business_id,
            question="Bao nhiêu % đơn bị huỷ?",
            as_of_date=date(2026, 3, 4),
        )
        errs_cancel = semantic_plan_errors(plan_cancel, context_neg, q_cancel)
        assert any(e.startswith("RETURN_LINE_IN_ORDER_COHORT") for e in errs_cancel)

    def test_single_row_homogeneous_numeric_transposition(self, business, rules):
        tbl = business.tables[0]
        plan_list = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[rules.records[0].id],
            sql='SELECT 9788500 AS rev_a, 156637000 AS rev_b',
            output=OutputSpec(type=AnswerType.LIST, shape="list", element_type="number"),
        )

        # 1. Homogeneous numeric row -> transposes to list of numbers
        cand_ok = _make_candidate(plan_list, [[9788500, 156637000]], ["rev_a", "rev_b"])
        verified_ok = verify_candidate(cand_ok, plan_list)
        assert verified_ok.verified is True
        assert verified_ok.value == [9788500, 156637000]

        # 2. Mixed row [["Prod A", 500]] -> raises ValueError, not verified
        cand_mixed = _make_candidate(plan_list, [["Prod A", 500]], ["product", "rev"])
        verified_mixed = verify_candidate(cand_mixed, plan_list)
        assert verified_mixed.verified is False
        assert any("List output requires one result column" in c for c in verified_mixed.checks)

        # 3. Multi-row [[1, 2], [3, 4]] -> raises ValueError, not verified
        cand_multi = _make_candidate(plan_list, [[1, 2], [3, 4]], ["col_a", "col_b"])
        verified_multi = verify_candidate(cand_multi, plan_list)
        assert verified_multi.verified is False
        assert any("List output requires one result column" in c for c in verified_multi.checks)

    def test_canonical_required_role_synonyms(self):
        assert _canonical_required_role("inventory_quantity") == "stock"
        assert _canonical_required_role("qty_stock") == "stock"
        assert _canonical_required_role("sales_channel") == "channel"
        assert _canonical_required_role("expiration_date") == "expires_at"

    # -----------------------------------------------------------------------
    # Round-2 Generalized Resilience Mechanisms (<15B & Open-Weights Models)
    # -----------------------------------------------------------------------

    def test_mechanism_1_output_null_recovery(self, business):
        # 1. Output table with columns=None, shape=None, rounding=None
        raw_table = {
            "metric": "generic_sql",
            "tables": [business.tables[0].name],
            "rule_refs": ["rb:r01"],
            "sql": "SELECT 1",
            "output": {"type": "table", "columns": None, "shape": None, "rounding": None},
        }
        res_table = canonicalize_plan_input(raw_table, business)
        assert res_table["output"]["columns"] == []
        assert res_table["output"]["shape"] == "table"
        assert res_table["output"]["rounding"] == "none"
        plan_table = QueryPlan.model_validate(res_table)
        assert plan_table.output.shape == "table"
        assert plan_table.output.rounding == "none"

        # 2. Output percent with shape=None, rounding=None
        raw_percent = {
            "metric": "generic_sql",
            "tables": [business.tables[0].name],
            "rule_refs": ["rb:r01"],
            "sql": "SELECT 1",
            "output": {"type": "percent", "columns": None, "shape": None, "rounding": None},
        }
        res_percent = canonicalize_plan_input(raw_percent, business)
        assert res_percent["output"]["columns"] == []
        assert res_percent["output"]["shape"] == "scalar"
        assert res_percent["output"]["rounding"] == "two_decimals"
        plan_percent = QueryPlan.model_validate(res_percent)
        assert plan_percent.output.rounding == "two_decimals"

        # 3. Output list with shape=None, rounding=None
        raw_list = {
            "metric": "generic_sql",
            "tables": [business.tables[0].name],
            "rule_refs": ["rb:r01"],
            "sql": "SELECT 1",
            "output": {"type": "list", "columns": None, "shape": None, "rounding": None},
        }
        res_list = canonicalize_plan_input(raw_list, business)
        assert res_list["output"]["shape"] == "list"
        assert res_list["output"]["rounding"] == "none"

    def test_mechanism_2_generic_sql_empty_rule_refs_recovery(self, business, rules):
        tbl = business.tables[0]
        q = Question(
            id="test_m2",
            business_id=business.business_id,
            question="Doanh thu hom nay",
            as_of_date=date(2026, 3, 5),
        )
        planner = Planner(None, rules, TemporalResolver(), Settings())

        # 1. Empty rule_refs list in generic_sql response
        raw_response_empty = {
            "metric": "generic_sql",
            "tables": [tbl.name],
            "rule_refs": [],
            "sql": f'SELECT 1 FROM "{tbl.name}"',
            "output": {"type": "number", "shape": "scalar", "rounding": "none"},
        }
        planner.model = SimpleNamespace(generate_json=lambda *a, **k: copy.deepcopy(raw_response_empty))
        plan = planner.make_plan(q, business, deadline=time.monotonic() + 30)
        assert plan.rule_refs == [next(iter(rules.by_id))]
        assert any("default rule_ref populated for generic_sql" in note for note in plan.derivation_notes)

        # 2. Unknown rule_refs list filtered to empty
        raw_response_unknown = {
            "metric": "generic_sql",
            "tables": [tbl.name],
            "rule_refs": ["rb:nonexistent_rule_xyz"],
            "sql": f'SELECT 1 FROM "{tbl.name}"',
            "output": {"type": "number", "shape": "scalar", "rounding": "none"},
        }
        planner.model = SimpleNamespace(generate_json=lambda *a, **k: copy.deepcopy(raw_response_unknown))
        plan2 = planner.make_plan(q, business, deadline=time.monotonic() + 30)
        assert plan2.rule_refs == [next(iter(rules.by_id))]
        assert any("default rule_ref populated for generic_sql" in note for note in plan2.derivation_notes)

        # 3. validate_plan remains strict when called directly with empty rule_refs
        strict_plan = plan.model_copy(update={"rule_refs": []})
        errs = validate_plan(strict_plan, business, rules, q)
        assert "INVALID_RULE_REFS" in errs

    def test_mechanism_3_table_stem_typo_recovery(self, business, rules):
        import sqlite3
        # Build context with a physical table named <stem>_<8-hex>
        stem = "danhsachhoadon_05_2026"
        actual_name = f"{stem}_4396fcb0"
        typo_name = f"{stem}_4396f0b0"
        base_tbl = business.tables[0]
        test_tbl = TableMeta(
            name=actual_name,
            sources=base_tbl.sources,
            columns=base_tbl.columns,
            grain=base_tbl.grain,
            row_count=base_tbl.row_count,
            raw_row_count=base_tbl.raw_row_count,
            coverage=base_tbl.coverage,
        )
        conn = sqlite3.connect(business.database)
        try:
            conn.execute(f'CREATE TABLE IF NOT EXISTS "{actual_name}" ("order_id" TEXT)')
            conn.commit()
        finally:
            conn.close()

        context = business.model_copy(update={"tables": [test_tbl]})
        q = Question(
            id="test_m3",
            business_id=business.business_id,
            question="Liet ke hoa don",
            as_of_date=date(2026, 3, 5),
        )
        planner = Planner(None, rules, TemporalResolver(), Settings())
        raw_response = {
            "metric": "generic_sql",
            "tables": [typo_name],
            "rule_refs": [next(iter(rules.by_id))],
            "sql": f'SELECT * FROM "{typo_name}" WHERE "{typo_name}".order_id IS NOT NULL',
            "required_fields": [f"{typo_name}.order_id"],
            "output": {"type": "table", "shape": "table"},
        }
        planner.model = SimpleNamespace(generate_json=lambda *a, **k: copy.deepcopy(raw_response))
        try:
            plan = planner.make_plan(q, context, deadline=time.monotonic() + 30)

            # Typo token replaced with actual table name
            assert actual_name in plan.tables
            assert typo_name not in plan.tables
            assert actual_name in plan.sql
            assert typo_name not in plan.sql
            assert any(
                f"table-stem hex typo recovered: {typo_name} -> {actual_name}" in note
                for note in plan.derivation_notes
            )
        finally:
            shutdown_sql_pool()

    def test_mechanism_4_argmax_scalar_2col_recovery(self, business, rules):
        tbl = business.tables[0]
        # 1. DATE argmax recovery: [[date_str, numeric_metric]]
        plan_date = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT ngay, rev FROM t ORDER BY rev DESC LIMIT 1',
            output=OutputSpec(type=AnswerType.DATE, shape="scalar"),
        )
        cand_date = _make_candidate(plan_date, [["2026-03-05", 15000000]], ["ngay", "rev"])
        verified_date = verify_candidate(cand_date, plan_date)
        assert verified_date.verified is True
        assert verified_date.value == "2026-03-05"

        # 2. STRING argmax recovery: [[label_str, numeric_metric]]
        plan_str = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT customer_name, rev FROM t ORDER BY rev DESC LIMIT 1',
            output=OutputSpec(type=AnswerType.STRING, shape="scalar"),
        )
        cand_str = _make_candidate(plan_str, [["Khach Hang VIP", 95000000]], ["customer_name", "rev"])
        verified_str = verify_candidate(cand_str, plan_str)
        assert verified_str.verified is True
        assert verified_str.value == "Khach Hang VIP"

        # 3. Number scalar with [100, 200] strictly raises ValueError
        plan_num = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT a, b FROM t',
            output=OutputSpec(type=AnswerType.NUMBER, shape="scalar"),
        )
        cand_num = _make_candidate(plan_num, [[100, 200]], ["a", "b"])
        verified_num = verify_candidate(cand_num, plan_num)
        assert verified_num.verified is False
        assert any("Scalar output requires exactly one non-null cell" in c for c in verified_num.checks)

    def test_mechanism_5_text_sort_on_decimal_helper(self, business, rules):
        tbl = business.tables[0]
        q = Question(id="test_m5", business_id=business.business_id, question="Top doanh thu", as_of_date=date(2026, 3, 5))

        # 1. Alias of decimal_sum in ORDER BY without CAST -> flag
        plan_alias = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT id, decimal_sum(amount) AS total_rev FROM t GROUP BY id ORDER BY total_rev DESC',
            output=OutputSpec(type=AnswerType.TABLE),
        )
        errs1 = semantic_plan_errors(plan_alias, business, q)
        assert any("TEXT_SORT_ON_DECIMAL_HELPER" in e for e in errs1)

        # 2. Direct decimal_sum in ORDER BY without CAST -> flag
        plan_direct = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT id FROM t GROUP BY id ORDER BY decimal_sum(amount) DESC',
            output=OutputSpec(type=AnswerType.LIST),
        )
        errs2 = semantic_plan_errors(plan_direct, business, q)
        assert any("TEXT_SORT_ON_DECIMAL_HELPER" in e for e in errs2)

        # 3. Wrapped with CAST(... AS REAL) in ORDER BY -> clean
        plan_clean1 = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT id, decimal_sum(amount) AS total_rev FROM t GROUP BY id ORDER BY CAST(total_rev AS REAL) DESC',
            output=OutputSpec(type=AnswerType.TABLE),
        )
        assert not any("TEXT_SORT_ON_DECIMAL_HELPER" in e for e in semantic_plan_errors(plan_clean1, business, q))

        # 4. Built-in SUM() in ORDER BY -> clean
        plan_clean2 = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT id, SUM(amount) AS total_rev FROM t GROUP BY id ORDER BY total_rev DESC',
            output=OutputSpec(type=AnswerType.TABLE),
        )
        assert not any("TEXT_SORT_ON_DECIMAL_HELPER" in e for e in semantic_plan_errors(plan_clean2, business, q))

    def test_mechanism_6_tuple_list_and_unrequested_id_table(self, business, rules):
        tbl = business.tables[0]
        # (a) OUTPUT_TYPE_MUST_BE_LIST
        q_tuple = Question(
            id="test_tuple",
            business_id=business.business_id,
            question="Doanh thu chi nhánh A và B lần lượt là bao nhiêu?",
            as_of_date=date(2026, 3, 5),
        )
        plan_tbl_out = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT 1, 2',
            output=OutputSpec(type=AnswerType.TABLE, shape="table"),
        )
        errs_tuple = semantic_plan_errors(plan_tbl_out, business, q_tuple)
        assert any("OUTPUT_TYPE_MUST_BE_LIST" in e for e in errs_tuple)

        # (b) UNREQUESTED_ID_COLUMN_IN_TABLE_OUTPUT
        cust_tbl = TableMeta(
            name="customer_tbl",
            sources=tbl.sources,
            columns=[
                ColumnMeta(name="ma_kh", original_name="Mã khách", dtype="string", semantic_role="customer_id"),
                ColumnMeta(name="ten_kh", original_name="Tên khách", dtype="string", semantic_role="customer_name"),
                ColumnMeta(name="doanh_thu", original_name="Doanh thu", dtype="number", semantic_role="order_revenue"),
            ],
            grain="dimension",
            row_count=10,
            raw_row_count=10,
            coverage={},
        )
        context_cust = business.model_copy(update={"tables": [cust_tbl]})
        q_top_cust = Question(
            id="test_top_cust",
            business_id=business.business_id,
            question="Top 5 khách hàng có doanh thu cao nhất?",
            as_of_date=date(2026, 3, 5),
        )
        # Model projects both ma_kh and ten_kh in table output when question didn't ask for ID
        plan_unreq_id = QueryPlan(
            metric="generic_sql",
            tables=[cust_tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql=f'SELECT ma_kh, ten_kh, SUM(doanh_thu) FROM "{cust_tbl.name}" GROUP BY ma_kh',
            output=OutputSpec(type=AnswerType.TABLE, shape="table"),
        )
        errs_id = semantic_plan_errors(plan_unreq_id, context_cust, q_top_cust)
        assert any("UNREQUESTED_ID_COLUMN_IN_TABLE_OUTPUT" in e for e in errs_id)

        # But if question explicitly asks for 'mã khách', unrequested ID check does NOT flag
        q_with_id = Question(
            id="test_with_id",
            business_id=business.business_id,
            question="Cho biết mã khách và tên khách hàng có doanh thu cao nhất?",
            as_of_date=date(2026, 3, 5),
        )
        assert not any("UNREQUESTED_ID_COLUMN_IN_TABLE_OUTPUT" in e for e in semantic_plan_errors(plan_unreq_id, context_cust, q_with_id))

    def test_mechanism_7_header_revenue_and_retail_exclusion(self, business, rules):
        # (a) RAW_RETAIL_EXCLUSION_WITHOUT_LOOKUP
        tbl = business.tables[0]
        q = Question(id="test_m7", business_id=business.business_id, question="Doanh thu khach", as_of_date=date(2026, 3, 5))
        plan_raw_retail = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT customer_name, SUM(amount) FROM orders WHERE customer_name NOT IN (\'lẻ\', \'kl\', \'khách lẻ\') GROUP BY customer_name',
            output=OutputSpec(type=AnswerType.TABLE),
        )
        errs_retail = semantic_plan_errors(plan_raw_retail, business, q)
        assert any("RAW_RETAIL_EXCLUSION_WITHOUT_LOOKUP" in e for e in errs_retail)

        plan_lookup_retail = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT customer_name, SUM(amount) FROM orders WHERE lookup(customer_name) NOT IN (\'le\', \'kl\', \'khach le\') GROUP BY customer_name',
            output=OutputSpec(type=AnswerType.TABLE),
        )
        assert not any("RAW_RETAIL_EXCLUSION_WITHOUT_LOOKUP" in e for e in semantic_plan_errors(plan_lookup_retail, business, q))

        # (b) HEADER_REVENUE_PREFERRED_OVER_LINE_SUM
        hdr_tbl = TableMeta(
            name="order_hdr",
            sources=tbl.sources,
            columns=[
                ColumnMeta(name="order_id", original_name="Mã đơn", dtype="string", semantic_role="order_id"),
                ColumnMeta(name="ordered_at", original_name="Ngày đặt", dtype="date", semantic_role="ordered_at"),
                ColumnMeta(name="tong_cong", original_name="Tổng cộng", dtype="number", semantic_role="total_due"),
                ColumnMeta(name="customer_id", original_name="Mã khách", dtype="string", semantic_role="customer_id"),
            ],
            grain="order_header",
            row_count=10,
            raw_row_count=10,
            coverage={},
        )
        line_tbl = TableMeta(
            name="order_items",
            sources=tbl.sources,
            columns=[
                ColumnMeta(name="order_id", original_name="Mã đơn", dtype="string", semantic_role="order_id"),
                ColumnMeta(name="product_id", original_name="Mã SP", dtype="string", semantic_role="product_id"),
                ColumnMeta(name="product_name", original_name="Tên SP", dtype="string", semantic_role="product_name"),
                ColumnMeta(name="thanh_tien", original_name="Thành tiền", dtype="number", semantic_role="line_amount"),
            ],
            grain="order_line",
            row_count=20,
            raw_row_count=20,
            coverage={},
        )
        context_orders = business.model_copy(update={"tables": [hdr_tbl, line_tbl]})
        # Query joins header and line ONLY to sum line_amount with NO product filter
        plan_line_sum = QueryPlan(
            metric="generic_sql",
            tables=[hdr_tbl.name, line_tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql=f'SELECT h.customer_id, SUM(l.thanh_tien) FROM "{hdr_tbl.name}" h JOIN "{line_tbl.name}" l ON h.order_id = l.order_id GROUP BY h.customer_id',
            output=OutputSpec(type=AnswerType.TABLE),
        )
        errs_hdr = semantic_plan_errors(plan_line_sum, context_orders, q)
        assert not any("HEADER_REVENUE_PREFERRED_OVER_LINE_SUM" in e for e in errs_hdr)

        # When product column IS referenced, check does NOT flag
        plan_prod_filter = QueryPlan(
            metric="generic_sql",
            tables=[hdr_tbl.name, line_tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql=f'SELECT l.product_name, SUM(l.thanh_tien) FROM "{hdr_tbl.name}" h JOIN "{line_tbl.name}" l ON h.order_id = l.order_id GROUP BY l.product_id',
            output=OutputSpec(type=AnswerType.TABLE),
        )
        assert not any("HEADER_REVENUE_PREFERRED_OVER_LINE_SUM" in e for e in semantic_plan_errors(plan_prod_filter, context_orders, q))

    def test_sqlite_premature_round_or_int_div_and_date_pattern(self, business, rules):
        q = Question(id="test_premature", business_id=business.business_id, question="Doanh thu trung binh", as_of_date=date(2026, 3, 5))
        tbl = business.tables[0]
        # (a) Integer division of aggregates without 1.0 or cast
        plan_int_div = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT SUM(amount) / COUNT(DISTINCT order_id) FROM orders',
            output=OutputSpec(type=AnswerType.NUMBER),
        )
        errs = semantic_plan_errors(plan_int_div, business, q)
        assert any("SQLITE_PREMATURE_ROUND_OR_INT_DIV" in e for e in errs)

        # (b) Premature ROUND wrapping division
        plan_round_div = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT ROUND(SUM(amount) / COUNT(order_id)) FROM orders',
            output=OutputSpec(type=AnswerType.NUMBER),
        )
        errs_round = semantic_plan_errors(plan_round_div, business, q)
        assert any("SQLITE_PREMATURE_ROUND_OR_INT_DIV" in e for e in errs_round)

        # (c) Accepted: decimal_div or * 1.0 without outer ROUND/CAST
        plan_decimal_div = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT decimal_div(SUM(amount), COUNT(DISTINCT order_id)) FROM orders',
            output=OutputSpec(type=AnswerType.NUMBER, rounding="money"),
        )
        assert not any("SQLITE_PREMATURE_ROUND_OR_INT_DIV" in e for e in semantic_plan_errors(plan_decimal_div, business, q))

        plan_mult_float = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT SUM(amount) * 1.0 / COUNT(DISTINCT order_id) FROM orders',
            output=OutputSpec(type=AnswerType.NUMBER, rounding="money"),
        )
        assert not any("SQLITE_PREMATURE_ROUND_OR_INT_DIV" in e for e in semantic_plan_errors(plan_mult_float, business, q))

        # (d) Swapped Vietnamese date text pattern in LIKE
        plan_bad_date = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql="SELECT SUM(amount) FROM orders WHERE order_date LIKE '%9 thg 2025%'",
            output=OutputSpec(type=AnswerType.NUMBER),
        )
        errs_date = semantic_plan_errors(plan_bad_date, business, q)
        assert any("INVALID_VN_TEXT_DATE_PATTERN" in e for e in errs_date)

        # (e) Two-column ratio query (separate SUM and COUNT columns) produces ZERO SQLITE_PREMATURE_ROUND_OR_INT_DIV
        plan_ratio_2col = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql="SELECT SUM(thanh_tien), COUNT(DISTINCT ma_don) FROM don_hang",
            output=OutputSpec(type=AnswerType.NUMBER, shape="ratio"),
        )
        assert not any("SQLITE_PREMATURE_ROUND_OR_INT_DIV" in e for e in semantic_plan_errors(plan_ratio_2col, business, q))

    def test_verifier_infer_money_rounding_half_up(self, business, rules):
        tbl = TableMeta(
            name="order_lines",
            sources=business.tables[0].sources,
            columns=[
                ColumnMeta(name="order_id", original_name="Mã đơn", dtype="string", semantic_role="order_id"),
                ColumnMeta(name="thanh_tien", original_name="Thành tiền", dtype="number", semantic_role="line_amount"),
            ],
            grain="order_line",
            row_count=10,
            raw_row_count=10,
            coverage={},
        )
        ctx = business.model_copy(update={"tables": [tbl]})
        q = Question(id="test_avg_money", business_id=business.business_id, question="Gia tri don hang trung binh", as_of_date=date(2026, 3, 5))
        plan = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql="SELECT decimal_div(SUM(thanh_tien), COUNT(DISTINCT order_id)) FROM order_lines",
            output=OutputSpec(type=AnswerType.NUMBER, rounding="none"),
        )
        cand1 = _make_candidate(plan, [[Decimal("549335.55")]], ["avg_val"])
        verified1 = verify_candidate(cand1, plan, context=ctx, question=q)
        assert verified1.verified
        assert verified1.output.rounding == "money"
        assert verified1.value == 549336

        # SQLite decimal_div custom function returns TEXT ("549335.55"), not float or Decimal
        cand_str = _make_candidate(plan, [["549335.55"]], ["avg_val"])
        verified_str = verify_candidate(cand_str, plan, context=ctx, question=q)
        assert verified_str.verified
        assert verified_str.output.rounding == "money"
        assert verified_str.value == 549336

        cand2 = _make_candidate(plan, [[Decimal("988474.5")]], ["avg_val"])
        verified2 = verify_candidate(cand2, plan, context=ctx, question=q)
        assert verified2.verified
        assert verified2.output.rounding == "money"
        assert verified2.value == 988475

    def test_verifier_1row_tuple_transposition(self, business, rules):
        tbl = business.tables[0]
        # Multi-part numeric answer projection with limit=1: [[12, 5]] -> [12, 5]
        plan_num_tuple = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql="SELECT 12, 5 LIMIT 1",
            limit=1,
            output=OutputSpec(type=AnswerType.LIST, element_type="number"),
        )
        cand_num = _make_candidate(plan_num_tuple, [[12, 5]], ["col1", "col2"])
        ver_num = verify_candidate(cand_num, plan_num_tuple)
        assert ver_num.verified
        assert ver_num.value == [12, 5]

        # Mixed top-1 ranking error projection with limit=1: [["Product A", 500]] -> REJECTED
        plan_mixed = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql="SELECT product_name, 500 LIMIT 1",
            limit=1,
            output=OutputSpec(type=AnswerType.LIST),
        )
        cand_mixed = _make_candidate(plan_mixed, [["Product A", 500]], ["pname", "val"])
        ver_mixed = verify_candidate(cand_mixed, plan_mixed)
        assert not ver_mixed.verified
        assert any("List output requires one result column" in c for c in ver_mixed.checks)

    def test_canonicalize_plan_input_output_none_and_filters_cleanup(self, business):
        raw_plan = {
            "metric": "unsupported",
            "tables": [business.tables[0].name],
            "rule_refs": [],
            "output": None,
            "filters": [{"field": f"{business.tables[0].name}.status", "operator": "eq", "values": ["completed"], "lookup_normalized": True, "reason": "test"}],
        }
        res = canonicalize_plan_input(raw_plan, business)
        assert res["output"] is not None
        assert res["output"]["type"] == "unanswerable"
        assert "lookup_normalized" not in res["filters"][0]
        assert "reason" not in res["filters"][0]

    def test_multi_month_temporal_resolution_and_partition_pruning(self, business, rules):
        tr = TemporalResolver()
        w1 = tr.resolve("Từ tháng 2/2026 đến tháng 4/2026", date(2026, 5, 1))
        assert len(w1) == 1
        assert w1[0].start == date(2026, 2, 1)
        assert w1[0].end_exclusive == date(2026, 5, 1)

        w2 = tr.resolve("qua 3 tháng 4/2025, 5/2025, 6/2025", date(2025, 7, 1))
        assert len(w2) == 1
        assert w2[0].start == date(2025, 4, 1)
        assert w2[0].end_exclusive == date(2025, 7, 1)

        p_feb = TableMeta(
            name="orders_02_2026",
            sources=business.tables[0].sources,
            columns=[
                ColumnMeta(name="order_id", original_name="Mã đơn", dtype="string", semantic_role="order_id"),
                ColumnMeta(name="ordered_at", original_name="Ngày đặt", dtype="date", semantic_role="ordered_at"),
            ],
            grain="order_header",
            row_count=10,
            raw_row_count=10,
            coverage={"ordered_at": ("2026-02-01", "2026-02-28")},
        )
        p_jan = TableMeta(
            name="orders_01_2026",
            sources=business.tables[0].sources,
            columns=[
                ColumnMeta(name="order_id", original_name="Mã đơn", dtype="string", semantic_role="order_id"),
                ColumnMeta(name="ordered_at", original_name="Ngày đặt", dtype="date", semantic_role="ordered_at"),
            ],
            grain="order_header",
            row_count=10,
            raw_row_count=10,
            coverage={"ordered_at": ("2026-01-01", "2026-01-31")},
        )
        plan_generic = QueryPlan(
            metric="generic_sql",
            tables=[p_feb.name],
            rule_refs=[next(iter(rules.by_id))],
            sql="SELECT COUNT(*) FROM orders_02_2026",
            output=OutputSpec(type=AnswerType.NUMBER),
            windows=[],
        )
        ctx = business.model_copy(update={"tables": [p_feb, p_jan]})
        assert partition_is_needed(p_feb, plan_generic, ctx, fallback_windows=w1)
        assert not partition_is_needed(p_jan, plan_generic, ctx, fallback_windows=w1)

    def test_sql_iso_date_parsing(self):
        assert _sql_iso_date("15 thg 9 2025") == "2025-09-15"
        assert _sql_iso_date("5 thg 09, 2025") == "2025-09-05"
        assert _sql_iso_date("15 thg 9, 2025") == "2025-09-15"
        assert _sql_iso_date("15 tháng 9 năm 2025") == "2025-09-15"
        assert _sql_iso_date("15/08/2025") == "2025-08-15"

    def test_triage_shorthands_and_subgroup_rejections(self):
        assert parse_fast_path_slots("dthu tháng 10/2025 bao nhiêu?") == {"metric": "sales_revenue", "top_k": 1}
        assert parse_fast_path_slots("coi giumf toongr dthu trong ngày hôm nay là bnhiu") == {"metric": "sales_revenue", "top_k": 1}
        assert parse_fast_path_slots("thang 8/2026 ti le don bi huy la bn %") == {"metric": "cancellation_rate", "top_k": 1}

        assert parse_fast_path_slots("dthu tháng 10/2025 của khách lẻ bao nhiêu?") is None
        assert parse_fast_path_slots("thang 8/2026 co bao nhieu don bi huy") is None

    def test_pipeline_validate_plan_receives_configured_temporal_fallback(self, business, rules, monkeypatch):
        temporal = TemporalResolver()
        expected_window = TimeWindow(role="target", start=date(2026, 2, 1), end_exclusive=date(2026, 3, 1))
        monkeypatch.setattr(temporal, "resolve", lambda q, as_of: [expected_window])

        captured_fallback = []
        import bizreason.pipeline as pipeline_mod
        orig_validate = pipeline_mod.validate_plan

        def spy_validate(plan, context, rules, question, fallback_windows=None):
            captured_fallback.append(fallback_windows)
            return orig_validate(plan, context, rules, question, fallback_windows=fallback_windows)

        monkeypatch.setattr(pipeline_mod, "validate_plan", spy_validate)

        tbl = business.tables[0]
        plan = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql="SELECT COUNT(*) FROM " + tbl.name,
            output=OutputSpec(type=AnswerType.NUMBER),
            windows=[],
        )
        model = OfflineModel(plan)
        settings = Settings(model="offline-model")
        solver = Solver(model=model, rules=rules, temporal=temporal, settings=settings)
        q = Question(id="test_fallback", business_id=business.business_id, question="Dịp tết vừa qua", as_of_date=date(2026, 3, 5))
        solver.solve(q, business)

        assert len(captured_fallback) > 0
        assert captured_fallback[0] == [expected_window]


class TestOpenWeightsRound4Fixes:
    def test_unsupported_canonicalize_and_validate(self, business, rules):
        # 1. metric="unsupported" / output.type="unanswerable" with "required_fields": ["customer_address"]
        # passes canonicalize_plan_input + validate_plan with 0 QUALIFY_REQUIRED_FIELD errors.
        raw = {
            "metric": "generic_sql",
            "tables": [],
            "required_fields": ["customer_address"],
            "filters": [],
            "group_by": [],
            "rule_refs": rules.required_refs("unsupported"),
            "output": {"type": "unanswerable"},
            "unsupported": {
                "kind": "missing_field",
                "required": ["customer_address"],
                "explanation": "No customer address column found",
                "source_refs": [],
            },
        }
        canonical = canonicalize_plan_input(raw, business)
        assert canonical["metric"] == "unsupported"
        assert canonical["required_fields"] == []
        assert canonical["filters"] == []
        assert canonical["group_by"] == []
        assert canonical["status_policy"] == "custom"
        assert canonical["output"]["type"] == "unanswerable"

        plan = QueryPlan.model_validate(canonical)
        q = Question(id="q_unsupp", business_id=business.business_id, question="Địa chỉ khách hàng", as_of_date=date(2026, 3, 1))
        errors = validate_plan(plan, business, rules, q)
        assert not any("QUALIFY_REQUIRED_FIELD" in e for e in errors)
        assert errors == []

    def test_plan_filter_canonicalization(self, business):
        # 2. PlanFilter canonicalization ("op": "!=", "op": "NOT IN", "op": "between",
        # scalar "value", and dropping unrepresentable decorative filters on generic_sql).
        tbl = business.tables[0]
        c1, c2, c3, c4 = tbl.columns[0].name, tbl.columns[1].name, tbl.columns[2].name, tbl.columns[3].name
        raw = {
            "metric": "generic_sql",
            "sql": f'SELECT 1 FROM "{tbl.name}"',
            "tables": [tbl.name],
            "rule_refs": [],
            "filters": [
                {"column": c1, "table": tbl.name, "op": "!=", "value": "val1"},
                {"field": f"{tbl.name}.{c2}", "op": "NOT IN", "value": "val2"},
                {"field": f"{tbl.name}.{c3}", "op": "between", "values": [10, 20]},
                "not_a_dict_so_drop_on_generic_sql",
                {"field": f"{tbl.name}.{c4}", "op": "unsupported_operator", "values": ["x"]},
            ],
        }
        canonical = canonicalize_plan_input(raw, business)
        assert len(canonical["filters"]) == 4
        assert canonical["filters"][0] == {"field": f"{tbl.name}.{c1}", "operator": "ne", "values": ["val1"]}
        assert canonical["filters"][1] == {"field": f"{tbl.name}.{c2}", "operator": "not_in", "values": ["val2"]}
        assert canonical["filters"][2] == {"field": f"{tbl.name}.{c3}", "operator": "gte", "values": [10]}
        assert canonical["filters"][3] == {"field": f"{tbl.name}.{c3}", "operator": "lte", "values": [20]}
        assert "dropped_unrepresentable_decorative_filter" in canonical.get("derivation_notes", [])

        # Verify QueryPlan validates cleanly
        plan = QueryPlan.model_validate(canonical)
        assert len(plan.filters) == 4

    def test_status_policy_custom_with_custom_sql_canonicalization(self, business):
        # 3. status_policy="custom" with metric="top_products" and non-empty sql
        # canonicalizes cleanly to metric="generic_sql" without INVALID_STATUS_POLICY.
        tbl = business.tables[0]
        raw = {
            "metric": "top_products",
            "status_policy": "custom",
            "sql": f'SELECT product_name FROM "{tbl.name}" GROUP BY product_id',
            "tables": [tbl.name],
            "rule_refs": [],
            "output": {"type": "string"},
        }
        canonical = canonicalize_plan_input(raw, business)
        assert canonical["metric"] == "generic_sql"
        assert canonical["status_policy"] == "custom"

        plan = QueryPlan.model_validate(canonical)
        rules = RuleRegistry("# Rules")
        q = Question(id="q_stat", business_id=business.business_id, question="Top sp", as_of_date=date(2026, 3, 1))
        errors = validate_plan(plan, business, rules, q)
        assert "INVALID_STATUS_POLICY" not in errors

    def test_singular_unit_price_typed_as_list_triggers_output_type_error(self, business):
        # 4. Singular unit-price question with output.type="list" triggers OUTPUT_TYPE_MUST_BE_NUMBER.
        plan = QueryPlan(
            metric="generic_sql",
            tables=[business.tables[0].name],
            rule_refs=[],
            sql=f'SELECT 1 FROM "{business.tables[0].name}"',
            output=OutputSpec(type=AnswerType.LIST, shape="list"),
        )
        q_singular_1 = Question(
            id="q_price_1",
            business_id=business.business_id,
            question="Đơn giá của sản phẩm này là bao nhiêu?",
            as_of_date=date(2026, 3, 1),
        )
        errors = semantic_plan_errors(plan, business, q_singular_1)
        assert any("OUTPUT_TYPE_MUST_BE_NUMBER" in e for e in errors)

        q_singular_2 = Question(
            id="q_price_2",
            business_id=business.business_id,
            question="Sản phẩm này có giá bao nhiêu một ly?",
            as_of_date=date(2026, 3, 1),
        )
        errors_2 = semantic_plan_errors(plan, business, q_singular_2)
        assert any("OUTPUT_TYPE_MUST_BE_NUMBER" in e for e in errors_2)

        # Plural/list markers prevent the error
        plural_samples = [
            "Liệt kê danh sách các đơn giá bao nhiêu",
            "Mỗi sản phẩm có đơn giá bao nhiêu?",
            "Từng mặt hàng có đơn giá là bao nhiêu?",
            "Các sản phẩm có đơn giá bao nhiêu?",
            "Những mặt hàng có đơn giá bao nhiêu?",
            "Top 5 đơn giá là bao nhiêu?",
            "Lần lượt đơn giá là bao nhiêu?",
            "Chẵn, lẻ đơn giá là bao nhiêu?",
        ]
        for idx, text in enumerate(plural_samples):
            q_plural = Question(
                id=f"q_plural_{idx}",
                business_id=business.business_id,
                question=text,
                as_of_date=date(2026, 3, 1),
            )
            errs = semantic_plan_errors(plan, business, q_plural)
            assert not any("OUTPUT_TYPE_MUST_BE_NUMBER" in e for e in errs), f"Failed on plural text: {text}"

    def test_customer_identity_grouping_error_includes_exact_column_name(self, business):
        # Verify CUSTOMER_IDENTITY_GROUPING_REQUIRED finds customer_id across plan.tables
        # and includes the exact column name in the error message.
        cust_tbl = business.tables[0]
        updated_cols = [
            *cust_tbl.columns,
            ColumnMeta(
                name="ma_dinh_danh_kh",
                original_name="Mã định danh KH",
                dtype="string",
                semantic_role="customer_id",
            ),
            ColumnMeta(
                name="customer_name",
                original_name="Tên khách hàng",
                dtype="string",
                semantic_role="customer_name",
            ),
        ]
        test_tbl = cust_tbl.model_copy(update={"columns": updated_cols})
        test_context = business.model_copy(
            update={"tables": [test_tbl if t.name == cust_tbl.name else t for t in business.tables]}
        )

        plan = QueryPlan(
            metric="generic_sql",
            tables=[test_tbl.name],
            rule_refs=[],
            sql=f'SELECT "customer_name" FROM "{test_tbl.name}" GROUP BY "customer_name"',
            group_by=["customer_name"],
            output=OutputSpec(type=AnswerType.TABLE),
        )
        q = Question(id="q_cust", business_id=business.business_id, question="Khách hàng", as_of_date=date(2026, 3, 1))
        errors = semantic_plan_errors(plan, test_context, q)
        assert any("CUSTOMER_IDENTITY_GROUPING_REQUIRED" in e and "ma_dinh_danh_kh" in e for e in errors)

    def test_triage_cancellation_rate_and_single_unit_top_products(self, business, rules):
        # 5. triage.py routes "thang 8/2026 ti le don bi huy la bn %" even when the database
        # contains a customer named "Lê Văn Thắng", and routes "top 3 mon ban nhieu nhat thang 4/2025"
        # on a single-unit qty_sale catalog.
        guard = DeterministicSlotGuard()
        temporal = TemporalResolver()

        # Add customer named "Lê Văn Thắng" into business table columns
        cust_tbl = business.tables[0]
        new_cols = [
            *cust_tbl.columns,
            ColumnMeta(
                name="ten_khach_hang",
                original_name="Tên khách hàng",
                dtype="string",
                semantic_role="customer_name",
                examples=["Lê Văn Thắng", "Nguyễn Thị Mai"],
            ),
        ]
        updated_cust_tbl = cust_tbl.model_copy(update={"columns": new_cols})
        business_with_thang = business.model_copy(
            update={"tables": [updated_cust_tbl if t.name == cust_tbl.name else t for t in business.tables]}
        )

        q_cancel = Question(
            id="q_cancel_thang",
            business_id=business.business_id,
            question="thang 8/2026 ti le don bi huy la bn %",
            as_of_date=date(2026, 9, 1),
        )
        plan_cancel = guard.route(q_cancel, business_with_thang, rules, temporal)
        assert plan_cancel is not None
        assert plan_cancel.metric == "cancellation_rate"

        # Now test single-unit qty_sale catalog
        # Remove qty_base and conversion_factor from lines table
        line_tbl = next(t for t in business.tables if t.grain == "order_line")
        single_unit_cols = [
            c.model_copy(update={"semantic_role": None}) if c.semantic_role in {"qty_base", "conversion_factor"} else c
            for c in line_tbl.columns
        ]
        single_unit_tbl = line_tbl.model_copy(update={"columns": single_unit_cols})
        single_unit_context = business.model_copy(
            update={"tables": [single_unit_tbl if t.name == line_tbl.name else t for t in business.tables]}
        )

        q_top = Question(
            id="q_top_mon",
            business_id=business.business_id,
            question="top 3 mon ban nhieu nhat thang 4/2025",
            as_of_date=date(2025, 5, 1),
        )
        plan_top = guard.route(q_top, single_unit_context, rules, temporal)
        assert plan_top is not None
        assert plan_top.metric == "top_products"
        assert plan_top.limit == 3
        assert any(c.endswith("qty_sale") for c in plan_top.required_fields)

        # Verify compilation compiles with qty_sale and no error
        compiler = TemplateCompiler(rules)
        program = compiler.compile(plan_top, single_unit_context)
        assert "qty_sale" in program.sql
        assert program.parameters[-1] == 3

        # Test "top 3 ban nhieu nhat" and "top 3 ban nhieu" without "mon"
        q_top_no_mon = Question(
            id="q_top_no_mon",
            business_id=business.business_id,
            question="top 3 ban nhieu nhat thang 4/2025",
            as_of_date=date(2025, 5, 1),
        )
        plan_top_no_mon = guard.route(q_top_no_mon, single_unit_context, rules, temporal)
        assert plan_top_no_mon is not None
        assert plan_top_no_mon.metric == "top_products"
        assert plan_top_no_mon.limit == 3

        q_top_ban_nhieu = Question(
            id="q_top_ban_nhieu",
            business_id=business.business_id,
            question="top 3 ban nhieu thang 4/2025",
            as_of_date=date(2025, 5, 1),
        )
        plan_top_ban_nhieu = guard.route(q_top_ban_nhieu, single_unit_context, rules, temporal)
        assert plan_top_ban_nhieu is not None
        assert plan_top_ban_nhieu.metric == "top_products"
        assert plan_top_ban_nhieu.limit == 3

    def test_omitted_hash_table_recovery(self, business, rules):
        # Table name in response["tables"] without 8-hex hash recovers uniquely
        tbl = business.tables[0]
        from pathlib import PurePosixPath
        stem = PurePosixPath(tbl.sources[0].path).stem
        plan_dict = {
            "metric": "generic_sql",
            "tables": [stem],
            "sql": f"SELECT COUNT(*) FROM {stem} WHERE {stem}.order_id IS NOT NULL",
            "required_fields": [f"{stem}.order_id"],
            "group_by": [],
            "rule_refs": [next(iter(rules.by_id))],
            "output": {"type": "number"},
        }
        model = OfflineModel(SimpleNamespace(model_dump=lambda mode: plan_dict))
        planner = Planner(model=model, rules=rules, temporal=TemporalResolver(), settings=Settings())
        plan = planner.make_plan(
            Question(id="q_stem", business_id=business.business_id, question="test stem", as_of_date=date(2026, 3, 1)),
            business,
            deadline=time.monotonic() + 60.0,
        )
        assert tbl.name in plan.tables
        assert stem not in plan.tables
        assert tbl.name in plan.sql
        assert f"{tbl.name}.order_id" in plan.required_fields

    def test_normalized_column_matching_in_metadata_fields(self, business):
        # Test bare column and prefixed column with casing/accent difference
        tbl = business.tables[0]
        raw = {
            "metric": "generic_sql",
            "tables": [tbl.name],
            "required_fields": [f"{tbl.name}.ORDER_ID", "order_id"],
            "group_by": ["order_id"],
            "filters": [{"field": "order_id", "operator": "eq", "values": ["1"]}],
            "rule_refs": [],
            "output": {"type": "number"},
        }
        canonical = canonicalize_plan_input(raw, business)
        assert canonical["required_fields"] == [f"{tbl.name}.order_id", f"{tbl.name}.order_id"]
        assert canonical["group_by"] == [f"{tbl.name}.order_id"]
        assert canonical["filters"][0]["field"] == f"{tbl.name}.order_id"

    def test_customer_identity_grouping_no_cid_in_tables_zero_errors(self, business):
        # 1. Table with customer_name and NO customer_id anywhere in plan.tables produces
        # ZERO CUSTOMER_IDENTITY_GROUPING_REQUIRED errors when grouping by customer_name.
        tbl = TableMeta(
            name="customer_feedback",
            sources=[],
            columns=[
                ColumnMeta(
                    name="customer_name",
                    original_name="Tên khách hàng",
                    dtype="string",
                    semantic_role="customer_name",
                ),
                ColumnMeta(
                    name="feedback",
                    original_name="Ý kiến",
                    dtype="string",
                ),
            ],
            row_count=10,
            raw_row_count=10,
        )
        context = business.model_copy(update={"tables": [tbl]})
        plan = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[],
            sql=f'SELECT "customer_name", COUNT(*) FROM "{tbl.name}" GROUP BY "customer_name"',
            group_by=["customer_name"],
            output=OutputSpec(type=AnswerType.TABLE),
        )
        q = Question(
            id="q_feedback",
            business_id=business.business_id,
            question="Thống kê theo khách hàng",
            as_of_date=date(2026, 3, 1),
        )
        errors = semantic_plan_errors(plan, context, q)
        assert not any("CUSTOMER_IDENTITY_GROUPING_REQUIRED" in e for e in errors)

    def test_output_type_percent_and_sqlite_round_on_percent(self, business):
        # 2. "doanh thu thang 2/2025 hon hay kem th truoc bao nhiu phan tram vay" with output.type="number"
        # triggers OUTPUT_TYPE_MUST_BE_PERCENT, while AnswerType.PERCENT with
        # SELECT ROUND(SUM(a) * 100.0 / SUM(b), 2) produces ZERO SQLITE_PREMATURE_ROUND_OR_INT_DIV errors.
        tbl = business.tables[0]
        q_pct = Question(
            id="q_pct_comp",
            business_id=business.business_id,
            question="doanh thu thang 2/2025 hon hay kem th truoc bao nhiu phan tram vay",
            as_of_date=date(2025, 3, 1),
        )
        plan_num = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[],
            sql=f'SELECT 12.5 FROM "{tbl.name}"',
            output=OutputSpec(type=AnswerType.NUMBER),
        )
        errors_num = semantic_plan_errors(plan_num, business, q_pct)
        assert any("OUTPUT_TYPE_MUST_BE_PERCENT" in e for e in errors_num)

        plan_pct = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[],
            sql=f'SELECT ROUND(SUM(a) * 100.0 / SUM(b), 2) FROM "{tbl.name}"',
            output=OutputSpec(type=AnswerType.PERCENT, rounding="two_decimals"),
        )
        errors_pct = semantic_plan_errors(plan_pct, business, q_pct)
        assert not any("SQLITE_PREMATURE_ROUND_OR_INT_DIV" in e for e in errors_pct)

    def test_generic_sql_prunes_decorative_expression_metadata(self, business, rules):
        # 3. generic_sql with decorative expression metadata "group_by": ["month", "date(t.ordered_at)"]
        # and "filters": [{"field": "time(t.ordered_at)", "operator": "gte", "values": ["06:00"]}]
        # prunes the decorative expressions and passes validate_plan with 0 INVALID_FILTER_OR_GROUP_FIELD errors.
        tbl = business.tables[0]
        raw = {
            "metric": "generic_sql",
            "tables": [tbl.name],
            "sql": f'SELECT "order_id" FROM "{tbl.name}" WHERE time(ordered_at) >= \'06:00\' GROUP BY strftime(\'%Y-%m\', ordered_at)',
            "required_fields": [f"{tbl.name}.order_id"],
            "group_by": ["month", "date(t.ordered_at)"],
            "filters": [{"field": "time(t.ordered_at)", "operator": "gte", "values": ["06:00"]}],
            "rule_refs": [],
            "output": {"type": "table"},
        }
        canonical = canonicalize_plan_input(raw, business)
        assert canonical["group_by"] == []
        assert canonical["filters"] == []
        assert "pruned_decorative_expression_metadata" in canonical.get("derivation_notes", [])

        plan = QueryPlan.model_validate(canonical)
        q = Question(
            id="q_decor",
            business_id=business.business_id,
            question="Đơn theo tháng",
            as_of_date=date(2026, 3, 1),
        )
        errors = validate_plan(plan, business, rules, q)
        assert not any("INVALID_FILTER_OR_GROUP_FIELD" in e for e in errors)

    def test_bare_join_key_required_fields_expansion(self, business, rules):
        # 4. Bare join key "order_id" in required_fields when both orders and order_lines
        # are selected expands cleanly to ["orders.order_id", "order_lines.order_id"]
        # with 0 QUALIFY_REQUIRED_FIELD errors.
        tbl_orders = TableMeta(
            name="orders",
            sources=[],
            columns=[
                ColumnMeta(name="order_id", original_name="Mã đơn", dtype="string"),
                ColumnMeta(name="total", original_name="Tổng", dtype="integer"),
            ],
            grain="order",
            row_count=10,
            raw_row_count=10,
        )
        tbl_lines = TableMeta(
            name="order_lines",
            sources=[],
            columns=[
                ColumnMeta(name="order_id", original_name="Mã đơn", dtype="string"),
                ColumnMeta(name="line_id", original_name="Dòng", dtype="string"),
            ],
            grain="order_line",
            row_count=20,
            raw_row_count=20,
        )
        context = business.model_copy(update={"tables": [tbl_orders, tbl_lines]})
        raw = {
            "metric": "generic_sql",
            "tables": ["orders", "order_lines"],
            "sql": 'SELECT orders.order_id, order_lines.line_id FROM orders JOIN order_lines ON orders.order_id = order_lines.order_id',
            "required_fields": ["order_id"],
            "rule_refs": [],
            "output": {"type": "table"},
        }
        canonical = canonicalize_plan_input(raw, context)
        assert canonical["required_fields"] == ["orders.order_id", "order_lines.order_id"]

        plan = QueryPlan.model_validate(canonical)
        q = Question(
            id="q_join_key",
            business_id=business.business_id,
            question="Chi tiết đơn hàng",
            as_of_date=date(2026, 3, 1),
        )
        errors = validate_plan(plan, context, rules, q)
        assert not any("QUALIFY_REQUIRED_FIELD" in e for e in errors)

    def test_unsupported_claim_generic_and_fake_source_refs(self, business):
        # 5. unsupported claim citing source_refs: ["rb:1234", "catalog"] canonicalizes and
        # passes verify_unsupported, while source_refs: ["fake_missing.csv"] still returns verified=False.
        context = business.model_copy(update={"schema_complete": True})

        # Generic source_refs ["rb:1234", "catalog"]
        raw_generic = {
            "metric": "unsupported",
            "tables": [],
            "required_fields": [],
            "rule_refs": [],
            "output": {"type": "unanswerable"},
            "unsupported": {
                "kind": "missing_field",
                "required": ["customer_address"],
                "explanation": "Customer address is not recorded",
                "source_refs": ["rb:1234", "catalog"],
            },
        }
        canonical_generic = canonicalize_plan_input(raw_generic, context)
        plan_generic = QueryPlan.model_validate(canonical_generic)
        evidence_generic = verify_unsupported(plan_generic, context)
        assert evidence_generic.verified is True

        # Fake source_refs ["fake_missing.csv"]
        raw_fake = {
            "metric": "unsupported",
            "tables": [],
            "required_fields": [],
            "rule_refs": [],
            "output": {"type": "unanswerable"},
            "unsupported": {
                "kind": "missing_field",
                "required": ["customer_address"],
                "explanation": "Customer address is not recorded",
                "source_refs": ["fake_missing.csv"],
            },
        }
        canonical_fake = canonicalize_plan_input(raw_fake, context)
        plan_fake = QueryPlan.model_validate(canonical_fake)
        evidence_fake = verify_unsupported(plan_fake, context)
        assert evidence_fake.verified is False

    def test_standard_policy_with_unrepresentable_filter_switches_to_generic_sql(self, business):
        tbl = business.tables[0]
        raw = {
            "metric": "sales_revenue",
            "status_policy": "revenue_eligible_recorded_status",
            "sql": f'SELECT SUM(total_due) FROM "{tbl.name}" WHERE total_due > 1000',
            "tables": [tbl.name],
            "rule_refs": [],
            "filters": [
                {"field": f"{tbl.name}.total_due", "operator": "unrepresentable_regex_match", "values": ["^1000"]},
            ],
            "output": {"type": "number"},
        }
        canonical = canonicalize_plan_input(raw, business)
        assert canonical["metric"] == "generic_sql"
        assert canonical["status_policy"] == "custom"
        assert canonical["filters"] == []
        assert "dropped_unrepresentable_decorative_filter" in canonical.get("derivation_notes", [])




