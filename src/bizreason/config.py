from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from bizreason.utils import WORKSPACE


def normalize_model_id(model: str, base_url: str) -> str:
    """Normalize Trammay model aliases when connecting to trammay.xyz endpoints.

    When targeting trammay.xyz, maps shorthand identifiers (e.g. gemini3.8-flash-high,
    gemini-3.8-flash-high, gemini3.8-flash-medium, gemini3.8-flash) to canonical
    ag/gemini-3.8-flash* IDs. Leaves non-trammay (e.g. local vLLM) models untouched.
    """
    if not model:
        return model
    cleaned = model.strip()
    if not base_url or "trammay.xyz" not in base_url.lower():
        return cleaned
    if cleaned.startswith("ag/"):
        sub = cleaned[3:]
        if sub.startswith("gemini3.8-"):
            return "ag/gemini-3.8-" + sub[len("gemini3.8-"):]
        return cleaned
    if cleaned.startswith("gemini3.8-"):
        return "ag/gemini-3.8-" + cleaned[len("gemini3.8-"):]
    if cleaned.startswith("gemini-3.8-flash"):
        return f"ag/{cleaned}"
    return cleaned


@dataclass(frozen=True)
class Settings:
    base_url: str = "https://trammay.xyz/v1"
    model: str = "ag/gemini-3.8-flash-high"
    api_key: str = field(default="", repr=False)
    api_style: str = "chat"
    request_timeout: float = 150
    question_timeout: float = 150
    sql_timeout: float = 15
    max_repairs: int = 1
    max_model_calls: int = 3
    max_probe_calls: int = 2
    max_output_tokens: int = 4000
    max_result_rows: int = 10000
    context_characters: int = 65000
    gate_mode: str = "verified"
    runtime_mode: str = "dev"
    use_sql_worker_pool: bool = True
    transport_retries: int = 2
    transport_backoff: float = 1.5

    def __post_init__(self) -> None:
        normalized = normalize_model_id(self.model, self.base_url)
        if normalized != self.model:
            object.__setattr__(self, "model", normalized)

    @classmethod
    def from_env(cls, env_file: Path | None = None, **overrides: object) -> Settings:
        values = {**dotenv_values(env_file or WORKSPACE / ".env"), **os.environ}
        effective_base_url = str(overrides.get("base_url") or values.get("BIZREASON_BASE_URL") or cls.base_url)
        base_url_lower = effective_base_url.lower()

        if "openrouter.ai" in base_url_lower:
            candidate_keys = [
                "OPENROUTER_API",
                "OPENROUTER_API_KEY",
                "BIZREASON_API_KEY",
                "OPENAI_API_KEY",
                "TRAMMAY_KEY",
            ]
        elif "groq.com" in base_url_lower:
            candidate_keys = [
                "GROQ_API_KEY",
                "BIZREASON_API_KEY",
                "OPENAI_API_KEY",
                "OPENROUTER_API",
                "TRAMMAY_KEY",
            ]
        elif "generativelanguage.googleapis.com" in base_url_lower:
            candidate_keys = [
                "GEMINI_API_KEY",
                "GOOGLE_API_KEY",
                "BIZREASON_API_KEY",
                "TRAMMAY_KEY",
            ]
        elif "nvidia.com" in base_url_lower:
            candidate_keys = [
                "NVIDIA_API_KEY",
                "BIZREASON_API_KEY",
                "OPENAI_API_KEY",
                "TRAMMAY_KEY",
            ]
        elif "mistral.ai" in base_url_lower:
            candidate_keys = [
                "MISTRAL_API_KEY",
                "BIZREASON_API_KEY",
                "OPENAI_API_KEY",
                "TRAMMAY_KEY",
            ]
        elif "trammay.xyz" in base_url_lower:
            candidate_keys = [
                "TRAMMAY_KEY",
                "BIZREASON_API_KEY",
                "OPENAI_API_KEY",
                "OPENROUTER_API",
                "OPENROUTER_API_KEY",
            ]
        else:
            candidate_keys = [
                "BIZREASON_API_KEY",
                "OPENAI_API_KEY",
                "TRAMMAY_KEY",
                "OPENROUTER_API",
                "OPENROUTER_API_KEY",
            ]

        default_api_key = ""
        for k in candidate_keys:
            val = values.get(k)
            if val:
                default_api_key = str(val)
                break

        raw_model = str(values.get("BIZREASON_MODEL", cls.model))
        config: dict[str, object] = {
            "api_key": str(default_api_key or ""),
            "base_url": effective_base_url,
            "model": raw_model,
            "request_timeout": float(values.get("BIZREASON_TIMEOUT", cls.request_timeout)),
            "runtime_mode": values.get("BIZREASON_RUNTIME_MODE", cls.runtime_mode),
        }
        if "BIZREASON_USE_SQL_POOL" in values:
            val = str(values["BIZREASON_USE_SQL_POOL"]).lower()
            config["use_sql_worker_pool"] = val not in {"0", "false", "no", "off"}
        if "BIZREASON_TRANSPORT_RETRIES" in values:
            config["transport_retries"] = int(values["BIZREASON_TRANSPORT_RETRIES"])
        if "BIZREASON_TRANSPORT_BACKOFF" in values:
            config["transport_backoff"] = float(values["BIZREASON_TRANSPORT_BACKOFF"])
        if "BIZREASON_CONTEXT_CHARS" in values:
            try:
                config["context_characters"] = int(values["BIZREASON_CONTEXT_CHARS"])
            except (ValueError, TypeError):
                pass
        config.update({key: value for key, value in overrides.items() if value is not None})
        if "model" in config and "base_url" in config:
            config["model"] = normalize_model_id(str(config["model"]), str(config["base_url"]))
        return cls(**config)
