"""bizreason.run_v2: end-to-end command on a tiny synthetic test folder, with a fake model client (no network)."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sqlite3
import threading
import time
from contextlib import closing
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bizreason import run_v2
from bizreason.contracts import AnswerType, Prediction, Question, TimeWindow
from bizreason.data import schema
from bizreason.formatting import SubmissionWriter
from bizreason.llm import ModelError
from bizreason.semantic.annotator import SCOPE
from bizreason.semantic.compiler import Intent
from bizreason.semantic.intent import SYSTEM, ParsedIntent
from bizreason.semantic.solver import SemanticSolver, SolveTrace
from bizreason.run_v2 import (
    Checkpoint,
    GatedModel,
    STAGES,
    ShopJob,
    build_order,
    build_shop_in_worker,
    format_summary,
    main,
    submission_line,
)
from bizreason.utils import PathPolicy

TOKEN = "TOKEN-DO-NOT-LEAK-7731"
RULEBOOK = "# Rules\n## 1. Orders\nEvery order counts once.\n"


class FakeClient:
    """Answers annotation calls from fixed replies and counts intent calls. Never touches the network."""

    def __init__(self) -> None:
        self.intent_calls = 0
        self.closed = False

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]:
        if system == SYSTEM:
            self.intent_calls += 1
            return {}  # no usable intent: every question abstains through the solver
        if system.startswith("Classify a "):
            return {"kind": "sales"}
        if system.startswith("Give the meaning"):
            # Column names in these tests are role names, so each column maps to itself when the role exists.
            return {column: column if column in SCOPE["sales"] else "other" for column in _columns_in(prompt)}
        return {}

    def close(self) -> None:
        self.closed = True


def _columns_in(prompt: str) -> list[str]:
    return [line[2:].split(" | ", 1)[0] for line in prompt.splitlines() if line.startswith("- ")]


def write_csv(path: Path, columns: list[str], rows: list[list[Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)


def make_test_root(root: Path) -> Path:
    shop = root / "data" / "shop_a"
    shop.mkdir(parents=True)
    (shop / "README.md").write_text("Orders and their lines, one row per line.\n", encoding="utf-8")
    write_csv(shop / "orders.csv", ["order_id", "ordered_at", "order_status", "order_total_due"],
              [["O1", "2026-03-01", "completed", "250000"], ["O2", "2026-03-02", "cancelled", "30000"]])
    write_csv(shop / "lines.csv", ["order_id", "product_name", "qty", "line_amount"],
              [["O1", "tea", "2", "250000"], ["O2", "coffee", "1", "30000"]])
    (root / "rulebook.md").write_text(RULEBOOK, encoding="utf-8")
    questions = [
        {"id": "q1", "business_id": "shop_a", "question": "How many orders?", "as_of_date": "2026-03-10"},
        {"id": "q2", "business_id": "shop_a", "question": "What is the revenue?", "as_of_date": "2026-03-10"},
    ]
    (root / "questions.jsonl").write_text("".join(json.dumps(q) + "\n" for q in questions), encoding="utf-8")
    return root


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    client = FakeClient()

    def factory(settings: Any, **_: Any) -> FakeClient:
        assert settings.api_key == TOKEN  # the token reaches the client, and only the client
        return client

    monkeypatch.setattr(run_v2, "TrammayClient", factory)
    return client


@pytest.fixture
def setup(tmp_path: Path) -> dict[str, Path]:
    root = make_test_root(tmp_path / "test_root")
    connection = tmp_path / "connection.json"
    connection.write_text(json.dumps({"model": "test-model", "bearer_token": TOKEN}), encoding="utf-8")
    endpoint = tmp_path / "endpoint.json"
    endpoint.write_text(json.dumps({"base_url": "http://127.0.0.1:9/v1"}), encoding="utf-8")
    return {"root": root, "connection": connection, "endpoint": endpoint, "out": tmp_path / "out"}


def command(setup: dict[str, Path], *extra: str) -> list[str]:
    return ["--data", str(setup["root"]), "--out", str(setup["out"]), "--connection", str(setup["connection"]),
            "--endpoint-file", str(setup["endpoint"]), "--workers", "2", *extra]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_end_to_end_run_writes_submission_timing_and_manifest(setup, fake_client) -> None:
    assert main(command(setup)) == 0
    out = setup["out"]

    submission = read_jsonl(out / "submission.jsonl")
    assert sorted(row["id"] for row in submission) == ["q1", "q2"]
    final = read_jsonl(out / "submission.final.jsonl")
    assert [row["id"] for row in final] == ["q1", "q2"]
    assert all(row["type"] in {"abstain", "unanswerable", "number", "percent", "string", "list", "table"} for row in final)

    timing = read_jsonl(out / "timing.jsonl")
    stages = {row["stage"] for row in timing if "stage" in row}
    assert set(STAGES) <= stages
    assert sorted(row["id"] for row in timing if "decision" in row) == ["q1", "q2"]
    assert (out / "sem" / "shop_a.sqlite").is_file()
    assert not (out / "sem" / "shop_a.building").exists()

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert "git_commit" in manifest
    assert manifest["rulebook_sha256"] == hashlib.sha256(setup["root"].joinpath("rulebook.md").read_bytes()).hexdigest()
    assert manifest["started_at"] and manifest["finished_at"]
    assert manifest["arguments"]["workers"] == 2
    assert not any("token" in key.lower() for key in manifest["arguments"])
    assert fake_client.closed  # the clients are closed in the finally block


def test_token_never_reaches_any_output_file(setup, fake_client) -> None:
    assert main(command(setup)) == 0
    for path in setup["out"].rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text(encoding="utf-8", errors="ignore"), path.name


def test_rerun_skips_answered_questions(setup, fake_client) -> None:
    assert main(command(setup)) == 0
    calls_after_first = fake_client.intent_calls
    assert calls_after_first > 0
    submission_before = (setup["out"] / "submission.jsonl").read_text(encoding="utf-8")
    timing_rows_before = len(read_jsonl(setup["out"] / "timing.jsonl"))

    assert main(command(setup)) == 0

    assert fake_client.intent_calls == calls_after_first
    assert (setup["out"] / "submission.jsonl").read_text(encoding="utf-8") == submission_before
    assert len(read_jsonl(setup["out"] / "timing.jsonl")) == timing_rows_before
    assert sorted(row["id"] for row in read_jsonl(setup["out"] / "submission.final.jsonl")) == ["q1", "q2"]


def test_question_that_raises_becomes_abstain_and_run_continues(setup, fake_client, monkeypatch) -> None:
    class RaisingSolver(SemanticSolver):
        def solve(self, question):
            if question.id == "q1":
                raise RuntimeError("boom")
            return super().solve(question)

    monkeypatch.setattr(run_v2, "SemanticSolver", RaisingSolver)
    assert main(command(setup)) == 0

    final = {row["id"]: row for row in read_jsonl(setup["out"] / "submission.final.jsonl")}
    assert final["q1"] == {"id": "q1", "type": "abstain", "reason": "error: RuntimeError"}
    assert "q2" in final
    submission = {row["id"] for row in read_jsonl(setup["out"] / "submission.jsonl")}
    assert submission == {"q1", "q2"}


def test_budget_exhausted_writes_abstain_for_remaining_questions(setup, fake_client) -> None:
    assert main(command(setup, "--total-seconds", "0")) == 0
    out = setup["out"]

    final = read_jsonl(out / "submission.final.jsonl")
    assert [row["id"] for row in final] == ["q1", "q2"]
    assert all(row["type"] == "abstain" and row["reason"] == "time budget exhausted" for row in final)
    assert (out / "submission.jsonl").read_text(encoding="utf-8") == ""  # not attempted, so not checkpointed
    assert fake_client.intent_calls == 0
    decisions = [row["decision"] for row in read_jsonl(out / "timing.jsonl") if "decision" in row]
    assert decisions == ["budget", "budget"]


@pytest.fixture
def seen_roles(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """ROLE_LOOKUP as it is when the shop context is built, i.e. inside the run."""
    seen: list[dict[str, Any]] = []
    real_build = run_v2.build_v2_context

    def recording_build(*args: Any, **kwargs: Any) -> Any:
        seen.append(dict(schema.ROLE_LOOKUP))
        return real_build(*args, **kwargs)

    monkeypatch.setattr(run_v2, "build_v2_context", recording_build)
    return seen


def test_aliases_default_off_clears_role_lookup_during_run_and_restores_after(setup, fake_client, seen_roles) -> None:
    before = dict(schema.ROLE_LOOKUP)
    assert before  # the alias list is populated, so clearing it is observable
    assert main(command(setup)) == 0
    assert seen_roles == [{}]
    assert dict(schema.ROLE_LOOKUP) == before
    manifest = json.loads((setup["out"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["arguments"]["aliases"] == "off"


def test_aliases_on_keeps_role_lookup(setup, fake_client, seen_roles) -> None:
    before = dict(schema.ROLE_LOOKUP)
    assert main(command(setup, "--aliases", "on")) == 0
    assert seen_roles == [before]
    assert dict(schema.ROLE_LOOKUP) == before
    manifest = json.loads((setup["out"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["arguments"]["aliases"] == "on"


def test_no_alias_is_accepted_as_deprecated_off(setup, fake_client, seen_roles) -> None:
    before = dict(schema.ROLE_LOOKUP)
    assert main(command(setup, "--no-alias")) == 0
    assert seen_roles == [{}]
    assert dict(schema.ROLE_LOOKUP) == before
    manifest = json.loads((setup["out"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["arguments"]["aliases"] == "off"


def test_aliases_rejects_unknown_value(setup) -> None:
    with pytest.raises(SystemExit):
        run_v2.parse_args(command(setup, "--aliases", "maybe"))


def test_submission_line_matches_submission_writer(tmp_path: Path) -> None:
    predictions = [
        Prediction(id="a", type=AnswerType.NUMBER, answer=12.5),
        Prediction(id="b", type=AnswerType.ABSTAIN, reason="no intent"),
        Prediction(id="c", type=AnswerType.PERCENT, answer=33.333),
        Prediction(id="d", type=AnswerType.UNANSWERABLE, reason="outside the data range"),
    ]
    path = tmp_path / "expected.jsonl"
    with PathPolicy(mode="competition", allowed_roots=[tmp_path]):
        with SubmissionWriter(path) as writer:
            for prediction in predictions:
                writer.write(prediction)
    assert [submission_line(prediction) for prediction in predictions] == path.read_text(encoding="utf-8").splitlines()


def test_checkpoint_drops_damaged_lines_and_appends(tmp_path: Path) -> None:
    path = tmp_path / "submission.jsonl"
    path.write_text('{"id":"q1","type":"abstain"}\n{"id":"q2","ty', encoding="utf-8")
    with PathPolicy(mode="competition", allowed_roots=[tmp_path]):
        checkpoint = Checkpoint(path)
        assert set(checkpoint.lines) == {"q1"}
        checkpoint.append("q3", '{"id":"q3","type":"abstain"}')
        checkpoint.close()
    assert [row["id"] for row in read_jsonl(path)] == ["q1", "q3"]


def test_summary_projection_uses_workers_for_question_time() -> None:
    rows = [
        {"biz": "a", "stage": "build", "seconds": 10.0},
        {"biz": "a", "stage": "ingestion", "seconds": 5.0},
        {"id": "q1", "seconds": 2.0, "decision": "value"},
        {"id": "q2", "seconds": 4.0, "decision": "abstain"},
        {"id": "q3", "seconds": 0.0, "decision": "budget"},
    ]
    text = format_summary(rows, workers=2, project_shops=3, project_questions=10)
    # 15 s per shop; mean over the two attempted questions is 3 s; 3 * 15 + 10 * 3 / 2 = 60
    assert "projected for 3 shops and 10 questions: 60 s" in text
    assert "shops built this run: 1" in text


def add_shop(root: Path, biz: str) -> None:
    """Copies shop_a's data under another business id and asks one question about it."""
    shutil.copytree(root / "data" / "shop_a", root / "data" / biz)
    question = {"id": f"{biz}-q1", "business_id": biz, "question": "How many orders?", "as_of_date": "2026-03-10"}
    with (root / "questions.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(question) + "\n")


class MarkerClient(FakeClient):
    """FakeClient whose model calls fail when the text mentions a marker word (one shop's documentation)."""

    def __init__(self, marker: str) -> None:
        super().__init__()
        self.marker = marker

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]:
        if self.marker in system or self.marker in prompt:
            raise ModelError("marked failure")
        return super().generate_json(system, prompt, timeout=timeout)


