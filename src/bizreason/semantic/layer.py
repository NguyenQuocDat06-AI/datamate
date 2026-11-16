"""Build canonical business tables from column meanings; independent of source table names."""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any, NamedTuple

from bizreason.contracts import BusinessContext, TableMeta
from bizreason.data.identities import unify_phone_spellings
from bizreason.data.notes_facets import classify_note_status
from bizreason.data.parsing import ParseStatus, blank, parse_date, parse_number, parse_status
from bizreason.semantic import annotation_check, debt_sources, obligations, rulebook_params, stock_sources
from bizreason.semantic.vocabulary import (
    ADDRESS_HEADER_TOKENS,
    CANCEL_TOKENS,
    CURRENCY_SUFFIXES,
    EXAMPLE_CUES,
    INGESTION_ROLES,
)
from bizreason.data.vocabulary import (
    UNIT_EACH_WORDS,
    UNIT_NON_QUANTITY_WORDS,
    UNIT_PACKING_CUES,
    UNIT_STATEMENT_LINKERS,
)
from bizreason.utils import normalize_lookup, quote_identifier as q

class ModelFailure(NamedTuple):
    """A model call that failed and was replaced by a fallback. `table_name` and `kind` are None for a shop-wide
    stage (line_kinds, status, value_groups:<role>) and for a table whose kind is not known."""

    stage: str
    table_name: str | None = None
    kind: str | None = None


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
    "product_name": "product_name", "unit": "unit",
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
STOCK_KINDS = ("inventory", "lot")  # tables that record stock: a stock report, or lots with their expiry dates
PRODUCT_ATTR_KINDS = ("product_catalog", "alias")  # tables that describe products beyond name and category
# Catalog kinds read across every sheet that shares their columns: a catalog exported in parts (one sheet per month)
# is one catalog. A role is chosen from the values of all its sheets, and a sheet whose columns differ slightly
# takes the annotation of its sibling.
CATALOG_KINDS = ("customer_catalog", "product_catalog", "employee_catalog")
# The sibling rule of annotator.SIBLING_MIN_SHARE. annotator imports this module, so the value is repeated here;
# tests check that the two agree.
CATALOG_SIBLING_MIN_SHARE = 0.8
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
AFFIX_KEY_MIN = 4  # shortest compact key that may name a column by being its prefix or suffix
TYPO_KEY_MIN = 6  # shortest compact key that may name a column one or two typing errors away
TYPO_MAX_EDITS = 2
BUILD_STAT_KEYS = (
    "qty_cells_nonblank", "qty_cells_unparsed", "money_cells_nonblank", "money_cells_unparsed",
    "product_rows_without_order", "annotation_keys_unresolved", "tables_skipped_missing_order_id",
    "status_unmapped_values", "subtotal_rows_removed", "combined_items_split", "combined_items_unsplit",
    "values_regrouped", "scaled_tables",
)
SUBTOTAL_TOLERANCE = 1  # a subtotal row's amount may differ from the sum of its run by at most this many units
COMBINED_SEPARATOR = " + "  # one sales cell naming an item and its toppings (rulebook section 6)
COMBINED_TOPPING_WORD = re.compile(r"\b(?:toppings?|them)\b")  # ASCII-folded words for a topping
TOPPING_PART = re.compile(r"\s*(?:(\d+)\s+)?(.*?)\s*(?:\(\s*(\d[\d.,]*)\s*\))?\s*")  # [count] name [(price)]
SENTENCE_END = re.compile(r"[.!?;\n]")
AMOUNT_SCALE_CODE = "AMOUNT_SCALE"  # ingestion diagnostic: a sheet's money is in units of the message's integer
# value group role -> the (table, field) canonical columns it regroups; the original is kept as <field>_raw for these
VALUE_GROUP_FIELDS = {
    "payment_method": (("orders", "payment_method"), ("lines", "payment_method")),
    "channel": (("orders", "channel"), ("lines", "channel")),
    "customer_group": (("orders", "customer_group"), ("lines", "customer_group")),
    "product_category": (("lines", "category"),),
    "unit": (("lines", "unit"),),
    "base_unit": (("lines", "base_unit"),),
}
VALUE_GROUP_KEEPS_RAW = ("payment_method", "channel", "customer_group", "category")
# Source columns that no canonical field reads are kept (see _carry_plan): x_<name> text, xn_<name> number and
# xd_<name> date twins. A table's unmapped columns are carried only for these kinds; every other source table
# is copied whole as src_<table>, including catalogs whose loader reads just selected roles (see _copy_unloaded).
EXTRA_PREFIXES = ("x_", "xn_", "xd_")
CARRIED_KINDS = ("sales", "service_ticket", "purchase", "payment", "delivery")
LAYER_KINDS = (*CARRIED_KINDS, "sales_return", "customer_catalog", "product_catalog", "unit_conversion", "alias",
               "employee_catalog", "expense", "price_history")
CARRY_MIN_SHARE = 0.8  # share of non-blank values that must parse as a number or a date, or read as a code
CODE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")  # letters and digits, no spaces: a plate or an id
CODE_MAX_LENGTH = 24
CODE_MIN_DIGITS = 10  # a digit string this long is an identifier, not an amount
JOIN_MIN_SHARE = 0.5  # share of a column's distinct values that must be order ids for it to join orders
CATALOG_SAMPLES = 8
# Order-level adjustments and the order total (see _derive_order_totals).
ORDER_LEVEL_MIN_ORDERS = 10  # multi-line orders with a discount needed to read a line column as an order discount
ORDER_LEVEL_SHARE = 0.95  # share of those orders whose lines all repeat one value
AMOUNT_TOLERANCE = 1  # two money amounts within this many units are equal
TOTAL_AGREE_SHARE = 0.9  # share of orders whose derived total must match paid plus outstanding, where both exist
# Ledger payments (see _load_ledger_payments).
LEDGER_KEY_SHARE = 0.95  # share of a ledger's keyed rows that must resolve to exactly one order
LEDGER_PRICE_SHARE = 0.9  # a paid column equal to the order total this often is the order price, not payments
LEDGER_LATER_SHARE = 0.5  # share of rows dated after their order for a ledger date to read as a payment date
KEY_NUMBER = re.compile(r"\d+")
TRAILING_DIGITS = re.compile(r"(\d+)\s*$")
CATALOG_TARGETS = ("raw_lines", "lines", "orders", "purchases", "payments", "deliveries")
# The roles each loader reads, so that every other column of its table is carried
LINE_READ_ROLES = tuple(dict.fromkeys([
    *LINE_FIELDS.values(), *LINE_NUMBERS.values(), *ORDER_ROLES, "order_time",
    "product_name", "product_id", "line_amount",
]))
SERVICE_READ_ROLES = ("ticket_id", "ticket_amount", "service_item")
PURCHASE_READ_ROLES = (*PURCHASE_FIELDS.values(), *PURCHASE_NUMBERS.values(), "lot_id", "expires_at",
                       "qty_purchased_base")
PAYMENT_READ_ROLES = (*PAYMENT_FIELDS.values(), *PAYMENT_NUMBERS.values())
DELIVERY_READ_ROLES = (*DELIVERY_FIELDS.values(), *DELIVERY_NUMBERS.values())

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


def _catalog_sibling_fit(names: list[str], other: list[str]) -> tuple[float, bool] | None:
    """The sibling rule of annotator._sibling_fit: (score, positional) when the normalized column names overlap by
    CATALOG_SIBLING_MIN_SHARE (Jaccard), or the counts are equal and that share sits at the same positions; else None."""
    mine = [normalize_lookup(name) for name in names]
    theirs = [normalize_lookup(name) for name in other]
    union = set(mine) | set(theirs)
    jaccard = len(set(mine) & set(theirs)) / len(union) if union else 0.0
    same_count = bool(mine) and len(mine) == len(theirs)
    positional = sum(a == b for a, b in zip(mine, theirs)) / len(mine) if same_count else 0.0
    if jaccard >= CATALOG_SIBLING_MIN_SHARE:
        return jaccard, positional >= CATALOG_SIBLING_MIN_SHARE
    if same_count and positional >= CATALOG_SIBLING_MIN_SHARE:
        return positional, True
    return None


def _catalog_sibling(table: TableMeta,
                     notes: dict[tuple[str, ...], dict[str, Any]]) -> tuple[tuple[str, ...], dict[str, Any]] | None:
    """The annotation of the best-fitting catalog sibling for a table that has none of its own: (sibling key, note
    with the roles of this table). Roles map by normalized column name; when the counts match and the sheets line up,
    a column whose name differs takes the role at its position. None when no usable catalog annotation fits."""
    names = [column.name for column in table.columns]
    best: tuple[float, bool, tuple[str, ...], dict[str, Any]] | None = None
    for key, note in notes.items():
        if note.get("kind") not in CATALOG_KINDS or "error" in note or note.get("partial") or note.get("kind_failed"):
            continue
        fit = _catalog_sibling_fit(names, list(key))
        if fit is not None and (best is None or fit[0] > best[0]):
            best = (fit[0], fit[1], key, note)
    if best is None:
        return None
    _, positional, key, sibling = best
    sibling_roles = sibling.get("roles") or {}
    by_name: dict[str, str] = {}
    for name in key:
        by_name.setdefault(normalize_lookup(name), sibling_roles.get(name, "other"))
    by_position = [sibling_roles.get(name, "other") for name in key]
    roles: dict[str, str] = {}
    for index, name in enumerate(names):
        role = by_name.get(normalize_lookup(name))
        if role is None and positional:
            role = by_position[index]
        roles[name] = role or "other"
    return key, {"kind": sibling["kind"], "columns": names, "roles": roles}


def effective_roles(table: TableMeta, note: dict[str, Any], dst: sqlite3.Connection | None = None,
                    pool: list[TableMeta] | None = None) -> dict[str, str]:
    """Role to column. The model's reading wins; ingestion evidence fills anything it left open.

    When the model gives one role to several columns, the source data (read through dst) picks one. pool lists the
    tables that share this annotation (a catalog in parts): the evidence is read from all of them, so that every
    sheet of the catalog picks the same column. Without dst the first such column wins.
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
    chosen = {role: _choose_column(dst, table, role, columns, pool) for role, columns in candidates.items()}
    for role in ("order_status", "qty_base"):
        if role in fallback:  # parsed and normalized at ingestion; stronger than a column-name reading
            chosen[role] = fallback[role]
    if "ordered_at" in fallback:
        chosen["ordered_at"] = _ordered_at_column(dst, table, fallback["ordered_at"], candidates.get("ordered_at", []))
    for role, column in fallback.items():
        chosen.setdefault(role, column)
    return chosen


def _ordered_at_column(dst: sqlite3.Connection | None, table: TableMeta, fallback_column: str,
                       model_columns: list[str]) -> str:
    """The ordered_at column. The ingestion column competes with the model's other ordered_at columns: one
    carrying a clock wins, but only if it also carries a date (a time-only column must never become the date)."""
    others = [column for column in model_columns if column != fallback_column]
    if dst is None or not others:
        return fallback_column
    winner = _choose_column(dst, table, "ordered_at", [fallback_column, *others])
    if winner == fallback_column:
        return fallback_column
    values = _distinct_texts(dst, table, winner)
    dated = sum(to_date(value) is not None for value in values)
    return winner if values and dated * 2 >= len(values) else fallback_column


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


def _compact_key(value: str) -> str:
    """Lookup form without spaces: case, accents, separators and spacing do not count."""
    return normalize_lookup(value).replace(" ", "")


def _column_named(key: str, table: TableMeta, by_column: dict[str, Any]) -> str | None:
    """The unassigned column a mistyped key names. Each tier is tried in turn: the same letters after case, accent,
    separator and spacing are dropped; the key is a prefix or suffix of the column name; the names are close by
    difflib ratio; or they are one or two typing errors apart. The first tier that matches any column decides, and
    a tier with two matches names nothing (ambiguous)."""
    wanted = _compact_key(key)
    if not wanted:
        return None
    free = [column.name for column in table.columns if not _is_role(by_column.get(column.name))]
    tiers = (
        lambda name: _compact_key(name) == wanted,
        lambda name: (len(wanted) >= AFFIX_KEY_MIN and _compact_key(name) != wanted
                      and (_compact_key(name).startswith(wanted) or _compact_key(name).endswith(wanted))),
        lambda name: difflib.SequenceMatcher(None, wanted, _compact_key(name)).ratio() >= FUZZY_KEY_RATIO,
        lambda name: (len(wanted) >= TYPO_KEY_MIN
                      and _edit_distance(wanted, _compact_key(name)) <= TYPO_MAX_EDITS),
    )
    for tier in tiers:
        matches = [name for name in free if tier(name)]
        if matches:
            return matches[0] if len(matches) == 1 else None
    return None


def _edit_distance(first: str, second: str) -> int:
    """Levenshtein distance: insertions, deletions and substitutions, one character each."""
    previous = list(range(len(second) + 1))
    for i, char in enumerate(first, start=1):
        current = [i]
        for j, other in enumerate(second, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char != other)))
        previous = current
    return previous[-1]


def _choose_column(dst: sqlite3.Connection | None, table: TableMeta, role: str, columns: list[str],
                   pool: list[TableMeta] | None = None) -> str:
    """The column that best carries a role when several do. No evidence, or a tie, keeps the first column."""
    if dst is None or len(columns) < 2:
        return columns[0]
    if role in ("product_id", "customer_id"):
        # the id that joins the matching catalog's ids wins; a barcode-like id that joins nothing loses
        catalog = {row[0] for row in dst.execute("SELECT id FROM temp.catalog_ids WHERE role=?", (role,))}
        score = {c: _share_in(_distinct_texts(dst, table, c, pool), catalog) for c in columns} if catalog else {}
        if not score or _no_clear_winner(score):
            # no catalog evidence: the id with the most filled, distinct values wins (a mostly blank barcode loses)
            score = {c: float(len(_distinct_texts(dst, table, c, pool))) for c in columns}
    elif role in ("ordered_at", "order_time"):
        # a column carrying a clock time other than midnight beats a date-only column
        score = {c: float(sum(_has_clock(v) for v in _distinct_texts(dst, table, c, pool))) for c in columns}
    elif role in ("customer_group", "product_category"):
        # the column that varies wins; a constant column never does
        score = {}
        for c in columns:
            distinct = len(_distinct_texts(dst, table, c, pool))
            score[c] = float(distinct) if distinct >= 2 else 0.0
    else:
        return columns[0]
    best = max(columns, key=lambda c: score[c])  # max returns the first of equal scores
    return best if score[best] > 0 else columns[0]


def _no_clear_winner(score: dict[str, float]) -> bool:
    """True when every candidate scores 0, or the best score is shared."""
    best = max(score.values())
    return best == 0 or sum(value == best for value in score.values()) > 1


def _distinct_texts(dst: sqlite3.Connection, table: TableMeta, column: str,
                    pool: list[TableMeta] | None = None) -> list[str]:
    """Distinct non-blank values of a source column, as trimmed text. Read only.

    With a pool, the values are those of every pool table (the table itself included) that has a column of the same
    normalized name.
    """
    members = list(pool or [])
    if not any(member.name == table.name for member in members):
        members.insert(0, table)
    texts: list[str] = []
    seen: set[str] = set()
    for member in members:
        source_column = _column_like(member, column)
        if source_column is None:
            continue
        for (value,) in dst.execute(f"SELECT DISTINCT {_text(source_column)} FROM source.{q(member.name)} AS src"):
            if value is not None and value not in seen:
                seen.add(value)
                texts.append(value)
    return texts


def _column_like(table: TableMeta, column: str) -> str | None:
    """The column of a table with this name, or else the one whose normalized name is the same; None when absent."""
    names = [c.name for c in table.columns]
    if column in names:
        return column
    wanted = normalize_lookup(column)
    return next((name for name in names if normalize_lookup(name) == wanted), None)


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


def _sales_source(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str], report: dict[str, Any],
                  stats: Counter) -> str | None:
    """Copy a sales table to temp.order_source, ready to load, or return None when it loads as it is.

    Rows identical in every source cell are one export error and keep only their first copy (order tables only).
    A row whose order id is blank is never a duplicate of another, so it is kept.
    A subtotal row (see _subtotal_rows) is removed. It also closes its group: a product row after it takes no
    order-level values from above it.
    A product row with no order id takes the order-level values of the nearest order above it, in source-row order
    (blank cells only, as ingestion does), and any order id change starts a new group. A product row with no order
    above it, or after a subtotal, stays an orphan and is counted.
    """
    order_column = roles.get("order_id")
    amount_column = roles.get("line_amount")
    if not order_column and not amount_column:
        return None  # nothing to remove or fill: the table loads as it is
    dst.execute("DROP TABLE IF EXISTS temp.order_source")
    if order_column:
        columns = _source_columns(dst, table)
        group = ",".join(q(column) for column in columns if not column.startswith("__"))
        keep = (f"{_text(order_column)} IS NULL OR src.rowid IN "
                f"(SELECT MIN(rowid) FROM source.{q(table.name)} GROUP BY {group})")
    else:
        keep = "1"
    dst.execute(f"CREATE TEMP TABLE order_source AS SELECT src.rowid AS __rid, src.* FROM source.{q(table.name)} "
                f"AS src WHERE {keep}")
    if order_column:
        total = dst.execute(f"SELECT COUNT(*) FROM source.{q(table.name)}").fetchone()[0]
        kept = dst.execute("SELECT COUNT(*) FROM temp.order_source").fetchone()[0]
        report["duplicate_lines_removed"] += total - kept

    product = [roles[role] for role in ("product_name", "product_id") if roles.get(role)]
    fill = []
    if order_column:
        fill = [order_column] + [roles[role] for role in ORDER_ROLES if role != "order_id" and roles.get(role)]
        fill = list(dict.fromkeys(fill))
    locale = _column_locale(dst, table, amount_column) if amount_column else None
    cursor = dst.execute("SELECT * FROM temp.order_source ORDER BY __source_row, __rid")
    names = [description[0] for description in cursor.description]
    rows = [dict(zip(names, record)) for record in cursor.fetchall()]
    subtotals = _subtotal_rows(rows, product, amount_column, locale)

    carried: dict[str, Any] | None = None
    updates: list[tuple[Any, ...]] = []
    orphans = 0
    for values in rows:
        if values["__rid"] in subtotals:
            carried = None  # a subtotal closes the group above it
            continue
        if not order_column:
            continue
        if not blank(values[order_column]):
            carried = {column: values[column] for column in fill}  # an order id starts its own group
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
    if subtotals:
        dst.executemany("DELETE FROM temp.order_source WHERE __rid=?", [(rid,) for rid in subtotals])
    stats["subtotal_rows_removed"] += len(subtotals)
    stats["product_rows_without_order"] += orphans
    return "temp.order_source"


def _subtotal_rows(rows: list[dict[str, Any]], product: list[str], amount_column: str | None,
                   locale: str | None) -> set[Any]:
    """The __rid of each subtotal row, in source-row order.

    A subtotal has no product (every product cell blank) and an amount within SUBTOTAL_TOLERANCE of the sum of the
    product rows since the previous subtotal or the table start. A run holding a product row whose amount does not
    read is never matched. A product row is never a subtotal, whatever its amount.
    """
    if not amount_column:
        return set()
    found: set[Any] = set()
    run_sum, run_count, run_known = 0.0, 0, True
    for values in rows:
        amount = _number(values[amount_column], locale or "unknown")
        if any(not blank(values[column]) for column in product):
            run_count += 1
            if amount is None:
                run_known = False
            else:
                run_sum += amount
        elif (amount is not None and run_count and run_known
              and abs(amount - run_sum) <= SUBTOTAL_TOLERANCE):
            found.add(values["__rid"])
            run_sum, run_count, run_known = 0.0, 0, True
    return found


class Carried(NamedTuple):
    """A source column that no canonical field reads. It is kept under its ingestion name (a safe identifier)."""

    table: str  # source table name
    source: str  # ingestion column name
    header: str  # the header the source file shows for the column
    role: str | None  # the annotated role; None when the table has no annotation
    value_type: str  # text, number, date or code
    numeric: bool  # an xn_ twin is kept
    dated: bool  # an xd_ twin is kept
    locale: str | None  # the amount separators read for the numeric twin


def _role_of(column: str, given: dict[str, Any] | None) -> str | None:
    if given is None:
        return None
    role = given.get(column)
    return role if isinstance(role, str) else "other"


def _carry_plan(dst: sqlite3.Connection, table: TableMeta, consumed: set[str],
                given: dict[str, Any] | None) -> list[Carried]:
    """Every column of table not in consumed, typed from its values. Each column is grouped by value once and every
    distinct value is read once, weighted by its count.

    A column has a numeric twin (dated twin) when CARRY_MIN_SHARE of its non-blank values parse with the number
    (date) reader; its value type is date, number, code (letters and digits, no spaces) or text, in that order.
    A value the number reader takes is not counted as a date: the date reader reads a bare five-digit amount as a
    spreadsheet serial day, which would give every trade-in or debt amount a date twin.
    """
    carried: list[Carried] = []
    for c in table.columns:
        if c.name in consumed or c.name.startswith("__"):
            continue
        grouped = dst.execute(f"SELECT src.{q(c.name)}, COUNT(*) FROM source.{q(table.name)} AS src "
                              "GROUP BY 1").fetchall()
        values = [(value, count) for value, count in grouped if not blank(value)]
        locale = infer_locale(value for value, _ in values)
        nonblank = numbers = dates = codes = long_numbers = 0
        for value, count in values:
            text = str(value).strip()
            nonblank += count
            if _number(value, locale or "unknown") is not None:
                numbers += count
                long_numbers += count if len(text) >= CODE_MIN_DIGITS else 0
            elif to_date(value) is not None:
                dates += count
            codes += count if is_code_text(text) else 0
        numeric = nonblank > 0 and numbers >= CARRY_MIN_SHARE * nonblank
        dated = nonblank > 0 and dates >= CARRY_MIN_SHARE * nonblank
        code = nonblank > 0 and codes >= CARRY_MIN_SHARE * nonblank
        # a column of long digit strings is an identifier (an IMEI, a phone number), not an amount
        identifier = nonblank > 0 and long_numbers >= CARRY_MIN_SHARE * nonblank
        value_type = ("date" if dated else ("code" if identifier else "number") if numeric
                      else "code" if code else "text")
        carried.append(Carried(table.name, c.name, c.original_name, _role_of(c.name, given), value_type,
                               numeric, dated, locale))
    return carried


def _carry_columns(c: Carried, plain: bool = False) -> list[tuple[str, str, str]]:
    """(column name, value expression over src, SQL type) of a carried column: x_<name> text, then the twins.
    A plain copy (plain=True, for src_ tables) keeps the source name for the text column."""
    raw = f"src.{q(c.source)}"
    out = [(c.source if plain else f"x_{c.source}", _text(c.source), "TEXT")]
    if c.numeric:
        out.append((f"xn_{c.source}", f"{_parse_fn('xn', c.locale)}({raw})", "REAL"))
    if c.dated:
        out.append((f"xd_{c.source}", f"to_date({raw})", "TEXT"))
    return out


def _extra_columns(dst: sqlite3.Connection, target: str) -> list[tuple[str, str]]:
    """(name, type) of the carried columns a layer table holds."""
    return [(row[1], row[2]) for row in dst.execute(f"PRAGMA table_info({q(target)})")
            if row[1].startswith(EXTRA_PREFIXES)]


def _consumed(mapping: dict[str, str], read_roles: Iterable[str]) -> set[str]:
    """The source columns that a loader reads: the column each role it reads maps to."""
    return {mapping[role] for role in read_roles if mapping.get(role)}


def _ensure_columns(dst: sqlite3.Connection, target: str, carried: list[Carried]) -> None:
    existing = {row[1] for row in dst.execute(f"PRAGMA table_info({q(target)})")}
    for c in carried:
        for name, _, sql_type in _carry_columns(c):
            if name not in existing:
                dst.execute(f"ALTER TABLE {q(target)} ADD COLUMN {q(name)} {sql_type}")
                existing.add(name)


def _extras_for(dst: sqlite3.Connection, table: TableMeta, given: dict[str, Any], consumed: set[str], target: str,
                scale: int, report: dict[str, Any], carry_log: list[tuple[str, list[Carried]]]) -> list[Carried]:
    """Plan the columns of table that target keeps, add them to target, and log them for the column catalog.
    A table's money scale is not applied to its numeric twins (no canonical field reads them); they are listed."""
    carried = _carry_plan(dst, table, consumed, given)
    _ensure_columns(dst, target, carried)
    if carried:
        carry_log.append((target, carried))
    if scale != 1:
        report["extra_numbers_not_scaled"] += [f"{table.name}.xn_{c.source}" for c in carried if c.numeric]
    return carried


