"""Offline review probes: counterexamples, not a new model benchmark or a fix."""

from __future__ import annotations

import json
import runpy
import struct
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from bizreason.config import Settings
from bizreason.contracts import Candidate, ExecutionResult, ExecutionStatus, OutputSpec, Program, QueryPlan, Question
from bizreason.data.notes_facets import classify_note_status
from bizreason.decision import DecisionGate, UnsupportedEvidence
from bizreason.execution import _SqliteWorkerPool, execute_sql, shutdown_sql_pool
from bizreason.model_ledger import count_safetensors_parameters, verify_model_ledger
from bizreason.pipeline import Solver
from bizreason.rules import RuleRegistry
from bizreason.semantic_checks import semantic_plan_errors
from bizreason.temporal import TemporalResolver
from bizreason.triage import DeterministicSlotGuard, LLMSemanticCritic
from bizreason.utils import WORKSPACE, atomic_json, digest, file_hash
from bizreason.verification import verify_candidate

OUT = WORKSPACE / "artifacts/pipeline_review_20261106"


class OfflineModel:
    """Return a fixed plan and critique without network/model inference."""

    def __init__(self, plan):
        self.plan = plan
        self.usage = SimpleNamespace(calls=0, tokens=0)

    def generate_json(self, *_args, **_kwargs):
        self.usage.calls += 1
        if self.usage.calls == 1:
            return self.plan.model_dump(mode="json")
        return {"verdict": "policy_violation", "score": 0.0, "reason": "Offline negative control"}


def model_entry(identity, parameters, **extra):
    return {"model_id": identity, "weights_revision": "fixture",
            "weights_sha256": identity * 64, "release_date": "2025-01-01",
            "licence": "fixture", "total_parameters_including_moe": parameters,
            "quantization": "none", "runtime": "fixture", "purpose": "review", **extra}


def candidate_for(plan, rows, columns):
    program = Program(backend="generated_sql", sql=plan.sql, parameters=[], policy_refs=plan.rule_refs)
    return Candidate(id="review", plan_hash=digest(plan), code_hash=digest(program), source_revision="fixture",
                     program=program, output=plan.output,
                     execution=ExecutionResult(status=ExecutionStatus.SUCCESS, rows=rows, columns=columns,
                                               accessed_tables=plan.tables))


