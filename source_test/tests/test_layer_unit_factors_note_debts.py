"""Per-row unit conversions of a catalog cell, README examples that are not rules, and credit orders whose debt state
is written in a free-text note (synthetic data only)."""
import sqlite3
from datetime import date

import pytest

from bizreason.contracts import BusinessContext, ColumnMeta, SourceRef, TableMeta
from bizreason.semantic import debt_sources, layer, rulebook_params
from bizreason.semantic.compiler import Compiler, Intent, Unsupported
from bizreason.semantic.layer import _row_factor_statements
from bizreason.temporal import TimeWindow
from bizreason.utils import PathPolicy

SALES = ["order", "day", "item", "unit", "qty", "status", "amount"]
SALES_ROLES = {"order": "order_id", "day": "ordered_at", "item": "product_name", "unit": "unit", "qty": "qty",
               "status": "order_status", "amount": "line_amount"}


def _build(tmp_path, specs, records, readme="", conventions=None):
    src = tmp_path / "in.sqlite"
    db = sqlite3.connect(src)
    for name, (cols, rows) in specs.items():
        db.execute(f"CREATE TABLE {name} (" + ",".join(f'"{c}" TEXT' for c in cols) + ",__source_row INTEGER,__raw_json TEXT)")
        for i, row in enumerate(rows, 1):
            db.execute(f"INSERT INTO {name} VALUES (" + ",".join("?" * (len(cols) + 2)) + ")", (*row, i, "{}"))
    db.commit()
    db.close()
    ctx = BusinessContext(business_id="s", revision="r", database=src, readme=readme, manifest={}, tables=[
        TableMeta(name=n, sources=[SourceRef(path=n + ".csv")],
                  columns=[ColumnMeta(name=c, original_name=c, dtype="text") for c in cols],
                  row_count=len(rows), raw_row_count=len(rows)) for n, (cols, rows) in specs.items()])
    out = tmp_path / "c.sqlite"
    rulebook_params.set_rulebook_text(None)
    with PathPolicy(mode="competition", allowed_roots=[tmp_path], forbidden_roots=[]):
        report = layer.build(ctx, records, out, conventions=conventions or [])
    return sqlite3.connect(out), report


# ---- per-row conversion cells -------------------------------------------------------------------------------------

CATALOG = ["item", "dvt", "conv"]
CATALOG_ROLES = {"item": "product_name", "dvt": "unit", "conv": "conversion_factor"}


def _units_shop(tmp_path, catalog, sold, readme=""):
    specs = {"sales": (SALES, [(f"o{i}", "2024-01-12", n, u, "2", "completed", "10") for i, (n, u) in enumerate(sold, 1)]),
             "catalog": (CATALOG, catalog)}
    records = [{"columns": SALES, "kind": "sales", "roles": SALES_ROLES},
               {"columns": CATALOG, "kind": "product_catalog", "roles": CATALOG_ROLES}]
    return _build(tmp_path, specs, records, readme)[0]


def _qb(db, name, unit):
    return db.execute("SELECT qty_base FROM lines WHERE product_name=? AND unit=?", [name, unit]).fetchone()[0]


def test_row_cell_gives_the_row_its_own_factor(tmp_path):
    db = _units_shop(tmp_path, [("Bim A", "bich", "1 thung = 6 bich"), ("Bim B", "bich", "1 thung = 4 bich")],
                     [("Bim A", "thung"), ("Bim B", "thung")])
    assert _qb(db, "Bim A", "thung") == 12 and _qb(db, "Bim B", "thung") == 8


def test_cell_with_several_units(tmp_path):
    db = _units_shop(tmp_path, [("Khan", "goi", "1 loc = 4 goi; 1 thung = 24 goi")], [("Khan", "loc"), ("Khan", "thung"), ("Khan", "goi")])
    assert (_qb(db, "Khan", "loc"), _qb(db, "Khan", "thung"), _qb(db, "Khan", "goi")) == (8, 48, 2)


def test_cell_units_chain_to_the_base_unit(tmp_path):
    db = _units_shop(tmp_path, [("Khan", "goi", "1 thung = 6 loc, 1 loc = 4 goi")], [("Khan", "thung")])
    assert _qb(db, "Khan", "thung") == 48


def test_statement_that_ends_in_another_unit_is_not_used(tmp_path):
    db = _units_shop(tmp_path, [("Sua", "hop", "1 thung = 12 chai")], [("Sua", "thung")])
    assert _qb(db, "Sua", "thung") is None  # the row counts in hop: the statement says nothing about it


def test_row_without_a_cell_keeps_its_unit_as_base(tmp_path):
    db = _units_shop(tmp_path, [("Sua", "hop", None), ("Bim", "bich", "1 thung = 6 bich")], [("Sua", "hop"), ("Bim", "thung")])
    assert _qb(db, "Sua", "hop") == 2 and _qb(db, "Bim", "thung") == 12


