"""Run the V2 semantic pipeline end to end on a test folder and write the submission.

    python -m bizreason.run_v2 --data <test_root> --out <out_dir> --connection <json> [--endpoint-file <json>]

Per shop, in order (each stage cached under <out_dir>): ingestion, annotation (ann/), status (status/),
line kinds (line_kinds/), value groups (groups/), then the read-only semantic layer (sem/<biz>.sqlite).
Shops are built in --build-workers processes. A shop's questions are answered on --workers threads as soon as
its database is ready. Every answer is checkpointed to submission.jsonl as it finishes; submission.final.jsonl
is the sorted, complete copy at the end.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import sqlite3
import statistics
import subprocess
import threading
import time
import uuid
from collections import Counter, deque
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bizreason.config import Settings
from bizreason.contracts import AnswerType, OutputSpec, Prediction, Question
from bizreason.data import schema
from bizreason.formatting import validate_answer
from bizreason.llm import TrammayClient, endpoint_file_resolver
from bizreason.semantic.annotator import Annotator, load_or_annotate, load_status_map, load_value_groups
from bizreason.semantic.fewshot import FewShotBank
from bizreason.semantic.intent import ConsensusParser, IntentParser, ParsedIntent
from bizreason.semantic.layer import annotations_by_signature, build, effective_roles, to_date, to_number
from bizreason.semantic.answer_gate import AnswerGate
from bizreason.semantic.solver import SemanticSolver
from bizreason.temporal import TemporalResolver
from bizreason.utils import PathPolicy, atomic_json, dumps, file_hash, normalize_lookup, quote_identifier, writable_path
from bizreason.semantic import definitions as rulebook_definitions
from bizreason.semantic import rule_terms
from bizreason.semantic import rulebook_params
from bizreason.v2_setup import build_v2_context

WORKTREE = Path(__file__).resolve().parents[2]
DEFAULT_TOTAL_SECONDS = 21600.0
BUDGET_MARGIN_SECONDS = 300.0
DEFAULT_OUTAGE_WAIT = 1800.0
DEFAULT_PROJECT_SHOPS = 30
DEFAULT_PROJECT_QUESTIONS = 3000
DEFAULT_BUILD_WORKERS = 2
DEFAULT_MAX_MODEL_CONCURRENCY = 4
# Same limits as the reference measurement scripts.
ANNOTATION_MAX_TOKENS = 1500
ANNOTATION_TIMEOUT = 280.0
INTENT_MAX_TOKENS = 400
# The analysis key of --reasoning on adds up to about three sentences before the intent.
INTENT_REASONING_MAX_TOKENS = 700
INTENT_TIMEOUT = 200.0
ANNOTATOR_WORKERS = 4
STAGES = ("ingestion", "annotation", "status", "line_kinds", "value_groups", "build")
# Decisions for questions that were never attempted: they are not checkpointed, so a rerun answers them.
NOT_ATTEMPTED = frozenset({"budget", "no_shop"})
SECRET_ARG_MARKERS = ("token", "secret", "password", "key")
PROMPT_PROFILES = ("default", "qwen")

# Shared by every model call of the run (builds and question parsing). Set in each build process by
# _init_build_worker; a fork-context semaphore, so it bounds requests across processes.
_MODEL_GATE: Any = None


@dataclass(frozen=True)
class Outcome:
    question_id: str
    decision: str
    seconds: float
    line: str
    checkpoint: bool
    finished: float  # time.monotonic() when the answer was complete
    trace: dict[str, Any] | None = None  # traces.jsonl row; None when --traces is off or nothing was solved


@dataclass(frozen=True)
class ShopJob:
    biz: str
    data: Path
    out: Path
    args: argparse.Namespace


@dataclass(frozen=True)
class ShopBuild:
    biz: str
    path: Path
    rows: list[dict[str, Any]]  # stage timing rows, written to timing.jsonl by the parent


@dataclass
class RunContext:
    data: Path
    out: Path
    builder: Any
    annotator: Annotator
    log: Any  # RunLog or StageRows; anything with add()


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


class StageRows:
    """Timing rows collected inside a build process; the parent appends them to timing.jsonl."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, row: dict[str, Any]) -> None:
        self.rows.append(row)


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


class TraceWriter:
    """traces.jsonl: one row per solved question, appended and flushed as each answer is recorded.

    On open, rows are kept only for ids already in submission.jsonl (keep_ids), first occurrence only, so a resumed
    run never duplicates a trace and a question that is answered again gets exactly one fresh row.
    """

    def __init__(self, path: Path, keep_ids: set[str]):
        self.path = writable_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.ids: set[str] = set()
        kept: list[str] = []
        if self.path.exists():
            for raw in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    qid = json.loads(raw)["id"]
                except (ValueError, KeyError, TypeError):
                    continue
                if isinstance(qid, str) and qid in keep_ids and qid not in self.ids:
                    self.ids.add(qid)
                    kept.append(raw)
        _write_text_atomic(self.path, "".join(line + "\n" for line in kept))
        self._handle = self.path.open("a", encoding="utf-8", newline="\n")

    def append(self, row: dict[str, Any]) -> None:
        with self._lock:
            if row["id"] in self.ids:
                return
            self.ids.add(row["id"])
            self._handle.write(dumps(row) + "\n")
            self._handle.flush()

    def close(self) -> None:
        self._handle.close()