def _insert(dst: sqlite3.Connection, target: str, table: TableMeta, roles: dict[str, str],
            texts: dict[str, str], numbers: dict[str, str], synthetic_prefix: str | None = None, *,
            origin: str | None = None, report: dict[str, Any] | None = None,
            defaults: dict[str, str] | None = None, stats: Counter | None = None, scale: int = 1,
            carried: list[Carried] | None = None) -> int:
    """Insert rows of a source table (or of origin, a prepared copy of it) into target.

    scale multiplies the table's money fields (every numeric field that is not a quantity). carried adds the
    table's unmapped columns (see _carry_plan), unscaled."""
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
        expr = f"{_parse_fn(field, locale)}(src.{q(column)})"
        if scale != 1 and field not in QUANTITY_FIELDS:
            expr = f"{expr}*{int(scale)}"
        exprs.append(expr)
        if stats is not None:
            _count_cells(dst, source, field, column, locale, stats, counted)
    for c in carried or ():
        for name, expr, _ in _carry_columns(c):
            names.append(name)
            exprs.append(expr)
    where = ""
    if target == "payments":
        # A method/label alone is not a payment event. Keep zero amounts and
        # unallocated receipts, but do not turn export annotations into payments.
        event = [exprs[names.index(field)] for field in ("order_id", "date", "amount")]
        where = " WHERE " + " OR ".join(f"({expr}) IS NOT NULL" for expr in event)
    cursor = dst.execute(
        f"INSERT INTO {target} ({','.join(names)}) SELECT {','.join(exprs)} FROM {source} AS src{where}"
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
                origin: str | None = None, defaults: dict[str, str] | None = None, scale: int = 1,
                carried: list[Carried] | None = None) -> int:
    rows = _insert(dst, "raw_lines", table, roles, LINE_FIELDS, LINE_NUMBERS, synthetic_prefix,
                   origin=origin, report=report, defaults=defaults, stats=stats, scale=scale, carried=carried)
    dst.execute("UPDATE raw_lines SET has_product=? WHERE has_product IS NULL", (has_product,))
    return rows


def _load_sales(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str], synthetic: str,
                has_product: int, report: dict[str, Any], stats: Counter, *, scale: int = 1,
                combined: bool = False, carried: list[Carried] | None = None) -> None:
    """A sales table's lines. Rows are first copied and cleaned when needed (see _sales_source). With combined,
    a line whose product cell joins an item and its toppings is split (see _split_combined)."""
    start = dst.execute("SELECT COALESCE(MAX(rowid), 0) FROM raw_lines").fetchone()[0]
    origin = _sales_source(dst, table, roles, report, stats)
    _load_lines(dst, table, roles, synthetic, has_product, report, stats, origin=origin, scale=scale,
                carried=carried)
    if origin:
        dst.execute("DROP TABLE temp.order_source")
    if combined:
        _split_combined(dst, start, scale, stats)


def _split_combined(dst: sqlite3.Connection, start: int, scale: int, stats: Counter) -> None:
    """Split the sales lines loaded after rowid start whose product cell has the separator (rulebook section 6).

    Each topping becomes its own line with the same order id: quantity is its count times the line quantity, and
    its amount is its price times that quantity. The main item keeps the remaining amount. A line is split only
    when every topping has a price, the line has a quantity and an amount, and the remaining amount is not negative;
    otherwise it stays whole and is counted as unsplit.
    """
    columns = [row[1] for row in dst.execute("PRAGMA table_info(raw_lines)")]
    cursor = dst.execute(
        "SELECT rowid AS __rid, * FROM raw_lines WHERE rowid > ? AND has_product = 1 "
        "AND instr(product_name, ?) > 0 ORDER BY rowid",
        (start, COMBINED_SEPARATOR),
    )
    names = [description[0] for description in cursor.description]
    for record in cursor.fetchall():
        line = dict(zip(names, record))
        split = _combined_parts(line, scale)
        if split is None:
            stats["combined_items_unsplit"] += 1
            continue
        main_name, main_amount, toppings = split
        dst.execute("UPDATE raw_lines SET product_name=?, line_amount=? WHERE rowid=?",
                    (main_name, main_amount, line["__rid"]))
        for label, quantity, amount, price in toppings:
            row = {column: line[column] for column in columns}
            row.update(product_name=label, product_id=None, qty=quantity, qty_base=None, line_amount=amount,
                       unit_price=price, line_discount=None, line_cost=None, category=None, line_kind=None,
                       has_product=1)
            dst.execute(f"INSERT INTO raw_lines ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                        [row[column] for column in columns])
        stats["combined_items_split"] += 1


def _combined_parts(line: dict[str, Any], scale: int) -> tuple[str, float, list[tuple[str, float, float, float]]] | None:
    """The main item's name and amount, and each topping as (name, quantity, amount, unit price); None when the
    line must stay whole."""
    pieces = line["product_name"].split(COMBINED_SEPARATOR)
    main = pieces[0].strip()
    qty, amount = line["qty"], line["line_amount"]
    if amount is None and qty is not None and line["unit_price"] is not None:
        amount = qty * line["unit_price"] - (line["line_discount"] or 0)
    if not main or qty is None or amount is None:
        return None
    toppings = []
    for piece in pieces[1:]:
        match = TOPPING_PART.fullmatch(piece)
        if match is None or not match[2].strip() or match[3] is None:
            return None  # no price, or no name
        price = _number(match[3], "unknown")
        if price is None:
            return None
        count = int(match[1]) if match[1] else 1
        price *= scale  # the price is in the sheet's money units, as the line amount is
        quantity = count * qty
        toppings.append((match[2].strip(), quantity, price * quantity, price))
    remainder = amount - sum(topping_amount for _, _, topping_amount, _ in toppings)
    if remainder < 0:
        return None  # the prices do not fit inside the line amount
    return main, remainder, toppings


def _combined_convention(readme: str) -> bool:
    """True when the README states in one sentence that a plus sign joins an item to its toppings.

    The check runs on an ASCII-folded copy: lower case, combining marks removed, other non-ASCII characters dropped."""
    folded = unicodedata.normalize("NFKD", readme.lower())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    folded = folded.encode("ascii", "ignore").decode("ascii")
    return any("+" in sentence and COMBINED_TOPPING_WORD.search(sentence)
               for sentence in SENTENCE_END.split(folded))


def _amount_scale(context: BusinessContext, table: TableMeta) -> int:
    """The integer an AMOUNT_SCALE ingestion diagnostic gives for this sheet (its source path and table), or 1.

    Two different factors for one sheet are a conflict, and nothing is scaled."""
    paths = {source.path for source in table.sources}
    factors = set()
    for item in context.diagnostics:
        if item.code != AMOUNT_SCALE_CODE or item.source is None:
            continue
        if item.source.table != table.name or item.source.path not in paths:
            continue
        text = item.message.strip()
        if re.fullmatch(r"[1-9]\d*", text):
            factors.add(int(text))
    return factors.pop() if len(factors) == 1 else 1


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
    """Category, base unit and catalog unit cost (per base unit) per product, from a product catalog or a unit
    conversion table."""
    if not (roles.get("product_id") or roles.get("product_name")):
        return
    if not roles.get("base_unit") and roles.get("unit") and (
            not roles.get("conversion_factor") or _text_factor_column(dst, table, roles)):
        # A product list with one unit column and no conversion (or whose conversion cells are statements such as
        # "1 <large> = <n> <unit>"): that unit is the unit the product is counted in.
        roles = {**roles, "base_unit": roles["unit"]}
    if not (roles.get("product_category") or roles.get("base_unit") or roles.get("catalog_cost")):
        return
    cost = f"to_number({_text(roles['catalog_cost'])})" if roles.get("catalog_cost") else "NULL"
    dst.execute(
        "INSERT INTO temp.product_facts (product_id, name_key, product_name, category, base_unit, unit_cost) "
        f"SELECT pid, NULLIF(lookup(nm), ''), nm, cat, base, cost FROM (SELECT {_text(roles.get('product_id'))} AS pid, "
        f"{_text(roles.get('product_name'))} AS nm, {_text(roles.get('product_category'))} AS cat, "
        f"{_text(roles.get('base_unit'))} AS base, {cost} AS cost FROM source.{q(table.name)} AS src) "
        "WHERE pid IS NOT NULL OR nm IS NOT NULL"
    )


def _load_expenses(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str], scale: int = 1) -> None:
    """Operating expense ledger rows (rulebook §5, lãi ròng): date and amount, with the category when written."""
    if not (roles.get("expense_date") and roles.get("expense_amount")):
        return
    dst.execute(
        "INSERT INTO expenses (date, amount, category) "
        f"SELECT to_date({_text(roles['expense_date'])}), to_number({_text(roles['expense_amount'])})*{int(scale)}, "
        f"{_text(roles.get('expense_category'))} FROM source.{q(table.name)} AS src "
        f"WHERE to_date({_text(roles['expense_date'])}) IS NOT NULL"
    )


def _load_price_changes(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str], scale: int = 1) -> None:
    """Price list rows: the day a product's sale price took effect, with the new and, when written, the old price."""
    if not (roles.get("price_effective_at") and roles.get("new_price")):
        return
    if not (roles.get("product_id") or roles.get("product_name")):
        return
    old = f"to_number({_text(roles['old_price'])})*{int(scale)}" if roles.get("old_price") else "NULL"
    dst.execute(
        "INSERT INTO price_changes (product_id, product_name, name_key, date, old_price, new_price) "
        f"SELECT {_text(roles.get('product_id'))}, {_text(roles.get('product_name'))}, "
        f"NULLIF(lookup({_text(roles.get('product_name'))}), ''), to_date({_text(roles['price_effective_at'])}), "
        f"{old}, to_number({_text(roles['new_price'])})*{int(scale)} FROM source.{q(table.name)} AS src "
        f"WHERE to_date({_text(roles['price_effective_at'])}) IS NOT NULL"
    )


# A conversion cell that names the unit its number counts in: "<number> <unit>" ("15 vỉ"), the unit being another
# unit of the same product rather than its base unit.
FACTOR_WITH_UNIT = re.compile(r"\s*(?P<number>\d[\d.,]*)\s*(?P<unit>[^\W\d_][^\d]*?)\s*")


class _FactorRow(NamedTuple):
    unit: str  # normalized unit the row converts
    cell: Any  # the factor cell as written
    bases: frozenset[str]  # normalized base units the row's product is counted in


