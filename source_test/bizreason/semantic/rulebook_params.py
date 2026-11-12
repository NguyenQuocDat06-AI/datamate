"""Business parameters that a shop's rulebook states, read at run time instead of being hard-coded.

The parameters: the shift windows with the baseline and afternoon shifts; the walk-in customer names; the service-fee
line names; the payment-method wording that makes an order a debt or not; the Tet window; and the two order-status
sets, the statuses whose orders are sales and the status of a cancelled order. DEFAULTS are today's constants
(imported from vocabulary), so a rulebook that states the same values changes nothing.

Two kinds of field. Word lists (walk-in names, service-fee names, payment wording) are unions with the defaults: a
rulebook names examples, so it adds words and never removes one. Status sets are definitions: a rulebook that
states "orders not cancelled and not returned count" states the whole set, so a valid reply replaces the default
rather than adding to it. A union would be wrong there, because a default that a rulebook drops (say 'shipping') would
silently stay in every revenue answer.

Wiring for a run, which run_v2 should do before it builds any shop (layer.build reads the walk-in and service-fee
names at build time; the compiler and temporal read the rest at query time):

    params, taken = load_or_extract(rulebook, annotation_client, out / "rulebook_params.json")
    set_active(params)
    log.info("rulebook parameters taken from the rulebook: %s", taken)

Failure policy: nothing here raises on a model failure. A failed call, a reply that is not a JSON object, or an
invalid field falls back to the default for that field (a shift group falls back as one). A failed call returns
DEFAULTS with taken == [], logs a warning and is never cached.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from bizreason.semantic.vocabulary import (
    DEBT_METHOD_TOKENS,
    NON_DEBT_METHOD_TOKENS,
    CANCELLED_STATUSES,
    RULEBOOK_CUES,
    SERVICE_FEE_NAMES,
    SHIFT_AFTERNOON,
    SHIFT_BASELINE,
    SHIFT_CANONICAL_NAMES,
    SHIFT_WINDOWS,
    TET_END,
    TET_START,
    VALID_SALE_STATUSES,
    WALK_IN_NAMES,
)
from bizreason.utils import normalize_lookup

log = logging.getLogger(__name__)

EXTRACT_TIMEOUT = 120.0
MAX_LIST_ITEMS = 20
MAX_SHIFTS = 8
# The canonical order statuses the layer produces (layer.CANONICAL_STATUS). Copied rather than imported because layer
# imports this module; a test pins the two tuples equal.
CANONICAL_ORDER_STATUSES = ("completed", "shipping", "cancelled", "returned")
_CLOCK = re.compile(r"([01]\d|2[0-3]):[0-5]\d:[0-5]\d")
_CUE_TOKENS = tuple(sorted({normalize_lookup(cue) for cues in RULEBOOK_CUES.values() for cue in cues} - {""}))
_TABLE_LINE = re.compile(r"^\s*\|")

SYSTEM_PROMPT = """You read one section of a shop's business rulebook, written in Vietnamese (diacritics may be missing), and report the parameters it states. Reply with one JSON object and nothing else, with exactly these keys:
{
 "shift_windows": {"<shift word>": ["HH:MM:SS", "HH:MM:SS"], ...},
 "shift_baseline": "<shift word of the baseline shift>",
 "shift_afternoon": "<shift word of the other shift>",
 "walk_in_names": ["<name the rulebook gives an order with no customer>", ...],
 "service_fee_names": ["<line name the rulebook gives a service charge, which is not a product>", ...],
 "debt_method_tokens": ["<payment wording the rulebook gives an order on credit>", ...],
 "non_debt_method_tokens": ["<payment wording the rulebook gives an order that is not customer debt>", ...],
 "tet_start": {"lunar_month": <int>, "lunar_day": <int>, "year_offset": <int>},
 "tet_end": {"lunar_month": <int>, "lunar_day": <int>},
 "valid_sale_statuses": ["<canonical order status whose orders count as sales>", ...],
 "cancelled_statuses": ["<canonical order status of a cancelled order>", ...]
}
Rules:
- shift_windows: the two shifts by order time, both ends inclusive, in HH:MM:SS. A shift 06:00-13:59 is ["06:00:00", "13:59:59"]. The shift word is the bare word the questions use for the shift, without the word "ca" (for example the word for morning, in the rulebook's spelling).
- tet_start: the first day of the Tet window. 23 of lunar month 12 of the previous lunar year is {"lunar_month": 12, "lunar_day": 23, "year_offset": -1}. tet_end: the last day, inclusive, in the Tet lunar year; 10 of lunar month 1 is {"lunar_month": 1, "lunar_day": 10}.
- valid_sale_statuses (orders that count as sales) and cancelled_statuses (cancelled orders) use only these English names: completed (order done), shipping (order being delivered), cancelled (order cancelled), returned (goods returned). A return is not a cancellation; valid_sale_statuses never has "cancelled".
- Copy other names and wording from the rulebook. Do not translate, shorten or add words.
- A field the section does not state is null. Never fill a field from general knowledge.
"""


class _Model(Protocol):
    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]: ...


@dataclass(frozen=True)
class RulebookParams:
    """Shift windows are name -> (start, end) as "HH:MM:SS", both inclusive. Names are normalized lookup strings.

    tet_start is (lunar month, lunar day, year offset from the Tet lunar year); tet_end is (lunar month, lunar day).
    """

    shift_windows: dict[str, tuple[str, str]]
    shift_baseline: str
    shift_afternoon: str
    walk_in_names: tuple[str, ...]
    service_fee_names: tuple[str, ...]
    debt_method_tokens: tuple[str, ...]
    non_debt_method_tokens: tuple[str, ...]
    tet_start: tuple[int, int, int]
    tet_end: tuple[int, int]
    # Definition sets of canonical order statuses (CANONICAL_ORDER_STATUSES), in that canonical order.
    valid_sale_statuses: tuple[str, ...]
    cancelled_statuses: tuple[str, ...]


DEFAULTS = RulebookParams(
    shift_windows=dict(SHIFT_WINDOWS),
    shift_baseline=SHIFT_BASELINE,
    shift_afternoon=SHIFT_AFTERNOON,
    walk_in_names=tuple(WALK_IN_NAMES),
    service_fee_names=tuple(SERVICE_FEE_NAMES),
    debt_method_tokens=tuple(DEBT_METHOD_TOKENS),
    non_debt_method_tokens=tuple(NON_DEBT_METHOD_TOKENS),
    tet_start=tuple(TET_START),
    tet_end=tuple(TET_END),
    valid_sale_statuses=tuple(VALID_SALE_STATUSES),
    cancelled_statuses=tuple(CANCELLED_STATUSES),
)

_active: RulebookParams = DEFAULTS


def set_active(params: RulebookParams) -> None:
    """Make these parameters the ones every consumer reads. Set them before building shops."""
    global _active
    if not isinstance(params, RulebookParams):
        raise TypeError("set_active needs a RulebookParams")
    _active = params


def active() -> RulebookParams:
    return _active


def relevant_rulebook_text(rulebook_text: str) -> str:
    """The rulebook lines that carry a cue word, each under its heading.

    A table keeps its column header and separator when any of its rows is kept. Only cue lines are kept, so the
    prompt stays short however long the rulebook is.
    """
    lines = rulebook_text.splitlines()
    out: list[str] = []
    heading: str | None = None
    shown_heading: str | None = None

    def add(line: str) -> None:
        nonlocal shown_heading
        if heading is not None and shown_heading != heading:
            out.append(heading)
            shown_heading = heading
        out.append(line)

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped
            index += 1
        elif _TABLE_LINE.match(line):
            end = index
            while end < len(lines) and _TABLE_LINE.match(lines[end]):
                end += 1
            block = lines[index:end]
            rows = [row for row in block[2:] if _has_cue(row)]
            if rows:
                for row in [*block[:2], *rows]:
                    add(row)
            index = end
        else:
            if stripped and _has_cue(line):
                add(line)
            index += 1
    return "\n".join(out)


def _has_cue(text: str) -> bool:
    padded = f" {normalize_lookup(text)} "
    return any(f" {cue} " in padded for cue in _CUE_TOKENS)


class _Invalid(ValueError):
    """One parameter field, or one group of fields, is not usable."""


def extract_rulebook_params(
    rulebook_text: str, model: _Model, *, timeout: float = EXTRACT_TIMEOUT,
) -> tuple[RulebookParams, list[str]]:
    """One model call on the relevant rulebook lines. Returns the parameters and the field names the rulebook stated."""
    params, taken, _ok = _extract(rulebook_text, model, timeout)
    return params, taken


def load_or_extract(
    rulebook_path: Path, model: _Model, cache_path: Path, *, timeout: float = EXTRACT_TIMEOUT,
) -> tuple[RulebookParams, list[str]]:
    """extract_rulebook_params, cached in cache_path under the sha256 of the rulebook file.

    A cache whose digest differs from the rulebook, or that does not parse strictly, is ignored. A failed extraction
    is not written to the cache, so the next run asks the model again.
    """
    rulebook_path = Path(rulebook_path)
    cache_path = Path(cache_path)
    raw = rulebook_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    cached = _read_cache(cache_path, digest)
    if cached is not None:
        return cached
    params, taken, ok = _extract(raw.decode("utf-8"), model, timeout)
    if ok:
        _write_cache(cache_path, digest, params, taken)
    return params, taken


def _extract(rulebook_text: str, model: _Model, timeout: float) -> tuple[RulebookParams, list[str], bool]:
    relevant = relevant_rulebook_text(rulebook_text)
    if not relevant:
        log.info("rulebook has no parameter cue lines; using defaults")
        return DEFAULTS, [], True
    try:
        reply = model.generate_json(SYSTEM_PROMPT, "Rulebook lines:\n" + relevant, timeout=timeout)
    except Exception as exc:  # a failed model call must not stop a run
        log.warning("rulebook parameter call failed (%s: %s); using defaults", type(exc).__name__, exc)
        return DEFAULTS, [], False
    if not isinstance(reply, dict):
        log.warning("rulebook parameter reply is not a JSON object; using defaults")
        return DEFAULTS, [], False
    params, taken = _from_reply(reply, strict=False)
    return params, taken, True


def _from_reply(reply: dict[str, Any], *, strict: bool) -> tuple[RulebookParams, list[str]]:
    """Validate every field. Lenient mode falls back per field and logs; strict mode raises _Invalid instead."""
    taken: list[str] = []

    def take(names: tuple[str, ...], parse: Callable[[], tuple], defaults: tuple) -> tuple:
        if all(reply.get(name) is None for name in names):
            if strict:
                raise _Invalid(f"{'/'.join(names)} missing")
            return defaults
        try:
            value = parse()
        except _Invalid as exc:
            if strict:
                raise
            log.warning("rulebook parameter %s is not usable (%s); using the default", "/".join(names), exc)
            return defaults
        taken.extend(names)
        return value

    shift_names = ("shift_windows", "shift_baseline", "shift_afternoon")
    shift = take(
        shift_names,
        lambda: _shift_group(reply),
        (dict(DEFAULTS.shift_windows), DEFAULTS.shift_baseline, DEFAULTS.shift_afternoon),
    )
    walk_in = take(("walk_in_names",), lambda: (_names(reply["walk_in_names"]),), (DEFAULTS.walk_in_names,))
    service = take(
        ("service_fee_names",), lambda: (_names(reply["service_fee_names"]),), (DEFAULTS.service_fee_names,),
    )
    debt = take(
        ("debt_method_tokens",), lambda: (_names(reply["debt_method_tokens"]),), (DEFAULTS.debt_method_tokens,),
    )
    non_debt = take(
        ("non_debt_method_tokens",), lambda: (_names(reply["non_debt_method_tokens"]),),
        (DEFAULTS.non_debt_method_tokens,),
    )
    tet = take(("tet_start", "tet_end"), lambda: _tet_group(reply), (DEFAULTS.tet_start, DEFAULTS.tet_end))
    # Status sets are definitions, so a valid reply replaces the default. The valid set may not hold 'cancelled', and
    # the cancelled set may not share a status with the valid set (the cancelled set is parsed second, against it).
    valid = take(
        ("valid_sale_statuses",), lambda: (_statuses(reply["valid_sale_statuses"], forbidden=("cancelled",)),),
        (DEFAULTS.valid_sale_statuses,),
    )
    cancelled = take(
        ("cancelled_statuses",), lambda: (_statuses(reply["cancelled_statuses"], forbidden=valid[0]),),
        (DEFAULTS.cancelled_statuses,),
    )
    # Word lists are unions with the defaults: a rulebook adds the words it names but never removes a default
    # word, because rulebooks name examples ("COD", "sàn TMĐT") rather than exhaustive lists.
    params = RulebookParams(
        shift_windows=shift[0],
        shift_baseline=shift[1],
        shift_afternoon=shift[2],
        walk_in_names=_union(DEFAULTS.walk_in_names, walk_in[0]),
        service_fee_names=_union(DEFAULTS.service_fee_names, service[0]),
        debt_method_tokens=_union(DEFAULTS.debt_method_tokens, debt[0]),
        non_debt_method_tokens=_union(DEFAULTS.non_debt_method_tokens, non_debt[0]),
        tet_start=tet[0],
        tet_end=tet[1],
        valid_sale_statuses=valid[0],
        cancelled_statuses=cancelled[0],
    )
    return params, taken


def _statuses(raw: Any, *, forbidden: tuple[str, ...]) -> tuple[str, ...]:
    """A set of canonical order statuses, in canonical order. Only canonical names, at least one, none forbidden."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= len(CANONICAL_ORDER_STATUSES):
        raise _Invalid("needs a list of canonical order statuses")
    named: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise _Invalid("status names must be text")
        name = item.strip().lower()
        if name not in CANONICAL_ORDER_STATUSES:
            raise _Invalid(f"status {item!r} is not a canonical order status")
        if name in forbidden:
            raise _Invalid(f"status {name} may not be in this set")
        named.add(name)
    return tuple(name for name in CANONICAL_ORDER_STATUSES if name in named)


def _shift_group(reply: dict[str, Any]) -> tuple[dict[str, tuple[str, str]], str, str]:
    windows = _windows(reply.get("shift_windows"))
    baseline = _shift_name(reply.get("shift_baseline"))
    afternoon = _shift_name(reply.get("shift_afternoon"))
    if baseline == afternoon or baseline not in windows or afternoon not in windows:
        raise _Invalid("baseline and afternoon must be two different shifts of shift_windows")
    roles = {"baseline": baseline, "afternoon": afternoon}
    for alias, role in SHIFT_CANONICAL_NAMES.items():
        windows.setdefault(alias, windows[roles[role]])
    return windows, baseline, afternoon


def _windows(raw: Any) -> dict[str, tuple[str, str]]:
    if not isinstance(raw, dict) or not 2 <= len(raw) <= MAX_SHIFTS:
        raise _Invalid("shift_windows needs two to eight shifts")
    out: dict[str, tuple[str, str]] = {}
    for name, span in raw.items():
        key = _shift_name(name)
        if key in out:
            raise _Invalid(f"shift {key} is given twice")
        if not isinstance(span, list) or len(span) != 2:
            raise _Invalid(f"shift {key} needs [start, end]")
        start, end = _clock(span[0]), _clock(span[1])
        if _seconds(start) >= _seconds(end):
            raise _Invalid(f"shift {key} must start before it ends")
        out[key] = (start, end)
    return out


def _clock(value: Any) -> str:
    if not isinstance(value, str) or not _CLOCK.fullmatch(value):
        raise _Invalid(f"time {value!r} is not HH:MM:SS")
    return value


def _seconds(clock: str) -> int:
    hours, minutes, seconds = (int(part) for part in clock.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _shift_name(raw: Any) -> str:
    if not isinstance(raw, str):
        raise _Invalid("a shift name must be text")
    key = normalize_lookup(raw)
    if not key:
        raise _Invalid("a shift name is empty")
    return key


def _names(raw: Any) -> tuple[str, ...]:
    """A list of names or wording, as normalized lookup strings, without duplicates."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_LIST_ITEMS:
        raise _Invalid(f"needs a list of one to {MAX_LIST_ITEMS} strings")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise _Invalid("list items must be text")
        key = normalize_lookup(item)
        if not key:
            raise _Invalid("list items must not be empty")
        if key not in out:
            out.append(key)
    return tuple(out)


def _tet_group(reply: dict[str, Any]) -> tuple[tuple[int, int, int], tuple[int, int]]:
    start, end = reply.get("tet_start"), reply.get("tet_end")
    start_month = _int(start, "lunar_month", 1, 12, "tet_start")
    start_day = _int(start, "lunar_day", 1, 30, "tet_start")
    offset = _int(start, "year_offset", -2, 0, "tet_start")
    end_month = _int(end, "lunar_month", 1, 12, "tet_end")
    end_day = _int(end, "lunar_day", 1, 30, "tet_end")
    if offset == 0 and (start_month, start_day) >= (end_month, end_day):
        raise _Invalid("the Tet window must start before it ends")
    return (start_month, start_day, offset), (end_month, end_day)


def _int(obj: Any, key: str, low: int, high: int, field: str) -> int:
    if not isinstance(obj, dict):
        raise _Invalid(f"{field} must be an object")
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _Invalid(f"{field}.{key} must be an integer from {low} to {high}")
    return value


def _to_reply(params: RulebookParams) -> dict[str, Any]:
    """The reply shape of a parameter set, so the cache is read back by the same validator."""
    return {
        "shift_windows": {name: list(span) for name, span in params.shift_windows.items()},
        "shift_baseline": params.shift_baseline,
        "shift_afternoon": params.shift_afternoon,
        "walk_in_names": list(params.walk_in_names),
        "service_fee_names": list(params.service_fee_names),
        "debt_method_tokens": list(params.debt_method_tokens),
        "non_debt_method_tokens": list(params.non_debt_method_tokens),
        "tet_start": {
            "lunar_month": params.tet_start[0], "lunar_day": params.tet_start[1], "year_offset": params.tet_start[2],
        },
        "tet_end": {"lunar_month": params.tet_end[0], "lunar_day": params.tet_end[1]},
        "valid_sale_statuses": list(params.valid_sale_statuses),
        "cancelled_statuses": list(params.cancelled_statuses),
    }


def _read_cache(path: Path, digest: str) -> tuple[RulebookParams, list[str]] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("sha256") != digest or not isinstance(data.get("params"), dict):
            return None
        params, _ = _from_reply(data["params"], strict=True)
        taken = data.get("taken")
        if not isinstance(taken, list) or not all(isinstance(name, str) for name in taken):
            return None
        return params, list(taken)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_cache(path: Path, digest: str, params: RulebookParams, taken: list[str]) -> None:
    payload = {"sha256": digest, "params": _to_reply(params), "taken": taken}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not write the rulebook parameter cache %s: %s", path, exc)


def _union(defaults: tuple[str, ...], stated: tuple[str, ...]) -> tuple[str, ...]:
    """Defaults first, then the rulebook's extra words, each once (compared after lookup normalization)."""
    from bizreason.utils import normalize_lookup

    seen: set[str] = set()
    out: list[str] = []
    for word in tuple(defaults) + tuple(stated):
        key = normalize_lookup(word)
        if key and key not in seen:
            seen.add(key)
            out.append(word)
    return tuple(out)
