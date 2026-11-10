"""layer.build: sales without an order column, service tickets, status mapping and report counts.

Source files mirror ingestion: every table carries __source_row and __raw_json.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from bizreason.contracts import BusinessContext, ColumnMeta, SourceRef, TableMeta
from bizreason.semantic import layer

# name -> (column names, rows)
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


def _context(path: Path, specs: Specs, originals: dict[str, str] | None = None) -> BusinessContext:
    """originals maps a column name to the header the file shows for it, where that differs."""
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
    return BusinessContext(
        business_id="test", revision="r1", database=path, readme="", manifest={}, tables=tables,
    )


def _record(columns: list[str], kind: str, roles: dict[str, str]) -> dict[str, Any]:
    return {"columns": columns, "kind": kind, "roles": roles}


def _query(out: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(out)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_sales_table_without_order_id_gives_one_order_per_row(tmp_path: Path) -> None:
    columns = ["item", "amount"]
    specs: Specs = {
        "sales_a": (columns, [("tea", "100"), ("coffee", "50")]),
        "sales_b": (columns, [("tea", "30")]),  # same column names: shares the record, ids still differ
    }
    records = [_record(columns, "sales", {"item": "product_name", "amount": "line_amount"})]
    out = tmp_path / "canonical.sqlite"

    report = layer.build(_context(_source(tmp_path, specs), specs), records, out)

    assert report["sales_tables"] == 2
    assert report["orders"] == 3
    ids = [row[0] for row in _query(out, "SELECT order_id FROM orders ORDER BY order_id")]
    assert ids == ["sales_a:1", "sales_a:2", "sales_b:1"]
    assert _query(out, "SELECT SUM(amount) FROM lines") == [(180.0,)]
    assert _query(out, "SELECT SUM(revenue) FROM orders") == [(180.0,)]


def test_service_tickets_produce_revenue_and_employee(tmp_path: Path) -> None:
    columns = ["ticket", "visit", "mechanic", "job", "bill"]
    specs: Specs = {
        "repairs": (columns, [
            ("T1", "2026-03-01", "An", "oil change", "200"),
            ("T2", "2026-03-02", "Binh", "brake pads", "450"),
        ]),
    }
    records = [_record(columns, "service_ticket", {
        "ticket": "ticket_id", "visit": "ordered_at", "mechanic": "employee_name",
        "job": "service_item", "bill": "ticket_amount",
    })]
    out = tmp_path / "canonical.sqlite"

    report = layer.build(_context(_source(tmp_path, specs), specs), records, out)

    assert report["service_tables"] == 1
    assert report["sales_tables"] == 0
    assert _query(out, "SELECT order_id, employee, revenue FROM orders ORDER BY order_id") == [
        ("T1", "An", 200.0),
        ("T2", "Binh", 450.0),
    ]
    assert _query(out, "SELECT order_id, product_name, amount FROM lines ORDER BY order_id") == [
        ("T1", "oil change", 200.0),
        ("T2", "brake pads", 450.0),
    ]


def test_employee_name_fills_employee_for_sales_when_employee_id_absent(tmp_path: Path) -> None:
    columns = ["order", "staff", "item", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "Lan", "tea", "10")])}
    records = [_record(columns, "sales", {
        "order": "order_id", "staff": "employee_name", "item": "product_name", "amount": "line_amount",
    })]
    out = tmp_path / "canonical.sqlite"

    layer.build(_context(_source(tmp_path, specs), specs), records, out)

    assert _query(out, "SELECT employee FROM orders") == [("Lan",)]


def test_status_map_rewrites_values_and_counts_mapped(tmp_path: Path) -> None:
    columns = ["order", "state", "amount"]
    specs: Specs = {"sales": (columns, [
        ("O1", "delivered", "100"),
        ("O2", "void", "50"),
        ("O3", "weird", "70"),  # not in the map: becomes NULL
        ("O4", "completed", "10"),
    ])}
    records = [_record(columns, "sales", {"order": "order_id", "state": "order_status", "amount": "line_amount"})]
    status_map = {"delivered": "completed", "void": "cancelled"}
    out = tmp_path / "canonical.sqlite"

    report = layer.build(_context(_source(tmp_path, specs), specs), records, out, status_map=status_map)

    assert report["status_mapped"] == 2
    assert report["status_null_orders"] == 1
    assert _query(out, "SELECT order_id, status FROM orders ORDER BY order_id") == [
        ("O1", "completed"), ("O2", "cancelled"), ("O3", None), ("O4", "completed"),
    ]


def test_unmapped_value_becomes_null_and_is_counted_and_canonical_is_kept(tmp_path: Path) -> None:
    columns = ["order", "state", "amount"]
    specs: Specs = {"sales": (columns, [
        ("O1", "delivered", "100"),  # mapped
        ("O2", "hold", "50"),        # unmapped: no reading and no map entry
        ("O3", "weird", "70"),       # unmapped
        ("O4", "completed", "10"),   # canonical: a map entry for it must not change it
    ])}
    records = [_record(columns, "sales", {"order": "order_id", "state": "order_status", "amount": "line_amount"})]
    status_map = {"delivered": "completed", "completed": "returned"}
    out = tmp_path / "canonical.sqlite"

    report = layer.build(_context(_source(tmp_path, specs), specs), records, out, status_map=status_map)

    assert report["status_mapped"] == 1
    assert report["status_null_orders"] == 2
    assert _query(out, "SELECT order_id, status FROM orders ORDER BY order_id") == [
        ("O1", "completed"), ("O2", None), ("O3", None), ("O4", "completed"),
    ]


def test_status_map_defaults_to_none_and_keeps_report_keys(tmp_path: Path) -> None:
    columns = ["order", "state", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "completed", "10")])}
    records = [_record(columns, "sales", {"order": "order_id", "state": "order_status", "amount": "line_amount"})]
    out = tmp_path / "canonical.sqlite"

    report = layer.build(_context(_source(tmp_path, specs), specs), records, out)

    assert report["status_mapped"] == 0
    assert report["status_null_orders"] == 0
    assert report["orders"] == 1


def test_order_listed_by_returns_table_is_returned_whole_and_ids_are_trimmed(tmp_path: Path) -> None:
    columns = ["order", "state", "item", "amount"]
    specs: Specs = {
        "sales": (columns, [
            ("O1", "completed", "tea", "100"), ("O1", "completed", "cake", "50"),
            ("O2", "completed", "tea", "80"), ("O3", "completed", "tea", "30"),
        ]),
        "returns": (["order", "memo"], [(" O1 ", "partial"), (None, "blank id"), ("O2", "whole")]),
    }
    records = [
        _record(columns, "sales", {
            "order": "order_id", "state": "order_status", "item": "product_name", "amount": "line_amount",
        }),
        _record(["order", "memo"], "sales_return", {"order": "order_id", "memo": "note"}),
    ]
    out = tmp_path / "canonical.sqlite"

    report = layer.build(_context(_source(tmp_path, specs), specs), records, out)

    assert report["return_tables"] == 1
    assert report["orders_marked_returned"] == 2
    assert _query(out, "SELECT order_id, status FROM orders ORDER BY order_id") == [
        ("O1", "returned"), ("O2", "returned"), ("O3", "completed"),
    ]
    assert _query(out, "SELECT SUM(revenue) FROM orders WHERE status IN ('completed','shipping')") == [(30.0,)]
    assert _query(out, "SELECT DISTINCT status FROM lines ORDER BY status") == [("completed",), ("returned",)]
    assert _query(out, "SELECT status FROM status_domain ORDER BY status") == [
        ("completed",), ("returned",),
    ]


def test_cancelled_order_listed_by_returns_table_stays_cancelled(tmp_path: Path) -> None:
    columns = ["order", "state", "item", "amount"]
    specs: Specs = {
        "sales": (columns, [("O1", "cancelled", "tea", "100"), ("O2", "completed", "tea", "80")]),
        "returns": (["order", "memo"], [("O1", "x"), ("O2", "y")]),
    }
    records = [
        _record(columns, "sales", {
            "order": "order_id", "state": "order_status", "item": "product_name", "amount": "line_amount",
        }),
        _record(["order", "memo"], "sales_return", {"order": "order_id", "memo": "note"}),
    ]
    out = tmp_path / "canonical.sqlite"

    report = layer.build(_context(_source(tmp_path, specs), specs), records, out)

    assert report["orders_marked_returned"] == 1
    assert _query(out, "SELECT order_id, status FROM orders ORDER BY order_id") == [
        ("O1", "cancelled"), ("O2", "returned"),
    ]


def test_discount_and_shipping_lines_are_excluded_from_revenue_and_lines(tmp_path: Path) -> None:
    columns = ["order", "item", "amount"]
    specs: Specs = {"sales": (columns, [
        ("O1", "tea", "100"),
        ("O1", "voucher", "-20"),
        ("O1", "delivery", "15"),
    ])}
    records = [_record(columns, "sales", {"order": "order_id", "item": "product_name", "amount": "line_amount"})]
    line_kinds = {"Voucher": "discount", "Delivery": "shipping_fee"}
    out = tmp_path / "canonical.sqlite"

    report = layer.build(_context(_source(tmp_path, specs), specs), records, out, line_kinds=line_kinds)

    assert report["non_product_lines"] == 2
    assert _query(out, "SELECT revenue FROM orders") == [(100.0,)]
    assert _query(out, "SELECT product_name FROM lines") == [("tea",)]


def test_service_fee_counts_in_revenue_but_is_not_an_item(tmp_path: Path) -> None:
    columns = ["order", "item", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "100"), ("O1", "service charge", "10")])}
    records = [_record(columns, "sales", {"order": "order_id", "item": "product_name", "amount": "line_amount"})]
    out = tmp_path / "canonical.sqlite"

    report = layer.build(
        _context(_source(tmp_path, specs), specs), records, out,
        line_kinds={"Service Charge": "service_fee"},
    )

    assert report["non_product_lines"] == 1
    assert _query(out, "SELECT revenue FROM orders") == [(110.0,)]
    assert _query(out, "SELECT product_name, is_item FROM lines ORDER BY product_name") == [
        ("service charge", 0), ("tea", 1),
    ]


def test_without_line_kinds_every_line_is_a_product(tmp_path: Path) -> None:
    columns = ["order", "item", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "100"), ("O1", "voucher", "-20")])}
    records = [_record(columns, "sales", {"order": "order_id", "item": "product_name", "amount": "line_amount"})]
    out = tmp_path / "canonical.sqlite"

    report = layer.build(_context(_source(tmp_path, specs), specs), records, out)

    assert report["non_product_lines"] == 0
    assert _query(out, "SELECT revenue FROM orders") == [(80.0,)]
    assert _query(out, "SELECT is_item FROM lines ORDER BY product_name") == [(1,), (1,)]


def test_unknown_line_kind_is_rejected(tmp_path: Path) -> None:
    columns = ["order", "item", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "100")])}
    records = [_record(columns, "sales", {"order": "order_id", "item": "product_name", "amount": "line_amount"})]

    with pytest.raises(ValueError):
        layer.build(_context(_source(tmp_path, specs), specs), records, tmp_path / "c.sqlite",
                    line_kinds={"tea": "gift"})


def test_lines_expose_unit_price_from_source_line(tmp_path: Path) -> None:
    columns = ["order", "item", "price", "qty", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "25", "2", "50")])}
    records = [_record(columns, "sales", {
        "order": "order_id", "item": "product_name", "price": "unit_price", "qty": "qty", "amount": "line_amount",
    })]
    out = tmp_path / "canonical.sqlite"

    layer.build(_context(_source(tmp_path, specs), specs), records, out)

    assert _query(out, "SELECT unit_price, amount FROM lines") == [(25.0, 50.0)]


def _run(tmp_path: Path, specs: Specs, records: list[dict[str, Any]], **kwargs: Any) -> tuple[dict, Path]:
    out = tmp_path / "canonical.sqlite"
    report = layer.build(_context(_source(tmp_path, specs), specs), records, out, **kwargs)
    return report, out


SALES_ROLES = {"order": "order_id", "item": "product_name", "amount": "line_amount"}


def test_identical_rows_of_one_order_are_counted_once(tmp_path: Path) -> None:
    columns = ["order", "item", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "100"), ("O1", "tea", "100"), ("O2", "tea", "100")])}
    report, out = _run(tmp_path, specs, [_record(columns, "sales", SALES_ROLES)])

    assert report["duplicate_lines_removed"] == 1
    assert _query(out, "SELECT order_id, amount FROM lines ORDER BY order_id") == [("O1", 100.0), ("O2", 100.0)]


def test_identical_rows_of_different_orders_are_both_kept(tmp_path: Path) -> None:
    columns = ["order", "item", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "100"), ("O2", "tea", "100")])}
    report, out = _run(tmp_path, specs, [_record(columns, "sales", SALES_ROLES)])

    assert report["duplicate_lines_removed"] == 0
    assert _query(out, "SELECT COUNT(*), SUM(amount) FROM lines") == [(2, 200.0)]


def test_rows_of_one_order_differing_in_one_cell_are_both_kept(tmp_path: Path) -> None:
    columns = ["order", "item", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "100"), ("O1", "tea", "90")])}
    report, out = _run(tmp_path, specs, [_record(columns, "sales", SALES_ROLES)])

    assert report["duplicate_lines_removed"] == 0
    assert _query(out, "SELECT COUNT(*), SUM(amount) FROM lines") == [(2, 190.0)]


def test_table_without_order_id_is_not_deduplicated(tmp_path: Path) -> None:
    columns = ["item", "amount"]
    specs: Specs = {"sales": (columns, [("tea", "100"), ("tea", "100")])}
    report, out = _run(tmp_path, specs, [_record(columns, "sales", {"item": "product_name", "amount": "line_amount"})])

    assert report["duplicate_lines_removed"] == 0
    assert _query(out, "SELECT order_id FROM orders ORDER BY order_id") == [("sales:1",), ("sales:2",)]
    assert _query(out, "SELECT SUM(amount) FROM lines") == [(200.0,)]


def test_grouped_thousands_column_is_read_and_decimal_comma_column_is_not(tmp_path: Path) -> None:
    columns = ["order", "item", "amount", "qty"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "75,000", "12,5"), ("O2", "tea", "1,250,000", "7,25")])}
    records = [_record(columns, "sales", {**SALES_ROLES, "qty": "qty"})]
    report, out = _run(tmp_path, specs, records)

    assert report["columns_with_locale_inferred"] == 1
    assert _query(out, "SELECT order_id, amount, qty FROM lines ORDER BY order_id") == [
        ("O1", 75000.0, 12.5), ("O2", 1250000.0, 7.25),
    ]


def test_mixed_comma_column_keeps_todays_reading(tmp_path: Path) -> None:
    columns = ["order", "item", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "75,000"), ("O2", "tea", "12,5")])}
    report, out = _run(tmp_path, specs, [_record(columns, "sales", SALES_ROLES)])

    assert report["columns_with_locale_inferred"] == 0
    assert _query(out, "SELECT order_id, amount FROM lines ORDER BY order_id") == [("O1", None), ("O2", 12.5)]


def test_dot_grouped_column_with_currency_suffix_is_read_as_vietnamese_thousands(tmp_path: Path) -> None:
    columns = ["order", "item", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "1.500.000"), ("O2", "tea", "250.000đ")])}
    report, out = _run(tmp_path, specs, [_record(columns, "sales", SALES_ROLES)])

    assert report["columns_with_locale_inferred"] == 1
    assert _query(out, "SELECT order_id, amount FROM lines ORDER BY order_id") == [
        ("O1", 1500000.0), ("O2", 250000.0),
    ]


def test_locale_inference_refuses_columns_that_mix_separator_shapes() -> None:
    assert layer.infer_locale(["1,000", "2.000"]) is None
    assert layer.infer_locale(["12,5", "7,25"]) is None
    assert layer.infer_locale(["1.5", "1,000"]) == "en"
    assert layer.infer_locale(["1.000.000đ", "300.000"]) == "vi"


def test_order_time_is_combined_with_the_row_date(tmp_path: Path) -> None:
    columns = ["order", "day", "clock", "item", "amount"]
    specs: Specs = {"sales": (columns, [
        ("O1", "2026-03-01", "14:05", "tea", "100"),
        ("O2", "2026-03-01", "6:30:15", "tea", "50"),
        ("O3", "2026-03-02", "", "tea", "10"),
    ])}
    records = [_record(columns, "sales", {
        "order": "order_id", "day": "ordered_at", "clock": "order_time", "item": "product_name",
        "amount": "line_amount",
    })]
    _, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, date, ts FROM orders ORDER BY order_id") == [
        ("O1", "2026-03-01", "2026-03-01 14:05:00"),
        ("O2", "2026-03-01", "2026-03-01 06:30:15"),
        ("O3", "2026-03-02", "2026-03-02"),  # blank time: the date text stands
    ]


def test_full_datetime_in_order_time_is_kept(tmp_path: Path) -> None:
    columns = ["order", "placed", "item", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "2026-03-01 09:30:00", "tea", "100")])}
    _, out = _run(tmp_path, specs, [_record(columns, "sales", {
        "order": "order_id", "placed": "order_time", "item": "product_name", "amount": "line_amount",
    })])
    assert _query(out, "SELECT ts FROM orders") == [("2026-03-01 09:30:00",)]


def test_ordered_at_datetime_without_order_time_keeps_working(tmp_path: Path) -> None:
    columns = ["order", "stamp", "item", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "2026-03-01 09:30:00", "tea", "100")])}
    _, out = _run(tmp_path, specs, [_record(columns, "sales", {
        "order": "order_id", "stamp": "ordered_at", "item": "product_name", "amount": "line_amount",
    })])
    assert _query(out, "SELECT ts, date FROM orders") == [("2026-03-01 09:30:00", "2026-03-01")]


def test_customer_group_is_filled_from_catalog_by_id_and_never_by_name_when_an_id_is_given(tmp_path: Path) -> None:
    sales_columns = ["order", "cust_id", "cust_name", "item", "amount"]
    catalog_columns = ["code", "name", "segment"]
    specs: Specs = {
        "sales": (sales_columns, [
            ("O1", "C1", "An", "tea", "100"),
            ("O2", None, "Binh", "tea", "50"),     # no id: matched by name
            ("O3", "C9", "Cuong", "tea", "30"),    # id not in the catalog: stays empty
            ("O4", "C9", "An", "tea", "10"),       # same name as C1 but another code: stays empty
        ]),
        "customers": (catalog_columns, [("C1", "An", "Gold"), ("C2", "Binh", "Silver")]),
    }
    records = [
        _record(sales_columns, "sales", {
            "order": "order_id", "cust_id": "customer_id", "cust_name": "customer_name",
            "item": "product_name", "amount": "line_amount",
        }),
        _record(catalog_columns, "customer_catalog", {
            "code": "customer_id", "name": "customer_name", "segment": "customer_group",
        }),
    ]
    _, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, customer_group FROM orders ORDER BY order_id") == [
        ("O1", "Gold"), ("O2", "Silver"), ("O3", None), ("O4", None),
    ]
    assert _query(out, "SELECT customer_group FROM lines WHERE order_id='O2'") == [("Silver",)]


def test_customer_group_from_name_only_catalog(tmp_path: Path) -> None:
    sales_columns = ["order", "cust_name", "item", "amount"]
    catalog_columns = ["name", "segment"]
    specs: Specs = {
        "sales": (sales_columns, [("O1", "An", "tea", "100")]),
        "customers": (catalog_columns, [("An", "Gold")]),
    }
    records = [
        _record(sales_columns, "sales", {"order": "order_id", "cust_name": "customer_name",
                                         "item": "product_name", "amount": "line_amount"}),
        _record(catalog_columns, "customer_catalog", {"name": "customer_name", "segment": "customer_group"}),
    ]
    _, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT customer_group FROM orders") == [("Gold",)]


def test_product_category_and_base_unit_come_from_the_catalog(tmp_path: Path) -> None:
    sales_columns = ["order", "pid", "pname", "cat", "amount"]
    catalog_columns = ["code", "name", "group", "unit_base"]
    specs: Specs = {
        "sales": (sales_columns, [
            ("O1", "P1", "Tra sua", None, "100"),       # by id
            ("O2", None, "Tra  Sua", None, "50"),       # by normalized name
            ("O3", "P9", "Ca phe", "Own", "30"),        # own category kept; base unit by name
        ]),
        "products": (catalog_columns, [("P1", "Tra sua", "Drink", "ly"), ("P2", "Ca phe", "Coffee", "goi")]),
    }
    records = [
        _record(sales_columns, "sales", {
            "order": "order_id", "pid": "product_id", "pname": "product_name",
            "cat": "product_category", "amount": "line_amount",
        }),
        _record(catalog_columns, "product_catalog", {
            "code": "product_id", "name": "product_name", "group": "product_category", "unit_base": "base_unit",
        }),
    ]
    _, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, category, base_unit FROM lines ORDER BY order_id") == [
        ("O1", "Drink", "ly"), ("O2", "Drink", "ly"), ("O3", "Own", "goi"),
    ]


def test_base_unit_comes_from_unit_conversion_by_name(tmp_path: Path) -> None:
    sales_columns = ["item", "amount"]
    conversion_columns = ["item", "base", "factor"]
    specs: Specs = {
        "sales": (sales_columns, [("Ca phe", "30"), ("Tra sua", "10")]),
        "conversions": (conversion_columns, [("Ca phe", "goi", "24")]),
    }
    records = [
        _record(sales_columns, "sales", {"item": "product_name", "amount": "line_amount"}),
        _record(conversion_columns, "unit_conversion", {
            "item": "product_name", "base": "base_unit", "factor": "conversion_factor",
        }),
    ]
    _, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT product_name, base_unit FROM lines ORDER BY product_name") == [
        ("Ca phe", "goi"), ("Tra sua", None),
    ]


def test_alias_sheet_pools_spellings_under_the_full_name(tmp_path: Path) -> None:
    sales_columns = ["order", "item", "amount"]
    alias_columns = ["short", "full"]
    specs: Specs = {
        "sales": (sales_columns, [
            ("O1", "TS", "100"),
            ("O2", "tra sua lon", "50"),
            ("O3", "Tra Sua Lon", "30"),
            ("O4", "TS ", "20"),
            ("O5", "TSX", "10"),     # not an exact normalized match
            ("O6", "T S", "5"),      # normalizes to t s, not ts
        ]),
        "aliases": (alias_columns, [("TS", "Tra sua lon")]),
    }
    records = [
        _record(sales_columns, "sales", SALES_ROLES),
        _record(alias_columns, "alias", {"short": "alias_key", "full": "product_name"}),
    ]
    report, out = _run(tmp_path, specs, records)

    assert report["alias_lines_resolved"] == 2
    assert _query(out, "SELECT order_id, product_name, product_key FROM lines ORDER BY order_id") == [
        ("O1", "Tra sua lon", "tra sua lon"),
        ("O2", "tra sua lon", "tra sua lon"),
        ("O3", "Tra Sua Lon", "tra sua lon"),
        ("O4", "Tra sua lon", "tra sua lon"),
        ("O5", "TSX", "tsx"),
        ("O6", "T S", "t s"),
    ]


def test_service_fee_is_revenue_but_not_an_item_without_line_kinds(tmp_path: Path) -> None:
    columns = ["order", "item", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "100"), ("O1", "Phí phục vụ", "10")])}
    report, out = _run(tmp_path, specs, [_record(columns, "sales", SALES_ROLES)])

    assert report["non_product_lines"] == 0
    assert _query(out, "SELECT revenue FROM orders") == [(110.0,)]
    assert _query(out, "SELECT product_name, is_item FROM lines ORDER BY product_name") == [
        ("Phí phục vụ", 0), ("tea", 1),
    ]


def test_barcode_like_id_loses_to_the_id_that_joins_the_catalog(tmp_path: Path) -> None:
    sales_columns = ["order", "barcode", "sku", "amount"]
    specs: Specs = {
        "sales": (sales_columns, [("O1", "8934567890123", "P1", "100"), ("O2", "8934567890124", "P2", "50")]),
        "products": (["code", "name"], [("P1", "Tra sua"), ("P2", "Ca phe")]),
    }
    records = [
        _record(sales_columns, "sales", {
            "order": "order_id", "barcode": "product_id", "sku": "product_id", "amount": "line_amount",
        }),
        _record(["code", "name"], "product_catalog", {"code": "product_id", "name": "product_name"}),
    ]
    _, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, product_id FROM lines ORDER BY order_id") == [("O1", "P1"), ("O2", "P2")]


def test_constant_group_column_loses_to_a_varying_one(tmp_path: Path) -> None:
    columns = ["order", "segment", "tier", "item", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "Retail", "A", "tea", "100"), ("O2", "Retail", "B", "tea", "50")])}
    records = [_record(columns, "sales", {
        "order": "order_id", "segment": "customer_group", "tier": "customer_group",
        "item": "product_name", "amount": "line_amount",
    })]
    _, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, customer_group FROM orders ORDER BY order_id") == [("O1", "A"), ("O2", "B")]


def test_varying_category_column_beats_a_constant_one(tmp_path: Path) -> None:
    columns = ["order", "item", "fixed", "varied", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "Drink", "Hot", "100"), ("O2", "cake", "Drink", "Cold", "50")])}
    records = [_record(columns, "sales", {
        "order": "order_id", "item": "product_name", "fixed": "product_category",
        "varied": "product_category", "amount": "line_amount",
    })]
    _, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, category FROM lines ORDER BY order_id") == [("O1", "Hot"), ("O2", "Cold")]


def test_timestamp_column_beats_a_date_only_column(tmp_path: Path) -> None:
    columns = ["order", "day", "stamp", "item", "amount"]
    specs: Specs = {"sales": (columns, [
        ("O1", "2026-03-01", "2026-03-01 14:05:00", "tea", "100"),
        ("O2", "2026-03-02", "2026-03-02 09:00:00", "tea", "50"),
    ])}
    records = [_record(columns, "sales", {
        "order": "order_id", "day": "ordered_at", "stamp": "ordered_at", "item": "product_name",
        "amount": "line_amount",
    })]
    _, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, date, ts FROM orders ORDER BY order_id") == [
        ("O1", "2026-03-01", "2026-03-01 14:05:00"),
        ("O2", "2026-03-02", "2026-03-02 09:00:00"),
    ]


def test_product_name_is_filled_only_on_an_exact_id_match(tmp_path: Path) -> None:
    sales_columns = ["order", "pid", "amount"]
    specs: Specs = {
        "sales": (sales_columns, [
            ("O1", "P1", "100"),
            ("O2", " P1 ", "50"),   # padding is ignored: the id joins
            ("O3", "P1X", "30"),    # not the same id
            ("O4", "P2", "10"),     # two catalog names for this id: stays empty
        ]),
        "products": (["code", "name"], [("P1", "Tra sua"), ("P2", "Ca phe"), ("P2", "Ca phe 2")]),
    }
    records = [
        _record(sales_columns, "sales", {"order": "order_id", "pid": "product_id", "amount": "line_amount"}),
        _record(["code", "name"], "product_catalog", {"code": "product_id", "name": "product_name"}),
    ]
    _, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, product_name FROM lines ORDER BY order_id") == [
        ("O1", "Tra sua"), ("O2", "Tra sua"), ("O3", None), ("O4", None),
    ]


def test_customer_name_is_filled_by_id_and_an_existing_name_is_kept(tmp_path: Path) -> None:
    sales_columns = ["order", "cust", "name", "item", "amount"]
    specs: Specs = {
        "sales": (sales_columns, [
            ("O1", "C1", None, "tea", "100"),
            ("O2", "C1", "Lan", "tea", "50"),
            ("O3", "C9", None, "tea", "30"),
        ]),
        "customers": (["code", "name"], [("C1", "An")]),
    }
    records = [
        _record(sales_columns, "sales", {
            "order": "order_id", "cust": "customer_id", "name": "customer_name",
            "item": "product_name", "amount": "line_amount",
        }),
        _record(["code", "name"], "customer_catalog", {"code": "customer_id", "name": "customer_name"}),
    ]
    _, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, customer_name FROM orders ORDER BY order_id") == [
        ("O1", "An"), ("O2", "Lan"), ("O3", None),
    ]


def test_purchase_lines_take_date_status_and_supplier_from_their_receipt(tmp_path: Path) -> None:
    purchase_columns = ["receipt", "day", "state", "maker", "pid", "qty", "cost"]
    specs: Specs = {
        "imports": (purchase_columns, [
            ("R1", "2026-03-01", "completed", "S1", "P1", "2", "100"),
            ("R1", None, None, None, "P2", "1", "50"),
        ]),
        "products": (["code", "name"], [("P2", "Ca phe")]),
        "suppliers": (["maker_code", "maker_name"], [("S1", "Cty A")]),
    }
    records = [
        _record(purchase_columns, "purchase", {
            "receipt": "purchase_id", "day": "purchased_at", "state": "purchase_status", "maker": "supplier_id",
            "pid": "product_id", "qty": "qty_purchased", "cost": "purchase_unit_cost",
        }),
        _record(["code", "name"], "product_catalog", {"code": "product_id", "name": "product_name"}),
        _record(["maker_code", "maker_name"], "supplier_list", {"maker_code": "supplier_id", "maker_name": "supplier_name"}),
    ]
    _, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT product_id, date, status, supplier_id, supplier_name, product_name "
                       "FROM purchases ORDER BY product_id") == [
        ("P1", "2026-03-01", "completed", "S1", "Cty A", None),
        ("P2", "2026-03-01", "completed", "S1", "Cty A", "Ca phe"),
    ]


def test_prescription_and_active_ingredient_come_from_the_catalog(tmp_path: Path) -> None:
    sales_columns = ["order", "pid", "item", "amount"]
    specs: Specs = {
        "sales": (sales_columns, [
            ("O1", "P1", "Paracetamol", "100"),
            ("O2", "P2", "Vitamin C", "50"),
            ("O3", "P3", "Gift", "30"),
            ("O4", None, "Paracetamol", "20"),  # no id: matched by name
        ]),
        "products": (["code", "name", "rx", "ingredient"], [
            ("P1", "Paracetamol", "True", "paracetamol"),
            ("P2", "Vitamin C", "FALSE", "ascorbic"),
            ("P3", "Gift", "maybe", ""),
        ]),
    }
    records = [
        _record(sales_columns, "sales", {"order": "order_id", "pid": "product_id", "item": "product_name",
                                         "amount": "line_amount"}),
        _record(["code", "name", "rx", "ingredient"], "product_catalog", {
            "code": "product_id", "name": "product_name", "rx": "prescription_flag", "ingredient": "active_ingredient",
        }),
    ]
    _, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, prescription, active_ingredient FROM lines ORDER BY order_id") == [
        ("O1", 1, "paracetamol"), ("O2", 0, "ascorbic"), ("O3", None, None), ("O4", 1, "paracetamol"),
    ]


def test_product_kind_comes_from_the_alias_sheet(tmp_path: Path) -> None:
    specs: Specs = {
        "sales": (["order", "item", "amount"], [("O1", "IP", "100"), ("O2", "Iphone X", "50"), ("O3", "Tea", "10")]),
        "aliases": (["short", "full", "kind"], [("IP", "Iphone X", "new")]),
    }
    records = [
        _record(["order", "item", "amount"], "sales", SALES_ROLES),
        _record(["short", "full", "kind"], "alias", {"short": "alias_key", "full": "product_name",
                                                     "kind": "product_kind"}),
    ]
    _, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, product_name, product_kind FROM lines ORDER BY order_id") == [
        ("O1", "Iphone X", "new"), ("O2", "Iphone X", "new"), ("O3", "Tea", None),
    ]


def test_service_ticket_without_status_column_is_completed(tmp_path: Path) -> None:
    columns = ["ticket", "visit", "job", "bill"]
    specs: Specs = {"repairs": (columns, [("T1", "2026-03-01", "oil change", "200"),
                                          ("T2", "2026-03-02", "brake pads", "450")])}
    records = [_record(columns, "service_ticket", {
        "ticket": "ticket_id", "visit": "ordered_at", "job": "service_item", "bill": "ticket_amount",
    })]
    report, out = _run(tmp_path, specs, records)

    assert report["tickets_defaulted_completed"] == 2
    assert _query(out, "SELECT order_id, status FROM orders ORDER BY order_id") == [("T1", "completed"), ("T2", "completed")]


def test_service_ticket_with_status_column_keeps_its_own_status(tmp_path: Path) -> None:
    columns = ["ticket", "visit", "state", "job", "bill"]
    specs: Specs = {"repairs": (columns, [("T1", "2026-03-01", "cancelled", "oil change", "200"),
                                          ("T2", "2026-03-02", "completed", "brake pads", "450")])}
    records = [_record(columns, "service_ticket", {
        "ticket": "ticket_id", "visit": "ordered_at", "state": "order_status", "job": "service_item",
        "bill": "ticket_amount",
    })]
    report, out = _run(tmp_path, specs, records)

    assert report["tickets_defaulted_completed"] == 0
    assert _query(out, "SELECT order_id, status FROM orders ORDER BY order_id") == [
        ("T1", "cancelled"), ("T2", "completed"),
    ]


def test_deliveries_table_is_built_from_delivery_tables(tmp_path: Path) -> None:
    specs: Specs = {
        "sales": (["order", "item", "amount"], [("O1", "tea", "100"), ("O2", "tea", "50")]),
        "shipping": (["order", "when", "trip", "qty"], [("O1", "2026-03-02", "T1", "3"), ("O2", "2026-03-03", "T1", "1")]),
    }
    records = [
        _record(["order", "item", "amount"], "sales", SALES_ROLES),
        _record(["order", "when", "trip", "qty"], "delivery", {
            "order": "order_id", "when": "delivered_at", "trip": "delivery_trip", "qty": "delivery_qty",
        }),
    ]
    report, out = _run(tmp_path, specs, records)

    assert report["deliveries"] == 2
    assert _query(out, "SELECT order_id, date, trip, qty FROM deliveries ORDER BY order_id") == [
        ("O1", "2026-03-02", "T1", 3.0), ("O2", "2026-03-03", "T1", 1.0),
    ]


def test_deliveries_qty_is_null_without_a_qty_role(tmp_path: Path) -> None:
    specs: Specs = {
        "sales": (["order", "item", "amount"], [("O1", "tea", "100")]),
        "shipping": (["order", "when", "trip"], [("O1", "2026-03-02", "T1")]),
    }
    records = [
        _record(["order", "item", "amount"], "sales", SALES_ROLES),
        _record(["order", "when", "trip"], "delivery", {"order": "order_id", "when": "delivered_at",
                                                        "trip": "delivery_trip"}),
    ]
    _, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, date, trip, qty FROM deliveries") == [("O1", "2026-03-02", "T1", None)]


def test_deliveries_table_exists_and_is_empty_without_delivery_tables(tmp_path: Path) -> None:
    specs: Specs = {"sales": (["order", "item", "amount"], [("O1", "tea", "100")])}
    records = [_record(["order", "item", "amount"], "sales", SALES_ROLES)]
    report, out = _run(tmp_path, specs, records)

    assert report["deliveries"] == 0
    assert _query(out, "SELECT COUNT(*) FROM deliveries") == [(0,)]


def test_shop_facts_are_true_when_address_group_and_dated_orders_exist(tmp_path: Path) -> None:
    columns = ["order", "day", "place", "seg", "item", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "2026-03-01", "Ha Noi", "Gold", "tea", "100")])}
    records = [_record(columns, "sales", {
        "order": "order_id", "day": "ordered_at", "place": "other", "seg": "customer_group",
        "item": "product_name", "amount": "line_amount",
    })]
    out = tmp_path / "canonical.sqlite"
    layer.build(_context(_source(tmp_path, specs), specs, {"place": "Dia chi giao hang"}), records, out)

    assert _query(out, "SELECT key, value FROM shop_facts ORDER BY key") == [
        ("has_address", 1), ("has_customer_group", 1), ("has_orders", 1),
    ]


def test_shop_facts_are_false_without_address_group_or_dated_orders(tmp_path: Path) -> None:
    columns = ["order", "item", "amount", "memo"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "100", "wrapped")])}
    records = [_record(columns, "sales", {"order": "order_id", "item": "product_name", "amount": "line_amount",
                                          "memo": "note"})]
    out = tmp_path / "canonical.sqlite"
    layer.build(_context(_source(tmp_path, specs), specs, {"memo": "Ghi chu"}), records, out)

    assert _query(out, "SELECT key, value FROM shop_facts ORDER BY key") == [
        ("has_address", 0), ("has_customer_group", 0), ("has_orders", 0),
    ]


def _with_raw_rows(path: Path, table: str, raw_rows: list[list[Any]]) -> None:
    """Write each source row's original cells as ingestion does: a JSON array in column order, by __source_row."""
    con = sqlite3.connect(path)
    for number, row in enumerate(raw_rows, start=1):
        con.execute(f'UPDATE "{table}" SET __raw_json=? WHERE __source_row=?', (json.dumps(row, ensure_ascii=False), number))
    con.commit()
    con.close()


