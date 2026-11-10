"""Build real planner contexts locally for all businesses, without any API calls."""

from __future__ import annotations

import json
import time

from bizreason.config import Settings
from bizreason.contracts import BusinessContext, Question
from bizreason.llm import ModelError, Usage
from bizreason.planning import Planner
from bizreason.rules import RuleRegistry
from bizreason.temporal import LunarCalendar, TemporalResolver
from bizreason.utils import WORKSPACE, atomic_json, load_jsonl


class CaptureModel:
    def __init__(self) -> None:
        self.usage = Usage()
        self.characters = 0

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict:
        self.characters = len(system) + len(prompt)
        self.usage.calls += 1
        # A placeholder contract for a context-only check; it is never executed.
        return {
            "metric": "generic_sql",
            "tables": [],
            "rule_refs": [],
            "output": {"type": "number"},
        }


def main() -> None:
    report = json.loads(
        (WORKSPACE / "artifacts/ingestion_validation_complete.json").read_text(encoding="utf-8")
    )
    data = WORKSPACE / "data/train"
    questions = load_jsonl(data / "questions.jsonl")
    rules = RuleRegistry.load(data / "rulebook.md")
    temporal = TemporalResolver(LunarCalendar(data / "calendar.csv"))
    results = []
    for record in report["businesses"]:
        catalog = (
            WORKSPACE
            / "artifacts/cache"
            / record["business_id"]
            / record["revision"][:20]
            / "catalog.json"
        )
        context = BusinessContext.model_validate_json(catalog.read_text(encoding="utf-8"))
        question = Question.model_validate(
            next(item for item in questions if item["business_id"] == context.business_id)
        )
        model = CaptureModel()
        error = None
        try:
            Planner(model, rules, temporal, Settings()).make_plan(
                question, context, time.monotonic() + 30
            )
        except ModelError as exception:
            error = str(exception)
        results.append(
            {
                "business_id": context.business_id,
                "prompt_characters": model.characters,
                "fits": error is None,
                "error": error,
            }
        )
    result = {
        "businesses": results,
        "all_fit": all(item["fits"] for item in results),
        "max_prompt_characters": max(item["prompt_characters"] for item in results),
        "budget_characters": Settings().context_characters,
        "external_api_calls": 0,
        "note": "One real question context per business. This checks construction/budget, not answer quality or every possible question.",
    }
    atomic_json(WORKSPACE / "artifacts/context_budget_validation.json", result)
    print(
        {
            key: result[key]
            for key in [
                "all_fit",
                "max_prompt_characters",
                "budget_characters",
                "external_api_calls",
            ]
        }
    )


if __name__ == "__main__":
    main()