def _factor_cell(cell: Any, locale: str | None) -> tuple[float, str | None] | None:
    """A factor cell as (number, unit key it counts in); the unit is None for a number alone. None when the cell
    gives no number or names its unit in a shape other than "<number> <unit>"."""
    if isinstance(cell, str):
        match = FACTOR_WITH_UNIT.fullmatch(cell)
        if match:
            number = _number(match.group("number"), locale, unit=None) if locale else to_quantity(match.group("number"))
            companion = normalize_lookup(match.group("unit"))
            return (number, companion) if number is not None and companion else None
    if cell is None:
        return None
    number = _number(cell, locale, unit=None) if locale else to_quantity(cell)
    return None if number is None else (number, None)


def _chain_factors(rows: list[_FactorRow], locale: str | None,
                   packing_units: frozenset[str] = frozenset()) -> list[float | None]:
    """Base units per unit for each conversion row of one product (None where the layer will not guess).

    A cell of a number alone counts base units. A cell "<number> <unit>" counts that unit: when it is a base unit
    the number stands; when it is another unit with a conversion row of the same product the number is multiplied
    by that unit's own factor, at any depth. A unit with no row, a loop, or a cell with no number gives no factor."""
    parsed = [_factor_cell(row.cell, locale) for row in rows]
    first: dict[str, int] = {}
    for index, row in enumerate(rows):
        if parsed[index] is not None:
            first.setdefault(row.unit, index)  # the first row of a unit wins, like every other reader of factors
    bases = frozenset().union(*(row.bases for row in rows)) if rows else frozenset()
    done: dict[int, float | None] = {}

    def resolve(index: int, visiting: tuple[int, ...]) -> float | None:
        if index in done:
            return done[index]
        number, companion = parsed[index]  # type: ignore[misc]
        value: float | None
        if companion is None or companion in bases or (companion not in first and companion not in packing_units):
            # A number alone, a number in the base unit, or a word the table never uses as a packing unit: the
            # number counts base units, as before units were read from the cell.
            value = number
        elif companion not in first:
            value = None  # a packing unit of other products only: this product's count in it is unknown
        elif first[companion] not in visiting and first[companion] != index:
            inner = resolve(first[companion], (*visiting, index))
            value = None if inner is None else number * inner
        else:
            value = None  # a loop between the product's own conversion rows
        done[index] = value
        return value

    return [None if parsed[index] is None else resolve(index, ()) for index in range(len(rows))]


def _load_unit_factors(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str]) -> None:
    """Base units per large unit, for each product that a conversion or catalog row gives one for.

    A factor cell may name the unit it counts in ("15 vỉ"): see _chain_factors."""
    if not (roles.get("product_id") or roles.get("product_name")):
        return
    if not (roles.get("large_unit") and roles.get("conversion_factor")):
        return
    column = roles["conversion_factor"]
    locale = _column_locale(dst, table, column)  # a ratio of quantities, not money
    base = f"lookup({_text(roles['base_unit'])})" if roles.get("base_unit") else "NULL"
    groups: dict[str, list[tuple[str | None, str | None, _FactorRow]]] = {}
    for pid, name, unit_key, cell, base_key in dst.execute(
            f"SELECT {_text(roles.get('product_id'))}, {_text(roles.get('product_name'))}, "
            f"NULLIF(lookup({_text(roles['large_unit'])}), ''), src.{q(column)}, {base} "
            f"FROM source.{q(table.name)} AS src"):
        name_key = normalize_lookup(name) if name else None
        if unit_key is None or not (pid or name_key):
            continue
        facts = dst.execute("SELECT base_unit FROM temp.product_facts WHERE base_unit IS NOT NULL AND "
                            + ("product_id=?" if pid else "name_key=?"), (pid or name_key,)).fetchall()
        bases = frozenset({base_key} if base_key else ()) | {normalize_lookup(row[0]) for row in facts}
        groups.setdefault(f"i:{pid}" if pid else f"n:{name_key}", []).append(
            (pid, name_key, _FactorRow(unit_key, cell, bases)))
    inserts: list[tuple[str | None, str | None, str, float]] = []
    packing = frozenset(m[2].unit for members in groups.values() for m in members)
    for members in groups.values():
        for (pid, name_key, row), factor in zip(members, _chain_factors([m[2] for m in members], locale, packing)):
            if factor is not None:
                inserts.append((pid, name_key, row.unit, factor))
    dst.executemany("INSERT INTO temp.unit_factors (product_id, name_key, large_key, factor) VALUES (?,?,?,?)", inserts)


def _row_factor_statements(cell: Any) -> list[tuple[str, float, str]]:
    """The (large unit, count, small unit) statements of one catalog conversion cell, units normalized."""
    if not isinstance(cell, str):
        return []
    found = []
    for match in ROW_FACTOR_STATEMENT.finditer(cell):
        large, small = normalize_lookup(match.group("large")), normalize_lookup(match.group("small"))
        if (not large or not small or large == small or {large, small} & UNIT_NON_QUANTITY_WORDS
                or _GROUPED_THOUSANDS.match(match.group("n"))):
            continue
        count = float(match.group("n").replace(",", "."))
        if count > 0:
            found.append((large, count, small))
    return found


def _text_factor_column(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str]) -> bool:
    """True when the table's conversion column holds statements of the shape "1 <unit> = <n> <unit>" (or nothing at
    all): the table's unit column is then the unit each product is counted in, the statements go from larger units to
    it. A column of plain numbers says nothing of the kind."""
    column = roles.get("conversion_factor")
    if not column:
        return False
    try:
        cells = dst.execute(f"SELECT {_text(column)} FROM source.{q(table.name)} AS src").fetchall()
    except sqlite3.Error:
        return False
    filled = [cell[0] for cell in cells if cell[0] is not None]
    return not filled or any(_row_factor_statements(cell) for cell in filled)


def _load_text_row_factors(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str]) -> int:
    """Conversions a catalog states per row, in a text cell "1 <large> = <n> <unit>[; ...]" (rulebook: the factor
    belongs to the product of the row). The unit the count is in must be the row's own base unit (the unit column) or
    another unit the same cell converts; a statement that ends in any other unit is left out. Returns factors added."""
    if roles.get("large_unit") or not roles.get("conversion_factor"):
        return 0
    if not (roles.get("product_id") or roles.get("product_name")):
        return 0
    base_column = roles.get("base_unit") or roles.get("unit")
    if not base_column:
        return 0
    added = 0
    inserts: list[tuple[str | None, str | None, str, float]] = []
    for pid, name, base, cell in dst.execute(
            f"SELECT {_text(roles.get('product_id'))}, {_text(roles.get('product_name'))}, {_text(base_column)}, "
            f"{_text(roles['conversion_factor'])} FROM source.{q(table.name)} AS src"):
        statements = _row_factor_statements(cell)
        base_key = normalize_lookup(base) if base else ""
        name_key = normalize_lookup(name) if name else None
        if not statements or not base_key or not (pid or name_key):
            continue
        counts: dict[str, list[tuple[float, str]]] = {}
        for large, count, small in statements:
            counts.setdefault(large, []).append((count, small))

        def resolve(unit: str, seen: tuple[str, ...]) -> float | None:
            if unit == base_key:
                return 1.0
            options = counts.get(unit)
            if not options or unit in seen:
                return None
            values = {count * inner for count, small in options
                      if (inner := resolve(small, (*seen, unit))) is not None}
            return values.pop() if len(values) == 1 else None

        for large in counts:
            factor = resolve(large, ())
            if factor is not None and factor > 0 and large != base_key:
                inserts.append((pid, name_key, large, factor))
                added += 1
    dst.executemany("INSERT INTO temp.unit_factors (product_id, name_key, large_key, factor) VALUES (?,?,?,?)", inserts)
    return added


def _load_stock_table(dst: sqlite3.Connection, table: TableMeta, note: dict[str, Any], roles: dict[str, str]) -> int:
    """Load a table of kind inventory as a stock report. Only a snapshot with exactly one quantity column and no date
    column qualifies: a movement ledger carries a running balance this layer does not read, so it is left out.
    Lot tables are not loaded here. Returns the number of stock rows loaded."""
    if note["kind"] != "inventory":
        return 0
    given, _ = _given_by_column(table, note.get("roles") or {})
    quantities = [column for column, role in given.items() if role == "inventory_qty"]
    dated = [column for column, role in given.items() if role == "inventory_date"]
    if len(quantities) != 1 or dated:
        _load_stock_ledger(dst, table, roles)
        return 0
    return _load_stock(dst, table, roles, quantities[0])


LEDGER_MIN_SHARE = 0.99  # share of a product's consecutive rows that must follow the running balance
LEDGER_MIN_DATE_SHARE = 0.95  # share of a column's values that must be dates for it to be the movement time


def _timestamp_text(value: Any) -> str | None:
    result = parse_date(value)
    return str(result.value)[:19] if result.status == ParseStatus.OK else None


LEDGER_SAMPLE = 300  # rows read to find the time column before the whole ledger is parsed


def _ledger_reading(rows: list[tuple[Any, ...]], columns: list[str], product: str) -> tuple[str, str, str] | None:
    """(time column, balance column, quantity column) of a movement ledger, from its values, or None.

    The time column is the one column whose values are dates (with a time of day). The balance column is the numeric
    column that, product by product in time order, changes by exactly the amount another column gives, in nearly
    every row. Two columns that fit the same role, or none, is a doubt and gives None."""
    index = {name: i for i, name in enumerate(columns)}
    step = max(1, len(rows) // LEDGER_SAMPLE)
    sample = rows[::step]
    times = [c for c in columns if c != product and any(row[index[c]] is not None for row in sample)
             and sum(_timestamp_text(row[index[c]]) is not None for row in sample) >= LEDGER_MIN_DATE_SHARE * len(sample)]
    if len(times) != 1:
        return None
    when = times[0]
    moments = [_timestamp_text(row[index[when]]) for row in rows]
    parsed: dict[str, list[float | None]] = {}
    for c in columns:
        if c in (product, when):
            continue
        values = [to_quantity(row[index[c]]) if row[index[c]] is not None and str(row[index[c]]).strip() else None
                  for row in rows]
        if all(value is not None or row[index[c]] is None or not str(row[index[c]]).strip()
               for value, row in zip(values, rows)):
            parsed[c] = values
    order = sorted((i for i, row in enumerate(rows) if row[index[product]] is not None and moments[i] is not None),
                   key=lambda i: (str(rows[i][index[product]]), moments[i], i))
    fits: list[tuple[str, str]] = []
    for balance, series in parsed.items():
        for quantity, change in parsed.items():
            if quantity == balance:
                continue
            checked = followed = 0
            previous: int | None = None
            for i in order:
                if previous is not None and rows[previous][index[product]] == rows[i][index[product]] \
                        and series[i] is not None and series[previous] is not None and change[i] is not None:
                    checked += 1
                    followed += abs(abs(series[i] - series[previous]) - abs(change[i])) < 1e-9
                previous = i
            if checked and followed >= LEDGER_MIN_SHARE * checked:
                fits.append((balance, quantity))
    balances = {balance for balance, _ in fits}
    if len(balances) != 1:
        return None
    balance = next(iter(balances))
    return when, balance, next(quantity for b, quantity in fits if b == balance)


def _load_stock_ledger(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str]) -> int:
    """A stock movement ledger with a running balance per row: its rows go to temp.ledger_rows for _build_ledger.
    Needs a product column and the evidence of _ledger_reading. Returns the rows kept."""
    product = roles.get("product_id") or roles.get("product_name")
    if not product:
        return 0
    columns = [c.name for c in table.columns]
    rows = dst.execute("SELECT " + ", ".join(f"src.{q(c)}" for c in columns)
                       + f" FROM source.{q(table.name)} AS src ORDER BY src.rowid").fetchall()
    if not rows:
        return 0
    reading = _ledger_reading(rows, columns, product)
    if reading is None:
        return 0
    when, balance, _ = reading
    kept = 0
    for row in rows:
        key, moment, value = row[columns.index(product)], _timestamp_text(row[columns.index(when)]), \
            to_quantity(row[columns.index(balance)])
        if key is None or moment is None or value is None:
            continue
        dst.execute("INSERT INTO temp.ledger_rows VALUES (?,?,?,?,?)",
                    (str(key).strip(), 1 if roles.get("product_id") == product else 0, moment, value, kept))
        kept += 1
    return kept


def _build_ledger(dst: sqlite3.Connection) -> bool:
    """The stock_ledger table (name_key, product_name, product_id, ts, balance: the balance after each movement),
    and, when no stock report was read, the stock report at the report's date from each product's last movement.
    A movement of a product the catalogs do not name leaves the whole ledger unbuilt. Returns whether it was built."""
    dst.execute("CREATE TABLE stock_ledger (name_key TEXT, product_name TEXT, product_id TEXT, ts TEXT, balance REAL)")
    if not dst.execute("SELECT 1 FROM temp.ledger_rows LIMIT 1").fetchone():
        return False
    facts = dst.execute("SELECT product_id, name_key, product_name FROM temp.product_facts "
                        "WHERE product_name IS NOT NULL").fetchall()
    by_id = {row[0]: row for row in facts if row[0] is not None}
    by_name = {row[1]: row for row in facts if row[1] is not None}
    rows = []
    for key, is_id, moment, balance, order in dst.execute(
            "SELECT product_key, is_id, ts, balance, seq FROM temp.ledger_rows ORDER BY product_key, ts, seq"):
        found = by_id.get(key) if is_id else by_name.get(normalize_lookup(key))
        if found is None:
            return False
        rows.append((found[1], found[2], found[0], moment, balance))
    dst.executemany("INSERT INTO stock_ledger VALUES (?,?,?,?,?)", rows)
    if not dst.execute("SELECT 1 FROM temp.stock_rows LIMIT 1").fetchone():
        dst.execute("INSERT INTO temp.stock_rows (product_id, name_key, product_name, unit, qty) "
                    "SELECT l.product_id, l.name_key, l.product_name, NULL, l.balance FROM stock_ledger l "
                    "WHERE NOT EXISTS (SELECT 1 FROM stock_ledger n WHERE n.name_key = l.name_key AND n.rowid > l.rowid)")
    return True


def _load_stock(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str], qty_column: str) -> int:
    """Stock on hand per product from a stock report: one quantity in base units, no date column. Rows without a
    product name are left out. Returns the number of rows loaded."""
    name = roles.get("product_name")
    if not name:
        return 0
    locale = _column_locale(dst, table, qty_column)
    factor = f"{_parse_fn('qty', locale)}(src.{q(qty_column)})"  # a count of units, not money
    cursor = dst.execute(
        "INSERT INTO temp.stock_rows (product_id, name_key, product_name, unit, qty) "
        f"SELECT pid, lookup(nm), nm, unit, qty FROM (SELECT {_text(roles.get('product_id'))} AS pid, "
        f"{_text(name)} AS nm, {_text(roles.get('unit'))} AS unit, {factor} AS qty "
        f"FROM source.{q(table.name)} AS src) WHERE nm IS NOT NULL AND qty IS NOT NULL"
    )
    return cursor.rowcount


def _single_value(dst: sqlite3.Connection, sql: str, product_id: str | None, name_key: str | None) -> float | None:
    """The one value a product's facts give: matched by product id when it has one, else by normalized name.
    None when the facts give no value or two different values."""
    for key, column in ((product_id, "product_id"), (name_key, "name_key")):
        if key is None:
            continue
        values = {row[0] for row in dst.execute(sql.format(column=column), (key,)) if row[0] is not None}
        if values:
            return values.pop() if len(values) == 1 else None
    return None


def _build_stock(dst: sqlite3.Connection) -> int:
    """The stock table: one row per product of a stock report, valued at the report's date.

    The report's date is the last day with dated orders or receipts (rulebook section 2: the balance at the end of
    the last day with data). A product's unit cost is its catalog cost per base unit; its large-unit factor is the
    base units in one large unit. A product name that two rows share is kept twice: the compiler refuses any answer
    that would count it once or twice. Returns the number of rows written.
    """
    snapshot = dst.execute(
        "SELECT MAX(d) FROM (SELECT MAX(date) AS d FROM orders UNION ALL SELECT MAX(date) FROM purchases)"
    ).fetchone()[0]
    received = {row[0] for row in dst.execute(
        "SELECT DISTINCT lookup(product_name) FROM purchases WHERE product_name IS NOT NULL")}
    rows = dst.execute("SELECT product_id, name_key, product_name, unit, qty FROM temp.stock_rows").fetchall()
    dst.execute("CREATE TABLE stock (name_key TEXT, product_name TEXT, unit TEXT, qty_base REAL, unit_cost REAL, "
                "large_factor REAL, received INTEGER, stock_date TEXT)")
    written = 0
    for product_id, name_key, product_name, unit, qty in rows:
        cost = _single_value(dst, "SELECT DISTINCT unit_cost FROM temp.product_facts "
                             "WHERE unit_cost IS NOT NULL AND {column}=?", product_id, name_key)
        factor = _single_value(dst, "SELECT DISTINCT factor FROM temp.unit_factors "
                               "WHERE factor IS NOT NULL AND {column}=?", product_id, name_key)
        dst.execute("INSERT INTO stock VALUES (?,?,?,?,?,?,?,?)",
                    (name_key, product_name, unit, qty, cost, factor, int(name_key in received), snapshot))
        written += 1
    return written


def _qty_expr(dst: sqlite3.Connection, table: TableMeta, column: str) -> str:
    """A quantity column read as a count of units (no money unit)."""
    return f"{_parse_fn('qty', _column_locale(dst, table, column))}(src.{q(column)})"