def test_unrecognized_status_is_recovered_from_raw_and_mapped(tmp_path: Path) -> None:
    columns = ["order", "item", "amount", "state"]
    specs: Specs = {"sales": (columns, [
        ("O1", "tea", "100", None), ("O2", "tea", "50", "completed"), ("O3", "tea", "10", None),
    ])}
    source = _source(tmp_path, specs)
    _with_raw_rows(source, "sales", [["O1", "tea", "100", "Bom hang"], ["O2", "tea", "50", "completed"],
                                     ["O3", "tea", "10", "Mystery"]])
    records = [_record(columns, "sales", {"order": "order_id", "item": "product_name", "amount": "line_amount",
                                          "state": "order_status"})]
    out = tmp_path / "canonical.sqlite"

    report = layer.build(_context(source, specs), records, out, status_map={"Bom hang": "cancelled"})

    assert report["status_mapped"] == 1
    assert _query(out, "SELECT order_id, status FROM orders ORDER BY order_id") == [
        ("O1", "cancelled"), ("O2", "completed"), ("O3", None),
    ]


def test_note_states_cancelled_on_a_sales_ledger_without_status_column(tmp_path: Path) -> None:
    columns = ["order", "item", "amount", "note"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "100", "khach huy don"), ("O2", "tea", "50", "giao buoi sang")])}
    records = [_record(columns, "sales", {"order": "order_id", "item": "product_name", "amount": "line_amount",
                                          "note": "note"})]
    report, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, status FROM orders ORDER BY order_id") == [
        ("O1", "cancelled"), ("O2", "completed"),
    ]
    assert report["status_null_orders"] == 0


