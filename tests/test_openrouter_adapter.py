from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from bizreason.config import Settings, normalize_model_id
from bizreason.llm import (
    ModelError,
    TrammayClient,
    _extract_payload_provider_error,
    build_chat_messages,
    parse_json_object,
)
from bizreason.streaming import StreamingTrammayClient


# ==============================================================================
# 1. Provider-Aware API Key & .env Resolution Tests
# ==============================================================================

def test_settings_from_env_provider_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in [
        "OPENROUTER_API",
        "OPENROUTER_API_KEY",
        "TRAMMAY_KEY",
        "GROQ_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "NVIDIA_API_KEY",
        "MISTRAL_API_KEY",
        "BIZREASON_API_KEY",
        "OPENAI_API_KEY",
        "BIZREASON_BASE_URL",
        "BIZREASON_MODEL",
    ]:
        monkeypatch.delenv(key, raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENROUTER_API=sk-or-test\n"
        "OPENROUTER_API_KEY=sk-or-key-test\n"
        "TRAMMAY_KEY=sk-trammay-test\n"
        "GROQ_API_KEY=sk-groq-test\n"
        "GEMINI_API_KEY=sk-gemini-test\n"
        "NVIDIA_API_KEY=sk-nvidia-test\n"
        "MISTRAL_API_KEY=sk-mistral-test\n"
        "BIZREASON_API_KEY=sk-biz-test\n"
        "OPENAI_API_KEY=sk-openai-test\n",
        encoding="utf-8",
    )

    # 1. OpenRouter endpoint selects OPENROUTER_API even though TRAMMAY_KEY is present
    s_or = Settings.from_env(env_file, base_url="https://openrouter.ai/api/v1")
    assert s_or.api_key == "sk-or-test"
    assert s_or.base_url == "https://openrouter.ai/api/v1"

    # 2. Trammay endpoint selects TRAMMAY_KEY
    s_tram = Settings.from_env(env_file, base_url="https://trammay.xyz/v1")
    assert s_tram.api_key == "sk-trammay-test"

    # 3. Groq endpoint selects GROQ_API_KEY
    s_groq = Settings.from_env(env_file, base_url="https://api.groq.com/openai/v1")
    assert s_groq.api_key == "sk-groq-test"

    # 4. Google AI Studio endpoint selects GEMINI_API_KEY
    s_gem = Settings.from_env(
        env_file, base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    assert s_gem.api_key == "sk-gemini-test"

    # 5. NVIDIA endpoint selects NVIDIA_API_KEY
    s_nvd = Settings.from_env(env_file, base_url="https://integrate.api.nvidia.com/v1")
    assert s_nvd.api_key == "sk-nvidia-test"

    # 6. Mistral endpoint selects MISTRAL_API_KEY
    s_mis = Settings.from_env(env_file, base_url="https://api.mistral.ai/v1")
    assert s_mis.api_key == "sk-mistral-test"

    # 7. Explicit api_key override takes precedence
    s_override = Settings.from_env(
        env_file, base_url="https://openrouter.ai/api/v1", api_key="sk-override-val"
    )
    assert s_override.api_key == "sk-override-val"

    # 8. api_key=None override keeps default_api_key
    s_none_override = Settings.from_env(
        env_file, base_url="https://openrouter.ai/api/v1", api_key=None
    )
    assert s_none_override.api_key == "sk-or-test"


def test_normalize_model_id():
    # Non-trammay base URLs leave model id untouched
    assert (
        normalize_model_id("google/gemma-3-12b-it", "https://openrouter.ai/api/v1")
        == "google/gemma-3-12b-it"
    )
    assert (
        normalize_model_id("google/gemma-3-12b-it:free", "https://openrouter.ai/api/v1")
        == "google/gemma-3-12b-it:free"
    )
    assert (
        normalize_model_id("  Qwen/Qwen2.5-72B-Instruct  ", "https://openrouter.ai/api/v1")
        == "Qwen/Qwen2.5-72B-Instruct"
    )

    # Trammay endpoint normalizes aliases
    assert (
        normalize_model_id("gemini3.8-flash-high", "https://trammay.xyz/v1")
        == "ag/gemini-3.8-flash-high"
    )
    assert (
        normalize_model_id("gemini-3.8-flash-medium", "https://trammay.xyz/v1")
        == "ag/gemini-3.8-flash-medium"
    )


# ==============================================================================
# 2. Gemma Chat Message Construction Tests
# ==============================================================================

def test_build_chat_messages():
    # Gemma 3 models merge system into user automatically
    msg_gemma3 = build_chat_messages("google/gemma-3-12b-it", "system prompt", "user query")
    assert msg_gemma3 == [{"role": "user", "content": "system prompt\n\nuser query"}]

    msg_gemma3_free = build_chat_messages(
        "google/gemma-3-12b-it:free", "system prompt", "user query"
    )
    assert msg_gemma3_free == [{"role": "user", "content": "system prompt\n\nuser query"}]

    msg_gemma2 = build_chat_messages("google/gemma-2-9b-it", "system prompt", "user query")
    assert msg_gemma2 == [{"role": "user", "content": "system prompt\n\nuser query"}]

    # Non-Gemma models keep separate system and user turns
    msg_oss = build_chat_messages("ag/gpt-oss-120b-medium", "system prompt", "user query")
    assert msg_oss == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user query"},
    ]

    msg_4o = build_chat_messages("openai/gpt-4o", "system prompt", "user query")
    assert msg_4o == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user query"},
    ]

    # Explicit merge_system=True forces merge for non-Gemma models
    msg_forced = build_chat_messages(
        "ag/gpt-oss-120b-medium", "system prompt", "user query", merge_system=True
    )
    assert msg_forced == [{"role": "user", "content": "system prompt\n\nuser query"}]

    # Empty system prompt merges without extra newlines
    msg_empty_sys = build_chat_messages("google/gemma-3-12b-it", "", "user query")
    assert msg_empty_sys == [{"role": "user", "content": "user query"}]


