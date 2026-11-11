"""Compiler changes: status gates, breakdown and two-member comparison, ranking measures, and new value measures.

Expected numbers are worked by hand in the comments next to each assertion.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from bizreason.contracts import TimeWindow
from bizreason.semantic.compiler import Answer, Compiler, Intent, Unsupported
from bizreason.utils import normalize_lookup

SCHEMA = """
CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, customer_key TEXT, channel TEXT, revenue REAL);
CREATE TABLE lines (order_id TEXT, date TEXT, status TEXT, customer_key TEXT, channel TEXT, product_key TEXT,
                    product_name TEXT, category TEXT, unit TEXT, qty REAL, qty_base REAL, amount REAL,
                    line_cost REAL);
CREATE TABLE purchases (purchase_id TEXT, date TEXT, supplier_name TEXT, status TEXT, product_name TEXT,
                        qty REAL, unit_cost REAL, amount REAL, cancelled INTEGER);
CREATE TABLE payments (order_id TEXT, date TEXT, amount REAL, method TEXT);
"""


def order(order_id, day, status, customer, channel, revenue):
    return (order_id, day, status, customer, channel, revenue)


def line(order_id, day, status, product, qty, amount, customer="c1", channel="dine"):
    return (order_id, day, status, customer, channel, product, product, None, None, qty, qty, amount, None)


# order_id, date, status, customer_key, channel, revenue
ORDERS = [
    order("O1", "2026-03-02", "completed", "c1", "dine", 100),
    order("O2", "2026-03-05", "cancelled", "c2", "dine", 60),
    order("O3", "2026-03-06", "completed", "c1", "take", 40),
    order("O4", "2026-03-20", "returned", "c2", "take", 30),
    order("O5", "2026-03-21", "shipping", "c3", "dine", 80),
    order("O6", "2026-02-10", "completed", "c1", "dine", 50),
    order("O7", "2026-03-02", "cancelled", "c3", "take", 45),
    order("O8", "2026-03-09", "cancelled", "c2", "take", 90),
]
# order_id, date, status, customer_key, channel, product_key, product_name, category, unit, qty, qty_base, amount, cost
LINES = [
    line("O1", "2026-03-02", "completed", "tea", 2, 100),
    line("O2", "2026-03-05", "cancelled", "coffee", 1, 60, "c2", "dine"),
    line("O3", "2026-03-06", "completed", "tea", 1, 40, "c1", "take"),
    line("O4", "2026-03-20", "returned", "cake", 1, 30, "c2", "take"),
    line("O5", "2026-03-21", "shipping", "coffee", 2, 80, "c3", "dine"),
    line("O6", "2026-02-10", "completed", "cake", 1, 50),
    line("O7", "2026-03-02", "cancelled", "tea", 1, 45, "c3", "take"),
    line("O8", "2026-03-09", "cancelled", "cake", 3, 90, "c2", "take"),
]
# purchase_id, date, supplier_name, status, product_name, qty, unit_cost, amount, cancelled
PURCHASES = [
    ("P1", "2026-03-01", "s1", "completed", "tea", 10, 5, 50, 0),
    ("P2", "2026-03-15", "s1", "completed", "tea", 30, 7, 210, 0),
    ("P3", "2026-03-18", "s2", "cancelled", "tea", 100, 1, 100, 1),
    ("P4", "2026-03-25", "s2", "completed", "coffee", 10, 99, 990, 0),
    ("P5", "2026-02-20", "s1", "completed", "tea", 5, 100, 500, 0),
]

MARCH = TimeWindow(start=date(2026, 3, 1), end_exclusive=date(2026, 4, 1))
APRIL = TimeWindow(start=date(2026, 4, 1), end_exclusive=date(2026, 5, 1))
FEBRUARY = TimeWindow(start=date(2026, 2, 1), end_exclusive=date(2026, 3, 1))


def make_db(orders=ORDERS, lines=LINES, purchases=PURCHASES) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.create_function("lookup", 1, lambda v: None if v is None else normalize_lookup(v))
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?)", orders)
    db.executemany("INSERT INTO lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", lines)
    db.executemany("INSERT INTO purchases VALUES (?,?,?,?,?,?,?,?,?)", purchases)
    db.commit()
    return db


def product(name: str) -> dict:
    return {"dim": "product", "value": name}


def channel(name: str, exclude: bool = False) -> dict:
    item = {"dim": "channel", "value": name}
    if exclude:
        item["exclude"] = True
    return item


def test_status_gate_raises_when_no_order_has_status() -> None:
    # Every order has a null status, so revenue would silently be 0 without the gate.
    null_status = [(o[0], o[1], None, o[3], o[4], o[5]) for o in ORDERS]
    compiler = Compiler(make_db(orders=null_status, lines=[]))
    with pytest.raises(Unsupported, match="order status unavailable"):
        compiler.value(Intent(measure="revenue"), MARCH)
    with pytest.raises(Unsupported, match="order status unavailable"):
        compiler.answer(Intent(measure="revenue", shape="argmax_entity", group_by="channel"), [MARCH])


def test_status_check_is_cached_on_the_instance() -> None:
    compiler = Compiler(make_db())
    assert compiler.value(Intent(measure="revenue"), MARCH) == 220
    assert compiler._has_status is True


def test_no_cancellation_recorded_raises_for_cancel_measures() -> None:
    # Statuses exist, but no order is ever cancelled, so a zero would hide missing data.
    orders = [order("O1", "2026-03-02", "completed", "c1", "dine", 100), order("O3", "2026-03-06", "returned", "c1", "dine", 40)]
    compiler = Compiler(make_db(orders=orders, lines=[]))
    with pytest.raises(Unsupported, match="cancellation not recorded"):
        compiler.value(Intent(measure="cancelled_count"), MARCH)
    with pytest.raises(Unsupported, match="cancellation not recorded"):
        compiler.value(Intent(measure="cancel_rate"), MARCH)
    # Revenue is still answered: 100 completed, and the returned order is left out.
    assert compiler.value(Intent(measure="revenue"), MARCH) == 100


def test_cancel_rate_is_percent_rounded_to_two_places() -> None:
    # March has 7 orders (O1 to O5, O7, O8); 3 are cancelled (O2, O7, O8): 100 * 3 / 7 = 42.857... -> 42.86.
    compiler = Compiler(make_db())
    assert compiler.answer(Intent(measure="cancel_rate"), [MARCH]) == Answer("percent", 42.86)
    # Cancelled orders in March: O2, O7, O8.
    assert compiler.value(Intent(measure="cancelled_count"), MARCH) == 3


def test_breakdown_by_group_follows_filter_order() -> None:
    # Valid March revenue by channel: dine = O1 100 + O5 80 = 180; take = O3 40 (O4 returned is left out).
    compiler = Compiler(make_db())
    dine_first = Intent(measure="revenue", shape="breakdown", group_by="channel", filters=[channel("dine"), channel("take")])
    assert compiler.answer(dine_first, [MARCH]) == Answer("list", [180, 40])
    take_first = Intent(measure="revenue", shape="breakdown", group_by="channel", filters=[channel("take"), channel("dine")])
    assert compiler.answer(take_first, [MARCH]) == Answer("list", [40, 180])


def test_breakdown_other_filters_apply_to_every_member() -> None:
    # Customer c1 only: dine = O1 100 (O5 is c3); take = O3 40.
    compiler = Compiler(make_db())
    intent = Intent(
        measure="revenue", shape="breakdown", group_by="channel",
        filters=[channel("dine"), channel("take"), {"dim": "customer", "value": "c1"}],
    )
    assert compiler.answer(intent, [MARCH]) == Answer("list", [100, 40])


def test_breakdown_without_member_filters_raises() -> None:
    compiler = Compiler(make_db())
    with pytest.raises(Unsupported):
        compiler.answer(Intent(measure="revenue", shape="breakdown", group_by="channel"), [MARCH])


def test_breakdown_with_one_member_filter_raises() -> None:
    # A question for several members names one: the answer would be a one-element list, a different question.
    compiler = Compiler(make_db())
    intent = Intent(measure="revenue", shape="breakdown", group_by="channel", filters=[channel("dine")])
    with pytest.raises(Unsupported, match="breakdown names one member"):
        compiler.answer(intent, [MARCH])
    # Two members of the same dimension still give two values.
    two = Intent(measure="revenue", shape="breakdown", group_by="channel", filters=[channel("dine"), channel("take")])
    assert compiler.answer(two, [MARCH]) == Answer("list", [180, 40])


def test_multi_period_breakdown_still_works() -> None:
    # March valid revenue 220 (O1, O3, O5); February 50 (O6).
    compiler = Compiler(make_db())
    assert compiler.answer(Intent(measure="revenue", shape="breakdown"), [MARCH, FEBRUARY]) == Answer("list", [220, 50])


def test_two_member_comparison_percent() -> None:
    # Dine 180 against take 40, the second filter as baseline: (180 - 40) / 40 * 100 = 350.
    compiler = Compiler(make_db())
    intent = Intent(measure="revenue", shape="compare_periods", filters=[channel("dine"), channel("take")])
    assert compiler.answer(intent, [MARCH]) == Answer("percent", 350.0)


def test_two_member_comparison_keeps_filter_order_and_value_mode() -> None:
    # Take against dine: (40 - 180) / 180 * 100 = -77.777... -> -77.78.
    compiler = Compiler(make_db())
    reversed_order = Intent(measure="revenue", shape="compare_periods", filters=[channel("take"), channel("dine")])
    assert compiler.answer(reversed_order, [MARCH]) == Answer("percent", -77.78)
    # The difference in value mode: dine minus take = 180 - 40 = 140.
    as_value = Intent(
        measure="revenue", shape="compare_periods", compare_as="value", filters=[channel("dine"), channel("take")]
    )
    assert compiler.answer(as_value, [MARCH]) == Answer("number", 140)


def test_two_member_comparison_needs_exactly_two_members() -> None:
    compiler = Compiler(make_db())
    intent = Intent(measure="revenue", shape="compare_periods", filters=[channel("dine")])
    with pytest.raises(Unsupported):
        compiler.answer(intent, [MARCH])


def test_ranking_by_cancelled_count_uses_cancelled_status() -> None:
    # Cancelled March orders by channel: take = O7, O8 (2); dine = O2 (1). The valid-status predicate would not show them.
    compiler = Compiler(make_db())
    intent = Intent(measure="cancelled_count", shape="top_k", group_by="channel", top_k=2)
    assert compiler.answer(intent, [MARCH]) == Answer("list", ["take", "dine"])
    argmax = Intent(measure="cancelled_count", shape="argmax_entity", group_by="channel")
    assert compiler.answer(argmax, [MARCH]) == Answer("string", "take")


def test_ranking_by_orders_containing_product_counts_distinct_orders() -> None:
    # Valid March lines: tea in O1 and O3 (2 orders); coffee in O5 (1). Cancelled O2 and O7 do not count.
    compiler = Compiler(make_db())
    intent = Intent(measure="orders_containing_product", shape="top_k", group_by="product", top_k=2)
    assert compiler.answer(intent, [MARCH]) == Answer("list", ["tea", "coffee"])


def test_ranking_by_orders_containing_product_needs_product_grouping() -> None:
    compiler = Compiler(make_db())
    intent = Intent(measure="orders_containing_product", shape="argmax_entity", group_by="channel")
    with pytest.raises(Unsupported):
        compiler.answer(intent, [MARCH])


def test_unit_sale_price_includes_cancelled_and_returned_lines() -> None:
    # Cake on 20 Mar: the only line is returned, 30 for one unit. Returned lines count for the price.
    compiler = Compiler(make_db())
    one_day = TimeWindow(start=date(2026, 3, 20), end_exclusive=date(2026, 3, 21))
    intent = Intent(measure="unit_sale_price_on_date", filters=[product("cake")])
    assert compiler.answer(intent, [one_day]) == Answer("number", 30)


def test_unit_sale_price_with_two_prices_that_day_raises() -> None:
    # Tea on 2 Mar: 100 / 2 = 50 (completed O1) and 45 / 1 = 45 (cancelled O7). Two prices, so no answer.
    compiler = Compiler(make_db())
    one_day = TimeWindow(start=date(2026, 3, 2), end_exclusive=date(2026, 3, 3))
    with pytest.raises(Unsupported):
        compiler.value(Intent(measure="unit_sale_price_on_date", filters=[product("tea")]), one_day)


def test_unit_sale_price_needs_one_product_and_one_day() -> None:
    compiler = Compiler(make_db())
    with pytest.raises(Unsupported):
        compiler.value(Intent(measure="unit_sale_price_on_date", filters=[product("tea")]), MARCH)
    one_day = TimeWindow(start=date(2026, 3, 20), end_exclusive=date(2026, 3, 21))
    with pytest.raises(Unsupported):
        compiler.value(Intent(measure="unit_sale_price_on_date"), one_day)


def test_unit_sale_price_rounds_money_half_up() -> None:
    # One line of 100 for 3 units: 33.33 -> 33. One line of 50 for 4 units: 12.5 -> 13 (half up).
    one_day = TimeWindow(start=date(2026, 3, 2), end_exclusive=date(2026, 3, 3))
    third = make_db(orders=[], purchases=[], lines=[line("O9", "2026-03-02", "completed", "tea", 3, 100)])
    fourth = make_db(orders=[], purchases=[], lines=[line("O9", "2026-03-02", "completed", "tea", 4, 50)])
    intent = Intent(measure="unit_sale_price_on_date", filters=[product("tea")])
    assert Compiler(third).answer(intent, [one_day]) == Answer("number", 33)
    assert Compiler(fourth).answer(intent, [one_day]) == Answer("number", 13)


def test_unit_purchase_price_is_quantity_weighted() -> None:
    # Tea purchases in March, non-cancelled: (10 * 5 + 30 * 7) / (10 + 30) = 260 / 40 = 6.5.
    # The cancelled P3 and the February P5 are left out; the plain average of 5 and 7 would be 6.
    compiler = Compiler(make_db())
    intent = Intent(measure="unit_purchase_price", filters=[product("tea")])
    assert compiler.value(intent, MARCH) == pytest.approx(6.5)
    assert compiler.answer(intent, [MARCH]) == Answer("number", 7)


def test_unit_purchase_price_needs_product_on_purchases() -> None:
    compiler = Compiler(make_db())
    # Cake is sold but never bought in the purchase rows.
    with pytest.raises(Unsupported):
        compiler.value(Intent(measure="unit_purchase_price", filters=[product("cake")]), MARCH)
    with pytest.raises(Unsupported):
        compiler.value(Intent(measure="unit_purchase_price"), MARCH)


def test_no_sale_count_excludes_products_never_sold_before() -> None:
    # Before March, only cake sold (O6, 10 Feb). Tea and coffee were first sold in March, so they are not counted.
    # Cake has no valid order in March (O4 returned, O8 cancelled), so March gives 1.
    compiler = Compiler(make_db())
    assert compiler.value(Intent(measure="no_sale_count"), MARCH) == 1
    # Before April, all three products were sold (tea O1, coffee O5 shipping, cake O6) and none sold in April.
    assert compiler.value(Intent(measure="no_sale_count"), APRIL) == 3


# Timed fixtures: orders and lines carry the order time in ts. Hand-computed values are in the comments.
# Valid March orders: A1 100 (07:30), A2 50 (13:59:59), A3 40 (14:00), A4 70 (21:59:59), A5 30 (22:00),
# A7 20 (09:15, total_due unknown), A10 25 (18:00), A12 15 (12:00, no customer). 8 orders, revenue 350.
# Left out: A6 returned, A8 and A11 cancelled, A9 in February.
# Line amounts are set per product on their own; each test reads one table.
T_ORDERS = [
    # order_id, date, ts, status, customer_key, customer_name, channel, revenue, total_due
    ("A1", "2026-03-02", "2026-03-02 07:30:00", "completed", "c1", "Lan", "dine", 100, 110),
    ("A2", "2026-03-02", "2026-03-02 13:59:59", "completed", "c1", "Lan", "dine", 50, 50),
    ("A3", "2026-03-02", "2026-03-02 14:00:00", "completed", "c2", "Minh", "take", 40, 45),
    ("A4", "2026-03-03", "2026-03-03 21:59:59", "shipping", "c2", "Minh", "dine", 70, 80),
    ("A5", "2026-03-03", "2026-03-03 22:00:00", "completed", "c3", None, "dine", 30, 30),
    ("A6", "2026-03-04", "2026-03-04 06:00:00", "returned", "c3", None, "take", 60, 60),
    ("A7", "2026-03-04", "2026-03-04 09:15:00", "completed", "c3", None, "dine", 20, None),
    ("A8", "2026-03-05", "2026-03-05 10:00:00", "cancelled", "c1", "Lan", "dine", 500, 500),
    ("A9", "2026-02-10", "2026-02-10 08:00:00", "completed", "c4", "Dung", "dine", 999, 999),
    ("A10", "2026-03-06", "2026-03-06 18:00:00", "completed", "c3", None, "take", 25, 25),
    ("A11", "2026-03-07", "2026-03-07 10:30:00", "cancelled", "c5", "Hoa", "dine", 10, 10),
    ("A12", "2026-03-08", "2026-03-08 12:00:00", "completed", None, None, "dine", 15, 15),
]
# Valid March lines. Tea: A1, A2, A4 (3 orders, qty_base 2+1+12 = 15, amount 220).
# Cake: A2, A3 (2 orders, qty 3+1 = 4 since A3 has no qty_base, amount 70).
# Coffee: A5, A7 (2 orders, qty 1+2 = 3, amount 50). The returned A6 line is left out.
T_LINES = [
    # order_id, date, ts, status, customer_key, channel, product_key, product_name, qty, qty_base, amount
    ("A1", "2026-03-02", "2026-03-02 07:30:00", "completed", "c1", "dine", "tea", "Tea", 2, 2, 100),
    ("A2", "2026-03-02", "2026-03-02 13:59:59", "completed", "c1", "dine", "tea", "Tea", 1, 1, 50),
    ("A2", "2026-03-02", "2026-03-02 13:59:59", "completed", "c1", "dine", "cake", "Cake", 3, 3, 30),
    ("A3", "2026-03-02", "2026-03-02 14:00:00", "completed", "c2", "take", "cake", "Cake", 1, None, 40),
    ("A4", "2026-03-03", "2026-03-03 21:59:59", "shipping", "c2", "dine", "tea", "Tea", 1, 12, 70),
    ("A5", "2026-03-03", "2026-03-03 22:00:00", "completed", "c3", "dine", "coffee", "Coffee", 1, 1, 30),
    ("A6", "2026-03-04", "2026-03-04 06:00:00", "returned", "c3", "take", "tea", "Tea", 1, 1, 60),
    ("A7", "2026-03-04", "2026-03-04 09:15:00", "completed", "c3", "dine", "coffee", "Coffee", 2, 2, 20),
]
# Receipts. R2 has two lines sharing one receipt total of 44. R4 is cancelled; R5 is in February.
# March receipts: R1 50, R2 44, R3 40 (amount missing, so qty * unit_cost). Sun Co: R1 + R2 = 94. Bean Ltd: 40.
T_PURCHASES = [
    # purchase_id, date, supplier_name, supplier_id, status, product_name, qty, unit_cost, amount, receipt_total, cancelled
    ("R1", "2026-03-01", "Sun Co", "S1", "completed", "tea", 10, 5, 50, None, 0),
    ("R2", "2026-03-05", "Sun Co", "S1", "completed", "tea", 4, 6, 24, 44, 0),
    ("R2", "2026-03-05", "Sun Co", "S1", "completed", "coffee", 2, 10, 20, 44, 0),
    ("R3", "2026-03-10", "Bean Ltd", "S2", "completed", "coffee", 5, 8, None, None, 0),
    ("R4", "2026-03-12", "Bean Ltd", "S2", "cancelled", "coffee", 1, 999, 999, None, 1),
    ("R5", "2026-02-20", "Sun Co", "S1", "completed", "tea", 1, 500, 500, None, 0),
]
TIMED_SCHEMA = """
CREATE TABLE orders (order_id TEXT, date TEXT, ts TEXT, status TEXT, customer_key TEXT, customer_name TEXT,
                     channel TEXT, revenue REAL, total_due REAL);
