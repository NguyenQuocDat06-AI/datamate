"""Unit conversions written in words (README, rulebook) and the guard on identity conversion (synthetic data)."""
import sqlite3

import pytest

from bizreason.contracts import BusinessContext, ColumnMeta, SourceRef, TableMeta
from bizreason.semantic import layer, rulebook_params
from bizreason.semantic.layer import _unit_statements
from bizreason.utils import PathPolicy


def _lines(items):
    return [(f"o{i}", "2024-01-12", n, u, "2", "completed", "10") for i, (n, u) in enumerate(items, 1)]


def _build(tmp_path, readme, sales, conv=None, conventions=None, rulebook=None, product_attrs=None):
    src = tmp_path / "in.sqlite"
    db = sqlite3.connect(src)
    specs = {"sales": (["order", "day", "item", "unit", "qty", "status", "amount"], sales)}
    if conv:
        specs["packing"] = (["item", "large", "factor", "base"], conv)
    if product_attrs:
        specs["catalog"] = (["item", "base"], product_attrs)
    for name, (cols, rows) in specs.items():
        db.execute(f"CREATE TABLE {name} (" + ",".join(f'"{c}" TEXT' for c in cols) + ",__source_row INTEGER,__raw_json TEXT)")
        for i, r in enumerate(rows, 1):
            db.execute(f"INSERT INTO {name} VALUES (" + ",".join("?" * (len(cols) + 2)) + ")", (*r, i, "{}"))
    db.commit()
    db.close()
    ctx = BusinessContext(business_id="s", revision="r", database=src, readme=readme, manifest={}, tables=[
        TableMeta(name=n, sources=[SourceRef(path=n + ".csv")],
                  columns=[ColumnMeta(name=c, original_name=c, dtype="text") for c in cols],
                  row_count=len(rows), raw_row_count=len(rows)) for n, (cols, rows) in specs.items()])
    recs = [{"columns": specs["sales"][0], "kind": "sales", "roles": {
        "order": "order_id", "day": "ordered_at", "item": "product_name", "unit": "unit", "qty": "qty",
        "status": "order_status", "amount": "line_amount"}}]
    if conv:
        recs.append({"columns": specs["packing"][0], "kind": "unit_conversion", "roles": {
            "item": "product_name", "large": "large_unit", "base": "base_unit", "factor": "conversion_factor"}})
    if product_attrs:
        recs.append({"columns": specs["catalog"][0], "kind": "product_catalog", "roles": {
            "item": "product_name", "base": "base_unit"}})
    out = tmp_path / "c.sqlite"
    rulebook_params.set_rulebook_text(rulebook)
    try:
        with PathPolicy(mode="competition", allowed_roots=[tmp_path], forbidden_roots=[]):
            report = layer.build(ctx, recs, out, conventions=conventions or [])
    finally:
        rulebook_params.set_rulebook_text(None)
    return sqlite3.connect(out), report


def _qb(db, name, unit):
    return db.execute("SELECT qty_base FROM lines WHERE product_name=? AND unit=?", [name, unit]).fetchone()[0]


def test_conversion_table_plain_number(tmp_path):
    db, _ = _build(tmp_path, "", _lines([("Hang A", "thùng")]), [("Hang A", "thùng", "24", "gói")])
    assert _qb(db, "Hang A", "thùng") == 48


def test_conversion_table_number_with_unit(tmp_path):
    db, _ = _build(tmp_path, "", _lines([("Hang A", "thùng")]), [("Hang A", "thùng", "24 gói", "gói")])
    assert _qb(db, "Hang A", "thùng") == 48


def test_readme_names_product(tmp_path):
    db, _ = _build(tmp_path, "- Hang Alpha bán theo thùng, 1 thùng = 24 gói",
                   _lines([("Hang Alpha", "thùng"), ("Hang Alpha", "gói")]))
    assert _qb(db, "Hang Alpha", "thùng") == 48 and _qb(db, "Hang Alpha", "gói") == 2


def test_readme_without_product_name(tmp_path):
    db, rep = _build(tmp_path, "- Hàng bán theo chục: 1 chục = 12 quả", _lines([("Trung X", "chục"), ("Trung X", "quả")]))
    assert _qb(db, "Trung X", "chục") == 24 and _qb(db, "Trung X", "quả") == 2
    assert rep["readme_unit_factors"] == 1


def test_readme_each_without_equals(tmp_path):
    db, _ = _build(tmp_path, "- Hang Alpha: mỗi thùng 24 gói", _lines([("Hang Alpha", "thùng"), ("Hang Alpha", "gói")]))
    assert _qb(db, "Hang Alpha", "thùng") == 48


