"""Replay saved plans on normalized public caches; this is not new inference."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from bizreason.compilation import TemplateCompiler
from bizreason.contracts import BusinessContext, Candidate, QueryPlan, Question
from bizreason.decision import verify_unsupported
from bizreason.execution import execute_sql
from bizreason.planning import validate_plan
from bizreason.rules import RuleRegistry
from bizreason.utils import atomic_json, digest, dumps
from bizreason.verification import verify_candidate

ROOT = Path(__file__).resolve().parents[1]


def main():
    ingestion = json.loads((ROOT / "artifacts/normalization_v2/public_ingestion_final.json").read_text(encoding="utf-8"))
    contexts = {}
    for record in ingestion["businesses"]:
        path = ROOT / "artifacts/cache" / record["business_id"] / record["revision"][:20] / "catalog.json"
        contexts[record["business_id"]] = BusinessContext.model_validate_json(path.read_text(encoding="utf-8"))
    first = json.loads((ROOT / "artifacts/public_first10/review.json").read_text(encoding="utf-8"))["details"]
    following = json.loads((ROOT / "artifacts/public_next20/review.json").read_text(encoding="utf-8"))["details"]
    records = [(r["question"], r["trace"], r["independent_type"], r["independent_answer"]) for r in first]
    records += [(r["question"], r["trace"], r["prediction"]["type"] if r["prediction"]["type"] != "abstain" else "number", r["independent_expected"]) for r in following]
    rules = RuleRegistry.load(ROOT / "data/public_test/rulebook.md")
    compiler = TemplateCompiler(rules)
    rows = []
    for raw_question, trace_path, expected_type, expected in records:
        trace = json.loads((ROOT / trace_path).read_text(encoding="utf-8"))
        question = Question.model_validate(raw_question)
        context = contexts[question.business_id]
        plan = QueryPlan.model_validate(trace["plans"][-1])
        violations = validate_plan(plan, context, rules, question)
        value, kind, backend, diagnostics = None, None, None, violations
        if not violations and plan.metric == "unsupported":
            claim = verify_unsupported(plan, context)
            kind = "unanswerable" if claim.verified else None
            diagnostics = [claim.reason]
        elif not violations:
            program = compiler.compile(plan, context)
            backend = program.backend
            execution = execute_sql(context.database, program.sql, program.parameters, plan.tables, timeout=30)
            candidate = verify_candidate(Candidate(
                id="normalization-replay", plan_hash=digest(plan), code_hash=digest(program),
                source_revision=context.revision, program=program, execution=execution, output=plan.output,
            ), plan)
            if candidate.verified:
                value, kind = candidate.value, plan.output.type.value
            diagnostics = [*execution.diagnostics, *candidate.checks]
        matches = kind == expected_type and dumps(value) == dumps(expected)
        if isinstance(value, str) and isinstance(expected, str):
            matches = kind == expected_type and value.casefold().strip() == expected.casefold().strip()
        rows.append({"id": question.id, "business_id": question.business_id,
                     "source_revision": context.revision, "expected_type": expected_type,
                     "independent_expected": expected, "replay_type": kind, "replay_value": value,
                     "matches_independent_check": matches, "backend": backend, "diagnostics": diagnostics})
        print(question.id, "match" if matches else "needs_replanning", value, flush=True)
    atomic_json(ROOT / "artifacts/normalization_v2/frozen_plan_replay.json", {
        "api_calls": 0, "new_inference": False, "questions": len(rows),
        "matched": sum(row["matches_independent_check"] for row in rows),
        "requires_replanning_or_review": sum(not row["matches_independent_check"] for row in rows),
        "backends": dict(Counter(row["backend"] or "unsupported" for row in rows)), "details": rows,
        "note": "Saved SQL was written against old representations. A mismatch is not an accuracy label for newly generated plans.",
    })


if __name__ == "__main__":
    main()