def test_service_ticket_note_states_its_status_else_completed(tmp_path: Path) -> None:
    columns = ["ticket", "visit", "job", "bill", "memo"]
    specs: Specs = {"repairs": (columns, [
        ("T1", "2026-03-01", "oil change", "200", "dang giao"),
        ("T2", "2026-03-02", "brake pads", "450", "thay dau"),
    ])}
    records = [_record(columns, "service_ticket", {
        "ticket": "ticket_id", "visit": "ordered_at", "job": "service_item", "bill": "ticket_amount", "memo": "note",
    })]
    report, out = _run(tmp_path, specs, records)

    assert report["tickets_defaulted_completed"] == 0
    assert _query(out, "SELECT order_id, status FROM orders ORDER BY order_id") == [
        ("T1", "shipping"), ("T2", "completed"),
    ]


def test_note_naming_a_person_huy_is_not_cancelled(tmp_path: Path) -> None:
    columns = ["order", "item", "amount", "note"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "100", "anh Huy giao hang sau")])}
    records = [_record(columns, "sales", {"order": "order_id", "item": "product_name", "amount": "line_amount",
                                          "note": "note"})]
    _, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT status FROM orders") == [("completed",)]


def _unit_catalog(name: str, columns: list[str], rows: list[tuple[Any, ...]], roles: dict[str, str],
                  kind: str) -> tuple[Specs, dict[str, Any]]:
    return {name: (columns, rows)}, _record(columns, kind, roles)


