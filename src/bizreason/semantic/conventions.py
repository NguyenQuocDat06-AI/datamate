"""Shop conventions read from the README by the model, checked on the data, applied when building the layer.

A shop README states how its book is written: '1 bó = 10 cây', 'warranty tickets carry 0 money', 'returned orders
are written in the note column', 'negative quantities are returns'. A private shop will word these differently or
state new ones, so instead of fixed patterns the model turns the README into typed directives once per shop. Each
directive is then checked against the shop's own tables; one that does not hold on the data is dropped, because a
wrong rule hurts more than a missing one.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bizreason.contracts import BusinessContext

# Switched off by run_v2 --conventions off (set before shop builds fork, so workers inherit it).
ENABLED = True

KINDS = ("unit_factor", "zero_amount_meaning", "return_marker", "negative_qty_is_return", "debt_state_column")
ZERO_MEANINGS = ("warranty", "gift", "internal", "other")

SYSTEM = (
    "You read the README of one shop's spreadsheets and list the conventions it states about how the data is "
    "written. Use only these directive types and only what the README states explicitly; never guess.\n"
    '- {"type": "unit_factor", "products": [<product names as written>], "large_unit": <unit>, "factor": <number>, '
    '"base_unit": <unit>}  e.g. "X is sold by the bundle, 1 bundle = 10 pieces". An empty products list means every product.\n'
    '- {"type": "zero_amount_meaning", "meaning": "warranty"|"gift"|"internal"|"other", "note_words": [<words>]}  '
    "rows or tickets written with amount 0 mean this (e.g. warranty visits that collect no money).\n"
    '- {"type": "return_marker", "column": <column header as written>, "words": [<words>]}  returned orders are '
    "marked by these words in that column.\n"
    '- {"type": "negative_qty_is_return"}  a negative quantity records goods returned.\n'
    '- {"type": "debt_state_column", "column": <column header as written>}  that column states, in words, whether '
    "an order was sold on credit and how much is still owed at the end of the data.\n"
    "Conversions written as '1 <unit> = <number> <unit>' are already read by the system: do not list those; list "
    "unit_factor only for conversions stated in other words. Keep the reply short.\n"
    'Reply with one JSON object: {"directives": [ ... ]} (an empty list when the README states none of these).'
)


def extract(model: Any, readme: str, timeout: float = 240.0) -> tuple[list[dict[str, Any]], bool]:
    """(directives, ok) from the README; ok is False when the model call failed (then nothing is cached)."""
    if not (readme or "").strip():
        return [], True
    try:
        reply = model.generate_json(SYSTEM, readme[:8000], timeout=timeout)
    except Exception:
        return [], False
    items = reply.get("directives") if isinstance(reply, dict) else None
    return [d for d in (items if isinstance(items, list) else []) if _well_formed(d)], True


def _well_formed(item: Any) -> bool:
    if not isinstance(item, dict) or item.get("type") not in KINDS:
        return False
    kind = item["type"]
    if kind == "unit_factor":
        try:
            factor = float(item.get("factor"))
        except (TypeError, ValueError):
            return False
        return (factor > 1 and isinstance(item.get("large_unit"), str) and isinstance(item.get("base_unit"), str)
                and isinstance(item.get("products", []), list))
    if kind == "zero_amount_meaning":
        return item.get("meaning") in ZERO_MEANINGS
    if kind == "debt_state_column":
        return isinstance(item.get("column"), str) and bool(item["column"].strip())
    if kind == "return_marker":
        return isinstance(item.get("column"), str) and isinstance(item.get("words"), list) and bool(item["words"])
    return True


def load_or_extract(model: Any, context: BusinessContext, cache_path: Path) -> list[dict[str, Any]]:
    """Cached per shop revision, like the other annotation stages."""
    if not ENABLED:
        return []
    try:
        cached = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        if cached.get("revision") == context.revision and isinstance(cached.get("directives"), list):
            return cached["directives"]
    except (OSError, ValueError, AttributeError):
        pass
    directives, ok = extract(model, context.readme)
    if ok:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(json.dumps({"revision": context.revision, "directives": directives},
                                               ensure_ascii=False, indent=1), encoding="utf-8")
    return directives


DEBT_VALUES_SYSTEM = (
    "Each line is a note written on one order of a shop, numbered. The shop's README says these notes state whether "
    "the order was sold on credit and how much the customer still owes at the end of the data. For every line return "
    "whether it is a credit sale and the amount still owed in full currency units (expand the local short forms for "
    "thousand and million, e.g. '1tr645' = 1645000; 0 when fully paid; null when the note does "
    'not say). Reply with one JSON object: {"items": [{"n": <number>, "credit": true|false, "owed": <number or null>}]}.'
)
DEBT_BATCH = 25  # keeps each reply well under the annotation token limit


def read_debt_values(model: Any, values: list[str], timeout: float = 240.0) -> tuple[dict[str, list[Any]], bool]:
    """{note: [credit, owed]} for the distinct notes, read by the model in batches; ok False on a failed call."""
    out: dict[str, list[Any]] = {}
    for start in range(0, len(values), DEBT_BATCH):
        batch = values[start:start + DEBT_BATCH]
        prompt = "\n".join(f"{i + 1}. {v}" for i, v in enumerate(batch))
        try:
            reply = model.generate_json(DEBT_VALUES_SYSTEM, prompt, timeout=timeout)
        except Exception:
            return out, False
        for item in reply.get("items", []) if isinstance(reply, dict) else []:
            if not isinstance(item, dict) or not isinstance(item.get("n"), int) or not 1 <= item["n"] <= len(batch):
                continue
            owed = item.get("owed")
            if owed is not None and (isinstance(owed, bool) or not isinstance(owed, (int, float)) or owed < 0):
                continue
            out[batch[item["n"] - 1]] = [bool(item.get("credit")), owed]
    return out, True


def distinct_column_values(context: BusinessContext, header: str, limit: int = 2000) -> list[str]:
    """Distinct non-empty values of every ingested column whose original header matches (accent-free)."""
    import sqlite3
    from bizreason.utils import normalize_lookup, quote_identifier as q
    wanted = normalize_lookup(header)
    found: list[str] = []
    con = sqlite3.connect(f"file:{context.database}?mode=ro", uri=True)
    try:
        for table in context.tables:
            for column in table.columns:
                if normalize_lookup(column.original_name) == wanted:
                    try:
                        rows = con.execute(f"SELECT DISTINCT CAST({q(column.name)} AS TEXT) FROM {q(table.name)} "
                                           f"WHERE {q(column.name)} IS NOT NULL LIMIT ?", (limit,)).fetchall()
                    except sqlite3.Error:
                        continue
                    found.extend(str(r[0]).strip() for r in rows if str(r[0]).strip())
    finally:
        con.close()
    return sorted(set(found))[:limit]


def attach_value_readings(model: Any, context: BusinessContext, directives: list[dict[str, Any]],
                          cache_path: Path) -> list[dict[str, Any]]:
    """Directives that need a reading of each value (debt_state_column) get it, cached per shop revision."""
    needed = [d for d in directives if d.get("type") == "debt_state_column"]
    if not needed:
        return directives
    try:
        cached = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        if cached.get("revision") == context.revision:
            for d in needed:
                d["values"] = cached["values"].get(d["column"], {})
            return directives
    except (OSError, ValueError, AttributeError, KeyError):
        pass
    readings: dict[str, dict[str, list[Any]]] = {}
    complete = True
    for d in needed:
        values, ok = read_debt_values(model, distinct_column_values(context, d["column"]))
        d["values"] = values
        readings[d["column"]] = values
        complete = complete and ok
    if complete:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(json.dumps({"revision": context.revision, "values": readings},
                                               ensure_ascii=False), encoding="utf-8")
    return directives