CREATE TABLE lines (order_id TEXT, date TEXT, ts TEXT, status TEXT, customer_key TEXT, channel TEXT,
                    product_key TEXT, product_name TEXT, qty REAL, qty_base REAL, amount REAL);
CREATE TABLE purchases (purchase_id TEXT, date TEXT, supplier_name TEXT, supplier_id TEXT, status TEXT,
                        product_name TEXT, qty REAL, unit_cost REAL, amount REAL, receipt_total REAL,
                        cancelled INTEGER);
"""


def make_timed_db(orders=T_ORDERS, lines=T_LINES, purchases=T_PURCHASES) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.create_function("lookup", 1, lambda v: None if v is None else normalize_lookup(v))
    db.executescript(TIMED_SCHEMA)
    db.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)", orders)
    db.executemany("INSERT INTO lines VALUES (?,?,?,?,?,?,?,?,?,?,?)", lines)
    db.executemany("INSERT INTO purchases VALUES (?,?,?,?,?,?,?,?,?,?,?)", purchases)
    db.commit()
    return db


def threshold(subject: str, metric: str, op: str, value: float, shape: str = "threshold_count", **extra) -> Intent:
    spec = {"subject": subject, "metric": metric, "op": op, "value": value}
    return Intent(measure="order_count", shape=shape, threshold=spec, **extra)


def shift(name: str) -> dict:
    return {"dim": "shift", "value": name}


def supplier(name: str, exclude: bool = False) -> dict:
    item = {"dim": "supplier", "value": name}
    if exclude:
        item["exclude"] = True
    return item


# threshold_count with the boundary operators.
def test_threshold_count_customer_gte_gt_lte_lt() -> None:
    # Valid March customers: c1 2 orders (A1, A2), c2 2 (A3, A4), c3 3 (A5, A7, A10).
    compiler = Compiler(make_timed_db())
    assert compiler.answer(threshold("customer", "order_count", "gte", 2), [MARCH]) == Answer("number", 3)
    assert compiler.answer(threshold("customer", "order_count", "gt", 2), [MARCH]) == Answer("number", 1)
    assert compiler.answer(threshold("customer", "order_count", "lte", 2), [MARCH]) == Answer("number", 2)
    assert compiler.answer(threshold("customer", "order_count", "lt", 2), [MARCH]) == Answer("number", 0)


def test_threshold_count_customer_revenue_and_null_key_excluded() -> None:
    # Revenue: c1 100+50 = 150, c2 40+70 = 110, c3 30+20+25 = 75 (A6 returned). A12 has no customer key, so it is not counted.
    compiler = Compiler(make_timed_db())
    assert compiler.answer(threshold("customer", "revenue", "gte", 100), [MARCH]) == Answer("number", 2)
    assert compiler.answer(threshold("customer", "revenue", "gt", 150), [MARCH]) == Answer("number", 0)


def test_threshold_count_order_and_day_subjects() -> None:
    # Valid March orders with revenue at least 50: A1 100, A2 50, A4 70 = 3; over 50: A1, A4 = 2.
    compiler = Compiler(make_timed_db())
    assert compiler.answer(threshold("order", "revenue", "gte", 50), [MARCH]) == Answer("number", 3)
    assert compiler.answer(threshold("order", "revenue", "gt", 50), [MARCH]) == Answer("number", 2)
    # Days by revenue: 03-02 190, 03-03 100, 03-04 20, 03-06 25, 03-08 15. At least 100: 2 days.
    assert compiler.answer(threshold("day", "revenue", "gte", 100), [MARCH]) == Answer("number", 2)
    assert compiler.answer(threshold("day", "revenue", "gt", 100), [MARCH]) == Answer("number", 1)
    # Days by order count: 03-02 3, 03-03 2, and three days with 1. At least 2: 2 days.
    assert compiler.answer(threshold("day", "order_count", "gte", 2), [MARCH]) == Answer("number", 2)


def test_threshold_count_product_metrics() -> None:
    # Tea: 3 orders, qty 15, amount 220. Cake: 2 orders, qty 4, amount 70. Coffee: 2 orders, qty 3, amount 50.
    compiler = Compiler(make_timed_db())
    assert compiler.answer(threshold("product", "order_count", "gte", 2), [MARCH]) == Answer("number", 3)
    assert compiler.answer(threshold("product", "order_count", "gt", 2), [MARCH]) == Answer("number", 1)
    # Quantity in base units: at least 4 includes cake (4); over 4 leaves only tea.
    assert compiler.answer(threshold("product", "qty", "gte", 4), [MARCH]) == Answer("number", 2)
    assert compiler.answer(threshold("product", "qty", "gt", 4), [MARCH]) == Answer("number", 1)
    # Amount: over 50 excludes coffee (50); at least 50 includes it.
    assert compiler.answer(threshold("product", "revenue", "gt", 50), [MARCH]) == Answer("number", 2)
    assert compiler.answer(threshold("product", "revenue", "gte", 50), [MARCH]) == Answer("number", 3)


# ratio_of_counts. The base is every subject with a valid order in the window.
def test_ratio_of_counts_denominator_is_subjects_with_valid_orders() -> None:
    # Base: c1, c2, c3. Left out: c5 (cancelled only), the null customer (A12) and c4 (February only).
    # Over 2 orders: c3 only, so 1 of 3 = 33.33. If c5 or the null key were in the base it would be 1 of 4 or 1 of 5.
    compiler = Compiler(make_timed_db())
    assert compiler.answer(threshold("customer", "order_count", "gt", 2, shape="ratio_of_counts"), [MARCH]) == Answer("percent", 33.33)
    assert compiler.answer(threshold("customer", "order_count", "gte", 2, shape="ratio_of_counts"), [MARCH]) == Answer("percent", 100.0)
    # Orders: 8 valid, 3 with revenue at least 50, so 37.5.
    assert compiler.answer(threshold("order", "revenue", "gte", 50, shape="ratio_of_counts"), [MARCH]) == Answer("percent", 37.5)
    # Days: 5 with valid orders, 2 with at least 2 orders, so 40.0.
    assert compiler.answer(threshold("day", "order_count", "gte", 2, shape="ratio_of_counts"), [MARCH]) == Answer("percent", 40.0)


def test_threshold_shapes_need_a_threshold_and_no_other_shape_takes_one() -> None:
    compiler = Compiler(make_timed_db())
    with pytest.raises(Unsupported, match="needs a threshold"):
        compiler.answer(Intent(measure="order_count", shape="threshold_count"), [MARCH])
    with pytest.raises(Unsupported, match="needs a threshold"):
        compiler.answer(Intent(measure="order_count", shape="ratio_of_counts"), [MARCH])
    # A scalar with a threshold would drop the condition and return a plain total.
    scalar = Intent(measure="revenue", threshold={"subject": "order", "metric": "revenue", "op": "gte", "value": 50})
    with pytest.raises(Unsupported, match="threshold on shape scalar"):
        compiler.answer(scalar, [MARCH])
    with pytest.raises(Unsupported, match="threshold on shape top_k"):
        compiler.answer(threshold("customer", "order_count", "gte", 2, shape="top_k", group_by="channel", top_k=2), [MARCH])


def test_threshold_rejects_metric_or_filter_it_cannot_apply() -> None:
    compiler = Compiler(make_timed_db())
    # Quantity is not defined per customer.
    with pytest.raises(Unsupported, match="threshold metric qty on customer"):
        compiler.answer(threshold("customer", "qty", "gte", 2), [MARCH])
    # A product filter needs line detail, and orders have no line rows to apply it to.
    with pytest.raises(Unsupported, match="line filter"):
        compiler.answer(
            threshold("customer", "order_count", "gte", 2, filters=[product("tea")]), [MARCH]
        )


# purchase_amount. Store-wide without filters; supplier filters apply; any other filter raises.
def test_purchase_amount_store_wide_and_supplier_filters() -> None:
    compiler = Compiler(make_timed_db())
    # Receipts in March, not cancelled: R1 50, R2 44 (one receipt total, not 68), R3 40. Total 134.
    assert compiler.answer(Intent(measure="purchase_amount"), [MARCH]) == Answer("number", 134)
    # Sun Co: R1 50 + R2 44 = 94. Excluding Sun Co leaves Bean Ltd: 40.
    assert compiler.answer(Intent(measure="purchase_amount", filters=[supplier("sun")]), [MARCH]) == Answer("number", 94)
    assert compiler.answer(
        Intent(measure="purchase_amount", filters=[supplier("Sun", exclude=True)]), [MARCH]
    ) == Answer("number", 40)


def test_purchase_amount_with_product_filter_raises() -> None:
    # A product filter must not be dropped, which would return the store-wide total.
    compiler = Compiler(make_timed_db())
    with pytest.raises(Unsupported, match="purchase amount with filter product"):
        compiler.answer(Intent(measure="purchase_amount", filters=[product("tea")]), [MARCH])
    with pytest.raises(Unsupported, match="unresolved supplier"):
        compiler.answer(Intent(measure="purchase_amount", filters=[supplier("Zed")]), [MARCH])


# Shift filter. Morning 06:00 to 13:59:59, afternoon 14:00 to 21:59:59, by order time.
def test_shift_filter_on_orders() -> None:
    # Morning: A1 100, A2 50, A7 20, A12 15 = 185. Afternoon: A3 40, A4 70, A10 25 = 135.
    # A5 at 22:00 is in neither shift: 185 + 135 = 320, and the 350 total less 320 is its 30.
    compiler = Compiler(make_timed_db())
    assert compiler.value(Intent(measure="revenue", filters=[shift("sáng")]), MARCH) == 185
    assert compiler.value(Intent(measure="revenue", filters=[shift("chiều")]), MARCH) == 135
    assert compiler.value(Intent(measure="revenue"), MARCH) == 350
    # Afternoon order count: A3, A4, A10 = 3.
    assert compiler.answer(Intent(measure="order_count", filters=[shift("chiều")]), [MARCH]) == Answer("number", 3)


def test_shift_filter_on_lines() -> None:
    # Morning lines: A1 2, A2 tea 1, A2 cake 3, A7 2 = 8 base units. Afternoon: A3 1 (qty_base missing), A4 12 = 13.
    compiler = Compiler(make_timed_db())
    assert compiler.value(Intent(measure="qty_base_sold", filters=[shift("sáng")]), MARCH) == 8
    assert compiler.value(Intent(measure="qty_base_sold", filters=[shift("chiều")]), MARCH) == 13
    # Tea in the morning: A1 100 and A2 50 = 150. The shift applies on the line source too.
    assert compiler.value(Intent(measure="revenue", filters=[product("tea"), shift("sáng")]), MARCH) == 150


def test_shift_baseline_is_morning_whatever_the_filter_order() -> None:
    # Afternoon 135 against morning 185: (135 - 185) / 185 * 100 = -27.027 -> -27.03. Both orders give the same answer.
    compiler = Compiler(make_timed_db())
    afternoon_first = Intent(measure="revenue", shape="compare_periods", filters=[shift("chiều"), shift("sáng")])
    morning_first = Intent(measure="revenue", shape="compare_periods", filters=[shift("sáng"), shift("chiều")])
    assert compiler.answer(afternoon_first, [MARCH]) == Answer("percent", -27.03)
    assert compiler.answer(morning_first, [MARCH]) == Answer("percent", -27.03)
    # As a difference: afternoon minus morning = 135 - 185 = -50.
    as_value = Intent(measure="revenue", shape="compare_periods", compare_as="value", filters=[shift("chiều"), shift("sáng")])
    assert compiler.answer(as_value, [MARCH]) == Answer("number", -50)


def test_shift_rejects_unknown_word_and_dates_without_time() -> None:
    compiler = Compiler(make_timed_db())
    with pytest.raises(Unsupported, match="unknown shift"):
        compiler.value(Intent(measure="revenue", filters=[shift("toi")]), MARCH)
    # Same rows with the time stripped: a shop that records dates only cannot say which shift an order is in.
    dates_only = make_timed_db(
        orders=[(o[0], o[1], o[1], *o[3:]) for o in T_ORDERS],
        lines=[(l[0], l[1], l[1], *l[3:]) for l in T_LINES],
    )
    with pytest.raises(Unsupported, match="order time unavailable"):
        Compiler(dates_only).value(Intent(measure="revenue", filters=[shift("sáng")]), MARCH)


# Ranking labels and top_k.
def test_top_k_one_or_none_returns_single_name() -> None:
    # Revenue by customer: Lan 150, Minh 110, then a customer with no name at 75.
    compiler = Compiler(make_timed_db())
    top_none = Intent(measure="revenue", shape="top_k", group_by="customer")
    assert compiler.answer(top_none, [MARCH]) == Answer("string", "Lan")
    top_one = Intent(measure="revenue", shape="top_k", group_by="customer", top_k=1)
    assert compiler.answer(top_one, [MARCH]) == Answer("string", "Lan")
    top_two = Intent(measure="revenue", shape="top_k", group_by="customer", top_k=2)
    assert compiler.answer(top_two, [MARCH]) == Answer("list", ["Lan", "Minh"])


def test_null_customer_name_in_ranking_raises() -> None:
    # The third customer in revenue order has no name. Asking for three names would list a null, so it raises.
    compiler = Compiler(make_timed_db())
    with pytest.raises(Unsupported, match="no name"):
        compiler.answer(Intent(measure="revenue", shape="top_k", group_by="customer", top_k=3), [MARCH])
    # The unnamed customer is not returned when only two names are asked for.
    two = Intent(measure="revenue", shape="top_k", group_by="customer", top_k=2)
    assert compiler.answer(two, [MARCH]) == Answer("list", ["Lan", "Minh"])


def spelled_lines_db(lines) -> sqlite3.Connection:
    # One order per line, so the status and revenue gates see the same rows.
    orders = [(l[0], l[1], l[2], l[3], l[4], "Lan", l[5], l[10], l[10]) for l in lines]
    return make_timed_db(orders=orders, lines=lines)


# order_id, date, ts, status, customer_key, channel, product_key, product_name, qty, qty_base, amount
SPELLED_LINES = [
    ("L1", "2026-03-02", "2026-03-02 09:00:00", "completed", "c1", "dine", "sua", "Cà phê sữa (M)", 1, 1, 40),
    ("L2", "2026-03-03", "2026-03-03 09:00:00", "completed", "c1", "dine", "sua", "Cà phê sữa (M)", 1, 1, 40),
    ("L3", "2026-03-04", "2026-03-04 09:00:00", "completed", "c1", "dine", "sua", "Cà phê sữa (M)", 1, 1, 40),
    ("L4", "2026-03-05", "2026-03-05 09:00:00", "completed", "c2", "dine", "sua", "Ca phe sua (M)", 1, 1, 40),
    ("L5", "2026-03-06", "2026-03-06 09:00:00", "completed", "c2", "dine", "tea", "Tea", 1, 1, 100),
]


def test_top_k_prints_the_accented_spelling_of_a_product() -> None:
    # Coffee: 3 rows spelled with diacritics and 1 without, 160 in revenue. The ASCII spelling sorts first, so
    # a plain MIN would print it. Tea is 100.
    compiler = Compiler(spelled_lines_db(SPELLED_LINES))
    top_one = Intent(measure="revenue", shape="top_k", group_by="product", top_k=1)
    assert compiler.answer(top_one, [MARCH]) == Answer("string", "Cà phê sữa (M)")
    top_two = Intent(measure="revenue", shape="top_k", group_by="product", top_k=2)
    assert compiler.answer(top_two, [MARCH]) == Answer("list", ["Cà phê sữa (M)", "Tea"])


def test_accented_spellings_with_diacritics_prefer_the_more_frequent_one() -> None:
    # Both spellings have three letters with diacritics; "Cà phê sữa (M)" has three rows, the other one.
    lines = [
        ("M1", "2026-03-02", "2026-03-02 09:00:00", "completed", "c1", "dine", "sua", "Cà Phê Sữa (m)", 1, 1, 60),
        ("M2", "2026-03-03", "2026-03-03 09:00:00", "completed", "c1", "dine", "sua", "Cà phê sữa (M)", 1, 1, 40),
        ("M3", "2026-03-04", "2026-03-04 09:00:00", "completed", "c1", "dine", "sua", "Cà phê sữa (M)", 1, 1, 40),
        ("M4", "2026-03-05", "2026-03-05 09:00:00", "completed", "c1", "dine", "sua", "Cà phê sữa (M)", 1, 1, 40),
    ]
    compiler = Compiler(spelled_lines_db(lines))
    top_one = Intent(measure="revenue", shape="top_k", group_by="product")
    assert compiler.answer(top_one, [MARCH]) == Answer("string", "Cà phê sữa (M)")


def test_equal_spellings_fall_back_to_the_lowest_string() -> None:
    # Same diacritics and same row count: the lowest string wins, so the answer does not depend on row order.
    lines = [
        ("T1", "2026-03-02", "2026-03-02 09:00:00", "completed", "c1", "dine", "sua", "Cà phê", 1, 1, 50),
        ("T2", "2026-03-03", "2026-03-03 09:00:00", "completed", "c1", "dine", "sua", "Cà Phê", 1, 1, 50),
    ]
    compiler = Compiler(spelled_lines_db(lines))
    top_one = Intent(measure="revenue", shape="top_k", group_by="product")
    assert compiler.answer(top_one, [MARCH]) == Answer("string", "Cà Phê")


def test_product_group_with_no_name_still_abstains() -> None:
    lines = [("N1", "2026-03-02", "2026-03-02 09:00:00", "completed", "c1", "dine", "sua", None, 1, 1, 50)]
    compiler = Compiler(spelled_lines_db(lines))
    with pytest.raises(Unsupported, match="no name"):
        compiler.answer(Intent(measure="revenue", shape="top_k", group_by="product"), [MARCH])


# order_id, date, ts, status, customer_key, customer_name, channel, revenue, total_due
SPELLED_CUSTOMER_ORDERS = [
    ("Q1", "2026-03-02", "2026-03-02 09:00:00", "completed", "c1", "Bích Ngọc", "dine", 100, 100),
    ("Q2", "2026-03-03", "2026-03-03 09:00:00", "completed", "c1", "Bích Ngọc", "dine", 100, 100),
    ("Q3", "2026-03-04", "2026-03-04 09:00:00", "completed", "c1", "Bích Ngọc", "dine", 100, 100),
    ("Q4", "2026-03-05", "2026-03-05 09:00:00", "completed", "c1", "Bich Ngoc", "dine", 100, 100),
    ("Q5", "2026-03-06", "2026-03-06 09:00:00", "completed", "c2", "Minh", "dine", 150, 150),
]


def test_customer_ranking_prints_the_accented_spelling_of_a_customer() -> None:
    # Bích Ngọc: 400 in revenue over 3 accented rows and 1 ASCII row. Minh is 150.
    compiler = Compiler(make_timed_db(orders=SPELLED_CUSTOMER_ORDERS, lines=[]))
    top_one = Intent(measure="revenue", shape="top_k", group_by="customer", top_k=1)
    assert compiler.answer(top_one, [MARCH]) == Answer("string", "Bích Ngọc")
    top_two = Intent(measure="revenue", shape="top_k", group_by="customer", top_k=2)
    assert compiler.answer(top_two, [MARCH]) == Answer("list", ["Bích Ngọc", "Minh"])


def test_customer_with_two_accented_spellings_prefers_the_more_frequent_one() -> None:
    orders = [
        ("R1", "2026-03-02", "2026-03-02 09:00:00", "completed", "c1", "Đức Anh", "dine", 60, 60),
        ("R2", "2026-03-03", "2026-03-03 09:00:00", "completed", "c1", "ĐỨC ANH", "dine", 60, 60),
        ("R3", "2026-03-04", "2026-03-04 09:00:00", "completed", "c1", "ĐỨC ANH", "dine", 60, 60),
        ("R4", "2026-03-05", "2026-03-05 09:00:00", "completed", "c1", "ĐỨC ANH", "dine", 60, 60),
    ]
    compiler = Compiler(make_timed_db(orders=orders, lines=[]))
    top_one = Intent(measure="revenue", shape="top_k", group_by="customer")
    assert compiler.answer(top_one, [MARCH]) == Answer("string", "ĐỨC ANH")


# order_total_due.
def test_order_total_due_sums_valid_orders() -> None:
    # Totals: A1 110, A2 50, A3 45, A4 80, A5 30, A10 25, A12 15. A7 has none and adds nothing. Sum 355.
    compiler = Compiler(make_timed_db())
    assert compiler.answer(Intent(measure="order_total_due"), [MARCH]) == Answer("number", 355)


def test_order_total_due_rounds_half_up_and_needs_a_total() -> None:
    compiler = Compiler(make_timed_db(orders=[("X1", "2026-03-02", "2026-03-02 10:00:00", "completed", "c1", "Lan", "dine", 10.5, 10.5)], lines=[], purchases=[]))
    # 10.5 rounds half up to 11.
    assert compiler.answer(Intent(measure="order_total_due"), [MARCH]) == Answer("number", 11)
    all_null = make_timed_db(
        orders=[("X1", "2026-03-02", "2026-03-02 10:00:00", "completed", "c1", "Lan", "dine", 10, None)], lines=[], purchases=[]
    )
    with pytest.raises(Unsupported, match="no total_due"):
        Compiler(all_null).answer(Intent(measure="order_total_due"), [MARCH])
    # A product filter needs lines, and the order total is not held per line.
    with pytest.raises(Unsupported, match="line filter"):
        Compiler(make_timed_db()).value(Intent(measure="order_total_due", filters=[product("tea")]), MARCH)


# Small hand-built line fixtures for the unit, size and threshold changes. Every line has an order row with a status.
def db_with_lines(extra_columns: str, rows: list[tuple]) -> sqlite3.Connection:
    """`rows` hold order_id, date, status, product_key, product_name, then the columns named in `extra_columns`."""
    db = sqlite3.connect(":memory:")
    db.create_function("lookup", 1, lambda v: None if v is None else normalize_lookup(v))
    db.executescript(
        "CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, customer_key TEXT, channel TEXT, revenue REAL);"
        f"CREATE TABLE lines (order_id TEXT, date TEXT, status TEXT, product_key TEXT, product_name TEXT, {extra_columns});"
    )
    seen = set()
    for row in rows:
        if row[0] not in seen:
            seen.add(row[0])
            db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?)", (row[0], row[1], row[2], "c1", "dine", 0))
    placeholders = ",".join("?" for _ in rows[0])
    db.executemany(f"INSERT INTO lines VALUES ({placeholders})", rows)
    db.commit()
    return db


# Unit filter. Rulebook §3: "bán được bao nhiêu <đơn vị>" sums base-unit quantity of products whose base unit is that unit.
# order_id, date, status, product_key, product_name, unit, base_unit, qty, qty_base, amount
BASE_UNIT_LINES = [
    ("O1", "2026-03-02", "completed", "tea", "Tea", "bottle", "bottle", 2, 2, 20),
    ("O2", "2026-03-03", "completed", "tea", "Tea", "case", "bottle", 1, 12, 100),   # a case of 12 bottles
    ("O3", "2026-03-04", "completed", "juice", "Juice", "bottle", "box", 3, 3, 30),  # sold in bottles, base is box
    ("O4", "2026-03-05", "cancelled", "tea", "Tea", "bottle", "bottle", 5, 5, 50),   # cancelled: never counts
]
UNIT_COLUMNS = "unit TEXT, base_unit TEXT, qty REAL, qty_base REAL, amount REAL"
OLD_UNIT_COLUMNS = "unit TEXT, qty REAL, qty_base REAL, amount REAL"


def without_base_unit(row: tuple) -> tuple:
    return row[:6] + row[7:]


def qty_base_for_unit(compiler: Compiler, value: str, exclude: bool = False) -> float:
    item = {"dim": "unit", "value": value}
    if exclude:
        item["exclude"] = True
    return compiler.value(Intent(measure="qty_base_sold", filters=[item]), MARCH)


def test_unit_filter_uses_base_unit_so_a_larger_unit_sale_counts() -> None:
    # Valid lines whose base unit is bottle: O1 2 + O2 12 (a case) = 14. O3 is sold in bottles but its base unit is box,
    # so it is left out. The cancelled O4 is left out. Matching on the sold unit would give 2 + 3 = 5.
    compiler = Compiler(db_with_lines(UNIT_COLUMNS, BASE_UNIT_LINES))
    assert qty_base_for_unit(compiler, "bottle") == 14


def test_unit_filter_base_unit_is_matched_exactly_after_lookup_normalization() -> None:
    compiler = Compiler(db_with_lines(UNIT_COLUMNS, BASE_UNIT_LINES))
    assert qty_base_for_unit(compiler, "Bottle") == 14
    # "box" is the base unit of juice only: 3.
    assert qty_base_for_unit(compiler, "box") == 3
    # A unit that is no base unit is not a partial match: "bottl" must not resolve.
    with pytest.raises(Unsupported, match="unresolved unit"):
        qty_base_for_unit(compiler, "bottl")


def test_unit_exclusion_uses_base_unit_and_keeps_other_base_units() -> None:
    # Excluding bottle keeps juice, whose base unit is box: 3.
    compiler = Compiler(db_with_lines(UNIT_COLUMNS, BASE_UNIT_LINES))
    assert qty_base_for_unit(compiler, "bottle", exclude=True) == 3


def test_unit_filter_without_base_unit_column_keeps_sold_unit_matching() -> None:
    # Fixture without base_unit: the old behaviour matches the sold unit. bottle lines: O1 2 + O3 3 = 5.
    rows = [without_base_unit(row) for row in BASE_UNIT_LINES]
    compiler = Compiler(db_with_lines(OLD_UNIT_COLUMNS, rows))
    assert qty_base_for_unit(compiler, "bottle") == 5


def test_unit_filter_with_base_unit_column_all_null_keeps_sold_unit_matching() -> None:
    rows = [row[:6] + (None,) + row[7:] for row in BASE_UNIT_LINES]
    compiler = Compiler(db_with_lines(UNIT_COLUMNS, rows))
    assert qty_base_for_unit(compiler, "bottle") == 5


# Size with add-ons. The size is read from the name before the add-on, so it is still found when the add-on follows it.
# order_id, date, status, product_key, product_name, qty, qty_base, amount
SIZE_LINES = [
    ("S1", "2026-03-02", "completed", "milk tea", "Milk tea (M)", 1, 1, 30),
    ("S2", "2026-03-03", "completed", "milk tea", "Milk tea (M) + pearl (5.000)", 2, 2, 40),
    ("S3", "2026-03-04", "completed", "milk tea", "Milk tea (L) + pearl (M)", 1, 1, 50),
    ("S4", "2026-03-05", "completed", "coffee", "Coffee (L)", 4, 4, 60),
]


def test_size_reads_the_name_before_an_add_on() -> None:
    # Size M: S1 30 and S2 40. S3 ends in (M) only inside its add-on, so it is left out: 70.
    # Matching the end of the whole name would give 30 + 50 = 80 and would miss S2.
    compiler = Compiler(db_with_lines("qty REAL, qty_base REAL, amount REAL", SIZE_LINES))
    size_m = Intent(measure="revenue", filters=[{"dim": "size", "value": "M"}])
    assert compiler.value(size_m, MARCH) == 70


def test_size_l_counts_the_add_on_free_name_and_the_add_on_is_not_a_size() -> None:
    # Size L: S3 50 (its head is Milk tea (L)) and S4 60. Total 110.
    compiler = Compiler(db_with_lines("qty REAL, qty_base REAL, amount REAL", SIZE_LINES))
    assert compiler.value(Intent(measure="revenue", filters=[{"dim": "size", "value": "L"}]), MARCH) == 110


def test_size_exclusion_keeps_names_without_the_size() -> None:
    # Excluding M removes S1 and S2 (size M). S3 has size L before its M add-on, so it stays: 50 + 60 = 110.
    compiler = Compiler(db_with_lines("qty REAL, qty_base REAL, amount REAL", SIZE_LINES))
    excluded = [{"dim": "size", "value": "M", "exclude": True}]
    assert compiler.value(Intent(measure="revenue", filters=excluded), MARCH) == 110


# Threshold on products uses the same service-line rule as ranking (rulebook §3).
# order_id, date, status, product_key, product_name, is_item, qty, qty_base, amount
THRESHOLD_ITEM_LINES = [
    ("T1", "2026-03-02", "completed", "tea", "Tea", 1, 1, 1, 20),
    ("T2", "2026-03-03", "completed", "tea", "Tea", 1, 1, 1, 20),
    ("T3", "2026-03-04", "completed", "service", "Service charge", 0, 1, 1, 5),  # service fee: not a product
    ("T4", "2026-03-05", "completed", "service", "Service charge", 0, 1, 1, 5),
    ("T5", "2026-03-06", "completed", "cake", "Cake", 1, 1, 1, 30),
]


def test_threshold_on_products_leaves_out_service_lines() -> None:
    # Products with at least one order: tea and cake. Service charge is not a product, so it is not counted.
    # Without the item filter the service charge would make the first count three.
    compiler = Compiler(db_with_lines("is_item INTEGER, qty REAL, qty_base REAL, amount REAL", THRESHOLD_ITEM_LINES))
    assert compiler.answer(threshold("product", "order_count", "gte", 1), [MARCH]) == Answer("number", 2)
    # Tea has two orders; cake one. At least 2: only tea.
    assert compiler.answer(threshold("product", "order_count", "gte", 2), [MARCH]) == Answer("number", 1)
    # Revenue over 25: tea 40 and cake 30. The service lines (5 each) would not change this count, so only the order count shows the rule.
    assert compiler.answer(threshold("product", "revenue", "gt", 25), [MARCH]) == Answer("number", 2)


def test_threshold_on_products_percent_base_excludes_service_lines() -> None:
    # Base for a ratio is the two products. Two of two have at least one order.
    compiler = Compiler(db_with_lines("is_item INTEGER, qty REAL, qty_base REAL, amount REAL", THRESHOLD_ITEM_LINES))
    result = compiler.answer(
        threshold("product", "order_count", "gte", 1, shape="ratio_of_counts"), [MARCH]
    )
    assert result == Answer("percent", 100.0)


# distinct_customers keeps its own rule: a scalar with a threshold is not answered.
def test_distinct_customers_scalar_with_threshold_still_raises() -> None:
    compiler = Compiler(make_timed_db())
    intent = Intent(
        measure="distinct_customers", shape="scalar",
        threshold={"subject": "customer", "metric": "order_count", "op": "gte", "value": 2},
    )
    with pytest.raises(Unsupported, match="threshold on shape scalar"):
        compiler.answer(intent, [MARCH])


# ---------------------------------------------------------------------------------------------------------------------
# Changes to the filter, ranking, shift, age, delivery, trend, two-measure and weekday handling.
# Each fixture is built by hand here; the expected values are worked in the comments.


def build(schema: str, **tables: list[tuple]) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(schema)
    for name, rows in tables.items():
        if rows:
            db.executemany(f"INSERT INTO {name} VALUES ({','.join('?' for _ in rows[0])})", rows)
    db.commit()
    return db


DRUG_SCHEMA = """
CREATE TABLE orders (order_id TEXT, date TEXT, ts TEXT, status TEXT, customer_key TEXT, customer_name TEXT,
                     channel TEXT, revenue REAL, total_due REAL);
