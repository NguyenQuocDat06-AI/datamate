"""Build canonical business tables from column meanings; independent of source table names."""

from __future__ import annotations

import difflib
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from bizreason.contracts import BusinessContext, TableMeta
from bizreason.data.notes_facets import classify_note_status
from bizreason.data.parsing import ParseStatus, blank, parse_date, parse_number, parse_status
from bizreason.semantic.vocabulary import (
    ADDRESS_HEADER_TOKENS,
    CANCEL_TOKENS,
    CURRENCY_SUFFIXES,
    INGESTION_ROLES,
    SERVICE_FEE_NAMES,
    WALK_IN_NAMES,
)
from bizreason.utils import normalize_lookup, quote_identifier as q

LINE_FIELDS = {
    "order_id": "order_id", "date": "ordered_at", "ts": "ordered_at", "status": "order_status",
    "product_id": "product_id", "product_name": "product_name", "customer_id": "customer_id",
    "customer_name": "customer_name", "customer_group": "customer_group", "employee": "employee_id",
    "channel": "channel", "payment_method": "payment_method", "unit": "unit", "category": "product_category",
}
LINE_NUMBERS = {
    "qty": "qty", "qty_base": "qty_base", "unit_price": "unit_price", "line_amount": "line_amount",
    "line_discount": "line_discount", "line_cost": "line_cost", "goods_total": "order_goods_total",
    "total_due": "order_total_due", "paid": "order_paid", "outstanding": "order_outstanding",
    "order_discount": "order_discount", "shipping_fee": "shipping_fee",
}
PURCHASE_FIELDS = {
    "purchase_id": "purchase_id", "date": "purchased_at", "supplier_id": "supplier_id",
    "supplier_name": "supplier_name", "status": "purchase_status", "product_id": "product_id",
    "product_name": "product_name",
}
PURCHASE_NUMBERS = {
    "qty": "qty_purchased", "unit_cost": "purchase_unit_cost", "amount": "purchase_line_amount",
    "receipt_total": "purchase_total",
}
PAYMENT_FIELDS = {"order_id": "order_id", "date": "paid_at", "method": "payment_method"}
PAYMENT_NUMBERS = {"amount": "paid_amount"}
DELIVERY_FIELDS = {"order_id": "order_id", "date": "delivered_at", "trip": "delivery_trip"}
DELIVERY_NUMBERS = {"qty": "delivery_qty"}
CATALOG_ID_ROLE = {"product_catalog": "product_id", "customer_catalog": "customer_id"}  # catalog kind -> id role
CATALOG_NAME_ROLE = {"product_id": "product_name", "customer_id": "customer_name", "supplier_id": "supplier_name"}
PRODUCT_ATTR_KINDS = ("product_catalog", "alias")  # tables that describe products beyond name and category
RECEIPT_FILL = ("date", "status", "supplier_id", "supplier_name")  # purchase columns a line can take from its receipt
CANONICAL_STATUS = ("completed", "shipping", "cancelled", "returned")
LINE_KINDS = ("sales", "service_ticket")  # kinds whose rows become raw_lines
SOURCE_ROW = "__source_row"  # ingestion adds this column to every source table
LINE_ITEM_KINDS = ("product", "discount", "shipping_fee", "service_fee")  # what a sale line can be classified as
NOT_REVENUE_LINE_KINDS = ("discount", "shipping_fee")  # lines that neither add to revenue nor appear in lines
QUANTITY_FIELDS = ("qty", "qty_base")  # parsed without a money unit; every other numeric field is money
# Roles that describe the order a row belongs to. A product row with no order id takes these from the order above it.
ORDER_ROLES = ("order_id", "ordered_at", "order_time", "order_status", "customer_id", "customer_name",
               "customer_group", "employee_id", "payment_method", "channel")
# A sales table with one of these but no order id has lost its order column; it is not loaded with synthetic orders.
ORDER_SIGNAL_ROLES = ("ordered_at", "order_status", "customer_id", "customer_name")
# Annotation roles whose keys must resolve to a column; an unresolved key is counted in build_stats.
COUNTED_ROLES = ("order_id", "product_name", "product_id", "qty", "line_amount", "ordered_at", "order_status",
                 "order_total_due", "paid_amount", "customer_id")
FUZZY_KEY_RATIO = 0.85  # minimum difflib ratio for an annotation key to name a column it does not spell exactly
BUILD_STAT_KEYS = (
    "qty_cells_nonblank", "qty_cells_unparsed", "money_cells_nonblank", "money_cells_unparsed",
    "product_rows_without_order", "annotation_keys_unresolved", "tables_skipped_missing_order_id",
    "status_unmapped_values",
)

_CURRENCY = "(?:" + "|".join(re.escape(suffix) for suffix in CURRENCY_SUFFIXES) + ")?"
COMMA_GROUPED = re.compile(r"[+-]?\d{1,3}(?:,\d{3})+\s*" + _CURRENCY, re.IGNORECASE)
DOT_GROUPED = re.compile(r"[+-]?\d{1,3}(?:\.\d{3})+\s*" + _CURRENCY, re.IGNORECASE)
TIME_OF_DAY = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")


def _number(value: Any, locale: str, unit: str | None = "VND") -> float | None:
    result = parse_number(value, locale=locale, unit=unit)
    return float(result.value) if result.status == ParseStatus.OK else None


def to_number(value: Any) -> float | None:
    return _number(value, "unknown")


def to_quantity(value: Any) -> float | None:
    """A quantity may carry a unit word ("1 ly"); the word is not a currency, so no unit is required."""
    return _number(value, "unknown", unit=None)


def _parse_fn(field: str, locale: str | None) -> str:
    base = "to_qty" if field in QUANTITY_FIELDS else "to_number"
    return f"{base}_{locale}" if locale else base


def to_date(value: Any) -> str | None:
    result = parse_date(value)
    return str(result.value)[:10] if result.status == ParseStatus.OK else None