def _load_lot_receipts(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str], qty_role: str,
                       scale: int = 1) -> int:
    """One row per lot received: its lot id, product, expiry date and quantity in base units.

    The row's purchase cost is kept with what it is counted per: a row that gives its quantity in base units
    gives the cost per base unit ('base'); a purchase line gives it per its own unit ('unit'), which
    _build_lot_stock converts only when the unit's factor is known."""
    lot, expiry = roles.get("lot_id"), roles.get("expires_at")
    product_id, product_name = roles.get("product_id"), roles.get("product_name")
    if not (lot and expiry and (product_id or product_name)):
        return 0
    cost_column = roles.get("purchase_unit_cost")
    if cost_column:
        money = _parse_fn("unit_cost", _column_locale(dst, table, cost_column))
        cost = f"{money}(src.{q(cost_column)})*{int(scale)}"
    else:
        cost = "NULL"
    if qty_role == "qty_purchased_base":
        cost_unit, basis = "NULL", "'base'"
    else:
        cost_unit, basis = _text(roles.get("unit")), "'unit'"
    cursor = dst.execute(
        "INSERT INTO temp.lot_receipts (lot_id, product_id, product_name, expires_at, qty, cost, cost_unit, cost_basis) "
        f"SELECT lid, pid, nm, to_date(exp), qty, cst, lookup(cu), CASE WHEN cst IS NULL THEN NULL ELSE bs END "
        f"FROM (SELECT {_text(lot)} AS lid, {_text(product_id)} AS pid, "
        f"{_text(product_name)} AS nm, {_text(expiry)} AS exp, {_qty_expr(dst, table, roles[qty_role])} AS qty, "
        f"{cost} AS cst, {cost_unit} AS cu, {basis} AS bs "
        f"FROM source.{q(table.name)} AS src) WHERE lid IS NOT NULL AND (pid IS NOT NULL OR nm IS NOT NULL) "
        "AND qty IS NOT NULL"
    )
    if cursor.rowcount:
        dst.execute("INSERT INTO temp.lot_sources VALUES ('receipts')")
    return cursor.rowcount


def _load_lot_sales(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str]) -> int:
    """Quantity sold from each lot on each order, and the quantity the customer returned to that lot."""
    order, lot, sold = roles.get("order_id"), roles.get("lot_id"), roles.get("qty_sold_base")
    if not (lot and sold):
        return 0
    returned = f"COALESCE({_qty_expr(dst, table, roles['qty_returned'])}, 0)" if roles.get("qty_returned") else "0"
    cursor = dst.execute(
        "INSERT INTO temp.lot_sales (order_id, lot_id, sold, returned) "
        f"SELECT oid, lid, sold, ret FROM (SELECT {_text(order)} AS oid, {_text(lot)} AS lid, "
        f"{_qty_expr(dst, table, sold)} AS sold, {returned} AS ret FROM source.{q(table.name)} AS src) "
        "WHERE lid IS NOT NULL AND sold IS NOT NULL"
    )
    if cursor.rowcount:
        dst.execute("INSERT INTO temp.lot_sources VALUES ('sales')")
    return cursor.rowcount


LOT_SALES_MIN_SHARE = 0.95  # share of a column's distinct values that must be known lot ids
LOT_SALES_MIN_ORDER_SHARE = 0.9  # share of a column's distinct values that must be known order ids
LOT_SALES_KINDS = ("sales", "other")  # annotation kinds a table of sales from lots may have been given


def _matching_share(dst: sqlite3.Connection, table: TableMeta, column: str,
                    known: list[tuple[TableMeta, str]]) -> float:
    """Share of a column's distinct non-blank values found in any of the known (table, column) value sets."""
    value = f"NULLIF(TRIM(CAST(src.{q(column)} AS TEXT)),'')"
    inside = " OR ".join(
        f"v IN (SELECT NULLIF(TRIM(CAST(k.{q(col)} AS TEXT)),'') FROM source.{q(other.name)} AS k)"
        for other, col in known)
    total, hit = dst.execute(
        f"SELECT COUNT(*), COALESCE(SUM({inside}), 0) FROM (SELECT DISTINCT {value} AS v "
        f"FROM source.{q(table.name)} AS src) WHERE v IS NOT NULL").fetchone()
    return hit / total if total else 0.0


def _is_ordinal(rows: list[tuple[Any, Any]]) -> bool:
    """A column that numbers the lines of each order: within every order its distinct values run 1, 2, ... without
    gaps, and some order has two. A column of quantities, including returns that are mostly 0, does not. A column
    counted from 0 is not recognised here, which leaves a doubt the caller resolves by not guessing."""
    per_order: dict[Any, set[float]] = {}
    for order, value in rows:
        per_order.setdefault(order, set()).add(value)
    if not any(len(values) >= 2 for values in per_order.values()):
        return False
    for values in per_order.values():
        ordered = sorted(values)
        if ordered != [1 + i for i in range(len(ordered))]:
            return False
    return True


def _lot_sales_reading(dst: sqlite3.Connection, table: TableMeta, note: dict[str, Any],
                       lot_columns: list[tuple[TableMeta, str]], order_columns: list[tuple[TableMeta, str]]
                       ) -> dict[str, str] | None:
    """The roles of a table of sales from lots, read from its values rather than from its column names, or None.

    Such a table has one column whose values are (nearly) all lot ids of the lot tables, one that joins the shop's
    orders, and the quantity sold from the lot; a second quantity that never exceeds the first is the quantity
    returned to the lot. It carries no product of its own, so it is never a table of sale lines. Any doubt (two
    columns that fit a role, a missing quantity) returns None and the table keeps its annotation."""
    given, _ = _given_by_column(table, note.get("roles") or {})
    if any(role in ("product_id", "product_name") for role in given.values()):
        return None
    names = [column.name for column in table.columns]
    lots = [c for c in names if _matching_share(dst, table, c, lot_columns) >= LOT_SALES_MIN_SHARE]
    if len(lots) != 1:
        return None
    orders = [c for c in names if c != lots[0]
              and _matching_share(dst, table, c, order_columns) >= LOT_SALES_MIN_ORDER_SHARE]
    if len(orders) != 1:
        return None
    lot, order = lots[0], orders[0]
    others = [c for c in names if c not in (lot, order)]
    cells = dst.execute("SELECT " + ", ".join(f"src.{q(c)}" for c in [order, *others])
                        + f" FROM source.{q(table.name)} AS src").fetchall()
    numeric: dict[str, list[float | None]] = {}
    for index, column in enumerate(others, 1):
        parsed = [to_quantity(row[index]) if row[index] is not None and str(row[index]).strip() else None
                  for row in cells]
        filled = [value for value in parsed if value is not None]
        present = sum(1 for row in cells if row[index] is not None and str(row[index]).strip())
        if filled and len(filled) >= CARRY_MIN_SHARE * present and min(filled) >= 0 \
                and not _is_ordinal([(row[0], value) for row, value in zip(cells, parsed) if value is not None]):
            numeric[column] = parsed
    quantity_roles = [c for c in numeric if given.get(c) in ("qty_base", "qty", "qty_sold_base")]
    if len(quantity_roles) == 1:
        sold = quantity_roles[0]
    elif not quantity_roles and len(numeric) == 1:
        sold = next(iter(numeric))
    else:  # roles that do not settle it (none, or two): the quantity no other column exceeds is the one sold
        sold = stock_sources.dominant_quantity(numeric, quantity_roles or list(numeric))
        if sold is None:
            return None
    returns = [c for c in numeric if c != sold and all(
        r is None or s is None or r <= s for r, s in zip(numeric[c], numeric[sold]))]
    if len([c for c in numeric if c != sold]) != len(returns) or len(returns) > 1:
        return None  # a quantity that is not a return amount: unclear what it is
    roles = {order: "order_id", lot: "lot_id", sold: "qty_sold_base"}
    if returns:
        roles[returns[0]] = "qty_returned"
    return roles


def _reread_lot_sales(dst: sqlite3.Connection, annotated: list[tuple[TableMeta, dict[str, Any]]]
                      ) -> list[tuple[TableMeta, dict[str, Any]]]:
    """Tables of sales from lots that the annotation took for sale lines (or left as other) become lot tables with
    the roles their values show (see _lot_sales_reading), so their rows are never counted as sales and the lot
    balances can be built. Needs lot ids from a lot or purchase table and orders from a sales table."""
    lot_columns: list[tuple[TableMeta, str]] = []
    order_columns: list[tuple[TableMeta, str]] = []
    for table, note in annotated:
        roles = effective_roles(table, note, dst)
        if note["kind"] in ("lot", "purchase") and roles.get("lot_id"):
            lot_columns.append((table, roles["lot_id"]))
        if note["kind"] == "sales" and roles.get("order_id") and (
                roles.get("product_name") or roles.get("product_id")
                or any(roles.get(role) for role in ORDER_SIGNAL_ROLES)):
            order_columns.append((table, roles["order_id"]))
    if not lot_columns or not order_columns:
        return annotated
    result = []
    for table, note in annotated:
        reading = None
        if note["kind"] in LOT_SALES_KINDS and (table, effective_roles(table, note, dst).get("order_id")) \
                not in order_columns:
            reading = _lot_sales_reading(dst, table, note, lot_columns,
                                         [pair for pair in order_columns if pair[0].name != table.name])
        result.append((table, note) if reading is None else (table, {**note, "kind": "lot", "roles": reading}))
    return result


def _load_lots(dst: sqlite3.Connection, table: TableMeta, note: dict[str, Any], roles: dict[str, str],
               scale: int = 1) -> None:
    """Lot tables give the lots received (a lot table with a purchased quantity, or a purchase line that names a lot)
    and the sales from lots (a lot table with a sold quantity)."""
    if note["kind"] == "lot" and roles.get("qty_sold_base"):
        _load_lot_sales(dst, table, roles)
        return
    for qty_role in ("qty_purchased_base", "qty_purchased"):
        if roles.get(qty_role):
            _load_lot_receipts(dst, table, roles, qty_role, scale)
            return


LOT_COST_TOLERANCE = 0.005  # relative spread within which two readings of one lot's cost are the same cost


def _cost_per_base_unit(dst: sqlite3.Connection, product: Any, cost: float, cost_unit: str | None,
                        basis: str | None) -> float | None:
    """A lot's purchase cost counted per base unit, or None when the unit it was written in has no known factor.
    product is the product_facts row (product_id, name_key, product_name, base_unit)."""
    if basis == "base":
        return float(cost)
    if basis != "unit" or not cost_unit:
        return None
    if product[3] is not None and normalize_lookup(product[3]) == cost_unit:
        return float(cost)
    factor = _single_value(
        dst, "SELECT DISTINCT factor FROM temp.unit_factors WHERE factor IS NOT NULL AND large_key="
        + _sql_text(cost_unit) + " AND {column}=?", product[0], product[1])
    return float(cost) / factor if factor else None


def _agreed_cost(readings: list[float | None]) -> float | None:
    """The one cost all known readings of a lot agree on. A reading whose unit has no known factor says nothing and
    is left out; None when no reading is known, one is not positive, or the known ones differ."""
    known = [r for r in readings if r is not None]
    if not known or any(r <= 0 for r in known):
        return None
    low, high = min(known), max(known)
    return known[0] if high - low <= LOT_COST_TOLERANCE * high else None


def _build_lot_stock(dst: sqlite3.Connection, write_stock: bool = True) -> int:
    """Stock from lots, for a shop with no stock report: each lot's balance is its received quantity less what was sold
    from it on orders not cancelled, plus what customers returned to it (rulebook §6, lots: a return goes back to the
    lot it came from). Products are the ones that received a lot; the stock table then holds them at the lot balances,
    valued at catalog cost like a stock report. A lot whose product or order cannot be placed leaves the whole lot
    stock unbuilt, so no answer rests on partial balances. Returns the number of products written."""
    dst.execute("CREATE TABLE lot_balance (name_key TEXT, product_name TEXT, product_id TEXT, lot_id TEXT, "
                "expires_at TEXT, balance REAL, unit_cost REAL)")
    sources = {row[0] for row in dst.execute("SELECT kind FROM temp.lot_sources")}
    if not {"receipts", "sales"} <= sources:
        return 0
    unplaced = dst.execute("SELECT COUNT(*) FROM temp.lot_sales s WHERE NOT EXISTS "
                           "(SELECT 1 FROM orders o WHERE o.order_id = s.order_id)").fetchone()[0]
    if unplaced:
        return 0
    facts = dst.execute("SELECT product_id, name_key, product_name, base_unit FROM temp.product_facts "
                        "WHERE product_name IS NOT NULL").fetchall()
    by_id = {row[0]: row for row in facts if row[0] is not None}
    by_name = {row[1]: row for row in facts if row[1] is not None}
    sold = {lot: (sold_qty or 0.0, returned or 0.0) for lot, sold_qty, returned in dst.execute(
        "SELECT s.lot_id, SUM(s.sold), SUM(s.returned) FROM temp.lot_sales s JOIN orders o ON o.order_id = s.order_id "
        "WHERE COALESCE(o.status, '') <> 'cancelled' GROUP BY s.lot_id")}
    lots: dict[tuple[str, str], tuple[str, str | None, str | None, float]] = {}
    costs: dict[tuple[str, str], list[float | None]] = {}
    for lot_id, product_id, product_name, expires_at, qty, cost, cost_unit, basis in dst.execute(
            "SELECT lot_id, product_id, product_name, expires_at, qty, cost, cost_unit, cost_basis "
            "FROM temp.lot_receipts"):
        row = by_id.get(product_id) if product_id is not None else None
        if row is None and product_name is not None:
            row = by_name.get(normalize_lookup(product_name))
        if row is None:
            return 0
        key = (str(lot_id), row[1])
        if key not in lots or qty > lots[key][3]:  # one line of a lot named by two tables counts once
            lots[key] = (row[1], row[0], expires_at, float(qty))
        if cost is not None:
            costs.setdefault(key, []).append(_cost_per_base_unit(dst, row, cost, cost_unit, basis))
    totals: dict[str, float] = {}
    rows_by_name: dict[str, Any] = {}
    for (lot_id, name_key), (_, product_id, expires_at, qty) in lots.items():
        sold_qty, returned = sold.get(lot_id, (0.0, 0.0))
        balance = qty - sold_qty + returned
        totals[name_key] = totals.get(name_key, 0.0) + balance
        rows_by_name.setdefault(name_key, (product_id, by_name[name_key][2], by_name[name_key][3]))
        dst.execute("INSERT INTO lot_balance VALUES (?,?,?,?,?,?,?)",
                    (name_key, by_name[name_key][2], product_id, lot_id, expires_at, balance,
                     _agreed_cost(costs.get((lot_id, name_key), []))))
    report_day = dst.execute(
        "SELECT MAX(d) FROM (SELECT MAX(date) AS d FROM orders UNION ALL SELECT MAX(date) FROM purchases)").fetchone()[0]
    for name_key, qty in (totals.items() if write_stock else ()):
        product_id, product_name, unit = rows_by_name[name_key]
        cost = _single_value(dst, "SELECT DISTINCT unit_cost FROM temp.product_facts "
                             "WHERE unit_cost IS NOT NULL AND {column}=?", product_id, name_key)
        factor = _single_value(dst, "SELECT DISTINCT factor FROM temp.unit_factors "
                               "WHERE factor IS NOT NULL AND {column}=?", product_id, name_key)
        dst.execute("INSERT INTO stock VALUES (?,?,?,?,?,?,?,?)",
                    (name_key, product_name, unit, qty, cost, factor, 1, report_day))
    dst.execute("INSERT INTO stock_sources VALUES (?,?)", ("lot_balance", len(lots)))
    return len(totals)


_UNIT_WORD = r"[^\W\d_]+"
_UNIT_COUNT = r"\d+(?:[.,]\d+)?"
_UNIT_LINK = "|".join(re.escape(w) for w in UNIT_STATEMENT_LINKERS)
_UNIT_EACH = "|".join(re.escape(w) for w in UNIT_EACH_WORDS)
_UNIT_CUE = "|".join(re.escape(w) for w in UNIT_PACKING_CUES)
# Each pattern captures large, n, base. A bare "X N Y" is read only right after a cue word, never inside a name.
_UNIT_STATEMENTS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    rf"(?<![\d.,])1\s+(?P<large>{_UNIT_WORD})\s*(?:{_UNIT_LINK})\s*(?P<n>{_UNIT_COUNT})\s+(?P<base>{_UNIT_WORD})",
    rf"(?<!\w)(?:{_UNIT_EACH})\s+(?P<large>{_UNIT_WORD})\s*(?:(?:{_UNIT_LINK})\s*)?(?P<n>{_UNIT_COUNT})\s+(?P<base>{_UNIT_WORD})",
    rf"(?<!\w)(?:{_UNIT_CUE})\s*:?\s*(?P<large>{_UNIT_WORD})\s*(?:(?:{_UNIT_LINK})\s*)?(?P<n>{_UNIT_COUNT})\s+(?P<base>{_UNIT_WORD})",
    rf"\|\s*1\s+(?P<large>{_UNIT_WORD})\s*\|\s*(?P<n>{_UNIT_COUNT})\s+(?P<base>{_UNIT_WORD})\s*(?:\||$)",
))
_GROUPED_THOUSANDS = re.compile(r"^\d{1,3}(?:[.,]\d{3})+$")

# One conversion a catalog cell states for its own row: "1 <large unit> = <n> <unit>"; a cell may hold several,
# separated by ";", "," or a line break. The units are words, the count a plain number.
ROW_FACTOR_STATEMENT = re.compile(
    rf"(?<![\d.,])1\s*(?P<large>{_UNIT_WORD})\s*(?:{_UNIT_LINK})\s*(?P<n>{_UNIT_COUNT})\s*(?P<small>{_UNIT_WORD})",
    re.IGNORECASE)


