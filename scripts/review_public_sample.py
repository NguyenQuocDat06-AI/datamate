"""Technical review/replay of the completed public sample, without reference labels."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from bizreason.contracts import (
    AnswerType,
    BusinessContext,
    Candidate,
    OutputSpec,
    Prediction,
    QueryPlan,
    Question,
)
from bizreason.execution import execute_sql
from bizreason.formatting import validate_answer
from bizreason.utils import WORKSPACE, atomic_json, digest, dumps, load_jsonl, writable_path
from bizreason.verification import verify_candidate


def main() -> None:
    root = WORKSPACE / "artifacts/public_50_trammay"
    manifest = json.loads((root / "run_manifest.json").read_text(encoding="utf-8"))
    status = json.loads((root / "status.json").read_text(encoding="utf-8"))
    output = Path(manifest["output"])
    if status["state"] != "complete" or status["completed"] != 50:
        raise ValueError("The 50-question run has not completed; review would be partial")
    raw = load_jsonl(output)
    expected = manifest["question_ids"]
    if len(raw) != 50 or {item["id"] for item in raw} != set(expected):
        raise ValueError("Output IDs do not match the selected 50 questions")
    questions = {
        item["id"]: Question.model_validate(item)
        for item in load_jsonl(WORKSPACE / "data/public_test/questions.jsonl")
    }
    context_cache = {}
    rows = []
    error_counts = Counter()
    for item in raw:
        prediction = Prediction.model_validate(item)
        question = questions[prediction.id]
        value_type = prediction.type not in {AnswerType.UNANSWERABLE, AnswerType.ABSTAIN}
        if value_type:
            validate_answer(prediction.answer, OutputSpec(type=prediction.type))
        trace = json.loads(
            (root / "traces" / (digest(prediction.id)[:24] + ".json")).read_text(encoding="utf-8")
        )
        errors = trace.get("errors", [])
        error_counts.update(error.split(":", 1)[0] for error in errors)
        replay_matches = None
        warnings = []
        if value_type:
            candidates = [
                Candidate.model_validate(candidate)
                for candidate in trace.get("candidates", [])
                if candidate.get("verified")
            ]
            selected = next(
                (candidate for candidate in candidates if candidate.program.backend == "template"),
                candidates[0] if candidates else None,
            )
            if selected is None:
                warnings.append("No verified candidate for a value output")
            else:
                plans = [QueryPlan.model_validate(plan) for plan in trace["plans"]]
                plan = next(
                    (plan for plan in plans if digest(plan) == selected.plan_hash), plans[-1]
                )
                identity = question.business_id
                if identity not in context_cache:
                    revision = manifest["business_revisions"][identity]
                    path = WORKSPACE / "artifacts/cache" / identity / revision[:20] / "catalog.json"
                    context_cache[identity] = BusinessContext.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                context = context_cache[identity]
                execution = execute_sql(
                    context.database,
                    selected.program.sql,
                    selected.program.parameters,
                    plan.tables,
                    timeout=20,
                )
                replay = verify_candidate(
                    selected.model_copy(update={"execution": execution, "verified": False}), plan
                )
                replay_matches = replay.verified and dumps(replay.value) == dumps(prediction.answer)
                if not replay_matches:
                    warnings.append("Saved program replay did not reproduce the serialized value")
                if (
                    "CURRENT_DATE" in selected.program.sql.upper()
                    or "CURRENT_TIMESTAMP" in selected.program.sql.upper()
                ):
                    warnings.append("Review use of system clock instead of as_of_date")
        if prediction.type == AnswerType.UNANSWERABLE and not trace.get("unsupported_verified"):
            warnings.append("Unanswerable evidence is not marked verified")
        rows.append(
            {
                "id": prediction.id,
                "business_id": question.business_id,
                "question": question.question,
                "as_of_date": question.as_of_date.isoformat(),
                "type": prediction.type.value,
                "answer": prediction.answer,
                "format_valid": True,
                "replay_matches": replay_matches,
                "warnings": warnings,
                "pipeline_errors": errors,
            }
        )
    result = {
        "questions": len(rows),
        "businesses": len({row["business_id"] for row in rows}),
        "answer_types": dict(Counter(row["type"] for row in rows)),
        "format_valid": sum(row["format_valid"] for row in rows),
        "value_answers_reproduced": sum(row["replay_matches"] is True for row in rows),
        "replay_mismatches": sum(row["replay_matches"] is False for row in rows),
        "pipeline_error_counts": dict(error_counts),
        "ground_truth_available": False,
        "pa": None,
        "note": "Format and execution reproducibility checks do not establish business-semantic correctness.",
        "details": rows,
    }
    atomic_json(root / "technical_review.json", result)
    lines = [
        "# Kiểm tra kỹ thuật 50 câu public test",
        "",
        "Không có reference answers. Báo cáo này kiểm tra định dạng và tái thực thi SQL, không chấm đúng/sai hoặc PA.",
        "",
        f"- Đã xử lý: {len(rows)} câu / {result['businesses']} doanh nghiệp.",
        f"- Đúng định dạng: {result['format_valid']}/50.",
        f"- Cơ cấu type: `{dumps(result['answer_types'])}`.",
        f"- Đáp án giá trị tái thực thi khớp: {result['value_answers_reproduced']}; không khớp: {result['replay_mismatches']}.",
        "",
        "| ID | Doanh nghiệp | Câu hỏi | Type | Giá trị | Replay | Ghi chú |",
        "|---|---|---|---|---|---|---|",
    ]

    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    for row in rows:
        value = dumps(row["answer"])
        if len(value) > 160:
            value = value[:157] + "..."
        replay = (
            "khớp"
            if row["replay_matches"] is True
            else "không khớp" if row["replay_matches"] is False else "—"
        )
        lines.append(
            "| "
            + " | ".join(
                cell(str(value))
                for value in [
                    row["id"],
                    row["business_id"],
                    row["question"],
                    row["type"],
                    value,
                    replay,
                    "; ".join(row["warnings"]),
                ]
            )
            + " |"
        )
    target = writable_path(WORKSPACE / "docs/PUBLIC_TEST_50_REVIEW.md")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print({key: value for key, value in result.items() if key != "details"})


if __name__ == "__main__":
    main()