def test_readme_example_is_not_a_shop_wide_rule(tmp_path):
    readme = '- **Quy doi**: quy doi don vi lon ra DVT, vd "1 thung = 4 bich"; trong neu chi ban theo DVT'
    db = _units_shop(tmp_path, [("Bim A", "bich", "1 thung = 6 bich"), ("Sua", "hop", None), ("Mi", "goi", None)],
                     [("Bim A", "thung"), ("Sua", "thung"), ("Mi", "thung")], readme)
    assert _qb(db, "Bim A", "thung") == 12  # the row's own factor, not the example's 4
    assert _qb(db, "Sua", "thung") is None and _qb(db, "Mi", "thung") is None


def test_readme_example_cue_alone_makes_a_line_not_a_rule(tmp_path):
    db = _units_shop(tmp_path, [("Mi", "goi", None)], [("Mi", "thung")], "Ví dụ: 1 thung = 30 goi")
    assert _qb(db, "Mi", "thung") is None


def test_catalog_unit_is_never_replaced_by_a_readme_statement(tmp_path):
    db = _units_shop(tmp_path, [("Sua", "hop", None), ("Bim", "bich", None)], [("Sua", "thung"), ("Bim", "thung")],
                     "Quy ước: 1 thung = 4 bich")
    assert _qb(db, "Bim", "thung") == 8  # a product counted in bich and sold by thung takes a rule that says so
    assert _qb(db, "Sua", "thung") is None


def test_readme_column_description_is_not_a_rule(tmp_path):
    readme = "- **conv**: don vi lon ra DVT, 1 thung = 4 bich\n"
    db = _units_shop(tmp_path, [("Bim", "bich", None)], [("Bim", "thung")], readme)
    assert _qb(db, "Bim", "thung") is None


def test_row_factor_wins_over_a_readme_rule(tmp_path):
    db = _units_shop(tmp_path, [("Bim", "bich", "1 thung = 6 bich")], [("Bim", "thung")], "Quy ước: 1 thung = 4 bich")
    assert _qb(db, "Bim", "thung") == 12


def test_conversion_table_with_a_real_large_unit_is_unchanged(tmp_path):
    cols = ["item", "large", "factor", "base"]
    specs = {"sales": (SALES, [("o1", "2024-01-12", "Xi mang", "bao", "2", "completed", "10")]),
             "packing": (cols, [("Xi mang", "bao", "50", "kg")])}
    records = [{"columns": SALES, "kind": "sales", "roles": SALES_ROLES},
               {"columns": cols, "kind": "unit_conversion", "roles": {
                   "item": "product_name", "large": "large_unit", "base": "base_unit", "factor": "conversion_factor"}}]
    db, _ = _build(tmp_path, specs, records)
    assert _qb(db, "Xi mang", "bao") == 100


@pytest.mark.parametrize("cell, expected", [
    ("1 thung = 6 bich", [("thung", 6.0, "bich")]),
    ("1 loc = 4 goi; 1 thung = 24 goi", [("loc", 4.0, "goi"), ("thung", 24.0, "goi")]),
    ("1 thùng bằng 12 hộp", [("thung", 12.0, "hop")]),
    ("1 thung = 24.000 goi", []),  # a grouped amount is money, not a count
    ("1 thung = 5 dong", []),  # money words are not units
    ("24", []), (None, []), ("thùng", []),
])
def test_row_factor_statements(cell, expected):
    assert _row_factor_statements(cell) == expected


# ---- debt notes -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("note, total, owed", [
    ("nợ 725 nghìn", 725000, 725000),
    ("ghi sổ 443 nghìn", 717000, 443000),
    ("khách đưa trước 87k, còn thiếu 73 nghìn", 160000, 73000),
    ("đưa trước 366k, còn thiếu 853 nghìn", 1219000, 853000),
    ("ghi nợ 1tr868, đã trả đủ", 2000000, 0),
    ("nợ cả đơn", 337500, 337500),
    ("đưa trước 236k, còn lại ghi sổ", 286000, 50000),
    ("trả 259k, nợ 449.500đ", 708500, 449500),
    ("ghi sổ 39.668đ", 359000, 39668),
    ("nợ 1tr120", 1130000, 1120000),
    ("nợ 12tr", 20000000, 12000000),
    ("khách trả hết rồi", 100000, 0),
])
def test_debt_note_patterns(note, total, owed):
    assert debt_sources.read_debt_note(note, total) == owed


@pytest.mark.parametrize("note, total", [
    ("nợ", 100000),  # no figure
    ("nợ 900k", 100000),  # more than the order
    ("đưa trước 50k, còn thiếu 20k", 100000),  # hand-over and rest do not add up
    ("nợ 5kg", 100000),
    ("nợ 300", 1000000),  # a bare small number has no unit
    ("hello", 100000), (None, 100000), ("", 100000),
])
def test_debt_note_unreadable(note, total):
    assert debt_sources.read_debt_note(note, total) is None