def _unit_statements(text: str) -> list[tuple[str, str, float, str]]:
    """Conversions written in words: (line, large unit, factor, base unit) for each, in order. A statement whose units
    are money or time, whose count is a grouped amount ("25.000"), or whose two units are the same, is not one."""
    found: list[tuple[str, str, float, str]] = []
    for line in (text or "").splitlines():
        for pattern in _UNIT_STATEMENTS:
            for match in pattern.finditer(line):
                raw = match.group("n")
                large, base = match.group("large"), match.group("base")
                keys = {normalize_lookup(large), normalize_lookup(base)}
                if (_GROUPED_THOUSANDS.match(raw) or len(keys) < 2 or keys & UNIT_NON_QUANTITY_WORDS
                        or "" in keys):
                    continue
                factor = float(raw.replace(",", "."))
                if factor > 0:
                    found.append((line, large, factor, base))
    return found


_COLUMN_DESCRIPTION = re.compile(r"^\s*(?:[-*+]\s*)?\*\*(?P<header>[^*]+)\*\*\s*:")


def _is_example_line(line: str) -> bool:
    """A line that gives its conversion as an example ("vd 1 thùng = 4 bịch"): a sample of the cell's shape, not a
    rule of the shop."""
    padded = " " + re.sub(r"[^a-z0-9]+", " ", normalize_lookup(line)) + " "
    return any(f" {re.sub('[^a-z0-9]+', ' ', normalize_lookup(cue)).strip()} " in padded for cue in EXAMPLE_CUES)


def _is_column_description(line: str, headers: frozenset[str]) -> bool:
    """A README line that describes one column of the data ("- **Quy đổi**: ..."): what it says about a unit
    conversion is the shape of that column's cells, and each row carries its own factor."""
    match = _COLUMN_DESCRIPTION.match(line)
    return bool(match and normalize_lookup(match.group("header")) in headers)


def _load_text_unit_factors(dst: sqlite3.Connection, text: str, headers: frozenset[str] = frozenset()) -> int:
    """Unit conversions a text (shop README or rulebook) states in words. A statement naming sold products gives them
    the factor; one naming none gives it to every product sold by the large unit whose base unit is the small unit or
    unknown (a product with another base unit is left alone). A factor the shop already has for a product and large
    unit stays; two statements of this text that disagree for one (product, large unit) give nothing. Returns the
    number of (product, unit) factors added. An example line and a line describing a column (`headers`, normalized
    source headers) state no rule of the shop. A product whose catalog gives its unit keeps it: the text never
    assigns a base unit to it."""
    statements = [item for item in _unit_statements(text)
                  if not _is_example_line(item[0]) and not _is_column_description(item[0], headers)]
    if not statements:
        return 0
    sold: dict[str, set[str]] = {}
    pids: dict[str, set[str]] = {}
    present = {row[1] for row in dst.execute("PRAGMA table_info(raw_lines)")}
    id_col, unit_col = ("product_id" if "product_id" in present else "NULL"), ("unit" if "unit" in present else "NULL")
    for name, pid, unit in dst.execute(
            f"SELECT DISTINCT product_name, {id_col}, {unit_col} FROM raw_lines WHERE product_name IS NOT NULL"):
        key = normalize_lookup(name)
        sold.setdefault(key, set()).add(normalize_lookup(unit) if unit else "")
        if pid is not None:
            pids.setdefault(key, set()).add(pid)
    base_by_id: dict[str, str] = {}
    base_by_name: dict[str, str] = {}
    for pid, name_key, base in dst.execute(
            "SELECT product_id, name_key, base_unit FROM temp.product_facts WHERE base_unit IS NOT NULL"):
        if pid is not None:
            base_by_id.setdefault(pid, normalize_lookup(base))
        if name_key is not None:
            base_by_name.setdefault(name_key, normalize_lookup(base))
    have_id: set[tuple[str, str]] = set()
    have_name: set[tuple[str, str]] = set()
    for pid, name_key, large in dst.execute("SELECT product_id, name_key, large_key FROM temp.unit_factors"):
        if pid is not None:
            have_id.add((pid, large))
        if name_key is not None:
            have_name.add((name_key, large))

    def known_base(key: str) -> str | None:
        for pid in pids.get(key, ()):
            if pid in base_by_id:
                return base_by_id[pid]
        return base_by_name.get(key)

    def has_factor(key: str, large: str) -> bool:
        return (key, large) in have_name or any((pid, large) in have_id for pid in pids.get(key, ()))

    proposals: dict[tuple[str, str], set[tuple[float, str, str]]] = {}
    for line, large_word, factor, base_word in statements:
        large, base = normalize_lookup(large_word), normalize_lookup(base_word)
        folded = f" {normalize_lookup(line)} "
        named = [key for key in sold if len(key) >= 6 and f" {key} " in folded]
        if named:
            keys = named
        else:
            keys = [key for key in sold if large in sold[key]
                    and (base in sold[key] or known_base(key) in (None, base))]
        for key in keys:
            proposals.setdefault((key, large), set()).add((factor, base, base_word))
    added = 0
    for (key, large), options in proposals.items():
        if len(options) != 1 or has_factor(key, large):
            continue
        factor, base, base_word = next(iter(options))
        dst.executemany(
            "INSERT INTO temp.unit_factors (product_id, name_key, large_key, factor) VALUES (NULL,?,?,?)",
            [(key, large, factor), (key, base, 1.0)])
        if known_base(key) is None:
            dst.execute("INSERT INTO temp.product_facts (product_id, name_key, category, base_unit, unit_cost) "
                        "VALUES (NULL,?,NULL,?,NULL)", (key, base_word))
        added += 1
    return added


def _load_readme_unit_factors(dst: sqlite3.Connection, readme: str, headers: frozenset[str] = frozenset()) -> int:
    """Unit conversions the shop README states in words ("… bán theo bó, 1 bó = 10 cây"). Returns the factors added."""
    return _load_text_unit_factors(dst, readme, headers)


def _derive_qty_base(dst: sqlite3.Connection) -> int:
    """Fill qty_base where a line has a quantity and none. Returns the number of lines filled.

    The sold unit is the base unit: the quantity stands. A packing unit with a factor: quantity times factor.
    A product with a known base unit that gives no factor for the sold unit stays NULL. A product with no
    base unit and no factor row at all sells in its base unit (rulebook section 10).
    Keep that identity branch's unit alongside its derived quantity. A supplied
    base quantity or an incomplete documented conversion does not identify a unit.
    """
    columns = {row[1] for row in dst.execute("PRAGMA table_info(raw_lines)")}
    if "_qty_base_identity_unit" not in columns:
        dst.execute("ALTER TABLE raw_lines ADD COLUMN _qty_base_identity_unit TEXT")
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
    identity_units: list[tuple[str, int]] = []
    sold_units: dict[str, set[str]] = {}  # a product's sold units, by id (else name): identity needs exactly one
    for pid, name, unit in dst.execute(
            "SELECT DISTINCT product_id, product_name, unit FROM raw_lines WHERE has_product=1"):
        unit_key = normalize_lookup(unit) if unit is not None else ""
        if unit_key:
            sold_units.setdefault(pid if pid is not None else normalize_lookup(name or ""), set()).add(unit_key)
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
        elif base is None and not factor_map and len(
                sold_units.get(pid if pid is not None else name_key, ())) == 1:
            updates.append((qty, rowid))
            identity_units.append((unit, rowid))
    dst.executemany("UPDATE raw_lines SET qty_base=? WHERE rowid=?", updates)
    dst.executemany("UPDATE raw_lines SET _qty_base_identity_unit=? WHERE rowid=?", identity_units)
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


ALIAS_MIN_OVERLAP = 0.5
ALIAS_MAX_KINDS = 10


def _promote_alias_tables(dst: sqlite3.Connection, annotated: list[tuple[TableMeta, dict[str, Any]]]) -> int:
    """A table the annotator left as 'other' that maps short names to full product names is an alias sheet.

    Found from the data, not from its wording: one column (the short name) has most of its values among the product
    names written on the sale lines, and another column is annotated product_name. A third column with few values
    is the product kind (e.g. new device, used device, accessory). Returns the number of tables read as aliases."""
    sold = {r[0] for r in dst.execute(
        "SELECT DISTINCT lookup(product_name) FROM raw_lines WHERE product_name IS NOT NULL")}
    found = 0
    for table, note in annotated:
        if note.get("kind") != "other":
            continue
        by_column = note.get("roles") or {}
        name_cols = [c for c, role in by_column.items() if role == "product_name"]
        if len(name_cols) != 1:
            continue
        name_col = name_cols[0]
        others = [c for c, role in by_column.items() if c != name_col and role == "other"]
        key_col, kind_col = None, None
        for column in others:
            try:
                values = {r[0] for r in dst.execute(
                    f"SELECT DISTINCT lookup({_text(column)}) FROM source.{q(table.name)} AS src") if r[0]}
            except sqlite3.Error:
                continue
            if not values:
                continue
            if key_col is None and len(values) >= 3 and len(values & sold) / len(values) >= ALIAS_MIN_OVERLAP:
                key_col = column
            elif kind_col is None and len(values) <= ALIAS_MAX_KINDS:
                kind_col = column
        if key_col is None:
            continue
        _load_aliases(dst, table, {"alias_key": key_col, "product_name": name_col})
        attrs = {"product_name": name_col, **({"product_kind": kind_col} if kind_col else {})}
        _load_product_attrs(dst, table, "alias", attrs)
        found += 1
    return found


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


def _load_employee_names(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str]) -> None:
    """Full staff names from a staff list. They are the candidates that short staff spellings are matched to."""
    name = roles.get("employee_name")
    if not name:
        return
    code = roles.get("employee_id")
    if code and code != name:
        dst.execute(
            "INSERT INTO temp.employee_codes (code, full_name) "
            f"SELECT DISTINCT lookup(cd), TRIM(fn) FROM (SELECT {_text(code)} AS cd, {_text(name)} AS fn "
            f"FROM source.{q(table.name)} AS src) WHERE cd IS NOT NULL AND fn IS NOT NULL AND TRIM(fn) <> ''"
        )
    dst.execute(
        "INSERT INTO temp.employee_names (full_name) "
        f"SELECT DISTINCT fn FROM (SELECT {_text(name)} AS fn FROM source.{q(table.name)} AS src) "
        "WHERE fn IS NOT NULL"
    )


def match_employee(spelling: str, full_names: Iterable[str]) -> str | None:
    """The full staff name a short or variant spelling stands for, or None when it does not name exactly one.

    The spelling's last token (of two letters or more) must be the last token of the full name, and every other
    token longer than one letter must appear in the full name. Zero matches, or several, leave the spelling as it is.
    """
    tokens = normalize_lookup(spelling).split()
    if not tokens or len(tokens[-1]) < 2:
        return None
    found: dict[str, str] = {}  # normalized full name -> the full name as given
    for full in full_names:
        words = normalize_lookup(full).split()
        if words and words[-1] == tokens[-1] and all(token in words for token in tokens if len(token) > 1):
            found.setdefault(normalize_lookup(full), full)
    return next(iter(found.values())) if len(found) == 1 else None


EMPLOYEE_CODE_MIN_SHARE = 0.95  # share of the codes on orders that the staff list must know


def _employee_code_mapping(dst: sqlite3.Connection, spellings: list[str]) -> dict[str, str]:
    """Staff names by the codes orders write, when the staff list gives each code one name: at least
    EMPLOYEE_CODE_MIN_SHARE of the codes on orders are in the list, no code has two names and no two codes share one.
    Otherwise empty, and the spellings are matched as names instead."""
    names: dict[str, set[str]] = {}
    for code, full in dst.execute("SELECT code, full_name FROM temp.employee_codes"):
        names.setdefault(code, set()).add(full)
    known = {s: names[normalize_lookup(s)] for s in spellings if normalize_lookup(s) in names}
    if not spellings or len(known) < EMPLOYEE_CODE_MIN_SHARE * len(spellings):
        return {}
    if any(len(full) != 1 for full in known.values()):
        return {}
    resolved = {s: next(iter(full)) for s, full in known.items()}
    if len({normalize_lookup(full) for full in resolved.values()}) != len(resolved):
        return {}
    return {s: full for s, full in resolved.items() if full != s}


