from __future__ import annotations

import random
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from bizreason import llm as llm_module
from bizreason.config import Settings
from bizreason.llm import ProviderError, TrammayClient
from bizreason.semantic.intent import IntentParser

BASE_URL = "https://flaky.example/v1"
OK_PAYLOAD = {
    "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
    "usage": {"total_tokens": 1},
}


class _Transport(httpx.BaseTransport):
    """Not an httpx.MockTransport, so failures really rebuild the shared client."""

    def __init__(self, handler):
        self.handler = handler

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self.handler(request)


def _settings(**overrides) -> Settings:
    values = {"api_key": "mock-key", "transport_retries": 2, "transport_backoff": 0.0}
    values.update(overrides)
    return Settings(base_url=BASE_URL, **values)


def _track_clients(client: TrammayClient, monkeypatch) -> list[httpx.Client]:
    """Record every httpx client the TrammayClient creates, in order."""
    created = [client._client]
    real_create = client._create_client

    def counting_create(transport=None):
        new = real_create(transport=transport)
        created.append(new)
        return new

    monkeypatch.setattr(client, "_create_client", counting_create)
    return created


def test_random_transport_faults_only_surface_as_provider_error(monkeypatch):
    rng = random.Random(1234)
    rng_lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with rng_lock:
            roll = rng.random()
        if roll < 0.2:
            raise httpx.ReadTimeout("read timed out", request=request)
        if roll < 0.4:
            raise ssl.SSLError("EOF occurred in violation of protocol")
        return httpx.Response(200, json=OK_PAYLOAD)

    client = TrammayClient(_settings(), transport=_Transport(handler))
    created = _track_clients(client, monkeypatch)

    def worker(_: int) -> tuple[int, list[BaseException]]:
        ok = 0
        other: list[BaseException] = []
        for _ in range(20):
            try:
                assert client.generate_json("system", "prompt", timeout=30) == {"ok": True}
                ok += 1
            except BaseException as exc:  # noqa: BLE001 - collecting every escape
                if type(exc) is not ProviderError:
                    other.append(exc)
        return ok, other

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(worker, range(16)))

    escaped = [exc for _, errors in results for exc in errors]
    assert escaped == []
    total_ok = sum(ok for ok, _ in results)
    assert 0 < total_ok <= 320

    client.close()
    assert all(c.is_closed for c in created)


def test_failures_on_one_client_generation_rebuild_once(monkeypatch):
    threads = 16
    barrier = threading.Barrier(threads, timeout=10)
    seen_lock = threading.Lock()
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        # The first 16 requests all hold the original client before any of them fails.
        with seen_lock:
            seen["n"] += 1
            first_wave = seen["n"] <= threads
        if first_wave:
            barrier.wait()
            raise httpx.ReadTimeout("read timed out", request=request)
        return httpx.Response(200, json=OK_PAYLOAD)

    transport = _Transport(handler)
    client = TrammayClient(_settings(transport_retries=1), transport=transport)
    original = client._client
    created = _track_clients(client, monkeypatch)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        results = list(pool.map(lambda _: client.generate_json("s", "p", timeout=30), range(threads)))

    assert results == [{"ok": True}] * threads
    rebuilds = len(created) - 1
    assert rebuilds == 1
    assert client._client is not original
    assert client._client._transport is transport
    client.close()


def test_direct_reset_with_stale_client_does_nothing():
    transport = _Transport(lambda request: httpx.Response(200, json=OK_PAYLOAD))
    client = TrammayClient(_settings(), transport=transport)
    stale = client._client
    request = httpx.Request("POST", BASE_URL + "/chat/completions")

    client._reset_client_if_transport_error(httpx.ConnectError("gone", request=request), stale)
    current = client._client
    assert current is not stale

    client._reset_client_if_transport_error(httpx.ConnectError("gone", request=request), stale)
    assert client._client is current
    client.close()


def test_retired_clients_are_closed_beyond_the_limit_and_on_close(monkeypatch):
    transport = _Transport(lambda request: httpx.Response(200, json=OK_PAYLOAD))
    client = TrammayClient(_settings(), transport=transport)
    created = _track_clients(client, monkeypatch)
    request = httpx.Request("POST", BASE_URL + "/chat/completions")
    error = httpx.ReadTimeout("slow", request=request)

    rebuilds = 10
    for _ in range(rebuilds):
        client._reset_client_if_transport_error(error, client._client)

    assert len(created) == rebuilds + 1
    retired = client._retired_clients
    assert len(retired) == llm_module.RETIRED_CLIENT_LIMIT
    assert retired == created[rebuilds - llm_module.RETIRED_CLIENT_LIMIT : rebuilds]
    closed = [c for c in created if c.is_closed]
    assert closed == created[: rebuilds - llm_module.RETIRED_CLIENT_LIMIT]
    assert not client._client.is_closed

    client.close()
    assert all(c.is_closed for c in created)


def test_runtime_error_from_closed_client_is_retried():
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Cannot send a request, as the client has been closed.")
        return httpx.Response(200, json={"data": [{"id": "model-a"}]})

    client = TrammayClient(_settings(), transport=_Transport(handler))
    try:
        assert client.models() == ["model-a"]
    finally:
        client.close()
    assert len(attempts) == 2


@pytest.mark.parametrize(
    "exc",
    [
        ssl.SSLError("EOF occurred in violation of protocol"),
        RuntimeError("Cannot send a request, as the client has been closed."),
        OSError("connection reset"),
    ],
    ids=["ssl", "closed-client", "oserror"],
)
def test_persistent_non_http_errors_end_as_provider_error(exc):
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise exc

    client = TrammayClient(_settings(transport_retries=2), transport=_Transport(handler))
    try:
        with pytest.raises(ProviderError, match=type(exc).__name__) as info:
            client.generate_json("system", "prompt", timeout=30)
        assert type(info.value) is ProviderError
    finally:
        client.close()
    assert len(attempts) == 4


class _AlwaysRaises:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def generate_json(self, system: str, prompt: str, *, timeout: float):
        raise self.exc


def test_intent_parse_reports_raw_transport_failure_as_model_failure():
    parser = IntentParser(_AlwaysRaises(RuntimeError("client has been closed")), max_attempts=2)
    result = parser.parse("question one")
    assert result.intent is None
    assert result.reason == "model call failed: RuntimeError"
    assert result.attempts == 2
