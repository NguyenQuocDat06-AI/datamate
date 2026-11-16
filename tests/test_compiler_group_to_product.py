"""Group values that name one product, and kept-column conditions on standard measures (synthetic data)."""
import sqlite3
from datetime import date

import pytest

from bizreason.contracts import TimeWindow
from bizreason.semantic.compiler import Compiler, Intent, NotRecorded, Unsupported
from bizreason.utils import normalize_lookup

MARCH = TimeWindow(role="target", start=date(2026, 3, 1), end_exclusive=date(2026, 4, 1), evidence=["q"])
APRIL = TimeWindow(role="comparison", start=date(2026, 4, 1), end_exclusive=date(2026, 5, 1), evidence=["q"])

SCHEMA = """
CREATE TABLE orders (order_id TEXT, date TEXT, ts TEXT, status TEXT, customer_key TEXT, channel TEXT,
                     revenue REAL, x_note TEXT, xd_due TEXT);
CREATE TABLE lines (order_id TEXT, date TEXT, status TEXT, customer_key TEXT, product_key TEXT, product_name TEXT,
                    category TEXT, qty REAL, qty_base REAL, amount REAL, is_item INTEGER, x_batch TEXT);
"""


def make(orders=None, lines=None, with_category=True):
    db = sqlite3.connect(":memory:")
    db.create_function("lookup", 1, lambda v: None if v is None else normalize_lookup(v))
    db.executescript(SCHEMA)
    orders = orders or [
        ("O1", "2026-03-02", None, "completed", "c1", "dine", 100, "gift wrap", "2026-06-01"),
        ("O2", "2026-03-03", None, "completed", "c2", "dine", 60, "plain", "2026-01-01"),
        ("O3", "2026-03-04", None, "completed", "c1", "take", 40, "gift card", None),
        ("O4", "2026-04-02", None, "completed", "c3", "dine", 30, "gift wrap", None),
    ]
    cat = (lambda c: c) if with_category else (lambda c: None)
    lines = lines or [
        ("O1", "2026-03-02", "completed", "c1", "tea", "Green Tea", cat("drink"), 2, 2, 70, 1, "A1"),
        ("O1", "2026-03-02", "completed", "c1", "cake", "Lemon Cake", cat("food"), 1, 1, 30, 1, "B1"),
        ("O2", "2026-03-03", "completed", "c2", "tea", "Green Tea", cat("drink"), 1, 1, 60, 1, "A1"),
        ("O3", "2026-03-04", "completed", "c1", "cake", "Lemon Cake", cat("food"), 1, 1, 40, 1, "B2"),
        ("O4", "2026-04-02", "completed", "c3", "tea", "Green Tea", cat("drink"), 1, 1, 30, 1, "A1"),
    ]
    db.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)", orders)
    db.executemany("INSERT INTO lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", lines)
    return db


def run(db, intent, windows=(MARCH,)):
    c = Compiler(db)
    c.as_of = date(2026, 5, 1)
    return c.answer(intent, list(windows))


def revenue(filters=(), column_filters=(), **kw):
    return Intent(measure="revenue", shape="scalar", filters=list(filters),
                  column_filters=list(column_filters), **kw)


# --- product_group that is really one product ---------------------------------------------------------------

def test_group_naming_one_product_becomes_product():
    assert run(make(), revenue([{"dim": "product_group", "value": "Green Tea"}])).value == 130


def test_group_naming_a_group_stays_a_group():
    assert run(make(), revenue([{"dim": "product_group", "value": "drink"}])).value == 130
    assert run(make(), revenue([{"dim": "product_group", "value": "food"}])).value == 70


def test_group_matching_no_group_and_no_product_abstains():
    with pytest.raises(Unsupported, match="unresolved product_group"):
        run(make(), revenue([{"dim": "product_group", "value": "Zzz Qqq"}]))


def test_group_matching_two_products_abstains():
    lines = [
        ("O1", "2026-03-02", "completed", "c1", "t1", "Green Tea Hot", "drink", 1, 1, 70, 1, "A"),
        ("O2", "2026-03-03", "completed", "c2", "t2", "Green Tea Iced", "drink", 1, 1, 60, 1, "A"),
    ]
    with pytest.raises(Unsupported):
        run(make(lines=lines), revenue([{"dim": "product_group", "value": "Green Tea"}]))
    # Different spellings of one name are still one product.
    lines = [
        ("O1", "2026-03-02", "completed", "c1", "t1", "Green Tea 100ml", "drink", 1, 1, 70, 1, "A"),
        ("O2", "2026-03-03", "completed", "c2", "t1", "Green Tea 100 ml", "drink", 1, 1, 60, 1, "A"),
    ]
    assert run(make(lines=lines), revenue([{"dim": "product_group", "value": "Green Tea 100ml"}])).value == 130


def test_excluded_group_naming_one_product_excludes_that_product():
    got = run(make(), revenue([{"dim": "product_group", "value": "Green Tea", "exclude": True}]))
    assert got.value == 70


def test_no_group_recorded_is_still_not_recorded():
    db = make(with_category=False)
    with pytest.raises(NotRecorded):
        run(db, revenue([{"dim": "product_group", "value": "Green Tea"}]))


# --- column filters on standard measures --------------------------------------------------------------------
