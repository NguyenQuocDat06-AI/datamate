"""Schema repair must retain the failed assistant turn and use normal transport accounting."""
import json

import httpx
import pytest

from bizreason.config import Settings
from bizreason.llm import TrammayClient


def test_chat_reask_preserves_history_and_accounts_usage():
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"label":"completed"}'},
                                                   "finish_reason": "stop"}], "usage": {"total_tokens": 7}})
    client = TrammayClient(Settings(api_key="synthetic", base_url="http://unit.invalid/v1"),
                          transport=httpx.MockTransport(handler))
    try:
        previous = {"label": "source wording"}
        result = client.reask_json("schema", "document", previous, "invalid enum", timeout=3)
        assert result == {"label": "completed"}
        messages = seen[0]["messages"]
        assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
        assert messages[0]["content"] == "schema" and messages[1]["content"] == "document"
        assert json.loads(messages[2]["content"]) == previous
        assert messages[3]["content"] == "invalid enum"
        assert client.usage.calls == 1 and client.usage.tokens == 7
    finally:
        client.close()


def test_system_role_fallback_keeps_the_repair_history():
    seen = []
    def handler(request):
        data = json.loads(request.content); seen.append(data)
        if len(seen) == 1:
            return httpx.Response(400, json={"error": "unsupported system"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
    client = TrammayClient(Settings(api_key="synthetic", base_url="http://unit.invalid/v1", transport_retries=0),
                          transport=httpx.MockTransport(handler))
    try:
        assert client.reask_json("schema", "document", {"x": 1}, "fix x", timeout=3) == {}
        messages = seen[1]["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant", "user"]
        assert "schema" in messages[0]["content"] and "document" in messages[0]["content"]
        assert json.loads(messages[1]["content"]) == {"x": 1}
        assert messages[-1]["content"] == "fix x"
    finally:
        client.close()


def test_responses_reask_has_the_same_user_assistant_user_history():
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"output": [{"content": [{"type": "output_text", "text": "{}"}]}]})
    client = TrammayClient(Settings(api_key="synthetic", api_style="responses"), transport=httpx.MockTransport(handler))
    try:
        assert client.reask_json("schema", "document", {"x": 1}, "fix x", timeout=3) == {}
        assert seen[0]["instructions"] == "schema"
        assert [m["role"] for m in seen[0]["input"]] == ["user", "assistant", "user"]
    finally:
        client.close()


def test_invalid_reask_request_is_rejected_before_transport():
    def handler(request):
        raise AssertionError("invalid correction must not reach the backend")
    client = TrammayClient(Settings(api_key="synthetic"), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ValueError):
            client.reask_json("schema", "document", [], "fix", timeout=3)
        with pytest.raises(ValueError):
            client.reask_json("schema", "document", {}, "", timeout=3)
    finally:
        client.close()
