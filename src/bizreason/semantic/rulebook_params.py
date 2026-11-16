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

import dataclasses
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from bizreason.semantic.vocabulary import (
    DEBT_METHOD_TOKENS,
    INSTALLMENT_TOPIC_CUES,
    OVERDUE_TOPIC_CUES,
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
CONTEXT_MODES = ("cues", "full")
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
 "cancelled_statuses": ["<canonical order status of a cancelled order>", ...],
 "installment_counts_as_debt": <true | false | null>,
 "overdue_due_day_counts_as_overdue": <true | false | null>
}
Rules:
- shift_windows: the two shifts by order time, both ends inclusive, in HH:MM:SS. A shift 06:00-13:59 is ["06:00:00", "13:59:59"]. The shift word is the bare word the questions use for the shift, without the word "ca" (for example the word for morning, in the rulebook's spelling).
- tet_start: the first day of the Tet window. 23 of lunar month 12 of the previous lunar year is {"lunar_month": 12, "lunar_day": 23, "year_offset": -1}. tet_end: the last day, inclusive, in the Tet lunar year; 10 of lunar month 1 is {"lunar_month": 1, "lunar_day": 10}.
- valid_sale_statuses (orders that count as sales) and cancelled_statuses (cancelled orders) use only these English names: completed (order done), shipping (order being delivered), cancelled (order cancelled), returned (goods returned). A return is not a cancellation; valid_sale_statuses never has "cancelled".
- installment_counts_as_debt: true when the rulebook says an instalment order is a debt order (unpaid instalments are money owed); false when it says they are not debt; null when not stated.
- overdue_due_day_counts_as_overdue: an instalment due ON the asked day: true if the rulebook says it is overdue; false if only those due BEFORE the asked day are overdue; null if not stated.
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
    # Conventions with no default: True, False, or None when the rulebook does not state them. A measure that depends
    # on one (MEASURE_CONVENTIONS) gives no number while it is None.
    installment_counts_as_debt: bool | None = None
    overdue_due_day_counts_as_overdue: bool | None = None


# Measure -> the conventions its answer depends on: (field, value the measure needs or None for "stated either way",
# what the rulebook would have to define). A rulebook that does not state one makes the measure unanswerable, so
# the answer is an abstain, never a convention carried over from another rulebook. A new gate is one more row.
MEASURE_CONVENTIONS: dict[str, tuple[tuple[str, bool | None, str], ...]] = {
    "installment_outstanding": (("installment_counts_as_debt", True, "that an instalment order is a debt order"),),
    "installments_due_count": (("installment_counts_as_debt", True, "that an instalment order is a debt order"),),
    "overdue_amount": (
        ("installment_counts_as_debt", True, "that an instalment order is a debt order"),
        ("overdue_due_day_counts_as_overdue", None, "whether an instalment due on the asked day is overdue"),
    ),
    "overdue_debtor_count": (
        ("installment_counts_as_debt", True, "that an instalment order is a debt order"),
        ("overdue_due_day_counts_as_overdue", None, "whether an instalment due on the asked day is overdue"),
    ),
}
# Fields grounded in the rulebook text: the field is dropped to None unless the rulebook writes each group of cues.
_FIELD_TOPICS: dict[str, tuple[tuple[str, ...], ...]] = {
    "installment_counts_as_debt": (INSTALLMENT_TOPIC_CUES,),
    "overdue_due_day_counts_as_overdue": (INSTALLMENT_TOPIC_CUES, OVERDUE_TOPIC_CUES),
}
TRI_STATE_FIELDS = tuple(_FIELD_TOPICS)


class RulebookGap(Exception):
    """The rulebook does not define a convention that a measure needs."""


def require(measure: str, params: RulebookParams | None = None) -> RulebookParams:
    """The active parameters when every convention the measure needs is stated (and as it needs); else RulebookGap."""
    params = _active if params is None else params
    for field, needed, meaning in MEASURE_CONVENTIONS.get(measure, ()):
        value = getattr(params, field)
        if value is None:
            raise RulebookGap(f"rulebook does not define {meaning}")
        if needed is not None and value is not needed:
            raise RulebookGap(f"rulebook does not say {meaning}")
    return params


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


# The test set's rulebook text after normalize_lookup, or None when no rulebook was given (then no rule that reads it
# applies). Set with set_rulebook_text before shops are built or questions solved.
_rulebook_text: str | None = None


_rulebook_raw: str | None = None


def rulebook_raw_text() -> str | None:
    """The rulebook as written (punctuation and digits intact), for readers of stated numbers."""
    return _rulebook_raw


def set_rulebook_text(text: str | None) -> None:
    global _rulebook_text, _rulebook_raw
    _rulebook_raw = text
    from bizreason.utils import normalize_lookup
    _rulebook_text = None if text is None else f" {normalize_lookup(text)} "


def rulebook_text() -> str | None:
    return _rulebook_text


def mentions(*phrases: str) -> bool:
    """True when the active rulebook writes every phrase (accent-free, whole words). Without a rulebook text
    (unit tests, no rulebook given) the answer is True, so behaviour stays as before."""
    if _rulebook_text is None:
        return True
    from bizreason.utils import normalize_lookup
    return all(f" {normalize_lookup(p)} " in _rulebook_text for p in phrases)


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
    rulebook_text: str, model: _Model, *, timeout: float = EXTRACT_TIMEOUT, context_mode: str = "cues",
    validation_retries: int = 0, diagnostics: dict[str, Any] | None = None,
) -> tuple[RulebookParams, list[str]]:
    """Extract validated parameters; full context is an explicit control for lexical retrieval."""
    params, taken, _ok = _extract(rulebook_text, model, timeout, context_mode=context_mode,
                                validation_retries=validation_retries, diagnostics=diagnostics)
    return params, taken


