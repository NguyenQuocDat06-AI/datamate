"""Concrete review checks: fallback deadline, provider attempts, and failed JSON repair."""
import json
from types import SimpleNamespace

import httpx
import pytest

from bizreason.config import Settings
from bizreason.llm import ProviderError, TrammayClient
from bizreason.semantic import rulebook_params as rp


def test_system_role_fallback_cannot_restart_an_exhausted_deadline(monkeypatch):
    import bizreason.llm as llm
    clock = [10.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock[0])
    client = TrammayClient(Settings(api_key="synthetic", base_url="http://unit.invalid/v1"))
    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            clock[0] += kwargs["timeout"]
            raise ProviderError("Provider HTTP 400")
        return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    monkeypatch.setattr(client, "_request", request)
    try:
        with pytest.raises(ProviderError, match="deadline"):
            client.reask_json("schema", "document", {"bad": 1}, "fix", timeout=2)
        assert calls == [2]
    finally:
        client.close()


def test_http_attempts_are_distinct_from_logical_model_calls():
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
    client = TrammayClient(Settings(api_key="synthetic", transport_backoff=0), transport=httpx.MockTransport(handler))
    try:
        # The existing transport reserves five seconds before retrying an error.
        assert client.generate_json("schema", "document", timeout=15) == {}
        assert client.usage.calls == 1 and client.usage.http_attempts == 2
    finally:
        client.close()


def test_repair_receives_only_the_remaining_timeout_in_the_real_client(monkeypatch):
    import bizreason.llm as llm
    clock = [10.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock[0])
    client = TrammayClient(Settings(api_key="synthetic"))
    timeouts = []
    def request(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        text = '{"valid_sale_statuses":["source label"]}' if len(timeouts) == 1 else '{"valid_sale_statuses":["completed"]}'
        clock[0] += 1
        return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
    monkeypatch.setattr(client, "_request", request)
    try:
        params, _ = rp.extract_rulebook_params("Only finalized records qualify.", client,
                                             context_mode="full", validation_retries=1, timeout=3)
        assert params.valid_sale_statuses == ("completed",)
        assert timeouts == [3, 2]
    finally:
        client.close()


def test_empty_status_repair_cannot_be_cached(tmp_path):
    class Model:
        def generate_json(self, *args, **kwargs):
            return {"valid_sale_statuses": ["source label"]}
        def reask_json(self, *args, **kwargs):
            return {"valid_sale_statuses": []}
    doc = tmp_path / "policy.md"; doc.write_text("Only finalized records qualify.")
    cache = tmp_path / "cache.json"
    params, taken = rp.load_or_extract(doc, Model(), cache, context_mode="full", validation_retries=1)
    assert "valid_sale_statuses" not in taken and not cache.exists()


def test_real_client_factory_does_not_reuse_a_closed_parameter_client(tmp_path, monkeypatch):
    from bizreason import run_v2
    connection = tmp_path / "synthetic_connection.json"
    connection.write_text(json.dumps({"model": "test-model", "bearer_token": "synthetic"}))
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
    def construct(settings, **kwargs):
        return TrammayClient(settings, transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(run_v2, "TrammayClient", construct)
    make = run_v2._client_factory(SimpleNamespace(connection=connection, endpoint_file=None,
                                                 base_url="http://unit.invalid/v1", outage_wait=0))
    parameter = make(1500, 280)
    parameter.close()
    annotation = make(1500, 280)
    try:
        assert annotation is not parameter
        assert annotation.generate_json("schema", "document", timeout=2) == {}
    finally:
        annotation.close()