SALES_UNIT_ROLES = {"order": "order_id", "item": "product_name", "unit": "unit", "qty": "qty", "amount": "line_amount"}
SALES_UNIT_COLUMNS = ["order", "item", "unit", "qty", "amount"]


def test_qty_base_is_the_quantity_when_the_sold_unit_is_the_base_unit(tmp_path: Path) -> None:
    catalog_specs, catalog = _unit_catalog("catalog", ["item", "base"], [("tea", "cup")],
                                           {"item": "product_name", "base": "base_unit"}, "product_catalog")
    specs: Specs = {"sales": (SALES_UNIT_COLUMNS, [("O1", "tea", "CUP", "2", "100"), ("O2", "tea", "Cup", "3", "150")]),
                    **catalog_specs}
    records = [_record(SALES_UNIT_COLUMNS, "sales", SALES_UNIT_ROLES), catalog]
    report, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, qty_base FROM lines ORDER BY order_id") == [("O1", 2.0), ("O2", 3.0)]
    assert report["qty_base_derived"] == 2


def test_qty_base_is_quantity_times_the_factor_of_the_sold_unit(tmp_path: Path) -> None:
    catalog_specs, catalog = _unit_catalog("conversions", ["item", "large", "base", "factor"],
                                           [("tea", "box", "cup", "10")],
                                           {"item": "product_name", "large": "large_unit", "base": "base_unit",
                                            "factor": "conversion_factor"}, "unit_conversion")
    specs: Specs = {"sales": (SALES_UNIT_COLUMNS, [("O1", "tea", "box", "2", "200"), ("O2", "tea", "cup", "3", "150")]),
                    **catalog_specs}
    records = [_record(SALES_UNIT_COLUMNS, "sales", SALES_UNIT_ROLES), catalog]
    report, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, qty_base FROM lines ORDER BY order_id") == [("O1", 20.0), ("O2", 3.0)]
    assert report["qty_base_derived"] == 2


