"""What a shop records about customers and about settled money (rulebook §3, §4, §7).

Two questions the compiler asks of the canonical layer, answered here from structure only (column roles, filled
cells, the shop's own README), never from a column or table name of one dataset:

* `customers_unrecorded`: no order, no other table and no README line names a customer. A question about individual
  customers is then unanswerable (rulebook §7: the shop does not record that kind of information), which differs from
  a shop that records customers in a place the order table does not carry (that stays an abstention).
* `payments_unrecorded`: no payment table, no per-order paid / still-owed amount, no column with such a role and no
  README line about money received. A debt question is then unanswerable.
* `order_state_settlements`: a shop with no payment table whose orders carry the amount payable and a state of
  settlement taken when the data was exported (an amount still owed, or an amount paid) gets one settled amount per
  order. It is valid only for a cutoff at or after the data's last day (`cutoff_covers_data_end`).

Every function reads the layer only and returns a plain value; the compiler decides how to raise.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import date, timedelta
from typing import Any

# Column roles that name a customer, wherever they sit in the layer.
CUSTOMER_ROLE_MARKER = "customer"
CUSTOMER_ORDER_COLUMNS = ("customer_key", "customer_id", "customer_name", "customer_group")
# README words for a customer (matched on accent-free lower case text).
CUSTOMER_README_TOKENS = ("khach", "customer", "client")

# Roles that carry money received or still owed, and the markers that make any other role count as one.
PAYMENT_EVIDENCE_ROLES = frozenset({"paid_amount", "paid_at", "order_total_due", "order_paid", "order_outstanding",
                                    "total_due", "paid", "outstanding"})
PAYMENT_ROLE_MARKERS = ("paid", "outstanding", "debt", "balance", "deposit", "settle", "repay")
# A role that only describes how or when an order is paid, never an amount.
PAYMENT_ROLE_NON_AMOUNT = frozenset({"payment_method", "due_at", "credit_days", "order_payment_method"})
PAYMENT_ORDER_COLUMNS = ("total_due", "paid", "outstanding")
# README words for money received or still owed (accent-free lower case).
PAYMENT_README_TOKENS = ("da tra", "da thu", "da thanh toan", "con no", "con thieu", "con phai thu", "so tien no",
                         "du no", "cong no", "tien da", "khach tra", "khach da tra", "thu no", "tra no",
                         "amount paid", "paid amount", "outstanding", "balance due")


STROKED_D = chr(0x111)  # the one Vietnamese letter that does not decompose into base plus accent


def _fold(text: Any) -> str:
    text = unicodedata.normalize("NFD", str(text or "").lower().replace(STROKED_D, "d"))
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _any_filled(db: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        return db.execute(f"SELECT 1 FROM {table} WHERE {column} IS NOT NULL LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def _catalog_roles(db: sqlite3.Connection) -> list[str]:
    try:
        return [str(row[0] or "") for row in db.execute("SELECT role FROM column_catalog")]
    except sqlite3.Error:
        return []


def _readme(db: sqlite3.Connection) -> str:
    try:
        rows = db.execute("SELECT text FROM shop_docs").fetchall()
    except sqlite3.Error:
        return ""
    return _fold(" ".join(str(row[0] or "") for row in rows))


def customers_unrecorded(db: sqlite3.Connection) -> bool:
    """True when nothing in the shop's data or README names a customer.

    Conservative: any filled customer column on orders, any customer-role column in any table (a customer file
    the order table does not join), or any customer word in the README means the shop may record customers
    somewhere, and the question is left to abstain."""
    if "orders" not in _tables(db):
        return False
    for column in _columns(db, "orders") & set(CUSTOMER_ORDER_COLUMNS):
        if _any_filled(db, "orders", column):
            return False
    if any(CUSTOMER_ROLE_MARKER in role for role in _catalog_roles(db)):
        return False
    readme = _readme(db)
    if not readme:
        return False  # no README to confirm the absence: do not claim it
    return not any(re.search(rf"\b{token}", readme) for token in CUSTOMER_README_TOKENS)


def _payment_roles(db: sqlite3.Connection) -> bool:
    for role in _catalog_roles(db):
        if role in PAYMENT_ROLE_NON_AMOUNT:
            continue
        if role in PAYMENT_EVIDENCE_ROLES or any(marker in role for marker in PAYMENT_ROLE_MARKERS):
            return True
    return False


def payments_unrecorded(db: sqlite3.Connection) -> bool:
    """True when the shop records no money received and no amount still owed, anywhere."""
    tables = _tables(db)
    if "orders" not in tables or "payments" not in tables:
        return False
    if db.execute("SELECT 1 FROM payments LIMIT 1").fetchone() is not None:
        return False
    for column in _columns(db, "orders") & set(PAYMENT_ORDER_COLUMNS):
        if _any_filled(db, "orders", column):
            return False
    if _payment_roles(db):
        return False
    readme = _readme(db)
    if not readme:
        return False
    return not any(token in readme for token in PAYMENT_README_TOKENS)


def order_state_settlements(db: sqlite3.Connection) -> dict[Any, list[tuple[Any, float]]] | None:
    """Settled amount per order from the export-time state kept on the order, or None when the orders do not carry
    enough to tell. Shape matches the payment index: order id -> [(date, amount)], dated on the order day.

    Needs the amount payable on every order that has a state, and a state on every such order: either an amount still
    owed (a missing one means nothing owed) or an amount paid (a missing one is unknown, so the shop is not used)."""
    columns = _columns(db, "orders")
    if "total_due" not in columns:
        return None
    if "outstanding" in columns and _any_filled(db, "orders", "outstanding"):
        rows = db.execute("SELECT order_id, date, total_due, COALESCE(outstanding, 0) FROM orders "
                          "WHERE total_due IS NOT NULL").fetchall()
        if not rows:
            return None
        return {oid: [(day, max(float(due) - float(owed), 0.0))] for oid, day, due, owed in rows}
    if "paid" in columns and _any_filled(db, "orders", "paid"):
        rows = db.execute("SELECT order_id, date, total_due, paid FROM orders WHERE total_due IS NOT NULL").fetchall()
        if not rows or any(paid is None for _, _, _, paid in rows):
            return None
        return {oid: [(day, min(max(float(paid), 0.0), float(due)))] for oid, day, due, paid in rows}
    return None


def cutoff_covers_data_end(db: sqlite3.Connection, end_exclusive: date) -> bool:
    """The state kept on the order is the state when the data ended: only a cutoff on or after the last day with
    orders may read it."""
    last = db.execute("SELECT MAX(date) FROM orders").fetchone()[0]
    if last is None:
        return False
    return (end_exclusive - timedelta(days=1)).isoformat() >= str(last)
