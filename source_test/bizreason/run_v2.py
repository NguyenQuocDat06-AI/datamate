"""Run the V2 semantic pipeline end to end on a test folder and write the submission.

    python -m bizreason.run_v2 --data <test_root> --out <out_dir> --connection <json> [--endpoint-file <json>]

Per shop, in order (each stage cached under <out_dir>): ingestion, annotation (ann/), status (status/),
line kinds (line_kinds/), value groups (groups/), then the read-only semantic layer (sem/<biz>.sqlite).
Questions are answered by SemanticSolver on --workers threads. Every answer is checkpointed to
submission.jsonl as it finishes; submission.final.jsonl is the sorted, complete copy at the end.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bizreason.cli import _builder
from bizreason.config import Settings
from bizreason.contracts import AnswerType, OutputSpec, Prediction, Question
from bizreason.data import schema
from bizreason.formatting import validate_answer
from bizreason.llm import TrammayClient, endpoint_file_resolver
from bizreason.semantic.annotator import Annotator, load_or_annotate, load_status_map, load_value_groups
from bizreason.semantic.intent import IntentParser
from bizreason.semantic.layer import annotations_by_signature, build, effective_roles, to_date, to_number
from bizreason.semantic.solver import SemanticSolver
from bizreason.temporal import TemporalResolver
from bizreason.utils import PathPolicy, atomic_json, dumps, file_hash, normalize_lookup, quote_identifier, writable_path

WORKTREE = Path(__file__).resolve().parents[1]
DEFAULT_TOTAL_SECONDS = 21600.0
BUDGET_MARGIN_SECONDS = 300.0
DEFAULT_OUTAGE_WAIT = 1800.0
DEFAULT_PROJECT_SHOPS = 30
DEFAULT_PROJECT_QUESTIONS = 3000
# Same limits as the reference measurement scripts.
ANNOTATION_MAX_TOKENS = 1500
ANNOTATION_TIMEOUT = 280.0
INTENT_MAX_TOKENS = 400
INTENT_TIMEOUT = 200.0
ANNOTATOR_WORKERS = 4
STAGES = ("ingestion", "annotation", "status", "line_kinds", "value_groups", "build")
# Decisions for questions that were never attempted: they are not checkpointed, so a rerun answers them.
NOT_ATTEMPTED = frozenset({"budget", "no_shop"})
SECRET_ARG_MARKERS = ("token", "secret", "password", "key")


@dataclass(frozen=True)
class Outcome:
    question_id: str
    decision: str
    seconds: float
    line: str
    checkpoint: bool


@dataclass
class RunContext:
    data: Path
    out: Path
    builder: Any
    annotator: Annotator
    log: RunLog


class RunLog:
    """timing.jsonl: one row per stage per shop and per question, flushed as written and kept for the summary."""

    def __init__(self, path: Path):
        self.path = writable_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []

    def add(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.rows.append(row)
            self._handle.write(dumps(row) + "\n")
            self._handle.flush()

    def close(self) -> None:
        self._handle.close()


class Checkpoint:
    """submission.jsonl: one line per answered question, appended as each finishes; damaged lines are dropped."""

    def __init__(self, path: Path):
        self.path = writable_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lines: dict[str, str] = {}
        kept: list[str] = []
        damaged = False
        if self.path.exists():
            for raw in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    qid = json.loads(raw)["id"]
                except (ValueError, KeyError, TypeError):
                    damaged = True
                    continue
                if not isinstance(qid, str) or qid in self.lines:
                    damaged = True
                    continue
                self.lines[qid] = raw
                kept.append(raw)
        if damaged:
            _write_text_atomic(self.path, "".join(line + "\n" for line in kept))
        self._handle = self.path.open("a", encoding="utf-8", newline="\n")

    def append(self, question_id: str, line: str) -> None:
        self._handle.write(line + "\n")
        self._handle.flush()
        self.lines[question_id] = line

    def close(self) -> None:
        self._handle.close()


class SolverPool:
    """Answers questions. Each thread keeps its own read-only connection and solver per shop."""

    def __init__(self, parser: IntentParser, temporal: TemporalResolver, paths: dict[str, Path]):
        self._parser = parser
        self._temporal = temporal
        self._paths = paths
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def solve(self, question: Question, deadline: float) -> Outcome:
        started = time.monotonic()
        if started > deadline:
            return _abstain(question, "time budget exhausted", "budget", started, checkpoint=False)
        path = self._paths.get(question.business_id)
        if path is None:
            return _abstain(question, "shop not built", "no_shop", started, checkpoint=False)
        try:
            prediction, trace = self._solver(question.business_id, path).solve(question)
            line = submission_line(prediction)
        except Exception as exc:  # one bad question must never stop the run
            return _abstain(question, f"error: {type(exc).__name__}", "error", started, checkpoint=True)
        return Outcome(question.id, trace.decision, _elapsed(started), line, checkpoint=True)

    def _solver(self, biz: str, path: Path) -> SemanticSolver:
        cache = getattr(self._local, "solvers", None)
        if cache is None:
            cache = self._local.solvers = {}
        if biz not in cache:
            connection = connect_read_only(path)
            with self._lock:
                self._connections.append(connection)
            cache[biz] = SemanticSolver(self._parser, self._temporal, connection)
        return cache[biz]

    def close(self) -> None:
        for connection in self._connections:
            connection.close()
        self._connections.clear()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m bizreason.run_v2", description=__doc__.split("\n")[0])
    parser.add_argument("--data", required=True, type=Path, help="test root: questions.jsonl, rulebook.md, data/<biz>")
    parser.add_argument("--out", required=True, type=Path, help="output and cache directory")
    parser.add_argument("--business", nargs="+", action="extend", metavar="ID", help="only these business ids")
    parser.add_argument("--limit", type=int, help="only the first N questions after the business filter")
    parser.add_argument("--workers", type=int, default=4, help="parallel question threads")
    parser.add_argument("--connection", required=True, type=Path, help="JSON with model and bearer_token")
    parser.add_argument("--endpoint-file", type=Path, help="JSON with base_url, re-read on tunnel outage")
    parser.add_argument("--base-url", help="model endpoint when no --endpoint-file is given")
    parser.add_argument("--total-seconds", type=float, default=DEFAULT_TOTAL_SECONDS,
                        help="wall budget; questions after it (minus a 5 minute margin) abstain")
    parser.add_argument("--outage-wait", type=float, default=DEFAULT_OUTAGE_WAIT,
                        help="seconds to wait for the endpoint to come back during an outage")
    parser.add_argument("--no-alias", action="store_true",
                        help="ignore the header alias list during ingestion (simulates unseen headers)")
    parser.add_argument("--project-shops", type=int, default=DEFAULT_PROJECT_SHOPS)
    parser.add_argument("--project-questions", type=int, default=DEFAULT_PROJECT_QUESTIONS)
    return parser.parse_args(argv)


def make_clients(args: argparse.Namespace) -> tuple[TrammayClient, TrammayClient]:
    """Annotation and intent clients. The connection file's token is read here and never logged or stored."""
    creds = json.loads(args.connection.read_text(encoding="utf-8-sig"))
    model = creds.get("model") if isinstance(creds, dict) else None
    token = creds.get("bearer_token") if isinstance(creds, dict) else None
    if not isinstance(model, str) or not model or not isinstance(token, str) or not token:
        raise ValueError("connection file must hold a non-empty model and bearer_token")
    resolver = endpoint_file_resolver(args.endpoint_file) if args.endpoint_file else None
    base_url = args.base_url or (resolver() if resolver else None)
    if not base_url:
        raise ValueError("no model base URL: pass --base-url or an --endpoint-file that holds base_url")

    def client(max_tokens: int, timeout: float) -> TrammayClient:
        settings = Settings(base_url=base_url, model=model, api_key=token,
                            max_output_tokens=max_tokens, request_timeout=timeout)
        return TrammayClient(settings, endpoint_resolver=resolver, outage_wait_seconds=args.outage_wait)

    return client(ANNOTATION_MAX_TOKENS, ANNOTATION_TIMEOUT), client(INTENT_MAX_TOKENS, INTENT_TIMEOUT)


