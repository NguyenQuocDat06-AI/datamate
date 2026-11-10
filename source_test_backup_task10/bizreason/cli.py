from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bizreason import __version__
from bizreason.config import Settings
from bizreason.contracts import AnswerType, Decision, Prediction, Question, QuestionTrace
from bizreason.data.ingestion import BusinessBuilder
from bizreason.data.readers import unpack_dataset
from bizreason.decision import DecisionGate, candidate_features
from bizreason.formatting import SubmissionWriter
from bizreason.llm import ModelError, TrammayClient
from bizreason.pipeline import Solver
from bizreason.rules import RuleRegistry
from bizreason.streaming import StreamingTrammayClient
from bizreason.temporal import LunarCalendar, TemporalResolver
from bizreason.triage import LLMSemanticCritic, TriageRouter
try:
    from bizreason.model_ledger import inspect_safetensors_parameters
except ImportError:
    inspect_safetensors_parameters = None
from bizreason.utils import (
    WORKSPACE,
    PathPolicy,
    atomic_json,
    digest,
    file_hash,
    load_jsonl,
    writable_path,
)


def _read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _registry(data: Path) -> tuple[RuleRegistry, TemporalResolver]:
    rules = RuleRegistry.load(data / "rulebook.md")
    calendar_path = data / "calendar.csv"
    temporal = TemporalResolver(LunarCalendar(calendar_path) if calendar_path.exists() else None)
    return rules, temporal


def _builder(data: Path, cache: Path) -> tuple[BusinessBuilder, RuleRegistry, TemporalResolver]:
    rules, temporal = _registry(data)
    return (
        BusinessBuilder(
            cache,
            rule_hash=rules.revision,
            inheritance_allowed=rules.inheritance_allowed,
            rules=rules,
        ),
        rules,
        temporal,
    )


def compute_question_budget(
    remaining: float,
    questions_left: int,
    configured_timeout: float,
    *,
    safety_reserve_ratio: float = 0.02,
    burst_factor: float = 4.0,
    min_floor: float = 5.0,
) -> float:
    if remaining <= 0:
        return 0.0
    if questions_left <= 1:
        return max(0.0, min(configured_timeout, remaining))
    usable = max(0.0, remaining * (1.0 - safety_reserve_ratio))
    fair_share = usable / questions_left
    dynamic_cap = max(min(min_floor, remaining), burst_factor * fair_share)
    return max(0.0, min(configured_timeout, remaining, dynamic_cap))


