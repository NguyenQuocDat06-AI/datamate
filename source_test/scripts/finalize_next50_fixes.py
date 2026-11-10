"""Compare fresh predictions after inference and verify experiment provenance."""

from __future__ import annotations

import json
import re
from pathlib import Path
from xml.etree import ElementTree

from bizreason.contracts import AnswerType, OutputSpec, Prediction
from bizreason.formatting import validate_answer
from bizreason.utils import atomic_json, digest, file_hash

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "artifacts/next50_fixes"


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main():
    live = EXPERIMENT / "live4"
    status = read(live / "status.json")
    manifest = read(live / "run_manifest.json")
    assert status["state"] == "complete"
    predictions_path = ROOT / "result/public_next50_fixes_live4.jsonl"
    predictions = [json.loads(line) for line in predictions_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(predictions) == len({p["id"] for p in predictions}) == status["completed"] == status["total"]
    assert {p["id"] for p in predictions} == set(manifest["question_ids"])

    # All reference reads belong to the evaluator, after fresh inference ended.
    review = read(ROOT / "artifacts/public_next50_16w_review/review.json")
    references = {row["question"]["id"]: row for row in review["details"]}
    selected = {identity for identity, row in references.items()
                if row["status"] != "match" and row["expected_type"] != "unanswerable"}
    assert set(manifest["question_ids"]) == selected
    results = []
    for raw in predictions:
        prediction = Prediction.model_validate(raw)
        if prediction.type not in {AnswerType.ABSTAIN, AnswerType.UNANSWERABLE}:
            validate_answer(prediction.answer, OutputSpec(type=prediction.type))
        filename = digest(prediction.id)[:24] + ".json"
        assert read(live / "answers" / filename) == raw
        trace = read(live / "traces" / filename)
        assert trace["question"]["id"] == prediction.id
        assert trace["source_revision"] == manifest["business_revisions"][trace["question"]["business_id"]]
        if prediction.type not in {AnswerType.ABSTAIN, AnswerType.UNANSWERABLE}:
            assert any(c["verified"] and c["value"] == prediction.answer for c in trace["candidates"])
        reference = references[prediction.id]
        results.append({"id": prediction.id, "question": reference["question"],
                        "before": reference["prediction"], "after": raw,
                        "expected_type": reference["expected_type"], "expected": reference["expected"],
                        "audit_match": raw["type"] == reference["expected_type"] and raw.get("answer") == reference["expected"]})

    current = {str(p.relative_to(ROOT)): file_hash(p) for p in (ROOT / "bizreason").rglob("*.py")}
    assert current == manifest["implementation"], "Runtime changed during fresh inference"
    assert file_hash(ROOT / "scripts/run_public_test.py") == manifest["runner_sha256"]
    sources = read(ROOT / "artifacts/public_next50_16w_review/source_hashes_before.json")
    mismatches = [name for name, expected in sources.items() if file_hash(ROOT / name) != expected]
    assert not mismatches
    frozen = read(ROOT / "artifacts/public_next50_16w/completion_checks.json")
    baseline_checks = {
        "result/public_test_next50_16w.jsonl": frozen["prediction_sha256"],
        "result/public_test_100_collected.jsonl": frozen["combined100_sha256"],
    }
    for name, expected in baseline_checks.items():
        assert file_hash(ROOT / name) == expected
    markers = [str(path.relative_to(ROOT)) for path in (ROOT / "bizreason").rglob("*.py")
               if re.search(r"q_[0-9a-f]{12}|dev_\d{3}", path.read_text(encoding="utf-8"))]
    assert not markers, "Case-specific markers in runtime"

    replay = read(EXPERIMENT / "replay50/summary.json")
    assert replay["api_calls"] == 0 and not replay["new_llm_inference"]
    assert replay["previously_correct"] == replay["previously_correct_unchanged"] == 45
    tests = ElementTree.parse(EXPERIMENT / "pytest_final.xml").getroot().findall("testsuite")
    test_counts = {key: sum(int(suite.get(key, "0")) for suite in tests)
                   for key in ("tests", "failures", "errors", "skipped")}
    assert test_counts["tests"] > 0 and test_counts["failures"] == test_counts["errors"] == 0
    ingestion = read(EXPERIMENT / "public_ingestion.json")
    assert ingestion["all_row_accounting_valid"]
    errors = [diagnostic for business in ingestion["businesses"] for diagnostic in business["diagnostics"]
              if diagnostic["severity"] == "error"]
    assert not errors
    report = {
        "fresh_api": {"completed": len(predictions), "audit_matches": sum(r["audit_match"] for r in results),
                      "workers": manifest["workers"], "model": manifest["settings"]["model"],
                      "model_calls_reported": status["model_calls"], "model_tokens_reported": status["model_tokens"],
                      "elapsed_seconds": status["elapsed_this_session_seconds"], "details": results},
        "saved_plan_replay": {key: value for key, value in replay.items() if key != "details"},
        "tests": test_counts, "runtime_matches_live_manifest": True, "runtime_case_markers": markers,
        "source_files_checked": len(sources), "source_hash_mismatches": mismatches,
        "historical_outputs_unchanged": baseline_checks,
        "fresh_predictions_sha256": file_hash(predictions_path),
        "cache": {key: value for key, value in ingestion.items() if key not in {"businesses", "note"}},
        "official_gold_available": False, "pa": None,
        "limitation": "Audit-derived expectations; fresh inference covers four previously failing cases. Replay of 50 stored plans is not 50 fresh model predictions or a holdout evaluation.",
    }
    atomic_json(EXPERIMENT / "final_verification.json", report)
    print(json.dumps({"fresh_matches": report["fresh_api"]["audit_matches"], "fresh_total": len(predictions),
                      "replay_matches": replay["audit_matches"], "replay_total": replay["total"],
                      "tests": test_counts, "source_files_checked": len(sources)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