def to_timestamp(order_time: Any, order_date: Any) -> str | None:
    """A full datetime is kept as it is. A time of day is joined to the row's date. Anything else is None."""
    if order_time is None or not str(order_time).strip():
        return None
    text = str(order_time).strip()
    clock = TIME_OF_DAY.fullmatch(text)
    if clock:
        day = to_date(order_date)
        hour, minute, second = int(clock[1]), int(clock[2]), int(clock[3] or 0)
        if day is None or hour > 23 or minute > 59 or second > 59:
            return None
        return f"{day} {hour:02d}:{minute:02d}:{second:02d}"
    result = parse_date(text)
    if result.status == ParseStatus.OK and " " in str(result.value):
        return str(result.value)
    return None


def infer_locale(values: Iterable[Any]) -> str | None:
    """Read one column's amount separators, or None to keep the default reading.

    "en": every value containing a comma is comma-grouped, so the comma is a thousands separator.
    "vi": the mirror rule for dots. A column mixing both shapes, or holding a value the other
    locale would misread, keeps the default reading.
    """
    texts = [str(value).strip() for value in values if value is not None and str(value).strip()]
    with_comma = [text for text in texts if "," in text]
    with_dot = [text for text in texts if "." in text]
    if with_comma and all(COMMA_GROUPED.fullmatch(text) for text in with_comma) \
            and not any(DOT_GROUPED.fullmatch(text) for text in texts):
        return "en"
    if with_dot and all(DOT_GROUPED.fullmatch(text) for text in with_dot) \
            and not any(COMMA_GROUPED.fullmatch(text) for text in texts):
        return "vi"
    return None


def annotations_by_signature(records: list[dict[str, Any]]) -> dict[tuple[str, ...], dict[str, Any]]:
    return {tuple(r["columns"]): r for r in records if "kind" in r}


def effective_roles(table: TableMeta, note: dict[str, Any], dst: sqlite3.Connection | None = None) -> dict[str, str]:
    """Role to column. The model's reading wins; ingestion evidence fills anything it left open.

    When the model gives one role to several columns, the source data (read through dst) picks one.
    Without dst the first such column wins.
    """
    given, _ = _given_by_column(table, note.get("roles") or {})
    candidates: dict[str, list[str]] = {}
    fallback: dict[str, str] = {}
    for column in table.columns:
        role = given.get(column.name)
        if _is_role(role):
            candidates.setdefault(role, []).append(column.name)
        mapped = INGESTION_ROLES.get(column.semantic_role or "")
        if mapped:
            fallback.setdefault(mapped, column.name)
    chosen = {role: _choose_column(dst, table, role, columns) for role, columns in candidates.items()}
    for role in ("order_status", "ordered_at", "qty_base"):
        if role in fallback:  # parsed and normalized at ingestion; stronger than a column-name reading
            chosen[role] = fallback[role]
    for role, column in fallback.items():
        chosen.setdefault(role, column)
    return chosen


def _is_role(role: Any) -> bool:
    return isinstance(role, str) and role != "other"


