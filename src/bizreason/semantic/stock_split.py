"""A stock quantity asked as whole large units plus the loose rest ("mấy hộp chẵn, mấy viên lẻ").

The stock report keeps one quantity in the product's base unit. A question may name two units of the product, the
large one that is counted whole and the smaller one for the loose rest, and the smaller one need not be the base unit
(whole boxes and loose blisters when the base unit is the tablet). The factors come from the shop's own conversion
table, found by the roles the annotation gave its columns (product, large unit, factor, optionally base unit), never
from unit names written in this module. The units the question uses are the product's own units: the base unit of the
stock report and the large units that table lists for it. Anything not certain raises SplitUnsupported, which the
caller turns into an abstain; a question that names fewer than two of the product's units is left to the plain path.
"""
from __future__ import annotations

import sqlite3

from bizreason.semantic.vocabulary import LARGE_UNIT_LOOSE_CUE, LARGE_UNIT_WHOLE_CUES
from bizreason.utils import normalize_lookup

CONVERSION_ROLES = ("large_unit", "conversion_factor")
PRODUCT_ROLES = ("product_name", "product_id")
BASE_UNIT_ROLE = "base_unit"


class SplitUnsupported(Exception):
    """The question asks a unit split the layer cannot answer with certainty."""


def split_quantity(qty: int, large_factor: int, small_factor: int) -> tuple[int, int]:
    """(whole large units, loose small units) of a base quantity. A rest below one small unit is a third quantity the
    question did not ask for, so the split is not answered rather than dropping it."""
    if qty < 0 or large_factor < 1 or small_factor < 1 or large_factor % small_factor:
        raise SplitUnsupported("units do not divide")
    whole, rest = divmod(qty, large_factor)
    if rest % small_factor:
        raise SplitUnsupported("a rest below one small unit")
    return whole, rest // small_factor


def _whole_number(value: object) -> int | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return int(number) if number == int(number) and number >= 1 else None


def _conversion_sources(db: sqlite3.Connection) -> list[dict[str, str | None]]:
    """Tables whose columns carry the roles of a unit conversion list: where each role's column is."""
    try:
        rows = db.execute(
            "SELECT table_name, column_name, role FROM column_catalog "
            "WHERE role IN ('product_name', 'product_id', 'large_unit', 'conversion_factor', 'base_unit')").fetchall()
    except sqlite3.Error:
        return []
    tables: dict[str, dict[str, list[str]]] = {}
    for table, column, role in rows:
        tables.setdefault(table, {}).setdefault(role, []).append(column)
    sources = []
    for table, roles in tables.items():
        if not all(role in roles for role in CONVERSION_ROLES) or not any(role in roles for role in PRODUCT_ROLES):
            continue
        factors = roles["conversion_factor"]
        # The numeric companion of a text column is written first by the layer under an "xn_" prefix.
        factor = next((c for c in factors if c.startswith("xn_")), factors[0])
        product_role = "product_name" if "product_name" in roles else "product_id"
        sources.append({
            "table": table, "product_role": product_role, "product": roles[product_role][0],
            "unit": roles["large_unit"][0], "factor": factor,
            "base": roles[BASE_UNIT_ROLE][0] if BASE_UNIT_ROLE in roles else None,
        })
    return sources


def _keys_of_id(db: sqlite3.Connection, product_id: object) -> set[str]:
    try:
        rows = db.execute("SELECT DISTINCT product_key FROM lines WHERE product_id = ?", (product_id,)).fetchall()
    except sqlite3.Error:
        return set()
    return {row[0] for row in rows if row[0]}


def unit_ladder(db: sqlite3.Connection, name_key: str, base_unit: str) -> dict[str, int]:
    """Unit (as written in the data) -> base units in it, for one stock product. The base unit is 1. A unit the
    conversion lists with two different factors, or whose own base unit differs from the report's, is left out."""
    base_norm = normalize_lookup(base_unit)
    found: dict[str, set[int]] = {}
    written: dict[str, str] = {}
    for src in _conversion_sources(db):
        try:
            rows = db.execute(
                f'SELECT "{src["product"]}", "{src["unit"]}", "{src["factor"]}"'
                + (f', "{src["base"]}"' if src["base"] else ", NULL")
                + f' FROM "{src["table"]}"').fetchall()
        except sqlite3.Error:
            continue
        for product, unit, factor, base in rows:
            if product is None or unit is None:
                continue
            if src["product_role"] == "product_name":
                if normalize_lookup(product) != name_key:
                    continue
            elif name_key not in _keys_of_id(db, product):
                continue
            if base is not None and normalize_lookup(base) != base_norm:
                continue
            number = _whole_number(factor)
            unit_norm = normalize_lookup(unit)
            if number is None or not unit_norm:
                continue
            found.setdefault(unit_norm, set()).add(number)
            written.setdefault(unit_norm, str(unit))
    ladder = {base_unit: 1}
    for unit_norm, factors in found.items():
        if unit_norm != base_norm and len(factors) == 1:
            ladder[written[unit_norm]] = next(iter(factors))
    return ladder