SLOW_SHOP_SECONDS = {"shop_c": 1.0}


def fake_slow_build(biz: str, ctx: Any) -> Path:
    """Stands in for build_shop: sleeps per shop, writes an empty database, and records when it finished."""
    time.sleep(SLOW_SHOP_SECONDS.get(biz, 0.05))
    target = ctx.out / "sem" / f"{biz}.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(target)):
        pass
    (ctx.out / "finished").mkdir(parents=True, exist_ok=True)
    (ctx.out / "finished" / biz).write_text(repr(time.time()), encoding="utf-8")
    return target


def model_failure_stages(database: Path) -> list[str]:
    with closing(sqlite3.connect(database)) as connection:
        return [row[0] for row in connection.execute("SELECT stage FROM model_failures")]


def test_questions_of_a_shop_start_before_the_last_shop_finishes(setup, monkeypatch) -> None:
    root = setup["root"]
    add_shop(root, "shop_b")
    add_shop(root, "shop_c")
    monkeypatch.setattr(run_v2, "build_shop", fake_slow_build)
    monkeypatch.setattr(run_v2, "TrammayClient", lambda settings, **_: FakeClient())
    starts: list[tuple[str, float]] = []

    class TimedSolver(SemanticSolver):
        def solve(self, question):
            starts.append((question.business_id, time.time()))
            prediction = Prediction(id=question.id, type=AnswerType.ABSTAIN, reason="timed")
            return prediction, SimpleNamespace(decision="abstain")

    monkeypatch.setattr(run_v2, "SemanticSolver", TimedSolver)

    assert main(command(setup, "--build-workers", "2")) == 0

    finished_last = float((setup["out"] / "finished" / "shop_c").read_text(encoding="utf-8"))
    assert min(started for _, started in starts) < finished_last  # answering began before the slow shop was done
    assert sorted(biz for biz, _ in starts) == ["shop_a", "shop_a", "shop_b", "shop_c"]
    final = read_jsonl(setup["out"] / "submission.final.jsonl")
    assert [row["id"] for row in final] == ["q1", "q2", "shop_b-q1", "shop_c-q1"]  # sorted by id, not by finish
    ready = [row for row in read_jsonl(setup["out"] / "timing.jsonl") if "ready_at" in row]
    assert sorted(row["biz"] for row in ready) == ["shop_a", "shop_b", "shop_c"]