def _given_by_column(table: TableMeta, given: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The model's roles keyed by column name. A key that names no column (a typo) moves its role to the
    unassigned column it names (see _column_named). Returns the roles and the roles whose keys stayed unresolved."""
    names = {column.name for column in table.columns}
    by_column = {name: given[name] for name in names if name in given}
    unresolved: list[str] = []
    for key, role in given.items():
        if key in names or not _is_role(role):
            continue
        target = _column_named(key, table, by_column)
        if target is None:
            unresolved.append(role)
        else:
            by_column[target] = role
    return by_column, unresolved


def _column_named(key: str, table: TableMeta, by_column: dict[str, Any]) -> str | None:
    """The unassigned column a mistyped key names: equal after lookup normalization, else the one column
    whose name is close enough. Two equal matches, or two close ones, name nothing."""
    wanted = normalize_lookup(key)
    free = [column.name for column in table.columns if not _is_role(by_column.get(column.name))]
    equal = [name for name in free if normalize_lookup(name) == wanted]
    if equal:
        return equal[0] if len(equal) == 1 else None
    close = [
        name for name in free
        if difflib.SequenceMatcher(None, wanted, normalize_lookup(name)).ratio() >= FUZZY_KEY_RATIO
    ]
    return close[0] if len(close) == 1 else None


def _choose_column(dst: sqlite3.Connection | None, table: TableMeta, role: str, columns: list[str]) -> str:
    """The column that best carries a role when several do. No evidence, or a tie, keeps the first column."""
    if dst is None or len(columns) < 2:
        return columns[0]
    if role in ("product_id", "customer_id"):
        # the id that joins the matching catalog's ids wins; a barcode-like id that joins nothing loses
        catalog = {row[0] for row in dst.execute("SELECT id FROM temp.catalog_ids WHERE role=?", (role,))}
        score = {c: _share_in(_distinct_texts(dst, table, c), catalog) for c in columns}
    elif role in ("ordered_at", "order_time"):
        # a column carrying a clock time other than midnight beats a date-only column
        score = {c: float(sum(_has_clock(v) for v in _distinct_texts(dst, table, c))) for c in columns}
    elif role in ("customer_group", "product_category"):
        # the column that varies wins; a constant column never does
        score = {}
        for c in columns:
            distinct = len(_distinct_texts(dst, table, c))
            score[c] = float(distinct) if distinct >= 2 else 0.0
    else:
        return columns[0]
    best = max(columns, key=lambda c: score[c])  # max returns the first of equal scores
    return best if score[best] > 0 else columns[0]


def _distinct_texts(dst: sqlite3.Connection, table: TableMeta, column: str) -> list[str]:
    """Distinct non-blank values of a source column, as trimmed text. Read only."""
    rows = dst.execute(f"SELECT DISTINCT {_text(column)} FROM source.{q(table.name)} AS src")
    return [row[0] for row in rows if row[0] is not None]


def _share_in(values: list[str], reference: set[str]) -> float:
    return sum(value in reference for value in values) / len(values) if values else 0.0


def _has_clock(text: str) -> bool:
    """True when the text carries a clock time other than midnight."""
    clock = TIME_OF_DAY.search(text)
    return clock is not None and any(int(part or 0) for part in clock.groups())


def _text(column: str | None) -> str:
    return f"NULLIF(TRIM(CAST(src.{q(column)} AS TEXT)),'')" if column else "NULL"


def _position(table: TableMeta, column: str) -> int:
    """Index of a column in the source row. Ingestion stores each source row as a JSON array in that order."""
    return [c.name for c in table.columns].index(column)


def _status_text(table: TableMeta, column: str) -> str:
    """The order status as text. Ingestion stores an unrecognized status as NULL and keeps the original cell in __raw_json."""
    raw = f"NULLIF(TRIM(CAST(json_extract(src.{q('__raw_json')}, '$[{_position(table, column)}]') AS TEXT)),'')"
    return f"COALESCE({_text(column)}, {raw})"


def _note_status(note: Any) -> str | None:
    """The status a note states, by the notes_facets rules. A note that states none means completed; a note
    that states conflicting statuses gives NULL."""
    status, is_error = classify_note_status(note)
    return None if is_error else status


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _column_locale(dst: sqlite3.Connection, table: TableMeta, column: str) -> str | None:
    rows = dst.execute(
        f"SELECT DISTINCT CAST(src.{q(column)} AS TEXT) FROM source.{q(table.name)} AS src "
        f"WHERE src.{q(column)} IS NOT NULL"
    )
    return infer_locale(row[0] for row in rows)


def _source_columns(dst: sqlite3.Connection, table: TableMeta) -> list[str]:
    return [row[0] for row in dst.execute("SELECT name FROM pragma_table_info(?, 'source')", (table.name,))]


def _order_source(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str], report: dict[str, Any],
                  stats: Counter) -> str:
    """Copy a sales table with an order column to temp.order_source, ready to load.

    Rows identical in every source cell are one export error and keep only their first copy. A row whose order
    id is blank is never a duplicate of another, so it is kept. A product row with no order id then takes the
    order-level values of the nearest order above it, in source-row order (blank cells only, as ingestion does).
    A product row with no order above it stays an orphan and is counted.
    """
    order_column = roles["order_id"]
    columns = _source_columns(dst, table)
    group = ",".join(q(column) for column in columns if not column.startswith("__"))
    keep = (f"{_text(order_column)} IS NULL OR src.rowid IN "
            f"(SELECT MIN(rowid) FROM source.{q(table.name)} GROUP BY {group})")
    dst.execute("DROP TABLE IF EXISTS temp.order_source")
    dst.execute(f"CREATE TEMP TABLE order_source AS SELECT src.rowid AS __rid, src.* FROM source.{q(table.name)} "
                f"AS src WHERE {keep}")
    total = dst.execute(f"SELECT COUNT(*) FROM source.{q(table.name)}").fetchone()[0]
    kept = dst.execute("SELECT COUNT(*) FROM temp.order_source").fetchone()[0]
    report["duplicate_lines_removed"] += total - kept

    fill = [order_column] + [roles[role] for role in ORDER_ROLES if role != "order_id" and roles.get(role)]
    fill = list(dict.fromkeys(fill))
    product = [roles[role] for role in ("product_name", "product_id") if roles.get(role)]
    cursor = dst.execute("SELECT * FROM temp.order_source ORDER BY __source_row, __rid")
    names = [description[0] for description in cursor.description]
    rows = cursor.fetchall()
    carried: dict[str, Any] | None = None
    updates: list[tuple[Any, ...]] = []
    orphans = 0
    for record in rows:
        values = dict(zip(names, record))
        if not blank(values[order_column]):
            carried = {column: values[column] for column in fill}
            continue
        if not any(not blank(values[column]) for column in product):
            continue
        if carried is None:
            orphans += 1
            continue
        filled = [carried[column] if blank(values[column]) else values[column] for column in fill]
        updates.append((*filled, values["__rid"]))
    if updates:
        assignments = ",".join(f"{q(column)}=?" for column in fill)
        dst.executemany(f"UPDATE temp.order_source SET {assignments} WHERE __rid=?", updates)
    stats["product_rows_without_order"] += orphans
    return "temp.order_source"


def _insert(dst: sqlite3.Connection, target: str, table: TableMeta, roles: dict[str, str],
            texts: dict[str, str], numbers: dict[str, str], synthetic_prefix: str | None = None, *,
            origin: str | None = None, report: dict[str, Any] | None = None,
            defaults: dict[str, str] | None = None, stats: Counter | None = None) -> int:
    """Insert rows of a source table (or of origin, a prepared copy of it) into target."""
    source = origin or f"source.{q(table.name)}"
    names, exprs = [], []
    counted: set[tuple[str, str]] = set()
    note_column = roles.get("order_note") or roles.get("note")
    for field, role in texts.items():
        column = roles.get(role)
        names.append(field)
        if field == "date":
            exprs.append(f"to_date(src.{q(column)})" if column else "NULL")
        elif field == "ts" and roles.get("order_time"):
            # the order time supplies the clock; the stored date text is the fallback when it is blank
            day = f"src.{q(column)}" if column else "NULL"
            exprs.append(f"COALESCE(to_timestamp(src.{q(roles['order_time'])}, {day}), {_text(column)})")
        elif role == "order_status" and column is not None:
            exprs.append(_status_text(table, column))
        elif role == "order_status" and note_column is not None:
            # a ledger with no status column records its status in the notes (rulebook section 8)
            exprs.append(f"note_status(src.{q(note_column)})")
        elif field == "order_id" and column is None and synthetic_prefix is not None:
            # no order column: each row is its own order, keyed by the table and the row's source position
            exprs.append(f"{_sql_text(synthetic_prefix)} || src.{q(SOURCE_ROW)}")
        elif column is None and defaults and field in defaults:
            exprs.append(_sql_text(defaults[field]))
        else:
            exprs.append(_text(column))
    for field, role in numbers.items():
        column = roles.get(role)
        names.append(field)
        if column is None:
            exprs.append("NULL")
            continue
        locale = _column_locale(dst, table, column)
        if locale and report is not None:
            report["columns_with_locale_inferred"] += 1
        exprs.append(f"{_parse_fn(field, locale)}(src.{q(column)})")
        if stats is not None:
            _count_cells(dst, source, field, column, locale, stats, counted)
    cursor = dst.execute(
        f"INSERT INTO {target} ({','.join(names)}) SELECT {','.join(exprs)} FROM {source} AS src"
    )
    return cursor.rowcount


def _count_cells(dst: sqlite3.Connection, source: str, field: str, column: str, locale: str | None,
                 stats: Counter, counted: set[tuple[str, str]]) -> None:
    """Non-blank cells of a numeric column, and those whose parse failed. A column counts once per table."""
    kind = "qty" if field in QUANTITY_FIELDS else "money"
    if (kind, column) in counted:
        return
    counted.add((kind, column))
    text = _text(column)
    nonblank, unparsed = dst.execute(
        f"SELECT COUNT({text}), SUM(CASE WHEN {text} IS NOT NULL AND {_parse_fn(field, locale)}(src.{q(column)}) "
        f"IS NULL THEN 1 ELSE 0 END) FROM {source} AS src"
    ).fetchone()
    stats[f"{kind}_cells_nonblank"] += nonblank or 0
    stats[f"{kind}_cells_unparsed"] += unparsed or 0


def _load_lines(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str],
                synthetic_prefix: str, has_product: int, report: dict[str, Any], stats: Counter, *,
                origin: str | None = None, defaults: dict[str, str] | None = None) -> int:
    rows = _insert(dst, "raw_lines", table, roles, LINE_FIELDS, LINE_NUMBERS, synthetic_prefix,
                   origin=origin, report=report, defaults=defaults, stats=stats)
    dst.execute("UPDATE raw_lines SET has_product=? WHERE has_product IS NULL", (has_product,))
    return rows


def _load_sales(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str], synthetic: str,
                has_product: int, report: dict[str, Any], stats: Counter) -> None:
    """A sales table's lines. With an order column, the rows are first copied and ordered (see _order_source)."""
    if not roles.get("order_id"):
        _load_lines(dst, table, roles, synthetic, has_product, report, stats)
        return
    origin = _order_source(dst, table, roles, report, stats)
    _load_lines(dst, table, roles, synthetic, has_product, report, stats, origin=origin)
    dst.execute("DROP TABLE temp.order_source")


def _load_returns(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str]) -> None:
    """Collect the order ids a returns table lists; blank ids are ignored."""
    column = roles.get("order_id")
    if not column:
        return
    dst.execute(
        f"INSERT INTO temp.returned_orders (order_id) SELECT DISTINCT id FROM "
        f"(SELECT {_text(column)} AS id FROM source.{q(table.name)} AS src) WHERE id IS NOT NULL"
    )