def load_or_extract(
    rulebook_path: Path, model: _Model, cache_path: Path, *, timeout: float = EXTRACT_TIMEOUT,
    context_mode: str = "cues",
    validation_retries: int = 0, model_identity: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> tuple[RulebookParams, list[str]]:
    """extract_rulebook_params, cached in cache_path under the sha256 of the rulebook file.

    Match the document, transformation, profile and caller-supplied model identity.
    CLI callers without a verified identity use a fresh nonce instead of cross-run reuse.
    Invalid explicit fields and failed requests are never cached as successful defaults.
    """
    _validate_context_mode(context_mode)
    _validate_retries(validation_retries)
    rulebook_path = Path(rulebook_path)
    cache_path = Path(cache_path)
    raw = rulebook_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    provenance = _cache_provenance(context_mode, validation_retries, model_identity)
    fingerprint = hashlib.sha256(json.dumps(provenance, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if diagnostics is not None:
        diagnostics.update(cache_hit=False, fingerprint=fingerprint, model_calls=0,
                           context_mode=context_mode, document_chars=len(raw.decode("utf-8")))
    cached = _read_cache(cache_path, digest, context_mode=context_mode, fingerprint=fingerprint)
    if cached is not None:
        if diagnostics is not None:
            text = raw.decode("utf-8")
            selected = text.strip() if context_mode == "full" else relevant_rulebook_text(text)
            diagnostics.update(cache_hit=True, outcome="cache_hit", taken=cached[1], selected_chars=len(selected),
                               validation_scope="schema", http_attempts=0)
        return cached
    params, taken, ok = _extract(raw.decode("utf-8"), model, timeout, context_mode=context_mode,
                                validation_retries=validation_retries, diagnostics=diagnostics)
    if ok:
        _write_cache(cache_path, digest, params, taken, context_mode=context_mode,
                     fingerprint=fingerprint, provenance=provenance)
    return params, taken


def _validate_context_mode(context_mode: str) -> None:
    if context_mode not in CONTEXT_MODES:
        raise ValueError(f"unknown rulebook context mode: {context_mode!r}")


def _validate_retries(retries: int) -> None:
    if type(retries) is not int or retries not in (0, 1):
        raise ValueError("validation retries must be zero or one")


def _cache_provenance(context_mode: str, retries: int, model_identity: dict[str, Any] | None) -> dict[str, Any]:
    module = Path(__file__).resolve()
    sources = {"extractor": module, "transport": module.parents[1] / "llm.py",
               "normalization": module.parents[1] / "utils.py", "vocabulary": module.with_name("vocabulary.py")}
    return {"version": 2, "context_mode": context_mode, "validation_retries": retries,
            "source_sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sources.items()},
            "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
            "cue_tokens": list(_CUE_TOKENS), "statuses": list(CANONICAL_ORDER_STATUSES),
            "defaults": _to_reply(DEFAULTS), "model_identity": model_identity}


def _validation_feedback(errors: dict[str, str]) -> str:
    return ("Correct the JSON response using these schema validation errors:\n"
            + "\n".join(f"{name}: {error}" for name, error in errors.items())
            + "\nThe status fields require canonical identifiers from " + json.dumps(list(CANONICAL_ORDER_STATUSES))
            + ". Map the source wording to its meaning; do not copy source status labels into enum fields. "
              "Keep the business meaning and unspecified fields unchanged. Return only the corrected JSON object.")


def _extract(rulebook_text: str, model: _Model, timeout: float, *,
             context_mode: str = "cues", validation_retries: int = 0,
             diagnostics: dict[str, Any] | None = None) -> tuple[RulebookParams, list[str], bool]:
    _validate_context_mode(context_mode)
    _validate_retries(validation_retries)
    started = time.monotonic()
    relevant = rulebook_text.strip() if context_mode == "full" else relevant_rulebook_text(rulebook_text)
    stats = diagnostics if diagnostics is not None else {}
    stats.update(model_calls=0, selected_chars=len(relevant), document_chars=len(rulebook_text),
                 context_mode=context_mode, validation_error_fields=[], validation_scope="schema")
    attempts_before = getattr(getattr(model, "usage", None), "http_attempts", None)

    def finish(params: RulebookParams, taken: list[str], ok: bool, outcome: str):
        attempts_after = getattr(getattr(model, "usage", None), "http_attempts", None)
        stats.update(outcome=outcome, taken=taken, elapsed_seconds=round(time.monotonic() - started, 4))
        stats["http_attempts"] = (attempts_after - attempts_before
                                  if attempts_before is not None and attempts_after is not None else None)
        return params, taken, ok

    if not relevant:
        log.info("rulebook has no parameter context in %s mode; using defaults", context_mode)
        return finish(DEFAULTS, [], True, "no_context")
    prompt = "Rulebook lines:\n" + relevant
    try:
        stats["model_calls"] += 1
        reply = model.generate_json(SYSTEM_PROMPT, prompt, timeout=timeout)
    except Exception as exc:  # a failed model call must not stop a run
        log.warning("rulebook parameter call failed (%s: %s); using defaults", type(exc).__name__, exc)
        return finish(DEFAULTS, [], False, "model_failure")
    if not isinstance(reply, dict):
        log.warning("rulebook parameter reply is not a JSON object; using defaults")
        return finish(DEFAULTS, [], False, "invalid_response")
    stats["initial_reply_sha256"] = hashlib.sha256(json.dumps(reply, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    errors: dict[str, str] = {}
    params, taken = _from_reply(reply, strict=False, errors=errors)
    stats["validation_error_fields"] = list(errors)
    if errors and validation_retries:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            return finish(params, taken, False, "retry_deadline")
        repair = getattr(model, "reask_json", None)
        if not callable(repair):
            return finish(params, taken, False, "retry_unavailable")
        try:
            stats["model_calls"] += 1
            corrected = repair(SYSTEM_PROMPT, prompt, reply, _validation_feedback(errors), timeout=remaining)
        except Exception as exc:
            log.warning("rulebook parameter repair failed (%s); not caching", type(exc).__name__)
            return finish(params, taken, False, "repair_failure")
        if not isinstance(corrected, dict):
            return finish(params, taken, False, "invalid_repair")
        stats["repair_reply_sha256"] = hashlib.sha256(json.dumps(corrected, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        # Only failing fields may change; deleting a failing field cannot hide its error.
        reply = {**reply, **{name: corrected[name] for name in errors if corrected.get(name) is not None}}
        errors = {}
        params, taken = _from_reply(reply, strict=False, errors=errors)
        stats["remaining_error_fields"] = list(errors)
    params, taken = _ground(params, taken, rulebook_text)
    if errors:
        return finish(params, taken, False, "invalid_parameters")
    return finish(params, taken, True, "validated" if taken else "unstated")


def _ground(params: RulebookParams, taken: list[str], rulebook_text: str) -> tuple[RulebookParams, list[str]]:
    """Drop a tri-state convention to None unless the rulebook writes the topic (every group of cue words)."""
    padded = f" {normalize_lookup(rulebook_text)} "
    drop = {field for field, groups in _FIELD_TOPICS.items() if getattr(params, field) is not None
            and not all(any(f" {normalize_lookup(cue)} " in padded for cue in group) for group in groups)}
    if not drop:
        return params, taken
    log.warning("rulebook conventions %s are not grounded in the rulebook text; treated as not stated", sorted(drop))
    return (dataclasses.replace(params, **{field: None for field in drop}),
            [name for name in taken if name not in drop])


def _tri_state(reply: dict[str, Any], name: str) -> bool | None:
    value = reply.get(name)
    if value is not None and not isinstance(value, bool):
        raise _Invalid("must be true, false or null")
    return value


def _from_reply(reply: dict[str, Any], *, strict: bool,
                errors: dict[str, str] | None = None) -> tuple[RulebookParams, list[str]]:
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
            if errors is not None:
                errors.update({name: str(exc) for name in names})
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
    # Tri-state conventions have no default: a missing, null or unusable value is "not stated" (None), which makes the
    # measures that depend on it abstain.
    tri: dict[str, bool | None] = {}
    for name in TRI_STATE_FIELDS:
        try:
            tri[name] = _tri_state(reply, name)
        except _Invalid as exc:
            if strict:
                raise
            if errors is not None:
                errors[name] = str(exc)
            log.warning("rulebook parameter %s is not usable (%s); treated as not stated", name, exc)
            tri[name] = None
        if tri[name] is not None:
            taken.append(name)
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
        **tri,
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
        **{name: getattr(params, name) for name in TRI_STATE_FIELDS},
    }


def _read_cache(path: Path, digest: str, *, context_mode: str = "cues",
                fingerprint: str) -> tuple[RulebookParams, list[str]] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("sha256") != digest or not isinstance(data.get("params"), dict):
            return None
        if data.get("context_mode", "cues") != context_mode:
            return None
        if data.get("fingerprint") != fingerprint:
            return None
        params, _ = _from_reply(data["params"], strict=True)
        taken = data.get("taken")
        if not isinstance(taken, list) or not all(isinstance(name, str) for name in taken):
            return None
        return params, list(taken)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_cache(path: Path, digest: str, params: RulebookParams, taken: list[str], *, context_mode: str = "cues",
                 fingerprint: str, provenance: dict[str, Any]) -> None:
    payload = {"sha256": digest, "context_mode": context_mode, "params": _to_reply(params), "taken": taken,
               "fingerprint": fingerprint, "provenance": provenance}
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=path.name + ".", suffix=".tmp",
                                         dir=path.parent, delete=False) as handle:
            tmp = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not write the rulebook parameter cache %s: %s", path, exc)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


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