def _find_span(tokens: list[str], phrase: list[str]) -> int | None:
    if not phrase:
        return None
    for start in range(len(tokens) - len(phrase) + 1):
        if tokens[start:start + len(phrase)] == phrase:
            return start
    return None


def _stock_rows(db: sqlite3.Connection) -> list[tuple[str, float, str]]:
    return [(row[0], row[1], row[2]) for row in db.execute(
        "SELECT name_key, qty_base, unit FROM stock WHERE name_key IS NOT NULL")]


def resolve_product(question_tokens: list[str], rows: list[tuple[str, float, str]],
                    filters: list[dict]) -> tuple[tuple[str, float, str], list[str]] | None:
    """The one stock product the question writes, and its words in the question. The whole stock name is looked
    for first (the longest when one name contains another); a product filter that is a run of words of exactly one
    stock name is the fallback. Two different products named -> SplitUnsupported; none -> None."""
    hits = []
    for row in rows:
        phrase = row[0].split()
        if _find_span(question_tokens, phrase) is not None:
            hits.append((row, phrase))
    hits = [h for h in hits if not any(h[0][0] != o[0][0] and f" {h[0][0]} " in f" {o[0][0]} " for o, _ in hits)]
    if len(hits) > 1:
        raise SplitUnsupported("several products named")
    if hits:
        return hits[0]
    candidates = []
    for item in filters:
        if item.get("dim") != "product" or item.get("exclude") is True:
            continue
        phrase = normalize_lookup(item.get("value") or "").split()
        if not phrase or _find_span(question_tokens, phrase) is None:
            continue
        owners = [row for row in rows if _find_span(row[0].split(), phrase) is not None]
        if len(owners) == 1:
            candidates.append((owners[0], phrase))
    if len({c[0][0] for c in candidates}) > 1:
        raise SplitUnsupported("several products named")
    return candidates[0] if candidates else None


def _unit_mentions(tokens: list[str], ladder: dict[str, int]) -> list[tuple[int, str]]:
    """(position, unit) of each ladder unit written in the tokens, longest unit names first so that a unit made of
    several words is not also counted as a shorter unit inside it."""
    taken: set[int] = set()
    mentions = []
    for unit in sorted(ladder, key=lambda u: -len(normalize_lookup(u).split())):
        phrase = normalize_lookup(unit).split()
        for start in range(len(tokens) - len(phrase) + 1):
            span = range(start, start + len(phrase))
            if tokens[start:start + len(phrase)] == phrase and not taken.intersection(span):
                taken.update(span)
                mentions.append((start, unit))
    return sorted(mentions)


def split_stock(db: sqlite3.Connection, question_text: str, filters: list[dict]) -> tuple[int, int] | None:
    """(whole large units, loose small units) for a question that asks a stock quantity in two units of one product,
    or None when the question is not of that shape (fewer than two of the product's units, or no product found).
    Raises SplitUnsupported when it is of that shape but the factors or the order of the units are not certain."""
    tokens = normalize_lookup(question_text).split()
    if LARGE_UNIT_LOOSE_CUE not in tokens or not any(cue in tokens for cue in LARGE_UNIT_WHOLE_CUES):
        return None
    try:
        rows = _stock_rows(db)
    except sqlite3.Error:
        return None
    if len({r[0] for r in rows}) != len(rows):
        raise SplitUnsupported("product listed twice in the stock report")
    resolved = resolve_product(tokens, rows, filters)
    if resolved is None:
        return None
    (name_key, qty, base_unit), phrase = resolved
    start = _find_span(tokens, phrase)
    rest = tokens[:start] + tokens[start + len(phrase):]
    ladder = unit_ladder(db, name_key, base_unit)
    mentions = _unit_mentions(rest, ladder)
    if len(mentions) < 2:
        return None
    if len(mentions) > 2 or mentions[0][1] == mentions[1][1]:
        raise SplitUnsupported("more than two units named")
    large, small = mentions[0][1], mentions[1][1]
    if ladder[large] <= ladder[small]:
        raise SplitUnsupported("units not in large-to-small order")
    if qty is None or float(qty) != int(qty) or qty < 0:
        raise SplitUnsupported("stock quantity is not a whole count")
    return split_quantity(int(qty), ladder[large], ladder[small])