def _load_customer_groups(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str]) -> None:
    group = roles.get("customer_group")
    if not group:
        return
    dst.execute(
        "INSERT INTO temp.customer_groups (customer_id, name_key, customer_group) "
        f"SELECT cid, NULLIF(lookup(nm), ''), grp FROM (SELECT {_text(roles.get('customer_id'))} AS cid, "
        f"{_text(roles.get('customer_name'))} AS nm, {_text(group)} AS grp "
        f"FROM source.{q(table.name)} AS src) WHERE grp IS NOT NULL AND (cid IS NOT NULL OR nm IS NOT NULL)"
    )


def _load_product_facts(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str]) -> None:
    """Category and base unit per product, from a product catalog or a unit conversion table."""
    if not (roles.get("product_id") or roles.get("product_name")):
        return
    if not (roles.get("product_category") or roles.get("base_unit")):
        return
    dst.execute(
        "INSERT INTO temp.product_facts (product_id, name_key, category, base_unit) "
        f"SELECT pid, NULLIF(lookup(nm), ''), cat, base FROM (SELECT {_text(roles.get('product_id'))} AS pid, "
        f"{_text(roles.get('product_name'))} AS nm, {_text(roles.get('product_category'))} AS cat, "
        f"{_text(roles.get('base_unit'))} AS base FROM source.{q(table.name)} AS src) "
        "WHERE pid IS NOT NULL OR nm IS NOT NULL"
    )


def _load_unit_factors(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str]) -> None:
    """Base units per large unit, for each product that a conversion or catalog row gives one for."""
    if not (roles.get("product_id") or roles.get("product_name")):
        return
    if not (roles.get("large_unit") and roles.get("conversion_factor")):
        return
    column = roles["conversion_factor"]
    locale = _column_locale(dst, table, column)
    factor = f"{_parse_fn('qty', locale)}(src.{q(column)})"  # a ratio of quantities, not money
    dst.execute(
        "INSERT INTO temp.unit_factors (product_id, name_key, large_key, factor) "
        f"SELECT pid, NULLIF(lookup(nm), ''), lk, fc FROM (SELECT {_text(roles.get('product_id'))} AS pid, "
        f"{_text(roles.get('product_name'))} AS nm, NULLIF(lookup({_text(roles['large_unit'])}), '') AS lk, "
        f"{factor} AS fc FROM source.{q(table.name)} AS src) "
        "WHERE (pid IS NOT NULL OR nm IS NOT NULL) AND lk IS NOT NULL AND fc IS NOT NULL"
    )


def _derive_qty_base(dst: sqlite3.Connection) -> int:
    """Fill qty_base where a line has a quantity and none. Returns the number of lines filled.

    The sold unit is the base unit: the quantity stands. A packing unit with a factor: quantity times factor.
    A product with a known base unit that gives no factor for the sold unit stays NULL. A product with no
    base unit and no factor row at all sells in its base unit (rulebook section 10).
    """
    base_by_id: dict[str, str] = {}
    base_by_name: dict[str, str] = {}
    for pid, name_key, base in dst.execute(
            "SELECT product_id, name_key, base_unit FROM temp.product_facts WHERE base_unit IS NOT NULL"):
        key = normalize_lookup(base)
        if pid is not None:
            base_by_id.setdefault(pid, key)
        if name_key is not None:
            base_by_name.setdefault(name_key, key)
    factors_by_id: dict[str, dict[str, float]] = {}
    factors_by_name: dict[str, dict[str, float]] = {}
    for pid, name_key, large, factor in dst.execute("SELECT product_id, name_key, large_key, factor FROM temp.unit_factors"):
        if pid is not None:
            factors_by_id.setdefault(pid, {}).setdefault(large, factor)
        if name_key is not None:
            factors_by_name.setdefault(name_key, {}).setdefault(large, factor)
    updates: list[tuple[float, int]] = []
    lines = dst.execute(
        "SELECT rowid, product_id, product_name, unit, qty FROM raw_lines "
        "WHERE qty_base IS NULL AND qty IS NOT NULL AND has_product=1"
    ).fetchall()
    for rowid, pid, name, unit, qty in lines:
        unit_key = normalize_lookup(unit) if unit is not None else ""
        if not unit_key:
            continue
        name_key = normalize_lookup(name) if name is not None else ""
        base = base_by_id.get(pid) if pid is not None else None
        if base is None and name_key:
            base = base_by_name.get(name_key)
        factor_map = (factors_by_id.get(pid) if pid is not None else None) or (
            factors_by_name.get(name_key) if name_key else None) or {}
        if base is not None and unit_key == base:
            updates.append((qty, rowid))
        elif unit_key in factor_map:
            updates.append((qty * factor_map[unit_key], rowid))
        elif base is None and not factor_map:
            updates.append((qty, rowid))
    dst.executemany("UPDATE raw_lines SET qty_base=? WHERE rowid=?", updates)
    return len(updates)