class _AttemptRecorder:
    """Wraps the intent parser, transparently, to remember how many attempts the last parse of this thread took.

    The solver does not report attempts in its trace, so the attempt count is read here.
    """

    def __init__(self, parser: Any):
        self._parser = parser
        self._local = threading.local()

    def parse(self, question: str, choices: dict[str, list[str]] | None = None, **kwargs: Any) -> ParsedIntent:
        result = self._parser.parse(question, choices=choices, **kwargs)
        self._local.attempts = getattr(result, "attempts", None)
        return result

    def reset(self) -> None:
        self._local.attempts = None

    def attempts(self) -> int | None:
        return getattr(self._local, "attempts", None)


class _QuestionBoundParser:
    """Gives the intent parser the id and shop of the question being solved, which few-shot retrieval needs.

    The solver calls parse(question, choices) and does not pass ids, so SolverPool binds each question before
    solving it. The binding is per thread: each thread solves one question at a time with its own solvers.
    A parse of any other text is refused, so retrieval can never run without the ids its leakage rules need.
    """

    def __init__(self, parser: Any):
        self._parser = parser
        self._local = threading.local()

    def bind(self, question: Question | None) -> None:
        self._local.question = question

    def parse(self, question: str, choices: dict[str, list[str]] | None = None) -> ParsedIntent:
        bound = getattr(self._local, "question", None)
        if bound is None or bound.question != question:
            raise RuntimeError("intent parse for a question that is not the bound question")
        return self._parser.parse(question, choices=choices, question_id=bound.id, business_id=bound.business_id)


def _shop_index_provider(out: Path, build: Callable[[Any], Any]) -> Callable[[str, str | None], str]:
    """(question, business_id) -> build(connection).block(question), one index per shop built on first use."""
    import threading
    lock, indexes = threading.Lock(), {}

    def provider(question: str, business_id: str | None) -> str:
        if not business_id:
            return ""
        with lock:
            index = indexes.get(business_id)
            if index is None:
                path = out / "sem" / f"{business_id}.sqlite"
                if not path.exists():
                    return ""
                index = build(connect_read_only(path))
                indexes[business_id] = index
        return index.block(question)

    return provider


def _value_link_provider(out: Path) -> Callable[[str, str | None], str]:
    """(question, business_id) -> the stored values the question likely names, from that shop's built layer.
    One index per shop, built on first use from a read-only connection."""
    import threading
    from bizreason.semantic.value_linking import ValueIndex
    lock, indexes = threading.Lock(), {}

    def provider(question: str, business_id: str | None) -> str:
        if not business_id:
            return ""
        with lock:
            index = indexes.get(business_id)
            if index is None:
                path = out / "sem" / f"{business_id}.sqlite"
                if not path.exists():
                    return ""
                index = ValueIndex.from_connection(connect_read_only(path))
                indexes[business_id] = index
        return index.block(question)

    return provider


def make_intent_parser(model: Any, profile: str, examples: Callable[..., str] | None = None,
                       reasoning: bool = False, definitions: Callable[[str], str] | None = None,
                       value_links: Callable[[str, str | None], str] | None = None,
                       column_links: Callable[[str, str | None], str] | None = None) -> IntentParser:
    """IntentParser with the prompt profile, optional few-shot examples and optional short reasoning.

    The default profile without examples or reasoning is passed as the old, keyword-free call.
    """
    if definitions is not None or value_links is not None or column_links is not None:
        return IntentParser(model, profile=profile, examples=examples, reasoning=reasoning, definitions=definitions,
                            value_links=value_links, column_links=column_links)
    if reasoning:
        try:
            return IntentParser(model, profile=profile, examples=examples, reasoning=True)
        except TypeError as exc:
            raise ValueError(f"--reasoning on needs an IntentParser that accepts reasoning=; "
                             f"this IntentParser does not ({exc})") from exc
    if examples is not None:
        try:
            return IntentParser(model, profile=profile, examples=examples)
        except TypeError as exc:
            raise ValueError(f"--fewshot-bank needs an IntentParser that accepts examples=; "
                             f"this IntentParser does not ({exc})") from exc
    if profile == "default":
        return IntentParser(model)
    try:
        return IntentParser(model, profile=profile)
    except TypeError as exc:
        raise ValueError(f"--prompt-profile {profile!r} needs an IntentParser that accepts profile=; "
                         f"this IntentParser does not ({exc})") from exc


def load_answer_gate(path: Path | None) -> AnswerGate | None:
    if path is None:
        return None
    return AnswerGate(json.loads(Path(path).read_text(encoding="utf-8")))


def _trace_row(question: Question, trace: Any, attempts: int | None, seconds: float) -> dict[str, Any]:
    intent = asdict(trace.intent) if trace.intent is not None else None
    windows = [[window.role, window.start.isoformat(), window.end_exclusive.isoformat()] for window in trace.windows]
    row = {"id": question.id, "business_id": question.business_id, "question": question.question,
            "decision": trace.decision, "reason": trace.reason, "intent": intent, "windows": windows,
            "attempts": attempts, "seconds": seconds}
    if getattr(trace, "semantic_contract", None) is not None:
        row["semantic_contract"] = trace.semantic_contract
    if getattr(trace, "plan", None) is not None:
        row["plan"] = trace.plan
    return row