# ==============================================================================
# 3. Non-Trammay HTTP 400 Auto-Fallback Tests
# ==============================================================================

def test_non_trammay_http_400_auto_fallback_trammay_client():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content.decode("utf-8"))
        # If separate system message was sent, reject with 400 Developer instruction not enabled
        if len(body.get("messages", [])) > 1:
            return httpx.Response(
                400,
                json={"error": {"message": "Developer instruction is not enabled", "code": 400}},
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 15},
            },
        )

    settings = Settings(
        base_url="https://openrouter.ai/api/v1",
        api_key="mock-key",
        model="ag/gpt-oss-120b-medium",
        transport_retries=1,
        transport_backoff=0.0,
    )
    client = TrammayClient(settings, transport=httpx.MockTransport(handler))
    try:
        res = client.generate_json("system instructions", "user query", timeout=10)
        assert res == {"ok": True}
        assert len(requests) == 2

        # First request sent separate system
        body0 = json.loads(requests[0].content.decode("utf-8"))
        assert len(body0["messages"]) == 2
        assert body0["messages"][0]["role"] == "system"

        # Second request sent merged user message
        body1 = json.loads(requests[1].content.decode("utf-8"))
        assert len(body1["messages"]) == 1
        assert body1["messages"][0]["role"] == "user"
        assert "system instructions\n\nuser query" in body1["messages"][0]["content"]

        # Calls quota must NOT be double-counted (exactly 1)
        assert client.usage.calls == 1
        assert client.usage.tokens == 15
    finally:
        client.close()


def test_non_trammay_http_400_auto_fallback_streaming_client():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content.decode("utf-8"))
        if len(body.get("messages", [])) > 1:
            return httpx.Response(
                400,
                json={"error": {"message": "Developer instruction is not enabled", "code": 400}},
            )
        events = [
            {"choices": [{"delta": {"content": '{"stream_ok": true}'}, "finish_reason": "stop"}]},
            {"usage": {"total_tokens": 20}},
        ]
        body_text = "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body_text)

    settings = Settings(
        base_url="https://openrouter.ai/api/v1",
        api_key="mock-key",
        model="ag/gpt-oss-120b-medium",
        transport_retries=1,
        transport_backoff=0.0,
    )
    client = StreamingTrammayClient(settings, transport=httpx.MockTransport(handler))
    try:
        res = client.generate_json("system instructions", "user query", timeout=10)
        assert res == {"stream_ok": True}
        assert len(requests) == 2
        assert client.usage.calls == 1
        assert client.usage.tokens == 20
    finally:
        client.close()


# ==============================================================================
# 4. OpenRouter SSE Streaming Mid-Stream 429 Recovery Tests
# ==============================================================================

def test_openrouter_sse_streaming_mid_stream_429_recovery():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Emit SSE comments and then an in-stream 429 rate limit error chunk
            body = (
                ": OPENROUTER PROCESSING\n\n"
                ": OPENROUTER PROCESSING\n\n"
                'data: {"error": {"code": 429, "message": "Rate limit"}}\n\n'
            )
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
        # Attempt 2 succeeds cleanly
        body = (
            ": OPENROUTER PROCESSING\n\n"
            'data: {"choices": [{"delta": {"content": "{\\"streamed\\": true}"}, "finish_reason": "stop"}]}\n\n'
            'data: {"usage": {"total_tokens": 25}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    settings = Settings(
        base_url="https://openrouter.ai/api/v1",
        api_key="mock-key",
        transport_retries=2,
        transport_backoff=0.0,
    )
    client = StreamingTrammayClient(settings, transport=httpx.MockTransport(handler))
    try:
        res = client.generate_json("system", "prompt", timeout=10)
        assert res == {"streamed": True}
        assert call_count == 2
        assert client.usage.calls == 1
        assert client.usage.tokens == 25
    finally:
        client.close()