def test_qty_base_stays_null_when_the_sold_unit_has_no_conversion(tmp_path: Path) -> None:
    catalog_specs, catalog = _unit_catalog("conversions", ["item", "large", "base", "factor"],
                                           [("tea", "box", "cup", "10")],
                                           {"item": "product_name", "large": "large_unit", "base": "base_unit",
                                            "factor": "conversion_factor"}, "unit_conversion")
    specs: Specs = {"sales": (SALES_UNIT_COLUMNS, [("O1", "tea", "box", "2", "200"), ("O2", "tea", "bottle", "1", "50")]),
                    **catalog_specs}
    records = [_record(SALES_UNIT_COLUMNS, "sales", SALES_UNIT_ROLES), catalog]
    report, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, qty_base FROM lines ORDER BY order_id") == [("O1", 20.0), ("O2", None)]
    assert report["qty_base_derived"] == 1


def test_qty_base_is_the_quantity_when_the_product_has_no_conversion_at_all(tmp_path: Path) -> None:
    specs: Specs = {"sales": (SALES_UNIT_COLUMNS, [("O1", "tea", "pack", "4", "80")])}
    records = [_record(SALES_UNIT_COLUMNS, "sales", SALES_UNIT_ROLES)]
    report, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT qty_base FROM lines") == [(4.0,)]
    assert report["qty_base_derived"] == 1


