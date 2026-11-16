"""Synthetic tests for bizreason.semantic.stock_sources (ledger balances on past days, expiry ranges, quantities)."""
import sqlite3
from datetime import date

import pytest

from bizreason.semantic import stock_sources as ss


def _ledger(rows):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE stock_ledger (name_key TEXT, product_name TEXT, product_id TEXT, ts TEXT, balance REAL)")
    db.executemany("INSERT INTO stock_ledger VALUES (?,?,?,?,?)", rows)
    return db


ROWS = [
    ("a", "Alpha", "P1", "2025-01-01 06:00:00", 10),
    ("a", "Alpha", "P1", "2025-01-05 09:00:00", 7),
    ("a", "Alpha", "P1", "2025-01-05 18:00:00", 4),
    ("b", "Beta", "P2", "2025-01-03 06:00:00", 5),
]


def test_balance_is_after_last_movement_of_the_day():
    db = _ledger(ROWS)
    assert ss.ledger_balance_on(db, ["Alpha"], date(2025, 1, 5)) == 4
    assert ss.ledger_balance_on(db, ["Alpha"], date(2025, 1, 4)) == 10
    assert ss.ledger_balance_on(db, ["Alpha"], date(2025, 3, 1)) == 4


def test_same_timestamp_ties_follow_ledger_order():
    db = _ledger([("a", "Alpha", "P1", "2025-01-01 06:00:00", 10), ("a", "Alpha", "P1", "2025-01-01 06:00:00", 6)])
    assert ss.ledger_balance_on(db, ["Alpha"], date(2025, 1, 1)) == 6


def test_no_movement_yet_is_not_zero():
    db = _ledger(ROWS)
    assert ss.ledger_balance_on(db, ["Beta"], date(2025, 1, 2)) is None


def test_ambiguous_or_unknown_product_is_none():
    db = _ledger(ROWS)
    assert ss.ledger_balance_on(db, ["Alpha", "Beta"], date(2025, 2, 1)) is None
    assert ss.ledger_balance_on(db, ["Gamma"], date(2025, 2, 1)) is None
    assert ss.ledger_balance_on(db, [], date(2025, 2, 1)) is None


def test_no_ledger_table_or_empty_ledger_is_none():
    assert ss.ledger_balance_on(sqlite3.connect(":memory:"), ["Alpha"], date(2025, 1, 5)) is None
    assert ss.ledger_balance_on(_ledger([]), ["Alpha"], date(2025, 1, 5)) is None


AS_OF = date(2026, 6, 28)


def test_expiry_range_without_window_starts_at_the_question_day():
    assert ss.lot_expiry_range(date.min, date.max, AS_OF, "argmax_date") == (AS_OF, None)
    assert ss.lot_expiry_range(date.min, date.max, AS_OF, "scalar") == (AS_OF, None)


def test_earliest_expiry_with_the_stock_day_as_window_is_open_ended():
    day, nxt = AS_OF, date(2026, 6, 29)
    assert ss.lot_expiry_range(day, nxt, AS_OF, "argmax_date") == (AS_OF, None)


def test_stated_expiry_range_is_kept():
    start, end = date(2026, 6, 28), date(2026, 9, 27)
    assert ss.lot_expiry_range(start, end, AS_OF, "scalar") == (start, date(2026, 9, 26))
    assert ss.lot_expiry_range(start, end, AS_OF, "argmax_date") == (start, date(2026, 9, 26))


def test_one_day_window_is_still_a_range_for_counts_and_for_other_days():
    # Counting lots that expire on the question's own day, or any earliest-expiry window on another day: unchanged.
    assert ss.lot_expiry_range(AS_OF, date(2026, 6, 29), AS_OF, "scalar") == (AS_OF, AS_OF)
    other = date(2026, 9, 1)
    assert ss.lot_expiry_range(other, date(2026, 9, 2), AS_OF, "argmax_date") == (other, other)


@pytest.mark.parametrize("qty", [-1, 2.5])
def test_quantity_answer_rejects_non_counts(qty):
    assert ss.quantity_answer(qty, 10, False) is None


def test_quantity_answer_number_and_split():
    assert ss.quantity_answer(25, None, False) == ("number", 25)
    assert ss.quantity_answer(25, 10, True) == ("list", [2, 5])
    assert ss.quantity_answer(25, None, True) is None
    assert ss.quantity_answer(25, 1, True) is None


def _lots(stock_rows, lot_rows):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE stock (name_key TEXT, qty_base REAL)")
    db.execute("CREATE TABLE lot_balance (name_key TEXT, lot_id TEXT, balance REAL)")
    db.execute("CREATE TABLE stock_sources (kind TEXT, rows INTEGER)")
    db.execute("INSERT INTO stock_sources VALUES ('lot_balance', 3)")
    db.executemany("INSERT INTO stock VALUES (?,?)", stock_rows)
    db.executemany("INSERT INTO lot_balance VALUES (?,?,?)", lot_rows)
    return db