def load_questions(path: Path, businesses: list[str] | None, limit: int | None) -> list[Question]:
    """Questions in file order; a repeated id is kept once. Business filter first, then the limit."""
    seen: set[str] = set()
    questions: list[Question] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw.strip():
            continue
        question = Question.model_validate(json.loads(raw))
        if businesses and question.business_id not in businesses:
            continue
        if question.id in seen:
            continue
        seen.add(question.id)
        questions.append(question)
    if limit is not None:
        questions = questions[:limit]
    return questions


def submission_line(prediction: Prediction) -> str:
    """The JSON line SubmissionWriter.write produces for this prediction (same keys, same answer checks)."""
    payload: dict[str, Any] = {"id": prediction.id, "type": prediction.type.value}
    if prediction.type not in {AnswerType.ABSTAIN, AnswerType.UNANSWERABLE}:
        payload["answer"] = validate_answer(
            prediction.answer, OutputSpec(type=prediction.type, rounding=prediction.rounding)
        )
    if prediction.reason is not None:
        payload["reason"] = prediction.reason
    return dumps(payload)


def connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    connection.create_function("lookup", 1, lambda value: None if value is None else normalize_lookup(value))
    connection.create_function("to_number", 1, to_number)
    connection.create_function("to_date", 1, to_date)
    return connection


