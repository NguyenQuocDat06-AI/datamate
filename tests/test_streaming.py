import json

import httpx
import pytest

from bizreason.config import Settings
from bizreason.llm import ModelError
from bizreason.streaming import StreamingTrammayClient


def test_streaming_assembles_content_and_ignores_reasoning():
    events = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "internal"}}]},
        {"choices": [{"index": 0, "delta": {"content": '{"ok":'}}]},
        {
            "choices": [{"index": 0, "delta": {"content": "true}"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 12},
        },
    ]
    data = (
        ": heartbeat\n\n"
        + "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        + "data: [DONE]\n\n"
    )
    client = StreamingTrammayClient(
        Settings(api_key="test"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=data
            )
        ),
    )
    try:
        assert client.generate_json("system", "prompt", timeout=10) == {"ok": True}
        assert client.usage.calls == 1 and client.usage.tokens == 12
    finally:
        client.close()


def test_truncated_stream_cannot_become_a_candidate():
    event = {"choices": [{"delta": {"content": '{"ok":true}'}, "finish_reason": "length"}]}
    client = StreamingTrammayClient(
        Settings(api_key="test"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="data: " + json.dumps(event) + "\n\n",
            )
        ),
    )
    try:
        with pytest.raises(ModelError, match="truncated"):
            client.generate_json("system", "prompt", timeout=10)
    finally:
        client.close()
