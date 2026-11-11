"""Bounded HTTP backend. Credentials and raw HTTP bodies never enter errors/logs."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from bizreason.config import Settings, normalize_model_id

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504, 520, 522, 524}
# Status codes a tunnel returns when its origin is gone; treated like transport errors
# only when an endpoint_resolver is configured.
TUNNEL_DOWN_STATUS_CODES = {502, 503, 504, 530}
ENDPOINT_RESOLVE_MIN_INTERVAL = 15.0
OUTAGE_POLL_SECONDS = 20.0

__all__ = [
    "LanguageModel",
    "ModelError",
    "ProviderError",
    "RETRYABLE_STATUS_CODES",
    "TrammayClient",
    "Usage",
    "_extract_payload_provider_error",
    "endpoint_file_resolver",
    "_repair_json_syntax",
    "build_chat_messages",
    "normalize_model_id",
    "parse_json_object",
]


class ModelError(RuntimeError):
    pass


class ProviderError(ModelError):
    """Transport/auth/provider failure; SQL repair cannot resolve it."""


@dataclass
class Usage:
    calls: int = 0
    tokens: int = 0


class LanguageModel(Protocol):
    usage: Usage

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]: ...


def _extract_payload_provider_error(payload: dict[str, Any]) -> tuple[int | None, str] | None:
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if isinstance(err, dict):
        raw_code = err.get("code")
        code: int | None = None
        if raw_code is not None:
            try:
                code = int(raw_code)
            except (ValueError, TypeError):
                code = None
        message = str(err.get("message") or "")
        return code, message
    return None


def build_chat_messages(
    model: str,
    system: str,
    prompt: str,
    *,
    merge_system: bool = False,
) -> list[dict[str, str]]:
    model_lower = (model or "").lower()
    if merge_system or bool(re.search(r"gemma-[23]\b", model_lower)):
        content = f"{system}\n\n{prompt}" if system else prompt
        return [{"role": "user", "content": content}]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]


def _repair_json_syntax(candidate: str) -> str:
    stripped = candidate.lstrip()
    is_json_obj = stripped.startswith("{")

    out: list[str] = []
    in_string = False
    escaped = False
    str_start_out_idx = -1
    str_is_key = False
    stack: list[str] = []
    i = 0
    n = len(candidate)
    while i < n:
        ch = candidate[i]
        if in_string:
            if escaped:
                escaped = False
                out.append(ch)
            elif ch == "\\":
                escaped = True
                out.append(ch)
            elif ch == '"':
                in_string = False
                out.append(ch)
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(ch)
            i += 1
        else:
            if ch == '"':
                in_string = True
                str_start_out_idx = len(out)
                prev_ch = None
                for k in range(len(out) - 1, -1, -1):
                    if not out[k].isspace():
                        prev_ch = out[k]
                        break
                str_is_key = bool(stack and stack[-1] == "}" and prev_ch != ":")
                out.append(ch)
                i += 1
            elif ch == "/" and i + 1 < n and candidate[i + 1] == "/":
                i += 2
                while i < n and candidate[i] not in ("\n", "\r"):
                    i += 1
            else:
                if ch in "{[":
                    stack.append("}" if ch == "{" else "]")
                elif ch in "}]":
                    if stack and stack[-1] == ch:
                        stack.pop()
                    elif stack and ch in stack:
                        while stack and stack[-1] != ch:
                            stack.pop()
                        if stack:
                            stack.pop()
                out.append(ch)
                i += 1

    if is_json_obj and '"sql"' in candidate and (in_string or stack):
        if in_string:
            if str_is_key:
                del out[str_start_out_idx:]
                while out and out[-1].isspace():
                    out.pop()
                if out and out[-1] == ",":
                    out.pop()
            else:
                if escaped and out:
                    out.pop()
                out.append('"')
            in_string = False

        prev_ch = None
        for k in range(len(out) - 1, -1, -1):
            if not out[k].isspace():
                prev_ch = out[k]
                break
        if prev_ch == ":":
            out.append(" null")
        elif prev_ch == ",":
            while out and (out[-1].isspace() or out[-1] == ","):
                out.pop()

        while stack:
            out.append(stack.pop())

    repaired = "".join(out)
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    return repaired


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<thought>[\s\S]*?</thought>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<\|channel>thought[\s\S]*?<channel\|>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<\|thought>[\s\S]*?<\|turn>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\s*<\|thought>[\s\S]*?(?=\{)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<\|thought>[\s\S]*?(?=\{)", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^\s*###\s*SQL\s*:?\s*", "", cleaned, flags=re.IGNORECASE).strip()

    fast_text = cleaned
    if fast_text.startswith("```"):
        lines = fast_text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            fast_text = "\n".join(lines[1:-1])
    try:
        value = json.loads(fast_text)
        if not isinstance(value, dict):
            raise ModelError("Model output must be a JSON object")
        return value
    except json.JSONDecodeError:
        pass

    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.IGNORECASE)
    if m:
        block = m.group(1).strip()
        try:
            val = json.loads(block)
            if not isinstance(val, dict):
                raise ModelError("Model output must be a JSON object")
            return val
        except json.JSONDecodeError:
            pass

    idx = cleaned.find("{")
    if idx != -1:
        try:
            obj, end = json.JSONDecoder().raw_decode(cleaned[idx:])
            if isinstance(obj, dict):
                suffix = cleaned[idx + end:].strip()
                if "{" not in suffix:
                    return obj
        except json.JSONDecodeError:
            pass

    candidates: list[str] = []
    if fast_text:
        candidates.append(fast_text)
    if m:
        candidates.append(m.group(1).strip())
    if idx != -1:
        candidates.append(cleaned[idx:])

    for cand in candidates:
        repaired = _repair_json_syntax(cand)
        try:
            val = json.loads(repaired)
            if not isinstance(val, dict):
                raise ModelError("Model output must be a JSON object")
            return val
        except json.JSONDecodeError:
            pass
        try:
            obj, end = json.JSONDecoder().raw_decode(repaired)
            if isinstance(obj, dict):
                suffix = repaired[end:].strip()
                if "{" not in suffix:
                    return obj
        except json.JSONDecodeError:
            pass

    # Fallback A: Try boundary-aware regex extraction of "sql": "..." from cleaned
    sql_boundary_m = re.search(
        r'"sql"\s*:\s*"(\s*(?:SELECT|WITH)\b[\s\S]+?)"\s*(?:,\s*"(?:parameters|template_eligible|filters|group_by|entity_constraints|joins|unresolved|probe_requests|unsupported|limit|units|output|status_policy|required_fields|rule_refs|windows|tables|metric|version|type)"\s*:|\}\s*(?:```)?\s*$)',
        cleaned,
        re.IGNORECASE,
    )
    if not sql_boundary_m and fast_text != cleaned:
        sql_boundary_m = re.search(
            r'"sql"\s*:\s*"(\s*(?:SELECT|WITH)\b[\s\S]+?)"\s*(?:,\s*"(?:parameters|template_eligible|filters|group_by|entity_constraints|joins|unresolved|probe_requests|unsupported|limit|units|output|status_policy|required_fields|rule_refs|windows|tables|metric|version|type)"\s*:|\}\s*(?:```)?\s*$)',
            fast_text,
            re.IGNORECASE,
        )

    raw_sql_val = None
    if sql_boundary_m:
        raw_sql_val = sql_boundary_m.group(1)
    else:
        # Check if truncated JSON where "sql" was the last field
        sql_trunc_m = re.search(
            r'"sql"\s*:\s*"(\s*(?:SELECT|WITH)\b[\s\S]+)$',
            cleaned.rstrip("`").strip(),
            re.IGNORECASE,
        )
        if sql_trunc_m and not re.search(r',\s*"[A-Za-z0-9_]+"\s*:', sql_trunc_m.group(1)):
            raw_sql_val = sql_trunc_m.group(1)
        else:
            m_sql = re.search(r'"sql"\s*:\s*"((?:[^"\\]|\\.)*)"', cleaned)
            if m_sql:
                raw_sql_val = m_sql.group(1)
            elif sql_trunc_m:
                raw_sql_val = sql_trunc_m.group(1)

    if raw_sql_val is not None:
        try:
            extracted_sql = json.loads('"' + raw_sql_val + '"')
        except Exception:
            extracted_sql = raw_sql_val.replace('\\"', '"').replace('\\n', ' ').replace('\\t', ' ')
        extracted_sql = (
            extracted_sql.replace('\\"', '"')
            .replace('\\n', ' ')
            .replace('\\t', ' ')
            .strip()
        )
        if extracted_sql.endswith('"') and extracted_sql.count('"') % 2 == 1:
            extracted_sql = extracted_sql[:-1].rstrip()
        extracted_sql = extracted_sql.rstrip(';').strip()
        if re.match(r"^(?:SELECT\b|WITH\b)", extracted_sql, re.IGNORECASE):
            fallback_res: dict[str, Any] = {"sql": extracted_sql}
            m_metric = re.search(r'"metric"\s*:\s*"([^"]+)"', cleaned)
            if m_metric:
                fallback_res["metric"] = m_metric.group(1).strip()
            m_type = re.search(r'"type"\s*:\s*"([^"]+)"', cleaned)
            if m_type:
                fallback_res["output"] = {"type": m_type.group(1).strip()}
            return fallback_res

    # Check fenced SQL code block
    sql_m = re.search(r"```(?:sql)?\s*([\s\S]*?)```", cleaned, re.IGNORECASE)
    if sql_m:
        fence_cand = sql_m.group(1).strip()
        fence_cand = re.sub(r"^\s*###\s*SQL\s*:?\s*", "", fence_cand, flags=re.IGNORECASE).strip()
        if ";" in fence_cand:
            fence_cand = fence_cand.split(";", 1)[0].strip()
        if "{" not in fence_cand and re.match(
            r"^(?:SELECT\b[\s\S]+\bFROM\b|WITH\b[\s\S]+\bSELECT\b)",
            fence_cand,
            re.IGNORECASE,
        ):
            return {"sql": fence_cand}

    # Check raw SQL when the output does not start with a JSON brace
    raw_cand = cleaned.strip()
    if "{" not in raw_cand:
        if re.match(r"^\s*UNSUPPORTED\b", raw_cand, re.IGNORECASE):
            return {
                "metric": "unsupported",
                "unsupported": {
                    "kind": "missing_field",
                    "required": ["customer_address"],
                    "explanation": raw_cand[:300],
                    "source_refs": ["README.md"],
                },
            }
        raw_sql_start = re.search(r"(?:^|\n)\s*((?:WITH\b|SELECT\b)[\s\S]+)", raw_cand, re.IGNORECASE)
        if raw_sql_start:
            sql_body = raw_sql_start.group(1).strip()
            if ";" in sql_body:
                sql_body = sql_body.split(";", 1)[0].strip()
            if re.match(
                r"^(?:SELECT\b[\s\S]+\bFROM\b|WITH\b[\s\S]+\bAS\s*\([\s\S]+)",
                sql_body,
                re.IGNORECASE,
            ):
                return {"sql": sql_body}

    # Fallback B: Search cleaned for any standalone SQL query when it's not inside {...}
    standalone_m = re.search(
        r"(?:^|\n)\s*((?:WITH\b[\s\S]+\)\s*)?SELECT\b[\s\S]+?\bFROM\b[\s\S]+?)(?:;|\n\s*\n|\Z)",
        cleaned,
        re.IGNORECASE,
    )
    if standalone_m:
        sql_candidate = standalone_m.group(1).strip().rstrip(";").strip()
        if "{" not in sql_candidate and "}" not in sql_candidate:
            return {"sql": sql_candidate}

    raise ModelError("Model returned invalid JSON")


def endpoint_file_resolver(path: Path) -> Callable[[], str | None]:
    """Return a resolver that reads the "base_url" field of a JSON file on each call.

    Returns None on any error (missing file, bad JSON, missing field). File content is never logged.
    """
    file_path = Path(path)

    def _resolve() -> str | None:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        value = data.get("base_url")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    return _resolve


class TrammayClient:
    # Class-level defaults so subclasses that bypass __init__ still behave as before.
    _endpoint_resolver: Callable[[], str | None] | None = None
    _outage_wait_seconds: float = 0.0
    _outage_poll_seconds: float = OUTAGE_POLL_SECONDS
    _last_resolve_at: float | None = None
    _transport: httpx.BaseTransport | None = None

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        endpoint_resolver: Callable[[], str | None] | None = None,
        outage_wait_seconds: float = 0.0,
        outage_poll_seconds: float = OUTAGE_POLL_SECONDS,
    ):
        if not settings.api_key:
            raise ModelError("TRAMMAY_KEY is missing (set TRAMMAY_KEY or OPENROUTER_API in source_test/.env)")
        if outage_poll_seconds <= 0:
            raise ValueError("outage_poll_seconds must be positive")
        self.settings = settings
        self.usage = Usage()
        self._transport = transport
        self._endpoint_resolver = endpoint_resolver
        self._outage_wait_seconds = float(outage_wait_seconds)
        self._outage_poll_seconds = float(outage_poll_seconds)
        self._last_resolve_at = None
        self._client = self._create_client(transport=transport)

    def _create_client(self, transport: httpx.BaseTransport | None = None) -> httpx.Client:
        return httpx.Client(
            base_url=self.settings.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {self.settings.api_key}"},
            timeout=self.settings.request_timeout,
            transport=transport,
            follow_redirects=False,
        )

    def _reset_client_if_transport_error(self, error: Exception) -> None:
        if isinstance(error, httpx.TransportError):
            current_transport = getattr(self._client, "_transport", None)
            if not isinstance(current_transport, httpx.MockTransport):
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = self._create_client()

    def _retry_delay_seconds(
        self,
        attempt: int,
        response: httpx.Response | None = None,
        *,
        status_code: int | None = None,
    ) -> float:
        if response is not None:
            raw_header = response.headers.get("retry-after")
            if raw_header:
                try:
                    val = float(raw_header)
                    if val > 0:
                        return min(val, 30.0)
                except (ValueError, TypeError):
                    pass
        if self.settings.transport_backoff <= 0:
            return 0.0
        effective_code = (
            status_code
            if status_code is not None
            else (response.status_code if response is not None else None)
        )
        cap = 20.0 if effective_code == 429 else 10.0
        return min(self.settings.transport_backoff * (2 ** attempt), cap)

    def close(self) -> None:
        self._client.close()

    def _switch_base_url(self, new_url: str) -> None:
        # Only the host is logged; the query string and headers never are.
        logger.info("Model endpoint changed; rebuilding client for host %s", urlsplit(new_url).hostname or "?")
        self.settings = replace(self.settings, base_url=new_url)
        old_client = self._client
        self._client = self._create_client(transport=self._transport)
        try:
            old_client.close()
        except Exception:
            pass

    def _try_switch_endpoint(self) -> bool:
        """Ask the resolver for the current base URL; switch if it changed. Rate-limited."""
        if self._endpoint_resolver is None:
            return False
        now = time.monotonic()
        if self._last_resolve_at is not None and now - self._last_resolve_at < ENDPOINT_RESOLVE_MIN_INTERVAL:
            return False
        self._last_resolve_at = now
        try:
            candidate = self._endpoint_resolver()
        except Exception:
            return False
        if not isinstance(candidate, str):
            return False
        candidate = candidate.strip().rstrip("/")
        parts = urlsplit(candidate)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return False
        if candidate == self.settings.base_url.strip().rstrip("/"):
            return False
        self._switch_base_url(candidate)
        return True

    def _request(self, method: str, path: str, *, count_call: bool = False, **kwargs: Any) -> dict[str, Any]:
        max_attempts = (
            max(1 + self.settings.transport_retries, 4)
            if self.settings.transport_retries > 0
            else 1
        )
        req_timeout = kwargs.pop("timeout", None)
        effective_timeout = (
            float(req_timeout)
            if req_timeout is not None
            else float(self.settings.request_timeout)
        )
        started = time.monotonic()
        last_error: ProviderError | None = None
        call_counted = False
        # Set once normal attempts are exhausted by transport errors and outage waiting is enabled.
        outage_deadline: float | None = None
        attempt = 0

        while True:
            elapsed = time.monotonic() - started
            remaining = effective_timeout - elapsed
            # During an outage wait the implicit (non-caller) deadline does not apply;
            # the outage deadline governs instead.
            if remaining <= 0 and outage_deadline is None:
                raise ProviderError("Provider request deadline exceeded")
            if req_timeout is not None and attempt > 0 and remaining <= 5.0:
                if last_error:
                    raise last_error
                raise ProviderError("Provider request deadline exceeded")

            attempt_timeout = (
                min(remaining, self.settings.request_timeout)
                if req_timeout is not None
                else self.settings.request_timeout
            )
            last_response: httpx.Response | None = None
            last_status_code: int | None = None
            transport_failure = False

            try:
                response = self._client.request(
                    method, path, timeout=attempt_timeout, **kwargs
                )
                last_response = response
                last_status_code = response.status_code
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ProviderError("Provider returned a non-object response")

                err_info = _extract_payload_provider_error(payload)
                if err_info is not None:
                    err_code, _ = err_info
                    if err_code in RETRYABLE_STATUS_CODES:
                        last_error = ProviderError(f"Provider error {err_code or 'unknown'}")
                        last_status_code = err_code
                    else:
                        if count_call and not call_counted:
                            self.usage.calls += 1
                            call_counted = True
                        raise ProviderError(f"Provider error {err_code or 'unknown'}")
                else:
                    if count_call and not call_counted:
                        self.usage.calls += 1
                        call_counted = True
                    return payload
            except httpx.HTTPStatusError as error:
                status_code = error.response.status_code
                last_response = error.response
                last_status_code = status_code
                tunnel_down = (
                    self._endpoint_resolver is not None and status_code in TUNNEL_DOWN_STATUS_CODES
                )
                if status_code not in RETRYABLE_STATUS_CODES and not tunnel_down:
                    if count_call and not call_counted:
                        self.usage.calls += 1
                        call_counted = True
                    raise ProviderError(f"Provider HTTP {status_code}") from None
                last_error = ProviderError(f"Provider HTTP {status_code}")
                transport_failure = tunnel_down
            except (httpx.HTTPError, ValueError) as error:
                self._reset_client_if_transport_error(error)
                last_response = None
                last_status_code = None
                last_error = ProviderError(f"Provider request failed ({type(error).__name__})")
                transport_failure = True

            switched = False
            if transport_failure and self._endpoint_resolver is not None:
                switched = self._try_switch_endpoint()

            # A successful endpoint switch always grants one immediate retry on the new URL.
            if attempt >= max_attempts - 1 and not switched:
                assert last_error is not None
                if not (
                    transport_failure
                    and self._endpoint_resolver is not None
                    and self._outage_wait_seconds > 0
                ):
                    raise last_error
                now = time.monotonic()
                if outage_deadline is None:
                    outage_deadline = now + self._outage_wait_seconds
                if now >= outage_deadline:
                    raise last_error
                rem = effective_timeout - (now - started)
                if req_timeout is not None and rem <= 5.0:
                    raise last_error
                wait = min(self._outage_poll_seconds, outage_deadline - now)
                if req_timeout is not None:
                    wait = min(wait, rem - 5.0)
                time.sleep(max(wait, 0.0))
                attempt += 1
                continue

            rem = effective_timeout - (time.monotonic() - started)
            if req_timeout is not None and rem <= 5.0:
                assert last_error is not None
                raise last_error

            if not switched:
                delay = self._retry_delay_seconds(
                    attempt, last_response, status_code=last_status_code
                )
                if delay > 0:
                    time.sleep(delay)
            attempt += 1

    def models(self) -> list[str]:
        payload = self._request("GET", "models")
        items = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            items = payload.get("models", []) if isinstance(payload, dict) else []
        return [
            item["id"]
            for item in items
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]:
        if self.settings.api_style == "responses":
            payload = self._request(
                "POST",
                "responses",
                count_call=True,
                timeout=timeout,
                json={
                    "model": self.settings.model,
                    "instructions": system,
                    "input": prompt,
                    "max_output_tokens": self.settings.max_output_tokens,
                },
            )
            parts = [
                part.get("text", "")
                for item in payload.get("output", [])
                for part in item.get("content", [])
                if part.get("type") == "output_text"
            ]
            text = "".join(parts) or payload.get("output_text", "")
            # Fall back to choices[0].message.content if responses returns chat.completion shape
            if not text:
                choices = payload.get("choices", [])
                if choices:
                    if choices[0].get("finish_reason") == "length":
                        raise ModelError("Provider returned no complete candidate")
                    msg = choices[0].get("message", {}).get("content", "")
                    if isinstance(msg, list):
                        text = "".join(part.get("text", "") for part in msg if isinstance(part, dict))
                    elif isinstance(msg, str):
                        text = msg
            if payload.get("status") == "incomplete":
                raise ModelError("Provider returned no complete candidate")
        else:
            def _build_chat_payload(merge: bool) -> dict[str, Any]:
                return {
                    "model": self.settings.model,
                    "messages": build_chat_messages(
                        self.settings.model, system, prompt, merge_system=merge
                    ),
                    "temperature": 0,
                    "max_tokens": self.settings.max_output_tokens,
                }

            initial_messages = build_chat_messages(
                self.settings.model, system, prompt, merge_system=False
            )
            sent_separate_system = any(m.get("role") == "system" for m in initial_messages)
            is_non_trammay = "trammay.xyz" not in self.settings.base_url.lower()

            try:
                payload = self._request(
                    "POST",
                    "chat/completions",
                    count_call=True,
                    timeout=timeout,
                    json=_build_chat_payload(merge=False),
                )
            except ProviderError as exc:
                if (
                    is_non_trammay
                    and sent_separate_system
                    and ("Provider HTTP 400" in str(exc) or "Provider error 400" in str(exc))
                ):
                    if self.usage.calls > 0:
                        self.usage.calls -= 1
                    payload = self._request(
                        "POST",
                        "chat/completions",
                        count_call=True,
                        timeout=timeout,
                        json=_build_chat_payload(merge=True),
                    )
                else:
                    raise

            choices = payload.get("choices", [])
            if not choices or choices[0].get("finish_reason") == "length":
                raise ModelError("Provider returned no complete candidate")
            text = choices[0].get("message", {}).get("content", "")
            if isinstance(text, list):
                text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        usage = payload.get("usage") or payload.get("response", {}).get("usage") or {}
        total = (
            usage.get("total_tokens")
            or (usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
            or (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
            or 0
        )
        self.usage.tokens += int(total)
        if not isinstance(text, str) or not text.strip():
            raise ModelError("Provider returned an empty candidate")
        return parse_json_object(text)