def _map_employees(dst: sqlite3.Connection) -> int:
    """Rewrite each employee spelling on orders and lines to its full staff name, where match_employee finds one.
    The raw spelling goes to employee_raw first, and only when some spelling changes.

    Returns the number of distinct spellings that were mapped.
    """
    names = [row[0] for row in dst.execute("SELECT full_name FROM temp.employee_names ORDER BY full_name")]
    if not names:
        return 0
    spellings = [row[0] for row in dst.execute("SELECT DISTINCT employee FROM orders WHERE employee IS NOT NULL")]
    mapping = _employee_code_mapping(dst, spellings)
    if mapping:
        # Orders write staff codes and the staff list names them. Rulebook §8: a file with codes goes by the codes, so
        # the code stays the employee (answers and rankings name it); the full name goes to employee_raw, where a
        # filter that names the person finds it (the compiler maps a raw spelling back to the code).
        for table in ("orders", "lines"):
            dst.execute(f"ALTER TABLE {table} ADD COLUMN employee_raw")
            dst.executemany(f"UPDATE {table} SET employee_raw=? WHERE employee=?",
                            [(full, code) for code, full in mapping.items()])
        return len(mapping)
    for spelling in spellings:
        full = match_employee(spelling, names)
        if full is not None and full != spelling:
            mapping[spelling] = full
    if not mapping:
        return 0
    for table in ("orders", "lines"):
        dst.execute(f"ALTER TABLE {table} ADD COLUMN employee_raw")
        dst.execute(f"UPDATE {table} SET employee_raw=employee")
        dst.executemany(f"UPDATE {table} SET employee=? WHERE employee_raw=?",
                        [(full, spelling) for spelling, full in mapping.items()])
    return len(mapping)


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
    # A row with an id and no name takes the name the catalogs give that id; an id with no single name keeps
    # joining by id alone, as before.
    dst.execute(
        "INSERT INTO temp.product_attrs (product_id, name_key, prescription, active_ingredient, product_kind) "
        f"SELECT pid, NULLIF(lookup(full_nm), ''), rx, ai, pk FROM (SELECT pid, "
        f"COALESCE(nm, {_catalog_name('product_id', 'pid')}) AS full_nm, rx, ai, pk FROM (SELECT "
        f"{_text(roles.get('product_id'))} AS pid, {_text(roles.get('product_name'))} AS nm, "
        f"{_prescription_sql(roles.get('prescription_flag'))} AS rx, "
        f"{_text(roles.get('active_ingredient'))} AS ai, {_text(kind_column)} AS pk "
        f"FROM source.{q(table.name)} AS src)) "
        "WHERE (pid IS NOT NULL OR full_nm IS NOT NULL) AND (rx IS NOT NULL OR ai IS NOT NULL OR pk IS NOT NULL)"
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


def _regroup_values(dst: sqlite3.Connection, value_groups: dict[str, dict[str, str]]) -> int:
    """Rewrite each grouped field through its value group, on orders and lines. Values are matched after lookup
    normalization. Where a field is kept raw, its original text goes to <field>_raw first.

    Returns the number of order and line rows whose value changed.
    """
    changed = 0
    for role, mapping in value_groups.items():
        target = {normalize_lookup(raw): canonical for raw, canonical in mapping.items()}
        for table, field in VALUE_GROUP_FIELDS[role]:
            if field in VALUE_GROUP_KEEPS_RAW:
                dst.execute(f"ALTER TABLE {table} ADD COLUMN {field}_raw")
                dst.execute(f"UPDATE {table} SET {field}_raw={field}")
            rows = dst.execute(f"SELECT rowid, {field} FROM {table}").fetchall()
            updates = []
            for rowid, value in rows:
                canonical = target.get(normalize_lookup(value)) if value is not None else None
                if canonical is not None and canonical != value:
                    updates.append((canonical, rowid))
            dst.executemany(f"UPDATE {table} SET {field}=? WHERE rowid=?", updates)
            changed += len(updates)
    return changed


def is_code_text(value: Any) -> bool:
    """Letters and digits with no spaces, at least one digit: a plate, an id or a document number."""
    return (value is not None and len(value) <= CODE_MAX_LENGTH and CODE_VALUE.fullmatch(value) is not None
            and any(char.isdigit() for char in value))


def _register(con: sqlite3.Connection) -> None:
    con.create_function("is_code_text", 1, is_code_text)
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
          status_map: dict[str, str] | None = None, line_kinds: dict[str, str] | None = None,
          value_groups: dict[str, dict[str, str]] | None = None,
          model_failures: list[ModelFailure | str] | None = None,
          conventions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """line_kinds maps a product name (normalized by normalize_lookup) to a LINE_ITEM_KINDS value.

    value_groups maps a role (a key of VALUE_GROUP_FIELDS) to {raw value: canonical value}.
    model_failures lists the model calls that failed and were replaced by fallbacks (a bare stage name counts as
    shop-wide); the solver judges each by its table and kind.
    """
    pairs: list[tuple[str, str]] = []
    for name, kind in (line_kinds or {}).items():
        if kind not in LINE_ITEM_KINDS:
            raise ValueError(f"unknown line kind {kind!r}; expected one of {LINE_ITEM_KINDS}")
        pairs.append((normalize_lookup(name), kind))
    for role in value_groups or {}:
        if role not in VALUE_GROUP_FIELDS:
            raise ValueError(f"unknown value group {role!r}; expected one of {tuple(VALUE_GROUP_FIELDS)}")
    combined = _combined_convention(context.readme)
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
    dst.execute("CREATE TEMP TABLE product_facts (product_id TEXT, name_key TEXT, product_name TEXT, category TEXT, "
                "base_unit TEXT, unit_cost REAL)")
    dst.execute("CREATE TEMP TABLE lot_receipts (lot_id TEXT, product_id TEXT, product_name TEXT, expires_at TEXT, "
                "qty REAL, cost REAL, cost_unit TEXT, cost_basis TEXT)")
    dst.execute("CREATE TEMP TABLE lot_sales (order_id TEXT, lot_id TEXT, sold REAL, returned REAL)")
    dst.execute("CREATE TEMP TABLE lot_sources (kind TEXT)")
    dst.execute("CREATE TEMP TABLE ledger_rows (product_key TEXT, is_id INTEGER, ts TEXT, balance REAL, seq INTEGER)")
    dst.execute("CREATE TEMP TABLE stock_rows (product_id TEXT, name_key TEXT, product_name TEXT, unit TEXT, "
                "qty REAL)")
    dst.execute("CREATE TEMP TABLE product_attrs (product_id TEXT, name_key TEXT, prescription INTEGER, "
                "active_ingredient TEXT, product_kind TEXT)")
    dst.execute("CREATE TEMP TABLE unit_factors (product_id TEXT, name_key TEXT, large_key TEXT, factor REAL)")
    dst.execute("CREATE TEMP TABLE catalog_ids (role TEXT, id TEXT, name TEXT)")
    dst.execute("CREATE TEMP TABLE alias_map (alias_key TEXT PRIMARY KEY, full_name TEXT)")
    dst.execute("CREATE TEMP TABLE employee_names (full_name TEXT)")
    dst.execute("CREATE TEMP TABLE employee_codes (code TEXT, full_name TEXT)")
    dst.execute("CREATE TABLE build_stats (key TEXT, value INTEGER)")
    dst.execute("CREATE TABLE stock_sources (kind TEXT, rows INTEGER)")
    dst.execute("CREATE TABLE expenses (date TEXT, amount REAL, category TEXT)")
    dst.execute("CREATE TABLE price_changes (product_id TEXT, product_name TEXT, name_key TEXT, date TEXT, "
                "old_price REAL, new_price REAL)")
    dst.execute("CREATE TABLE model_failures (stage TEXT, table_name TEXT, kind TEXT)")
    dst.executemany("INSERT INTO model_failures VALUES (?,?,?)", [_failure_row(item) for item in model_failures or []])
    stats: Counter[str] = Counter({key: 0 for key in BUILD_STAT_KEYS})
    report: dict[str, Any] = {
        "sales_tables": 0, "service_tables": 0, "service_event_rows": 0, "purchase_tables": 0, "return_tables": 0, "unannotated": [],
        "duplicate_lines_removed": 0, "columns_with_locale_inferred": 0, "tickets_defaulted_completed": 0,
        "qty_base_derived": 0, "extra_columns": 0, "extra_numbers_not_scaled": [], "src_tables": [],
        "join_keys": [], "catalog_rows": 0,
    }
    # A table takes the annotation of its columns, else that of a catalog sibling; the catalog sheets that share one
    # annotation form a pool, and their roles are chosen from the pooled values (see effective_roles).
    annotated: list[tuple[TableMeta, dict[str, Any]]] = []
    annotation_key: dict[str, tuple[str, ...]] = {}
    for table in context.tables:
        key = tuple(c.name for c in table.columns)
        note = notes.get(key)
        if note is None:
            sibling = _catalog_sibling(table, notes)
            if sibling is None:
                report["unannotated"].append(table.name)
                continue
            key, note = sibling
        annotated.append((table, note))
        annotation_key[table.name] = key
    annotated = _reread_lot_sales(dst, annotated)
    annotated, report["annotation_fixes"] = annotation_check.check_annotations(dst, annotated)
    pools: dict[tuple[str, ...], list[TableMeta]] = {}
    for table, note in annotated:
        if note["kind"] in CATALOG_KINDS:
            pools.setdefault(annotation_key[table.name], []).append(table)

    def pool_of(table: TableMeta, note: dict[str, Any]) -> list[TableMeta] | None:
        return pools.get(annotation_key[table.name]) if note["kind"] in CATALOG_KINDS else None

    # catalog ids and names are read first, so that a duplicated id column can be judged by how it joins them.
    # Every table's roles are chosen before any id is loaded, so the first pass cannot judge a column by its own ids.
    first_pass = [(table, note["kind"], effective_roles(table, note, dst, pool_of(table, note)))
                  for table, note in annotated]
    for table, kind, roles in first_pass:
        _load_catalog_keys(dst, table, kind, roles)
    carry_log: list[tuple[str, list[Carried]]] = []  # (layer table, columns carried into it), for the catalog
    carried_tables: set[str] = set()  # all source columns survive in canonical fields or carried extra columns
    for table, note in annotated:
        given, unresolved = _given_by_column(table, note.get("roles") or {})
        roles = effective_roles(table, note, dst, pool_of(table, note))
        # Only a role that stayed missing after every fallback can break the table.
        stats["annotation_keys_unresolved"] += sum(role in COUNTED_ROLES and not roles.get(role) for role in unresolved)
        if not roles.get("employee_id") and roles.get("employee_name"):
            roles["employee_id"] = roles["employee_name"]
        synthetic = f"{table.name}:"  # order id prefix for rows that have no order column
        # an order column that was named but matches no column, on a table with order-level columns: the
        # rows cannot be tied to their orders, so synthetic per-row orders would only break payment links
        lost_order = note["kind"] == "sales" and "order_id" in unresolved and not roles.get("order_id") and any(
            roles.get(role) for role in ORDER_SIGNAL_ROLES)
        if lost_order:
            stats["tables_skipped_missing_order_id"] += 1
            continue
        if note["kind"] in CARRIED_KINDS and _layer_loads(note["kind"], roles):
            carried_tables.add(table.name)
        scale = _amount_scale(context, table)
        if scale != 1:
            stats["scaled_tables"] += 1
        if note["kind"] == "sales":
            product = roles.get("product_name") or roles.get("product_id")
            carried = _extras_for(dst, table, given, _consumed(roles, LINE_READ_ROLES), "raw_lines", scale, report,
                                  carry_log)
            _load_sales(dst, table, roles, synthetic, 1 if product else 0, report, stats, scale=scale,
                        combined=combined, carried=carried)
            report["sales_tables"] += 1
        elif note["kind"] == "service_ticket" and _ticket_has_money(roles):
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
            carried = _extras_for(dst, table, given, _consumed(ticket, LINE_READ_ROLES) | _consumed(roles,
                                  SERVICE_READ_ROLES), "raw_lines", scale, report, carry_log)
            rows = _load_lines(dst, table, ticket, synthetic, 1, report, stats,
                               defaults={"status": "completed"} if status_defaulted else None, scale=scale,
                               carried=carried)
            if status_defaulted:
                report["tickets_defaulted_completed"] += rows
            report["service_tables"] += 1
        elif note["kind"] == "service_ticket":
            # a visit log with no money column: its rows are events (a warranty or reception book), never orders
            report["service_event_rows"] += _load_service_events(dst, table, roles, synthetic, report, stats, scale)
        elif note["kind"] == "purchase" and (roles.get("purchase_id") or roles.get("purchased_at")):
            carried = _extras_for(dst, table, given, _consumed(roles, PURCHASE_READ_ROLES), "purchases", scale,
                                  report, carry_log)
            _insert(dst, "purchases", table, roles, PURCHASE_FIELDS, PURCHASE_NUMBERS, report=report, stats=stats,
                    scale=scale, carried=carried)
            report["purchase_tables"] += 1
        elif note["kind"] == "payment" and (roles.get("order_id") or roles.get("paid_at")):
            carried = _extras_for(dst, table, given, _consumed(roles, PAYMENT_READ_ROLES), "payments", scale,
                                  report, carry_log)
            _insert(dst, "payments", table, roles, PAYMENT_FIELDS, PAYMENT_NUMBERS, report=report, stats=stats,
                    scale=scale, carried=carried)
        elif note["kind"] == "delivery" and (roles.get("order_id") or roles.get("delivered_at")):
            carried = _extras_for(dst, table, given, _consumed(roles, DELIVERY_READ_ROLES), "deliveries", scale,
                                  report, carry_log)
            _insert(dst, "deliveries", table, roles, DELIVERY_FIELDS, DELIVERY_NUMBERS, report=report, stats=stats,
                    carried=carried)
        elif note["kind"] == "sales_return":
            _load_returns(dst, table, roles)
            report["return_tables"] += 1
        elif note["kind"] == "customer_catalog":
            _load_customer_groups(dst, table, roles)
        elif note["kind"] in ("product_catalog", "unit_conversion"):
            _load_product_facts(dst, table, roles)
            _load_unit_factors(dst, table, roles)
            _load_text_row_factors(dst, table, roles)
        elif note["kind"] == "alias":
            _load_aliases(dst, table, roles)
        elif note["kind"] == "employee_catalog":
            _load_employee_names(dst, table, roles)
        elif note["kind"] == "expense":
            _load_expenses(dst, table, roles, scale)
        elif note["kind"] == "price_history":
            _load_price_changes(dst, table, roles, scale)
        if note["kind"] in PRODUCT_ATTR_KINDS:
            _load_product_attrs(dst, table, note["kind"], roles)
        if note["kind"] in STOCK_KINDS:
            loaded = _load_stock_table(dst, table, note, roles)
            dst.execute("INSERT INTO stock_sources VALUES (?,?)", (note["kind"], loaded))
        if note["kind"] in ("lot", "purchase"):
            _load_lots(dst, table, note, roles, scale)
    _fill_receipts(dst)
    _fill_from_catalogs(dst)
    report["alias_tables_found"] = _promote_alias_tables(dst, annotated)
    report["alias_lines_resolved"] = _resolve_aliases(dst)
    report["non_product_lines"] = _classify_lines(dst, pairs)
    report["status_mapped"] = _map_status(dst, status_map or {}, stats)
    headers = frozenset(normalize_lookup(c.original_name) for t in context.tables for c in t.columns)
    report["readme_unit_factors"] = _load_readme_unit_factors(dst, context.readme, headers)
    report["convention_unit_factors"] = _apply_unit_conventions(dst, conventions or [])
    report["rulebook_unit_factors"] = _load_text_unit_factors(dst, rulebook_params.rulebook_raw_text() or "", headers)
    report["qty_base_derived"] = _derive_qty_base(dst)
    # the annotation's roles, not the header lookup, decide which column names the customer: spellings of one phone
    # number ("Name 0353 838 285", "Name (0353838285)") are one customer before the order's key is taken from the name
    report["phone_spellings_unified"] = unify_phone_spellings(dst, [("raw_lines", "customer_name", "customer_id")])
    derived_columns = _derive(dst, report)
    report["ledger_payments"] = _load_ledger_payments(dst, context, annotated, report)
    report["ledger_built"] = _build_ledger(dst)
    report["stock_rows"] = _build_stock(dst)
    if not report["stock_rows"]:
        report["lot_stock_rows"] = _build_lot_stock(dst)
    else:  # a stock read from a report or the movement ledger: the lots still get their balances
        report["lot_stock_rows"] = _build_lot_stock(dst, write_stock=False)
        report["lot_report_mismatch"] = stock_sources.drop_lots_if_disagreeing(dst)
    # Tables the annotator did not read may hold stock or lots: a question about them is not unanswerable.
    # A table of kind other is read as unknown too: it may hold lots.
    unknown = len(report["unannotated"]) + sum(1 for _, note in annotated if note["kind"] == "other")
    dst.execute("INSERT INTO stock_sources VALUES (?,?)", ("unannotated", unknown))
    report["employee_spellings_mapped"] = _map_employees(dst)
    stats["values_regrouped"] = _regroup_values(dst, value_groups or {})
    report["orders_marked_returned"] = _mark_returned(dst)
    notes_of = {table.name: note for table, note in annotated}
    for table in context.tables:
        if table.name not in carried_tables:
            copied = _copy_unloaded(dst, table, notes_of.get(table.name), carry_log)
            if copied:
                report["src_tables"].append(copied)
    report["extra_columns"] = sum(len(carried) for target, carried in carry_log if not target.startswith("src_"))
    report["join_keys"], report["catalog_rows"] = _write_column_catalog(dst, carry_log)
    _register_derived(dst, derived_columns)
    report["obligations"] = obligations.build(dst)  # debts and instalments of a side ledger (see obligations.py)
    report["catalog_attributes"] = _attach_catalog_attributes(dst, annotated)
    _write_shop_facts(dst, context)
    report["conventions_applied"] = _apply_data_conventions(dst, conventions or [])
    # The shop's own documentation, kept for the question stage (column descriptions, conventions).
    dst.execute("CREATE TABLE shop_docs (key TEXT, text TEXT)")
    dst.execute("INSERT INTO shop_docs VALUES ('readme', ?)", (context.readme or "",))
    dst.executemany("INSERT INTO build_stats VALUES (?,?)", [(key, stats[key]) for key in BUILD_STAT_KEYS])
    report["status_null_orders"] = dst.execute("SELECT COUNT(*) FROM orders WHERE status IS NULL").fetchone()[0]
    report["orders"] = dst.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    report["lines"] = dst.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
    report["purchases"] = dst.execute("SELECT COUNT(*) FROM purchases").fetchone()[0]
    report["payments"] = dst.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    report["deliveries"] = dst.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
    report["model_failures"] = len(model_failures or [])
    dst.commit()
    dst.execute("DETACH DATABASE source")
    dst.commit()
    dst.close()
    return report


def _layer_loads(kind: str, roles: dict[str, str]) -> bool:
    """True when the layer reads the table: its kind has a loader and, for a purchase, payment or delivery table,
    the roles that loader needs are present (the same guards as build)."""
    if kind == "purchase":
        return bool(roles.get("purchase_id") or roles.get("purchased_at"))
    if kind == "payment":
        return bool(roles.get("order_id") or roles.get("paid_at"))
    if kind == "delivery":
        return bool(roles.get("order_id") or roles.get("delivered_at"))
    if kind == "service_ticket":
        return _ticket_has_money(roles)
    return kind in LAYER_KINDS


def _load_service_events(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str], synthetic: str,
                         report: dict[str, Any], stats: Counter, scale: int) -> int:
    """Rows of a service log with no amount, kept apart from sales in service_events (event_id, date, status,
    customer_name, item). They are read with the same date, status and name rules as any ticket, then moved out of
    raw_lines so that no order, line or per-order figure counts them. Returns the rows kept."""
    dst.execute("CREATE TABLE IF NOT EXISTS service_events (event_id, date, status, customer_name, item, source_table)")
    ticket = {role: column for role, column in roles.items() if role != "order_id"}
    if roles.get("ticket_id"):
        ticket["order_id"] = roles["ticket_id"]
    if roles.get("service_item"):
        ticket["product_name"] = roles["service_item"]
    start = dst.execute("SELECT COALESCE(MAX(rowid), 0) FROM raw_lines").fetchone()[0]
    status_defaulted = "order_status" not in ticket and not (ticket.get("order_note") or ticket.get("note"))
    _load_lines(dst, table, ticket, synthetic, 1, report, stats,
                defaults={"status": "completed"} if status_defaulted else None, scale=scale)
    dst.execute(
        "INSERT INTO service_events SELECT order_id, date, status, customer_name, product_name, ? "
        "FROM raw_lines WHERE rowid > ?", (table.name, start))
    kept = dst.execute("SELECT changes()").fetchone()[0]
    dst.execute("DELETE FROM raw_lines WHERE rowid > ?", (start,))
    return kept


def _ticket_has_money(roles: dict[str, str]) -> bool:
    """A service ticket becomes an order only when the table records an amount. A ledger of visits with no money
    column (a warranty or reception log) is an event record, not sales: turning each row into an order with a NULL
    amount would add phantom orders that dilute every per-order average and count. Such a table stays a source."""
    return bool(roles.get("ticket_amount") or roles.get("line_amount"))


def _carry_order_columns(dst: sqlite3.Connection) -> None:
    """Order-level extras: the first non-empty value of the order's lines, in line order. A blank cell in a later
    row of a group repeats the first row (rulebook), so the order takes the first value its lines give."""
    for name, sql_type in _extra_columns(dst, "raw_lines"):
        dst.execute(f"ALTER TABLE orders ADD COLUMN {q(name)} {sql_type}")
        dst.execute(
            f"UPDATE orders SET {q(name)}=(SELECT r.{q(name)} FROM raw_lines AS r "
            f"WHERE r.order_id=orders.order_id AND r.{q(name)} IS NOT NULL ORDER BY r.rowid LIMIT 1)"
        )


def _copy_unloaded(dst: sqlite3.Connection, table: TableMeta, note: dict[str, Any] | None,
                   carry_log: list[tuple[str, list[Carried]]]) -> str | None:
    """A source not fully carried into canonical tables, copied whole as src_<table name>: every column as text,
    with numeric and date twins. This also preserves catalog attributes not consumed by a specialized loader.
    Returns the copy's name, or None when the table has no column."""
    given = _given_by_column(table, note.get("roles") or {})[0] if note is not None else None
    carried = _carry_plan(dst, table, set(), given)
    if not carried:
        return None
    columns: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for c in carried:  # a twin whose name a plain column already has is left out
        for entry in _carry_columns(c, plain=True):
            if entry[0] not in seen:
                seen.add(entry[0])
                columns.append(entry)
    name = f"src_{table.name}"
    definitions = ", ".join(f"{q(column)} {sql_type}" for column, _, sql_type in columns)
    names = ", ".join(q(column) for column, _, _ in columns)
    values = ", ".join(expr for _, expr, _ in columns)
    dst.execute(f"CREATE TABLE {q(name)} ({definitions})")
    dst.execute(f"INSERT INTO {q(name)} ({names}) SELECT {values} FROM source.{q(table.name)} AS src")
    carry_log.append((name, carried))
    return name