def test_openrouter_in_body_429_recovery_trammay_client():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                200, json={"error": {"code": 429, "message": "Upstream rate limit"}}
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 10},
            },
        )

    settings = Settings(
        base_url="https://openrouter.ai/api/v1",
        api_key="mock-key",
        transport_retries=2,
        transport_backoff=0.0,
    )
    client = TrammayClient(settings, transport=httpx.MockTransport(handler))
    try:
        res = client.generate_json("system", "prompt", timeout=10)
        assert res == {"ok": True}
        assert call_count == 2
        assert client.usage.calls == 1
        assert client.usage.tokens == 10
    finally:
        client.close()


# ==============================================================================
# 5. Small-Model JSON Extraction Resilience Tests
# ==============================================================================

def test_parse_json_object_resilience():
    # Plain JSON
    assert parse_json_object('{"decision": "APPROVE"}') == {"decision": "APPROVE"}

    # Fenced JSON with json identifier
    assert parse_json_object('```json\n{"decision": "APPROVE"}\n```') == {"decision": "APPROVE"}

    # Fenced JSON without language specifier
    assert parse_json_object('```\n{"decision": "APPROVE"}\n```') == {"decision": "APPROVE"}

    # Wrapped in <think> reasoning block
    think_text = (
        "<think>\n"
        "1. Examine query.\n"
        "2. The decision should be APPROVE.\n"
        "</think>\n"
        '```json\n{"decision": "APPROVE"}\n```'
    )
    assert parse_json_object(think_text) == {"decision": "APPROVE"}

    # Preamble text before fenced JSON and trailing prose
    preamble_fence = (
        "Here is the requested JSON output:\n"
        '```json\n{"decision": "APPROVE", "score": 99}\n```\n'
        "Hope this helps!"
    )
    assert parse_json_object(preamble_fence) == {"decision": "APPROVE", "score": 99}

    # Raw JSON object embedded directly in conversational text
    raw_embedded = "Certainly! The answer is: {\"decision\": \"APPROVE\"}. Let me know if you need more."
    assert parse_json_object(raw_embedded) == {"decision": "APPROVE"}

    # Non-dict JSON (lists) must raise ModelError("Model output must be a JSON object")
    with pytest.raises(ModelError, match="Model output must be a JSON object"):
        parse_json_object("[1, 2, 3]")

    with pytest.raises(ModelError, match="Model output must be a JSON object"):
        parse_json_object('```json\n[1, 2]\n```')

    # Scalar JSON must raise ModelError("Model output must be a JSON object")
    with pytest.raises(ModelError, match="Model output must be a JSON object"):
        parse_json_object("42")

    # Multiple concatenated JSON objects must be rejected
    with pytest.raises(ModelError, match="Model returned invalid JSON"):
        parse_json_object('{"a": 1} {"b": 2}')

    # Malformed JSON must raise ModelError("Model returned invalid JSON")
    with pytest.raises(ModelError, match="Model returned invalid JSON"):
        parse_json_object('{"a":')

    with pytest.raises(ModelError, match="Model returned invalid JSON"):
        parse_json_object("plain text without any json")


# ==============================================================================
# 6. Retry Delay & Error Extraction Helper Tests
# ==============================================================================

def test_retry_delay_seconds_and_headers():
    settings = Settings(api_key="mock-key", transport_backoff=1.5)
    client = TrammayClient(settings)
    try:
        # Retry-After header parsed and capped at 30.0
        resp_5 = httpx.Response(429, headers={"Retry-After": "5.5"})
        assert client._retry_delay_seconds(0, resp_5) == 5.5

        resp_60 = httpx.Response(429, headers={"Retry-After": "60"})
        assert client._retry_delay_seconds(0, resp_60) == 30.0

        # Negative header falls back to exponential backoff
        resp_neg = httpx.Response(429, headers={"Retry-After": "-2"})
        assert client._retry_delay_seconds(0, resp_neg, status_code=429) == 1.5

        # 429 exponential backoff capped at 20.0
        assert client._retry_delay_seconds(4, status_code=429) == 20.0

        # 500 exponential backoff capped at 10.0
        assert client._retry_delay_seconds(4, status_code=500) == 10.0
    finally:
        client.close()


def test_extract_payload_provider_error():
    assert _extract_payload_provider_error(
        {"error": {"code": 429, "message": "Rate limit"}}
    ) == (429, "Rate limit")
    assert _extract_payload_provider_error(
        {"error": {"code": "502", "message": "Bad gateway"}}
    ) == (502, "Bad gateway")
    assert _extract_payload_provider_error({"error": {"message": "Unknown"}}) == (None, "Unknown")
    assert _extract_payload_provider_error({"choices": []}) is None
    assert _extract_payload_provider_error("not a dict") is None  # type: ignore[arg-type]


def test_missing_api_key_message_contains_trammay_key():
    settings = Settings(api_key="")
    with pytest.raises(ModelError, match="TRAMMAY_KEY"):
        TrammayClient(settings)