class GatedModel:
    """Thin proxy around a model client: generate_json holds one slot of the shared gate for the whole call.

    The annotator and the intent parser only call generate_json. TrammayClient._request is called inside it, so
    gating generate_json gates each HTTP request exactly once.
    """

    def __init__(self, client: Any, gate: Any):
        self._client = client
        self._gate = gate

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]:
        return self._gated("generate_json", system, prompt, timeout=timeout)

    def reask_json(self, system: str, prompt: str, previous: dict[str, Any], feedback: str,
                   *, timeout: float) -> dict[str, Any]:
        return self._gated("reask_json", system, prompt, previous, feedback, timeout=timeout)

    def _gated(self, method: str, *args: Any, timeout: float, deadline: float | None = None) -> dict[str, Any]:
        call_deadline = time.monotonic() + timeout
        if deadline is not None:
            call_deadline = min(call_deadline, deadline)
        remaining = call_deadline - time.monotonic()
        if remaining <= 0 or not self._gate.acquire(timeout=remaining):
            raise TimeoutError("model gate deadline exhausted")
        try:
            remaining = call_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("model request deadline exhausted")
            return getattr(self._client, method)(*args, timeout=remaining)
        finally:
            self._gate.release()

    def close(self) -> None:
        self._client.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class DeadlineGatedModel(GatedModel):
    """Bound both semaphore waiting and the request by the experiment's wall deadline."""

    def __init__(self, client: Any, gate: Any, deadline: float):
        super().__init__(client, gate)
        self._deadline = deadline

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]:
        return self._gated("generate_json", system, prompt, timeout=timeout, deadline=self._deadline)

    def reask_json(self, system: str, prompt: str, previous: dict[str, Any], feedback: str,
                   *, timeout: float) -> dict[str, Any]:
        return self._gated("reask_json", system, prompt, previous, feedback,
                           timeout=timeout, deadline=self._deadline)


class SolverPool:
    """Answers questions. Each thread keeps its own read-only connection and solver per shop."""

    def __init__(self, parser: IntentParser, temporal: TemporalResolver, paths: dict[str, Path], *,
                 traces: bool = False, bind_questions: bool = False, answer_gate: AnswerGate | None = None):
        self._answer_gate = answer_gate
        self._recorder = _AttemptRecorder(parser) if traces else None
        self._parser = self._recorder if traces else parser
        self._binder = _QuestionBoundParser(self._parser) if bind_questions else None
        if self._binder is not None:
            self._parser = self._binder
        self._traces = traces
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
        if self._recorder is not None:
            self._recorder.reset()
        if self._binder is not None:
            self._binder.bind(question)
        try:
            try:
                prediction, trace = self._solver(question.business_id, path).solve(question)
                gated_answer = None
                if self._answer_gate is not None:
                    allowed, gate_key = self._answer_gate.allows(
                        asdict(trace.intent) if trace.intent is not None else None,
                        _trace_row(question, trace, None, 0.0), prediction.type.value)
                    if not allowed:
                        gated_answer = prediction.answer
                        prediction = Prediction(id=question.id, type=AnswerType.ABSTAIN,
                                                reason=f"answer gate: {gate_key}")
                        trace.decision = "answer_gate"
                line = submission_line(prediction)
            except Exception as exc:  # one bad question must never stop the run
                return _abstain(question, f"error: {type(exc).__name__}", "error", started, checkpoint=True)
        finally:
            if self._binder is not None:
                self._binder.bind(None)
        seconds = _elapsed(started)
        row = None
        if self._traces:
            attempts = self._recorder.attempts() if self._recorder is not None else None
            row = _trace_row(question, trace, attempts, seconds)
            if gated_answer is not None:
                row["gated_answer"] = gated_answer
        return Outcome(question.id, trace.decision, seconds, line, checkpoint=True,
                       finished=time.monotonic(), trace=row)

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
    parser.add_argument("--rulebook-params", choices=["on", "off"], default="on",
                        help="read shifts, walk-in names, fees, debt methods and the Tet window from the rulebook")
    parser.add_argument("--rulebook-context", choices=rulebook_params.CONTEXT_MODES, default="cues",
                        help="parameter extraction context: lexical cues (default), or full document for ablation")
    parser.add_argument("--rulebook-validation-retries", type=int, choices=(0, 1), default=0,
                        help="bounded validator-feedback repair for rulebook parameters (default off)")
    parser.add_argument("--model-identity-file", type=Path,
                        help="public verified model metadata used to identify rulebook extraction caches")
    parser.add_argument("--data", required=True, type=Path, help="test root: questions.jsonl, rulebook.md, data/<biz>")
    parser.add_argument("--out", required=True, type=Path, help="output and cache directory")
    parser.add_argument("--business", nargs="+", action="extend", metavar="ID", help="only these business ids")
    parser.add_argument("--limit", type=int, help="only the first N questions after the business filter")
    parser.add_argument("--workers", type=int, default=4, help="parallel question threads, shared by all shops")
    parser.add_argument("--build-workers", type=int, default=DEFAULT_BUILD_WORKERS,
                        help="shops built in parallel, one process each")
    parser.add_argument("--max-model-concurrency", type=int, default=DEFAULT_MAX_MODEL_CONCURRENCY,
                        help="model requests in flight across builds and question parsing")
    parser.add_argument("--connection", required=True, type=Path, help="JSON with model and bearer_token")
    parser.add_argument("--endpoint-file", type=Path, help="JSON with base_url, re-read on tunnel outage")
    parser.add_argument("--base-url", help="model endpoint when no --endpoint-file is given")
    parser.add_argument("--total-seconds", type=float, default=DEFAULT_TOTAL_SECONDS,
                        help="wall budget; questions after it (minus a 5 minute margin) abstain, and no new shop starts")
    parser.add_argument("--outage-wait", type=float, default=DEFAULT_OUTAGE_WAIT,
                        help="seconds to wait for the endpoint to come back during an outage")
    parser.add_argument("--aliases", choices=["off", "on"], default="off",
                        help="use the header alias list during ingestion (default off: it hurts on unseen headers)")
    parser.add_argument("--no-alias", dest="aliases", action="store_const", const="off",
                        help="deprecated: same as --aliases off")
    parser.add_argument("--project-shops", type=int, default=DEFAULT_PROJECT_SHOPS)
    parser.add_argument("--project-questions", type=int, default=DEFAULT_PROJECT_QUESTIONS)
    parser.add_argument("--conventions", choices=("off", "on"), default="on",
                        help="read shop README conventions with the model (checked on the data) when building")
    parser.add_argument("--phrase-rules", choices=("off", "on"), default="on",
                        help="phrase-triggered repairs learned from seen questions (for ablation)")
    parser.add_argument("--column-measures", choices=("off", "on"), default="on",
                        help="offer the parser the shop's kept columns (column_catalog) as measures and filters")
    parser.add_argument("--value-linking", choices=("off", "on"), default="on",
                        help="show the parser the stored values each question likely names (per-shop value index)")
    parser.add_argument("--plan-fallback", choices=("off", "on"), default="off",
                        help="when the typed intent cannot express a question, answer from a typed query plan")
    parser.add_argument("--time-reader", choices=("off", "on"), default="off",
                        help="when the temporal rules find no window, let the model read periods; code checks every date")
    parser.add_argument("--plan-binding-retries", type=int, choices=(0, 1), default=0,
                        help="experimental: one bounded identifier-only plan repair (default off)")
    parser.add_argument("--plan-definitions", choices=("eligible", "all"), default="eligible",
                        help="runtime definition eligibility for the plan path; requires rulebook-definitions on")
    parser.add_argument("--semantic-contracts", choices=("off", "on"), default="off",
                        help="experimental: compile documented metrics and validate question contracts before intent routing")
    parser.add_argument("--sql-fallback", choices=("off", "on"), default="off",
                        help="when the typed intent cannot express a question, answer from two agreeing SQL queries")
    parser.add_argument("--rule-terms", choices=("off", "on"), default="on",
                        help="translate the rulebook's ranking terms once and apply them to ranking questions")
    parser.add_argument("--consensus", choices=("off", "on"), default="off",
                        help="parse each question twice with different few-shot examples; abstain when they disagree")
    parser.add_argument("--rulebook-definitions", choices=("off", "on"), default="on",
                        help="show the intent parser the rulebook definitions of ranking terms the question uses")
    parser.add_argument("--reasoning", choices=("off", "on"), default="off",
                        help="ask the intent model for a short analysis (first JSON key) before the intent")
    parser.add_argument("--prompt-profile", choices=PROMPT_PROFILES, default="default",
                        help="intent prompt profile passed to every IntentParser built for question solving")
    parser.add_argument("--answer-gate", type=Path, default=None, metavar="PATH",
                        help="fitted answer-gate JSON (scripts/fit_answer_gate.py); abstain on low-precision solve paths. "
                             "Default: off")
    parser.add_argument("--traces", action="store_true",
                        help="also write traces.jsonl: one row per solved question with its intent and windows")
    parser.add_argument("--questions-file", type=Path, metavar="PATH",
                        help="questions.jsonl to use instead of <data>/questions.jsonl (same format)")
    parser.add_argument("--fewshot-bank", type=Path, metavar="PATH",
                        help="few-shot bank jsonl (solved questions with intents); the k most similar, from other "
                             "shops, are shown to the intent parser for each question")
    parser.add_argument("--fewshot-k", type=int, default=4, metavar="N",
                        help="number of few-shot examples per question when --fewshot-bank is given (default 4)")
    parser.add_argument("--fewshot-groups", type=Path,
                        help="JSON mapping business ids to explicit industry/source-family group tags")
    parser.add_argument("--fewshot-exclude-groups", nargs="+", default=[],
                        help="remove these groups before fitting the few-shot index")
    parser.add_argument("--fewshot-exclude-businesses", nargs="+", default=[],
                        help="remove all examples of these shops before fitting the few-shot index")
    return parser.parse_args(argv)


