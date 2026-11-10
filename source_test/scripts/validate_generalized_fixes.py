"""Replay saved plans as regressions; read raw-audit references only afterwards."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from bizreason.config import Settings
from bizreason.contracts import BusinessContext, Question
from bizreason.llm import Usage
from bizreason.pipeline import Solver
from bizreason.rules import RuleRegistry
from bizreason.temporal import LunarCalendar, TemporalResolver
from bizreason.utils import atomic_json, digest

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/generalized_fixes/regression20"


class SavedPlan:
    def __init__(self, plan):
        self.plan = plan
        self.usage = Usage()

    def generate_json(self, system, prompt, *, timeout):
        self.usage.calls += 1
        return copy.deepcopy(self.plan)


def main():
    old = ROOT / "artifacts/public_last20_trammay_extended"
    selected = json.loads((old / "selection.json").read_text(encoding="utf-8"))["questions"]
    ingestion = json.loads((ROOT / "artifacts/generalized_fixes/public_ingestion.json").read_text(encoding="utf-8"))
    contexts = {}
    for record in ingestion["businesses"]:
        bid, revision = record["business_id"], record["revision"]
        contexts[bid] = BusinessContext.model_validate_json(
            (ROOT / "artifacts/cache" / bid / revision[:20] / "catalog.json").read_text(encoding="utf-8"))
    rules = RuleRegistry.load(ROOT / "data/public_test/rulebook.md")
    temporal = TemporalResolver(LunarCalendar(ROOT / "data/public_test/calendar.csv"))
    records = []
    for item in selected:
        question = Question.model_validate({k: item[k] for k in ("id", "business_id", "question", "as_of_date")})
        filename = digest(question.id)[:24] + ".json"
        trace = json.loads((old / "traces" / filename).read_text(encoding="utf-8"))
        model = SavedPlan(trace["plans"][-1])
        prediction, current = Solver(model, rules, temporal,
            Settings(max_model_calls=1, max_repairs=0, max_probe_calls=0)).solve(question, contexts[question.business_id])
        atomic_json(OUT / "traces" / filename, current)
        records.append({"id": question.id, "prediction": prediction.model_dump(mode="json", exclude_none=True)})
    atomic_json(OUT / "predictions.json", records)
    # Evaluation process boundary: references are not available to SavedPlan/Solver.
    old_review = json.loads((ROOT / "artifacts/public_last20_review/review.json").read_text(encoding="utf-8"))
    previously_correct = {r["question"]["id"]: r["prediction"] for r in old_review["details"] if r["status"] == "match"}
    same = []
    for item in records:
        if item["id"] in previously_correct:
            previous = previously_correct[item["id"]]
            same.append({"id": item["id"], "unchanged": item["prediction"].get("type") == previous["type"]
                         and item["prediction"].get("answer") == previous.get("answer")})
    result = {"mode": "saved_plan_regression", "new_llm_inference": False, "api_calls": 0,
        "previously_correct": len(same), "previously_correct_unchanged": sum(r["unchanged"] for r in same),
        "checks": same, "details": records}
    atomic_json(OUT / "summary.json", result)
    print(json.dumps({k: v for k, v in result.items() if k not in {"details", "checks"}}))


if __name__ == "__main__":
    main()
