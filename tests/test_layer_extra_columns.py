"""layer.build keeps every source column that no canonical field reads: x_ text, xn_ number and xd_ date twins on the
line, order, purchase, payment and delivery tables; a table the layer does not read is copied as src_<table>; and
column_catalog describes every such column. Synthetic tables only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from bizreason.contracts import BusinessContext, ColumnMeta, Diagnostic, SourceRef, TableMeta
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


def _context(path: Path, specs: Specs, originals: dict[str, str] | None = None,
             diagnostics: tuple[Diagnostic, ...] = ()) -> BusinessContext:
    tables = [
        TableMeta(
            name=name,
            sources=[SourceRef(path=f"{name}.csv")],
            columns=[ColumnMeta(name=c, original_name=(originals or {}).get(c, c), dtype="text") for c in columns],
            row_count=len(rows),
            raw_row_count=len(rows),
        )
        for name, (columns, rows) in specs.items()
    ]
    return BusinessContext(business_id="test", revision="r1", database=path, readme="", manifest={},
                           tables=tables, diagnostics=list(diagnostics))


def _record(columns: list[str], kind: str, roles: dict[str, str]) -> dict[str, Any]:
    return {"columns": columns, "kind": kind, "roles": roles}


def _query(out: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(out)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _columns(out: Path, table: str) -> set[str]:
    return {row[0] for row in _query(out, f"SELECT name FROM pragma_table_info('{table}')")}


def _run(tmp_path: Path, specs: Specs, records: list[dict[str, Any]], originals: dict[str, str] | None = None,
         diagnostics: tuple[Diagnostic, ...] = ()) -> tuple[dict, Path]:
    out = tmp_path / "canonical.sqlite"
    context = _context(_source(tmp_path, specs), specs, originals, diagnostics)
    return layer.build(context, records, out), out


SALES_COLUMNS = ["order", "item", "amount", "tradein", "plate"]
SALES_ROLES = {"order": "order_id", "item": "product_name", "amount": "line_amount",
               "tradein": "other", "plate": "other"}


def test_unmapped_columns_reach_lines_and_orders_with_first_non_empty_order_value(tmp_path: Path) -> None:
    specs: Specs = {"sales": (SALES_COLUMNS, [
        ("A1", "tea", "100", "", "29A-12345"),
        ("A1", "coffee", "50", "30000", ""),  # the trade-in sits on the second line of order A1
        ("A2", "tea", "30", "", "43B-99999"),
    ])}
    report, out = _run(tmp_path, specs, [_record(SALES_COLUMNS, "sales", SALES_ROLES)],
                       originals={"tradein": "Trade-in value", "plate": "Plate"})

    assert {"x_tradein", "xn_tradein", "x_plate"} <= _columns(out, "raw_lines")
    assert "xn_plate" not in _columns(out, "raw_lines")  # plates do not parse as numbers
    assert _query(out, "SELECT x_tradein, xn_tradein FROM raw_lines ORDER BY rowid") == [
        (None, None), ("30000", 30000.0), (None, None)]
    assert _query(out, "SELECT x_tradein FROM lines WHERE order_id='A1' ORDER BY rowid") == [(None,), ("30000",)]
    assert _query(out, "SELECT x_tradein, xn_tradein FROM orders WHERE order_id='A1'") == [("30000", 30000.0)]
    assert _query(out, "SELECT x_tradein FROM orders WHERE order_id='A2'") == [(None,)]
    assert _query(out, "SELECT x_plate FROM orders WHERE order_id='A1'") == [("29A-12345",)]
    assert _query(out, "SELECT SUM(amount) FROM lines") == [(180.0,)]  # canonical values are unchanged
    assert report["extra_columns"] == 2  # trade-in and plate; order, item and amount are canonical


def test_numeric_and_date_twins_need_eighty_percent_of_values(tmp_path: Path) -> None:
    columns = ["order", "item", "amount", "due", "noisy", "cash"]
    roles = {"order": "order_id", "item": "product_name", "amount": "line_amount", "due": "other",
             "noisy": "other", "cash": "other"}
    specs: Specs = {"sales": (columns, [
        ("B1", "tea", "100", "2026-03-01", "10", "100"),
        ("B1", "coffee", "50", "01/04/2026", "abc", "200"),
        ("B2", "tea", "30", "2026-05-05", "20", "300"),
        ("B2", "tea", "30", "15/06/2026", "def", "400"),
        ("B3", "tea", "30", "", "5", "n/a"),
    ])}
    _, out = _run(tmp_path, specs, [_record(columns, "sales", roles)])

    names = _columns(out, "raw_lines")
    assert "xd_due" in names and "xn_due" not in names  # four of four dates; none parse as numbers
    assert "xn_noisy" not in names  # three of five numbers is 60 percent: no twin
    assert "xn_cash" in names  # four of five numbers is 80 percent: a twin
    assert _query(out, "SELECT xd_due FROM raw_lines WHERE xd_due IS NOT NULL ORDER BY rowid") == [
        ("2026-03-01",), ("2026-04-01",), ("2026-05-05",), ("2026-06-15",)]
    assert _query(out, "SELECT SUM(xn_cash) FROM raw_lines") == [(1000.0,)]


def test_union_of_extra_columns_across_tables_with_nulls_elsewhere(tmp_path: Path) -> None:
    columns_a = ["order", "item", "amount", "tradein"]
    columns_b = ["order", "item", "amount", "debt"]
    roles_a = {"order": "order_id", "item": "product_name", "amount": "line_amount", "tradein": "other"}
    roles_b = {"order": "order_id", "item": "product_name", "amount": "line_amount", "debt": "other"}
    specs: Specs = {
        "sales_a": (columns_a, [("A1", "tea", "100", "5000")]),
        "sales_b": (columns_b, [("B1", "tea", "50", "7000")]),
    }
    _, out = _run(tmp_path, specs, [_record(columns_a, "sales", roles_a), _record(columns_b, "sales", roles_b)])

    assert {"x_tradein", "x_debt"} <= _columns(out, "orders")
    assert _query(out, "SELECT x_tradein, x_debt FROM orders ORDER BY order_id") == [("5000", None), (None, "7000")]


def test_service_ticket_keeps_unmapped_columns_and_not_the_mapped_ones(tmp_path: Path) -> None:
    columns = ["ticket", "visit", "mechanic", "job", "bill", "plate"]
    roles = {"ticket": "ticket_id", "visit": "ordered_at", "mechanic": "employee_name", "job": "service_item",
             "bill": "ticket_amount", "plate": "other"}
    specs: Specs = {"repairs": (columns, [("T1", "2026-03-01", "An", "oil change", "200", "29A-12345")])}
    _, out = _run(tmp_path, specs, [_record(columns, "service_ticket", roles)])

    assert _query(out, "SELECT product_name, amount, x_plate FROM lines") == [("oil change", 200.0, "29A-12345")]
    assert not {"x_job", "x_bill", "x_mechanic"} & _columns(out, "raw_lines")


def test_purchase_delivery_and_payment_keep_their_unmapped_columns(tmp_path: Path) -> None:
    specs: Specs = {
        "receipts": (["receipt", "day", "item", "memo"], [("R1", "2026-02-01", "rice", "urgent")]),
        "drops": (["order", "day", "product", "trips", "driver_note"],
                  [("A1", "2026-02-03", "rice bag", "1", "gate 2")]),
        "payments": (["order", "paid_on", "amount", "channel_note"], [("A1", "2026-02-04", "150", "cash desk")]),
    }
    records = [
        _record(["receipt", "day", "item", "memo"], "purchase",
                {"receipt": "purchase_id", "day": "purchased_at", "item": "product_name", "memo": "other"}),
        _record(["order", "day", "product", "trips", "driver_note"], "delivery",
                {"order": "order_id", "day": "delivered_at", "trips": "delivery_trip", "product": "other",
                 "driver_note": "other"}),
        _record(["order", "paid_on", "amount", "channel_note"], "payment",
                {"order": "order_id", "paid_on": "paid_at", "amount": "paid_amount", "channel_note": "other"}),
    ]
    _, out = _run(tmp_path, specs, records)

    assert "x_memo" in _columns(out, "purchases") and "x_item" not in _columns(out, "purchases")
    assert {"x_product", "x_driver_note"} <= _columns(out, "deliveries")
    assert _query(out, "SELECT x_product, x_driver_note FROM deliveries") == [("rice bag", "gate 2")]
    assert _query(out, "SELECT x_channel_note, amount FROM payments") == [("cash desk", 150.0)]


def test_unloaded_tables_are_copied_as_src_with_join_key_and_twins(tmp_path: Path) -> None:
    sales_columns = ["order", "item", "amount"]
    sales_roles = {"order": "order_id", "item": "product_name", "amount": "line_amount"}
    specs: Specs = {
        "sales": (sales_columns, [("A1", "tea", "100"), ("A2", "tea", "30")]),
        "debts": (["order_ref", "due_on", "owed"], [
            ("A1", "2026-07-01", "300"), ("A2", "2026-08-01", "200"), ("ZZ", "2026-09-01", "100")]),
        "notes": (["text"], [("called back",), ("paid late",)]),
        "receipts_bad": (["memo"], [("x",)]),  # a purchase table with no purchase id: not read by the layer
    }
    records = [
        _record(sales_columns, "sales", sales_roles),
        _record(["order_ref", "due_on", "owed"], "debt_ledger",
                {"order_ref": "other", "due_on": "other", "owed": "other"}),
        _record(["memo"], "purchase", {"memo": "other"}),
    ]
    report, out = _run(tmp_path, specs, records)

    tables = [row[0] for row in _query(out, "SELECT name FROM sqlite_master WHERE name LIKE 'src_%' ORDER BY name")]
    assert tables == ["src_debts", "src_notes", "src_receipts_bad"]
    assert report["src_tables"] == tables
    assert {"order_ref", "due_on", "owed", "xd_due_on", "xn_owed"} <= _columns(out, "src_debts")
    assert _query(out, "SELECT due_on, owed FROM src_debts ORDER BY rowid")[0] == ("2026-07-01", "300")
    assert _query(out, "SELECT xn_owed FROM src_debts ORDER BY rowid") == [(300.0,), (200.0,), (100.0,)]
    keys = _query(out, "SELECT column_name, join_key_to FROM column_catalog WHERE table_name='src_debts' "
                       "ORDER BY column_name")
    assert ("order_ref", "orders.order_id") in keys
    assert ("due_on", None) in keys
    assert report["join_keys"] == ["src_debts.order_ref"]


def test_column_catalog_describes_carried_columns(tmp_path: Path) -> None:
    specs: Specs = {"sales": (SALES_COLUMNS, [
        ("A1", "tea", "100", "", "29A-12345"),
        ("A1", "coffee", "50", "30000", ""),
        ("A2", "tea", "30", "", "43B-99999"),
    ])}
    _, out = _run(tmp_path, specs, [_record(SALES_COLUMNS, "sales", SALES_ROLES)],
                  originals={"tradein": "Trade-in value"})

    rows = _query(out, "SELECT source_table, source_header, role, value_type, distinct_count, sample_values "
                       "FROM column_catalog WHERE table_name='orders' AND column_name='x_tradein'")
    assert rows == [("sales", "Trade-in value", "other", "number", 1, json.dumps(["30000"]))]
    twin = _query(out, "SELECT value_type, sample_values FROM column_catalog "
                       "WHERE table_name='orders' AND column_name='xn_tradein'")
    assert twin == [("number", json.dumps([30000.0]))]
    plate = _query(out, "SELECT value_type, distinct_count FROM column_catalog "
                        "WHERE table_name='raw_lines' AND column_name='x_plate'")
    assert plate == [("code", 2)]


def test_money_scale_is_not_applied_to_numeric_twins_and_is_recorded(tmp_path: Path) -> None:
    specs: Specs = {"sales": (SALES_COLUMNS, [("A1", "tea", "100", "30", "")])}
    diagnostic = Diagnostic(code="AMOUNT_SCALE", message="1000", source=SourceRef(path="sales.csv", table="sales"))
    report, out = _run(tmp_path, specs, [_record(SALES_COLUMNS, "sales", SALES_ROLES)], diagnostics=(diagnostic,))

    assert _query(out, "SELECT amount FROM lines") == [(100000.0,)]  # the canonical money is scaled as before
    assert _query(out, "SELECT xn_tradein FROM orders") == [(30.0,)]  # the carried twin is not
    assert report["extra_numbers_not_scaled"] == ["sales.xn_tradein"]


def test_long_digit_strings_are_identifiers_and_short_numbers_are_amounts(tmp_path: Path) -> None:
    columns = ["order", "item", "amount", "imei", "fee"]
    roles = {"order": "order_id", "item": "product_name", "amount": "line_amount", "imei": "other", "fee": "other"}
    specs: Specs = {"sales": (columns, [
        ("C1", "phone", "100", "359506993102576", "150000"),
        ("C2", "phone", "50", "352921824498407", "20000"),
        ("C3", "case", "30", "350212858269464", "5000"),
    ])}
    _, out = _run(tmp_path, specs, [_record(columns, "sales", roles)])

    assert _query(out, "SELECT value_type FROM column_catalog WHERE table_name='orders' AND column_name='x_imei'") \
        == [("code",)]
    assert "xn_imei" in _columns(out, "orders")  # the numeric twin is still kept
    assert _query(out, "SELECT value_type FROM column_catalog WHERE table_name='orders' AND column_name='x_fee'") \
        == [("number",)]
