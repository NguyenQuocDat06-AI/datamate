"""Check fresh predictions, final cache equivalence, and immutable baseline inputs."""

from __future__ import annotations

import json
import re
from pathlib import Path

from bizreason.contracts import BusinessContext, Candidate, QueryPlan, Question
from bizreason.execution import execute_sql
from bizreason.planning import validate_plan
from bizreason.rules import RuleRegistry
from bizreason.utils import atomic_json, digest, file_hash
from bizreason.verification import verify_candidate

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/generalized_fixes"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    live = OUT / "live3_v2"
    manifest, status = read(live / "run_manifest.json"), read(live / "status.json")
    assert status["state"] == "complete" and status["completed"] == 3
    current = {str(p.relative_to(ROOT)): file_hash(p) for p in (ROOT / "bizreason").rglob("*.py")}
    differences = sorted(k for k, value in current.items() if manifest["implementation"].get(k) != value)
    assert set(differences) <= {r"bizreason\data\ingestion.py", r"bizreason\data\ledger_schema.py"}
    equivalence = read(OUT / "schema_guard_equivalence.json")
    assert all(r["catalog_equal_excluding_revision_path"] and r["database_bytes_identical"] for r in equivalence)
    records = read(OUT / "public_ingestion.json")["businesses"]
    revisions = {r["business_id"]: r["revision"] for r in records}
    rules = RuleRegistry.load(ROOT / "data/public_test/rulebook.md")
    predictions = {p["id"]: p for p in map(json.loads,
        (ROOT / "result/public_generalized_fixes_live3_v2.jsonl").read_text(encoding="utf-8").splitlines())}
    checked = []
    for identity in manifest["question_ids"]:
        trace = read(live / "traces" / (digest(identity)[:24] + ".json"))
        question = Question.model_validate(trace["question"])
        revision = revisions[question.business_id]
        context = BusinessContext.model_validate_json((ROOT / "artifacts/cache" / question.business_id / revision[:20] / "catalog.json").read_text(encoding="utf-8"))
        candidate = next(Candidate.model_validate(c) for c in trace["candidates"] if c["verified"])
        plan = next(QueryPlan.model_validate(p) for p in trace["plans"] if digest(QueryPlan.model_validate(p)) == candidate.plan_hash)
        assert not validate_plan(plan, context, rules, question)
        execution = execute_sql(context.database, candidate.program.sql, candidate.program.parameters, plan.tables)
        replay = verify_candidate(candidate.model_copy(update={"execution": execution,
            "source_revision": context.revision, "verified": False}), plan)
        assert replay.verified and replay.value == predictions[identity]["answer"]
        checked.append({"id": identity, "prediction": predictions[identity],
            "replay_on_final_cache": replay.value, "source_revision": context.revision,
            "validation_errors": [], "execution_status": execution.status.value})
    # References enter only after model inference and execution are complete.
    references = read(ROOT / "artifacts/public_last20_review/raw_audit.json")
    for row in checked:
        reference = references[row["id"]]
        assert row["prediction"]["type"] == reference["expected_type"]
        assert row["prediction"]["answer"] == reference["expected"]
        row["independent_audit_match"] = True
    frozen = read(ROOT / "artifacts/public_last20_trammay_extended/completion_checks.json")
    assert file_hash(ROOT / "result/public_test_last20_trammay.jsonl") == frozen["last20_sha256"]
    assert file_hash(ROOT / "result/public_test_50_collected.jsonl") == frozen["combined50_sha256"]
    sources = read(ROOT / "artifacts/public_last20_review/source_hashes_before.json")
    assert all(file_hash(ROOT / name) == value for name, value in sources.items())
    pattern = re.compile(r"q_0594583b3d46|q_07e2508ad671|q_0a9819711554|400700000|Lê Sơn Trung|Le Son Trung|SH Mode")
    assert not any(pattern.search(p.read_text(encoding="utf-8")) for p in (ROOT / "bizreason").rglob("*.py"))
    result = {"fresh_api_predictions": 3, "matches_independent_audit": 3,
        "replayed_on_final_cache": 3, "api_calls_in_this_verifier": 0,
        "post_api_runtime_changes": differences,
        "post_api_change_note": "Only added a conservative README-evidence guard to ingestion. Both affected SQLite files are byte-identical and catalogs identical except revision/path; model-generated SQL re-executed on final caches.",
        "baseline_predictions_unchanged": True, "source_files_unchanged": len(sources),
        "production_case_id_name_answer_scan_passed": True,
        "implementation_sha256": current, "details": checked, "official_gold_available": False, "pa": None}
    atomic_json(OUT / "final_verification.json", result)
    print(json.dumps({k: v for k, v in result.items() if k not in {"implementation_sha256", "details"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
