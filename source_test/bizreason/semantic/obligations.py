"""Debt obligations read from side ledgers written as a first row plus follow-up rows (rulebook §4).

A shop with no payment table may keep its debts in a ledger whose block for one order is
* a first row: the order's date, and what is owed (an amount still owed, or a price less a deposit paid at the
  sale that is split into equal instalments); and
* follow-up rows: each one a repayment (an amount and the day it was paid); the first repayment may sit on the
  first row; the order is named in a key column, or in a note ("<label> n").
The rulebook says an instalment order is a debt order, so both kinds of ledger give the same three tables:

* `obligations(order_id, kind, amount_due, date, installment_count, installment_amount, due_day, source_table)`
* `obligation_events(order_id, date, amount, source_table)`: the repayments, by day
* `obligation_schedule(order_id, k, due_date, amount)`: instalment k, when it falls due, and its amount

Everything is read from the layer's own tables (the carried `src_*` tables and `column_catalog`) and from the
numbers in them; no column name, header word or value of one dataset is used. A ledger is built only when every
structural check below holds; otherwise nothing is built and `report["obligations"]["skipped"]` says why, so the
compiler keeps its old behaviour (an ambiguous ledger is never guessed at):
* the blocks are runs of one resolved order key, each order once;
* the order key resolves to an order for KEY_SHARE of the blocks;
* what is owed is one column of the first rows that the annotation reads as an amount still owed, or four number
  columns of the first rows with price - deposit = instalments x amount per instalment (within a tolerance);
* the repayment amount and its day are filled on the same rows, and every repayment has a day;
* what was repaid never exceeds what was owed (except for a few orders);
* the ledger's order day agrees with the order's own day, and a repayment is never dated before its order;
* the instalment schedule is built only when a due day of the month is found and the first repayments fall near
  the instalment dates under the month offset chosen from the data.
"""

from __future__ import annotations

import calendar
import json
import re
import sqlite3
from collections import defaultdict
from datetime import date
from statistics import median
from typing import Any

KEY_SHARE = 0.98  # share of blocks whose key resolves to exactly one order
HEAD_ONLY_SHARE = 0.95  # share of a column's filled cells that must sit on the first row of a block to be a head column
HEAD_COVERAGE = 0.9  # share of the blocks whose first row must fill a head column
NEAR_PAIR = 0.8  # agreement below PAIR_AGREEMENT but this close names the ledger's problem in the report
PAIR_AGREEMENT = 0.98  # repayment amount and day are filled on the same rows this often
MIN_BLOCKS = 5
MAX_ROWS = 500_000
UNPARSED_SHARE = 0.02  # cells of a used number column that failed to parse
FIT_SHARE = 0.95  # share of blocks that must satisfy an arithmetic check
OVERPAID_SHARE = 0.05  # share of blocks that may be repaid more than owed
DATE_AGREE_SHARE = 0.9  # share of blocks whose ledger order day equals the order's day
EARLY_EVENT_SHARE = 0.05  # share of repayments that may be dated before their order
MAX_INSTALLMENTS = 120
MONTH_OFFSETS = (1, 0)  # months from the order's month to the first instalment's month, in order of preference
OFFSET_MARGIN_DAYS = 10  # the chosen offset must fit the repayments this much better than the other one
MIN_SCHEDULE_FIT_BLOCKS = 5

# Roles (from the annotation) that say a column holds what is still owed, or an amount paid.
DUE_ROLES = frozenset({"order_outstanding", "outstanding"})
PAID_ROLES = frozenset({"paid_amount"})
ID_ROLES = frozenset({"order_id", "customer_id", "customer_name", "customer_group", "product_id", "note",
                      "order_note", "employee_id"})