def test_sales_loads_when_every_ingestion_role_is_none(tmp_path: Path) -> None:
    columns = ["order", "day", "item", "state", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "2026-03-01", "tea", "cancelled", "100"),
                                        ("O2", "2026-03-02", "tea", "completed", "50")])}
    records = [_record(columns, "sales", {"order": "order_id", "day": "ordered_at", "item": "product_name",
                                          "state": "order_status", "amount": "line_amount"})]
    path = _source(tmp_path, specs)
    context = _context(path, specs)
    blank = [table.model_copy(update={"columns": [c.model_copy(update={"semantic_role": None}) for c in table.columns]})
             for table in context.tables]
    context = context.model_copy(update={"tables": blank})
    assert all(c.semantic_role is None for t in context.tables for c in t.columns)
    out = tmp_path / "canonical.sqlite"

    layer.build(context, records, out)

    assert _query(out, "SELECT order_id, date, status FROM orders ORDER BY order_id") == [
        ("O1", "2026-03-01", "cancelled"), ("O2", "2026-03-02", "completed"),
    ]


def _stats(out: Path) -> dict[str, int]:
    return dict(_query(out, "SELECT key, value FROM build_stats"))


def test_quantity_with_unit_word_is_read_and_counted(tmp_path: Path) -> None:
    columns = ["order", "item", "qty", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "1 ly", "100"), ("O2", "tea", "two", "100"),
                                        ("O3", "tea", "", "100")])}
    records = [_record(columns, "sales", {"order": "order_id", "item": "product_name", "qty": "qty",
                                          "amount": "line_amount"})]
    report, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, qty FROM lines ORDER BY order_id") == [
        ("O1", 1.0), ("O2", None), ("O3", None),
    ]
    stats = _stats(out)
    assert stats["qty_cells_nonblank"] == 2
    assert stats["qty_cells_unparsed"] == 1
    assert stats["money_cells_nonblank"] == 3
    assert stats["money_cells_unparsed"] == 0