def test_lots_that_reproduce_the_report_are_kept():
    db = _lots([("a", 10), ("b", 0), ("c", 4)], [("a", "L1", 6), ("a", "L2", 4), ("b", "L3", 0)])
    assert ss.lot_report_mismatches(db) == (2, 0)  # product c has no lot: not compared
    assert ss.drop_lots_if_disagreeing(db) == 0
    assert db.execute("SELECT COUNT(*) FROM lot_balance").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM stock_sources WHERE kind = 'lot_balance'").fetchone()[0] == 1


def test_lots_that_disagree_with_the_report_are_dropped():
    db = _lots([("a", 10), ("b", 2)], [("a", "L1", 6), ("a", "L2", 3), ("b", "L3", 2)])
    assert ss.lot_report_mismatches(db) == (2, 1)
    assert ss.drop_lots_if_disagreeing(db) == 1
    assert db.execute("SELECT COUNT(*) FROM lot_balance").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM stock_sources WHERE kind = 'lot_balance'").fetchone()[0] == 0


def test_mismatch_check_without_tables_is_neutral():
    assert ss.lot_report_mismatches(sqlite3.connect(":memory:")) == (0, 0)


def test_dominant_quantity_is_the_sold_one():
    numeric = {"a": [5.0, 2.0, 7.0], "b": [1.0, 0.0, 0.0]}
    assert ss.dominant_quantity(numeric, ["a", "b"]) == "a"
    assert ss.dominant_quantity({"a": numeric["b"], "b": numeric["a"]}, ["a", "b"]) == "b"


def test_dominant_quantity_ignores_blanks_but_not_conflicts():
    assert ss.dominant_quantity({"a": [5.0, None, 7.0], "b": [1.0, 4.0, None]}, ["a", "b"]) == "a"
    assert ss.dominant_quantity({"a": [5.0, 1.0], "b": [1.0, 4.0]}, ["a", "b"]) is None  # neither always larger


def test_dominant_quantity_needs_a_strict_winner():
    assert ss.dominant_quantity({"a": [3.0, 2.0], "b": [3.0, 2.0]}, ["a", "b"]) is None  # identical columns
    assert ss.dominant_quantity({"a": [1.0, 2.0], "b": [0.0, 0.0]}, ["b"]) is None  # candidate is not the larger
    assert ss.dominant_quantity({"a": [1.0, 2.0]}, ["a"]) is None  # nothing to compare


def _basis_db(stock=0, ledger=0, purchases=0, kinds=()):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE stock_sources (kind TEXT, rows INTEGER)")
    db.execute("CREATE TABLE stock (name_key TEXT)")
    db.execute("CREATE TABLE stock_ledger (name_key TEXT)")
    db.execute("CREATE TABLE purchases (product_name TEXT)")
    db.executemany("INSERT INTO stock_sources VALUES (?, 0)", [(k,) for k in kinds])
    db.executemany("INSERT INTO stock VALUES ('x')", [()] * stock)
    db.executemany("INSERT INTO stock_ledger VALUES ('x')", [()] * ledger)
    db.executemany("INSERT INTO purchases VALUES ('x')", [()] * purchases)
    return db


def test_shop_with_nothing_to_rest_a_stock_on_records_none():
    assert ss.has_no_stock_basis(_basis_db(kinds=("unannotated",)))


@pytest.mark.parametrize("kwargs", [
    {"stock": 1}, {"ledger": 1}, {"purchases": 1}, {"kinds": ("inventory",)}, {"kinds": ("lot",)},
    {"kinds": ("lot_balance",)}])
def test_any_stock_basis_means_stock_is_not_unrecorded(kwargs):
    assert not ss.has_no_stock_basis(_basis_db(**kwargs))


def test_layer_without_the_tables_is_not_judged():
    assert not ss.has_no_stock_basis(sqlite3.connect(":memory:"))


class _Intent:
    def __init__(self, **kw):
        self.shape, self.measure, self.filters = "list_filter", "inventory_qty", []
        self.measure2 = self.group_by = self.threshold = None
        self.__dict__.update(kw)


def test_expiry_range_list_recognised():
    assert ss.is_expiry_range_list(_Intent(), date(2026, 5, 25), date(2026, 9, 23))


@pytest.mark.parametrize("change", [
    {"shape": "scalar"}, {"measure": "inventory_value"}, {"filters": [{"dim": "product", "value": "x"}]},
    {"measure2": "inventory_qty"}, {"group_by": "unit"}, {"threshold": 5}])
def test_expiry_range_list_needs_a_plain_stock_list(change):
    assert not ss.is_expiry_range_list(_Intent(**change), date(2026, 5, 25), date(2026, 9, 23))


def test_expiry_range_list_needs_a_real_range():
    assert not ss.is_expiry_range_list(_Intent(), date.min, date(2026, 9, 23))
    assert not ss.is_expiry_range_list(_Intent(), date(2026, 5, 25), date(2026, 5, 26))


def test_unread_table_keeps_the_shop_undecided():
    db = _basis_db(kinds=("unannotated",))
    db.execute("UPDATE stock_sources SET rows = 2")
    assert not ss.has_no_stock_basis(db)