QUIET_REASONS = frozenset({"no column names the orders", "no repayment amount and day filled together",
                          "too few rows", "too few blocks", "too many rows"})
ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PLAIN_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
KEY_NUMBER = re.compile(r"\d+")
TRAILING_DIGITS = re.compile(r"(\d+)\s*$")
INTEGER_TEXT = re.compile(r"^(\d+)(?:\.0+)?$")
DERIVED_COLUMNS = {
    "obligations": [
        ("order_id", "order_id", "the order that owes"),
        ("kind", "obligation_kind", "debt (owed after the sale) or installment (instalment plan)"),
        ("amount_due", "obligation_amount", "money owed after the sale: the amount still owed, or price less deposit"),
        ("date", "ordered_at", "day of the order"),
        ("installment_count", "installment_count", "number of instalments"),
        ("installment_amount", "installment_amount", "money per instalment"),
        ("due_day", "due_day", "day of the month an instalment falls due"),
    ],
    "obligation_events": [
        ("order_id", "order_id", "the order repaid"),
        ("date", "paid_at", "day of the repayment"),
        ("amount", "paid_amount", "money repaid"),
    ],
    "obligation_schedule": [
        ("order_id", "order_id", "the order"),
        ("k", "installment_index", "instalment number, from 1"),
        ("due_date", "due_at", "day the instalment falls due"),
        ("amount", "installment_amount", "money of the instalment"),
    ],
}


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def active(db: sqlite3.Connection) -> bool:
    """True when the layer holds obligations and no payment row, so the obligations are the record of repayments."""
    tables = _tables(db)
    if "obligations" not in tables or "obligation_events" not in tables:
        return False
    try:
        if "payments" in tables and db.execute("SELECT 1 FROM payments LIMIT 1").fetchone():
            return False
        return db.execute("SELECT 1 FROM obligations LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def has_schedule(db: sqlite3.Connection) -> bool:
    try:
        return active(db) and db.execute("SELECT 1 FROM obligation_schedule LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _plain_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and PLAIN_NUMBER.match(value.strip()):
        return float(value.strip())
    return None


class _Table:
    """The rows of one carried table, read through the typed companions the layer wrote (xn_ number, xd_ day)."""

    def __init__(self, db: sqlite3.Connection, name: str, catalog: dict[str, tuple[str, str, str | None]]):
        self.name = name
        self.names = [row[1] for row in db.execute(f'PRAGMA table_info("{name}")')]
        self.rows = [dict(zip(self.names, row)) for row in
                     db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
        companion = set(self.names)
        self.bases = [c for c in self.names if not c.startswith(("xn_", "xd_", "x_"))]
        self.catalog = {c: catalog[c] for c in self.bases if c in catalog}
        self.has_num = {c: f"xn_{c}" in companion for c in self.bases}
        self.has_day = {c: f"xd_{c}" in companion for c in self.bases}

    def role(self, column: str) -> str:
        return self.catalog.get(column, ("", "", None))[0] or ""

    def kind(self, column: str) -> str:
        return self.catalog.get(column, ("", "", None))[1] or ""

    def number(self, row: dict[str, Any], column: str) -> float | None:
        if self.has_num[column]:
            return _plain_number(row.get(f"xn_{column}"))
        return _plain_number(row.get(column))

    def day(self, row: dict[str, Any], column: str) -> str | None:
        value = row.get(f"xd_{column}") if self.has_day[column] else row.get(column)
        text = str(value).strip()[:10] if not _blank(value) else ""
        return text if ISO_DAY.match(text) else None


def _catalog(db: sqlite3.Connection) -> dict[str, dict[str, tuple[str, str, str | None]]]:
    out: dict[str, dict[str, tuple[str, str, str | None]]] = defaultdict(dict)
    try:
        rows = db.execute("SELECT table_name, column_name, role, value_type, join_key_to FROM column_catalog").fetchall()
    except sqlite3.Error:
        return out
    for table, column, role, kind, join in rows:
        out[table][column] = (role or "", kind or "", join)
    return out


def _order_keys(db: sqlite3.Connection) -> tuple[set[str], dict[int, str]]:
    """The order ids, and the order each number names: a number maps to an order only when exactly one order id
    ends in it, so a note's "<label> n" never picks between two orders."""
    ids = {str(row[0]) for row in db.execute("SELECT order_id FROM orders") if row[0] is not None}
    seen: dict[int, set[str]] = {}
    for order_id in ids:
        found = TRAILING_DIGITS.search(order_id)
        if found:
            seen.setdefault(int(found.group(1)), set()).add(order_id)
    return ids, {n: next(iter(v)) for n, v in seen.items() if len(v) == 1}


def _resolve(cell: Any, ids: set[str], by_number: dict[int, str], from_note: bool) -> str | None:
    if _blank(cell):
        return None
    text = str(cell).strip()
    if text in ids:
        return text
    whole = INTEGER_TEXT.match(text)
    if whole and str(int(whole.group(1))) in ids:
        return str(int(whole.group(1)))
    if not from_note:
        return None
    numbers = KEY_NUMBER.findall(text)
    return by_number.get(int(numbers[0])) if len(numbers) == 1 else None


class _Skip(Exception):
    """A ledger that cannot be read without guessing; the message is the reason."""


def _key_column(table: _Table, ids: set[str], by_number: dict[int, str]) -> tuple[str, list[str | None]]:
    """The column that names the order of each row, with the order of every row (None where the cell is blank or
    unresolved). The candidate resolving the most rows wins; a tie between different readings is ambiguous."""
    scored: list[tuple[int, str, list[str | None]]] = []
    for column in table.bases:
        role, kind, join = table.catalog.get(column, ("", "", None))
        from_note = role in ("note", "order_note") or kind == "text"
        if not (role == "order_id" or join or from_note):
            continue
        resolved = [_resolve(row.get(column), ids, by_number, from_note) for row in table.rows]
        count = sum(1 for key in resolved if key)
        if count:
            scored.append((count, column, resolved))
    if not scored:
        raise _Skip("no column names the orders")
    scored.sort(key=lambda item: (-item[0], item[1]))
    best = scored[0]
    if len(scored) > 1 and scored[1][0] == best[0] and scored[1][2] != best[2]:
        raise _Skip("two columns name the orders equally well")
    return best[1], best[2]


def _blocks(table: _Table, key_column: str, resolved: list[str | None]) -> list[tuple[str | None, list[int]]]:
    """Runs of rows of one order: a blank key continues the block above (rulebook §8); a filled key that names no
    order starts a block of key None, which is counted as unresolved."""
    blocks: list[tuple[str | None, list[int]]] = []
    for index, row in enumerate(table.rows):
        if _blank(row.get(key_column)):
            if blocks:
                blocks[-1][1].append(index)
            continue
        key = resolved[index]
        if blocks and blocks[-1][0] == key and key is not None:
            blocks[-1][1].append(index)
        else:
            blocks.append((key, [index]))
    return blocks


def _unparsed(table: _Table, column: str) -> float:
    filled = [row for row in table.rows if not _blank(row.get(column))]
    if not filled:
        return 1.0
    return sum(1 for row in filled if table.number(row, column) is None) / len(filled)


def _unparsed_day(table: _Table, column: str) -> float:
    filled = [row for row in table.rows if not _blank(row.get(column))]
    if not filled:
        return 1.0
    return sum(1 for row in filled if table.day(row, column) is None) / len(filled)


def _first_rows(blocks: list[tuple[Any, list[int]]]) -> set[int]:
    return {rows[0] for _, rows in blocks}


def _fill(table: _Table, column: str, kind: str) -> set[int]:
    if kind == "day":
        return {i for i, row in enumerate(table.rows) if table.day(row, column) is not None}
    return {i for i, row in enumerate(table.rows) if table.number(row, column) is not None}


def _is_head_column(fills: set[int], firsts: set[int], block_count: int) -> bool:
    if not fills:
        return False
    on_first = len(fills & firsts)
    return on_first / len(fills) >= HEAD_ONLY_SHARE and on_first / block_count >= HEAD_COVERAGE


def _find_installments(table: _Table, blocks: list[tuple[Any, list[int]]], heads: list[str]
                       ) -> tuple[str, str | None, str, str, str | None] | None:
    """(price, deposit or None, count, per-instalment amount, due-day column) with price - deposit = count x amount
    on FIT_SHARE of the blocks; None when no four columns do. Two readings that both fit are ambiguous."""
    first = [rows[0] for _, rows in blocks]
    values = {c: [table.number(table.rows[i], c) for i in first] for c in heads}
    found: list[tuple[str, str | None, str, str]] = []
    for count in heads:
        counts = values[count]
        if any(v is not None and (v != int(v) or not 1 <= v <= MAX_INSTALLMENTS) for v in counts):
            continue
        for amount in heads:
            if amount == count or any(v is not None and v <= 0 for v in values[amount]):
                continue
            for price in heads:
                if price in (count, amount):
                    continue
                for deposit in [None, *[h for h in heads if h not in (count, amount, price)]]:
                    fits = 0
                    for n, (c, a, g) in enumerate(zip(counts, values[amount], values[price])):
                        p = values[deposit][n] if deposit else 0.0
                        if None in (c, a, g, p) or p < 0 or g <= p:
                            continue
                        if abs(g - p - c * a) <= max(1.0, 0.0005 * g):
                            fits += 1
                    if fits >= FIT_SHARE * len(first):
                        found.append((price, deposit, count, amount))
    if not found:
        return None
    # A deposit that explains no more than none does is not a column of the sheet's plan.
    with_deposit = [item for item in found if item[1] is not None]
    pool = with_deposit or found
    if len({(item[0], item[2], item[3]) for item in pool}) > 1 or len({item[1] for item in pool}) > 1:
        raise _Skip("several columns satisfy price - deposit = instalments x amount")
    price, deposit, count, amount = pool[0]
    due_day = None
    spare = [c for c in heads if c not in (price, deposit, count, amount)]
    days = [c for c in spare if all(v is None or (v == int(v) and 1 <= v <= 31) for v in values[c])]
    if len(days) > 1:
        roled = [c for c in days if table.role(c) == "due_at"]
        days = roled if len(roled) == 1 else []
    if len(days) == 1:
        due_day = days[0]
    return price, deposit, count, amount, due_day


def _add_months(day: date, months: int, due_day: int) -> date:
    total = day.year * 12 + (day.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(due_day, calendar.monthrange(year, month)[1]))


def _schedule_offset(blocks_info: list[dict[str, Any]]) -> int | None:
    """The months between the order's month and the first instalment's, whichever fits the repayments clearly better
    than the other, or None when the data cannot tell."""
    fits: dict[int, list[int]] = {offset: [] for offset in MONTH_OFFSETS}
    for info in blocks_info:
        events = sorted(info["events"])
        if not events or info["due_day"] is None:
            continue
        first_day = date.fromisoformat(info["date"])
        for offset in MONTH_OFFSETS:
            due = _add_months(first_day, offset, info["due_day"])
            fits[offset].append(abs((date.fromisoformat(events[0][0]) - due).days))
    if len(fits[MONTH_OFFSETS[0]]) < MIN_SCHEDULE_FIT_BLOCKS:
        return None
    ranked = sorted(MONTH_OFFSETS, key=lambda offset: median(fits[offset]))
    best, other = ranked[0], ranked[1]
    if median(fits[other]) - median(fits[best]) < OFFSET_MARGIN_DAYS:
        return None
    return best


def _read_ledger(db: sqlite3.Connection, name: str, catalog: dict[str, tuple[str, str, str | None]],
                 ids: set[str], by_number: dict[int, str], order_day: dict[str, str | None]) -> dict[str, Any]:
    cols = catalog
    if not any(v[1] == "number" for v in cols.values()) or not any(v[1] == "date" for v in cols.values()):
        raise _Skip("too few blocks")
    if db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] > MAX_ROWS:
        raise _Skip("too many rows")
    table = _Table(db, name, catalog)
    if len(table.rows) < MIN_BLOCKS:
        raise _Skip("too few rows")
    key_column, keys = _key_column(table, ids, by_number)
    blocks = _blocks(table, key_column, keys)
    block_count = len(blocks)
    if block_count < MIN_BLOCKS:
        raise _Skip("too few blocks")
    firsts = _first_rows(blocks)

    numbers = [c for c in table.bases if table.kind(c) == "number" and c != key_column
               and table.role(c) not in ID_ROLES]
    days = [c for c in table.bases if table.kind(c) == "date"]
    untyped = [c for c in days if not table.has_day[c] and _unparsed_day(table, c) > UNPARSED_SHARE]
    days = [c for c in days if c not in untyped]
    fills = {c: _fill(table, c, "number") for c in numbers} | {c: _fill(table, c, "day") for c in days}
    heads = [c for c in numbers if _is_head_column(fills[c], firsts, block_count)]
    head_days = [c for c in days if _is_head_column(fills[c], firsts, block_count)]
    later = [c for c in numbers if c not in heads and fills[c]]
    later_days = [c for c in days if c not in head_days and fills[c]]

    pairs = []
    for amount in later:
        for day in later_days:
            union = fills[amount] | fills[day]
            if len(fills[amount] & fills[day]) >= PAIR_AGREEMENT * len(union):
                pairs.append((amount, day))
    if len(pairs) > 1:
        pairs = [pair for pair in pairs if table.role(pair[0]) in PAID_ROLES]
    if not pairs:
        near = max((len(fills[a] & fills[d]) / len(fills[a] | fills[d]) for a in later for d in later_days), default=0.0)
        if near >= NEAR_PAIR and not untyped:
            raise _Skip("a repayment has no day, or a day has no amount")
        if untyped:
            raise _Skip("date columns without a typed companion (xd_*): " + ", ".join(untyped))
        raise _Skip("no repayment amount and day filled together")
    if len(pairs) > 1:
        raise _Skip("several repayment columns")
    paid_column, paid_day_column = pairs[0]
    if _unparsed(table, paid_column) > UNPARSED_SHARE:
        raise _Skip("repayment amounts do not parse")

    # The table has the shape of a ledger; from here on a problem is worth a line in the report.
    if sum(1 for key, _ in blocks if key) < KEY_SHARE * block_count:
        raise _Skip("the order key does not resolve")
    unresolved = block_count - sum(1 for key, _ in blocks if key)
    blocks = [(key, rows) for key, rows in blocks if key]
    if len({key for key, _ in blocks}) != len(blocks):
        raise _Skip("an order has two separate blocks")
    block_count = len(blocks)

    order_date_column = None
    if len(head_days) == 1:
        order_date_column = head_days[0]
    elif len(head_days) > 1:
        roled = [c for c in head_days if table.role(c) == "ordered_at"]
        order_date_column = roled[0] if len(roled) == 1 else None

    plan = _find_installments(table, blocks, heads)
    due_column = None
    kind = "installment"
    if plan is not None:
        price, deposit, count_column, amount_column, due_day_column = plan
        for column in (price, deposit, count_column, amount_column):
            if column and _unparsed(table, column) > UNPARSED_SHARE:
                raise _Skip("instalment amounts do not parse")
    else:
        kind = "debt"
        owed = [c for c in heads if table.role(c) in DUE_ROLES]
        if len(owed) != 1:
            raise _Skip("no single column of what is owed on the first rows")
        due_column = owed[0]
        if _unparsed(table, due_column) > UNPARSED_SHARE:
            raise _Skip("amounts owed do not parse")

    blocks_info: list[dict[str, Any]] = []
    undated = 0
    dropped = 0
    for key, rows in blocks:
        head = table.rows[rows[0]]
        if kind == "debt":
            owed_amount = table.number(head, due_column)
            info = {"count": None, "per": None, "due_day": None}
        else:
            price_v, count_v, per_v = (table.number(head, c) for c in (price, count_column, amount_column))
            deposit_v = table.number(head, deposit) if deposit else 0.0
            owed_amount = None if None in (price_v, deposit_v) else price_v - deposit_v
            day_v = table.number(head, due_day_column) if due_day_column else None
            info = {"count": int(count_v) if count_v is not None else None, "per": per_v,
                    "due_day": int(day_v) if day_v is not None else None}
        if owed_amount is None or owed_amount < 0:
            dropped += 1
            continue
        events = []
        for i in rows:
            row = table.rows[i]
            amount = table.number(row, paid_column)
            if amount is None:
                continue
            paid_on = table.day(row, paid_day_column)
            if paid_on is None:
                undated += 1
                continue
            if amount < 0:
                raise _Skip("a repayment is negative")
            events.append((paid_on, amount))
        ledger_day = table.day(head, order_date_column) if order_date_column else None
        blocks_info.append({"order_id": key, "amount_due": owed_amount, "events": events, "ledger_day": ledger_day,
                            "date": order_day.get(key), **info})
    if undated:
        raise _Skip("a repayment has no day")
    if dropped > (1 - KEY_SHARE) * block_count:
        raise _Skip("blocks with no readable amount owed")
    if not any(info["events"] for info in blocks_info):
        raise _Skip("no repayments")
    if any(info["date"] is None for info in blocks_info):
        raise _Skip("an order has no day")

    overpaid = sum(1 for info in blocks_info
                   if sum(a for _, a in info["events"]) > info["amount_due"] + max(1.0, 0.0005 * info["amount_due"]))
    if overpaid > OVERPAID_SHARE * len(blocks_info):
        raise _Skip("repayments exceed what is owed")
    compared = [info for info in blocks_info if info["ledger_day"] is not None]
    if order_date_column is not None and compared and \
            sum(1 for info in compared if info["ledger_day"] == info["date"]) < DATE_AGREE_SHARE * len(compared):
        raise _Skip("the ledger's order days differ from the orders' days")
    events_all = [(info["date"], e[0]) for info in blocks_info for e in info["events"]]
    if sum(1 for ordered, paid_on in events_all if paid_on < ordered) > EARLY_EVENT_SHARE * len(events_all):
        raise _Skip("repayments dated before their orders")

    offset = None
    if kind == "installment":
        usable = [info for info in blocks_info if info["due_day"] is not None and info["count"]]
        if len(usable) == len(blocks_info):
            offset = _schedule_offset(blocks_info)
    return {"table": name, "kind": kind, "blocks": blocks_info, "offset": offset, "dropped": dropped,
            "unresolved": unresolved, "key_column": key_column}


def _create(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE obligations (order_id TEXT PRIMARY KEY, kind TEXT, amount_due REAL, date TEXT, "
               "installment_count INTEGER, installment_amount REAL, due_day INTEGER, source_table TEXT)")
    db.execute("CREATE TABLE obligation_events (order_id TEXT, date TEXT, amount REAL, source_table TEXT)")
    db.execute("CREATE TABLE obligation_schedule (order_id TEXT, k INTEGER, due_date TEXT, amount REAL)")
    db.execute("CREATE INDEX obligation_events_order ON obligation_events(order_id)")
    db.execute("CREATE INDEX obligation_schedule_order ON obligation_schedule(order_id)")


def build(db: sqlite3.Connection) -> dict[str, Any]:
    """Build the three tables from the ledgers of the layer, or build nothing. Returns the report entry."""
    report: dict[str, Any] = {"built": 0, "skipped": []}
    tables = _tables(db)
    if "orders" not in tables or "column_catalog" not in tables:
        return report
    if "payments" in tables and db.execute("SELECT 1 FROM payments LIMIT 1").fetchone():
        report["skipped"].append("payments: the shop has a payment table")
        return report
    catalog = _catalog(db)
    ids, by_number = _order_keys(db)
    order_day = {str(oid): (str(day)[:10] if day else None) for oid, day in db.execute("SELECT order_id, date FROM orders")}
    ledgers = []
    for name in sorted(t for t in tables if t.startswith("src_")):
        if name not in catalog:
            continue
        try:
            ledgers.append(_read_ledger(db, name, catalog[name], ids, by_number, order_day))
        except _Skip as why:
            # Most tables are not ledgers; only a table with a repayment-like shape is worth a line.
            if str(why) not in QUIET_REASONS:
                report["skipped"].append(f"{name}: {why}")
        except (ValueError, TypeError, ArithmeticError) as exc:
            report["skipped"].append(f"{name}: unreadable ({type(exc).__name__})")
    if not ledgers:
        return report
    seen: set[str] = set()
    for ledger in ledgers:
        keys = {info["order_id"] for info in ledger["blocks"]}
        if keys & seen:
            report["skipped"].append("two ledgers name the same order: nothing built")
            return report
        seen |= keys
    _create(db)
    total_events = 0.0
    n_events = 0
    for ledger in ledgers:
        for info in ledger["blocks"]:
            per = info["per"]
            db.execute("INSERT INTO obligations VALUES (?,?,?,?,?,?,?,?)",
                       (info["order_id"], ledger["kind"], info["amount_due"], info["date"], info["count"], per,
                        info["due_day"] if ledger["offset"] is not None else None, ledger["table"]))
            for paid_on, amount in info["events"]:
                db.execute("INSERT INTO obligation_events VALUES (?,?,?,?)",
                           (info["order_id"], paid_on, amount, ledger["table"]))
                total_events += amount
                n_events += 1
            if ledger["offset"] is not None:
                start = date.fromisoformat(info["date"])
                for k in range(1, info["count"] + 1):
                    db.execute("INSERT INTO obligation_schedule VALUES (?,?,?,?)",
                               (info["order_id"], k, _add_months(start, ledger["offset"] + k - 1, info["due_day"])
                                .isoformat(), per))
    got = db.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM obligation_events").fetchone()
    if got[0] != n_events or abs(got[1] - total_events) > 1e-6:
        raise RuntimeError("obligation events do not match the ledger rows")
    report["built"] = db.execute("SELECT COUNT(*) FROM obligations").fetchone()[0]
    report["events"] = n_events
    report["schedule_rows"] = db.execute("SELECT COUNT(*) FROM obligation_schedule").fetchone()[0]
    report["tables"] = {ledger["table"]: {"kind": ledger["kind"], "blocks": len(ledger["blocks"]),
                                          "dropped": ledger["dropped"], "schedule": ledger["offset"] is not None}
                        for ledger in ledgers}
    _register(db)
    return report


def _register(db: sqlite3.Connection) -> None:
    """column_catalog rows (derived=1) for the three tables."""
    for table, columns in DERIVED_COLUMNS.items():
        for column, role, description in columns:
            distinct = db.execute(f'SELECT COUNT(DISTINCT "{column}") FROM {table}').fetchone()[0]
            samples = [row[0] for row in db.execute(
                f'SELECT DISTINCT "{column}" FROM {table} WHERE "{column}" IS NOT NULL LIMIT 8')]
            db.execute(
                "INSERT INTO column_catalog (table_name, column_name, source_table, source_header, role, value_type, "
                "distinct_count, sample_values, join_key_to, derived) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 1)",
                (table, column, description, role,
                 "number" if column in ("amount_due", "amount", "installment_amount", "installment_count", "k",
                                        "due_day") else ("date" if "date" in column else "text"),
                 distinct, json.dumps(samples, ensure_ascii=False),
                 "orders.order_id" if column == "order_id" else None))
