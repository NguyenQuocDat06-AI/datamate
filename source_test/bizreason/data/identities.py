"""Canonical names from released product codes/catalogs, with raw values retained."""

from __future__ import annotations

import sqlite3
import re
import unicodedata
from collections import defaultdict

from bizreason.contracts import TableMeta
from bizreason.utils import normalize_lookup, quote_identifier

RETAIL_ALIASES = {"kl", "le", "khach le", "khachle", "khach vang lai", "vang lai"}
PHONE_PATTERN = re.compile(
    r"""(?:
        \s* \(\s* (?:(?:sdt|sđt|tel|phone)\s*[:.]?\s*)? (?P<p1> (?:\+?84\s*|0) [235789] [\d\.\-\s]* \d ) \s*\)
      | \s* [-–—:] \s* (?:(?:sdt|sđt|tel|phone)\s*[:.]?\s*)? (?P<p2> (?:\+?84\s*|0) [235789] [\d\.\-\s]* \d )
      | \s+ (?:(?:sdt|sđt|tel|phone)\s*[:.]?\s*)? (?P<p3> (?:\+?84\s*|0) [235789] [\d\.\-\s]* \d )
    )\s*$""",
    re.VERBOSE | re.IGNORECASE,
)


def clean_text(value: object) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value)).split())


def strip_embedded_phone(name: str) -> tuple[str, str | None]:
    if not name or not isinstance(name, str):
        return ("", None)
    cleaned = clean_text(name)
    m = PHONE_PATTERN.search(cleaned)
    if not m:
        return (cleaned, None)
    phone_raw = m.group("p1") or m.group("p2") or m.group("p3")
    digits = re.sub(r"\D", "", phone_raw)
    is_phone = False
    if phone_raw.strip().startswith("+84") or digits.startswith("84"):
        is_phone = bool(re.fullmatch(r"84[235789]\d{8,9}", digits))
    elif digits.startswith("0"):
        is_phone = bool(re.fullmatch(r"0[235789]\d{8,9}", digits))
    if not is_phone:
        return (cleaned, None)
    prefix = re.sub(r"[\s\-–—:]+$", "", cleaned[: m.start()]).strip()
    words = prefix.split()
    alpha_words = [w for w in words if any(c.isalpha() for c in w) and not any(c.isdigit() for c in w)]
    if len(alpha_words) < 2:
        return (cleaned, None)
    return (clean_text(prefix), phone_raw.strip())


def is_retail_customer(value: object) -> bool:
    text = " ".join(re.sub(r"[^\w\s]", " ", clean_text(value).casefold()).split())
    return text in RETAIL_ALIASES | {"lẻ", "khách lẻ", "khách vãng lai", "vãng lai"}


def canonicalize_products(connection: sqlite3.Connection, tables: list[TableMeta]) -> dict[str, str]:
    q = quote_identifier
    names: dict[str, set[str]] = defaultdict(set)
    codes: dict[str, set[str]] = defaultdict(set)
    for table in tables:
        name, code = table.role("product_name"), table.role("product_id")
        if table.grain != "product_dimension" or not name:
            continue
        alias = next((c.name for c in table.columns if normalize_lookup(c.original_name) in {"ten goi khac", "aliases", "alias"}), None)
        fields = [q(name), q(code) if code else "NULL", q(alias) if alias else "NULL"]
        for display, identity, alternatives in connection.execute(f"SELECT {','.join(fields)} FROM {q(table.name)}"):
            if display is None:
                continue
            display = clean_text(display)
            for variant in [display, *str(alternatives or "").split(",")]:
                if variant.strip():
                    names[normalize_lookup(variant)].add(display)
            if identity is not None:
                codes[str(identity)].add(display)
    aliases = {name: next(iter(values)) for name, values in names.items() if len(values) == 1}
    code_names = {code: next(iter(values)) for code, values in codes.items() if len(values) == 1}
    for table in tables:
        name, code = table.role("product_name"), table.role("product_id")
        if not name:
            continue
        fields = ["rowid", q(name), q(code) if code else "NULL"]
        updates = []
        for rowid, original, identity in connection.execute(f"SELECT {','.join(fields)} FROM {q(table.name)}"):
            if original is None:
                continue
            canonical = code_names.get(str(identity)) if identity is not None else None
            canonical = canonical or aliases.get(normalize_lookup(original))
            if canonical and canonical != original:
                updates.append((canonical, rowid))
            if len(updates) >= 2000:
                connection.executemany(f"UPDATE {q(table.name)} SET {q(name)}=? WHERE rowid=?", updates)
                updates.clear()
        if updates:
            connection.executemany(f"UPDATE {q(table.name)} SET {q(name)}=? WHERE rowid=?", updates)
    return aliases


