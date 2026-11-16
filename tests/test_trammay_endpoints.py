from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from bizreason.cli import build_parser, main
from bizreason.config import Settings, normalize_model_id
from bizreason.contracts import (
    AnswerType,
    Candidate,
    ExecutionResult,
    ExecutionStatus,
    OutputSpec,
    Program,
    Question,
)
from bizreason.llm import LanguageModel, ModelError, TrammayClient, Usage
from bizreason.streaming import StreamingTrammayClient
from bizreason.triage import LLMSemanticCritic, SemanticCritic


def test_normalize_model_id_trammay_aliases():
    trammay_url = "https://trammay.xyz/v1"
    assert normalize_model_id("gemini3.8-flash-high", trammay_url) == "ag/gemini-3.8-flash-high"
    assert normalize_model_id("gemini-3.8-flash-high", trammay_url) == "ag/gemini-3.8-flash-high"
    assert normalize_model_id("gemini3.8-flash-medium", trammay_url) == "ag/gemini-3.8-flash-medium"
    assert normalize_model_id("gemini3.8-flash-low", trammay_url) == "ag/gemini-3.8-flash-low"
    assert normalize_model_id("gemini3.8-flash", trammay_url) == "ag/gemini-3.8-flash"
    assert normalize_model_id("gemini-3.8-flash", trammay_url) == "ag/gemini-3.8-flash"
    assert normalize_model_id("ag/gemini-3.8-flash-high", trammay_url) == "ag/gemini-3.8-flash-high"

    # Case-insensitive base_url
    assert normalize_model_id("gemini3.8-flash-high", "https://TRAMMAY.XYZ/V1") == "ag/gemini-3.8-flash-high"

    # Local / non-trammay endpoints remain untouched
    local_url = "http://127.0.0.1:8000/v1"
    assert normalize_model_id("gemini3.8-flash-high", local_url) == "gemini3.8-flash-high"
    assert normalize_model_id("gemini-3.8-flash-high", "http://localhost:8000/v1") == "gemini-3.8-flash-high"
    assert normalize_model_id("meta-llama/Llama-3-8B", trammay_url) == "meta-llama/Llama-3-8B"

    # Settings and Settings.from_env post-init normalization
    settings_trammay = Settings(base_url=trammay_url, model="gemini3.8-flash-high")
    assert settings_trammay.model == "ag/gemini-3.8-flash-high"

    settings_env = Settings.from_env(base_url=trammay_url, model="gemini3.8-flash-high")
    assert settings_env.model == "ag/gemini-3.8-flash-high"

    settings_local = Settings(base_url=local_url, model="gemini3.8-flash-high")
    assert settings_local.model == "gemini3.8-flash-high"


def test_settings_from_env_api_key_priority_cloudflare_vs_trammay(monkeypatch, tmp_path):
    for k in [
        "TRAMMAY_KEY",
        "BIZREASON_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API",
        "OPENROUTER_API_KEY",
    ]:
        monkeypatch.delenv(k, raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "TRAMMAY_KEY=tk\n"
        "BIZREASON_API_KEY=bk\n",
        encoding="utf-8",
    )

    s_cf = Settings.from_env(env_file, base_url="https://demo.trycloudflare.com/v1")
    assert s_cf.api_key == "bk"

    s_tram = Settings.from_env(env_file, base_url="https://trammay.xyz/v1")
    assert s_tram.api_key == "tk"

    # Also verify when set via process environment variables
    monkeypatch.setenv("TRAMMAY_KEY", "tk")
    monkeypatch.setenv("BIZREASON_API_KEY", "bk")

    s_cf_env = Settings.from_env(env_file, base_url="https://demo.trycloudflare.com/v1")
    assert s_cf_env.api_key == "bk"

    s_tram_env = Settings.from_env(env_file, base_url="https://trammay.xyz/v1")
    assert s_tram_env.api_key == "tk"


def test_get_v1_models_parsing():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "ag/gemini-3.8-flash-high"},
                    {"id": "ag/gemini-3.8-flash-medium"},
                ]
            },
        )

    client = TrammayClient(
        Settings(api_key="mock-key"),
        transport=httpx.MockTransport(handler),
    )
    try:
        models = client.models()
        assert models == ["ag/gemini-3.8-flash-high", "ag/gemini-3.8-flash-medium"]
    finally:
        client.close()


