"""Reading stock and lot questions from the sources the layer keeps (stock_ledger, lot_balance).

Pure functions on a read-only layer connection; the compiler calls them and decides what an unanswerable result means.
Nothing here names a shop's tables or columns: only the layer's own canonical tables are read.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Iterable


def _has_rows(db: sqlite3.Connection, table: str) -> bool:
    try:
        return db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:
        return False


def ledger_balance_on(db: sqlite3.Connection, product_names: Iterable[str], day: date) -> float | None:
    """The stock of the one product `product_names` stand for at the end of `day`, from the movement ledger.

    The balance is the one after the product's last movement dated up to the end of `day` (ties by order in the
    ledger). None when there is no ledger, when the names reach no product or more than one product, or when the
    product has no movement by then (it is not known whether it existed yet, so no zero is invented)."""
    names = sorted({name for name in product_names if name is not None})
    if not names or not _has_rows(db, "stock_ledger"):
        return None
    slots = ",".join("?" for _ in names)
    keys = {row[0] for row in db.execute(
        f"SELECT DISTINCT name_key FROM stock_ledger WHERE product_name IN ({slots})", names)}
    if len(keys) != 1:
        return None
    row = db.execute(
        "SELECT balance FROM stock_ledger WHERE name_key = ? AND ts < ? ORDER BY ts DESC, rowid DESC LIMIT 1",
        (next(iter(keys)), (day + timedelta(days=1)).isoformat())).fetchone()
    return None if row is None or row[0] is None else float(row[0])


def lot_expiry_range(window_start: date, window_end_exclusive: date, as_of: date, shape: str
                     ) -> tuple[date, date | None]:
    """(first expiry day, last expiry day or None) a lot question looks at.

    A window of no start, or one that is only the question's own day, states no expiry range for an
    'earliest expiry' question: the day names the date of the stock, so the lots are the ones expiring from that day
    on. Any other window is the stated expiry range."""
    if window_start == date.min:
        return as_of, None
    one_day = window_end_exclusive - window_start == timedelta(days=1)
    if shape == "argmax_date" and one_day and window_start == as_of:
        return as_of, None
    return window_start, window_end_exclusive - timedelta(days=1)


def quantity_answer(qty: float, factor: float | None, split: bool) -> tuple[str, int | list[int]] | None:
    """A stock quantity as an answer: ('number', count) or, for a split, ('list', [whole large units, loose units]).
    None when the quantity is not a whole non-negative count, or a split is asked and there is no large unit."""
    if qty < 0 or float(qty) != int(qty):
        return None
    if not split:
        return "number", int(qty)
    if factor is None or float(factor) != int(factor) or int(factor) < 2:
        return None
    whole, loose = divmod(int(qty), int(factor))
    return "list", [whole, loose]


def lot_report_mismatches(db: sqlite3.Connection) -> tuple[int, int]:
    """(products compared, products that differ): the stock report's quantity of each product against the sum of
    its lot balances. Only products present in both are compared; a product with no lot is not a disagreement."""
    try:
        rows = db.execute(
            "SELECT s.qty_base, l.total FROM stock s JOIN (SELECT name_key, SUM(balance) AS total FROM lot_balance "
            "GROUP BY name_key) l ON l.name_key = s.name_key WHERE s.qty_base IS NOT NULL").fetchall()
    except sqlite3.OperationalError:
        return 0, 0
    return len(rows), sum(1 for qty, total in rows if abs(float(qty) - float(total)) > 1e-6)


def drop_lots_if_disagreeing(db: sqlite3.Connection) -> int:
    """Forget the lot balances when they do not reproduce the stock report they were built beside: a lot reading that
    cannot explain the report's own quantities is not trusted for expiry answers. Returns the products that differ
    (the lot balances are dropped when it is above zero)."""
    _, differing = lot_report_mismatches(db)
    if differing:
        db.execute("DELETE FROM lot_balance")
        db.execute("DELETE FROM stock_sources WHERE kind = 'lot_balance'")
    return differing


def dominant_quantity(numeric: dict[str, list[float | None]], candidates: list[str]) -> str | None:
    """The quantity sold from a lot, chosen by values when the roles given to the columns do not settle it.

    Of the numeric columns of a table of sales from lots, the sold quantity is the one that no other column ever
    exceeds in a row (a quantity returned to the lot cannot be larger than the quantity sold from it), and that is
    larger than each other column in at least one row. Exactly one candidate must qualify, else None."""
    def beats(first: str, second: str) -> bool:
        pairs = [(a, b) for a, b in zip(numeric[first], numeric[second]) if a is not None and b is not None]
        return bool(pairs) and all(b <= a for a, b in pairs) and any(b < a for a, b in pairs)

    if len(numeric) < 2:
        return None
    winners = [c for c in candidates if c in numeric and all(beats(c, other) for other in numeric if other != c)]
    return winners[0] if len(winners) == 1 else None


def has_no_stock_basis(db: sqlite3.Connection) -> bool:
    """True when the layer holds nothing a stock could rest on: no stock report, no movement ledger, no lot balance
    and no purchase receipts (so the stock cannot be rebuilt from receipts and sales either). A shop like that
    records no stock, and a stock question about it is unanswerable (rulebook section 7), not a failure to compute.
    A layer that lacks any of these tables is not judged: False."""
    try:
        if db.execute("SELECT 1 FROM stock_sources WHERE kind IN ('inventory', 'lot', 'lot_balance') LIMIT 1").fetchone():
            return False
        # A table nobody read (or read as other) may hold the stock or lots: the shop stays undecided.
        if db.execute("SELECT COALESCE(SUM(rows), 0) FROM stock_sources WHERE kind = 'unannotated'").fetchone()[0]:
            return False
        return not any(_has_rows(db, table) for table in ("stock", "stock_ledger", "purchases")) \
            and all(_exists(db, table) for table in ("stock", "stock_ledger", "purchases"))
    except sqlite3.OperationalError:
        return False


def _exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def is_expiry_range_list(intent, window_start: date, window_end_exclusive: date) -> bool:
    """A 'which products are in stock' list over a stated range of days, read as the list of products with a lot in
    stock expiring in that range. A stock is a point in time, so a range of days beside a plain stock list can only
    be a range of expiry dates. Needs the list shape, the plain stock quantity measure, no filter, grouping, second
    measure or threshold, and a window of at least two days."""
    return (intent.shape == "list_filter" and intent.measure == "inventory_qty" and not intent.filters
            and intent.measure2 is None and intent.group_by is None and intent.threshold is None
            and window_start != date.min and window_end_exclusive - window_start > timedelta(days=1))
