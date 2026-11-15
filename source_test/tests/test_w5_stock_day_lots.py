"""Compiler: stock at the end of any day from the stock ledger, and the cost of lots about to expire (synthetic data)."""
import sqlite3
from datetime import date

import pytest

from bizreason.contracts import TimeWindow
from bizreason.semantic.compiler import Compiler, Intent, Unsupported


def _db(ledger=None, stock=None, lots=None, lot_cost=True, stock_date="2025-03-10"):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT)")
    db.execute("CREATE TABLE lines (order_id TEXT, date TEXT, qty REAL, amount REAL)")
    db.execute("CREATE TABLE stock_sources (kind TEXT, rows INTEGER)")
    db.execute("CREATE TABLE stock (name_key TEXT, product_name TEXT, unit TEXT, qty_base REAL, unit_cost REAL, "
               "large_factor REAL, received INTEGER, stock_date TEXT)")
    db.execute("CREATE TABLE stock_ledger (name_key TEXT, product_name TEXT, product_id TEXT, ts TEXT, balance REAL)")
    db.execute("CREATE TABLE lot_balance (name_key TEXT, product_name TEXT, product_id TEXT, lot_id TEXT, "
               "expires_at TEXT, balance REAL" + (", unit_cost REAL)" if lot_cost else ")"))
    db.execute("INSERT INTO stock_sources VALUES ('inventory', 1), ('lot', 1), ('lot_balance', 5)")
    for name, qty, cost, received in stock or []:
        db.execute("INSERT INTO stock VALUES (?,?,?,?,?,?,?,?)", (name, name, "u", qty, cost, None, received, stock_date))
    for name, ts, balance in ledger or []:
        db.execute("INSERT INTO stock_ledger VALUES (?,?,?,?,?)", (name, name, None, ts, balance))
    for row in lots or []:
        values = (row[0], row[0], None, row[1], row[2], row[3]) + ((row[4],) if lot_cost else ())
        db.execute(f"INSERT INTO lot_balance VALUES ({','.join('?' * len(values))})", values)
    return db


def _window(day):
    d = date.fromisoformat(day)
    return TimeWindow(start=d, end_exclusive=date.fromordinal(d.toordinal() + 1))


# a: 5 -> 0 -> 4 (two movements on 03-04), b: 2 then 0 on 03-06, c: first movement on 03-08, d: never sold, never 0.
STOCK = [("a", 4, 10.0, 1), ("b", 0, 20.0, 1), ("c", 3, 5.0, 1), ("d", 9, 1.0, 0)]
LEDGER = [("a", "2025-03-01 09:00:00", 5), ("a", "2025-03-04 08:00:00", 0), ("a", "2025-03-04 17:00:00", 4),
          ("b", "2025-03-02", 2), ("b", "2025-03-06 10:00:00", 0),
          ("c", "2025-03-08 10:00:00", 3), ("d", "2025-03-01", 9)]


def _stock(measure, day, **kw):
    return Compiler(_db(LEDGER, STOCK, **kw)).stock(Intent(measure=measure), _window(day))


def test_out_of_stock_uses_the_last_movement_of_the_day():
    assert _stock("out_of_stock_count", "2025-03-04").value == 0  # a ends the day at 4
    assert _stock("out_of_stock_count", "2025-03-06").value == 1  # b reached 0 that day
    assert _stock("out_of_stock_count", "2025-03-05").value == 0  # b still 2
    assert _stock("out_of_stock_count", "2025-03-10").value == 1


def test_out_of_stock_mid_day_zero_does_not_count():
    # a was 0 at 08:00 on 03-04 but 4 by the end of the day; only the end of day matters.
    assert _stock("out_of_stock_count", "2025-03-04").value == 0


def test_product_without_movement_yet_is_not_counted_or_valued():
    # c has no row before 03-08, so it holds nothing on 03-07.
    assert _stock("inventory_value", "2025-03-07").value == 0 * 10.0 + 2 * 20.0 + 9 * 1.0
    assert _stock("inventory_value", "2025-03-08").value == 0 * 10.0 + 2 * 20.0 + 3 * 5.0 + 9 * 1.0


def test_inventory_value_counts_only_positive_balances():
    assert _stock("inventory_value", "2025-03-10").value == 4 * 10.0 + 3 * 5.0 + 9 * 1.0


def test_never_received_product_is_not_out_of_stock():
    ledger = [*LEDGER, ("e", "2025-03-01", 0)]
    stock = [*STOCK, ("e", 0, 1.0, 0)]
    assert Compiler(_db(ledger, stock)).stock(Intent(measure="out_of_stock_count"), _window("2025-03-10")).value == 1


def test_missing_cost_of_a_product_in_stock_is_unsupported():
    db = _db(LEDGER, [("a", 4, None, 1), *STOCK[1:]])
    with pytest.raises(Unsupported):
        Compiler(db).stock(Intent(measure="inventory_value"), _window("2025-03-10"))


def test_without_ledger_other_day_still_unsupported_counterexample():
    db = _db(None, STOCK)
    with pytest.raises(Unsupported, match="other than the report date"):
        Compiler(db).stock(Intent(measure="out_of_stock_count"), _window("2025-03-04"))
    assert Compiler(db).stock(Intent(measure="out_of_stock_count"), _window("2025-03-10")).value == 1


