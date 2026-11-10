"""Resumable parallel API experiment. No references, fitting, or portal submission."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from bizreason.config import Settings
from bizreason.contracts import AnswerType, BusinessContext, OutputSpec, Prediction, Question
from bizreason.formatting import SubmissionWriter, validate_answer
from bizreason.llm import TrammayClient
from bizreason.pipeline import Solver
from bizreason.rules import RuleRegistry
from bizreason.streaming import StreamingTrammayClient
from bizreason.temporal import LunarCalendar, TemporalResolver
from bizreason.utils import WORKSPACE, atomic_json, digest, file_hash, load_jsonl, writable_path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def run_lock(root: Path):
    directory = writable_path(root)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "run.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError(
                "Another process owns this experiment; do not start duplicate API jobs"
            ) from None
        yield


class Checkpoints:
    def __init__(self, root: Path, signature: str, manifest: dict):
        self.root = writable_path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "run_manifest.json"
        if path.exists():
            if read_json(path)["signature"] != signature:
                raise ValueError(
                    "Run settings, code or input changed; use a new artifact directory"
                )
        else:
            atomic_json(path, {**manifest, "signature": signature})

    def answer_path(self, identity: str) -> Path:
        return self.root / "answers" / (digest(identity)[:24] + ".json")

    def load(self, questions: list[Question]) -> dict[str, Prediction]:
        result = {}
        for question in questions:
            path = self.answer_path(question.id)
            if path.exists():
                prediction = Prediction.model_validate_json(path.read_text(encoding="utf-8"))
                if prediction.id != question.id:
                    raise ValueError("Checkpoint question ID mismatch")
                if prediction.type not in {AnswerType.ABSTAIN, AnswerType.UNANSWERABLE}:
                    validate_answer(prediction.answer, OutputSpec(type=prediction.type))
                result[question.id] = prediction
        return result

    def save(self, prediction: Prediction, trace: dict) -> None:
        name = digest(prediction.id)[:24] + ".json"
        atomic_json(self.root / "traces" / name, trace)
        # The answer is the commit marker; a trace is always written first.
        atomic_json(
            self.answer_path(prediction.id), prediction.model_dump(mode="json", exclude_none=True)
        )


def write_predictions(
    path: Path, questions: list[Question], predictions: dict[str, Prediction]
) -> None:
    target = writable_path(path)
    temporary = target.with_suffix(target.suffix + ".writing")
    with SubmissionWriter(temporary) as writer:
        for question in questions:
            if question.id in predictions:
                writer.write(predictions[question.id])
    temporary.replace(target)


class Experiment:
    def __init__(
        self,
        contexts: dict[str, BusinessContext],
        rules: RuleRegistry,
        temporal: TemporalResolver,
        settings: Settings,
        backend: str,
        stream: bool = False,
    ):
        self.contexts, self.rules, self.temporal = contexts, rules, temporal
        self.settings, self.backend = settings, backend
        self.stream = stream
        self.local = threading.local()
        self.clients: list[TrammayClient] = []
        self.lock = threading.Lock()

    def client(self) -> TrammayClient | None:
        if self.backend == "none":
            return None
        if not hasattr(self.local, "client"):
            factory = StreamingTrammayClient if self.stream else TrammayClient
            self.local.client = factory(self.settings)
            with self.lock:
                self.clients.append(self.local.client)
        return self.local.client

    def solve(self, question: Question) -> tuple[Prediction | None, dict, str | None]:
        context = self.contexts[question.business_id]
        blockers = [
            item.code
            for item in context.diagnostics
            if item.code in {"SOURCE_READ_ERROR", "UNRESOLVED_HEADER"}
        ]
        if blockers:
            return (
                Prediction(
                    id=question.id, type=AnswerType.ABSTAIN, reason="Incomplete business catalog"
                ),
                {
                    "stage": "preparation",
                    "question_id": question.id,
                    "errors": blockers,
                    "model_calls": 0,
                },
                None,
            )
        model = self.client()
        attempts = []
        for attempt in range(2):
            solver = Solver(model, self.rules, self.temporal, self.settings)
            prediction, trace = solver.solve(question, context)
            attempts.append(trace.model_dump(mode="json"))
            provider_errors = [
                message for message in trace.errors if message.startswith("Provider ")
            ]
            if not provider_errors:
                payload = trace.model_dump(mode="json")
                if len(attempts) > 1:
                    payload["transport_attempts"] = attempts
                return prediction, payload, None
            code = provider_errors[-1]
            if any(value in code for value in ["HTTP 401", "HTTP 402", "HTTP 403"]):
                return None, {"question_id": question.id, "transport_attempts": attempts}, code
            if attempt == 0:
                time.sleep(30 if "HTTP 429" in code else 10)
        return None, {"question_id": question.id, "transport_attempts": attempts}, code

    def usage(self) -> tuple[int, int]:
        with self.lock:
            return sum(client.usage.calls for client in self.clients), sum(
                client.usage.tokens for client in self.clients
            )

    def close(self) -> None:
        for client in self.clients:
            client.close()


def run(args: argparse.Namespace) -> None:
    data = Path(args.data).resolve()
    directory = writable_path(args.artifacts)
    output = writable_path(args.output)
    partial = output.with_name(output.stem + ".partial.jsonl")
    questions = [Question.model_validate(item) for item in load_jsonl(data / "questions.jsonl")]
    if getattr(args, "question_id", None):
        selected = set(args.question_id)
        if not selected <= {question.id for question in questions}:
            raise ValueError("Unknown selected question ID")
        questions = [question for question in questions if question.id in selected]
    if args.limit is not None:
        questions = questions[: args.limit]
    if not questions or args.workers < 1:
        raise ValueError("A nonempty question selection and positive worker count are required")
    prepared = read_json(Path(args.ingestion_report))
    if prepared["source_hash_mismatches"]:
        raise ValueError("Source checksums differ from the provided archive")
    contexts = {}
    for record in prepared["businesses"]:
        identity = record["business_id"]
        path = WORKSPACE / "artifacts/cache" / identity / record["revision"][:20] / "catalog.json"
        context = BusinessContext.model_validate_json(path.read_text(encoding="utf-8"))
        for relative, expected in context.manifest.items():
            source = (data / "data" / identity / relative).resolve()
            if (
                not source.is_relative_to((data / "data").resolve())
                or file_hash(source) != expected
            ):
                raise ValueError("Prepared source changed or escaped the data directory")
        contexts[identity] = context
    if not {question.business_id for question in questions} <= contexts.keys():
        raise ValueError("Some question businesses have not been prepared")
    settings = Settings.from_env(
        question_timeout=args.question_timeout,
        request_timeout=args.request_timeout,
        max_model_calls=3,
        max_repairs=1,
        max_output_tokens=4000,
    )
    public_settings = {key: value for key, value in asdict(settings).items() if key != "api_key"}
    manifest = {
        "questions_sha256": file_hash(data / "questions.jsonl"),
        "rulebook_sha256": file_hash(data / "rulebook.md"),
        "calendar_sha256": file_hash(data / "calendar.csv"),
        "backend": args.backend,
        "stream": args.stream,
        "settings": public_settings,
        "business_revisions": {name: context.revision for name, context in contexts.items()},
        "implementation": {
            str(path.relative_to(WORKSPACE)): file_hash(path)
            for path in sorted((WORKSPACE / "bizreason").rglob("*.py"))
        },
        "runner_sha256": file_hash(Path(__file__)),
        "question_ids": [question.id for question in questions],
        "output": str(output),
        "workers": args.workers,
        "note": "Remote API development experiment; not an eligible offline competition submission.",
    }
    signature = digest(manifest)
    if output.exists() and not (directory / "run_manifest.json").exists():
        raise FileExistsError("Refusing to replace an output from another experiment")
    store = Checkpoints(directory, signature, manifest)
    predictions = store.load(questions)
    if args.reuse_from:
        old_root = Path(args.reuse_from).resolve()
        old_manifest = read_json(old_root / "run_manifest.json")
        for field in ["questions_sha256", "rulebook_sha256", "calendar_sha256"]:
            if old_manifest.get(field) != manifest[field]:
                raise ValueError("Imported checkpoints use different input data")
        if any(
            old_manifest["settings"][field] != public_settings[field]
            for field in ["model", "base_url"]
        ):
            raise ValueError("Imported checkpoints use a different provider/model")
        for question in questions:
            old_answer = old_root / "answers" / (digest(question.id)[:24] + ".json")
            old_trace = old_root / "traces" / old_answer.name
            if question.id in predictions or not old_answer.exists() or not old_trace.exists():
                continue
            trace = read_json(old_trace)
            if (
                trace.get("question") != question.model_dump(mode="json")
                or trace.get("source_revision") != contexts[question.business_id].revision
            ):
                raise ValueError("Imported checkpoint question/source mismatch")
            prediction = Prediction.model_validate_json(old_answer.read_text(encoding="utf-8"))
            if prediction.id != question.id:
                raise ValueError("Imported checkpoint ID mismatch")
            trace["imported_from"] = str(old_root)
            store.save(prediction, trace)
            predictions[question.id] = prediction
    pending = iter(question for question in questions if question.id not in predictions)
    rules = RuleRegistry.load(data / "rulebook.md")
    experiment = Experiment(
        contexts,
        rules,
        TemporalResolver(LunarCalendar(data / "calendar.csv")),
        settings,
        args.backend,
        args.stream,
    )
    previous = read_json(directory / "status.json") if (directory / "status.json").exists() else {}
    base_calls, base_tokens = previous.get("model_calls", 0), previous.get("model_tokens", 0)
    started = time.monotonic()
    issues: list[dict] = []
    blocked = False
    consecutive_service_errors = 0
    state = "running"

    def progress() -> dict:
        calls, tokens = experiment.usage()
        types = Counter(prediction.type.value for prediction in predictions.values())
        result = {
            "pid": os.getpid(),
            "state": state,
            "completed": len(predictions),
            "total": len(questions),
            "answer_types": dict(types),
            "model_calls": base_calls + calls,
            "model_tokens": base_tokens + tokens,
            "elapsed_this_session_seconds": time.monotonic() - started,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "service_issues": issues[-10:],
            "workers": args.workers,
            "output": str(output),
            "partial_output": str(partial),
            "ground_truth_available": False,
            "pa": None,
        }
        atomic_json(directory / "status.json", result)
        return result

    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / "process.json", {"pid": os.getpid(), "signature": signature})
    print(
        f"Starting {len(questions)} questions; resumed={len(predictions)}; workers={args.workers}",
        flush=True,
    )
    progress()
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            active = {}

            def submit_next() -> bool:
                try:
                    question = next(pending)
                except StopIteration:
                    return False
                active[pool.submit(experiment.solve, question)] = question
                return True

            for _ in range(args.workers):
                if not submit_next():
                    break
            while active:
                done, _ = wait(active, timeout=5, return_when=FIRST_COMPLETED)
                if (directory / "STOP").exists() or time.monotonic() - started > args.total_seconds:
                    blocked, state = True, "stopping"
                for future in done:
                    question = active.pop(future)
                    try:
                        prediction, trace, service_error = future.result()
                    except Exception as error:
                        prediction = Prediction(
                            id=question.id,
                            type=AnswerType.ABSTAIN,
                            reason=f"Runtime failure: {type(error).__name__}",
                        )
                        trace, service_error = {
                            "question_id": question.id,
                            "runner_error": type(error).__name__,
                        }, None
                    if service_error:
                        atomic_json(
                            directory / "service_failures" / (digest(question.id)[:24] + ".json"),
                            trace,
                        )
                        issues.append({"id": question.id, "error": service_error})
                        consecutive_service_errors += 1
                        if any(
                            code in service_error for code in ["HTTP 401", "HTTP 402", "HTTP 403"]
                        ) or consecutive_service_errors >= min(args.workers, 4):
                            blocked, state = True, "service_blocked"
                    else:
                        consecutive_service_errors = 0
                        assert prediction is not None
                        store.save(prediction, trace)
                        predictions[question.id] = prediction
                        print(
                            f"[{len(predictions)}/{len(questions)}] {question.id}: {prediction.type.value}",
                            flush=True,
                        )
                        write_predictions(partial, questions, predictions)
                    if not blocked:
                        submit_next()
                progress()
        if len(predictions) == len(questions):
            write_predictions(output, questions, predictions)
            state = "complete"
        elif state == "running":
            state = "interrupted"
    finally:
        experiment.close()
        summary = progress()
        atomic_json(directory / "runtime_report.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data/public_test")
    parser.add_argument("--ingestion-report", default="artifacts/public_ingestion_validation.json")
    parser.add_argument("--artifacts", default="artifacts/public_full_trammay")
    parser.add_argument("--output", default="result/public_test_trammay.jsonl")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--backend", choices=["api", "none"], default="api")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--question-id", action="append", help="Select explicit IDs, preserving input order")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--reuse-from")
    parser.add_argument("--question-timeout", type=float, default=240)
    parser.add_argument("--request-timeout", type=float, default=150)
    parser.add_argument("--total-seconds", type=float, default=86400)
    args = parser.parse_args()
    with run_lock(Path(args.artifacts)):
        run(args)


if __name__ == "__main__":
    main()
