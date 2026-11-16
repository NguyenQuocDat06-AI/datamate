"""Conditions on a side table linked to orders by an order-id column, and counting tickets that list an item."""
import sqlite3
from datetime import date

import pytest

from bizreason.contracts import TimeWindow
from bizreason.semantic.compiler import Compiler, Intent, Unsupported


def _db(extra_key: bool = False):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, revenue REAL, channel TEXT)")
    con.execute("CREATE TABLE lines (order_id TEXT, date TEXT, status TEXT, product_name TEXT, amount REAL)")
    con.execute("CREATE TABLE column_catalog (table_name, column_name, source_header, role, value_type, "
                "distinct_count, sample_values, join_key_to)")
    cols = "code TEXT, due TEXT, xd_due TEXT" + (", alt TEXT" if extra_key else "")
    con.execute(f"CREATE TABLE src_term ({cols})")
    con.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", [
        ("o1", "2026-01-05", "completed", 10, "shop"), ("o2", "2026-01-06", "completed", 10, "web"),
        ("o3", "2026-01-07", "cancelled", 10, "shop"), ("o4", "2026-01-20", "completed", 10, "shop"),
        ("o5", "2026-02-20", "completed", 10, "shop")])
    rows = [("o1", "2026-03-01", "2026-03-01"), ("o1", "2026-04-01", "2026-04-01"),  # o1 has two instalments
            ("o2", "2026-03-10", "2026-03-10"), ("o3", "2026-03-20", "2026-03-20"),
            ("o4", "2026-01-15", "2026-01-15"), ("o5", "2026-03-05", "2026-03-05")]
    if extra_key:
        rows = [r + ("o1",) for r in rows]
    con.executemany(f"INSERT INTO src_term VALUES ({'?,?,?,?' if extra_key else '?,?,?'})", rows)
    catalog = [("src_term", "code", "Code", "order_id", "code", 5, "[]", "orders.order_id"),
               ("src_term", "due", "Due", "due_at", "date", 5, "[]", None)]
    if extra_key:
        catalog.append(("src_term", "alt", "Alt", "order_id", "code", 1, "[]", "orders.order_id"))
    con.executemany("INSERT INTO column_catalog VALUES (?,?,?,?,?,?,?,?)", catalog)
    return con


def _until(end: date) -> TimeWindow:
    return TimeWindow(role="target", start=date(1900, 1, 1), end_exclusive=end, evidence=["q"])


def _count(**kw) -> Intent:
    return Intent(measure="order_count", column_measure={"op": "count", "column": "code"}, **kw)


def _after_today() -> dict:
    return {"column": "due", "op": "after_today", "value": None}


def test_each_order_counted_once_even_with_several_side_rows():
    c = Compiler(_db())
    c.as_of = date(2026, 2, 28)
    # o1 (two future instalments), o2, o5 are valid; o3 is cancelled; o4 is due in the past.
    assert c.answer(_count(column_filters=[_after_today()]), [_until(date(2026, 3, 1))]).value == 3


def test_orders_placed_after_today_are_not_counted():
    c = Compiler(_db())
    c.as_of = date(2026, 2, 10)  # o5 was placed on 02-20: it does not exist yet
    assert c.answer(_count(column_filters=[_after_today()]), [_until(date(2026, 2, 11))]).value == 2


def test_order_level_filter_applies_on_a_side_table():
    c = Compiler(_db())
    c.as_of = date(2026, 2, 28)
    intent = _count(column_filters=[_after_today()], filters=[{"dim": "channel", "value": "web", "exclude": False}])
    assert c.answer(intent, [_until(date(2026, 3, 1))]).value == 1


def test_period_applies_to_the_side_table_date_not_the_order_date():
    c = Compiler(_db())
    c.as_of = date(2026, 2, 28)
    intent = _count(column_filters=[{"column": "due", "op": "in_period", "value": None}])
    march = TimeWindow(role="target", start=date(2026, 3, 1), end_exclusive=date(2026, 4, 1), evidence=["q"])
    # Orders placed in January/February, due in March: o1, o2, o5 (o3 cancelled).
    assert c.answer(intent, [march]).value == 3