def _load_aliases(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str]) -> None:
    """Alias to full product name. When an alias repeats, the first row wins."""
    key, name = roles.get("alias_key"), roles.get("product_name")
    if not key or not name:
        return
    dst.execute(
        "INSERT OR IGNORE INTO temp.alias_map (alias_key, full_name) "
        f"SELECT ak, fn FROM (SELECT NULLIF(lookup({_text(key)}), '') AS ak, {_text(name)} AS fn "
        f"FROM source.{q(table.name)} AS src) WHERE ak IS NOT NULL AND fn IS NOT NULL"
    )


def _load_catalog_keys(dst: sqlite3.Connection, table: TableMeta, kind: str, roles: dict[str, str]) -> None:
    """Ids, and the name where the table gives one, for joins and fills. Supplier ids and names come from any table."""
    id_role = CATALOG_ID_ROLE.get(kind)
    if id_role:
        _insert_catalog_keys(dst, table, id_role, roles)
    if roles.get("supplier_id") and roles.get("supplier_name"):
        _insert_catalog_keys(dst, table, "supplier_id", roles)


def _insert_catalog_keys(dst: sqlite3.Connection, table: TableMeta, id_role: str, roles: dict[str, str]) -> None:
    if not roles.get(id_role):
        return
    dst.execute(
        "INSERT INTO temp.catalog_ids (role, id, name) "
        f"SELECT ?, cid, nm FROM (SELECT {_text(roles[id_role])} AS cid, "
        f"{_text(roles.get(CATALOG_NAME_ROLE[id_role]))} AS nm FROM source.{q(table.name)} AS src) "
        "WHERE cid IS NOT NULL",
        (id_role,),
    )


def _prescription_sql(column: str | None) -> str:
    """A yes or no column as 1, 0 or NULL."""
    if not column:
        return "NULL"
    text = f"lower({_text(column)})"
    return f"CASE WHEN {text} IN ('true','1','yes') THEN 1 WHEN {text} IN ('false','0','no') THEN 0 END"


def _load_product_attrs(dst: sqlite3.Connection, table: TableMeta, kind: str, roles: dict[str, str]) -> None:
    """Prescription flag and active ingredient from a product catalog or alias sheet; product kind from alias sheets.
    Matched to a line by product id or by normalized full name."""
    if not (roles.get("product_id") or roles.get("product_name")):
        return
    kind_column = roles.get("product_kind") if kind == "alias" else None
    if not (roles.get("prescription_flag") or roles.get("active_ingredient") or kind_column):
        return
    dst.execute(
        "INSERT INTO temp.product_attrs (product_id, name_key, prescription, active_ingredient, product_kind) "
        f"SELECT pid, NULLIF(lookup(nm), ''), rx, ai, pk FROM (SELECT {_text(roles.get('product_id'))} AS pid, "
        f"{_text(roles.get('product_name'))} AS nm, {_prescription_sql(roles.get('prescription_flag'))} AS rx, "
        f"{_text(roles.get('active_ingredient'))} AS ai, {_text(kind_column)} AS pk "
        f"FROM source.{q(table.name)} AS src) "
        "WHERE (pid IS NOT NULL OR nm IS NOT NULL) AND (rx IS NOT NULL OR ai IS NOT NULL OR pk IS NOT NULL)"
    )


def _catalog_name(id_role: str, id_expr: str) -> str:
    """The catalog name for an id, only when exactly one name goes with that id; otherwise NULL."""
    return (
        "(SELECT CASE WHEN COUNT(DISTINCT c.name)=1 THEN MIN(c.name) END FROM temp.catalog_ids c "
        f"WHERE c.role={_sql_text(id_role)} AND c.name IS NOT NULL AND c.id=TRIM(CAST({id_expr} AS TEXT)))"
    )


def _fill_from_catalogs(dst: sqlite3.Connection) -> None:
    """A line or purchase with an id and no name takes the catalog name for that id."""
    dst.execute(
        f"UPDATE raw_lines SET product_name={_catalog_name('product_id', 'raw_lines.product_id')} "
        "WHERE product_name IS NULL AND product_id IS NOT NULL"
    )
    dst.execute(
        f"UPDATE purchases SET product_name={_catalog_name('product_id', 'purchases.product_id')} "
        "WHERE product_name IS NULL AND product_id IS NOT NULL"
    )
    dst.execute(
        f"UPDATE purchases SET supplier_name={_catalog_name('supplier_id', 'purchases.supplier_id')} "
        "WHERE supplier_name IS NULL AND supplier_id IS NOT NULL"
    )


def _fill_receipts(dst: sqlite3.Connection) -> None:
    """Lines of one receipt share its date, status and supplier. A blank one takes the value from a sibling line."""
    for column in RECEIPT_FILL:
        dst.execute(
            f"UPDATE purchases SET {q(column)}=(SELECT MIN(p.{q(column)}) FROM purchases p "
            f"WHERE p.purchase_id=purchases.purchase_id AND p.{q(column)} IS NOT NULL) "
            f"WHERE {q(column)} IS NULL AND purchase_id IS NOT NULL"
        )


def _resolve_aliases(dst: sqlite3.Connection) -> int:
    """A line whose name is an alias takes the full product name. Only exact normalized matches change."""
    return dst.execute(
        "UPDATE raw_lines SET product_name=(SELECT a.full_name FROM temp.alias_map a "
        "WHERE a.alias_key=lookup(raw_lines.product_name)) "
        "WHERE lookup(raw_lines.product_name) IN (SELECT alias_key FROM temp.alias_map)"
    ).rowcount