@contextmanager
def timed(log: RunLog, biz: str, stage: str) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    finally:
        log.add({"biz": biz, "stage": stage, "seconds": _elapsed(started)})


def load_or_annotate_line_kinds(annotator: Annotator, context: Any, records: list[dict[str, Any]],
                                cache_path: Path) -> dict[str, str]:
    """Kind of each sold product name the catalog does not list. Cached like the other stages; a failure is not cached."""
    cached = _read_cache(cache_path)
    if isinstance(cached, dict) and cached.get("revision") == context.revision and isinstance(cached.get("line_kinds"), dict):
        return cached["line_kinds"]
    failures_before = annotator.failure_count()
    line_kinds = annotator.annotate_line_kinds(
        context, records, _names(context, records, {"product_catalog"}), _names(context, records, {"sales"})
    )
    if annotator.failure_count() != failures_before:
        return line_kinds  # a transient model failure must not persist
    atomic_json(cache_path, {"revision": context.revision, "line_kinds": line_kinds})
    return line_kinds


def build_shop(biz: str, ctx: RunContext) -> Path:
    """Build sem/<biz>.sqlite unless it already exists. Returns its path."""
    target = ctx.out / "sem" / f"{biz}.sqlite"
    if target.is_file():
        return target
    annotator = ctx.annotator
    annotator.take_failures()  # failures left by a shop that did not build must not reach this one
    with timed(ctx.log, biz, "ingestion"):
        context = ctx.builder.prepare(ctx.data / "data" / biz)
    with timed(ctx.log, biz, "annotation"):
        records = load_or_annotate(annotator, context, ctx.out / "ann" / f"{biz}.json")
    with timed(ctx.log, biz, "status"):
        status_map = load_status_map(annotator, context, records, ctx.out / "status" / f"{biz}.json")
    with timed(ctx.log, biz, "line_kinds"):
        line_kinds = load_or_annotate_line_kinds(annotator, context, records, ctx.out / "line_kinds" / f"{biz}.json")
    with timed(ctx.log, biz, "value_groups"):
        value_groups = load_value_groups(annotator, context, records, ctx.out / "groups" / f"{biz}.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".building")
    with timed(ctx.log, biz, "build"):
        build(context, records, partial, status_map=status_map, line_kinds=line_kinds,
              value_groups=value_groups, model_failures=annotator.take_failures())
        partial.replace(target)
    return target


def solve_questions(questions: list[Question], parser: IntentParser, temporal: TemporalResolver,
                    paths: dict[str, Path], deadline: float, workers: int,
                    record: Callable[[Outcome], None]) -> None:
    """Answer on a thread pool. `record` runs on the calling thread as each outcome finishes."""
    pool = SolverPool(parser, temporal, paths)
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(pool.solve, question, deadline) for question in questions]
            for future in as_completed(futures):
                record(future.result())
    finally:
        pool.close()