def test_post_chat_completions_json_extraction_and_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "```json\n{\"revenue\": 1500000}\n```"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
            },
        )

    client = TrammayClient(
        Settings(api_key="mock-key", api_style="chat"),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.generate_json("System instructions", "User prompt", timeout=10)
        assert result == {"revenue": 1500000}
        assert client.usage.calls == 1
        assert client.usage.tokens == 150
    finally:
        client.close()


def test_post_responses_standard_openai_schema():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/responses"
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "content": [
                            {"type": "output_text", "text": "{\"metric\": \"debt_total\"}"}
                        ]
                    }
                ],
                "usage": {"input_tokens": 40, "output_tokens": 15},
            },
        )

    client = TrammayClient(
        Settings(api_key="mock-key", api_style="responses"),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.generate_json("System prompt", "Input prompt", timeout=10)
        assert result == {"metric": "debt_total"}
        assert client.usage.calls == 1
        assert client.usage.tokens == 55
    finally:
        client.close()


def test_post_responses_trammay_chat_completion_fallback():
    # Trammay live endpoint returns a chat.completion-shaped payload on /responses
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/responses"
        return httpx.Response(
            200,
            json={
                "object": "chat.completion",
                "choices": [
                    {
                        "message": {"content": "{\"metric\": \"cancellation_rate\", \"value\": 5.2}"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"total_tokens": 88},
            },
        )

    client = TrammayClient(
        Settings(api_key="mock-key", api_style="responses"),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.generate_json("System prompt", "Input prompt", timeout=10)
        assert result == {"metric": "cancellation_rate", "value": 5.2}
        assert client.usage.calls == 1
        assert client.usage.tokens == 88
    finally:
        client.close()


def test_post_responses_length_truncation_raises_model_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "{\"incomplete\": true"},
                        "finish_reason": "length",
                    }
                ]
            },
        )

    client = TrammayClient(
        Settings(api_key="mock-key", api_style="responses"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ModelError, match="candidate"):
            client.generate_json("system", "prompt", timeout=5)
    finally:
        client.close()


def test_streaming_trammay_responses_sse_output_text_delta():
    events = [
        {"type": "response.output_text.delta", "delta": '{"metric":'},
        {"type": "response.output_text.delta", "delta": ' "gross_profit"}'},
        {"usage": {"input_tokens": 10, "output_tokens": 15}},
    ]
    data = (
        "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        + "data: [DONE]\n\n"
    )

    client = StreamingTrammayClient(
        Settings(api_key="test-key", api_style="responses"),
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=data
            )
        ),
    )
    try:
        result = client.generate_json("system", "prompt", timeout=10)
        assert result == {"metric": "gross_profit"}
        assert client.usage.calls == 1
        assert client.usage.tokens == 25
    finally:
        client.close()


def test_streaming_trammay_responses_non_sse_trammay_shape():
    # When Trammay returns non-SSE chat.completion shape for streaming request
    payload = {
        "object": "chat.completion",
        "choices": [
            {
                "message": {"content": '{"fast": true}'},
                "finish_reason": "stop",
            }
        ],
        "usage": {"total_tokens": 42},
    }

    client = StreamingTrammayClient(
        Settings(api_key="test-key", api_style="responses"),
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"content-type": "application/json"}, text=json.dumps(payload)
            )
        ),
    )
    try:
        result = client.generate_json("system", "prompt", timeout=10)
        assert result == {"fast": True}
        assert client.usage.tokens == 42
    finally:
        client.close()


@dataclass
class MockLanguageModel(LanguageModel):
    return_payload: dict[str, Any] | None = None
    raise_error: Exception | None = None
    usage: Usage = field(default_factory=Usage)

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]:
        self.usage.calls += 1
        if self.raise_error:
            raise self.raise_error
        return self.return_payload or {}


def test_llm_semantic_critic_scoring_and_failsafe(business, rules):
    question = Question(
        id="q_critic_test",
        business_id=business.business_id,
        question="Doanh thu tháng 3 năm 2026",
        as_of_date="2026-03-31",
    )
    candidate = Candidate(
        id="cand_critic_test",
        plan_hash="hash1",
        code_hash="code1",
        source_revision=business.revision,
        program=Program(sql="SELECT sum(amount) FROM sales", parameters=[], backend="template", policy_refs=[]),
        execution=ExecutionResult(status=ExecutionStatus.SUCCESS, columns=["res"], rows=[[100]], row_count=1),
        output=OutputSpec(type=AnswerType.NUMBER),
    )

    # 1. Aligned verdict
    mock_aligned = MockLanguageModel(
        return_payload={"verdict": "aligned", "score": 0.95, "reason": "Accurately complies with rulebook"}
    )
    critic = LLMSemanticCritic(mock_aligned)
    assert isinstance(critic, SemanticCritic)
    score = critic.score(question, candidate, business, rules)
    assert score == 0.95
    audit = critic.audit(question, candidate, business, rules)
    assert audit["verdict"] == "aligned"
    assert audit["score"] == 0.95

    # 2. Policy violation verdict
    mock_violation = MockLanguageModel(
        return_payload={"verdict": "policy_violation", "score": 0.1, "reason": "Missing cancelled filter"}
    )
    critic_violation = LLMSemanticCritic(mock_violation)
    assert critic_violation.score(question, candidate, business, rules) == 0.1

    # 3. Fail-safe on ModelError (transient network or model failure)
    mock_err = MockLanguageModel(raise_error=ModelError("Upstream timeout"))
    critic_err = LLMSemanticCritic(mock_err)
    fallback_score = critic_err.score(question, candidate, business, rules)
    assert fallback_score == 0.5
    audit_err = critic_err.audit(question, candidate, business, rules)
    assert audit_err["verdict"] == "uncertain"
    assert audit_err["score"] == 0.5