def test_directive_without_product(tmp_path):
    conv = [{"type": "unit_factor", "products": [], "large_unit": "chục", "factor": 12, "base_unit": "quả"}]
    db, _ = _build(tmp_path, "", _lines([("Trung X", "chục"), ("Trung X", "quả")]), conventions=conv)
    assert _qb(db, "Trung X", "chục") == 24


def test_rulebook_statement_reaches_layer(tmp_path):
    db, rep = _build(tmp_path, "", _lines([("Trung X", "chục"), ("Trung X", "quả")]), rulebook="# Quy đổi\n- 1 chục = 12 quả.")
    assert _qb(db, "Trung X", "chục") == 24 and _qb(db, "Trung X", "quả") == 2
    assert rep["rulebook_unit_factors"] == 1


def test_rulebook_markdown_table_row(tmp_path):
    db, _ = _build(tmp_path, "", _lines([("Trung X", "chục"), ("Trung X", "quả")]), rulebook="| Đơn vị | Quy đổi |\n|---|---|\n| 1 chục | 12 quả |")
    assert _qb(db, "Trung X", "chục") == 24


def test_shop_table_beats_rulebook_and_readme(tmp_path):
    db, rep = _build(tmp_path, "1 chục = 11 quả", _lines([("Trung X", "chục")]), [("Trung X", "chục", "10", "quả")],
                     rulebook="1 chục = 12 quả")
    assert _qb(db, "Trung X", "chục") == 20
    assert rep["rulebook_unit_factors"] == 0


def test_readme_beats_rulebook(tmp_path):
    db, _ = _build(tmp_path, "1 chục = 11 quả", _lines([("Trung X", "chục")]), rulebook="1 chục = 12 quả")
    assert _qb(db, "Trung X", "chục") == 22


def test_contradicting_statements_of_one_source_give_nothing(tmp_path):
    db, rep = _build(tmp_path, "1 chục = 12 quả\n1 chục = 10 quả", _lines([("Trung X", "chục"), ("Trung X", "quả")]))
    assert _qb(db, "Trung X", "chục") is None
    assert rep["readme_unit_factors"] == 0


def test_not_applied_when_base_unit_differs(tmp_path):
    """"1 thùng = 24 gói" must not reach a product whose base unit is lon (sold only by the thùng)."""
    db, _ = _build(tmp_path, "- 1 thùng = 24 gói", _lines([("Nuoc Y", "thùng")]),
                   product_attrs=[("Nuoc Y", "lon")])
    assert _qb(db, "Nuoc Y", "thùng") is None


def test_money_statement_makes_no_factor(tmp_path):
    db, rep = _build(tmp_path, "- 1 ly = 25.000 đ\n- 1 ly = 25.000đ", _lines([("Tra Z", "ly"), ("Tra Z", "chai")]))
    assert rep["readme_unit_factors"] == 0
    assert _unit_statements("1 ly = 25 đồng\n1 ly = 25.000 cái") == []


def test_product_name_with_spec_is_not_a_statement():
    assert _unit_statements("Mì thùng 30 gói\nhộp 25 vỉ x 4 viên\nBán 3 thùng 30 gói") == []
    assert [(l, f, b) for _, l, f, b in _unit_statements("Quy đổi: thùng 30 gói")] == [("thùng", 30.0, "gói")]
    assert [(l, f, b) for _, l, f, b in _unit_statements("mỗi bó có 2,5 kg")] == [("bó", 2.5, "kg")]


def test_two_units_without_factor_stay_null(tmp_path):
    """A product sold by chục and by quả with no conversion anywhere: neither unit is known to be the base, so both
    kinds of line stay NULL (abstain) rather than adding dozens to eggs."""
    db, _ = _build(tmp_path, "", _lines([("Trung X", "chục"), ("Trung X", "quả")]))
    assert _qb(db, "Trung X", "chục") is None and _qb(db, "Trung X", "quả") is None


def test_single_unit_product_keeps_identity(tmp_path):
    db, _ = _build(tmp_path, "", _lines([("Hang B", "cái"), ("Hang B", "Cái")]))
    assert _qb(db, "Hang B", "cái") == 2 and _qb(db, "Hang B", "Cái") == 2


def test_line_without_unit_unchanged(tmp_path):
    db, _ = _build(tmp_path, "", _lines([("Hang B", "")]))
    assert db.execute("SELECT qty_base FROM lines WHERE product_name='Hang B'").fetchone()[0] is None