def _client_factory(args: argparse.Namespace) -> Callable[[int, float], TrammayClient]:
    """Validates the connection file and endpoint, then returns make(max_tokens, timeout) for a new client.

    The token is read here and never logged or stored; a build process reads the file again for its own client.
    """
    creds = json.loads(args.connection.read_text(encoding="utf-8-sig"))
    model = creds.get("model") if isinstance(creds, dict) else None
    token = creds.get("bearer_token") if isinstance(creds, dict) else None
    if not isinstance(model, str) or not model or not isinstance(token, str) or not token:
        raise ValueError("connection file must hold a non-empty model and bearer_token")
    resolver = endpoint_file_resolver(args.endpoint_file) if args.endpoint_file else None
    base_url = args.base_url or (resolver() if resolver else None)
    if not base_url:
        raise ValueError("no model base URL: pass --base-url or an --endpoint-file that holds base_url")

    def make(max_tokens: int, timeout: float) -> TrammayClient:
        settings = Settings(base_url=base_url, model=model, api_key=token,
                            max_output_tokens=max_tokens, request_timeout=timeout)
        return TrammayClient(settings, endpoint_resolver=resolver, outage_wait_seconds=args.outage_wait)

    return make


def _rulebook_model_identity(model: Any, path: Path | None) -> dict[str, Any]:
    """Use only public generation metadata; unknown identities cannot share caches across runs."""
    configured_model = getattr(getattr(model, "settings", None), "model", None)
    if path is None:
        return {"model": configured_model, "unverified_run": uuid.uuid4().hex}
    metadata = json.loads(path.read_text(encoding="utf-8"))
    required = ("model", "revision", "weights_sha256", "quantization", "compute_dtype")
    if (not isinstance(metadata, dict) or metadata.get("weights_verified") is not True
            or not all(isinstance(metadata.get(name), str) and metadata[name].strip() for name in required)
            or configured_model is None or metadata["model"] != configured_model):
        raise ValueError("model identity metadata is incomplete, unverified, or differs from the configured model")
    allowed = (*required, "parameters", "seed", "torch", "cuda", "transformers", "bitsandbytes", "batch_max",
               "server_source_sha256")
    identity = {name: metadata[name] for name in allowed if name in metadata}
    settings = getattr(model, "settings", None)
    for name in ("max_output_tokens", "api_style"):
        value = getattr(settings, name, None)
        if value is not None:
            identity[name] = value
    return identity