def canonicalize_customers(connection: sqlite3.Connection, tables: list[TableMeta]) -> dict[str, str]:
    q = quote_identifier
    TRANSACTIONAL_ROLES = {
        "order_id",
        "ordered_at",
        "paid_at",
        "product_id",
        "purchase_id",
        "line_amount",
        "total_due",
    }
    dim_candidates: dict[str, set[str]] = defaultdict(set)
    for table in tables:
        cid_col, cname_col = table.role("customer_id"), table.role("customer_name")
        if not cid_col or not cname_col:
            continue
        if any(table.role(role) for role in TRANSACTIONAL_ROLES):
            continue
        row = connection.execute(
            f"SELECT COUNT({q(cid_col)}), COUNT(DISTINCT {q(cid_col)}) FROM {q(table.name)} WHERE {q(cid_col)} IS NOT NULL"
        ).fetchone()
        if not row or row[0] == 0 or row[0] != row[1]:
            continue
        for cid, name in connection.execute(
            f"SELECT {q(cid_col)}, {q(cname_col)} FROM {q(table.name)} WHERE {q(cid_col)} IS NOT NULL AND {q(cname_col)} IS NOT NULL"
        ):
            if is_retail_customer(name):
                continue
            cleaned, _ = strip_embedded_phone(clean_text(name))
            if cleaned:
                dim_candidates[str(cid)].add(cleaned)

    catalog_map = {cid: next(iter(names)) for cid, names in dim_candidates.items() if len(names) == 1}

    cid_observed: dict[str, set[str]] = defaultdict(set)
    for table in tables:
        cid_col, cname_col = table.role("customer_id"), table.role("customer_name")
        if not cid_col or not cname_col:
            continue
        for cid, name in connection.execute(
            f"SELECT {q(cid_col)}, {q(cname_col)} FROM {q(table.name)} WHERE {q(cid_col)} IS NOT NULL AND {q(cname_col)} IS NOT NULL"
        ):
            txt = clean_text(name)
            if txt and not is_retail_customer(txt):
                cid_observed[str(cid)].add(txt)

    canonical_map: dict[str, str] = {}
    for cid_str, names in cid_observed.items():
        if cid_str in catalog_map:
            canonical_map[cid_str] = catalog_map[cid_str]
        else:
            stripped_map = {n: strip_embedded_phone(n)[0] for n in names}
            norm_stripped = {normalize_lookup(s) for s in stripped_map.values()}
            if len(norm_stripped) == 1:
                clean_candidates = [n for n in names if n == stripped_map[n]]
                if clean_candidates:
                    clean_candidates.sort(key=lambda x: (x.isupper(), -len(x)))
                    canonical_map[cid_str] = clean_candidates[0]
                else:
                    canonical_map[cid_str] = next(iter(stripped_map.values()))

    for table in tables:
        cid_col, cname_col = table.role("customer_id"), table.role("customer_name")
        if not cid_col or not cname_col:
            continue
        updates = []
        for rowid, original, identity in connection.execute(
            f"SELECT rowid, {q(cname_col)}, {q(cid_col)} FROM {q(table.name)} WHERE {q(cid_col)} IS NOT NULL AND {q(cname_col)} IS NOT NULL"
        ):
            if is_retail_customer(original):
                continue
            canonical = canonical_map.get(str(identity))
            if canonical and canonical != original:
                updates.append((canonical, rowid))
            if len(updates) >= 2000:
                connection.executemany(f"UPDATE {q(table.name)} SET {q(cname_col)}=? WHERE rowid=?", updates)
                updates.clear()
        if updates:
            connection.executemany(f"UPDATE {q(table.name)} SET {q(cname_col)}=? WHERE rowid=?", updates)

    return canonical_map


def refresh_examples(connection: sqlite3.Connection, tables: list[TableMeta]) -> list[TableMeta]:
    q = quote_identifier
    refreshed = []
    for table in tables:
        columns = []
        for column in table.columns:
            if column.semantic_role in {"product_name", "status", "customer_name"}:
                examples = [row[0] for row in connection.execute(
                    f"SELECT DISTINCT {q(column.name)} FROM {q(table.name)} WHERE {q(column.name)} IS NOT NULL LIMIT 3")]
                column = column.model_copy(update={"examples": examples})
            columns.append(column)
        refreshed.append(table.model_copy(update={"columns": columns}))
    return refreshed