CREATE TABLE lines (order_id TEXT, date TEXT, ts TEXT, status TEXT, customer_key TEXT, channel TEXT,
                    product_key TEXT, product_name TEXT, qty REAL, qty_base REAL, amount REAL,
                    prescription INTEGER, active_ingredient TEXT, product_kind TEXT);
"""
# order_id, date, ts, status, customer_key, customer_name, channel, revenue, total_due
DRUG_ORDERS = [
    ("B1", "2026-03-02", "2026-03-02 09:00:00", "completed", "c1", "Lan", "dine", 100, 100),
    ("B2", "2026-03-03", "2026-03-03 15:00:00", "completed", "c2", "Minh", "take", 90, 90),
    ("B3", "2026-03-04", "2026-03-04 10:00:00", "cancelled", "c1", "Lan", "dine", 500, 500),
    ("B4", "2026-03-05", "2026-03-05 16:00:00", "completed", "c3", "Hoa", "dine", 40, 40),
]
# order_id, date, ts, status, customer_key, channel, product_key, product_name, qty, qty_base, amount,
# prescription, active_ingredient, product_kind
DRUG_LINES = [
    ("B1", "2026-03-02", "2026-03-02 09:00:00", "completed", "c1", "dine", "amox", "Amoxi 500", 1, 1, 100,
     1, "amoxicillin", "drug"),
    ("B2", "2026-03-03", "2026-03-03 15:00:00", "completed", "c2", "take", "vitc", "Vitamin C", 2, 2, 60,
     0, "ascorbic acid", "drug"),
    ("B2", "2026-03-03", "2026-03-03 15:00:00", "completed", "c2", "take", "combo", "Combo", 1, 1, 30,
     1, "amoxicillin + clavulanate", "drug"),
    ("B3", "2026-03-04", "2026-03-04 10:00:00", "cancelled", "c1", "dine", "amox", "Amoxi 500", 1, 1, 500,
     1, "amoxicillin", "drug"),
    ("B4", "2026-03-05", "2026-03-05 16:00:00", "completed", "c3", "dine", "phone", "Phone X", 1, 1, 40,
     None, None, "used"),
]


def drug_db(orders=DRUG_ORDERS, lines=DRUG_LINES) -> sqlite3.Connection:
    return build(DRUG_SCHEMA, orders=orders, lines=lines)


def by(dim: str, value: str, exclude: bool = False) -> dict:
    item = {"dim": dim, "value": value}
    if exclude:
        item["exclude"] = True
    return item


# Change 1: prescription, active_ingredient and product_kind. Valid March lines: B1 100, B2 vitamin 60,
# B2 combo 30, B4 phone 40. The cancelled B3 line is left out.
def test_prescription_yes_no_and_exclusion() -> None:
    compiler = Compiler(drug_db())
    assert compiler.value(Intent(measure="revenue", filters=[by("prescription", "yes")]), MARCH) == 130
    # "no" and "khong ke don" both mean 0: the vitamin line only.
    assert compiler.value(Intent(measure="revenue", filters=[by("prescription", "no")]), MARCH) == 60
    assert compiler.value(Intent(measure="revenue", filters=[by("prescription", "khong ke don")]), MARCH) == 60
    # Excluding prescription yes keeps the vitamin line (0) and the phone (NULL): 60 + 40 = 100.
    excluded = Intent(measure="revenue", filters=[by("prescription", "yes", exclude=True)])
    assert compiler.value(excluded, MARCH) == 100


def test_active_ingredient_is_an_exact_whole_string_match() -> None:
    compiler = Compiler(drug_db())
    # Only Amoxi 500 has "amoxicillin" on its own: 100 (the cancelled line is left out).
    assert compiler.value(Intent(measure="revenue", filters=[by("active_ingredient", "Amoxicillin")]), MARCH) == 100
    # The combination is its own kind of drug: "amoxicillin + clavulanate" is 30, not counted in "amoxicillin".
    combo = Intent(measure="revenue", filters=[by("active_ingredient", "amoxicillin + clavulanate")])
    assert compiler.value(combo, MARCH) == 30
    # A partial ingredient name does not match, even though it is a substring of a stored one.
    with pytest.raises(Unsupported, match="unresolved active_ingredient"):
        compiler.value(Intent(measure="revenue", filters=[by("active_ingredient", "amoxi")]), MARCH)


def test_product_kind_matches_normalized_value() -> None:
    compiler = Compiler(drug_db())
    # drug: vitamin 60 + amoxi 100 + combo 30 = 190; used: phone 40.
    assert compiler.value(Intent(measure="revenue", filters=[by("product_kind", "DRUG")]), MARCH) == 190
    assert compiler.value(Intent(measure="revenue", filters=[by("product_kind", "used")]), MARCH) == 40
    with pytest.raises(Unsupported, match="unresolved product_kind"):
        compiler.value(Intent(measure="revenue", filters=[by("product_kind", "new")]), MARCH)


def test_drug_dimensions_raise_when_the_columns_are_absent() -> None:
    # Timed fixture: lines carry no prescription, active_ingredient or product_kind column. Nothing else changes.
    compiler = Compiler(make_timed_db())
    for dim, value in [("prescription", "yes"), ("active_ingredient", "x"), ("product_kind", "x")]:
        with pytest.raises(Unsupported, match=f"{dim} unavailable"):
            compiler.value(Intent(measure="revenue", filters=[by(dim, value)]), MARCH)
    assert compiler.value(Intent(measure="revenue"), MARCH) == 350


# Change 2: generic words are removed at the start and end before an exact match; partial matches are kept only
# when they name exactly one stored value; customer and customer_group never match in part.
def test_generic_word_is_removed_before_an_exact_match() -> None:
    compiler = Compiler(drug_db())
    # "san pham" is generic: "san pham vitamin c" becomes "vitamin c", which is the stored product: 60.
    assert compiler.value(Intent(measure="revenue", filters=[by("product", "San pham Vitamin C")]), MARCH) == 60
    # "khach" is generic: "khach Lan" becomes "lan", the name of customer c1: valid B1 100 (B3 is cancelled).
    assert compiler.value(Intent(measure="revenue", filters=[by("customer", "khach Lan")]), MARCH) == 100


def test_partial_match_only_when_it_yields_one_value() -> None:
    compiler = Compiler(drug_db())
    # "Vitamin" is part of one product only: 60.
    assert compiler.value(Intent(measure="revenue", filters=[by("product", "Vitamin")]), MARCH) == 60
    # "di" is part of the channel dine only: valid dine revenue 100 + 40 = 140.
    assert compiler.value(Intent(measure="revenue", filters=[by("channel", "di")]), MARCH) == 140
    # "o" is part of three products: no single match, so no answer.
    with pytest.raises(Unsupported, match="unresolved product"):
        compiler.value(Intent(measure="revenue", filters=[by("product", "o")]), MARCH)


def test_customer_never_matches_in_part() -> None:
    compiler = Compiler(drug_db())
    # "La" is part of the name Lan, but a customer named in part is another customer, so it does not resolve.
    with pytest.raises(Unsupported, match="unresolved customer"):
        compiler.value(Intent(measure="revenue", filters=[by("customer", "La")]), MARCH)


# Change 3: a customer filter matches the code or the stored name. One name over two codes is ambiguous.
def test_customer_name_mapping_to_two_codes_raises() -> None:
    orders = DRUG_ORDERS + [("B5", "2026-03-06", "2026-03-06 11:00:00", "completed", "c9", "Lan", "dine", 25, 25)]
    compiler = Compiler(drug_db(orders=orders, lines=DRUG_LINES))
    # "Lan" is c1 and c9: two codes with one name, which rulebook section 3 counts as two customers.
    with pytest.raises(Unsupported, match="ambiguous customer name"):
        compiler.value(Intent(measure="revenue", filters=[by("customer", "Lan")]), MARCH)
    # The code itself is unambiguous: c9 is 25.
    assert compiler.value(Intent(measure="revenue", filters=[by("customer", "c9")]), MARCH) == 25


def test_customer_matches_stored_name_when_name_is_unique() -> None:
    compiler = Compiler(drug_db())
    # Minh is customer c2 (name only): valid B2 revenue 90.
    assert compiler.value(Intent(measure="revenue", filters=[by("customer", "minh")]), MARCH) == 90
    assert compiler.value(Intent(measure="revenue", filters=[by("customer", "c3")]), MARCH) == 40


# Change 4: suppliers ranked by import money (per receipt) and by number of receipts. Timed purchases:
# Sun Co R1 50 + R2 44 = 94 with 2 receipts; Bean Ltd R3 40 (amount missing, so 5 * 8) with 1 receipt.
# R4 is cancelled and R5 is in February.
def test_supplier_ranking_by_purchase_amount_and_receipt_count() -> None:
    compiler = Compiler(make_timed_db())
    top = Intent(measure="purchase_amount", shape="top_k", group_by="supplier", top_k=2)
    assert compiler.answer(top, [MARCH]) == Answer("list", ["Sun Co", "Bean Ltd"])
    most_receipts = Intent(measure="purchase_receipt_count", shape="argmax_entity", group_by="supplier")
    assert compiler.answer(most_receipts, [MARCH]) == Answer("string", "Sun Co")
    by_money = Intent(measure="purchase_amount", shape="argmax_entity", group_by="supplier")
    assert compiler.answer(by_money, [MARCH]) == Answer("string", "Sun Co")


def test_supplier_ranking_uses_id_when_no_receipt_has_a_name() -> None:
    rows = [(p[0], p[1], None, p[3], *p[4:]) for p in T_PURCHASES]
    compiler = Compiler(make_timed_db(purchases=rows))
    # S1 is 50 + 44 = 94, S2 is 40.
    assert compiler.answer(Intent(measure="purchase_amount", shape="argmax_entity", group_by="supplier"), [MARCH]) == Answer("string", "S1")


def test_supplier_ranking_rejects_a_product_filter() -> None:
    compiler = Compiler(make_timed_db())
    intent = Intent(measure="purchase_receipt_count", shape="argmax_entity", group_by="supplier", filters=[product("tea")])
    with pytest.raises(Unsupported, match="purchase amount with filter product"):
        compiler.answer(intent, [MARCH])


# Change 5: one named shift is compared with the other shift, the morning shift being the baseline.
# Morning 185, afternoon 135: (135 - 185) / 185 * 100 = -27.027 -> -27.03; as a difference, 135 - 185 = -50.
def test_single_named_shift_compares_afternoon_over_morning() -> None:
    compiler = Compiler(make_timed_db())
    afternoon = Intent(measure="revenue", shape="compare_periods", filters=[shift("chiều")])
    assert compiler.answer(afternoon, [MARCH]) == Answer("percent", -27.03)
    morning = Intent(measure="revenue", shape="compare_periods", filters=[shift("sáng")])
    assert compiler.answer(morning, [MARCH]) == Answer("percent", -27.03)
    as_value = Intent(measure="revenue", shape="compare_periods", compare_as="value", filters=[shift("chiều")])
    assert compiler.answer(as_value, [MARCH]) == Answer("number", -50)


def test_single_shift_exclusion_has_no_comparison() -> None:
    compiler = Compiler(make_timed_db())
    intent = Intent(measure="revenue", shape="compare_periods", filters=[{"dim": "shift", "value": "sáng", "exclude": True}])
    with pytest.raises(Unsupported):
        compiler.answer(intent, [MARCH])


# Change 6: an age on a non-debt measure is ignored. Valid March revenue is 350 and order count is 8.
def test_age_on_non_debt_measure_is_ignored_in_answers() -> None:
    compiler = Compiler(make_timed_db())
    assert compiler.answer(Intent(measure="revenue", age_days=7), [MARCH]) == Answer("number", 350)
    assert compiler.answer(Intent(measure="order_count", age_days=7), [MARCH]) == Answer("number", 8)


# Change 7: delivery trips are the distinct (order, trip) pairs dated in the window, whatever the order status.
# March: (D1, t1), (D1, t2), (D2, t1), (D4, t1) = 4. February: (D3, t1) = 1. The second D1 line repeats (D1, t1).
DELIVERY_SCHEMA = """
CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, revenue REAL);
CREATE TABLE lines (order_id TEXT, date TEXT, status TEXT, amount REAL);
CREATE TABLE deliveries (order_id TEXT, date TEXT, trip TEXT, qty REAL);
"""
DELIVERY_ROWS = [
    ("D1", "2026-03-02", "t1", 1), ("D1", "2026-03-02", "t1", 2), ("D1", "2026-03-09", "t2", 1),
    ("D2", "2026-03-05", "t1", 3), ("D3", "2026-02-20", "t1", 1), ("D4", "2026-03-31", "t1", 1),
]


def test_delivery_trip_count_counts_distinct_order_and_trip() -> None:
    compiler = Compiler(build(DELIVERY_SCHEMA, deliveries=DELIVERY_ROWS))
    assert compiler.answer(Intent(measure="delivery_trip_count"), [MARCH]) == Answer("number", 4)
    assert compiler.answer(Intent(measure="delivery_trip_count"), [FEBRUARY]) == Answer("number", 1)


def test_delivery_trip_count_needs_rows_and_no_filter() -> None:
    empty = Compiler(build(DELIVERY_SCHEMA))
    with pytest.raises(Unsupported, match="no deliveries"):
        empty.value(Intent(measure="delivery_trip_count"), MARCH)
    compiler = Compiler(build(DELIVERY_SCHEMA, deliveries=DELIVERY_ROWS))
    with pytest.raises(Unsupported, match="delivery trips with a filter"):
        compiler.value(Intent(measure="delivery_trip_count", filters=[product("tea")]), MARCH)
    with pytest.raises(Unsupported, match="no deliveries table"):
        Compiler(build("CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, revenue REAL);"
                       "CREATE TABLE lines (order_id TEXT, date TEXT, status TEXT, amount REAL);")).value(
            Intent(measure="delivery_trip_count"), MARCH)


# Change 8: strictly increasing across the windows given, in order. Totals: January 50, February 100, March 150.
TREND_SCHEMA = """
CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, revenue REAL);
CREATE TABLE lines (order_id TEXT, date TEXT, status TEXT, amount REAL);
"""
TREND_ORDERS = [
    ("J1", "2026-01-10", "completed", 50), ("F1", "2026-02-10", "completed", 100),
    ("M1", "2026-03-10", "completed", 100), ("M2", "2026-03-20", "completed", 50),
]
JANUARY = TimeWindow(start=date(2026, 1, 1), end_exclusive=date(2026, 2, 1))


def test_trend_check_is_strict_and_in_window_order() -> None:
    compiler = Compiler(build(TREND_SCHEMA, orders=TREND_ORDERS))
    trend = Intent(measure="revenue", shape="trend_check")
    assert compiler.answer(trend, [JANUARY, FEBRUARY, MARCH]) == Answer("boolean", True)
    # Equal neighbours are not an increase.
    assert compiler.answer(trend, [FEBRUARY, FEBRUARY]) == Answer("boolean", False)
    assert compiler.answer(trend, [JANUARY, FEBRUARY, MARCH, MARCH]) == Answer("boolean", False)
    # Reversed order is a fall.
    assert compiler.answer(trend, [MARCH, FEBRUARY, JANUARY]) == Answer("boolean", False)


def test_trend_check_with_an_empty_window_raises() -> None:
    compiler = Compiler(build(TREND_SCHEMA, orders=TREND_ORDERS))
    trend = Intent(measure="revenue", shape="trend_check")
    # April has no orders, so its value is None and no yes or no can be given.
    with pytest.raises(Unsupported, match="empty period"):
        compiler.answer(trend, [JANUARY, APRIL])
    with pytest.raises(Unsupported, match="two windows"):
        compiler.answer(trend, [JANUARY])


# Change 9: a second measure on a scalar gives a list in the order asked. Timed March: revenue 350, order count 8,
# store-wide purchase amount 134 (R1 50, R2 44, R3 40).
def test_second_measure_gives_a_list() -> None:
    compiler = Compiler(make_timed_db())
    assert compiler.answer(Intent(measure="revenue", measure2="order_count"), [MARCH]) == Answer("list", [350, 8])
    assert compiler.answer(Intent(measure="revenue", measure2="purchase_amount"), [MARCH]) == Answer("list", [350, 134])


def test_second_measure_on_another_shape_raises() -> None:
    compiler = Compiler(make_timed_db())
    intent = Intent(measure="revenue", measure2="order_count", shape="top_k", group_by="customer", top_k=2)
    with pytest.raises(Unsupported, match="second measure"):
        compiler.answer(intent, [MARCH])


def test_location_is_not_silently_dropped() -> None:
    compiler = Compiler(make_timed_db())
    with pytest.raises(Unsupported, match="location"):
        compiler.answer(Intent(measure="revenue", location="Nguyen Hue"), [MARCH])


# Change 11: weekday filter. March 2026 has five Mondays (2, 9, 16, 23, 30) and five Tuesdays.
# Valid March orders on Monday: A1 100, A2 50, A3 40, so 3 orders and 190. On Tuesday: A4 70, A5 30, 2 orders, 100.
# Valid March total is 350, so excluding Monday leaves 160.
def test_weekday_filter_by_name_and_number() -> None:
    compiler = Compiler(make_timed_db())
    monday = [by("weekday", "thu 2")]
    assert compiler.value(Intent(measure="revenue", filters=monday), MARCH) == 190
    assert compiler.value(Intent(measure="revenue", filters=[by("weekday", "2")]), MARCH) == 190
    assert compiler.value(Intent(measure="revenue", filters=[by("weekday", "Monday")]), MARCH) == 190
    assert compiler.value(Intent(measure="revenue", filters=[by("weekday", "thu 3")]), MARCH) == 100
    # Sunday (chu nhat): A12 15.
    assert compiler.value(Intent(measure="revenue", filters=[by("weekday", "chu nhat")]), MARCH) == 15


def test_weekday_exclusion_and_unknown_weekday() -> None:
    compiler = Compiler(make_timed_db())
    assert compiler.value(Intent(measure="revenue", filters=[by("weekday", "thu 2", exclude=True)]), MARCH) == 160
    with pytest.raises(Unsupported, match="unknown weekday"):
        compiler.value(Intent(measure="revenue", filters=[by("weekday", "thu 9")]), MARCH)


def test_weekday_average_divides_by_the_days_of_that_weekday() -> None:
    compiler = Compiler(make_timed_db())
    # Mondays in March: 5, including days with no order. Order count 3 / 5 = 0.6; revenue 190 / 5 = 38.
    average_orders = Intent(measure="order_count", shape="scalar", filters=[by("weekday", "thu 2")],
                            compare_as="average_per_weekday")
    assert compiler.answer(average_orders, [MARCH]) == Answer("number", 0.6)
    average_revenue = Intent(measure="revenue", shape="scalar", filters=[by("weekday", "thu 2")],
                             compare_as="average_per_weekday")
    assert compiler.answer(average_revenue, [MARCH]) == Answer("number", 38)
    # Tuesdays in March: 5. Revenue 100 / 5 = 20.
    tuesday = Intent(measure="revenue", shape="scalar", filters=[by("weekday", "thu 3")],
                     compare_as="average_per_weekday")
    assert compiler.answer(tuesday, [MARCH]) == Answer("number", 20)


def test_weekday_average_without_a_weekday_raises() -> None:
    compiler = Compiler(make_timed_db())
    # "On average per order" for the week: there is no weekday to divide by, so the total must not be returned.
    intent = Intent(measure="revenue", shape="scalar", compare_as="average_per_weekday")
    with pytest.raises(Unsupported, match="average per weekday without a weekday"):
        compiler.answer(intent, [MARCH])
    # The same average with a weekday filter still divides by the days of that weekday.
    with_weekday = Intent(measure="revenue", shape="scalar", filters=[by("weekday", "thu 2")],
                          compare_as="average_per_weekday")
    assert compiler.answer(with_weekday, [MARCH]) == Answer("number", 38)


def test_weekday_without_average_only_filters() -> None:
    compiler = Compiler(make_timed_db())
    # The average is asked only with average_per_weekday; a plain weekday question keeps the total of that weekday.
    plain = Intent(measure="revenue", shape="scalar", filters=[by("weekday", "thu 2")])
    assert compiler.answer(plain, [MARCH]) == Answer("number", 190)
    # Average is not defined for other measures here, so the weekday only filters.
    other = Intent(measure="qty_base_sold", shape="scalar", filters=[by("weekday", "thu 2")],
                   compare_as="average_per_weekday")
    # Valid March Monday lines in base units: A1 2, A2 tea 1, A2 cake 3, A3 1 (no qty_base, so qty). Total 7.
    assert compiler.answer(other, [MARCH]) == Answer("number", 7)


# The parser sometimes labels a two-part question with the wrong shape; these cases are answered as a scalar pair.
# Timed March: revenue 350, order count 8. Revenue by channel: dine 285 (A1, A2, A4, A5, A7, A12), take 65 (A3, A10).
def test_breakdown_with_second_measure_is_answered_as_scalar_pair() -> None:
    compiler = Compiler(make_timed_db())
    intent = Intent(measure="revenue", measure2="order_count", shape="breakdown")
    scalar = Intent(measure="revenue", measure2="order_count", shape="scalar")
    assert compiler.answer(intent, [MARCH]) == compiler.answer(scalar, [MARCH]) == Answer("list", [350, 8])


def test_compare_periods_with_two_members_of_one_dim_is_answered_per_member() -> None:
    compiler = Compiler(make_timed_db())
    intent = Intent(measure="revenue", measure2="revenue", shape="compare_periods",
                    filters=[channel("dine"), channel("take")], compare_as="value")
    dine = compiler.answer(Intent(measure="revenue", shape="scalar", filters=[channel("dine")]), [MARCH])
    take = compiler.answer(Intent(measure="revenue", shape="scalar", filters=[channel("take")]), [MARCH])
    assert dine == Answer("number", 285) and take == Answer("number", 65)
    assert compiler.answer(intent, [MARCH]) == Answer("list", [285, 65])
    # The order of the filters decides the order of the answer.
    reversed_intent = Intent(measure="revenue", measure2="revenue", shape="compare_periods",
                             filters=[channel("take"), channel("dine")], compare_as="value")
    assert compiler.answer(reversed_intent, [MARCH]) == Answer("list", [65, 285])


def test_pair_with_a_missing_member_value_abstains() -> None:
    # The "bar" channel only has an order in February, so its March revenue is missing.
    february_bar = ("B1", "2026-02-11", "2026-02-11 09:00:00", "completed", "c1", "Lan", "bar", 10, 10)
    compiler = Compiler(make_timed_db(orders=T_ORDERS + [february_bar]))
    intent = Intent(measure="revenue", measure2="revenue", shape="compare_periods",
                    filters=[channel("dine"), channel("bar")], compare_as="value")
    with pytest.raises(Unsupported, match="member value missing"):
        compiler.answer(intent, [MARCH])


def test_breakdown_with_group_by_and_second_measure_still_raises() -> None:
    compiler = Compiler(make_timed_db())
    intent = Intent(measure="revenue", measure2="order_count", shape="breakdown", group_by="channel")
    with pytest.raises(Unsupported, match="second measure"):
        compiler.answer(intent, [MARCH])