def test_failures_of_one_shop_are_not_attributed_to_another_built_alongside(setup, monkeypatch) -> None:
    root = setup["root"]
    add_shop(root, "shop_b")  # copies shop_a's data, so its documentation must change after the copy
    (root / "data" / "shop_a" / "README.md").write_text("ALPHA-MARKER orders and their lines.\n", encoding="utf-8")
    monkeypatch.setattr(run_v2, "TrammayClient", lambda settings, **_: MarkerClient("ALPHA-MARKER"))

    assert main(command(setup, "--build-workers", "2")) == 0

    assert model_failure_stages(setup["out"] / "sem" / "shop_a.sqlite")  # the failing shop records its failures
    assert model_failure_stages(setup["out"] / "sem" / "shop_b.sqlite") == []


def test_model_gate_bounds_concurrent_model_calls() -> None:
    class CountingModel:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.active = 0
            self.peak = 0
            self.calls = 0

        def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]:
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
                self.calls += 1
            time.sleep(0.02)
            with self.lock:
                self.active -= 1
            return {}

        def close(self) -> None:
            pass

    model = CountingModel()
    gated = GatedModel(model, threading.BoundedSemaphore(2))
    threads = [threading.Thread(target=gated.generate_json, args=("system", "prompt"), kwargs={"timeout": 1.0})
               for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert model.calls == 8
    assert model.peak <= 2


def test_build_shop_in_worker_returns_database_path_and_stage_rows(setup, fake_client, monkeypatch) -> None:
    monkeypatch.setattr(run_v2, "_MODEL_GATE", threading.BoundedSemaphore(1))
    args = run_v2.parse_args(command(setup))
    result = build_shop_in_worker(ShopJob("shop_a", setup["root"], setup["out"], args))
    assert result.biz == "shop_a"
    assert result.path == setup["out"] / "sem" / "shop_a.sqlite" and result.path.is_file()
    assert set(STAGES) <= {row["stage"] for row in result.rows if "stage" in row}


def test_build_order_puts_shops_with_more_questions_first() -> None:
    def question(qid: str, biz: str) -> Question:
        return Question.model_validate({"id": qid, "business_id": biz, "question": "q", "as_of_date": "2026-03-10"})

    questions = [question("1", "b"), question("2", "a"), question("3", "c"), question("4", "c"), question("5", "a"),
                 question("6", "a")]
    assert build_order(questions) == ["a", "c", "b"]  # equal counts keep first-seen order


def test_summary_projection_uses_overlap_of_builds_and_questions() -> None:
    rows = [
        {"biz": "a", "stage": "build", "seconds": 10.0},
        {"biz": "a", "stage": "ingestion", "seconds": 5.0},
        {"biz": "a", "ready_at": 15.0},
        {"id": "q1", "seconds": 2.0, "decision": "value", "end_at": 20.0},
        {"id": "q2", "seconds": 4.0, "decision": "abstain", "end_at": 30.0},
        {"id": "q3", "seconds": 0.0, "decision": "budget", "end_at": 30.0},
    ]
    # Shop cost 15 s; steady question wall (30 - 18) / 2 = 6 s; build part 30 * 15 / 2 = 225 s.
    text = format_summary(rows, workers=2, project_shops=30, project_questions=10, build_workers=2, wall_seconds=100.0)
    assert "actual wall time: 100.0 s" in text
    assert "projected for 30 shops and 10 questions: 225 s" in text  # builds dominate: 60 s of questions + 165 s
    text = format_summary(rows, workers=2, project_shops=30, project_questions=3000, build_workers=2)
    assert "projected for 30 shops and 3000 questions: 18015 s" in text  # 18000 s of questions + 15 s first ready


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replaces IntentParser and SemanticSolver: every question parses to an order-count intent and is a value."""
    kwargs_seen: list[dict[str, Any]] = []
    parse_calls: list[str] = []

    class ScriptedParser:
        def __init__(self, model: Any, **kwargs: Any) -> None:
            kwargs_seen.append(kwargs)

        def parse(self, question: str, choices: Any = None) -> ParsedIntent:
            parse_calls.append(question)
            return ParsedIntent(intent=Intent(measure="order_count"), reason="", attempts=2, raw=None)

    class ScriptedSolver(SemanticSolver):
        def solve(self, question):
            parsed = self.parser.parse(question.question, choices=None)
            trace = SolveTrace(question_id=question.id, intent=parsed.intent,
                               windows=[TimeWindow(role="target", start=date(2026, 3, 1), end_exclusive=date(2026, 3, 10))],
                               decision="value", reason="")
            return Prediction(id=question.id, type=AnswerType.NUMBER, answer=2.0), trace

    monkeypatch.setattr(run_v2, "IntentParser", ScriptedParser)
    monkeypatch.setattr(run_v2, "SemanticSolver", ScriptedSolver)
    return SimpleNamespace(kwargs_seen=kwargs_seen, parse_calls=parse_calls)


def test_traces_hold_intent_windows_attempts_and_decision(setup, fake_client, scripted) -> None:
    assert main(command(setup, "--traces")) == 0
    traces = read_jsonl(setup["out"] / "traces.jsonl")
    assert sorted(row["id"] for row in traces) == ["q1", "q2"]
    row = {row["id"]: row for row in traces}["q2"]
    assert set(row) == {"id", "business_id", "question", "decision", "reason", "intent", "windows", "attempts", "seconds"}
    assert row["business_id"] == "shop_a"
    assert row["question"] == "What is the revenue?"
    assert row["decision"] == "value" and row["reason"] == ""
    assert row["intent"]["measure"] == "order_count" and row["intent"]["shape"] == "scalar"  # dataclass as a dict
    assert row["windows"] == [["target", "2026-03-01", "2026-03-10"]]
    assert row["attempts"] == 2
    assert isinstance(row["seconds"], float) and row["seconds"] >= 0


def test_traces_are_absent_unless_requested(setup, fake_client, scripted) -> None:
    assert main(command(setup)) == 0
    assert not (setup["out"] / "traces.jsonl").exists()


def test_traces_null_intent_when_parser_gives_none(setup, fake_client) -> None:
    assert main(command(setup, "--traces")) == 0  # the fake client yields no intent, so every question abstains
    traces = read_jsonl(setup["out"] / "traces.jsonl")
    assert sorted(row["id"] for row in traces) == ["q1", "q2"]
    assert all(row["intent"] is None and row["decision"] == "abstain" and row["attempts"] >= 1 for row in traces)


def test_traces_resume_does_not_duplicate_and_redoes_only_unanswered(setup, fake_client, scripted) -> None:
    assert main(command(setup, "--traces")) == 0
    traces_path = setup["out"] / "traces.jsonl"
    first = traces_path.read_text(encoding="utf-8")
    assert len(scripted.parse_calls) == 2

    assert main(command(setup, "--traces")) == 0  # everything is checkpointed: nothing solved, nothing added
    assert traces_path.read_text(encoding="utf-8") == first
    assert len(scripted.parse_calls) == 2

    # Drop q2 from the checkpoint: the rerun answers q2 again and writes exactly one new trace for it.
    submission = setup["out"] / "submission.jsonl"
    submission.write_text("".join(line + "\n" for line in submission.read_text(encoding="utf-8").splitlines()
                                  if '"q1"' in line), encoding="utf-8")
    assert main(command(setup, "--traces")) == 0
    traces = read_jsonl(traces_path)
    assert sorted(row["id"] for row in traces) == ["q1", "q2"]
    assert len(scripted.parse_calls) == 3
    assert sorted(row["id"] for row in read_jsonl(setup["out"] / "submission.jsonl")) == ["q1", "q2"]


def test_prompt_profile_reaches_intent_parser_and_manifest(setup, fake_client, scripted) -> None:
    assert main(command(setup, "--prompt-profile", "qwen")) == 0
    assert scripted.kwargs_seen and all(kwargs == {"profile": "qwen"} for kwargs in scripted.kwargs_seen)
    manifest = json.loads((setup["out"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["prompt_profile"] == "qwen"
    assert manifest["arguments"]["prompt_profile"] == "qwen"


def test_default_prompt_profile_passes_no_keyword(setup, fake_client, scripted) -> None:
    assert main(command(setup)) == 0
    assert scripted.kwargs_seen and all(kwargs == {} for kwargs in scripted.kwargs_seen)
    manifest = json.loads((setup["out"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["prompt_profile"] == "default"


def test_default_profile_works_with_parser_that_has_no_profile_keyword(setup, fake_client, monkeypatch) -> None:
    class OldParser:
        def __init__(self, model: Any) -> None:
            pass

        def parse(self, question: str, choices: Any = None) -> ParsedIntent:
            return ParsedIntent(intent=None, reason="scripted", attempts=1, raw=None)

    monkeypatch.setattr(run_v2, "IntentParser", OldParser)
    assert main(command(setup)) == 0


def test_qwen_profile_fails_fast_when_parser_lacks_the_keyword(setup, fake_client, monkeypatch) -> None:
    class OldParser:
        def __init__(self, model: Any) -> None:
            pass

    monkeypatch.setattr(run_v2, "IntentParser", OldParser)
    with pytest.raises(ValueError, match="--prompt-profile 'qwen' needs an IntentParser that accepts profile="):
        main(command(setup, "--prompt-profile", "qwen"))
    assert (setup["out"] / "submission.jsonl").read_text(encoding="utf-8") == ""  # failed before any question ran


def test_questions_file_restricts_questions_and_shops(setup, fake_client) -> None:
    root = setup["root"]
    add_shop(root, "shop_b")
    subset = setup["out"].parent / "subset.jsonl"
    subset.write_text("".join(line + "\n" for line in (root / "questions.jsonl").read_text(encoding="utf-8").splitlines()
                              if '"shop_b"' in line), encoding="utf-8")

    assert main(command(setup, "--questions-file", str(subset))) == 0

    final = read_jsonl(setup["out"] / "submission.final.jsonl")
    assert [row["id"] for row in final] == ["shop_b-q1"]
    assert (setup["out"] / "sem" / "shop_b.sqlite").is_file()
    assert not (setup["out"] / "sem" / "shop_a.sqlite").exists()
    manifest = json.loads((setup["out"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["questions_path"] == str(subset)


@pytest.fixture
def fewshot_bank(setup) -> Path:
    """Shop_b examples are usable for shop_a questions; the shop_a row and the q1 variant must never be shown."""
    rows = [
        {"id": "shop_b-q9", "business_id": "shop_b", "question": "How many orders?",
         "intent": {"measure": "order_count", "shape": "scalar", "filters": [], "group_by": None}},
        {"id": "shop_a-q9", "business_id": "shop_a", "question": "How many orders?",
         "intent": {"measure": "order_count", "shape": "scalar", "filters": [], "group_by": None}},
        {"id": "q1__abbrev", "business_id": "shop_b", "question": "How many orders?",
         "intent": {"measure": "order_count", "shape": "scalar", "filters": [], "group_by": None}},
    ]
    path = setup["out"].parent / "fewshot.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


@pytest.fixture
def recording(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A fake parser that records its constructor keywords and every parse, and runs the examples provider."""
    seen = SimpleNamespace(kwargs=[], calls=[], blocks={})

    class RecordingParser:
        def __init__(self, model: Any, **kwargs: Any) -> None:
            seen.kwargs.append(kwargs)
            self.examples = kwargs.get("examples")

        def parse(self, question: str, choices: Any = None, *, question_id: Any = None,
                  business_id: Any = None) -> ParsedIntent:
            seen.calls.append((question, question_id, business_id))
            if self.examples is not None:
                seen.blocks[question_id] = self.examples(question, question_id, business_id)
            return ParsedIntent(intent=Intent(measure="order_count"), reason="", attempts=1, raw=None)

    class ScriptedSolver(SemanticSolver):
        def solve(self, question):
            parsed = self.parser.parse(question.question, choices=None)  # the solver passes no ids
            trace = SolveTrace(question_id=question.id, intent=parsed.intent, windows=[], decision="value", reason="")
            return Prediction(id=question.id, type=AnswerType.NUMBER, answer=2.0), trace

    monkeypatch.setattr(run_v2, "IntentParser", RecordingParser)
    monkeypatch.setattr(run_v2, "SemanticSolver", ScriptedSolver)
    return seen


def test_fewshot_flags_default_to_off_and_k_four(tmp_path: Path) -> None:
    base = ["--data", str(tmp_path), "--out", str(tmp_path), "--connection", str(tmp_path / "c.json")]
    args = run_v2.parse_args(base)
    assert args.fewshot_bank is None and args.fewshot_k == 4
    args = run_v2.parse_args(base + ["--fewshot-bank", str(tmp_path / "b.jsonl"), "--fewshot-k", "2"])
    assert args.fewshot_bank == tmp_path / "b.jsonl" and args.fewshot_k == 2


def test_without_a_bank_the_parser_gets_no_examples(setup, fake_client, recording) -> None:
    assert main(command(setup)) == 0
    assert recording.kwargs == [{}]  # the default profile without a bank is the old, keyword-free IntentParser(model)
    assert recording.blocks == {}
    manifest = json.loads((setup["out"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["fewshot_bank"] is None and manifest["fewshot_k"] is None and manifest["fewshot_bank_sha256"] is None


def test_fewshot_bank_reaches_the_parser_with_leakage_rules(setup, fake_client, recording, fewshot_bank) -> None:
    assert main(command(setup, "--fewshot-bank", str(fewshot_bank), "--fewshot-k", "2")) == 0

    assert len(recording.kwargs) == 1 and callable(recording.kwargs[0]["examples"])
    # Each question reached the parser with its own id and shop, not only its text.
    assert sorted(recording.calls) == [("How many orders?", "q1", "shop_a"), ("What is the revenue?", "q2", "shop_a")]
    # For q1 only shop_b-q9 is usable: shop_a-q9 shares the shop and q1__abbrev shares q1's base id.
    assert recording.blocks["q1"] == (
        'Similar solved questions:\nQ: How many orders?\nA: {"measure": "order_count", "shape": "scalar"}'
    )
    manifest = json.loads((setup["out"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["fewshot_bank"] == str(fewshot_bank)
    assert manifest["fewshot_k"] == 2
    assert manifest["fewshot_bank_sha256"] == hashlib.sha256(fewshot_bank.read_bytes()).hexdigest()


def test_fewshot_k_must_be_positive(setup, fake_client, recording, fewshot_bank) -> None:
    with pytest.raises(ValueError, match="--fewshot-k must be at least 1"):
        main(command(setup, "--fewshot-bank", str(fewshot_bank), "--fewshot-k", "0"))


def test_empty_fewshot_bank_is_refused(setup, fake_client, recording, tmp_path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="has no examples"):
        main(command(setup, "--fewshot-bank", str(empty)))


def test_bound_parser_refuses_text_that_is_not_the_bound_question() -> None:
    class Inner:
        def parse(self, question: str, choices: Any = None, **kwargs: Any) -> ParsedIntent:
            return ParsedIntent(intent=None, reason=str(kwargs), attempts=1, raw=None)

    bound = run_v2._QuestionBoundParser(Inner())
    with pytest.raises(RuntimeError, match="not the bound question"):
        bound.parse("What is the revenue?")  # nothing bound yet
    question = Question(id="q2", business_id="shop_a", question="What is the revenue?", as_of_date=date(2026, 3, 10))
    bound.bind(question)
    assert bound.parse("What is the revenue?").reason == "{'question_id': 'q2', 'business_id': 'shop_a'}"
    with pytest.raises(RuntimeError):
        bound.parse("some other text")
    bound.bind(None)
    with pytest.raises(RuntimeError):
        bound.parse("What is the revenue?")


def test_make_intent_parser_reasoning_on():
    from bizreason.run_v2 import make_intent_parser
    from bizreason.semantic.intent import REASONING_ADDENDUM

    parser = make_intent_parser(object(), "default", None, reasoning=True)
    assert parser.reasoning and parser.system.endswith(REASONING_ADDENDUM)
    assert not make_intent_parser(object(), "default").reasoning
