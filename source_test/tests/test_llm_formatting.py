import json

import httpx
import pytest

from bizreason.config import Settings
from bizreason.contracts import OutputSpec, Prediction
from bizreason.formatting import SubmissionWriter, validate_answer
from bizreason.llm import ModelError, TrammayClient, parse_json_object


def test_model_listing_and_chat_transport_do_not_log_credentials():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "ag/gemini-3.8-flash-high"}]})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 17},
            },
        )

    settings = Settings(api_key="secret-unit-test")
    client = TrammayClient(settings, transport=httpx.MockTransport(handler))
    try:
        assert client.models() == [settings.model]
        assert client.generate_json("system", "input", timeout=5) == {"ok": True}
        assert client.usage.tokens == 17
        assert requests[1].url.path == "/v1/chat/completions"
        assert "secret-unit-test" not in repr(settings)
    finally:
        client.close()


def test_http_error_hides_provider_response_and_secret():
    client = TrammayClient(
        Settings(api_key="secret"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": "secret"})
        ),
    )
    try:
        with pytest.raises(ModelError, match="HTTP 401") as error:
            client.models()
        assert "secret" not in str(error.value)
    finally:
        client.close()


def test_responses_transport():
    client = TrammayClient(
        Settings(api_key="test", api_style="responses"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "output": [{"content": [{"type": "output_text", "text": '{"ok":true}'}]}],
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                },
            )
        ),
    )
    try:
        assert client.generate_json("system", "input", timeout=5) == {"ok": True}
        assert client.usage.tokens == 5
    finally:
        client.close()


def test_json_fence_and_incomplete_output():
    assert parse_json_object('```json\n{"a":1}\n```') == {"a": 1}
    with pytest.raises(ModelError):
        parse_json_object('{"a":')


def test_rounding_and_output_types():
    assert validate_answer(10.5, OutputSpec(type="number", rounding="money")) == 11
    assert validate_answer(-12.345, OutputSpec(type="percent")) == -12.35
    with pytest.raises(ValueError):
        validate_answer("10", OutputSpec(type="number"))
    with pytest.raises(ValueError):
        validate_answer(float("nan"), OutputSpec(type="number"))


def test_checkpoint_writer_flushes_and_rejects_duplicate_ids(workspace_tmp):
    target = workspace_tmp / "submission.jsonl"
    with SubmissionWriter(target) as writer:
        writer.write(Prediction(id="q1", type="number", answer=0))
        assert json.loads(target.read_text(encoding="utf-8")) == {
            "id": "q1",
            "type": "number",
            "answer": 0,
        }
        with pytest.raises(ValueError):
            writer.write(Prediction(id="q1", type="abstain"))