ORDERS = ["order", "day", "item", "unit", "qty", "status", "amount", "total", "method", "note"]
ORDER_ROLES = {**SALES_ROLES, "total": "order_total_due", "method": "payment_method", "note": "note"}
DEBT_CONVENTION = [{"type": "debt_state_column", "column": "note"}]


def _debt_shop(tmp_path, rows, conventions=DEBT_CONVENTION):
    data = [(oid, "2024-01-12", "Hang", "cai", "1", "completed", total, total, method, note)
            for oid, total, method, note in rows]
    specs = {"sales": (ORDERS, data)}
    records = [{"columns": ORDERS, "kind": "sales", "roles": ORDER_ROLES}]
    return _build(tmp_path, specs, records, "", conventions)


def _outstanding(db):
    return dict(db.execute("SELECT order_id, outstanding FROM orders"))


def test_notes_set_the_debt_state_of_credit_orders(tmp_path):
    db, _ = _debt_shop(tmp_path, [("o1", "100000", "debt", "nợ 40k"), ("o2", "200000", "debt", "nợ, đã trả đủ"),
                                  ("o3", "300000", "debt", "nợ cả đơn"), ("o4", "50000", "cash", None)])
    out = _outstanding(db)
    assert out["o1"] == 40000 and out["o2"] == 0 and out["o3"] == 300000 and out["o4"] is None


def test_unreadable_note_leaves_the_state_unknown(tmp_path):
    db, _ = _debt_shop(tmp_path, [("o1", "100000", "debt", "nợ 40k"), ("o2", "100000", "debt", "xem lại sau"),
                                  ("o3", "100000", "debt", "nợ 900k")])
    out = _outstanding(db)
    assert out["o1"] == 40000 and out["o2"] is None and out["o3"] is None


def test_a_note_on_an_order_that_is_not_credit_is_not_read(tmp_path):
    db, _ = _debt_shop(tmp_path, [("o1", "100000", "debt", "nợ 40k"), ("o2", "100000", "cash", "nợ 40k")])
    assert _outstanding(db)["o2"] is None


def test_model_reading_of_a_note_still_wins(tmp_path):
    conv = [{"type": "debt_state_column", "column": "note", "values": {"nợ 40k": [True, 10000]}}]
    db, _ = _debt_shop(tmp_path, [("o1", "100000", "debt", "nợ 40k"), ("o2", "100000", "debt", "nợ 70k")], conv)
    out = _outstanding(db)
    assert out["o1"] == 10000 and out["o2"] == 70000


WINDOW = TimeWindow(start=date(2024, 1, 1), end_exclusive=date(2024, 2, 1))


def _owed(db, measure="debt_outstanding"):
    db.row_factory = None
    return Compiler(db).answer(Intent(measure=measure), [WINDOW]).value


def test_debt_is_summed_from_the_notes(tmp_path):
    db, _ = _debt_shop(tmp_path, [("o1", "100000", "debt", "nợ 40k"), ("o2", "200000", "debt", "đưa trước 50k, còn thiếu 150k"),
                                  ("o3", "300000", "debt", "nợ, đã trả đủ")])
    assert _owed(db) == 190000


def test_debt_abstains_when_a_credit_order_has_an_unreadable_state(tmp_path):
    db, _ = _debt_shop(tmp_path, [("o1", "100000", "debt", "nợ 40k"), ("o2", "200000", "debt", "để sau")])
    with pytest.raises(Unsupported):
        _owed(db)
    with pytest.raises(Unsupported):
        _owed(db, "debtor_count")


def test_cancelled_unreadable_order_does_not_block(tmp_path):
    db, _ = _debt_shop(tmp_path, [("o1", "100000", "debt", "nợ 40k")])
    db.execute("INSERT INTO orders (order_id, date, status, payment_method, total_due, revenue) "
               "VALUES ('x', '2024-01-20', 'cancelled', 'debt', 100, 100)")
    assert _owed(db) == 40000


def test_shop_with_payments_table_is_unaffected(tmp_path):
    pay = ["order", "day", "amount"]
    data = [("o1", "2024-01-12", "Hang", "cai", "1", "completed", "100", "100", "debt", "để sau")]
    specs = {"sales": (ORDERS, data), "pay": (pay, [("o1", "2024-01-15", "30")])}
    records = [{"columns": ORDERS, "kind": "sales", "roles": ORDER_ROLES},
               {"columns": pay, "kind": "payment", "roles": {"order": "order_id", "day": "paid_at", "amount": "paid_amount"}}]
    db, _ = _build(tmp_path, specs, records, "", DEBT_CONVENTION)
    assert _owed(db) == 70
