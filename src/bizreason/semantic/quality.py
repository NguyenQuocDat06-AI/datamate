"""Data-quality flags: is a part of the canonical tables too broken to answer from?

Each flag is read from the build statistics table or from the canonical tables themselves. A missing table, column or
key is unknown, and an unknown flag never fires. Every reason starts with its flag name and a colon, so a caller can
tell which flag a reason belongs to.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# Calibrated on 14 shops, where none of these detectors fired on a correct answer and each fired on a data defect that
# made answers wrong. They must not be tuned on evaluation questions.
QTY_UNPARSED_SHARE = 0.005
MONEY_UNPARSED_SHARE = 0.005
NULL_STATUS_SHARE = 0.05
UNLINKED_PAYMENT_SHARE = 0.20
# Orders with a customer id but no customer group, while the shop gives groups to other orders. Measured on the
# shops with customer ids: none has an ungrouped share above zero.
UNGROUPED_CUSTOMER_SHARE = 0.05

# Build statistics keys, as the layer writes them.
QTY_NONBLANK = "qty_cells_nonblank"
QTY_UNPARSED = "qty_cells_unparsed"
MONEY_NONBLANK = "money_cells_nonblank"
MONEY_UNPARSED = "money_cells_unparsed"
ORDER_STRUCTURE_KEYS = (
    "product_rows_without_order", "annotation_keys_unresolved", "tables_skipped_missing_order_id",
)


@dataclass
class QualityFlags:
    qty_unreliable: bool
    money_unreliable: bool
    order_structure_broken: bool
    status_unreliable: bool
    payments_unlinked: bool
    customer_group_incomplete: bool
    reasons: list[str] = field(default_factory=list)


def _stat(connection: sqlite3.Connection, key: str) -> int | None:
    """A build statistic as a whole number, or None when it is absent, unreadable or negative."""
    try:
        row = connection.execute("SELECT value FROM build_stats WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        number = int(str(row[0]).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _unparsed_share(connection: sqlite3.Connection, nonblank_key: str, unparsed_key: str) -> float | None:
    nonblank = _stat(connection, nonblank_key)
    unparsed = _stat(connection, unparsed_key)
    if not nonblank or unparsed is None:
        return None
    return unparsed / nonblank


def _null_status_share(connection: sqlite3.Connection) -> float | None:
    try:
        total, nulls = connection.execute(
            "SELECT COUNT(*), SUM(CASE WHEN status IS NULL THEN 1 ELSE 0 END) FROM orders"
        ).fetchone()
    except sqlite3.Error:
        return None
    if not total:
        return None
    return (nulls or 0) / total


def _unlinked_payment_share(connection: sqlite3.Connection) -> float | None:
    """Share of dated orders whose order id is in no payment row. None when the shop has no payment rows."""
    try:
        payment_rows = connection.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        if not payment_rows:
            return None
        dated, unlinked = connection.execute(
            # A set lookup: a correlated subquery over an unindexed table is quadratic.
            "SELECT COUNT(*), SUM(CASE WHEN o.order_id NOT IN "
            "(SELECT order_id FROM payments WHERE order_id IS NOT NULL) THEN 1 ELSE 0 END) "
            "FROM orders o WHERE o.date IS NOT NULL"
        ).fetchone()
    except sqlite3.Error:
        return None
    if not dated:
        return None
    return (unlinked or 0) / dated


def _ungrouped_customer_share(connection: sqlite3.Connection) -> float | None:
    """Share of orders with a customer id that have no customer group. None when the shop gives no order a group, or
    when no order has a customer id."""
    try:
        with_id, ungrouped, grouped = connection.execute(
            "SELECT COUNT(CASE WHEN customer_id IS NOT NULL THEN 1 END), "
            "COUNT(CASE WHEN customer_id IS NOT NULL AND customer_group IS NULL THEN 1 END), "
            "COUNT(customer_group) FROM orders"
        ).fetchone()
    except sqlite3.Error:
        return None
    if not with_id or not grouped:
        return None
    return (ungrouped or 0) / with_id


def assess(connection: sqlite3.Connection) -> QualityFlags:
    reasons: list[str] = []

    qty = _unparsed_share(connection, QTY_NONBLANK, QTY_UNPARSED)
    qty_bad = qty is not None and qty > QTY_UNPARSED_SHARE
    if qty_bad:
        reasons.append(f"qty_unreliable: {qty:.2%} of quantity cells did not parse")

    money = _unparsed_share(connection, MONEY_NONBLANK, MONEY_UNPARSED)
    money_bad = money is not None and money > MONEY_UNPARSED_SHARE
    if money_bad:
        reasons.append(f"money_unreliable: {money:.2%} of amount cells did not parse")

    order_bad = False
    for key in ORDER_STRUCTURE_KEYS:
        count = _stat(connection, key)
        if count:
            order_bad = True
            reasons.append(f"order_structure_broken: {key} is {count}")

    nulls = _null_status_share(connection)
    status_bad = nulls is not None and nulls > NULL_STATUS_SHARE
    if status_bad:
        reasons.append(f"status_unreliable: {nulls:.2%} of orders have no status")

    unlinked = _unlinked_payment_share(connection)
    payments_bad = unlinked is not None and unlinked > UNLINKED_PAYMENT_SHARE
    if payments_bad:
        reasons.append(f"payments_unlinked: {unlinked:.2%} of dated orders have no payment")

    ungrouped = _ungrouped_customer_share(connection)
    group_bad = ungrouped is not None and ungrouped > UNGROUPED_CUSTOMER_SHARE
    if group_bad:
        reasons.append(f"customer_group_incomplete: {ungrouped:.2%} of orders with a customer id have no customer group")

    return QualityFlags(
        qty_unreliable=qty_bad,
        money_unreliable=money_bad,
        order_structure_broken=order_bad,
        status_unreliable=status_bad,
        payments_unlinked=payments_bad,
        customer_group_incomplete=group_bad,
        reasons=reasons,
    )
