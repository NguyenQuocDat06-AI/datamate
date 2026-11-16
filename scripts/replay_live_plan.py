"""Replay the previously received API plan on current code/data, with no network."""

from __future__ import annotations

import json

from bizreason.config import Settings
from bizreason.contracts import BusinessContext, QuestionTrace
from bizreason.llm import Usage
from bizreason.pipeline import Solver
from bizreason.rules import RuleRegistry
from bizreason.temporal import LunarCalendar, TemporalResolver
from bizreason.utils import WORKSPACE, atomic_json, load_jsonl


class RecordedPlanModel:
    def __init__(self, plan: dict):
        self.plan = plan
        self.usage = Usage()

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict:
        self.usage.calls += 1
        return self.plan


def main() -> None:
    trace_path = next((WORKSPACE / "artifacts/live_verified/traces").glob("*.json"))
    recorded = QuestionTrace.model_validate_json(trace_path.read_text(encoding="utf-8"))
    report = json.loads(
        (WORKSPACE / "artifacts/ingestion_validation_complete.json").read_text(encoding="utf-8")
    )
    record = next(
        item
        for item in report["businesses"]
        if item["business_id"] == recorded.question.business_id
    )
    catalog = (
        WORKSPACE
        / "artifacts/cache"
        / record["business_id"]
        / record["revision"][:20]
        / "catalog.json"
    )
    context = BusinessContext.model_validate_json(catalog.read_text(encoding="utf-8"))
    rules = RuleRegistry.load(WORKSPACE / "data/train/rulebook.md")
    model = RecordedPlanModel(recorded.plans[-1].model_dump(mode="json"))
    solver = Solver(
        model,
        rules,
        TemporalResolver(LunarCalendar(WORKSPACE / "data/train/calendar.csv")),
        Settings(),
    )
    prediction, trace = solver.solve(recorded.question, context)
    # Load references only after inference, on the independent evaluation side.
    from bizreason.evaluation.scorer import score_records

    references = [
        item
        for item in load_jsonl(WORKSPACE / "data/train/answers.jsonl")
        if item["id"] == prediction.id
    ]
    score = score_records([prediction.model_dump(mode="json", exclude_none=True)], references)
    result = {
        "question_id": prediction.id,
        "prediction": prediction.model_dump(mode="json", exclude_none=True),
        "correct": score["correct"] == 1,
        "n": 1,
        "external_api_calls": 0,
        "source_revision": context.revision,
        "original_trace": str(trace_path.relative_to(WORKSPACE)),
        "errors": trace.errors,
        "trace": trace.model_dump(mode="json"),
        "note": "Re-executes an already observed API plan, not a new model prediction or holdout benchmark.",
    }
    atomic_json(WORKSPACE / "artifacts/live_replay.json", result)
    print(
        {
            key: result[key]
            for key in ["question_id", "correct", "n", "external_api_calls", "errors"]
        }
    )


if __name__ == "__main__":
    main()