def write_final(path: Path, questions: list[Question], lines: dict[str, str]) -> None:
    """Every selected question id once, sorted; an id with no answer becomes an abstain."""
    body = []
    for question_id in sorted({question.id for question in questions}):
        line = lines.get(question_id)
        if line is None:
            line = submission_line(Prediction(id=question_id, type=AnswerType.ABSTAIN, reason="not answered"))
        body.append(line + "\n")
    _write_text_atomic(writable_path(path), "".join(body))


def format_summary(rows: list[dict[str, Any]], workers: int, project_shops: int, project_questions: int) -> str:
    """Stage totals, mean seconds per attempted question, and the projection for the given shop and question counts."""
    totals = {stage: 0.0 for stage in STAGES}
    shops: set[str] = set()
    question_seconds: list[float] = []
    for row in rows:
        if "stage" in row:
            totals[row["stage"]] = totals.get(row["stage"], 0.0) + row["seconds"]
            if row["stage"] == "build":
                shops.add(row["biz"])
        elif row["decision"] not in NOT_ATTEMPTED:
            question_seconds.append(row["seconds"])
    shop_total = sum(totals.values())
    per_shop = shop_total / len(shops) if shops else 0.0
    mean_question = statistics.fmean(question_seconds) if question_seconds else 0.0
    projected = project_shops * per_shop + project_questions * mean_question / max(workers, 1)
    lines = ["stage            total_s"]
    lines += [f"{stage:<16} {totals.get(stage, 0.0):>9.1f}" for stage in STAGES]
    lines.append(f"shops built this run: {len(shops)}")
    lines.append(f"questions attempted: {len(question_seconds)}, mean seconds per question: {mean_question:.2f} "
                 f"(workers={max(workers, 1)})")
    lines.append(f"projected for {project_shops} shops and {project_questions} questions: {projected:.0f} s "
                 f"(~{projected / 3600:.2f} h; shop cost per shop {per_shop:.1f} s, question wall time divided by workers)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data = args.data.resolve()
    out = args.out.resolve()
    # Outputs may go under --out (outside this worktree); nothing may be written inside the test data.
    with PathPolicy(mode="competition", allowed_roots=[out], forbidden_roots=[data]):
        return _run(args, data, out)


def _run(args: argparse.Namespace, data: Path, out: Path) -> int:
    t0 = time.monotonic()
    if args.workers < 1:
        raise ValueError("--workers must be at least 1")
    questions = load_questions(data / "questions.jsonl", args.business, args.limit)
    if not questions:
        raise ValueError("No questions selected")
    out.mkdir(parents=True, exist_ok=True)
    rulebook = data / "rulebook.md"
    manifest: dict[str, Any] = {
        "git_commit": git_commit(),
        "arguments": {key: _plain(value) for key, value in vars(args).items() if not _is_secret_arg(key)},
        "rulebook_path": str(rulebook),
        "rulebook_sha256": file_hash(rulebook),
        "started_at": _now(),
        "finished_at": None,
    }
    atomic_json(out / "manifest.json", manifest)

    checkpoint = Checkpoint(out / "submission.jsonl")
    log = RunLog(out / "timing.jsonl")
    clients: list[TrammayClient] = []
    saved_roles: dict[str, Any] | None = None
    try:
        if args.no_alias:
            saved_roles = dict(schema.ROLE_LOOKUP)
            schema.ROLE_LOOKUP.clear()
        annotation_client, intent_client = make_clients(args)
        clients = [annotation_client, intent_client]
        builder, _rules, temporal = _builder(data, out / "cache")
        ctx = RunContext(data, out, builder, Annotator(annotation_client, workers=ANNOTATOR_WORKERS), log)
        deadline = t0 + args.total_seconds - BUDGET_MARGIN_SECONDS

        pending = [question for question in questions if question.id not in checkpoint.lines]
        paths: dict[str, Path] = {}
        for biz in dict.fromkeys(question.business_id for question in pending):
            if time.monotonic() > deadline:
                break
            try:
                paths[biz] = build_shop(biz, ctx)
            except Exception as exc:  # the shop's questions abstain; the other shops still run
                print(f"shop {biz}: build failed ({type(exc).__name__})", flush=True)

        lines = dict(checkpoint.lines)
        done = [0]

        def record(outcome: Outcome) -> None:
            log.add({"id": outcome.question_id, "seconds": outcome.seconds, "decision": outcome.decision})
            if outcome.checkpoint:
                checkpoint.append(outcome.question_id, outcome.line)
            lines[outcome.question_id] = outcome.line
            done[0] += 1
            print(f"[{done[0]}/{len(pending)}] {outcome.question_id}: {outcome.decision} ({outcome.seconds:.2f}s)",
                  flush=True)

        solve_questions(pending, IntentParser(intent_client), temporal, paths, deadline, args.workers, record)
        write_final(out / "submission.final.jsonl", questions, lines)
    finally:
        checkpoint.close()
        log.close()
        for client in clients:
            try:
                client.close()
            except Exception:
                pass
        if saved_roles is not None:
            schema.ROLE_LOOKUP.clear()
            schema.ROLE_LOOKUP.update(saved_roles)

    manifest["finished_at"] = _now()
    manifest["elapsed_seconds"] = _elapsed(t0)
    atomic_json(out / "manifest.json", manifest)
    print(format_summary(log.rows, args.workers, args.project_shops, args.project_questions), flush=True)
    return 0


def _names(context: Any, records: list[dict[str, Any]], wanted: set[str]) -> list[str]:
    """Distinct product names read from the tables of the wanted kinds (read-only)."""
    by_signature = annotations_by_signature(records)
    found: set[str] = set()
    connection = sqlite3.connect(f"{context.database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        for table in context.tables:
            note = by_signature.get(tuple(column.name for column in table.columns))
            if note is None or note.get("kind") not in wanted:
                continue
            column = effective_roles(table, note).get("product_name")
            if not column:
                continue
            sql = (f"SELECT DISTINCT {quote_identifier(column)} FROM {quote_identifier(table.name)} "
                   f"WHERE {quote_identifier(column)} IS NOT NULL")
            found.update(str(row[0]).strip() for row in connection.execute(sql))
    finally:
        connection.close()
    return sorted(found)


def _abstain(question: Question, reason: str, decision: str, started: float, *, checkpoint: bool) -> Outcome:
    line = submission_line(Prediction(id=question.id, type=AnswerType.ABSTAIN, reason=reason))
    return Outcome(question.id, decision, _elapsed(started), line, checkpoint=checkpoint)


def _read_cache(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_text_atomic(path: Path, text: str) -> None:
    path = writable_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def git_commit() -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=WORKTREE, capture_output=True, text=True,
                                timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _is_secret_arg(name: str) -> bool:
    return any(marker in name.lower() for marker in SECRET_ARG_MARKERS)


def _plain(value: Any) -> Any:
    return str(value) if isinstance(value, Path) else value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _elapsed(started: float) -> float:
    return round(time.monotonic() - started, 3)


if __name__ == "__main__":
    raise SystemExit(main())
