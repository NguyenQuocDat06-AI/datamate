import sqlite3
from datetime import date

from bizreason.contracts import TimeWindow
from bizreason.semantic.column_links import ColumnIndex
from bizreason.semantic.compiler import Compiler, Intent


def _db():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, revenue REAL, x_trade TEXT, xn_trade REAL)")
    con.execute("CREATE TABLE lines (order_id TEXT, date TEXT, status TEXT, product_name TEXT, amount REAL)")
    con.execute("CREATE TABLE ledger_src (code TEXT, due TEXT, xd_due TEXT)")
    con.execute("CREATE TABLE column_catalog (table_name, column_name, source_header, role, value_type, "
                "distinct_count, sample_values, join_key_to)")
    con.execute("ALTER TABLE ledger_src RENAME TO src_ledger")
    con.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?)", [
        ("o1", "2026-01-05", "completed", 100, "50", 50), ("o2", "2026-01-06", "completed", 80, "", None),
        ("o3", "2026-01-07", "cancelled", 90, "30", 30), ("o4", "2026-02-01", "completed", 70, "20", 20)])
    con.executemany("INSERT INTO src_ledger VALUES (?,?,?)", [
        ("o1", "2026-03-01", "2026-03-01"), ("o2", "2026-01-10", "2026-01-10"), ("o4", "2026-04-01", "2026-04-01")])
    con.executemany("INSERT INTO column_catalog VALUES (?,?,?,?,?,?,?,?)", [
        ("orders", "x_trade", "Trade in", "other", "number", 3, '["50","30"]', None),
        ("src_ledger", "code", "Order code", "other", "code", 3, '["o1"]', "orders.order_id"),
        ("src_ledger", "due", "Due date", "other", "date", 3, '["2026-03-01"]', None)])
    return con


def _jan():
    return TimeWindow(role="target", start=date(2026, 1, 1), end_exclusive=date(2026, 2, 1), evidence=["q"])


def test_sum_of_kept_column_over_valid_sales():
    c = Compiler(_db())
    intent = Intent(measure="outside", column_measure={"op": "sum", "column": "x_trade"})
    assert c.answer(intent, [_jan()]).value == 50


def test_count_with_date_condition_on_linked_source_table():
    c = Compiler(_db())
    c.as_of = date(2026, 2, 15)
    intent = Intent(measure="order_count", column_measure={"op": "count", "column": "code"},
                    column_filters=[{"column": "due", "op": "after_today", "value": None}])
    whole = TimeWindow(role="target", start=date(1900, 1, 1), end_exclusive=date(2026, 2, 16), evidence=["q"])
    assert c.answer(intent, [whole]).value == 2


def test_column_index_offers_columns_named_in_the_question():
    index = ColumnIndex.from_connection(_db())
    assert "x_trade" in index.block("tổng trade in tháng 1")
    assert index.block("doanh thu tháng 1") == ""


def test_orders_delivered_in_two_or_more_trips():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, revenue REAL, customer_key TEXT)")
    con.execute("CREATE TABLE lines (order_id TEXT, date TEXT, status TEXT, product_name TEXT, amount REAL)")
    con.execute("CREATE TABLE deliveries (order_id TEXT, date TEXT, trip TEXT, qty REAL)")
    con.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", [
        ("o1", "2026-03-02", "completed", 1, None), ("o2", "2026-03-03", "completed", 1, None),
        ("o3", "2026-03-04", "completed", 1, None)])
    con.executemany("INSERT INTO deliveries VALUES (?,?,?,?)", [
        ("o1", "2026-03-03", "1", 1), ("o1", "2026-03-09", "2", 1), ("o2", "2026-03-05", "1", 1),
        ("o3", "2026-03-05", "1", 1), ("o3", "2026-04-20", "2", 1)])
    c = Compiler(con)
    c.as_of = date(2026, 4, 1)
    intent = Intent(measure="order_count", shape="threshold_count",
                    threshold={"subject": "order", "metric": "delivery_trips", "op": "gte", "value": 2})
    march = TimeWindow(role="target", start=date(2026, 3, 1), end_exclusive=date(2026, 4, 1), evidence=["q"])
    assert c.answer(intent, [march]).value == 1  # o3's second trip is after today