def _run(args: argparse.Namespace) -> None:
    started = time.monotonic()
    data = Path(args.data).resolve()
    if not data.exists():
        for candidate in [
            WORKSPACE / "data" / "train",
            WORKSPACE / "data",
            WORKSPACE.parent / "train",
        ]:
            if candidate.exists() and (candidate / "data").exists():
                data = candidate.resolve()
                break
    settings = Settings.from_env(
        model=args.model,
        base_url=args.base_url,
        api_style=args.api_style,
        gate_mode=args.gate,
        max_repairs=args.max_repairs,
        question_timeout=args.question_timeout,
        sql_timeout=args.sql_timeout,
        max_model_calls=args.max_model_calls,
        request_timeout=args.request_timeout,
        max_output_tokens=args.max_output_tokens,
        context_characters=getattr(args, "context_characters", None),
        runtime_mode=getattr(args, "runtime_mode", None),
    )
    runtime_mode = getattr(args, "runtime_mode", None) or settings.runtime_mode
    output_dir = Path(args.output).resolve().parent
    artifacts_dir = Path(args.artifacts).resolve()
    cache_dir = Path(args.cache).resolve()

    policy = PathPolicy(
        mode=runtime_mode,
        allowed_roots=[output_dir, artifacts_dir, cache_dir],
        forbidden_roots=[data, Path("/data").resolve()],
    )
    with policy:
        output = writable_path(args.output)
        artifacts = writable_path(args.artifacts)
        artifacts.mkdir(parents=True, exist_ok=True)
        questions_path = Path(args.questions).resolve() if getattr(args, "questions", None) else (data / "questions.jsonl")
        questions = [Question.model_validate(item) for item in load_jsonl(questions_path)]
        if args.business:
            questions = [question for question in questions if question.business_id in args.business]
        if args.limit is not None:
            questions = questions[: args.limit]
        if not questions:
            raise ValueError("No questions selected")

        original_order_ids = [q.id for q in questions]

        if getattr(args, "group_by_business", False):
            business_order = []
            business_to_questions: dict[str, list[Question]] = {}
            for q in questions:
                if q.business_id not in business_to_questions:
                    business_order.append(q.business_id)
                    business_to_questions[q.business_id] = []
                business_to_questions[q.business_id].append(q)
            questions = [q for bid in business_order for q in business_to_questions[bid]]

        remaining_by_business: dict[str, int] = {}
        for q in questions:
            remaining_by_business[q.business_id] = remaining_by_business.get(q.business_id, 0) + 1

        if args.backend == "local":
            local_url = args.base_url or "http://127.0.0.1:8000/v1"
            if urlparse(local_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("Local mode only allows a loopback endpoint")
            settings = replace(settings, base_url=local_url, api_key="local")
        workers = getattr(args, "workers", 1) or 1
        if workers <= 1:
            if args.backend == "none":
                client = None
            elif args.backend == "api" and getattr(args, "stream", False):
                client = StreamingTrammayClient(settings)
            else:
                client = TrammayClient(settings)
        else:
            client = None

        builder, rules, temporal = _builder(data, Path(args.cache))
        gate = DecisionGate(args.gate, _read_json(args.calibration) if args.calibration else None)
        triage_router = TriageRouter() if getattr(args, "enable_fast_triage", False) else None
        semantic_critic = (
            LLMSemanticCritic(client)
            if getattr(args, "enable_semantic_critic", False) and client is not None
            else None
        )
        solver = Solver(
            client,
            rules,
            temporal,
            settings,
            gate,
            triage_router=triage_router,
            semantic_critic=semantic_critic,
        )
        atomic_json(
            artifacts / "run_manifest.json",
            {
                "version": __version__,
                "python_version": sys.version.split()[0],
                "implementation_sha256": digest(
                    {
                        path.relative_to(WORKSPACE).as_posix(): file_hash(path)
                        for path in sorted((WORKSPACE / "bizreason").rglob("*.py"))
                    }
                ),
                "backend": args.backend,
                "model": settings.model,
                "base_url": settings.base_url,
                "gate": args.gate,
                "stream": getattr(args, "stream", False),
                "enable_fast_triage": getattr(args, "enable_fast_triage", False),
                "enable_semantic_critic": getattr(args, "enable_semantic_critic", False),
                "group_by_business": getattr(args, "group_by_business", False),
                "fair_pacing": getattr(args, "fair_pacing", True),
                "workers": workers,
                "rule_hash": rules.revision,
                "questions_sha256": file_hash(questions_path),
                "selected_question_ids": original_order_ids,
                "limits": {
                    "total_seconds": args.total_seconds,
                    "question_seconds": settings.question_timeout,
                    "sql_seconds": settings.sql_timeout,
                    "max_model_calls": settings.max_model_calls,
                    "max_repairs": settings.max_repairs,
                },
                "development_remote_api": args.backend == "api",
                "runtime_mode": runtime_mode,
            },
        )
        contexts: dict[str, Any] = {}
        preparation_errors: dict[str, str] = {}
        context_lock = threading.Lock()

        def get_business_context(identity: str) -> Any:
            with context_lock:
                if identity in preparation_errors:
                    return None
                if identity in contexts:
                    return contexts[identity]
                business_dir = (data / "data" / identity).resolve()
                if not business_dir.is_relative_to((data / "data").resolve()):
                    raise ValueError("Business identifier escapes the input directory")
                try:
                    contexts[identity] = builder.prepare(business_dir)
                    return contexts[identity]
                except (ValueError, OSError) as error:
                    preparation_errors[identity] = type(error).__name__
                    return None

        def finish_business_question(identity: str) -> None:
            with context_lock:
                if identity in remaining_by_business:
                    remaining_by_business[identity] -= 1
                    if (
                        getattr(args, "group_by_business", False)
                        and remaining_by_business[identity] <= 0
                    ):
                        contexts.pop(identity, None)

        completed_predictions: dict[str, Prediction] = {}
        written_order: list[str] = []
        latencies: list[float] = []
        total_model_calls = 0
        total_model_tokens = 0
        counts: dict[str, int] = {}
        created_clients: list[Any] = []
        try:
            with SubmissionWriter(output) as writer:
                if workers > 1:
                    clients_lock = threading.Lock()
                    thread_local = threading.local()
                    pacer_lock = threading.Lock()
                    questions_remaining = len(questions)

                    def get_worker_solver() -> Solver:
                        if not hasattr(thread_local, "solver"):
                            if args.backend == "none":
                                worker_client = None
                            elif args.backend == "api" and getattr(args, "stream", False):
                                worker_client = StreamingTrammayClient(settings)
                            else:
                                worker_client = TrammayClient(settings)
                            if worker_client is not None:
                                with clients_lock:
                                    created_clients.append(worker_client)
                            worker_critic = (
                                LLMSemanticCritic(worker_client)
                                if getattr(args, "enable_semantic_critic", False) and worker_client is not None
                                else None
                            )
                            worker_gate = DecisionGate(
                                args.gate, _read_json(args.calibration) if args.calibration else None
                            )
                            worker_router = (
                                TriageRouter() if getattr(args, "enable_fast_triage", False) else None
                            )
                            thread_local.solver = Solver(
                                worker_client,
                                rules,
                                temporal,
                                settings,
                                worker_gate,
                                triage_router=worker_router,
                                semantic_critic=worker_critic,
                            )
                        return thread_local.solver

                    def process_question(idx: int, question: Question) -> tuple[int, Prediction, QuestionTrace]:
                        nonlocal questions_remaining
                        identity = question.business_id
                        try:
                            remaining = args.total_seconds - (time.monotonic() - started)
                            if remaining <= 0:
                                pred = Prediction(
                                    id=question.id,
                                    type=AnswerType.ABSTAIN,
                                    reason="Global runtime budget exhausted",
                                )
                                trace = QuestionTrace(
                                    question=question,
                                    source_revision=rules.revision,
                                    decision=Decision(action="abstain", reason=pred.reason, policy_version=rules.revision),
                                    errors=["Global runtime budget exhausted"],
                                    elapsed_ms=0,
                                    model_calls=0,
                                    model_tokens=0,
                                    finished_at=datetime.now(timezone.utc),
                                )
                                return idx, pred, trace

                            context = get_business_context(identity)
                            if identity in preparation_errors or context is None:
                                pred = Prediction(
                                    id=question.id,
                                    type=AnswerType.ABSTAIN,
                                    reason="Business preparation failed",
                                )
                                trace = QuestionTrace(
                                    question=question,
                                    source_revision=rules.revision,
                                    decision=Decision(action="abstain", reason=pred.reason, policy_version=rules.revision),
                                    errors=["Business preparation failed"],
                                    elapsed_ms=0,
                                    model_calls=0,
                                    model_tokens=0,
                                    finished_at=datetime.now(timezone.utc),
                                )
                                return idx, pred, trace

                            with pacer_lock:
                                questions_left = questions_remaining
                            if getattr(args, "fair_pacing", True):
                                q_timeout = compute_question_budget(
                                    remaining, max(1, questions_left), settings.question_timeout
                                )
                            else:
                                q_timeout = max(0.0, min(settings.question_timeout, remaining))

                            worker_solver = get_worker_solver()
                            worker_solver.settings = replace(settings, question_timeout=q_timeout)
                            prediction, trace = worker_solver.solve(question, context)
                            return idx, prediction, trace
                        finally:
                            with pacer_lock:
                                questions_remaining -= 1
                            finish_business_question(identity)

                    completed_count = 0
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        future_to_index = {
                            executor.submit(process_question, idx, q): idx
                            for idx, q in enumerate(questions)
                        }
                        for future in as_completed(future_to_index):
                            idx, prediction, trace = future.result()
                            completed_count += 1
                            writer.write(prediction)
                            written_order.append(prediction.id)
                            completed_predictions[prediction.id] = prediction
                            counts[prediction.type.value] = counts.get(prediction.type.value, 0) + 1
                            latencies.append(trace.elapsed_ms)
                            total_model_calls += trace.model_calls
                            total_model_tokens += trace.model_tokens
                            atomic_json(artifacts / "traces" / (digest(prediction.id)[:24] + ".json"), trace)
                            print(
                                f"[{completed_count}/{len(questions)}] {prediction.id}: {prediction.type.value} ({trace.elapsed_ms / 1000:.2f}s)",
                                flush=True,
                            )
                else:
                    for index, question in enumerate(questions, 1):
                        remaining = args.total_seconds - (time.monotonic() - started)
                        if remaining <= 0:
                            prediction = Prediction(
                                id=question.id,
                                type=AnswerType.ABSTAIN,
                                reason="Global runtime budget exhausted",
                            )
                            writer.write(prediction)
                            written_order.append(prediction.id)
                            completed_predictions[prediction.id] = prediction
                            counts["abstain"] = counts.get("abstain", 0) + 1
                            finish_business_question(question.business_id)
                            continue

                        identity = question.business_id
                        context = get_business_context(identity)
                        if identity in preparation_errors or context is None:
                            prediction = Prediction(
                                id=question.id,
                                type=AnswerType.ABSTAIN,
                                reason="Business preparation failed",
                            )
                            writer.write(prediction)
                            written_order.append(prediction.id)
                            completed_predictions[prediction.id] = prediction
                            counts["abstain"] = counts.get("abstain", 0) + 1
                            finish_business_question(identity)
                            continue

                        remaining = args.total_seconds - (time.monotonic() - started)
                        questions_left = len(questions) - index + 1
                        if getattr(args, "fair_pacing", True):
                            q_timeout = compute_question_budget(
                                remaining, questions_left, settings.question_timeout
                            )
                        else:
                            q_timeout = max(0.0, min(settings.question_timeout, remaining))
                        solver.settings = replace(settings, question_timeout=q_timeout)
                        prediction, trace = solver.solve(question, context)
                        writer.write(prediction)
                        written_order.append(prediction.id)
                        completed_predictions[prediction.id] = prediction
                        finish_business_question(identity)

                        atomic_json(artifacts / "traces" / (digest(question.id)[:24] + ".json"), trace)
                        latencies.append(trace.elapsed_ms)
                        total_model_calls += trace.model_calls
                        total_model_tokens += trace.model_tokens
                        counts[prediction.type.value] = counts.get(prediction.type.value, 0) + 1
                        print(
                            f"[{index}/{len(questions)}] {question.id}: {prediction.type.value} ({trace.elapsed_ms / 1000:.2f}s)",
                            flush=True,
                        )
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass
            for c in created_clients:
                try:
                    c.close()
                except Exception:
                    pass
            if (
                len(completed_predictions) > 1
                and written_order != [qid for qid in original_order_ids if qid in completed_predictions]
            ):
                try:
                    with SubmissionWriter(output) as final_writer:
                        for qid in original_order_ids:
                            if qid in completed_predictions:
                                final_writer.write(completed_predictions[qid])
                except Exception:
                    pass

        ordered = sorted(latencies)
        summary = {
            "questions": len(questions),
            "answer_types": counts,
            "elapsed_seconds": time.monotonic() - started,
            "model_calls": total_model_calls,
            "model_tokens": total_model_tokens,
            "p50_ms": statistics.median(ordered) if ordered else None,
            "p95_ms": (
                ordered[min(len(ordered) - 1, int((len(ordered) - 1) * 0.95))] if ordered else None
            ),
            "preparation_errors": preparation_errors,
            "output": str(output),
            "note": "Runtime summary only. PA requires independent evaluation against references.",
        }
        atomic_json(artifacts / "runtime_report.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))


def _calibration_records(args: argparse.Namespace) -> None:
    from bizreason.evaluation.scorer import matches

    references = {item["id"]: item for item in load_jsonl(Path(args.references))}
    manifest = _read_json(args.split_manifest)
    fit_businesses = set(manifest["splits"]["fit"]["businesses"])
    allowed_ids = set(manifest["splits"]["calibration"]["question_ids"])
    records = []
    for path in sorted(Path(args.traces).glob("*.json")):
        trace = QuestionTrace.model_validate_json(path.read_text(encoding="utf-8"))
        question = trace.question
        if question.id not in allowed_ids:
            continue
        if question.business_id in fit_businesses:
            raise ValueError("Calibration predictions overlap solver-fit businesses")
        reference = references[question.id]
        candidates = [candidate for candidate in trace.candidates if candidate.verified]
        candidate = next(
            (candidate for candidate in candidates if candidate.program.backend == "template"),
            candidates[0] if candidates else None,
        )
        correct_value = bool(
            candidate
            and matches({"type": candidate.output.type.value, "answer": candidate.value}, reference)
        )
        records.append(
            {
                "id": question.id,
                "business_id": question.business_id,
                "features": candidate_features(
                    candidate, trace.repair_count, trace.unsupported_verified
                ),
                "correct_value": correct_value,
                "correct_unanswerable": reference["type"] == "unanswerable",
                "prediction_origin": "held_out",
                "source_revision": trace.source_revision,
            }
        )
    target = writable_path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    from bizreason.utils import dumps

    target.write_text("".join(dumps(record) + "\n" for record in records), encoding="utf-8")
    print(f"Exported {len(records)} held-out calibration records")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bizreason", description="Vietnamese business-data reasoning pipeline"
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    unpack = commands.add_parser(
        "unpack", help="Safely extract a competition ZIP under source_test"
    )
    unpack.add_argument("--archive", required=True)
    unpack.add_argument("--destination", default=str(WORKSPACE / "data"))
    models = commands.add_parser("models", help="List model IDs available to TRAMMAY_KEY")
    models.add_argument("--env", type=Path)
    inspect = commands.add_parser("inspect", help="Build and inspect a business catalog")
    inspect.add_argument("--data", required=True)
    inspect.add_argument("--business", required=True)
    inspect.add_argument("--cache", default=str(WORKSPACE / "artifacts" / "cache"))
    run = commands.add_parser("run", help="Solve questions and checkpoint submission.jsonl")
    run.add_argument("--data", "--data-root", dest="data", required=True)
    run.add_argument("--questions", help="Explicit path to questions.jsonl file")
    run.add_argument("--output", "--out", dest="output", default=str(WORKSPACE / "result" / "submission.jsonl"))
    run.add_argument("--artifacts", "--artifact-dir", "--trace-dir", dest="artifacts", default=str(WORKSPACE / "artifacts" / "run"))
    run.add_argument("--cache", "--cache-dir", dest="cache", default=str(WORKSPACE / "artifacts" / "cache"))
    run.add_argument("--provider", default="trammay", help="Model provider (e.g. trammay)")
    run.add_argument("--workers", type=int, default=1, help="Worker concurrency")
    run.add_argument("--backend", choices=["api", "local", "none"], default="api")
    run.add_argument("--business", action="append")
    run.add_argument("--limit", type=int)
    run.add_argument("--model")
    run.add_argument("--base-url")
    run.add_argument("--api-style", choices=["chat", "responses"], default="chat")
    run.add_argument(
        "--gate", choices=["verified", "templates", "abstain", "calibrated"], default="verified"
    )
    run.add_argument("--calibration")
    run.add_argument("--question-timeout", type=float, default=150)
    run.add_argument("--sql-timeout", type=float, default=15)
    run.add_argument("--total-seconds", type=float, default=21600)
    run.add_argument("--max-model-calls", type=int, default=3)
    run.add_argument("--max-repairs", type=int, default=1)
    run.add_argument("--request-timeout", type=float)
    run.add_argument("--max-output-tokens", type=int)
    run.add_argument(
        "--context-characters",
        type=int,
        default=None,
        help="Maximum prompt character budget for LLM planner",
    )
    run.add_argument(
        "--runtime-mode",
        choices=["dev", "competition"],
        default=None,
        help="Runtime isolation mode (dev allows workspace, competition restricts to allowed roots)",
    )
    run.add_argument(
        "--group-by-business",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Group questions deterministically by business_id to maximize prefix cache and bound RAM",
    )
    run.add_argument(
        "--fair-pacing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable fair dynamic time pacing per question",
    )
    run.add_argument(
        "--enable-fast-triage",
        action="store_true",
        default=False,
        help="Enable deterministic fast-path triage router",
    )
    run.add_argument(
        "--enable-semantic-critic",
        action="store_true",
        default=False,
        help="Enable concrete LLM-backed semantic critic to audit candidate SQL and policies",
    )
    run.add_argument(
        "--stream",
        action="store_true",
        default=False,
        help="Enable SSE streaming client when backend is api",
    )
    verify_ledger = commands.add_parser(
        "verify-ledger", help="Verify offline model parameters and release date compliance"
    )
    verify_ledger.add_argument("--ledger", required=True, help="Path to model ledger JSON file")
    verify_ledger.add_argument(
        "--weights-dir", help="Optional path to directory of safetensors weights"
    )
    score = commands.add_parser("score", help="Run the independent grader")
    score.add_argument("--predictions", "--pred", dest="predictions", required=True)
    score.add_argument("--references", "--gold", dest="references", required=True)
    score.add_argument("--questions")
    score.add_argument(
        "--subset-predicted", action="store_true", help="Explicitly score only submitted IDs"
    )
    score.add_argument("--output", "--out", dest="output", default=str(WORKSPACE / "artifacts" / "score.json"))
    split = commands.add_parser(
        "split", help="Create disjoint fit/calibration/evaluation business groups"
    )
    split.add_argument("--questions", required=True)
    split.add_argument("--seed", type=int, default=2026)
    split.add_argument("--output", default=str(WORKSPACE / "artifacts" / "split.json"))
    compare = commands.add_parser("compare", help="Compare scored A/B runs on identical questions")
    compare.add_argument("--before", required=True)
    compare.add_argument("--after", required=True)
    compare.add_argument("--output", default=str(WORKSPACE / "artifacts" / "comparison.json"))
    records = commands.add_parser("calibration-records")
    records.add_argument("--traces", required=True)
    records.add_argument("--references", required=True)
    records.add_argument("--split-manifest", required=True)
    records.add_argument(
        "--output", default=str(WORKSPACE / "artifacts" / "calibration_records.jsonl")
    )
    fit = commands.add_parser("fit-gate")
    fit.add_argument("--records", required=True)
    fit.add_argument("--output", default=str(WORKSPACE / "artifacts" / "gate.json"))
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "unpack":
            print(unpack_dataset(Path(args.archive), Path(args.destination)))
        elif args.command == "models":
            client = TrammayClient(Settings.from_env(args.env))
            try:
                print("\n".join(client.models()))
            finally:
                client.close()
        elif args.command == "inspect":
            data = Path(args.data).resolve()
            builder, _, _ = _builder(data, Path(args.cache))
            context = builder.prepare(data / "data" / args.business)
            print(
                json.dumps(
                    {
                        "business": context.business_id,
                        "revision": context.revision,
                        "tables": len(context.tables),
                        "rows": sum(table.row_count for table in context.tables),
                        "joins": len(context.joins),
                        "diagnostics": [
                            item.model_dump(mode="json") for item in context.diagnostics
                        ],
                        "database": str(context.database),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "run":
            _run(args)
        elif args.command == "score":
            from bizreason.evaluation.scorer import score_records

            predictions, references = load_jsonl(Path(args.predictions)), load_jsonl(
                Path(args.references)
            )
            if args.subset_predicted:
                selected = {item["id"] for item in predictions}
                references = [item for item in references if item["id"] in selected]
            report = score_records(
                predictions,
                references,
                load_jsonl(Path(args.questions)) if args.questions else None,
            )
            report["subset_predicted_only"] = args.subset_predicted
            atomic_json(Path(args.output), report)
            print(
                json.dumps(
                    {
                        key: report[key]
                        for key in ["n", "correct", "wrong", "abstain", "pa", "coverage"]
                    },
                    indent=2,
                )
            )
        elif args.command == "split":
            from bizreason.evaluation.splits import grouped_split

            report = grouped_split(load_jsonl(Path(args.questions)), args.seed)
            atomic_json(Path(args.output), report)
            print({name: len(group["businesses"]) for name, group in report["splits"].items()})
        elif args.command == "compare":
            from bizreason.evaluation.splits import compare_reports

            report = compare_reports(_read_json(args.before), _read_json(args.after))
            atomic_json(Path(args.output), report)
            print(json.dumps(report, indent=2))
        elif args.command == "calibration-records":
            _calibration_records(args)
        elif args.command == "fit-gate":
            from bizreason.evaluation.calibration import fit_gate

            artifact = fit_gate(load_jsonl(Path(args.records)))
            atomic_json(Path(args.output), artifact)
            print(f"Fitted gate on {artifact['n']} records")
        elif args.command == "verify-ledger":
            inspector = inspect_safetensors_parameters
            if inspector is None:
                try:
                    from bizreason.model_ledger import inspect_safetensors_parameters as inspector
                except ImportError:
                    inspector = None
            from bizreason.model_ledger import count_safetensors_parameters, verify_model_ledger

            ledger = _read_json(args.ledger)
            violations = verify_model_ledger(ledger)
            weights_info = None
            if args.weights_dir:
                weights_path = Path(args.weights_dir)
                if inspector is not None:
                    inspection = inspector(weights_path)
                    if isinstance(inspection, dict):
                        counted = inspection.get(
                            "total_parameters", inspection.get("counted_parameters", 0)
                        )
                        weights_info = {
                            "weights_dir": str(args.weights_dir),
                            "counted_parameters": counted,
                            **inspection,
                        }
                    else:
                        counted = int(inspection)
                        weights_info = {
                            "weights_dir": str(args.weights_dir),
                            "counted_parameters": counted,
                            "inspection": inspection,
                        }
                else:
                    counted = count_safetensors_parameters(weights_path)
                    weights_info = {
                        "weights_dir": str(args.weights_dir),
                        "counted_parameters": counted,
                    }
                if counted > 15_000_000_000:
                    violations.append(
                        f"SAFETENSORS_EXCEEDS_MAX_PARAMETERS:{counted}>15000000000"
                    )
            report = {
                "status": "COMPLIANT" if not violations else "NON_COMPLIANT",
                "violations": violations,
                "offline_total_parameters": ledger.get("offline_total_parameters"),
                "weights_verification": weights_info,
            }
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if violations:
                parser.exit(1, f"Model ledger validation failed with {len(violations)} violations\n")
    except (ValueError, OSError, ModelError) as error:
        parser.exit(2, f"Error: {error}\n")


if __name__ == "__main__":
    main()