def test_money_with_unit_word_stays_invalid(tmp_path: Path) -> None:
    columns = ["order", "item", "qty", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "1", "1 ly")])}
    records = [_record(columns, "sales", {"order": "order_id", "item": "product_name", "qty": "qty",
                                          "amount": "line_amount"})]
    report, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT qty, amount FROM lines") == [(1.0, None)]
    assert _stats(out)["money_cells_unparsed"] == 1


def test_order_fields_fill_down_onto_product_rows_without_order_id(tmp_path: Path) -> None:
    columns = ["order", "date", "status", "item", "amount"]
    specs: Specs = {"sales": (columns, [
        ("O1", "2024-01-02", "completed", "tea", "100"),
        ("", "", "", "coffee", "50"),
        ("O2", "2024-01-03", "shipping", "milk", "30"),
        ("", "", "", "sugar", "10"),
    ])}
    records = [_record(columns, "sales", {"order": "order_id", "date": "ordered_at", "status": "order_status",
                                          "item": "product_name", "amount": "line_amount"})]
    report, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, date, status, amount FROM lines ORDER BY order_id, amount") == [
        ("O1", "2024-01-02", "completed", 50.0), ("O1", "2024-01-02", "completed", 100.0),
        ("O2", "2024-01-03", "shipping", 10.0), ("O2", "2024-01-03", "shipping", 30.0),
    ]
    assert report["orders"] == 2
    assert _stats(out)["product_rows_without_order"] == 0


