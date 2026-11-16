"""Canonical loaders must not discard unfamiliar attributes of recognized source kinds."""

import json
import sqlite3

import pytest

from bizreason.contracts import BusinessContext, ColumnMeta, SourceRef, TableMeta
from bizreason.semantic import layer


def _build(tmp_path, columns, rows, kind, roles):
    source = tmp_path / "source.sqlite"
    db = sqlite3.connect(source)
    specs = {
        "sales": (["order_key", "item", "amount"], [("R1", "sample", "120")]),
        "reference": (columns, rows),
    }
    for name, (names, values) in specs.items():
        definitions = ", ".join(f'"{c}" TEXT' for c in names)
        db.execute(f'CREATE TABLE "{name}" ({definitions}, __source_row INTEGER, __raw_json TEXT)')
        marks = ",".join("?" for _ in range(len(names) + 2))
        for index, row in enumerate(values, 1):
            db.execute(f'INSERT INTO "{name}" VALUES ({marks})', (*row, index, json.dumps(row)))
    db.commit()
    db.close()
    tables = [TableMeta(
        name=name,
        columns=[ColumnMeta(name=c, original_name=c, dtype="text") for c in names],
        sources=[SourceRef(path=f"{name}.csv")],
        row_count=len(values), raw_row_count=len(values),
    ) for name, (names, values) in specs.items()]
    context = BusinessContext(
        business_id="synthetic", revision="r1", database=source, readme="", manifest={}, tables=tables,
    )
    records = [
        {"kind": "sales", "columns": specs["sales"][0], "roles": {
            "order_key": "order_id", "item": "product_name", "amount": "line_amount",
        }},
        {"kind": kind, "columns": columns, "roles": roles},
    ]
    out = tmp_path / "layer.sqlite"
    report = layer.build(context, records, out)
    return sqlite3.connect(out), report


@pytest.mark.parametrize("kind,columns,rows,roles,extra,expected", [
    ("employee_catalog", ["staff", "commission"], [("Ada", "0.15"), ("Ben", "0.20")],
     {"staff": "employee_name", "commission": "other"}, "commission", [0.15, 0.20]),
    ("product_catalog", ["sku", "label", "warranty_days"], [("P1", "sample", "90")],
     {"sku": "product_id", "label": "product_name", "warranty_days": "other"}, "warranty_days", [90]),
    ("customer_catalog", ["client", "segment", "credit_limit"], [("C1", "retail", "850")],
     {"client": "customer_id", "segment": "customer_group", "credit_limit": "other"}, "credit_limit", [850]),
    ("expense", ["day", "cost", "reimbursed"], [("2024-08-01", "50", "30")],
     {"day": "expense_date", "cost": "expense_amount", "reimbursed": "other"}, "reimbursed", [30]),
    ("price_history", ["sku", "day", "before", "after", "rebate"],
     [("P1", "2024-08-01", "10", "15", "2")],
     {"sku": "product_id", "day": "price_changed_at", "before": "old_price", "after": "new_price",
      "rebate": "other"}, "rebate", [2]),
])
def test_loaded_reference_table_keeps_unconsumed_columns(
    tmp_path, kind, columns, rows, roles, extra, expected
):
    db, report = _build(tmp_path, columns, rows, kind, roles)
    try:
        assert "src_reference" in report["src_tables"]
        assert db.execute(f'SELECT "{extra}" FROM src_reference').fetchall() == [
            (str(row[columns.index(extra)]),) for row in rows
        ]
        # Numeric twins are usable by the validated query plan without model-side parsing.
        assert db.execute(f'SELECT "xn_{extra}" FROM src_reference').fetchall() == [
            (value,) for value in expected
        ]
        assert db.execute(
            "SELECT source_table, source_header FROM column_catalog "
            "WHERE table_name='src_reference' AND column_name=?", (extra,)
        ).fetchall() == [("reference", extra)]
        assert db.execute("SELECT COUNT(*), SUM(revenue) FROM orders").fetchone() == (1, 120)
        assert db.execute("SELECT COUNT(*), SUM(amount) FROM lines").fetchone() == (1, 120)
        assert "src_sales" not in report["src_tables"]
    finally:
        db.close()


def test_source_copy_keeps_keys_and_date_twins(tmp_path):
    db, _ = _build(
        tmp_path, ["staff", "hired", "commission"], [("Ada", "2024-08-01", "0.15")],
        "employee_catalog", {"staff": "employee_name", "hired": "other", "commission": "other"},
    )
    try:
        assert db.execute("SELECT staff, hired, xd_hired FROM src_reference").fetchall() == [
            ("Ada", "2024-08-01", "2024-08-01")
        ]
        assert db.execute(
            "SELECT role, value_type FROM column_catalog "
            "WHERE table_name='src_reference' AND column_name='staff'"
        ).fetchone()[0] == "employee_name"
    finally:
        db.close()
