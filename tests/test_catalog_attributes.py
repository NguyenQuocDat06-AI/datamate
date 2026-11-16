"""Catalog attributes must combine sheet shards without inventing a conflicting fact."""
import sqlite3

from bizreason.contracts import ColumnMeta, TableMeta
from bizreason.semantic.layer import _attach_catalog_attributes
from bizreason.utils import normalize_lookup


def _db():
    db = sqlite3.connect(":memory:")
    db.create_function("lookup", 1, lambda v: normalize_lookup(str(v)) if v is not None else None)
    db.execute("ATTACH DATABASE ':memory:' AS source")
    db.execute("CREATE TABLE lines (product_id TEXT, product_name TEXT)")
    db.executemany("INSERT INTO lines VALUES (?,?)", [("p1", "Road Bike"), ("p2", "City Bike")])
    db.execute("CREATE TABLE column_catalog (table_name, column_name, source_table, source_header, "
               "role, value_type, distinct_count, sample_values, join_key_to)")
    return db


def _catalog(db, table, rows, *, by_id=False):
    key = "sku" if by_id else "item"
    db.execute(f'CREATE TABLE source."{table}" ("{key}" TEXT, brand TEXT)')
    db.executemany(f'INSERT INTO source."{table}" VALUES (?,?)', rows)
    meta = TableMeta(name=table, sources=[], columns=[
        ColumnMeta(name=key, original_name=key, dtype="text"),
        ColumnMeta(name="brand", original_name="Brand", dtype="text")],
        row_count=len(rows), raw_row_count=len(rows))
    return meta, {"kind": "product_catalog", "roles": {key: "product_id" if by_id else "product_name"}}


def test_an_unmatched_first_sheet_does_not_create_a_duplicate_column():
    db = _db()
    notes = [_catalog(db, "old_catalog", [("Unrelated", "Heron")]),
             _catalog(db, "live_catalog", [("Road Bike", "Falcon")])]
    assert _attach_catalog_attributes(db, notes) == ["x_cat_brand"]
    assert db.execute("SELECT x_cat_brand FROM lines").fetchall() == [("Falcon",), (None,)]


def test_catalog_shards_fill_all_products_with_one_metadata_row():
    db = _db()
    notes = [_catalog(db, "catalog_a", [("Road Bike", "Falcon")]),
             _catalog(db, "catalog_b", [("City Bike", "Heron")])]
    assert _attach_catalog_attributes(db, notes) == ["x_cat_brand"]
    assert db.execute("SELECT x_cat_brand FROM lines").fetchall() == [("Falcon",), ("Heron",)]
    assert db.execute("SELECT COUNT(*) FROM column_catalog WHERE column_name='x_cat_brand'").fetchone() == (1,)


def test_conflicting_catalog_values_are_left_unknown_independent_of_sheet_order():
    for reverse in (False, True):
        db = _db()
        notes = [_catalog(db, "catalog_a", [("Road Bike", "Falcon"), ("City Bike", "Heron")]),
                 _catalog(db, "catalog_b", [("Road Bike", "Eagle")])]
        _attach_catalog_attributes(db, notes[::-1] if reverse else notes)
        assert db.execute("SELECT x_cat_brand FROM lines").fetchall() == [(None,), ("Heron",)]


def test_duplicates_with_the_same_normalized_attribute_are_safe():
    db = _db()
    notes = [_catalog(db, "catalog_a", [("Road Bike", "Falcon"), ("Road Bike", "falcon")])]
    assert _attach_catalog_attributes(db, notes) == ["x_cat_brand"]
    assert db.execute("SELECT x_cat_brand FROM lines WHERE product_id='p1'").fetchone() == ("Falcon",)


def test_id_only_catalog_is_skipped_when_sale_lines_have_no_product_ids():
    db = _db()
    db.execute("UPDATE lines SET product_id=NULL")
    notes = [_catalog(db, "catalog_a", [("p1", "Falcon")], by_id=True)]
    assert _attach_catalog_attributes(db, notes) == []
    assert "x_cat_brand" not in {r[1] for r in db.execute("PRAGMA table_info(lines)")}


def test_catalog_id_matching_does_not_lose_shards_or_multiply_sale_rows():
    db = _db()
    notes = [_catalog(db, "catalog_a", [("p1", "Falcon"), ("p1", "Falcon")], by_id=True),
             _catalog(db, "catalog_b", [("p2", "Heron")], by_id=True)]
    assert _attach_catalog_attributes(db, notes) == ["x_cat_brand"]
    assert db.execute("SELECT product_id,x_cat_brand FROM lines").fetchall() == [
        ("p1", "Falcon"), ("p2", "Heron")]