def _join_key(dst: sqlite3.Connection, target: str, column: str) -> str | None:
    """orders.order_id when at least JOIN_MIN_SHARE of the column's distinct values are order ids and the values are
    spread like a key (a small-domain numeric column that matches a few order ids is not one)."""
    from bizreason.semantic import annotation_check
    is_key, _reason = annotation_check.join_key_verdict(dst, target, column, JOIN_MIN_SHARE)
    return "orders.order_id" if is_key else None


def _catalog_row(dst: sqlite3.Connection, target: str, column: str, c: Carried, value_type: str,
                 join_key: str | None) -> tuple[Any, ...]:
    table, name = q(target), q(column)
    distinct = dst.execute(f"SELECT COUNT(DISTINCT {name}) FROM {table}").fetchone()[0]
    samples = [value for (value,) in dst.execute(
        f"SELECT __value FROM (SELECT {name} AS __value, MIN(rowid) AS __first FROM {table} "
        f"WHERE {name} IS NOT NULL GROUP BY __value ORDER BY __first LIMIT {CATALOG_SAMPLES})")]
    return (target, column, c.table, c.header, c.role, value_type, distinct,
            json.dumps(samples, ensure_ascii=False), join_key)


def _write_column_catalog(dst: sqlite3.Connection, carry_log: list[tuple[str, list[Carried]]]) -> tuple[list[str], int]:
    """column_catalog: a row for each carried column of a layer table and each column of a src_ table, with its
    original header, annotated role, value type, distinct count, sample values and join key (a src_ column whose
    values are order ids). Returns the join keys found and the number of rows."""
    dst.execute(
        "CREATE TABLE column_catalog (table_name TEXT, column_name TEXT, source_table TEXT, source_header TEXT, "
        "role TEXT, value_type TEXT, distinct_count INTEGER, sample_values TEXT, join_key_to TEXT, "
        "derived INTEGER DEFAULT 0)"
    )
    origins: dict[str, Carried] = {}  # a carried column's name is its source column; the first table to carry it
    for target, carried in carry_log:
        if not target.startswith("src_"):
            for c in carried:
                origins.setdefault(c.source, c)
    rows: list[tuple[Any, ...]] = []
    for target in CATALOG_TARGETS:
        for name, _ in _extra_columns(dst, target):
            prefix = next(p for p in EXTRA_PREFIXES if name.startswith(p))
            c = origins.get(name[len(prefix):])
            if c is not None:
                value_type = {"x_": c.value_type, "xn_": "number", "xd_": "date"}[prefix]
                rows.append(_catalog_row(dst, target, name, c, value_type, None))
    joins: list[str] = []
    for target, carried in carry_log:
        if not target.startswith("src_"):
            continue
        existing = {row[1] for row in dst.execute(f"PRAGMA table_info({q(target)})")}
        seen: set[str] = set()
        for c in carried:
            for name, _, _ in _carry_columns(c, plain=True):
                if name in seen or name not in existing:
                    continue
                seen.add(name)
                join_key = None
                if name == c.source:
                    value_type = c.value_type
                    join_key = _join_key(dst, target, name)
                else:
                    value_type = "number" if name.startswith("xn_") else "date"
                if join_key:
                    joins.append(f"{target}.{name}")
                rows.append(_catalog_row(dst, target, name, c, value_type, join_key))
    dst.executemany("INSERT INTO column_catalog (table_name, column_name, source_table, source_header, role, "
                    "value_type, distinct_count, sample_values, join_key_to) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return joins, len(rows)


def _failure_row(item: ModelFailure | str) -> tuple[str, str | None, str | None]:
    if isinstance(item, str):
        return (item, None, None)
    return (item.stage, item.table_name, item.kind)


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


CATALOG_ATTR_MAX_DISTINCT = 100
CATALOG_ATTR_KINDS = ("product_catalog", "unit_conversion", "alias")
# Roles the catalog loaders read (product facts, unit factors, aliases, attributes); any other column is attached.
CATALOG_READ_ROLES = ("product_id", "product_name", "product_category", "base_unit", "catalog_cost", "catalog_price",
                      "unit", "conversion_factor", "large_unit", "product_alias", "alias_key", "prescription",
                      "active_ingredient", "product_kind")


def _attach_catalog_attributes(dst: sqlite3.Connection, annotated: list[tuple[TableMeta, dict[str, Any]]]) -> list[str]:
    """Columns of a product list that no canonical field reads and that hold few distinct values (a size, a brand,
    a kind) are attached to each sale line of that product as x_cat_<column>, matched by product id when both
    sides have one, else by normalized name, and described in column_catalog. Returns the columns attached."""
    if not _has_table(dst, "lines") or not _has_table(dst, "column_catalog"):
        return []
    line_cols = {r[1] for r in dst.execute("PRAGMA table_info(lines)")}
    attached: list[str] = []
    # Catalogs may be split across sheets. Collect every candidate before writing a line:
    # the first sheet is not necessarily complete, and contradictory rows are not a fact.
    attributes: dict[str, list[tuple[str, str, str]]] = {}
    origins: dict[str, tuple[str, str]] = {}
    sold_names = {normalize_lookup(str(row[0])) for row in dst.execute(
        "SELECT DISTINCT product_name FROM lines WHERE product_name IS NOT NULL")} \
        if "product_name" in line_cols else set()
    sold_ids = {str(row[0]).strip() for row in dst.execute(
        "SELECT DISTINCT product_id FROM lines WHERE product_id IS NOT NULL")} \
        if "product_id" in line_cols else set()
    for table, note in annotated:
        if note.get("kind") not in CATALOG_ATTR_KINDS:
            continue
        roles = {v: k for k, v in (note.get("roles") or {}).items() if isinstance(v, str)}
        by_column = note.get("roles") or {}
        pid, name = roles.get("product_id"), roles.get("product_name")
        if not (pid or name):
            continue
        use_id = bool(pid and sold_ids)
        if not use_id and not (name and sold_names):
            continue
        for column in table.columns:
            if by_column.get(column.name) in CATALOG_READ_ROLES or column.name.startswith("__"):
                continue
            target = f"x_cat_{column.name}"
            if target in line_cols:
                continue
            distinct = dst.execute(f"SELECT COUNT(DISTINCT {_text(column.name)}) FROM source.{q(table.name)} AS src"
                                   ).fetchone()[0]
            if not distinct or distinct > CATALOG_ATTR_MAX_DISTINCT:
                continue
            key_src = _text(pid) if use_id else f"lookup({_text(name)})"
            sold = sold_ids if use_id else sold_names
            values = [("id" if use_id else "name", str(key), str(value))
                      for key, value in dst.execute(
                          f"SELECT DISTINCT {key_src}, {_text(column.name)} "
                          f"FROM source.{q(table.name)} AS src "
                          f"WHERE {key_src} IS NOT NULL AND {_text(column.name)} IS NOT NULL")
                      if str(key) in sold]
            if values:
                attributes.setdefault(target, []).extend(values)
                origins.setdefault(target, (table.name, column.original_name))
    if not attributes:
        return []
    dst.execute("CREATE TEMP TABLE _catalog_attrs (kind TEXT, key TEXT, value TEXT)")
    dst.execute("CREATE INDEX temp._catalog_attrs_key ON _catalog_attrs(kind, key)")
    try:
        for target, values in attributes.items():
            if len({normalize_lookup(value) for _, _, value in values}) > CATALOG_ATTR_MAX_DISTINCT:
                continue
            dst.execute("DELETE FROM temp._catalog_attrs")
            dst.executemany("INSERT INTO temp._catalog_attrs VALUES (?,?,?)", values)
            dst.execute(f"ALTER TABLE lines ADD COLUMN {q(target)} TEXT")
            line_cols.add(target)
            matches = []
            if sold_ids:
                matches.append("(kind='id' AND key=TRIM(CAST(lines.product_id AS TEXT)))")
            if sold_names:
                matches.append("(kind='name' AND key=lookup(lines.product_name))")
            dst.execute(
                f"UPDATE lines SET {q(target)} = (SELECT CASE WHEN COUNT(DISTINCT lookup(value))=1 "
                f"THEN MIN(value) END FROM temp._catalog_attrs WHERE {' OR '.join(matches)})")
            filled = dst.execute(f"SELECT COUNT({q(target)}) FROM lines").fetchone()[0]
            if not filled:
                continue
            distinct = dst.execute(f"SELECT COUNT(DISTINCT {q(target)}) FROM lines").fetchone()[0]
            samples = [r[0] for r in dst.execute(
                f"SELECT DISTINCT {q(target)} FROM lines WHERE {q(target)} IS NOT NULL LIMIT {CATALOG_SAMPLES}")]
            source_table, header = origins[target]
            dst.execute("INSERT INTO column_catalog (table_name, column_name, source_table, source_header, role, "
                        "value_type, distinct_count, sample_values, join_key_to) VALUES (?,?,?,?,?,?,?,?,?)",
                        ("lines", target, source_table, header, "other", "text", distinct,
                         json.dumps(samples, ensure_ascii=False), None))
            attached.append(target)
    finally:
        dst.execute("DROP TABLE temp._catalog_attrs")
    return attached


def _apply_unit_conventions(dst: sqlite3.Connection, directives: list[dict[str, Any]]) -> int:
    """README unit conversions extracted by the model, applied only for products whose sale lines use that large
    unit (checked on the data). Returns the number of products given a factor."""
    sold = {}
    for name, unit in dst.execute("SELECT DISTINCT product_name, unit FROM raw_lines WHERE product_name IS NOT NULL"):
        sold.setdefault(normalize_lookup(name), set()).add(normalize_lookup(unit) if unit else "")
    added = 0
    for item in directives:
        if item.get("type") != "unit_factor":
            continue
        large, base = normalize_lookup(item["large_unit"]), item["base_unit"]
        named = [normalize_lookup(p) for p in item.get("products") or [] if isinstance(p, str)]
        if named:
            keys = [k for k in sold if any(n and (k == n or n in k) for n in named)]
        else:
            keys = list(sold)
        keys = [k for k in keys if large in sold[k]]  # the product is really sold by that large unit
        for key in keys:
            dst.executemany(
                "INSERT INTO temp.unit_factors (product_id, name_key, large_key, factor) VALUES (NULL,?,?,?)",
                [(key, large, float(item["factor"])), (key, normalize_lookup(base), 1.0)])
            dst.execute("INSERT INTO temp.product_facts (product_id, name_key, category, base_unit, unit_cost) "
                        "VALUES (NULL,?,NULL,?,NULL)", (key, base))
            added += 1
    return added


def _apply_data_conventions(dst: sqlite3.Connection, directives: list[dict[str, Any]]) -> list[str]:
    """Conventions about meaning, each checked on the data before it is recorded or applied:
    - zero_amount_meaning: recorded as a shop fact only when the shop has orders with zero money;
    - return_marker: orders whose kept column (matched by its original header) holds one of the words are marked
      returned, only when that column exists and some values hold the words;
    - negative_qty_is_return: recorded as a shop fact when negative quantities exist."""
    applied: list[str] = []
    for item in directives:
        kind = item.get("type")
        if kind == "zero_amount_meaning":
            zero = dst.execute("SELECT COUNT(*) FROM orders WHERE COALESCE(revenue, 0) = 0").fetchone()[0]
            if _has_table(dst, "service_events"):  # visits that collected no money are zero-amount visits too
                zero += dst.execute("SELECT COUNT(*) FROM service_events").fetchone()[0]
            if zero:
                dst.execute("INSERT INTO shop_facts VALUES (?, 1)", (f"zero_amount_is_{item['meaning']}",))
                applied.append(f"zero_amount_is_{item['meaning']}")
        elif kind == "debt_state_column":
            header = normalize_lookup(item.get("column", ""))
            row = dst.execute(
                "SELECT column_name FROM column_catalog WHERE table_name='orders' AND lookup(source_header)=?",
                (header,)).fetchone() if _has_table(dst, "column_catalog") else None
            if row is None or "outstanding" not in {r[1] for r in dst.execute("PRAGMA table_info(orders)")}:
                continue
            values = item.get("values") if isinstance(item.get("values"), dict) else {}
            states = {normalize_lookup(k): v for k, v in values.items() if isinstance(v, list) and len(v) == 2}
            debt_tokens = rulebook_params.active().debt_method_tokens
            updates = []
            read_by_pattern = 0
            for order_id, text, due, method in dst.execute(
                    f"SELECT order_id, {row[0]}, COALESCE(total_due, revenue), payment_method FROM orders "
                    f"WHERE {row[0]} IS NOT NULL"):
                state = states.get(normalize_lookup(str(text)))
                if state and state[0]:
                    updates.append((float(state[1] or 0), order_id))
                    continue
                # a note the model's reading does not cover: the patterns of a debt note, on a credit order only
                words = f" {normalize_lookup(method or '')} "
                if state is None and any(f" {token} " in words for token in debt_tokens):
                    owed = debt_sources.read_debt_note(text, due)
                    if owed is not None:
                        updates.append((owed, order_id))
                        read_by_pattern += 1
            if updates:
                dst.executemany("UPDATE orders SET outstanding=? WHERE order_id=?", updates)
                dst.execute("INSERT INTO shop_facts VALUES ('debt_state_at_data_end', 1)")
                applied.append(f"debt_state_column:{row[0]}:{len(updates)}:{read_by_pattern}")
        elif kind == "negative_qty_is_return":
            if dst.execute("SELECT 1 FROM raw_lines WHERE qty < 0 LIMIT 1").fetchone():
                dst.execute("INSERT INTO shop_facts VALUES ('negative_qty_is_return', 1)")
                applied.append("negative_qty_is_return")
        elif kind == "return_marker":
            header = normalize_lookup(item["column"])
            row = dst.execute(
                "SELECT column_name FROM column_catalog WHERE table_name='orders' AND column_name LIKE 'x\\_%' "
                "ESCAPE '\\' AND lookup(source_header)=?", (header,)).fetchone() if _has_table(dst, "column_catalog") else None
            if row is None:
                continue
            words = [normalize_lookup(w) for w in item["words"] if isinstance(w, str) and normalize_lookup(w)]
            cond = " OR ".join("lookup({c}) LIKE ?".format(c=row[0]) for _ in words)
            if not cond:
                continue
            args = [f"%{w}%" for w in words]
            hits = dst.execute(f"SELECT order_id FROM orders WHERE {cond}", args).fetchall()
            if not hits:
                continue
            ids = [h[0] for h in hits]
            marks = ",".join("?" for _ in ids)
            cancelled = ",".join(_sql_text(v) for v in rulebook_params.active().cancelled_statuses) or "''"
            for table in ("orders", "lines"):
                dst.execute(f"UPDATE {table} SET status='returned' WHERE order_id IN ({marks}) "
                            f"AND COALESCE(status,'') NOT IN ({cancelled})", ids)
            applied.append(f"return_marker:{row[0]}:{len(ids)}")
    return applied


def _has_table(dst: sqlite3.Connection, name: str) -> bool:
    return dst.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


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


def _derive(dst: sqlite3.Connection, report: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Builds orders and lines; returns the order columns the layer computed (see _derive_order_totals)."""
    _derive_orders(dst)
    _carry_order_columns(dst)
    derived = _derive_order_totals(dst, report)
    _fill_customer_names(dst)
    _fill_customer_groups(dst)
    _derive_lines(dst)
    tokens = " OR ".join(f"lookup(status) LIKE '%{t}%'" for t in CANCEL_TOKENS)
    dst.execute("ALTER TABLE purchases ADD COLUMN cancelled")
    dst.execute(f"UPDATE purchases SET cancelled=CASE WHEN status IS NOT NULL AND ({tokens}) THEN 1 ELSE 0 END")
    return derived


def _derive_orders(dst: sqlite3.Connection) -> None:
    # Read when the tables are built: rulebook_params.set_active must run before build.
    walk_in = ",".join(_sql_text(name) for name in rulebook_params.active().walk_in_names)
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


def _columns_of(dst: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in dst.execute(f"PRAGMA table_info({q(table)})")}


def _repeats_per_order(dst: sqlite3.Connection) -> bool:
    """True when raw_lines.line_discount is a whole-order discount repeated on every line of the order.

    Read from the data, on orders with two or more product lines and a discount: the line values of such an order are
    all given and equal, and the line amounts are the undiscounted quantity times price (so the discount was never
    taken off a line). A shop whose line amounts are missing, or whose discounts differ between the lines of an order,
    is not read this way."""
    columns = _columns_of(dst, "raw_lines")
    if not {"line_discount", "line_amount", "qty", "unit_price"} <= columns:
        return False
    orders, constant = dst.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN filled=n AND lo=hi THEN 1 ELSE 0 END), 0) FROM ("
        "SELECT COUNT(*) AS n, COUNT(line_discount) AS filled, MIN(line_discount) AS lo, MAX(line_discount) AS hi "
        "FROM raw_lines WHERE has_product=1 AND order_id IS NOT NULL GROUP BY order_id "
        "HAVING COUNT(*)>=2 AND MAX(ABS(COALESCE(line_discount,0)))>0)").fetchone()
    if orders < ORDER_LEVEL_MIN_ORDERS or constant < ORDER_LEVEL_SHARE * orders:
        return False
    lines, undiscounted = dst.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN ABS(line_amount - qty*unit_price) <= ? THEN 1 ELSE 0 END), 0) "
        "FROM raw_lines WHERE has_product=1 AND line_discount IS NOT NULL AND ABS(line_discount)>0 "
        "AND line_amount IS NOT NULL AND qty IS NOT NULL AND unit_price IS NOT NULL",
        (AMOUNT_TOLERANCE,)).fetchone()
    return bool(lines) and undiscounted >= ORDER_LEVEL_SHARE * lines


def _order_component(dst: sqlite3.Connection, name: str, kind: str, column: str | None,
                     report: dict[str, Any]) -> bool:
    """Add orders.<name> from the lines: the order's value of a repeated column (MAX over its lines, a negative-only
    column read as positive) and, separately, the sum of the lines classified as `kind` (an off-goods line such as a
    voucher or a delivery fee, counted by its absolute amount). An order given by both sources is ambiguous and stays
    NULL. Returns whether any order has a value; the column is added only then."""
    sources: list[str] = []
    if column is not None:
        sign = dst.execute(f"SELECT MIN({q(column)}) < 0 AND MAX({q(column)}) <= 0 FROM raw_lines").fetchone()[0]
        pick = f"-MIN({q(column)})" if sign else f"MAX({q(column)})"
        sources.append(f"(SELECT order_id, {pick} AS v FROM raw_lines "
                       f"WHERE {q(column)} IS NOT NULL AND order_id IS NOT NULL GROUP BY order_id)")
    has_rows = dst.execute("SELECT 1 FROM raw_lines WHERE line_kind=? LIMIT 1", (kind,)).fetchone() is not None
    if has_rows:
        amount = "COALESCE(line_amount, qty*unit_price, unit_price)"
        sources.append(f"(SELECT order_id, SUM(ABS({amount})) AS v FROM raw_lines WHERE line_kind={_sql_text(kind)} "
                       f"AND order_id IS NOT NULL AND {amount} IS NOT NULL GROUP BY order_id)")
    if not sources:
        return False
    dst.execute("DROP TABLE IF EXISTS temp._component")
    dst.execute("CREATE TEMP TABLE _component (order_id TEXT, v REAL, n INTEGER)")
    for source in sources:
        dst.execute(f"INSERT INTO temp._component (order_id, v, n) SELECT order_id, v, 1 FROM {source}")
    dst.execute("CREATE INDEX temp._component_order ON _component(order_id)")
    conflicts = dst.execute("SELECT COUNT(*) FROM (SELECT order_id FROM temp._component GROUP BY order_id "
                            "HAVING COUNT(*)>1)").fetchone()[0]
    report[f"{name}_conflicts"] = conflicts
    dst.execute(f"ALTER TABLE orders ADD COLUMN {name} REAL")
    dst.execute(f"UPDATE orders SET {name}=(SELECT CASE WHEN COUNT(*)=1 THEN MIN(v) END FROM temp._component c "
                f"WHERE c.order_id=orders.order_id)")
    dst.execute("DROP TABLE temp._component")
    return dst.execute(f"SELECT 1 FROM orders WHERE {name} IS NOT NULL LIMIT 1").fetchone() is not None


def _derive_order_totals(dst: sqlite3.Connection, report: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Order-level discount and delivery fee on orders, and the order total where the rulebook defines it.

    * orders.order_discount / orders.shipping_fee: a column the sheet gives per order (repeated on every line, so one
      value per order), or the lines the shop classes as a discount / delivery fee, or a line-discount column that
      is a whole-order discount repeated on each line (see _repeats_per_order); positive amounts.
    * orders.total_due is derived only when no sheet gives it (rulebook section 4: the total the customer pays for
      the whole order, after the order-level discount, including the delivery fee): the goods total (the sheet's, else
      the sum of the product lines, which already have line discounts taken off) less the order discount plus the
      delivery fee. It needs a recorded source of at least one of the two; every order that is not cancelled must
      have a goods total; and where the sheet gives both a paid and an outstanding amount the derived total must
      agree with their sum, or the sheet records another total and nothing is derived (taxes are never guessed).

    Returns (column, role, description) of each column added, for column_catalog."""
    derived: list[tuple[str, str, str]] = []
    raw = _columns_of(dst, "raw_lines")
    discount_column = "order_discount" if "order_discount" in raw and dst.execute(
        "SELECT 1 FROM raw_lines WHERE order_discount IS NOT NULL LIMIT 1").fetchone() else None
    shipping_column = "shipping_fee" if "shipping_fee" in raw and dst.execute(
        "SELECT 1 FROM raw_lines WHERE shipping_fee IS NOT NULL LIMIT 1").fetchone() else None
    order_level_line_discount = False
    if discount_column is None and dst.execute(
            "SELECT 1 FROM raw_lines WHERE line_kind='discount' LIMIT 1").fetchone() is None:
        order_level_line_discount = _repeats_per_order(dst)
        if order_level_line_discount:
            discount_column = "line_discount"
    report["order_discount_from_line_column"] = order_level_line_discount
    has_discount = _order_component(dst, "order_discount", "discount", discount_column, report)
    has_shipping = _order_component(dst, "shipping_fee", "shipping_fee", shipping_column, report)
    if has_discount:
        derived.append(("order_discount", "order_discount",
                        "order-level discount: the order's value of a per-order column, or the sum of its discount lines"))
    if has_shipping:
        derived.append(("shipping_fee", "shipping_fee",
                        "delivery fee: the order's value of a per-order column, or the sum of its delivery-fee lines"))
    report["order_total_derived"] = 0
    if dst.execute("SELECT 1 FROM orders WHERE total_due IS NOT NULL LIMIT 1").fetchone() or not (
            has_discount or has_shipping):
        return derived
    if report.get("order_discount_conflicts") or report.get("shipping_fee_conflicts"):
        report["order_total_skipped"] = "an order's discount or delivery fee is given by two sources"
        return derived
    base = "COALESCE(goods_total, line_total)"
    adjust = ("-COALESCE(order_discount,0)" if has_discount else "") + ("+COALESCE(shipping_fee,0)" if has_shipping else "")
    cancelled = "COALESCE(status,'')='cancelled'"
    if dst.execute(f"SELECT 1 FROM orders WHERE NOT {cancelled} AND {base} IS NULL LIMIT 1").fetchone():
        report["order_total_skipped"] = "an order has no goods total"
        return derived
    both, agree = dst.execute(
        f"SELECT COUNT(*), COALESCE(SUM(CASE WHEN ABS(({base}{adjust}) - (paid+outstanding)) <= ? THEN 1 ELSE 0 END), 0) "
        "FROM orders WHERE paid IS NOT NULL AND outstanding IS NOT NULL", (AMOUNT_TOLERANCE,)).fetchone()
    if both and agree < TOTAL_AGREE_SHARE * both:
        report["order_total_skipped"] = "the sheet's paid and outstanding amounts do not add up to the derived total"
        return derived
    dst.execute(f"UPDATE orders SET total_due={base}{adjust} WHERE {base} IS NOT NULL")
    report["order_total_derived"] = dst.execute("SELECT COUNT(total_due) FROM orders").fetchone()[0]
    derived.append(("total_due", "derived_order_total",
                    "order total (rulebook section 4): goods total less order discount plus delivery fee"))
    return derived


def _register_derived(dst: sqlite3.Connection, derived: list[tuple[str, str, str]]) -> None:
    """column_catalog rows (derived=1) for the columns the layer computed rather than read."""
    for name, role, description in derived:
        distinct = dst.execute(f"SELECT COUNT(DISTINCT {q(name)}) FROM orders").fetchone()[0]
        samples = [row[0] for row in dst.execute(
            f"SELECT DISTINCT {q(name)} FROM orders WHERE {q(name)} IS NOT NULL LIMIT {CATALOG_SAMPLES}")]
        dst.execute(
            "INSERT INTO column_catalog (table_name, column_name, source_table, source_header, role, value_type, "
            "distinct_count, sample_values, join_key_to, derived) VALUES ('orders', ?, NULL, ?, ?, 'number', ?, ?, NULL, 1)",
            (name, description, role, distinct, json.dumps(samples, ensure_ascii=False)))


def _order_key_map(dst: sqlite3.Connection) -> tuple[set[str], dict[int, str]]:
    """The order ids, and the order each number names: a number maps to an order id only when exactly one order id
    ends in it (read as an integer), so that a note's "<label> n" never picks between two orders."""
    ids = {str(row[0]) for row in dst.execute("SELECT order_id FROM orders")}
    by_number: dict[int, set[str]] = {}
    for order_id in ids:
        found = TRAILING_DIGITS.search(order_id)
        if found:
            by_number.setdefault(int(found.group(1)), set()).add(order_id)
    return ids, {number: next(iter(found)) for number, found in by_number.items() if len(found) == 1}


def _resolve_key(cell: Any, ids: set[str], by_number: dict[int, str], from_note: bool) -> str | None:
    text = str(cell).strip()
    if text in ids:
        return text
    if not from_note:
        return None
    numbers = KEY_NUMBER.findall(text)  # a note naming one number: "<label> n"
    return by_number.get(int(numbers[0])) if len(numbers) == 1 else None


def _ledger_candidates(annotated: list[tuple[TableMeta, dict[str, Any]]], dst: sqlite3.Connection
                       ) -> list[tuple[TableMeta, dict[str, str], dict[str, Any]]]:
    """Tables the layer does not read as payments that give an amount paid: (table, roles, role of each column)."""
    found = []
    for table, note in annotated:
        if note["kind"] in LAYER_KINDS:
            continue
        by_column = _given_by_column(table, note.get("roles") or {})[0]
        if sum(1 for role in by_column.values() if role == "paid_amount") == 1:  # two such columns are ambiguous
            found.append((table, effective_roles(table, note, dst, None), by_column))
    return found


def _eligible_unpaid_orders(dst: sqlite3.Connection) -> set[str]:
    """Orders the debt rules count (rulebook section 4): not cancelled, not a payment method the rulebook says is
    never customer debt."""
    params = rulebook_params.active()
    out: set[str] = set()
    for order_id, method in dst.execute("SELECT order_id, payment_method FROM orders WHERE COALESCE(status,'')<>'cancelled'"):
        padded = f" {normalize_lookup(method)} " if method is not None else ""
        if not any(f" {normalize_lookup(token)} " in padded for token in params.non_debt_method_tokens):
            out.add(str(order_id))
    return out


def _load_ledger_payments(dst: sqlite3.Connection, context: BusinessContext,
                          annotated: list[tuple[TableMeta, dict[str, Any]]], report: dict[str, Any]) -> int:
    """payments(order_id, date, amount) from the amount-paid column of a side ledger (debts, instalments).

    The compiler reads payments as the whole record of money received: an order with no payment row owes its total.
    A ledger is therefore loaded only when it is the whole record and nothing about it is ambiguous:
    * one amount column; payments is otherwise empty (no second source to double count);
    * the order key resolves for LEDGER_KEY_SHARE of the keyed rows: the order-id column, else a note naming one
      number ("<label> n") that identifies exactly one order; an unresolved row is not loaded;
    * every row has a payment date: a paid_at column, or the ledger's date column when it falls after the order for
      most rows and never before (a date equal to the order's date is the sale, not a payment);
    * the amount column is not the order price (it equals the order total for most rows);
    * the ledger holds every order the debt rules count, and where orders carry an amount paid it equals the ledger.
    Otherwise payments stays empty and report["ledger_payments_skipped"] says why. Money is checked: the rows
    loaded sum to the amounts of the resolved ledger rows, once per order row (no row is multiplied)."""
    if dst.execute("SELECT 1 FROM payments LIMIT 1").fetchone():
        return 0
    candidates = _ledger_candidates(annotated, dst)
    if not candidates:
        return 0
    ids, by_number = _order_key_map(dst)
    skipped: list[str] = []
    loaded = 0
    for table, roles, by_column in candidates:
        amount_column = roles.get("paid_amount")
        if not amount_column:
            continue
        reason = None
        rows = dst.execute(f"SELECT * FROM source.{q(table.name)} AS src").fetchall()
        names = [row[1] for row in dst.execute(f"PRAGMA source.table_info({q(table.name)})")]
        index = {name: i for i, name in enumerate(names)}
        locale = _column_locale(dst, table, amount_column)
        scale = _amount_scale(context, table)
        keyed: list[tuple[str, Any, Any]] = []  # (order id, date cell, amount) of each resolved row
        best = None
        for column, from_note in ((roles.get("order_id"), False), (roles.get("order_note"), True),
                                  (roles.get("note"), True)):
            if not column or column not in index:
                continue
            cells = [row[index[column]] for row in rows if not blank(row[index[column]])]
            resolved = [_resolve_key(cell, ids, by_number, from_note) for cell in cells]
            share = sum(1 for key in resolved if key) / len(cells) if cells else 0.0
            if share >= LEDGER_KEY_SHARE and (best is None or share > best[0]):
                best = (share, column, from_note)
        if best is None:
            reason = "no column resolves to the orders"
        else:
            _, key_column, from_note = best
            for row in rows:
                if blank(row[index[key_column]]):
                    continue
                key = _resolve_key(row[index[key_column]], ids, by_number, from_note)
                amount = _number(row[index[amount_column]], locale or "unknown")
                if key is not None and amount is not None:
                    keyed.append((key, row, amount * scale))
        date_column = None
        if reason is None:
            dates = [roles.get(role) for role in ("paid_at", "ordered_at") if roles.get(role) in index]
            order_day = dict(dst.execute("SELECT order_id, date FROM orders"))
            for column in dates:
                days = [(to_date(row[index[column]]), order_day.get(key)) for key, row, _ in keyed]
                if any(day is None or ordered is None for day, ordered in days):
                    continue
                if any(day < ordered for day, ordered in days):
                    continue
                later = sum(1 for day, ordered in days if day > ordered)
                if column == roles.get("paid_at") or (days and later >= LEDGER_LATER_SHARE * len(days)):
                    date_column = column
                    break
            if date_column is None:
                reason = "no payment date"
        if reason is None:
            totals = dict(dst.execute("SELECT order_id, COALESCE(total_due, revenue) FROM orders"))
            priced = sum(1 for key, _, amount in keyed
                         if totals.get(key) is not None and abs(amount - totals[key]) <= AMOUNT_TOLERANCE)
            if keyed and priced >= LEDGER_PRICE_SHARE * len(keyed):
                reason = "the amount column is the order price"
        if reason is None:
            covered = {key for key, _, _ in keyed}
            missing = _eligible_unpaid_orders(dst) - covered
            if missing:
                reason = "the ledger does not hold every order the debt rules count"
        if reason is None:
            paid = dict(dst.execute("SELECT order_id, paid FROM orders WHERE paid IS NOT NULL"))
            sums: dict[str, float] = {}
            for key, _, amount in keyed:
                sums[key] = sums.get(key, 0.0) + amount
            checked = [key for key in sums if key in paid]
            if checked and any(abs(paid[key] - sums[key]) > AMOUNT_TOLERANCE for key in checked):
                reason = "the amount paid on the orders differs from the ledger"
        if reason is not None:
            skipped.append(f"{table.name}: {reason}")
            continue
        dst.executemany("INSERT INTO payments (order_id, date, amount) VALUES (?,?,?)",
                        [(key, to_date(row[index[date_column]]), amount) for key, row, amount in keyed])
        check = dst.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payments").fetchone()
        if check[0] != len(keyed) or abs(check[1] - sum(amount for _, _, amount in keyed)) > 1e-6:
            raise RuntimeError(f"ledger payments of {table.name} do not match the source rows")
        report["ledger_payments_table"] = table.name
        loaded = len(keyed)
        break  # one ledger is the record; a second would be a second source
    if skipped:
        report["ledger_payments_skipped"] = skipped
    return loaded


def _fill_customer_names(dst: sqlite3.Connection) -> None:
    """An order with a customer id and no name takes the name the customer catalog gives that id."""
    dst.execute(
        f"UPDATE orders SET customer_name={_catalog_name('customer_id', 'orders.customer_id')} "
        "WHERE customer_name IS NULL AND customer_id IS NOT NULL"
    )


def _fill_customer_groups(dst: sqlite3.Connection) -> None:
    """An order with no group takes its customer's group from a customer catalog: by id when the order
    has an id, by normalized name when it has none (two codes sharing a name are two customers).

    The catalog rows of every sheet are one list. A key that the catalog gives two different groups (the same id or
    name in two sheets, say) takes no group: it is never resolved by picking one of them."""
    dst.execute(
        "UPDATE orders SET customer_group = COALESCE("
        "(SELECT CASE WHEN COUNT(DISTINCT m.customer_group)=1 THEN MIN(m.customer_group) END "
        " FROM temp.customer_groups m "
        " WHERE orders.customer_id IS NOT NULL AND m.customer_id = orders.customer_id), "
        "(SELECT CASE WHEN COUNT(DISTINCT m.customer_group)=1 THEN MIN(m.customer_group) END "
        " FROM temp.customer_groups m "
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
    service_fees = ",".join(_sql_text(name) for name in rulebook_params.active().service_fee_names)
    extra = "".join(f", l.{q(name)}" for name, _ in _extra_columns(dst, "raw_lines"))  # each line's own values
    raw_columns = {row[1] for row in dst.execute("PRAGMA table_info(raw_lines)")}
    identity_unit = "l._qty_base_identity_unit" if "_qty_base_identity_unit" in raw_columns else "NULL"
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
               COALESCE({_product_fact('base_unit')}, {identity_unit}) AS base_unit,
               {_product_fact('prescription', 'temp.product_attrs')} AS prescription,
               {_product_fact('active_ingredient', 'temp.product_attrs')} AS active_ingredient,
               {_product_fact('product_kind', 'temp.product_attrs')} AS product_kind{extra}
        FROM raw_lines l JOIN orders o USING(order_id)
        WHERE l.has_product=1 AND l.line_kind NOT IN ({not_revenue})
          AND (l.product_name IS NOT NULL OR l.product_id IS NOT NULL);
        CREATE INDEX lines_date ON lines(date);
        CREATE TABLE status_domain AS SELECT DISTINCT status FROM orders WHERE status IN ('{status}');
        """
    )