def _classify_lines(dst: sqlite3.Connection, pairs: list[tuple[str, str]]) -> int:
    """Set line_kind on raw_lines by normalized product name; unmapped lines are products.

    Returns the number of lines that are not products.
    """
    dst.executemany("INSERT OR REPLACE INTO temp.line_kind_map (name, kind) VALUES (?,?)", pairs)
    dst.execute(
        "UPDATE raw_lines SET line_kind=COALESCE("
        "(SELECT m.kind FROM temp.line_kind_map m WHERE m.name=lookup(product_name)), 'product')"
    )
    return dst.execute("SELECT COUNT(*) FROM raw_lines WHERE line_kind<>'product'").fetchone()[0]


def _status_target(value: str, status_map: dict[str, str]) -> str | None:
    """The canonical status for a value: the deterministic reading when it has one, else the map, else None."""
    parsed = parse_status(value, domain="order")
    if parsed.status == ParseStatus.OK and parsed.value in CANONICAL_STATUS:
        return parsed.value
    target = status_map.get(value)
    return target if target in CANONICAL_STATUS else None


def _map_status(dst: sqlite3.Connection, status_map: dict[str, str], stats: Counter) -> int:
    """Canonical values stay; other values take their canonical reading; anything else becomes NULL.
    Returns the number of distinct values rewritten to a different canonical status."""
    values = [row[0] for row in dst.execute("SELECT DISTINCT status FROM raw_lines WHERE status IS NOT NULL")]
    mapped = 0
    for value in values:
        if value in CANONICAL_STATUS:
            continue
        target = _status_target(value, status_map)
        if target is not None:
            mapped += 1
            dst.execute("UPDATE raw_lines SET status=? WHERE status=?", (target, value))
        else:
            stats["status_unmapped_values"] += 1
            dst.execute("UPDATE raw_lines SET status=NULL WHERE status=?", (value,))
    return mapped


def _register(con: sqlite3.Connection) -> None:
    con.create_function("to_number", 1, to_number)
    con.create_function("to_number_en", 1, lambda v: _number(v, "en"))
    con.create_function("to_number_vi", 1, lambda v: _number(v, "vi"))
    con.create_function("to_qty", 1, to_quantity)
    con.create_function("to_qty_en", 1, lambda v: _number(v, "en", unit=None))
    con.create_function("to_qty_vi", 1, lambda v: _number(v, "vi", unit=None))
    con.create_function("to_date", 1, to_date)
    con.create_function("to_timestamp", 2, to_timestamp)
    con.create_function("lookup", 1, lambda v: None if v is None else normalize_lookup(v))
    con.create_function("note_status", 1, _note_status)


