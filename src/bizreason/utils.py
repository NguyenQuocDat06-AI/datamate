from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[2]  # repository root (data/, artifacts/); /code in the image

_ACTIVE_POLICY: contextvars.ContextVar[PathPolicy | None] = contextvars.ContextVar(
    "_ACTIVE_POLICY", default=None
)
_GLOBAL_MODE: str = "dev"
_GLOBAL_ALLOWED_ROOTS: list[Path] = []
_GLOBAL_FORBIDDEN_ROOTS: list[Path] = []


class PathPolicy:
    def __init__(
        self,
        mode: str = "dev",
        allowed_roots: Iterable[str | Path] = (),
        forbidden_roots: Iterable[str | Path] = (),
    ) -> None:
        self.mode = mode
        self.allowed_roots = tuple(Path(p).resolve() for p in allowed_roots)
        self.forbidden_roots = tuple(Path(p).resolve() for p in forbidden_roots)
        self._token: contextvars.Token[PathPolicy | None] | None = None

    def __enter__(self) -> PathPolicy:
        self._token = _ACTIVE_POLICY.set(self)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._token is not None:
            _ACTIVE_POLICY.reset(self._token)
            self._token = None


def configure_path_policy(
    mode: str | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
    forbidden_roots: Iterable[str | Path] | None = None,
) -> None:
    global _GLOBAL_MODE
    if mode is not None:
        _GLOBAL_MODE = mode
    if allowed_roots is not None:
        _GLOBAL_ALLOWED_ROOTS.clear()
        _GLOBAL_ALLOWED_ROOTS.extend(Path(p).resolve() for p in allowed_roots)
    if forbidden_roots is not None:
        _GLOBAL_FORBIDDEN_ROOTS.clear()
        _GLOBAL_FORBIDDEN_ROOTS.extend(Path(p).resolve() for p in forbidden_roots)


def register_forbidden_root(path: str | Path) -> None:
    resolved = Path(path).resolve()
    if resolved not in _GLOBAL_FORBIDDEN_ROOTS:
        _GLOBAL_FORBIDDEN_ROOTS.append(resolved)


def register_input_data_dir(path: str | Path) -> None:
    register_forbidden_root(path)


def get_active_path_policy() -> PathPolicy:
    policy = _ACTIVE_POLICY.get()
    if policy is not None:
        return policy
    mode = os.environ.get("BIZREASON_RUNTIME_MODE", _GLOBAL_MODE)
    allowed = list(_GLOBAL_ALLOWED_ROOTS)
    env_roots = os.environ.get("BIZREASON_ALLOWED_WRITE_ROOTS", "").strip()
    if env_roots:
        for item in re.split(r"[,;]", env_roots):
            item = item.strip()
            if item:
                allowed.append(Path(item).resolve())
    forbidden = list(_GLOBAL_FORBIDDEN_ROOTS)
    return PathPolicy(mode=mode, allowed_roots=allowed, forbidden_roots=forbidden)


def writable_path(path: str | Path) -> Path:
    """Enforce the filesystem boundary according to the active path policy."""
    resolved = Path(path).resolve()
    policy = get_active_path_policy()

    forbidden_list = list(policy.forbidden_roots)
    for g_f in _GLOBAL_FORBIDDEN_ROOTS:
        if g_f not in forbidden_list:
            forbidden_list.append(g_f)
    if policy.mode == "competition":
        data_root = Path("/data").resolve()
        if data_root not in forbidden_list:
            forbidden_list.append(data_root)

    for forbidden in forbidden_list:
        if resolved == forbidden or resolved.is_relative_to(forbidden):
            raise ValueError("Refusing to write inside read-only data directory")

    if policy.mode == "competition":
        allowed = (WORKSPACE, *policy.allowed_roots)
        if any(resolved == root or resolved.is_relative_to(root) for root in allowed):
            return resolved
        raise ValueError(
            f"Outputs must stay within {WORKSPACE.name}/ or allowed roots; got {resolved}"
        )

    if not resolved.is_relative_to(WORKSPACE):
        raise ValueError(f"Outputs must stay within {WORKSPACE.name}/")
    return resolved


def normalize_lookup(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value).lower().replace("đ", "d"))
    return " ".join(
        re.sub(
            r"[^a-z0-9]+", " ", "".join(char for char in text if not unicodedata.combining(char))
        ).split()
    )


def identifier(value: str) -> str:
    normalized = normalize_lookup(value).replace(" ", "_") or "column"
    return "c_" + normalized if normalized[0].isdigit() else normalized


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(type(value).__name__)


def dumps(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        default=json_default,
        indent=indent,
    )


def prompt_dumps(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=False,
        default=json_default,
        indent=indent,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path = writable_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            item = json.loads(line, parse_float=Decimal)
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ValueError(f"Invalid JSONL record at line {number}")
            if item["id"] in seen:
                raise ValueError(f"Duplicate ID at line {number}")
            seen.add(item["id"])
            records.append(item)
    return records