def test_line_filter_on_a_side_table_is_not_guessed():
    c = Compiler(_db())
    c.as_of = date(2026, 2, 28)
    c.has_lines = True
    intent = _count(column_filters=[_after_today()], filters=[{"dim": "product", "value": "x", "exclude": False}])
    with pytest.raises(Unsupported):
        c.answer(intent, [_until(date(2026, 3, 1))])


def test_two_order_link_columns_are_ambiguous():
    c = Compiler(_db(extra_key=True))
    c.as_of = date(2026, 2, 28)
    with pytest.raises(Unsupported, match="several order links"):
        c.answer(_count(column_filters=[_after_today()]), [_until(date(2026, 3, 1))])


def test_side_table_without_an_order_link_is_not_guessed():
    con = _db()
    con.execute("DELETE FROM column_catalog WHERE column_name = 'code'")
    c = Compiler(con)
    c.as_of = date(2026, 2, 28)
    with pytest.raises(Unsupported, match="no order link"):
        c.answer(_count(column_filters=[_after_today()]), [_until(date(2026, 3, 1))])


def test_sum_on_side_table_is_not_multiplied_by_a_join():
    con = _db()
    con.execute("ALTER TABLE src_term ADD COLUMN xn_amt REAL")
    con.execute("UPDATE src_term SET xn_amt = 5")
    con.execute("INSERT INTO column_catalog VALUES ('src_term','xn_amt','Amt','other','number',1,'[]',NULL)")
    c = Compiler(con)
    c.as_of = date(2026, 2, 28)
    intent = Intent(measure="outside", column_measure={"op": "sum", "column": "xn_amt"},
                    column_filters=[_after_today()])
    assert c.answer(intent, [_until(date(2026, 3, 1))]).value == 20  # four valid future rows x 5


def _tickets():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, revenue REAL)")
    con.execute("CREATE TABLE lines (order_id TEXT, date TEXT, status TEXT, product_name TEXT, amount REAL)")
    names = ["fix wiring", "tune, fix wiring + wash", "wash", "prefix wiring", "Latte (M)", "Latte (L)", "wash, Latte (M)", "wash + tune"]
    for i, name in enumerate(names):
        con.execute("INSERT INTO orders VALUES (?,?,?,?)", (f"o{i}", "2026-01-05", "completed", 1))
        con.execute("INSERT INTO lines VALUES (?,?,?,?,?)", (f"o{i}", "2026-01-05", "completed", name, 1))
    return con


def _jan():
    return TimeWindow(role="target", start=date(2026, 1, 1), end_exclusive=date(2026, 2, 1), evidence=["q"])


def _tickets_with(value: str) -> float:
    intent = Intent(measure="order_count", filters=[{"dim": "product", "value": value, "exclude": False}])
    return Compiler(_tickets()).answer(intent, [_jan()]).value


def test_ticket_counts_every_cell_listing_the_item():
    # "fix wiring" alone and inside a list; "prefix wiring" is a different item and is not counted.
    assert _tickets_with("fix wiring") == 2


def test_exact_spelling_still_separates_sizes():
    assert _tickets_with("Latte (M)") == 2  # the plain cell and the one listing it; never "Latte (L)"
    assert _tickets_with("Latte (L)") == 1


def test_exact_name_wins_when_cells_are_not_lists():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, revenue REAL)")
    con.execute("CREATE TABLE lines (order_id TEXT, date TEXT, status TEXT, product_name TEXT, amount REAL)")
    for i, name in enumerate(["Latte (M)", "Latte (L)", "Latte (M)", "Mocha"]):
        con.execute("INSERT INTO orders VALUES (?,?,?,?)", (f"o{i}", "2026-01-05", "completed", 1))
        con.execute("INSERT INTO lines VALUES (?,?,?,?,?)", (f"o{i}", "2026-01-05", "completed", name, 1))
    intent = Intent(measure="order_count", filters=[{"dim": "product", "value": "Latte (M)", "exclude": False}])
    assert Compiler(con).answer(intent, [_jan()]).value == 2
