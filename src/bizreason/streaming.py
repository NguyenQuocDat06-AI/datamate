"""Chat-completions and Responses SSE transport with the same JSON planning contract."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from bizreason.llm import (
    ModelError,
    ProviderError,
    RETRYABLE_STATUS_CODES,
    TrammayClient,
    _extract_payload_provider_error,
    build_chat_messages,
    parse_json_object,
)


class StreamingTrammayClient(TrammayClient):
    @property
    def client(self) -> httpx.Client:
        return self._client

    @client.setter
    def client(self, value: httpx.Client) -> None:
        self._client = value

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]:
        is_non_trammay = "trammay.xyz" not in self.settings.base_url.lower()
        is_responses = self.settings.api_style == "responses"
        initial_messages = (
            []
            if is_responses
            else build_chat_messages(self.settings.model, system, prompt, merge_system=False)
        )
        sent_separate_system = any(m.get("role") == "system" for m in initial_messages)

        try:
            return self._stream_generate_json(
                system, prompt, timeout=timeout, merge_system=False
            )
        except ProviderError as exc:
            if (
                not is_responses
                and is_non_trammay
                and sent_separate_system
                and ("Provider HTTP 400" in str(exc) or "Provider error 400" in str(exc))
            ):
                if self.usage.calls > 0:
                    self.usage.calls -= 1
                return self._stream_generate_json(
                    system, prompt, timeout=timeout, merge_system=True
                )
            raise

    def _stream_generate_json(
        self, system: str, prompt: str, *, timeout: float, merge_system: bool = False
    ) -> dict[str, Any]:
        started = time.monotonic()
        deadline = started + timeout
        is_responses = self.settings.api_style == "responses"
        endpoint = "responses" if is_responses else "chat/completions"

        if is_responses:
            request_body: dict[str, Any] = {
                "model": self.settings.model,
                "instructions": system,
                "input": prompt,
                "max_output_tokens": self.settings.max_output_tokens,
                "stream": True,
            }
        else:
            request_body = {
                "model": self.settings.model,
                "messages": build_chat_messages(
                    self.settings.model, system, prompt, merge_system=merge_system
                ),
                "temperature": 0,
                "max_tokens": self.settings.max_output_tokens,
                "stream": True,
            }

        max_attempts = (
            max(1 + self.settings.transport_retries, 4)
            if self.settings.transport_retries > 0
            else 1
        )
        last_error: Exception | None = None
        call_counted = False

        for attempt in range(max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("Provider streaming deadline exceeded")
            if attempt > 0 and remaining <= 5.0:
                if last_error:
                    raise last_error
                raise ProviderError("Provider streaming deadline exceeded")

            fragments: list[str] = []
            reported_tokens = 0
            truncated = False
            stream_timeout = httpx.Timeout(
                connect=30.0,
                write=30.0,
                read=min(self.settings.request_timeout, max(1.0, remaining)),
                pool=30.0,
            )
            stream_retry_code: int | None = None
            last_response: httpx.Response | None = None
            last_status_code: int | None = None

            try:
                with self.client.stream(
                    "POST",
                    endpoint,
                    timeout=stream_timeout,
                    json=request_body,
                ) as response:
                    last_response = response
                    last_status_code = response.status_code
                    response.raise_for_status()
                    if not call_counted:
                        self.usage.calls += 1
                        call_counted = True
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        payload = json.loads(response.read())
                        err_info = _extract_payload_provider_error(payload)
                        if err_info is not None:
                            err_code, _ = err_info
                            if err_code in RETRYABLE_STATUS_CODES:
                                if call_counted:
                                    self.usage.calls -= 1
                                    call_counted = False
                                stream_retry_code = err_code
                                raise ProviderError(f"Provider error {err_code or 'unknown'}")
                            else:
                                raise ProviderError(f"Provider error {err_code or 'unknown'}")

                        parts = [
                            part.get("text", "")
                            for item in payload.get("output", [])
                            for part in item.get("content", [])
                            if part.get("type") == "output_text"
                        ]
                        candidate_text = "".join(parts) or payload.get("output_text", "")
                        choices = payload.get("choices", [])
                        if not candidate_text and choices:
                            msg = choices[0].get("message", {}).get("content", "")
                            if isinstance(msg, list):
                                candidate_text = "".join(
                                    part.get("text", "") for part in msg if isinstance(part, dict)
                                )
                            elif isinstance(msg, str):
                                candidate_text = msg
                        if not candidate_text:
                            raise ModelError("Provider returned no streaming candidate")
                        fragments = [candidate_text]
                        truncated = (
                            (choices[0].get("finish_reason") == "length" if choices else False)
                            or payload.get("status") == "incomplete"
                            or payload.get("finish_reason") == "length"
                        )
                        usage = payload.get("usage") or payload.get("response", {}).get("usage") or {}
                        reported_tokens = int(
                            usage.get("total_tokens")
                            or (usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
                            or (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
                            or 0
                        )
                    else:
                        for line in response.iter_lines():
                            if time.monotonic() - started > timeout + 45.0:
                                raise ProviderError("Provider streaming deadline exceeded")
                            if not line.startswith("data:"):
                                continue
                            raw = line[5:].strip()
                            if raw == "[DONE]":
                                break
                            payload = json.loads(raw)
                            if isinstance(payload.get("error"), dict):
                                err_dict = payload["error"]
                                raw_code = err_dict.get("code")
                                err_code = None
                                if raw_code is not None:
                                    try:
                                        err_code = int(raw_code)
                                    except (ValueError, TypeError):
                                        err_code = None
                                if err_code in RETRYABLE_STATUS_CODES:
                                    if call_counted:
                                        self.usage.calls -= 1
                                        call_counted = False
                                    stream_retry_code = err_code
                                    raise ProviderError(f"Provider error {err_code or 'unknown'}")
                                else:
                                    raise ProviderError(f"Provider error {err_code or 'unknown'}")

                            usage = payload.get("usage") or payload.get("response", {}).get("usage") or {}
                            tokens = int(
                                usage.get("total_tokens")
                                or (usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
                                or (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
                                or 0
                            )
                            reported_tokens = max(reported_tokens, tokens)

                            # Standard chat completion chunk: choices[0].delta.content
                            for choice in payload.get("choices", []):
                                if choice.get("index", 0) != 0:
                                    continue
                                content = (choice.get("delta") or {}).get("content")
                                if isinstance(content, str):
                                    fragments.append(content)
                                truncated |= choice.get("finish_reason") == "length"

                            # SSE response.output_text.delta events (OpenAI Responses format)
                            event_type = payload.get("type")
                            if event_type == "response.output_text.delta":
                                delta = payload.get("delta")
                                if isinstance(delta, str):
                                    fragments.append(delta)
                                elif isinstance(delta, dict) and isinstance(delta.get("text"), str):
                                    fragments.append(delta["text"])
                            elif isinstance(payload.get("delta"), str) and "choices" not in payload:
                                fragments.append(payload["delta"])

                            if payload.get("status") == "incomplete" or payload.get("finish_reason") == "length":
                                truncated = True

                self.usage.tokens += reported_tokens
                if truncated:
                    raise ModelError("Provider returned a truncated streaming candidate")
                text = "".join(part for part in fragments if isinstance(part, str))
                if not text.strip():
                    raise ModelError("Provider returned an empty streaming candidate")
                return parse_json_object(text)

            except ProviderError as error:
                if stream_retry_code in RETRYABLE_STATUS_CODES:
                    last_error = error
                    last_status_code = stream_retry_code
                else:
                    raise
            except ModelError:
                raise
            except httpx.HTTPStatusError as error:
                status_code = error.response.status_code
                last_response = error.response
                last_status_code = status_code
                if status_code not in RETRYABLE_STATUS_CODES:
                    if not call_counted:
                        self.usage.calls += 1
                        call_counted = True
                    raise ProviderError(f"Provider HTTP {status_code}") from None
                last_error = ProviderError(f"Provider HTTP {status_code}")
            except httpx.HTTPError as error:
                self._reset_client_if_transport_error(error)
                last_response = None
                last_status_code = None
                last_error = ProviderError(f"Provider streaming failed ({type(error).__name__})")
            except (ValueError, TypeError):
                last_error = ModelError("Provider returned invalid streaming data")

            if attempt == max_attempts - 1:
                assert last_error is not None
                raise last_error

            rem = timeout - (time.monotonic() - started)
            if rem <= 5.0:
                assert last_error is not None
                raise last_error

            delay = self._retry_delay_seconds(
                attempt, last_response, status_code=last_status_code
            )
            if delay > 0:
                time.sleep(delay)

        assert last_error is not None
        raise last_error
