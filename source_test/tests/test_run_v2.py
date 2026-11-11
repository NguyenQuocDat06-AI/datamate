"""bizreason.run_v2: end-to-end command on a tiny synthetic test folder, with a fake model client (no network)."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from bizreason import run_v2
from bizreason.contracts import AnswerType, Prediction
from bizreason.data import schema
from bizreason.formatting import SubmissionWriter
from bizreason.semantic.annotator import SCOPE
from bizreason.semantic.intent import SYSTEM
from bizreason.semantic.solver import SemanticSolver
from bizreason.run_v2 import Checkpoint, STAGES, format_summary, main, submission_line
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


def test_no_alias_restores_header_lookup(setup, fake_client) -> None:
    before = dict(schema.ROLE_LOOKUP)
    assert main(command(setup, "--no-alias")) == 0
    assert dict(schema.ROLE_LOOKUP) == before


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
