from __future__ import annotations

import logging

import httpx
import pytest

from bizreason import llm as llm_module
from bizreason.config import Settings
from bizreason.llm import ProviderError, TrammayClient, endpoint_file_resolver

OLD_URL = "https://old.example/v1"
NEW_URL = "https://new.example/v1"
CHAT_PAYLOAD = {
    "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
    "usage": {"total_tokens": 3},
}


class FakeClock:
    """Stands in for the time module inside bizreason.llm; sleep() advances the clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(llm_module, "time", fake)
    return fake


def _settings(base_url: str = OLD_URL, **overrides) -> Settings:
    values = {"api_key": "mock-key", "transport_retries": 2, "transport_backoff": 0.0}
    values.update(overrides)
    return Settings(base_url=base_url, **values)


def test_endpoint_switch_after_connect_error_uses_new_url():
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "old.example":
            raise httpx.ConnectError("tunnel is down", request=request)
        return httpx.Response(200, json=CHAT_PAYLOAD)

    resolved: list[str] = []

    def resolver() -> str:
        resolved.append(NEW_URL)
        return NEW_URL

    client = TrammayClient(_settings(), transport=httpx.MockTransport(handler), endpoint_resolver=resolver)
    try:
        assert client.generate_json("system", "prompt", timeout=60) == {"ok": True}
        assert client.generate_json("system", "prompt", timeout=60) == {"ok": True}
    finally:
        client.close()

    assert hosts == ["old.example", "new.example", "new.example"]
    assert len(resolved) == 1
    assert client.settings.base_url == NEW_URL


def test_without_resolver_connect_error_still_raises_provider_error():
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        raise httpx.ConnectError("tunnel is down", request=request)

    client = TrammayClient(_settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError, match="ConnectError"):
            client.models()
    finally:
        client.close()

    assert request_count == 4
    assert client.settings.base_url == OLD_URL


def test_without_resolver_530_is_not_retried():
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(530, json={"error": "origin down"})

    client = TrammayClient(_settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError, match="HTTP 530"):
            client.models()
    finally:
        client.close()

    assert request_count == 1


@pytest.mark.parametrize("status", [502, 503, 504, 530])
def test_tunnel_down_status_with_resolver_switches_endpoint(status):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "old.example":
            return httpx.Response(status, json={"error": "origin gone"})
        return httpx.Response(200, json={"data": [{"id": "ag/model"}]})

    client = TrammayClient(
        _settings(),
        transport=httpx.MockTransport(handler),
        endpoint_resolver=lambda: NEW_URL,
    )
    try:
        assert client.models() == ["ag/model"]
    finally:
        client.close()

    assert client.settings.base_url == NEW_URL


def test_resolver_is_rate_limited_and_same_url_is_not_switched(clock):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("tunnel is down", request=request)

    def resolver() -> str:
        nonlocal calls
        calls += 1
        return OLD_URL  # unchanged URL: no switch

    client = TrammayClient(
        _settings(transport_retries=3),
        transport=httpx.MockTransport(handler),
        endpoint_resolver=resolver,
    )
    try:
        with pytest.raises(ProviderError):
            client.models()
    finally:
        client.close()

    # All attempts happen at the same fake instant, so the 15 s throttle allows one resolve.
    assert calls == 1
    assert client.settings.base_url == OLD_URL


def test_resolver_exception_is_swallowed(clock):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("tunnel is down", request=request)

    def resolver() -> str:
        raise OSError("boom")

    client = TrammayClient(_settings(), transport=httpx.MockTransport(handler), endpoint_resolver=resolver)
    try:
        with pytest.raises(ProviderError):
            client.models()
    finally:
        client.close()


def test_outage_wait_gives_up_after_total_wait_and_raises_last_error(clock):
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        raise httpx.ConnectError("tunnel is down", request=request)

    client = TrammayClient(
        _settings(),
        transport=httpx.MockTransport(handler),
        endpoint_resolver=lambda: None,
        outage_wait_seconds=60.0,
    )
    try:
        with pytest.raises(ProviderError, match="ConnectError"):
            client.models()
    finally:
        client.close()

    # 4 normal attempts, then one attempt per 20 s poll until 60 s have elapsed.
    assert request_count == 7
    assert clock.sleeps == [20.0, 20.0, 20.0]
    assert clock.now == 60.0


def test_outage_wait_recovers_when_resolver_reports_new_url(clock):
    hosts: list[str] = []
    resolver_results = iter([None, NEW_URL])

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "old.example":
            raise httpx.ConnectError("tunnel is down", request=request)
        return httpx.Response(200, json={"data": [{"id": "ag/model"}]})

    client = TrammayClient(
        _settings(),
        transport=httpx.MockTransport(handler),
        endpoint_resolver=lambda: next(resolver_results),
        outage_wait_seconds=60.0,
    )
    try:
        assert client.models() == ["ag/model"]
    finally:
        client.close()

    assert hosts == ["old.example"] * 5 + ["new.example"]
    assert clock.sleeps == [20.0]


def test_outage_wait_respects_explicit_caller_timeout(clock):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("tunnel is down", request=request)

    client = TrammayClient(
        _settings(),
        transport=httpx.MockTransport(handler),
        endpoint_resolver=lambda: None,
        outage_wait_seconds=600.0,
    )
    try:
        with pytest.raises(ProviderError):
            client._request("GET", "models", timeout=30)
    finally:
        client.close()

    assert clock.now <= 30.0


def test_endpoint_file_resolver_valid_file_returns_base_url(tmp_path):
    path = tmp_path / "endpoint.json"
    path.write_text('{"base_url": "https://new.example/v1", "other": 1}', encoding="utf-8")
    assert endpoint_file_resolver(path)() == "https://new.example/v1"


def test_endpoint_file_resolver_missing_file_returns_none(tmp_path):
    assert endpoint_file_resolver(tmp_path / "missing.json")() is None


def test_endpoint_file_resolver_bad_json_returns_none_without_logging_content(tmp_path, caplog):
    path = tmp_path / "endpoint.json"
    path.write_text('{"base_url": "https://new.example/v1", "token": SECRET_TOKEN', encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        assert endpoint_file_resolver(path)() is None
    assert "SECRET_TOKEN" not in caplog.text
    assert "new.example" not in caplog.text
