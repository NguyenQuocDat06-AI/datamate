"""Layer: order-level discount and delivery fee roll up onto orders, the order total is derived only where the rulebook
defines it, a side ledger of amounts paid becomes payments only when nothing about it is ambiguous, and every source
column stays reachable through x_/src_ columns and column_catalog. Synthetic tables only."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from bizreason.contracts import BusinessContext, ColumnMeta, SourceRef, TableMeta
from bizreason.semantic import layer

Specs = dict[str, tuple[list[str], list[tuple[Any, ...]]]]


def _source(tmp_path: Path, specs: Specs) -> Path:
    path = tmp_path / "source.sqlite"
    con = sqlite3.connect(path)
    for name, (columns, rows) in specs.items():
        defs = ", ".join(f'"{c}" TEXT' for c in columns)
        con.execute(f'CREATE TABLE "{name}" ({defs}, __source_row INTEGER, __raw_json TEXT)')
        marks = ",".join("?" * (len(columns) + 2))
        for number, row in enumerate(rows, start=1):
            con.execute(f'INSERT INTO "{name}" VALUES ({marks})', (*row, number, "{}"))
    con.commit()
    con.close()
    return path


def _context(path: Path, specs: Specs) -> BusinessContext:
    tables = [TableMeta(name=name, sources=[SourceRef(path=f"{name}.csv")],
                        columns=[ColumnMeta(name=c, original_name=c, dtype="text") for c in columns],
                        row_count=len(rows), raw_row_count=len(rows)) for name, (columns, rows) in specs.items()]
    return BusinessContext(business_id="test", revision="r1", database=path, readme="", manifest={},
                           tables=tables, diagnostics=[])


def _record(columns: list[str], kind: str, roles: dict[str, str]) -> dict[str, Any]:
    return {"columns": columns, "kind": kind, "roles": roles}


def _build(tmp_path: Path, specs: Specs, records: list[dict[str, Any]],
           line_kinds: dict[str, str] | None = None) -> tuple[dict, Path]:
    out = tmp_path / "canonical.sqlite"
    report = layer.build(_context(_source(tmp_path, specs), specs), records, out, line_kinds=line_kinds)
    return report, out


def _query(out: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(out)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _columns(out: Path, table: str) -> set[str]:
    return {row[0] for row in _query(out, f"SELECT name FROM pragma_table_info('{table}')")}


LINES = ["order", "day", "item", "qty", "price", "amount"]
LINE_ROLES = {"order": "order_id", "day": "ordered_at", "item": "product_name", "qty": "qty",
              "price": "unit_price", "amount": "line_amount"}
# an order of two goods lines (100 + 50), one with a voucher row (-30) and a delivery-fee row (15)
KIND_ROWS = [
    ("A1", "2026-03-01", "tea", "1", "100", "100"),
    ("A1", "2026-03-01", "coffee", "1", "50", "50"),
    ("A1", "2026-03-01", "voucher", "1", "-30", "-30"),
    ("A1", "2026-03-01", "ship", "1", "15", "15"),
    ("A2", "2026-03-02", "tea", "2", "100", "200"),
]
KIND_MAP = {"voucher": "discount", "ship": "shipping_fee"}


# --- L1: off-goods lines roll up --------------------------------------------------------------------------------

def test_discount_and_delivery_lines_roll_up_and_total_is_derived(tmp_path: Path) -> None:
    report, out = _build(tmp_path, {"sales": (LINES, KIND_ROWS)}, [_record(LINES, "sales", LINE_ROLES)], KIND_MAP)

    assert _query(out, "SELECT order_id, order_discount, shipping_fee, total_due, revenue FROM orders "
                       "ORDER BY order_id") == [("A1", 30.0, 15.0, 135.0, 150.0), ("A2", None, None, 200.0, 200.0)]
    assert _query(out, "SELECT COUNT(*) FROM orders") == [(2,)]  # no order is multiplied
    assert _query(out, "SELECT SUM(amount) FROM lines") == [(350.0,)]  # canonical values are unchanged
    assert report["order_total_derived"] == 2


def test_derived_columns_are_flagged_in_the_catalog(tmp_path: Path) -> None:
    _, out = _build(tmp_path, {"sales": (LINES, KIND_ROWS)}, [_record(LINES, "sales", LINE_ROLES)], KIND_MAP)

    rows = _query(out, "SELECT column_name, derived FROM column_catalog WHERE table_name='orders' "
                       "AND column_name IN ('order_discount','shipping_fee','total_due') ORDER BY column_name")
    assert rows == [("order_discount", 1), ("shipping_fee", 1), ("total_due", 1)]
    assert _query(out, "SELECT COUNT(*) FROM column_catalog WHERE derived=1 AND source_table IS NOT NULL") == [(0,)]


def test_no_recorded_adjustment_leaves_total_empty(tmp_path: Path) -> None:
    rows = [row for row in KIND_ROWS if row[2] in ("tea", "coffee")]
    report, out = _build(tmp_path, {"sales": (LINES, rows)}, [_record(LINES, "sales", LINE_ROLES)])

    assert "order_discount" not in _columns(out, "orders") and "shipping_fee" not in _columns(out, "orders")
    assert _query(out, "SELECT COUNT(total_due) FROM orders") == [(0,)]
    assert report["order_total_derived"] == 0


def test_discount_given_by_a_column_and_by_lines_is_ambiguous_and_nothing_is_derived(tmp_path: Path) -> None:
    columns = [*LINES, "cut"]
    roles = {**LINE_ROLES, "cut": "order_discount"}
    rows = [(*row, "30" if row[0] == "A1" else "") for row in KIND_ROWS]
    report, out = _build(tmp_path, {"sales": (columns, rows)}, [_record(columns, "sales", roles)], KIND_MAP)

    assert _query(out, "SELECT order_discount FROM orders ORDER BY order_id") == [(None,), (None,)]
    assert _query(out, "SELECT COUNT(total_due) FROM orders") == [(0,)]
    assert report["order_discount_conflicts"] == 1 and "order_total_skipped" in report


def test_an_order_without_goods_total_stops_the_derivation(tmp_path: Path) -> None:
    rows = [*KIND_ROWS, ("A3", "2026-03-03", "voucher", "1", "-5", "-5")]  # an order of only a voucher
    report, out = _build(tmp_path, {"sales": (LINES, rows)}, [_record(LINES, "sales", LINE_ROLES)], KIND_MAP)

    assert _query(out, "SELECT COUNT(total_due) FROM orders") == [(0,)]
    assert report["order_total_skipped"] == "an order has no goods total"
    assert _query(out, "SELECT order_discount FROM orders WHERE order_id='A3'") == [(5.0,)]


# --- L2: per-order columns --------------------------------------------------------------------------------------

HEAD = ["order", "day", "item", "qty", "price", "amount", "cut", "fee", "goods", "paid", "owed"]
HEAD_ROLES = {"order": "order_id", "day": "ordered_at", "item": "product_name", "qty": "qty", "price": "unit_price",
              "amount": "line_amount", "cut": "order_discount", "fee": "shipping_fee", "goods": "order_goods_total",
              "paid": "order_paid", "owed": "order_outstanding"}


def _head_rows() -> list[tuple[Any, ...]]:
    # order B1: goods 150, order discount 20 repeated on both lines, delivery fee 10 -> total 140
    return [
        ("B1", "2026-03-01", "tea", "1", "100", "100", "20", "10", "150", "100", "40"),
        ("B1", "2026-03-01", "coffee", "1", "50", "50", "20", "10", "150", "100", "40"),
        ("B2", "2026-03-02", "tea", "2", "100", "200", "", "", "200", "200", "0"),
    ]


def test_order_column_repeated_on_every_line_counts_once(tmp_path: Path) -> None:
    report, out = _build(tmp_path, {"sales": (HEAD, _head_rows())}, [_record(HEAD, "sales", HEAD_ROLES)])

    assert _query(out, "SELECT order_id, order_discount, shipping_fee, total_due FROM orders ORDER BY order_id") == [
        ("B1", 20.0, 10.0, 140.0), ("B2", None, None, 200.0)]
    assert report["order_total_derived"] == 2


def test_total_given_by_the_sheet_is_never_overwritten(tmp_path: Path) -> None:
    columns = [*HEAD, "due"]
    roles = {**HEAD_ROLES, "due": "order_total_due"}
    rows = [(*row, "999" if row[0] == "B1" else "") for row in _head_rows()]
    report, out = _build(tmp_path, {"sales": (columns, rows)}, [_record(columns, "sales", roles)])

    assert _query(out, "SELECT order_id, total_due FROM orders ORDER BY order_id") == [("B1", 999.0), ("B2", None)]
    assert report["order_total_derived"] == 0
    assert _query(out, "SELECT COUNT(*) FROM column_catalog WHERE column_name='total_due' AND derived=1") == [(0,)]


def test_total_that_disagrees_with_paid_plus_outstanding_is_not_derived(tmp_path: Path) -> None:
    rows = [(*row[:9], "300", "0") for row in _head_rows()]  # the sheet records a different total (tax, say)
    report, out = _build(tmp_path, {"sales": (HEAD, rows)}, [_record(HEAD, "sales", HEAD_ROLES)])

    assert _query(out, "SELECT COUNT(total_due) FROM orders") == [(0,)]
    assert "paid and outstanding" in report["order_total_skipped"]
    assert _query(out, "SELECT order_discount FROM orders WHERE order_id='B1'") == [(20.0,)]  # the roll-up still holds


def test_discount_recorded_as_negative_numbers_reads_positive(tmp_path: Path) -> None:
    rows = [(*row[:6], "-20" if row[0] == "B1" else "", *row[7:]) for row in _head_rows()]
    _, out = _build(tmp_path, {"sales": (HEAD, rows)}, [_record(HEAD, "sales", HEAD_ROLES)])

    assert _query(out, "SELECT order_discount, total_due FROM orders WHERE order_id='B1'") == [(20.0, 140.0)]


def _repeated_discount_rows(count: int, amounts_net: bool) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for number in range(count):
        discount = 10 + number % 3
        for item, price in (("tea", 100), ("coffee", 50)):
            net = price - discount if amounts_net else price
            rows.append((f"C{number}", "2026-03-01", item, "1", str(price), str(net), str(discount) if not amounts_net
                         else str(discount + (7 if item == "coffee" else 0))))
    return rows


def test_line_column_repeated_per_order_with_undiscounted_amounts_is_an_order_discount(tmp_path: Path) -> None:
    columns = [*LINES, "cut"]
    roles = {**LINE_ROLES, "cut": "line_discount"}
    report, out = _build(tmp_path, {"sales": (columns, _repeated_discount_rows(12, False))},
                         [_record(columns, "sales", roles)])

    assert report["order_discount_from_line_column"] is True
    assert _query(out, "SELECT order_discount, total_due FROM orders WHERE order_id='C0'") == [(10.0, 140.0)]
    assert _query(out, "SELECT COUNT(*) FROM orders WHERE order_discount IS NULL") == [(0,)]
    assert _query(out, "SELECT SUM(line_discount) FROM raw_lines") == [(sum(2 * (10 + n % 3) for n in range(12)),)]


def test_a_genuine_line_discount_is_not_read_as_an_order_discount(tmp_path: Path) -> None:
    columns = [*LINES, "cut"]
    roles = {**LINE_ROLES, "cut": "line_discount"}
    report, out = _build(tmp_path, {"sales": (columns, _repeated_discount_rows(12, True))},
                         [_record(columns, "sales", roles)])

    assert report["order_discount_from_line_column"] is False
    assert "order_discount" not in _columns(out, "orders")
    assert _query(out, "SELECT COUNT(total_due) FROM orders") == [(0,)]


# --- L3: side ledger -> payments ---------------------------------------------------------------------------------

SALES = ["order", "day", "item", "qty", "price", "amount", "method"]
SALES_ROLES = {"order": "order_id", "day": "ordered_at", "item": "product_name", "qty": "qty", "price": "unit_price",
               "amount": "line_amount", "method": "payment_method"}


def _sales_rows() -> list[tuple[Any, ...]]:
    return [("S1", "2026-03-01", "tea", "1", "1000", "1000", "debt"), ("S2", "2026-03-02", "tea", "1", "500", "500", "debt"),
            ("S3", "2026-03-03", "tea", "3", "100", "300", "debt")]


LEDGER = ["ref", "when", "paid_now", "memo"]


def _ledger_roles(**override: str) -> dict[str, str]:
    return {"ref": "order_id", "when": "paid_at", "paid_now": "paid_amount", "memo": "other", **override}


def _ledger_build(tmp_path: Path, ledger: list[tuple[Any, ...]], roles: dict[str, str],
                  columns: list[str] | None = None) -> tuple[dict, Path]:
    ledger_columns = columns or LEDGER
    specs: Specs = {"sales": (SALES, _sales_rows()), "debts": (ledger_columns, ledger)}
    return _build(tmp_path, specs, [_record(SALES, "sales", SALES_ROLES), _record(ledger_columns, "debt_ledger", roles)])


FULL_LEDGER = [("S1", "2026-03-05", "400", "a"), ("S1", "2026-03-09", "600", "b"), ("S2", "2026-03-04", "200", "c"),
               ("S3", "2026-03-06", "100", "d")]


def test_ledger_with_order_keys_and_payment_dates_becomes_payments(tmp_path: Path) -> None:
    report, out = _ledger_build(tmp_path, FULL_LEDGER, _ledger_roles())

    assert _query(out, "SELECT order_id, date, amount FROM payments ORDER BY date, order_id") == [
        ("S2", "2026-03-04", 200.0), ("S1", "2026-03-05", 400.0), ("S3", "2026-03-06", 100.0),
        ("S1", "2026-03-09", 600.0)]
    assert _query(out, "SELECT SUM(amount) FROM payments") == [(1300.0,)]  # the sheet's total
    assert report["ledger_payments"] == 4 and report["ledger_payments_table"] == "debts"
    assert _query(out, "SELECT COUNT(*) FROM orders") == [(3,)]


def test_ledger_date_in_the_ledgers_own_date_column_counts_when_it_follows_the_order(tmp_path: Path) -> None:
    _, out = _ledger_build(tmp_path, FULL_LEDGER, _ledger_roles(when="ordered_at"))

    assert _query(out, "SELECT COUNT(*) FROM payments") == [(4,)]


def test_ledger_date_equal_to_the_order_date_is_the_sale_not_a_payment(tmp_path: Path) -> None:
    ledger = [("S1", "2026-03-01", "400", "a"), ("S2", "2026-03-02", "200", "b"), ("S3", "2026-03-03", "100", "c")]
    report, out = _ledger_build(tmp_path, ledger, _ledger_roles(when="ordered_at"))

    assert _query(out, "SELECT COUNT(*) FROM payments") == [(0,)]
    assert "no payment date" in report["ledger_payments_skipped"][0]


def test_ledger_key_in_a_note_naming_one_number_resolves_to_one_order(tmp_path: Path) -> None:
    ledger = [("pay for order 1", "2026-03-05", "400", ""), ("pay for order 2", "2026-03-04", "200", ""),
              ("pay for order 3", "2026-03-06", "100", "")]
    # order ids end in 1, 2, 3 so each number names one order (S1..S3)
    _, out = _ledger_build(tmp_path, ledger, _ledger_roles(ref="note"))

    assert _query(out, "SELECT order_id, amount FROM payments ORDER BY order_id") == [
        ("S1", 400.0), ("S2", 200.0), ("S3", 100.0)]


def test_ambiguous_note_key_is_not_loaded(tmp_path: Path) -> None:
    specs: Specs = {
        "sales": (SALES, [*_sales_rows(), ("T1", "2026-03-04", "tea", "1", "70", "70", "debt")]),  # S1 and T1 both end in 1
        "debts": (LEDGER, [("pay for order 1", "2026-03-05", "400", ""), ("pay for order 2", "2026-03-04", "200", ""),
                           ("pay for order 3", "2026-03-06", "100", "")]),
    }
    report, out = _build(tmp_path, specs, [_record(SALES, "sales", SALES_ROLES),
                                           _record(LEDGER, "debt_ledger", _ledger_roles(ref="note"))])

    assert _query(out, "SELECT COUNT(*) FROM payments") == [(0,)]
    assert "resolves to the orders" in report["ledger_payments_skipped"][0]


def test_unresolvable_keys_keep_payments_empty(tmp_path: Path) -> None:
    ledger = [("X1", "2026-03-05", "400", ""), ("X2", "2026-03-04", "200", ""), ("S3", "2026-03-06", "100", "")]
    report, out = _ledger_build(tmp_path, ledger, _ledger_roles())

    assert _query(out, "SELECT COUNT(*) FROM payments") == [(0,)]
    assert report["ledger_payments"] == 0


def test_column_equal_to_the_order_price_is_not_a_payment(tmp_path: Path) -> None:
    ledger = [("S1", "2026-03-05", "1000", ""), ("S2", "2026-03-04", "500", ""), ("S3", "2026-03-06", "300", "")]
    report, out = _ledger_build(tmp_path, ledger, _ledger_roles())

    assert _query(out, "SELECT COUNT(*) FROM payments") == [(0,)]
    assert "order price" in report["ledger_payments_skipped"][0]


def test_two_amount_columns_are_ambiguous(tmp_path: Path) -> None:
    columns = [*LEDGER, "upfront"]
    ledger = [(*row, "50") for row in FULL_LEDGER]
    _, out = _ledger_build(tmp_path, ledger, _ledger_roles(upfront="paid_amount"), columns)

    assert _query(out, "SELECT COUNT(*) FROM payments") == [(0,)]


def test_ledger_that_misses_an_order_the_debt_rules_count_is_not_the_whole_record(tmp_path: Path) -> None:
    report, out = _ledger_build(tmp_path, FULL_LEDGER[:3], _ledger_roles())  # S3 has no ledger row

    assert _query(out, "SELECT COUNT(*) FROM payments") == [(0,)]
    assert "every order" in report["ledger_payments_skipped"][0]


def test_an_existing_payment_table_is_the_only_source(tmp_path: Path) -> None:
    columns = ["order", "when", "money"]
    specs: Specs = {"sales": (SALES, _sales_rows()), "receipts": (columns, [("S1", "2026-03-05", "1000")]),
                    "debts": (LEDGER, FULL_LEDGER)}
    _, out = _build(tmp_path, specs, [
        _record(SALES, "sales", SALES_ROLES),
        _record(columns, "payment", {"order": "order_id", "when": "paid_at", "money": "paid_amount"}),
        _record(LEDGER, "debt_ledger", _ledger_roles())])

    assert _query(out, "SELECT order_id, amount FROM payments") == [("S1", 1000.0)]


def test_orders_paid_amount_that_differs_from_the_ledger_is_ambiguous(tmp_path: Path) -> None:
    columns = [*SALES, "paid"]
    rows = [(*row, "100") for row in _sales_rows()]  # the orders say 100 paid on each; the ledger sums to 1000 on S1
    roles = {**SALES_ROLES, "paid": "order_paid"}
    specs: Specs = {"sales": (columns, rows), "debts": (LEDGER, FULL_LEDGER)}
    report, out = _build(tmp_path, specs, [_record(columns, "sales", roles),
                                           _record(LEDGER, "debt_ledger", _ledger_roles())])

    assert _query(out, "SELECT COUNT(*) FROM payments") == [(0,)]
    assert "differs" in report["ledger_payments_skipped"][0]


# --- L5: every source column stays reachable ----------------------------------------------------------------------

def test_a_loaded_ledger_still_keeps_every_column_and_its_catalog_rows(tmp_path: Path) -> None:
    columns = [*LEDGER, "deducted_from"]
    ledger = [(*row, "S1") for row in FULL_LEDGER]
    _, out = _ledger_build(tmp_path, ledger, _ledger_roles(deducted_from="other"), columns)

    assert {"ref", "when", "paid_now", "memo", "deducted_from"} <= _columns(out, "src_debts")
    cataloged = {row[0] for row in _query(out, "SELECT column_name FROM column_catalog WHERE table_name='src_debts'")}
    assert {"ref", "when", "paid_now", "memo", "deducted_from"} <= cataloged
    assert _query(out, "SELECT join_key_to FROM column_catalog WHERE table_name='src_debts' "
                       "AND column_name='deducted_from'") == [("orders.order_id",)]