def main():
    started = time.monotonic()
    OUT.mkdir(parents=True, exist_ok=True)
    source_paths = sorted((WORKSPACE / "bizreason").rglob("*.py"))
    code_before = {p.relative_to(WORKSPACE).as_posix(): file_hash(p) for p in source_paths}
    fixtures = runpy.run_path(str(WORKSPACE / "tests/conftest.py"))
    rules = RuleRegistry(fixtures["RULEBOOK"])
    report = {"scope": "offline_fixture_counterexamples", "api_calls": 0, "neural_inference": False}
    with tempfile.TemporaryDirectory(dir=OUT) as temporary:
        context = fixtures["business"].__wrapped__(Path(temporary), rules)
        lines = next(t for t in context.tables if t.grain == "order_line")
        payments = next(t for t in context.tables if t.grain == "payment_event")
        temporal = TemporalResolver()
        report["triage"] = []
        for text in ["Doanh thu thang 3/2026", "Doanh thu online thang 3/2026",
                     "Doanh thu truoc 10 gio hom qua", "Top 5 san pham ban chay thang 3/2026"]:
            question = Question(id="review", business_id=context.business_id, question=text, as_of_date="2026-03-05")
            plan = DeterministicSlotGuard().route(question, context, rules, temporal)
            report["triage"].append({"question": text, "routed": plan is not None,
                                     "plan": plan.model_dump(mode="json") if plan else None})

        report["rounding"] = []
        for value in [123.45, 123.456]:
            plan = QueryPlan(metric="generic_sql", tables=[lines.name], rule_refs=rules.required_refs("sales_revenue"),
                             required_fields=[f"{lines.name}.line_amount"], sql=f'SELECT AVG(line_amount) FROM "{lines.name}"',
                             output=OutputSpec(type="number"))
            result = verify_candidate(candidate_for(plan, [[value]], ["average"]), plan)
            report["rounding"].append({"input": value, "verified": result.verified, "value": result.value,
                                        "rounding": result.output.rounding, "checks": result.checks})
        mixed = QueryPlan(metric="generic_sql", tables=[lines.name], rule_refs=rules.required_refs("sales_revenue"),
                          sql=f'SELECT product_name,COUNT(*),SUM(qty_sale) FROM "{lines.name}" GROUP BY product_name',
                          output=OutputSpec(type="table", columns=["product", "orders", "kg"],
                                            column_types=["string", "number", "number"]))
        result = verify_candidate(candidate_for(mixed, [["A", 1, 2.5]], ["product", "orders", "kg"]), mixed)
        report["mixed_count_and_fractional_sum"] = {"verified": result.verified, "checks": result.checks}

        raw_join = (f'SELECT SUM(p.paid_amount) FROM "{lines.name}" l JOIN "{payments.name}" p '
                    'ON l.order_id=p.order_id')
        report["fanout"] = []
        for sql in [raw_join, "WITH unrelated AS (SELECT 1) " + raw_join]:
            plan = QueryPlan(metric="generic_sql", tables=[lines.name, payments.name],
                             rule_refs=rules.required_refs("sales_revenue"), sql=sql, output=OutputSpec(type="number"))
            question = Question(id="review", business_id=context.business_id, question="Tong tien da thanh toan", as_of_date="2026-03-05")
            result = execute_sql(context.database, sql, [], plan.tables)
            report["fanout"].append({"sql": sql, "semantic_errors": semantic_plan_errors(plan, context, question),
                                      "result": result.rows})
        report["fanout_correct_payment_sum"] = execute_sql(
            context.database, f'SELECT SUM(paid_amount) FROM "{payments.name}"', [], [payments.name]).rows

        plan = QueryPlan(metric="generic_sql", tables=[lines.name], rule_refs=rules.required_refs("sales_revenue"),
                         sql=f'SELECT SUM(line_amount) FROM "{lines.name}"', output=OutputSpec(type="number", rounding="money"))
        candidate = verify_candidate(candidate_for(plan, [[1]], ["total"]), plan)
        evidence = UnsupportedEvidence(False, "fixture")
        report["semantic_score_gate"] = {
            str(score): DecisionGate().choose(candidate, evidence, semantic_score=score).model_dump(mode="json")
            for score in [0.0, 1.0]
        }
        model = OfflineModel(plan)
        question = Question(id="review", business_id=context.business_id, question="Tong tien", as_of_date="2026-03-05")
        prediction, trace = Solver(model, rules, temporal, Settings(max_model_calls=1, max_probe_calls=0),
                                   semantic_critic=LLMSemanticCritic(model)).solve(question, context)
        report["critic_budget"] = {"configured_max_model_calls": 1, "actual_mock_calls": model.usage.calls,
                                    "trace_model_calls": trace.model_calls, "prediction_type": prediction.type.value}

        base = model_entry("a", 8_000_000_000)
        adapter = model_entry("b", 8_100_000_000, shared_base_sha256=base["weights_sha256"],
                              base_parameters=8_000_000_000, adapter_parameters=100_000_000)
        report["ledger_order"] = {
            "base_then_adapter": verify_model_ledger({"offline_models": [base, adapter]}),
            "adapter_then_base": verify_model_ledger({"offline_models": [adapter, base]}),
        }
        inconsistent = model_entry("c", 20_000_000_000, shared_base_sha256="d" * 64,
                                   base_parameters=0, adapter_parameters=1)
        report["ledger_inconsistent_decomposition"] = verify_model_ledger({"offline_models": [inconsistent]})
        packed = Path(temporary) / "packed.safetensors"
        header = {
            "layer.qweight": {"dtype": "I32", "shape": [8, 8], "data_offsets": [0, 256]},
            "layer.qzeros": {"dtype": "I32", "shape": [1, 8], "data_offsets": [256, 288]},
            "layer.scales": {"dtype": "F16", "shape": [1, 64], "data_offsets": [288, 416]},
        }
        encoded = json.dumps(header).encode("utf-8")
        encoded += b" " * (-len(encoded) % 8)
        packed.write_bytes(struct.pack("<Q", len(encoded)) + encoded + b"\0" * 416)
        report["packed_tensor_count"] = {"stored_tensor_elements": count_safetensors_parameters(packed),
                                           "logical_weight_matrix_parameters": 8 * 64,
                                           "note": "Synthetic AWQ-shaped packing fixture; no real weights loaded."}

        report["notes"] = {text: classify_note_status(text) for text in [
            "khach tra no", "khach mang tra", "khong huy", "da huy", "tra hang"]}
        pool = _SqliteWorkerPool(max_workers=1)
        leased = []
        try:
            leased.append(pool.lease_worker())
            leased.append(pool.lease_worker())
            report["pool_limit"] = {"configured_max_workers": 1,
                                      "simultaneously_leased_live_workers": sum(w.is_alive() for w in leased)}
        finally:
            for worker in leased:
                pool.discard_worker(worker)
            pool.shutdown()
    shutdown_sql_pool()
    report["production_code_unchanged"] = code_before == {
        p.relative_to(WORKSPACE).as_posix(): file_hash(p) for p in source_paths}
    report["elapsed_seconds"] = time.monotonic() - started
    report["script_sha256"] = file_hash(Path(__file__))
    atomic_json(OUT / "code_snapshot.json", code_before)
    atomic_json(OUT / "probes.json", report)
    print(json.dumps({"output": str(OUT / "probes.json"), "production_code_unchanged": report["production_code_unchanged"],
                      "elapsed_seconds": report["elapsed_seconds"]}))


if __name__ == "__main__":
    main()