def build(context: BusinessContext, records: list[dict[str, Any]], path: Path,
          status_map: dict[str, str] | None = None, line_kinds: dict[str, str] | None = None) -> dict[str, Any]:
    """line_kinds maps a product name (normalized by normalize_lookup) to a LINE_ITEM_KINDS value."""
    pairs: list[tuple[str, str]] = []
    for name, kind in (line_kinds or {}).items():
        if kind not in LINE_ITEM_KINDS:
            raise ValueError(f"unknown line kind {kind!r}; expected one of {LINE_ITEM_KINDS}")
        pairs.append((normalize_lookup(name), kind))
    notes = annotations_by_signature(records)
    path.unlink(missing_ok=True)
    dst = sqlite3.connect(path)
    dst.close()
    dst = sqlite3.connect(f"file:{path}", uri=True)
    _register(dst)
    dst.execute("ATTACH DATABASE ? AS source", (str(context.database),))
    line_columns = list(LINE_FIELDS) + list(LINE_NUMBERS) + ["has_product", "line_kind"]
    dst.execute(f"CREATE TABLE raw_lines ({','.join(line_columns)})")
    purchase_columns = list(PURCHASE_FIELDS) + list(PURCHASE_NUMBERS)
    dst.execute(f"CREATE TABLE purchases ({','.join(purchase_columns)})")
    dst.execute("CREATE TABLE payments (order_id, date, amount, method)")
    dst.execute("CREATE TABLE deliveries (order_id, date, trip, qty)")
    dst.execute("CREATE TEMP TABLE returned_orders (order_id TEXT)")
    dst.execute("CREATE TEMP TABLE line_kind_map (name TEXT PRIMARY KEY, kind TEXT)")
    dst.execute("CREATE TEMP TABLE customer_groups (customer_id TEXT, name_key TEXT, customer_group TEXT)")
    dst.execute("CREATE TEMP TABLE product_facts (product_id TEXT, name_key TEXT, category TEXT, base_unit TEXT)")
    dst.execute("CREATE TEMP TABLE product_attrs (product_id TEXT, name_key TEXT, prescription INTEGER, "
                "active_ingredient TEXT, product_kind TEXT)")
    dst.execute("CREATE TEMP TABLE unit_factors (product_id TEXT, name_key TEXT, large_key TEXT, factor REAL)")
    dst.execute("CREATE TEMP TABLE catalog_ids (role TEXT, id TEXT, name TEXT)")
    dst.execute("CREATE TEMP TABLE alias_map (alias_key TEXT PRIMARY KEY, full_name TEXT)")
    dst.execute("CREATE TABLE build_stats (key TEXT, value INTEGER)")
    stats: Counter[str] = Counter({key: 0 for key in BUILD_STAT_KEYS})
    report: dict[str, Any] = {
        "sales_tables": 0, "service_tables": 0, "purchase_tables": 0, "return_tables": 0, "unannotated": [],
        "duplicate_lines_removed": 0, "columns_with_locale_inferred": 0, "tickets_defaulted_completed": 0,
        "qty_base_derived": 0,
    }
    annotated: list[tuple[TableMeta, dict[str, Any]]] = []
    for table in context.tables:
        note = notes.get(tuple(c.name for c in table.columns))
        if note is None:
            report["unannotated"].append(table.name)
        else:
            annotated.append((table, note))
    # catalog ids and names are read first, so that a duplicated id column can be judged by how it joins them
    for table, note in annotated:
        _load_catalog_keys(dst, table, note["kind"], effective_roles(table, note))
    for table, note in annotated:
        unresolved = _given_by_column(table, note.get("roles") or {})[1]
        stats["annotation_keys_unresolved"] += sum(role in COUNTED_ROLES for role in unresolved)
        roles = effective_roles(table, note, dst)
        if not roles.get("employee_id") and roles.get("employee_name"):
            roles["employee_id"] = roles["employee_name"]
        synthetic = f"{table.name}:"  # order id prefix for rows that have no order column
        if note["kind"] == "sales":
            # an order column that was named but matches no column, on a table with order-level columns: the
            # rows cannot be tied to their orders, so synthetic per-row orders would only break payment links
            lost_order = "order_id" in unresolved and not roles.get("order_id") and any(
                roles.get(role) for role in ORDER_SIGNAL_ROLES)
            if lost_order:
                stats["tables_skipped_missing_order_id"] += 1
                continue
            product = roles.get("product_name") or roles.get("product_id")
            _load_sales(dst, table, roles, synthetic, 1 if product else 0, report, stats)
            report["sales_tables"] += 1
        elif note["kind"] == "service_ticket":
            # one row per visit: the ticket is the order, its amount is the line, the work done is the product
            ticket = {role: column for role, column in roles.items() if role != "order_id"}
            if roles.get("ticket_id"):
                ticket["order_id"] = roles["ticket_id"]
            if roles.get("ticket_amount"):
                ticket["line_amount"] = roles["ticket_amount"]
            if roles.get("service_item"):
                ticket["product_name"] = roles["service_item"]
            # a ledger with no status column records it in its notes, and a note with no status means completed;
            # only a ledger with neither a status nor a note column is completed by default
            status_defaulted = "order_status" not in ticket and not (ticket.get("order_note") or ticket.get("note"))
            rows = _load_lines(dst, table, ticket, synthetic, 1, report, stats,
                               defaults={"status": "completed"} if status_defaulted else None)
            if status_defaulted:
                report["tickets_defaulted_completed"] += rows
            report["service_tables"] += 1
        elif note["kind"] == "purchase" and (roles.get("purchase_id") or roles.get("purchased_at")):
            _insert(dst, "purchases", table, roles, PURCHASE_FIELDS, PURCHASE_NUMBERS, report=report, stats=stats)
            report["purchase_tables"] += 1
        elif note["kind"] == "payment" and (roles.get("order_id") or roles.get("paid_at")):
            _insert(dst, "payments", table, roles, PAYMENT_FIELDS, PAYMENT_NUMBERS, report=report, stats=stats)
        elif note["kind"] == "delivery" and (roles.get("order_id") or roles.get("delivered_at")):
            _insert(dst, "deliveries", table, roles, DELIVERY_FIELDS, DELIVERY_NUMBERS, report=report, stats=stats)
        elif note["kind"] == "sales_return":
            _load_returns(dst, table, roles)
            report["return_tables"] += 1
        elif note["kind"] == "customer_catalog":
            _load_customer_groups(dst, table, roles)
        elif note["kind"] in ("product_catalog", "unit_conversion"):
            _load_product_facts(dst, table, roles)
            _load_unit_factors(dst, table, roles)
        elif note["kind"] == "alias":
            _load_aliases(dst, table, roles)
        if note["kind"] in PRODUCT_ATTR_KINDS:
            _load_product_attrs(dst, table, note["kind"], roles)
    _fill_receipts(dst)
    _fill_from_catalogs(dst)
    report["alias_lines_resolved"] = _resolve_aliases(dst)
    report["non_product_lines"] = _classify_lines(dst, pairs)
    report["status_mapped"] = _map_status(dst, status_map or {}, stats)
    report["qty_base_derived"] = _derive_qty_base(dst)
    _derive(dst)
    report["orders_marked_returned"] = _mark_returned(dst)
    _write_shop_facts(dst, context)
    dst.executemany("INSERT INTO build_stats VALUES (?,?)", [(key, stats[key]) for key in BUILD_STAT_KEYS])
    report["status_null_orders"] = dst.execute("SELECT COUNT(*) FROM orders WHERE status IS NULL").fetchone()[0]
    report["orders"] = dst.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    report["lines"] = dst.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
    report["purchases"] = dst.execute("SELECT COUNT(*) FROM purchases").fetchone()[0]
    report["payments"] = dst.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    report["deliveries"] = dst.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
    dst.commit()
    dst.execute("DETACH DATABASE source")
    dst.commit()
    dst.close()
    return report


def _mark_returned(dst: sqlite3.Connection) -> int:
    """Orders listed by a returns table are returned whole; cancelled orders stay cancelled.

    Returns the number of orders newly set to returned.
    """
    marked = dst.execute(
        "UPDATE orders SET status='returned' WHERE order_id IN (SELECT order_id FROM temp.returned_orders) "
        "AND COALESCE(status,'') NOT IN ('cancelled','returned')"
    ).rowcount
    dst.execute(
        "UPDATE lines SET status=(SELECT o.status FROM orders o WHERE o.order_id=lines.order_id) "
        "WHERE order_id IN (SELECT order_id FROM temp.returned_orders)"
    )
    status = ",".join(_sql_text(value) for value in CANONICAL_STATUS)
    dst.execute("DELETE FROM status_domain")
    dst.execute(f"INSERT INTO status_domain SELECT DISTINCT status FROM orders WHERE status IN ({status})")
    return marked


def _write_shop_facts(dst: sqlite3.Connection, context: BusinessContext) -> None:
    """Yes-or-no facts about the shop, read by the question layer: an address column, a customer group, any orders."""
    address = any(_has_address_header(column.original_name) for table in context.tables for column in table.columns)
    group = dst.execute("SELECT EXISTS(SELECT 1 FROM orders WHERE customer_group IS NOT NULL)").fetchone()[0]
    dated = dst.execute("SELECT EXISTS(SELECT 1 FROM orders WHERE date IS NOT NULL)").fetchone()[0]
    dst.execute("CREATE TABLE shop_facts (key TEXT, value INTEGER)")
    dst.executemany("INSERT INTO shop_facts VALUES (?,?)",
                    [("has_address", int(address)), ("has_customer_group", group), ("has_orders", dated)])