def test_cli_flags_semantic_critic_and_stream():
    parser = build_parser()
    args = parser.parse_args([
        "run",
        "--data", "dummy_path",
        "--enable-semantic-critic",
        "--stream",
    ])
    assert args.enable_semantic_critic is True
    assert args.stream is True

    # Defaults
    args_default = parser.parse_args(["run", "--data", "dummy_path"])
    assert args_default.enable_semantic_critic is False
    assert args_default.stream is False


def test_trammay_client_recovers_from_524_and_remote_protocol_error():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(524, json={"error": "Cloudflare Gateway Timeout"})
        if call_count == 2:
            raise httpx.RemoteProtocolError("Server disconnected", request=request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"recovered": true}'}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 25},
            },
        )

    settings = Settings(
        api_key="mock-key",
        transport_retries=2,
        transport_backoff=0.0,
    )
    client = TrammayClient(settings, transport=httpx.MockTransport(handler))
    try:
        result = client.generate_json("system", "prompt", timeout=10)
        assert result == {"recovered": True}
        assert call_count == 3
        # usage.calls must be counted once per generate_json invocation
        assert client.usage.calls == 1
        assert client.usage.tokens == 25
    finally:
        client.close()


def test_trammay_client_fails_fast_on_401():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(401, json={"error": "Unauthorized"})

    settings = Settings(
        api_key="mock-key",
        transport_retries=3,
        transport_backoff=0.0,
    )
    client = TrammayClient(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError, match="HTTP 401"):
            client.generate_json("system", "prompt", timeout=10)
        # Must fail fast without retrying
        assert call_count == 1
        assert client.usage.calls == 1
    finally:
        client.close()


def test_streaming_trammay_client_recovers_from_remote_protocol_error():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate a generator that raises RemoteProtocolError midway through stream
            def faulty_stream():
                yield b"data: " + json.dumps({"choices": [{"delta": {"content": "{\"partial\": "}}]}).encode() + b"\n\n"
                raise httpx.RemoteProtocolError("Connection broken mid-stream", request=request)

            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=faulty_stream(),
            )
        # Attempt 2: clean stream
        events = [
            {"choices": [{"delta": {"content": '{"partial": false, '}}]},
            {"choices": [{"delta": {"content": '"success": true}'}, "finish_reason": "stop"}]},
            {"usage": {"total_tokens": 15}},
        ]
        data = (
            "".join("data: " + json.dumps(ev) + "\n\n" for ev in events)
            + "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=data,
        )

    settings = Settings(
        api_key="mock-key",
        transport_retries=2,
        transport_backoff=0.0,
    )
    client = StreamingTrammayClient(settings, transport=httpx.MockTransport(handler))
    try:
        result = client.generate_json("system", "prompt", timeout=10)
        assert result == {"partial": False, "success": True}
        assert call_count == 2
        # usage.calls must only be incremented once
        assert client.usage.calls == 1
        assert client.usage.tokens == 15
    finally:
        client.close()


def test_cli_run_workers_writes_output_in_exact_order(workspace_tmp, rules):
    data_dir = workspace_tmp / "workers_data"
    data_dir.mkdir()
    (data_dir / "rulebook.md").write_text(rules.text, encoding="utf-8")
    (data_dir / "data" / "biz_a").mkdir(parents=True)
    (data_dir / "data" / "biz_a" / "README.md").write_text("No data", encoding="utf-8")

    q_ids = [f"q_order_{i:02d}" for i in range(12)]
    questions_content = "".join(
        json.dumps({
            "id": qid,
            "business_id": "biz_a",
            "question": f"Question {qid}",
            "as_of_date": "2026-03-04",
        }) + "\n"
        for qid in q_ids
    )
    (data_dir / "questions.jsonl").write_text(questions_content, encoding="utf-8")

    out_file = workspace_tmp / "workers_submission.jsonl"
    artifacts_dir = workspace_tmp / "artifacts_workers"
    cache_dir = workspace_tmp / "cache_workers"

    main([
        "run",
        "--data", str(data_dir),
        "--output", str(out_file),
        "--artifacts", str(artifacts_dir),
        "--cache", str(cache_dir),
        "--backend", "none",
        "--workers", "3",
    ])

    assert out_file.exists()
    lines = [json.loads(line) for line in out_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    written_ids = [item["id"] for item in lines]
    assert written_ids == q_ids
