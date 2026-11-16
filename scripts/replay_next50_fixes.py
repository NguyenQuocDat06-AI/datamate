"""Regression replay of frozen plans; no reference values enter the solver."""

from __future__ import annotations

import json
from pathlib import Path

from bizreason.config import Settings
from bizreason.contracts import BusinessContext, Question
from bizreason.pipeline import Solver
from bizreason.rules import RuleRegistry
from bizreason.temporal import LunarCalendar, TemporalResolver
from bizreason.utils import atomic_json, digest
from scripts.validate_generalized_fixes import SavedPlan

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/next50_fixes/replay50"


def main():
    old = ROOT / "artifacts/public_next50_16w"
    questions = json.loads((old / "selection.json").read_text(encoding="utf-8"))["questions"]
    ingestion = json.loads((ROOT / "artifacts/next50_fixes/public_ingestion.json").read_text(encoding="utf-8"))
    contexts = {}
    for record in ingestion["businesses"]:
        bid, revision = record["business_id"], record["revision"]
        contexts[bid] = BusinessContext.model_validate_json((ROOT / "artifacts/cache" / bid / revision[:20] / "catalog.json").read_text(encoding="utf-8"))
    rules = RuleRegistry.load(ROOT / "data/public_test/rulebook.md")
    temporal = TemporalResolver(LunarCalendar(ROOT / "data/public_test/calendar.csv"))
    results = []
    for raw in questions:
        question = Question.model_validate({k: raw[k] for k in ("id", "business_id", "question", "as_of_date")})
        filename = digest(question.id)[:24] + ".json"
        saved = json.loads((old / "traces" / filename).read_text(encoding="utf-8"))
        model = SavedPlan(saved["plans"][-1])
        prediction, trace = Solver(model, rules, temporal,
            Settings(max_model_calls=1, max_probe_calls=0, max_repairs=0)).solve(question, contexts[question.business_id])
        atomic_json(OUT / "traces" / filename, trace)
        results.append({"id": question.id, "prediction": prediction.model_dump(mode="json", exclude_none=True)})
    atomic_json(OUT / "predictions.json", results)
    # Evaluation reads references only after all inference/execution has finished.
    audit = json.loads((ROOT / "artifacts/public_next50_16w_review/review.json").read_text(encoding="utf-8"))
    references = {r["question"]["id"]: r for r in audit["details"]}
    unchanged, outcomes = [], []
    for row in results:
        reference = references[row["id"]]
        p = row["prediction"]
        if reference["status"] == "match":
            unchanged.append(p["type"] == reference["prediction"]["type"] and p.get("answer") == reference["prediction"].get("answer"))
        outcomes.append({**row, "audit_match": p["type"] == reference["expected_type"] and p.get("answer") == reference["expected"]})
    report = {"mode": "saved_plan_replay", "new_llm_inference": False, "api_calls": 0,
        "previously_correct": len(unchanged), "previously_correct_unchanged": sum(unchanged),
        "audit_matches": sum(r["audit_match"] for r in outcomes), "total": len(results), "details": outcomes}
    atomic_json(OUT / "summary.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "details"}))


if __name__ == "__main__":
    main()