def test_product_the_ledger_never_mentions_keeps_the_report_path():
    db = _db([row for row in LEDGER if row[0] != "d"], STOCK)
    with pytest.raises(Unsupported, match="other than the report date"):
        Compiler(db).stock(Intent(measure="out_of_stock_count"), _window("2025-03-04"))


def test_filter_on_a_ledger_day_is_unsupported():
    with pytest.raises(Unsupported):
        Compiler(_db(LEDGER, STOCK)).stock(
            Intent(measure="out_of_stock_count", filters=[{"dim": "product", "value": "a"}]), _window("2025-03-04"))


# lots: (product, lot, expires, balance, unit_cost)
LOTS = [("a", "L1", "2025-04-01", 10, 2.5), ("a", "L2", "2025-06-30", 4, 3.0), ("b", "L3", "2025-04-10", 0, 9.0),
        ("c", "L4", "2025-03-09", 7, 1.0), ("d", "L5", "2025-04-20", 2, 10.0)]


def _lot_value(as_of, start, last, lots=LOTS, **kw):
    db = _db(LEDGER, STOCK, lots, **kw)
    window = TimeWindow(start=date.fromisoformat(start), end_exclusive=date.fromordinal(date.fromisoformat(last).toordinal() + 1))
    return Compiler(db).lots(Intent(measure="expiring_lot_value"), window, date.fromisoformat(as_of))


def test_expiring_lot_value_prices_each_lot_by_its_own_cost():
    # L1 (04-01) and L5 (04-20) fall in the range; L2 is later, L3 is empty, L4 expired before today.
    assert _lot_value("2025-03-10", "2025-03-10", "2025-04-30").value == 10 * 2.5 + 2 * 10.0


def test_expiring_range_is_inclusive_of_the_last_day():
    assert _lot_value("2025-03-10", "2025-03-10", "2025-04-01").value == 25
    assert _lot_value("2025-03-10", "2025-03-10", "2025-03-31").value == 0


def test_expiring_lot_value_null_cost_in_range_is_unsupported():
    lots = [*LOTS[:1], ("e", "L9", "2025-04-02", 3, None)]
    with pytest.raises(Unsupported):
        _lot_value("2025-03-10", "2025-03-10", "2025-04-30", lots)


def test_expiring_lot_value_null_cost_outside_range_is_fine():
    lots = [*LOTS[:1], ("e", "L9", "2025-09-02", 3, None)]
    assert _lot_value("2025-03-10", "2025-03-10", "2025-04-30", lots).value == 25


def test_expiring_lot_value_without_cost_column_is_unsupported():
    with pytest.raises(Unsupported):
        _lot_value("2025-03-10", "2025-03-10", "2025-04-30", lot_cost=False)


def test_expiring_lot_value_needs_balances_not_older_than_today():
    # Balances are as of 03-10; asking as of 03-05 would need the later sales rolled back.
    with pytest.raises(Unsupported):
        _lot_value("2025-03-05", "2025-03-05", "2025-04-30")
    # Today after the last recorded day: nothing moves in between.
    assert _lot_value("2025-03-12", "2025-03-12", "2025-04-30").value == 45


def test_expiry_lot_still_needs_balances_on_exactly_this_day_counterexample():
    db = _db(LEDGER, STOCK, LOTS)
    window = TimeWindow(start=date(2025, 3, 12), end_exclusive=date(2025, 5, 1))
    with pytest.raises(Unsupported):
        Compiler(db).lots(Intent(measure="expiry_lot", shape="scalar"), window, date(2025, 3, 12))
    assert Compiler(db).lots(Intent(measure="expiry_lot", shape="scalar"), window.model_copy(
        update={"start": date(2025, 3, 10)}), date(2025, 3, 10)).value == 2


# _list_cell_match: an item equal to the filter text wins over items that only contain it.
def _cells(values, text):
    from bizreason.semantic.compiler import _list_cell_match
    return sorted(_list_cell_match(values, _norm(text)))


def _norm(text):
    from bizreason.semantic.compiler import normalize_lookup
    return normalize_lookup(text)


DRINKS = ["Latte (M)", "LATTE (M)", "Matcha latte (M)", "Latte (M) + Trân châu", "Matcha latte (M) + Pudding",
          "Mocha (M)", "Cà phê, Trà", "Latte (L), Mocha (M)"]


def test_exact_item_does_not_take_names_that_only_contain_it():
    got = _cells(DRINKS, "Latte (M)")
    assert "Matcha latte (M)" not in got and "Matcha latte (M) + Pudding" not in got
    assert got == sorted(["Latte (M)", "LATTE (M)", "Latte (M) + Trân châu"])


def test_without_an_exact_item_containing_match_is_kept_counterexample():
    got = _cells(DRINKS, "Matcha")
    assert got == sorted(["Matcha latte (M)", "Matcha latte (M) + Pudding"])


def test_item_inside_a_multi_item_cell_still_matches():
    cells = ["Thay nhớt, vá ruột", "Vá ruột", "Thay nhớt", "Vá ruột xe, Thay nhớt", "Rửa xe + Thay nhớt"]
    assert _cells(cells, "vá ruột") == sorted(["Thay nhớt, vá ruột", "Vá ruột"])


def test_no_list_cells_means_no_list_match_counterexample():
    assert _cells(["Latte (M)", "Matcha latte (M)"], "Latte (M)") == []