def test_product_row_above_any_order_stays_orphan_and_is_counted(tmp_path: Path) -> None:
    columns = ["order", "date", "item", "amount"]
    specs: Specs = {"sales": (columns, [
        ("", "", "tea", "100"),
        ("O1", "2024-01-02", "coffee", "50"),
    ])}
    records = [_record(columns, "sales", {"order": "order_id", "date": "ordered_at", "item": "product_name",
                                          "amount": "line_amount"})]
    report, out = _run(tmp_path, specs, records)

    assert report["orders"] == 1
    assert report["lines"] == 1
    assert _query(out, "SELECT order_id, amount FROM lines") == [("O1", 50.0)]
    assert _stats(out)["product_rows_without_order"] == 1


def test_blank_order_rows_are_never_deduplicated(tmp_path: Path) -> None:
    columns = ["order", "item", "amount"]
    specs: Specs = {"sales": (columns, [
        ("O1", "tea", "100"),
        ("", "tea", "100"),  # identical to the row above in every cell but the order: not a duplicate
        ("", "tea", "100"),  # and not a duplicate of the row before it either
    ])}
    records = [_record(columns, "sales", {"order": "order_id", "item": "product_name", "amount": "line_amount"})]
    report, out = _run(tmp_path, specs, records)

    assert report["duplicate_lines_removed"] == 0
    assert _query(out, "SELECT COUNT(*), SUM(amount) FROM lines") == [(3, 300.0)]
    assert _stats(out)["product_rows_without_order"] == 0


def test_blank_order_rows_with_no_order_above_are_all_kept(tmp_path: Path) -> None:
    columns = ["order", "item", "amount"]
    specs: Specs = {"sales": (columns, [("", "tea", "100"), ("", "tea", "100")])}
    records = [_record(columns, "sales", {"order": "order_id", "item": "product_name", "amount": "line_amount"})]
    report, out = _run(tmp_path, specs, records)

    assert report["duplicate_lines_removed"] == 0
    assert _stats(out)["product_rows_without_order"] == 2
    assert report["lines"] == 0


def test_status_read_by_the_order_parser_needs_no_map(tmp_path: Path) -> None:
    columns = ["order", "state", "amount"]
    specs: Specs = {"sales": (columns, [
        ("O1", "da giao", "100"),
        ("O2", "dang giao hang", "50"),
        ("O3", "weird", "70"),
    ])}
    records = [_record(columns, "sales", {"order": "order_id", "state": "order_status", "amount": "line_amount"})]
    report, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id, status FROM orders ORDER BY order_id") == [
        ("O1", "completed"), ("O2", "shipping"), ("O3", None),
    ]
    assert _stats(out)["status_unmapped_values"] == 1


def test_mistyped_order_key_keeps_its_role_when_it_names_a_column(tmp_path: Path) -> None:
    columns = ["order_numbers", "date", "item", "amount"]
    specs: Specs = {"sales": (columns, [
        ("O1", "2024-01-02", "tea", "100"),
        ("O1", "2024-01-02", "coffee", "50"),
        ("O2", "2024-01-03", "tea", "30"),
    ])}
    records = [_record(columns, "sales", {"order_number": "order_id", "date": "ordered_at",
                                          "item": "product_name", "amount": "line_amount"})]
    report, out = _run(tmp_path, specs, records)

    assert report["orders"] == 2
    assert _query(out, "SELECT order_id, SUM(amount) FROM lines GROUP BY order_id ORDER BY order_id") == [
        ("O1", 150.0), ("O2", 30.0),
    ]
    assert _stats(out)["annotation_keys_unresolved"] == 0


def test_mistyped_order_key_equal_after_lookup_normalization_is_resolved(tmp_path: Path) -> None:
    columns = ["order_id", "item", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "100"), ("O2", "tea", "30")])}
    records = [_record(columns, "sales", {"Order-ID": "order_id", "item": "product_name",
                                          "amount": "line_amount"})]
    report, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id FROM orders ORDER BY order_id") == [("O1",), ("O2",)]


def test_unresolved_order_key_skips_a_table_with_order_signals_and_is_counted(tmp_path: Path) -> None:
    lost_columns = ["ref_code", "date", "item", "amount"]
    kept_columns = ["order", "item", "amount"]
    specs: Specs = {
        "sales_lost": (lost_columns, [("R1", "2024-01-02", "tea", "100")]),
        "sales_kept": (kept_columns, [("O1", "tea", "30")]),
    }
    records = [
        _record(lost_columns, "sales", {"order_reference": "order_id", "date": "ordered_at",
                                        "item": "product_name", "amount": "line_amount"}),
        _record(kept_columns, "sales", {"order": "order_id", "item": "product_name", "amount": "line_amount"}),
    ]
    report, out = _run(tmp_path, specs, records)

    stats = _stats(out)
    assert stats["tables_skipped_missing_order_id"] == 1
    assert stats["annotation_keys_unresolved"] == 1
    assert report["sales_tables"] == 1
    assert _query(out, "SELECT order_id FROM lines") == [("O1",)]


def test_unresolved_key_without_order_signals_keeps_synthetic_orders(tmp_path: Path) -> None:
    columns = ["item", "amount"]
    specs: Specs = {"sales": (columns, [("tea", "100")])}
    records = [_record(columns, "sales", {"order_reference": "order_id", "item": "product_name",
                                          "amount": "line_amount"})]
    report, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id FROM orders") == [("sales:1",)]
    assert _stats(out)["tables_skipped_missing_order_id"] == 0
    assert _stats(out)["annotation_keys_unresolved"] == 1


def test_money_cell_that_does_not_parse_is_counted(tmp_path: Path) -> None:
    columns = ["order", "item", "amount"]
    specs: Specs = {"sales": (columns, [("O1", "tea", "100"), ("O2", "coffee", "abc")])}
    records = [_record(columns, "sales", {"order": "order_id", "item": "product_name", "amount": "line_amount"})]
    report, out = _run(tmp_path, specs, records)

    stats = _stats(out)
    assert stats["money_cells_nonblank"] == 2
    assert stats["money_cells_unparsed"] == 1
    assert stats["qty_cells_nonblank"] == 0


def test_build_stats_exists_with_every_key_when_there_are_no_tables(tmp_path: Path) -> None:
    specs: Specs = {}
    report, out = _run(tmp_path, specs, [])

    assert _stats(out) == {
        "qty_cells_nonblank": 0, "qty_cells_unparsed": 0, "money_cells_nonblank": 0, "money_cells_unparsed": 0,
        "product_rows_without_order": 0, "annotation_keys_unresolved": 0,
        "tables_skipped_missing_order_id": 0, "status_unmapped_values": 0,
    }