def make_annotation_client(args: argparse.Namespace) -> TrammayClient:
    return _client_factory(args)(ANNOTATION_MAX_TOKENS, ANNOTATION_TIMEOUT)


def make_intent_client(args: argparse.Namespace) -> TrammayClient:
    return _client_factory(args)(INTENT_MAX_TOKENS, INTENT_TIMEOUT)


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


def build_order(questions: list[Question]) -> list[str]:
    """Shop ids with more questions first. Equal counts keep first-seen order (the sort is stable)."""
    counts = Counter(question.business_id for question in questions)
    return sorted(counts, key=lambda biz: -counts[biz])


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
def timed(log: Any, biz: str, stage: str) -> Iterator[None]:
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
    with timed(ctx.log, biz, "conventions"):
        from bizreason.semantic import conventions
        directives = conventions.load_or_extract(annotator.model, context, ctx.out / "conventions" / f"{biz}.json")
        directives = conventions.attach_value_readings(annotator.model, context, directives,
                                                       ctx.out / "conventions" / f"{biz}_values.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".building")
    with timed(ctx.log, biz, "build"):
        build(context, records, partial, status_map=status_map, line_kinds=line_kinds,
              value_groups=value_groups, model_failures=annotator.take_failures(), conventions=directives)
        partial.replace(target)
    return target


def _init_build_worker(gate: Any) -> None:
    global _MODEL_GATE
    _MODEL_GATE = gate


def build_shop_in_worker(job: ShopJob) -> ShopBuild:
    """Builds one shop inside a build process: its own client, annotator (so failures stay with this shop),
    and builder. Returns the shop's database path and its timing rows."""
    with PathPolicy(mode="competition", allowed_roots=[job.out], forbidden_roots=[job.data]):
        client = make_annotation_client(job.args)
        try:
            builder, _rules, _temporal = build_v2_context(job.data, job.out / "cache")
            rows = StageRows()
            annotator = Annotator(GatedModel(client, _MODEL_GATE), workers=ANNOTATOR_WORKERS)
            ctx = RunContext(job.data, job.out, builder, annotator, rows)
            path = build_shop(job.biz, ctx)
        finally:
            try:
                client.close()
            except Exception:
                pass
    return ShopBuild(job.biz, path, rows.rows)


def build_and_answer(args: argparse.Namespace, data: Path, out: Path, questions: list[Question], parser: IntentParser,
                     temporal: TemporalResolver, gate: Any, deadline: float, t0: float, log: RunLog,
                     record: Callable[[Outcome], None]) -> None:
    """Build shops in a process pool; answer each shop's questions as soon as its database is ready.

    Builds start in build_order, at most args.build_workers at a time, and none starts after the deadline. Shops
    that never build still get their questions answered (as no_shop or budget abstains). All questions share one
    thread pool, and the parent calls `record` for each finished answer.
    """
    by_shop: dict[str, list[Question]] = {}
    for question in questions:
        by_shop.setdefault(question.business_id, []).append(question)
    waiting = deque(build_order(questions))
    paths: dict[str, Path] = {}
    solvers = SolverPool(parser, temporal, paths, traces=args.traces, bind_questions=args.fewshot_bank is not None,
                         answer_gate=load_answer_gate(args.answer_gate))
    builds: dict[Future[ShopBuild], str] = {}
    answers: set[Future[Outcome]] = set()

    def start_builds(builder: ProcessPoolExecutor) -> None:
        while waiting and len(builds) < args.build_workers and time.monotonic() <= deadline:
            biz = waiting.popleft()
            builds[builder.submit(build_shop_in_worker, ShopJob(biz, data, out, args))] = biz

    def answer_shop(biz: str, answerer: ThreadPoolExecutor) -> None:
        for question in by_shop[biz]:
            answers.add(answerer.submit(solvers.solve, question, deadline))

    # Fork, not spawn: build processes inherit the parent's state (and test fakes) and the fork-context gate.
    context = multiprocessing.get_context("fork")

    def answer_unbuilt(answerer: ThreadPoolExecutor) -> None:
        if not builds:  # the deadline stopped the builds; the shops still waiting never build
            while waiting:
                answer_shop(waiting.popleft(), answerer)

    try:
        with ProcessPoolExecutor(max_workers=args.build_workers, mp_context=context,
                                 initializer=_init_build_worker, initargs=(gate,)) as builder, \
                ThreadPoolExecutor(max_workers=args.workers) as answerer:
            start_builds(builder)
            answer_unbuilt(answerer)
            while builds or answers:
                done, _ = wait([*builds, *answers], return_when=FIRST_COMPLETED)
                for future in done:
                    if future in builds:
                        biz = builds.pop(future)
                        try:
                            shop = future.result()
                        except Exception as exc:  # the shop's questions abstain; the other shops still run
                            print(f"shop {biz}: build failed ({type(exc).__name__})", flush=True)
                        else:
                            paths[biz] = shop.path
                            for row in shop.rows:
                                log.add(row)
                            log.add({"biz": biz, "ready_at": _elapsed(t0)})
                        answer_shop(biz, answerer)
                    else:
                        answers.discard(future)
                        record(future.result())
                start_builds(builder)
                answer_unbuilt(answerer)
    finally:
        solvers.close()


def write_final(path: Path, questions: list[Question], lines: dict[str, str]) -> None:
    """Every selected question id once, sorted; an id with no answer becomes an abstain."""
    body = []
    for question_id in sorted({question.id for question in questions}):
        line = lines.get(question_id)
        if line is None:
            line = submission_line(Prediction(id=question_id, type=AnswerType.ABSTAIN, reason="not answered"))
        body.append(line + "\n")
    _write_text_atomic(writable_path(path), "".join(body))


def format_summary(rows: list[dict[str, Any]], workers: int, project_shops: int, project_questions: int, *,
                   build_workers: int = 1, wall_seconds: float | None = None) -> str:
    """Stage totals, the actual wall time, and the projection for the given shop and question counts.

    With wall-clock offsets in the rows (ready_at per shop, end_at per question), builds and questions overlap:
      question_part = project_questions * question_wall
      build_not_overlapped = max(first shop ready, project_shops * shop_cost / build_workers - question_part)
      projected = question_part + build_not_overlapped
    question_wall is the steady-state seconds per question: from when the last shop was ready (or the first
    question started, if later) to the last finish, divided by the questions finished in that window.
    Rows without offsets (older timing files) use the sequential estimate:
      projected = project_shops * shop_cost + project_questions * mean_question_seconds / workers.
    """
    totals = {stage: 0.0 for stage in STAGES}
    shops: set[str] = set()
    ready_at: list[float] = []
    question_seconds: list[float] = []
    spans: list[tuple[float, float]] = []  # (start, end) offsets of attempted questions
    for row in rows:
        if "stage" in row:
            totals[row["stage"]] = totals.get(row["stage"], 0.0) + row["seconds"]
            if row["stage"] == "build":
                shops.add(row["biz"])
        elif "ready_at" in row:
            ready_at.append(row["ready_at"])
        elif row["decision"] not in NOT_ATTEMPTED:
            question_seconds.append(row["seconds"])
            if "end_at" in row:
                spans.append((row["end_at"] - row["seconds"], row["end_at"]))
    shop_total = sum(totals.values())
    per_shop = shop_total / len(shops) if shops else 0.0
    mean_question = statistics.fmean(question_seconds) if question_seconds else 0.0
    if ready_at and spans and len(spans) == len(question_seconds):
        question_wall, basis = _steady_question_wall(spans, ready_at)
        first_ready = min(ready_at)
        question_part = project_questions * question_wall
        build_part = project_shops * per_shop / max(build_workers, 1)
        not_overlapped = max(first_ready, build_part - question_part)
        projected = question_part + not_overlapped
        method = (f"overlapped: {question_wall:.3f} s per question ({basis}) x questions, plus build not overlapped "
                  f"{not_overlapped:.0f} s = max(first shop ready {first_ready:.0f} s, shops x shop cost / build "
                  f"workers - question part)")
    else:
        projected = project_shops * per_shop + project_questions * mean_question / max(workers, 1)
        method = ("sequential estimate (no wall-clock offsets in timing): shops x shop cost + questions x mean "
                  "seconds / workers")
    lines = ["stage            total_s"]
    lines += [f"{stage:<16} {totals.get(stage, 0.0):>9.1f}" for stage in STAGES]
    lines.append(f"shops built this run: {len(shops)}, shop cost per shop {per_shop:.1f} s (sum of stages)")
    lines.append(f"questions attempted: {len(question_seconds)}, mean seconds per question: {mean_question:.2f} "
                 f"(workers={max(workers, 1)}, build_workers={max(build_workers, 1)})")
    if wall_seconds is not None:
        lines.append(f"actual wall time: {wall_seconds:.1f} s (~{wall_seconds / 3600:.2f} h)")
    lines.append(f"projected for {project_shops} shops and {project_questions} questions: {projected:.0f} s "
                 f"(~{projected / 3600:.2f} h)")
    lines.append(f"  {method}")
    return "\n".join(lines)


def _steady_question_wall(spans: list[tuple[float, float]], ready_at: list[float]) -> tuple[float, str]:
    """Seconds per question in steady state, from question spans (start, end) and shop ready offsets."""
    first_start = min(start for start, _ in spans)
    last_end = max(end for _, end in spans)
    window_start = max(max(ready_at), first_start)
    finished = [end for _, end in spans if end > window_start]
    if finished:
        return (max(finished) - window_start) / len(finished), "steady state after the last shop was ready"
    return (last_end - first_start) / len(spans), "whole question phase; no question finished after the last shop"


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
    if args.build_workers < 1:
        raise ValueError("--build-workers must be at least 1")
    if args.max_model_concurrency < 1:
        raise ValueError("--max-model-concurrency must be at least 1")
    fewshot: FewShotBank | None = None
    fewshot_original_size = 0
    if (args.fewshot_groups or args.fewshot_exclude_groups or args.fewshot_exclude_businesses) and args.fewshot_bank is None:
        raise ValueError("few-shot fold options require --fewshot-bank")
    if args.fewshot_bank is not None:
        if args.fewshot_k < 1:
            raise ValueError("--fewshot-k must be at least 1")
        fewshot = FewShotBank.load(args.fewshot_bank)
        fewshot_original_size = len(fewshot)
        groups = json.loads(args.fewshot_groups.read_text(encoding="utf-8")) if args.fewshot_groups else None
        if groups is not None and (not isinstance(groups, dict) or any(
            not isinstance(tags, list) or not tags or any(not isinstance(tag, str) or not tag for tag in tags)
            for tags in groups.values()
        )):
            raise ValueError("--fewshot-groups must map business ids to nonempty lists of group tags")
        if args.fewshot_exclude_businesses or args.fewshot_exclude_groups:
            fewshot = fewshot.excluding(businesses=args.fewshot_exclude_businesses,
                                       groups=args.fewshot_exclude_groups, business_groups=groups)
        if len(fewshot) == 0:
            raise ValueError(f"--fewshot-bank {args.fewshot_bank} has no examples")
    questions_path = args.questions_file if args.questions_file is not None else data / "questions.jsonl"
    questions = load_questions(questions_path, args.business, args.limit)
    if not questions:
        raise ValueError("No questions selected")
    out.mkdir(parents=True, exist_ok=True)
    rulebook = data / "rulebook.md"
    manifest: dict[str, Any] = {
        "git_commit": git_commit(),
        "arguments": {key: _plain(value) for key, value in vars(args).items() if not _is_secret_arg(key)},
        "prompt_profile": args.prompt_profile,
        "reasoning": args.reasoning,
        "rulebook_definitions": args.rulebook_definitions,
        "consensus": args.consensus,
        "rule_terms": args.rule_terms,
        "sql_fallback": args.sql_fallback,
        "plan_fallback": args.plan_fallback,
        "time_reader": args.time_reader,
        "plan_binding_retries": args.plan_binding_retries,
        "plan_definitions": args.plan_definitions,
        "semantic_contracts": args.semantic_contracts,
        "value_linking": args.value_linking,
        "column_measures": args.column_measures,
        "conventions": args.conventions,
        "phrase_rules": args.phrase_rules,
        "rulebook_context": args.rulebook_context,
        "rulebook_validation_retries": args.rulebook_validation_retries,
        "model_identity_sha256": file_hash(args.model_identity_file) if args.model_identity_file else None,
        "fewshot_bank": str(args.fewshot_bank) if fewshot is not None else None,
        "fewshot_bank_sha256": file_hash(args.fewshot_bank) if fewshot is not None else None,
        "fewshot_k": args.fewshot_k if fewshot is not None else None,
        "fewshot_examples_before_exclusion": fewshot_original_size,
        "fewshot_examples_after_exclusion": len(fewshot) if fewshot is not None else 0,
        "fewshot_groups_sha256": file_hash(args.fewshot_groups) if args.fewshot_groups else None,
        "questions_path": str(questions_path),
        "rulebook_path": str(rulebook),
        "rulebook_sha256": file_hash(rulebook),
        "started_at": _now(),
        "finished_at": None,
    }
    atomic_json(out / "manifest.json", manifest)

    checkpoint = Checkpoint(out / "submission.jsonl")
    log = RunLog(out / "timing.jsonl")
    traces = TraceWriter(out / "traces.jsonl", set(checkpoint.lines)) if args.traces else None
    intent_client: TrammayClient | None = None
    auxiliary_clients: list[Any] = []
    plan_client: Any = None
    saved_roles: dict[str, Any] | None = None
    try:
        if args.aliases == "off":
            saved_roles = dict(schema.ROLE_LOOKUP)
            schema.ROLE_LOOKUP.clear()
        from bizreason.semantic import conventions as _conventions, solver as _solver
        _solver.set_contract_runtime(None)
        # Separate invocations in the same process start from the declared defaults,
        # even when extraction is disabled or the next root has no rulebook.
        rulebook_params.set_active(rulebook_params.DEFAULTS)
        rule_terms.set_active(())
        _conventions.ENABLED = args.conventions == "on"
        _solver.PHRASE_RULES = args.phrase_rules == "on"
        make_client = _client_factory(args)  # validates the connection file and endpoint before any build starts
        intent_client = make_client(
            INTENT_REASONING_MAX_TOKENS if args.reasoning == "on" else INTENT_MAX_TOKENS, INTENT_TIMEOUT)
        _, _rules, temporal = build_v2_context(data, out / "cache")
        rulebook_params.set_rulebook_text(rulebook.read_text(encoding="utf-8") if rulebook.exists() else None)
        if args.rulebook_params == "on" and rulebook.exists():
            # Business parameters (shifts, walk-in names, fees, debt methods, Tết) come from this test set's own
            # rulebook. Set before any shop build so forked build workers inherit them; defaults when unstated.
            parameter_client = make_client(ANNOTATION_MAX_TOKENS, ANNOTATION_TIMEOUT)
            extraction_diagnostics: dict[str, Any] = {}
            try:
                identity = _rulebook_model_identity(parameter_client, args.model_identity_file)
                params, taken = rulebook_params.load_or_extract(
                    rulebook, parameter_client, out / "rulebook_params.json", context_mode=args.rulebook_context,
                    validation_retries=args.rulebook_validation_retries, model_identity=identity,
                    diagnostics=extraction_diagnostics)
            finally:
                if parameter_client is not intent_client:
                    parameter_client.close()
            atomic_json(out / "rulebook_extraction.json", extraction_diagnostics)
            rulebook_params.set_active(params)
            print(f"rulebook params taken from the rulebook: {taken or 'none (defaults)'}", flush=True)
        if args.rule_terms == "on" and rulebook.exists():
            # Ranking terms the rulebook defines, translated once into measure / grouping / minimum orders.
            terms = rule_terms.load_or_extract(
                rulebook, make_client(ANNOTATION_MAX_TOKENS, ANNOTATION_TIMEOUT), out / "rule_terms.json")
            rule_terms.set_active(terms)
            print(f"rulebook ranking terms translated: {len(terms)}", flush=True)
        deadline = t0 + args.total_seconds - BUDGET_MARGIN_SECONDS
        gate = multiprocessing.get_context("fork").BoundedSemaphore(args.max_model_concurrency)
        examples = fewshot.provider(args.fewshot_k) if fewshot is not None else None
        alternate = fewshot.provider(args.fewshot_k, alternate=True) if fewshot is not None else None
        definitions = (rulebook_definitions.provider(rulebook.read_text(encoding="utf-8"))
                       if args.rulebook_definitions == "on" and rulebook.exists() else None)
        value_links = _value_link_provider(out) if args.value_linking == "on" else None
        from bizreason.semantic.column_links import ColumnIndex
        column_links = _shop_index_provider(out, ColumnIndex.from_connection) if args.column_measures == "on" else None
        intent_parser = make_intent_parser(GatedModel(intent_client, gate), args.prompt_profile, examples,
                                           reasoning=args.reasoning == "on", definitions=definitions,
                                           value_links=value_links, column_links=column_links)
        if args.plan_fallback == "on":
            from bizreason.semantic.plan import PlanFallback
            from bizreason.semantic.solver import set_fallback
            from bizreason.semantic.value_linking import ValueIndex
            plan_definitions = (rulebook_definitions.provider(rulebook.read_text(encoding="utf-8"),
                                include_all=args.plan_definitions == "all")
                                if args.rulebook_definitions == "on" and rulebook.exists() else None)
            plan_client = DeadlineGatedModel(make_client(INTENT_REASONING_MAX_TOKENS, INTENT_TIMEOUT), gate, deadline)
            auxiliary_clients.append(plan_client)
            set_fallback(PlanFallback(plan_client, temporal, definitions=plan_definitions,
                                      value_index=ValueIndex.from_connection,
                                      binding_retries=args.plan_binding_retries))
        if args.time_reader == "on":
            from bizreason.semantic.solver import set_time_reader
            from bizreason.semantic.time_reader import TimeReader
            time_client = DeadlineGatedModel(make_client(300, INTENT_TIMEOUT), gate, deadline)
            auxiliary_clients.append(time_client)
            set_time_reader(TimeReader(time_client, rulebook_text=rulebook.read_text(encoding="utf-8")
                                       if rulebook.exists() else None, timeout=INTENT_TIMEOUT))
        if args.sql_fallback == "on":
            from bizreason.semantic.sql_fallback import SqlFallback
            from bizreason.semantic.solver import set_fallback
            set_fallback(SqlFallback(GatedModel(make_client(ANNOTATION_MAX_TOKENS, INTENT_TIMEOUT), gate),
                                     rulebook.read_text(encoding="utf-8") if rulebook.exists() else None))
        if args.consensus == "on":
            if alternate is None:
                raise ValueError("--consensus on needs --fewshot-bank (the second parse reads other examples)")
            second = make_intent_parser(GatedModel(intent_client, gate), args.prompt_profile, alternate,
                                        reasoning=args.reasoning == "on", definitions=definitions)
            intent_parser = ConsensusParser(intent_parser, second)
        if args.semantic_contracts == "on":
            from bizreason.semantic.contract_runtime import ContractRuntime
            extraction_client = DeadlineGatedModel(make_client(3000, 120.0), gate, deadline)
            question_client = DeadlineGatedModel(make_client(INTENT_REASONING_MAX_TOKENS, 120.0), gate, deadline)
            auxiliary_clients.extend([extraction_client, question_client])
            _solver.set_contract_runtime(ContractRuntime(
                extraction_client, question_client, temporal,
                rulebook.read_text(encoding="utf-8") if rulebook.exists() else "", out / "metrics",
                model_identity=intent_client.settings.model, deadline=deadline))

        pending = [question for question in questions if question.id not in checkpoint.lines]
        lines = dict(checkpoint.lines)
        done = [0]

        def record(outcome: Outcome) -> None:
            log.add({"id": outcome.question_id, "seconds": outcome.seconds, "decision": outcome.decision,
                     "end_at": round(outcome.finished - t0, 3)})
            if outcome.checkpoint:
                checkpoint.append(outcome.question_id, outcome.line)
                if traces is not None and outcome.trace is not None:
                    traces.append(outcome.trace)
            lines[outcome.question_id] = outcome.line
            done[0] += 1
            print(f"[{done[0]}/{len(pending)}] {outcome.question_id}: {outcome.decision} ({outcome.seconds:.2f}s)",
                  flush=True)

        build_and_answer(args, data, out, pending, intent_parser, temporal, gate,
                         deadline, t0, log, record)
        write_final(out / "submission.final.jsonl", questions, lines)
    finally:
        from bizreason.semantic.solver import set_contract_runtime, set_fallback, set_time_reader
        set_contract_runtime(None)
        set_fallback(None)
        set_time_reader(None)
        if plan_client is not None:
            usage = getattr(plan_client, "usage", None)
            if usage is not None:
                manifest["plan_model_usage"] = {key: getattr(usage, key, None)
                                                for key in ("calls", "http_attempts")}
        for client in auxiliary_clients:
            client.close()
        checkpoint.close()
        log.close()
        if traces is not None:
            traces.close()
        if intent_client is not None:
            try:
                intent_client.close()
            except Exception:
                pass
        if saved_roles is not None:
            schema.ROLE_LOOKUP.clear()
            schema.ROLE_LOOKUP.update(saved_roles)

    manifest["finished_at"] = _now()
    manifest["elapsed_seconds"] = _elapsed(t0)
    atomic_json(out / "manifest.json", manifest)
    print(format_summary(log.rows, args.workers, args.project_shops, args.project_questions,
                         build_workers=args.build_workers, wall_seconds=_elapsed(t0)), flush=True)
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
    return Outcome(question.id, decision, _elapsed(started), line, checkpoint=checkpoint, finished=time.monotonic())


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
