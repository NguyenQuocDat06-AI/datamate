"""Check the model's column roles against the values of the shop's own source tables, before the layer is built.

The annotation model names a role for each column from its header and a few examples. A role can be wrong in a way
the values show: a date role on a column that holds no dates, an order id on a table that shares no order with the
shop's orders, a "base unit" column that holds the large units of a conversion table. This step only removes or
swaps a role when the values give strong evidence; any doubt keeps the annotation as it is. Every change is
returned as a short dict (and build puts the list in report["annotation_fixes"]).

A lone paid_amount column is never judged by order totals: a payment table of fully paid orders holds the same numbers
as the totals, and the values cannot tell the two readings apart. Only a table with a second paid_amount column that
does not look like order totals loses the total-like one.

No table, column or value of any dataset is named here: the checks read structure (parse rates, shared keys,
numeric ranges), and the role names are the annotation's own vocabulary.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from bizreason.contracts import TableMeta
from bizreason.utils import normalize_lookup, quote_identifier as q

Annotated = list[tuple[TableMeta, dict[str, Any]]]

# Roles whose cells are dates, quantities or money. A role that is not listed is never judged by its values.
DATE_ROLES = ("ordered_at", "purchased_at", "paid_at", "delivered_at", "expires_at")
QUANTITY_ROLES = ("qty", "qty_base", "qty_purchased", "delivery_qty")
MONEY_ROLES = ("unit_price", "line_amount", "line_discount", "line_cost", "order_goods_total", "order_total_due",
               "order_paid", "order_outstanding", "order_discount", "shipping_fee", "purchase_unit_cost",
               "purchase_line_amount", "purchase_total", "paid_amount")

# A column keeps its date / numeric role when at least this share of its non-blank cells parses as one.
ROLE_MIN_PARSE_SHARE = 0.5  # below half parsed: the role is wrong; a few bad cells in a real date/number column are kept
# ... and is judged only when it has at least this many non-blank cells (a tiny sample proves nothing).
ROLE_MIN_CELLS = 5
# An order id on a table that is not one of the order-linked kinds must share this share of its distinct values
# with the order ids of the shop's sales tables, or the role is dropped.
ORDER_ID_MIN_SHARE = 0.5
# Kinds whose rows legitimately belong to orders (or to lots of an order): their order id is never judged here.
ORDER_LINKED_KINDS = ("sales", "service_ticket", "payment", "delivery", "sales_return", "lot")
ORDER_SOURCE_KIND = "sales"
# A unit column of a conversion table is swapped from base to large unit when every factor is a plain number
# above this value and the units are confirmed as large (see _large_unit_evidence).
LARGE_FACTOR_ABOVE = 1.0
PLAIN_NUMBER = re.compile(r"\s*\d+(?:[.,]\d+)?\s*")
# A real key to the orders spreads over the orders it names: its distinct values reach at least this share of
# min(rows with a value, order ids known). A small-domain numeric column (a count of periods, a number of days) that
# happens to match a few order ids reaches far less. Only a column with at most ORDER_KEY_SMALL_DOMAIN distinct values
# is judged this way: a real key to a large shop's orders is never that small.
ORDER_KEY_MIN_SPREAD = 0.1
ORDER_KEY_SMALL_DOMAIN = 50
# ... and each of its values sits on at least this many rows on average (a key to a few orders is not repeated so much).
ORDER_KEY_MIN_REPEAT = 20
# A paid_amount column whose value is at least the goods total of the joined order (within this relative margin) on
# at least this share of its joined rows holds order totals, not payments; it is judged only with this many joined rows
# and when at least this share of its rows joined an order.
PAID_TOTAL_MARGIN = 0.005
PAID_TOTAL_MIN_SHARE = 0.8
PAID_TOTAL_MIN_JOINED = 5
PAID_TOTAL_MIN_JOIN_SHARE = 0.5


def _text(column: str | None) -> str:
    return f"NULLIF(TRIM(CAST(src.{q(column)} AS TEXT)),'')" if column else "NULL"


def _cells(dst: sqlite3.Connection, table: TableMeta, column: str) -> list[str]:
    return [row[0] for row in dst.execute(
        f"SELECT {_text(column)} FROM source.{q(table.name)} AS src WHERE {_text(column)} IS NOT NULL")]


def _parse_share(dst: sqlite3.Connection, table: TableMeta, column: str, role: str) -> tuple[float, int] | None:
    """(share of non-blank cells that parse as the role's kind, number of such cells), or None for an unjudged role."""
    from bizreason.semantic import layer  # layer imports this module; it is loaded by the time a check runs
    if role in DATE_ROLES:
        parse = layer.to_date
    elif role in QUANTITY_ROLES:
        locale = layer._column_locale(dst, table, column)
        parse = (lambda v: layer._number(v, locale, unit=None)) if locale else layer.to_quantity
    elif role in MONEY_ROLES:
        locale = layer._column_locale(dst, table, column)
        parse = (lambda v: layer._number(v, locale)) if locale else layer.to_number
    else:
        return None
    cells = _cells(dst, table, column)
    if not cells:
        return None
    return sum(parse(cell) is not None for cell in cells) / len(cells), len(cells)


def _order_columns(dst: sqlite3.Connection, annotated: Annotated) -> list[tuple[TableMeta, str]]:
    """(table, order id column) of the shop's sales tables that carry orders: the reference the order ids of other
    tables are matched against (the same reading as layer._reread_lot_sales)."""
    from bizreason.semantic import layer
    found = []
    for table, note in annotated:
        if note.get("kind") != ORDER_SOURCE_KIND:
            continue
        roles = layer.effective_roles(table, note, dst)
        if roles.get("order_id") and (roles.get("product_name") or roles.get("product_id")
                                      or any(roles.get(role) for role in layer.ORDER_SIGNAL_ROLES)):
            found.append((table, roles["order_id"]))
    return found


def _large_unit_evidence(dst: sqlite3.Connection, table: TableMeta, unit_column: str, factor_column: str,
                         product_column: str | None, annotated: Annotated) -> str | None:
    """Why the unit column of a conversion table holds large units rather than base units, or None.

    Needed first: every factor is a plain number above 1 (a base unit has no factor above 1 to a smaller unit; a
    cell such as "15 box" is a chain factor and stops the check). Then one of
    - the same product has several unit rows with different factors: they cannot all be its base unit;
    - every unit of the column is a unit that sale lines of the shop count in a quantity differing from its base
      quantity (qty_base != qty): a large unit, by how the shop's own sales use it."""
    from bizreason.semantic import layer
    rows = dst.execute(
        f"SELECT {_text(unit_column)}, {_text(factor_column)}, {_text(product_column)} "
        f"FROM source.{q(table.name)} AS src").fetchall()
    rows = [(u, f, p) for u, f, p in rows if u is not None and f is not None]
    if len(rows) < 2:
        return None
    factors = []
    for _, cell, _ in rows:
        if not PLAIN_NUMBER.fullmatch(cell):
            return None
        factors.append(float(cell.strip().replace(",", ".")))
    if min(factors) <= LARGE_FACTOR_ABOVE:
        return None
    per_product: dict[str, set[tuple[str, float]]] = {}
    for (unit, _, product), factor in zip(rows, factors):
        if product is not None:
            per_product.setdefault(product, set()).add((normalize_lookup(unit), factor))
    if any(len({u for u, _ in pairs}) >= 2 and len({f for _, f in pairs}) >= 2 for pairs in per_product.values()):
        return "one product has several units with different factors"
    large: set[str] = set()
    for sales, note in annotated:
        if note.get("kind") != "sales":
            continue
        roles = layer.effective_roles(sales, note, dst)
        if not (roles.get("unit") and roles.get("qty") and roles.get("qty_base")):
            continue
        for (unit,) in dst.execute(
                f"SELECT DISTINCT lookup({_text(roles['unit'])}) FROM source.{q(sales.name)} AS src "
                f"WHERE to_qty({_text(roles['qty'])}) IS NOT NULL AND to_qty({_text(roles['qty_base'])}) IS NOT NULL "
                f"AND to_qty({_text(roles['qty'])}) != to_qty({_text(roles['qty_base'])})"):
            if unit:
                large.add(unit)
    units = {normalize_lookup(u) for u, _, _ in rows}
    if units and units <= large:
        return "every unit is counted in a quantity differing from its base quantity on the shop's sale lines"
    return None


def _order_key_spread(dst: sqlite3.Connection, table: TableMeta, column: str,
                      order_columns: list[tuple[TableMeta, str]]) -> tuple[float, int] | None:
    """(distinct values / min(rows with a value, order ids known), distinct values, rows with a value) for a column
    that should name orders, or None when it cannot be judged (no order ids, no values)."""
    if not order_columns:
        return None
    rows, distinct = dst.execute(
        f"SELECT COUNT({_text(column)}), COUNT(DISTINCT {_text(column)}) FROM source.{q(table.name)} AS src").fetchone()
    union = " UNION ".join(
        f"SELECT NULLIF(TRIM(CAST(k.{q(col)} AS TEXT)),'') AS v FROM source.{q(other.name)} AS k"
        for other, col in order_columns)
    known = dst.execute(f"SELECT COUNT(v) FROM ({union})").fetchone()[0]
    denominator = min(rows, known)
    return (distinct / denominator, distinct, rows) if denominator and distinct else None


def join_key_verdict(dst: sqlite3.Connection, target: str, column: str, min_share: float) -> tuple[bool, str]:
    """Whether a layer column (in table `target`, orders already loaded) is a key to orders.order_id, with the reason.

    Needs both: at least min_share of its distinct values are order ids, and its values are spread like a key (see
    ORDER_KEY_MIN_SPREAD): a small-domain numeric column that matches a few order ids is not a key."""
    total, hits = dst.execute(
        f"SELECT COUNT(__value), SUM(CASE WHEN __value IN (SELECT order_id FROM orders) THEN 1 ELSE 0 END) "
        f"FROM (SELECT DISTINCT {q(column)} AS __value FROM {q(target)})").fetchone()
    hits = hits or 0
    if not total or hits < min_share * total:
        return False, f"{hits} of {total} distinct values are order ids"
    rows = dst.execute(f"SELECT COUNT({q(column)}) FROM {q(target)}").fetchone()[0]
    orders = dst.execute("SELECT COUNT(DISTINCT order_id) FROM orders").fetchone()[0]
    denominator = min(rows, orders)
    if (total <= ORDER_KEY_SMALL_DOMAIN and denominator and total / denominator < ORDER_KEY_MIN_SPREAD
            and rows >= ORDER_KEY_MIN_REPEAT * total):
        return False, f"only {total} distinct values over {rows} rows and {orders} orders: not spread like a key"
    return True, f"{hits} of {total} distinct values are order ids"


def _order_goods_totals(dst: sqlite3.Connection, order_columns: list[tuple[TableMeta, str]],
                        annotated: Annotated) -> dict[str, float]:
    """Order id (trimmed text) to the sum of its sale lines' amounts, over the shop's sales tables."""
    from bizreason.semantic import layer
    totals: dict[str, float] = {}
    by_name = {table.name: note for table, note in annotated}
    for table, order_column in order_columns:
        roles = layer.effective_roles(table, by_name[table.name], dst)
        amount = roles.get("line_amount")
        if not amount:
            continue
        locale = layer._column_locale(dst, table, amount)
        parse = (lambda v: layer._number(v, locale)) if locale else layer.to_number
        for order, cell in dst.execute(
                f"SELECT {_text(order_column)}, {_text(amount)} FROM source.{q(table.name)} AS src"):
            value = parse(cell) if cell is not None else None
            if order is not None and value is not None:
                totals[order] = totals.get(order, 0.0) + value
    return totals


def _order_total_evidence(dst: sqlite3.Connection, table: TableMeta, column: str, order_column: str,
                          totals: dict[str, float]) -> str | None:
    """Why the column holds order totals rather than payments (its value is at least the goods total of the order on
    its row, on most rows), or None."""
    from bizreason.semantic import layer
    locale = layer._column_locale(dst, table, column)
    parse = (lambda v: layer._number(v, locale)) if locale else layer.to_number
    rows = dst.execute(f"SELECT {_text(order_column)}, {_text(column)} FROM source.{q(table.name)} AS src "
                       f"WHERE {_text(column)} IS NOT NULL").fetchall()
    joined = at_least = 0
    for order, cell in rows:
        value = parse(cell)
        total = totals.get(order) if order is not None else None
        if value is None or total is None or total <= 0:
            continue
        joined += 1
        at_least += value >= total * (1 - PAID_TOTAL_MARGIN)
    if (joined < PAID_TOTAL_MIN_JOINED or joined < PAID_TOTAL_MIN_JOIN_SHARE * len(rows)
            or at_least < PAID_TOTAL_MIN_SHARE * joined):
        return None
    return f"{at_least} of {joined} joined rows are at least the goods total of their order"


def _set_role(table: TableMeta, roles: dict[str, Any], column: str, old: str, new: str) -> dict[str, Any] | None:
    """The roles with the key that names column (same compact spelling) moved from old to new, or None when no
    key names it (then the annotation is left alone)."""
    from bizreason.semantic import layer
    target = layer._compact_key(column)
    keys = [key for key, role in roles.items() if role == old and layer._compact_key(key) == target]
    if not keys:
        return None
    return {key: (new if key in keys else role) for key, role in roles.items()}


def check_annotations(dst: sqlite3.Connection, annotated: Annotated) -> tuple[Annotated, list[dict[str, Any]]]:
    """Annotated tables with the roles the source values contradict removed or swapped, and the list of changes.

    dst has the shop's source attached as `source` and the layer's SQL functions registered (see layer._register)."""
    from bizreason.semantic import layer
    order_columns = _order_columns(dst, annotated)
    fixes: list[dict[str, Any]] = []
    totals: dict[str, float] | None = None  # read on the first paid_amount column to judge
    result: Annotated = []
    for table, note in annotated:
        roles = note.get("roles") or {}
        given, _ = layer._given_by_column(table, roles)
        updated = roles
        for column, role in given.items():
            if not layer._is_role(role):
                continue
            new_role, why = None, None
            if role in (*DATE_ROLES, *QUANTITY_ROLES, *MONEY_ROLES):
                judged = _parse_share(dst, table, column, role)
                if judged is not None and judged[1] >= ROLE_MIN_CELLS and judged[0] < ROLE_MIN_PARSE_SHARE:
                    new_role, why = "other", f"only {judged[0]:.0%} of {judged[1]} cells parse as {role}"
            elif role == "order_id":
                others = [pair for pair in order_columns if pair[0].name != table.name]
                if note.get("kind") not in ORDER_LINKED_KINDS and others:
                    share = layer._matching_share(dst, table, column, others)
                    if share < ORDER_ID_MIN_SHARE:
                        new_role, why = "other", f"{share:.0%} of its values are order ids of the sales tables"
                if new_role is None and note.get("kind") not in ORDER_LINKED_KINDS and others:
                    spread = _order_key_spread(dst, table, column, others)
                    if (spread and spread[1] <= ORDER_KEY_SMALL_DOMAIN and spread[0] < ORDER_KEY_MIN_SPREAD
                            and spread[2] >= ORDER_KEY_MIN_REPEAT * spread[1]):
                        new_role = "other"
                        why = (f"{spread[1]} distinct values spread over {spread[0]:.1%} of the orders it could "
                               f"name: a small-domain column, not an order key")
            elif role == "base_unit" and note.get("kind") == "unit_conversion":
                factor = next((c for c, r in given.items() if r == "conversion_factor"), None)
                product = next((c for c, r in given.items() if r in ("product_id", "product_name")), None)
                if factor and "large_unit" not in given.values():
                    why = _large_unit_evidence(dst, table, column, factor, product, annotated)
                    if why:
                        new_role = "large_unit"
            if new_role is None and role == "paid_amount":
                if totals is None:
                    totals = _order_goods_totals(dst, order_columns, annotated)
                order_column = next((c for c, r in given.items() if r == "order_id"), None)
                siblings = [c for c, r in given.items() if r == "paid_amount" and c != column]
                if order_column and totals and siblings:  # a lone paid column is never judged: see module note
                    evidence = {c: _order_total_evidence(dst, table, c, order_column, totals)
                                for c in (column, *siblings)}
                    # the column goes only while another paid column stays: the shop must keep some payment column
                    if evidence[column] and not all(evidence[c] for c in siblings):
                        new_role, why = "other", f"holds order totals, not payments: {evidence[column]}"
            if new_role is None:
                continue
            changed = _set_role(table, updated, column, role, new_role)
            if changed is None:
                continue
            updated = changed
            fixes.append({"table": table.name, "column": column, "from": role, "to": new_role, "why": why})
        result.append((table, note) if updated is roles else (table, {**note, "roles": updated}))
    return result, fixes