def _has_address_header(original_name: str) -> bool:
    text = normalize_lookup(original_name)
    return any(token in text for token in ADDRESS_HEADER_TOKENS)


def _derive(dst: sqlite3.Connection) -> None:
    _derive_orders(dst)
    _fill_customer_names(dst)
    _fill_customer_groups(dst)
    _derive_lines(dst)
    tokens = " OR ".join(f"lookup(status) LIKE '%{t}%'" for t in CANCEL_TOKENS)
    dst.execute("ALTER TABLE purchases ADD COLUMN cancelled")
    dst.execute(f"UPDATE purchases SET cancelled=CASE WHEN status IS NOT NULL AND ({tokens}) THEN 1 ELSE 0 END")


def _derive_orders(dst: sqlite3.Connection) -> None:
    walk_in = ",".join(f"'{name}'" for name in WALK_IN_NAMES)
    not_revenue = ",".join(_sql_text(kind) for kind in NOT_REVENUE_LINE_KINDS)
    dst.executescript(
        f"""
        CREATE INDEX raw_order ON raw_lines(order_id);
        CREATE TABLE orders AS
        SELECT order_id,
               MIN(date) AS date, MIN(COALESCE(ts, date)) AS ts,
               CASE WHEN SUM(status='returned')>0 THEN 'returned'
                    WHEN SUM(status='cancelled')>0 THEN 'cancelled'
                    WHEN SUM(status='shipping')>0 THEN 'shipping'
                    WHEN SUM(status='completed')>0 THEN 'completed' END AS status,
               MAX(customer_id) AS customer_id, MAX(customer_name) AS customer_name,
               MAX(customer_group) AS customer_group, MAX(employee) AS employee,
               MAX(channel) AS channel, MAX(payment_method) AS payment_method,
               SUM(CASE WHEN has_product=1 AND line_kind NOT IN ({not_revenue}) THEN COALESCE(
                    line_amount, qty*unit_price-COALESCE(line_discount,0)) END) AS line_total,
               MAX(goods_total) AS goods_total, MAX(total_due) AS total_due,
               MAX(paid) AS paid, MAX(outstanding) AS outstanding
        FROM raw_lines WHERE order_id IS NOT NULL GROUP BY order_id;
        ALTER TABLE orders ADD COLUMN revenue;
        UPDATE orders SET revenue=COALESCE(line_total, goods_total);
        ALTER TABLE orders ADD COLUMN customer_key;
        UPDATE orders SET customer_key=CASE
            WHEN EXISTS (SELECT 1 FROM orders o WHERE o.customer_id IS NOT NULL) THEN customer_id
            WHEN customer_name IS NULL OR lookup(customer_name) IN ({walk_in}) THEN NULL
            ELSE lookup(customer_name) END;
        CREATE UNIQUE INDEX orders_id ON orders(order_id);
        CREATE INDEX orders_date ON orders(date);
        """
    )


def _fill_customer_names(dst: sqlite3.Connection) -> None:
    """An order with a customer id and no name takes the name the customer catalog gives that id."""
    dst.execute(
        f"UPDATE orders SET customer_name={_catalog_name('customer_id', 'orders.customer_id')} "
        "WHERE customer_name IS NULL AND customer_id IS NOT NULL"
    )


def _fill_customer_groups(dst: sqlite3.Connection) -> None:
    """An order with no group takes its customer's group from a customer catalog: by id when the order
    has an id, by normalized name when it has none (two codes sharing a name are two customers)."""
    dst.execute(
        "UPDATE orders SET customer_group = COALESCE("
        "(SELECT MIN(m.customer_group) FROM temp.customer_groups m "
        " WHERE orders.customer_id IS NOT NULL AND m.customer_id = orders.customer_id), "
        "(SELECT MIN(m.customer_group) FROM temp.customer_groups m "
        " WHERE orders.customer_id IS NULL AND m.name_key = NULLIF(lookup(orders.customer_name), ''))) "
        "WHERE customer_group IS NULL"
    )


def _product_fact(column: str, source: str = "temp.product_facts") -> str:
    """A catalog value for a line: by product id, else by normalized product name."""
    return (
        f"COALESCE((SELECT MIN(f.{column}) FROM {source} f "
        f"WHERE l.product_id IS NOT NULL AND f.product_id = l.product_id), "
        f"(SELECT MIN(f.{column}) FROM {source} f "
        f"WHERE f.name_key = NULLIF(lookup(l.product_name), '')))"
    )


def _derive_lines(dst: sqlite3.Connection) -> None:
    not_revenue = ",".join(_sql_text(kind) for kind in NOT_REVENUE_LINE_KINDS)
    status = "','".join(CANONICAL_STATUS)
    service_fees = ",".join(_sql_text(normalize_lookup(name)) for name in SERVICE_FEE_NAMES)
    dst.executescript(
        f"""
        CREATE TABLE lines AS
        SELECT l.order_id, o.date, o.ts, o.status, o.customer_key, o.customer_name,
               o.customer_group, o.channel, o.payment_method, o.employee,
               l.product_id, l.product_name, COALESCE(l.product_id, lookup(l.product_name)) AS product_key,
               COALESCE(l.category, {_product_fact('category')}) AS category,
               l.unit, l.qty, l.qty_base,
               COALESCE(l.line_amount, l.qty*l.unit_price-COALESCE(l.line_discount,0)) AS amount,
               l.line_cost, l.unit_price,
               CASE WHEN l.line_kind='service_fee' OR lookup(l.product_name) IN ({service_fees}) THEN 0 ELSE 1 END
                   AS is_item,
               {_product_fact('base_unit')} AS base_unit,
               {_product_fact('prescription', 'temp.product_attrs')} AS prescription,
               {_product_fact('active_ingredient', 'temp.product_attrs')} AS active_ingredient,
               {_product_fact('product_kind', 'temp.product_attrs')} AS product_kind
        FROM raw_lines l JOIN orders o USING(order_id)
        WHERE l.has_product=1 AND l.line_kind NOT IN ({not_revenue})
          AND (l.product_name IS NOT NULL OR l.product_id IS NOT NULL);
        CREATE INDEX lines_date ON lines(date);
        CREATE TABLE status_domain AS SELECT DISTINCT status FROM orders WHERE status IN ('{status}');
        """
    )
