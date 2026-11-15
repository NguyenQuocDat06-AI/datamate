"""Check the model's column roles against the values of the shop's own source tables, before the layer is built.

The annotation model names a role for each column from its header and a few examples. A role can be wrong in a way
the values show: a date role on a column that holds no dates, an order id on a table that shares no order with the
shop's orders, a "base unit" column that holds the large units of a conversion table. This step only removes or
swaps a role when the values give strong evidence; any doubt keeps the annotation as it is. Every change is
returned as a short dict (and build puts the list in report["annotation_fixes"]).

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
            elif role == "order_id" and note.get("kind") not in ORDER_LINKED_KINDS:
                others = [pair for pair in order_columns if pair[0].name != table.name]
                if others:
                    share = layer._matching_share(dst, table, column, others)
                    if share < ORDER_ID_MIN_SHARE:
                        new_role, why = "other", f"{share:.0%} of its values are order ids of the sales tables"
            elif role == "base_unit" and note.get("kind") == "unit_conversion":
                factor = next((c for c, r in given.items() if r == "conversion_factor"), None)
                product = next((c for c, r in given.items() if r in ("product_id", "product_name")), None)
                if factor and "large_unit" not in given.values():
                    why = _large_unit_evidence(dst, table, column, factor, product, annotated)
                    if why:
                        new_role = "large_unit"
            if new_role is None:
                continue
            changed = _set_role(table, updated, column, role, new_role)
            if changed is None:
                continue
            updated = changed
            fixes.append({"table": table.name, "column": column, "from": role, "to": new_role, "why": why})
        result.append((table, note) if updated is roles else (table, {**note, "roles": updated}))
    return result, fixes
