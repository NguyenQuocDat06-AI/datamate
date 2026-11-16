import sqlite3
from types import SimpleNamespace

from bizreason.semantic import layer


def test_other_table_mapping_short_names_is_read_as_alias(monkeypatch):
    dst = sqlite3.connect(":memory:")
    dst.create_function("lookup", 1, lambda v: None if v is None else layer.normalize_lookup(v))
    dst.execute("ATTACH ':memory:' AS source")
    dst.execute("CREATE TABLE source.t (short TEXT, full TEXT, kind TEXT)")
    dst.executemany("INSERT INTO source.t VALUES (?,?,?)",
                    [("Cap C", "Cap Type-C 1m", "phu kien"), ("Op A", "Op lung A", "phu kien"),
                     ("May X", "May X 128GB", "may moi"), ("May Y", "May Y 64GB", "may cu")])
    dst.execute("CREATE TABLE raw_lines (product_name TEXT)")
    dst.executemany("INSERT INTO raw_lines VALUES (?)", [("Cap C",), ("Op A",), ("May X",), ("Khac",)])
    seen = {}
    monkeypatch.setattr(layer, "_load_aliases", lambda d, t, roles: seen.setdefault("alias", roles))
    monkeypatch.setattr(layer, "_load_product_attrs", lambda d, t, kind, roles: seen.setdefault("attrs", roles))
    table = SimpleNamespace(name="t")
    note = {"kind": "other", "roles": {"short": "other", "full": "product_name", "kind": "other"}}
    assert layer._promote_alias_tables(dst, [(table, note)]) == 1
    assert seen["alias"] == {"alias_key": "short", "product_name": "full"}
    assert seen["attrs"] == {"product_name": "full", "product_kind": "kind"}


def test_other_table_unrelated_to_sales_is_left_alone():
    dst = sqlite3.connect(":memory:")
    dst.create_function("lookup", 1, lambda v: None if v is None else layer.normalize_lookup(v))
    dst.execute("ATTACH ':memory:' AS source")
    dst.execute("CREATE TABLE source.t (a TEXT, full TEXT)")
    dst.executemany("INSERT INTO source.t VALUES (?,?)", [("x1", "A"), ("x2", "B"), ("x3", "C")])
    dst.execute("CREATE TABLE raw_lines (product_name TEXT)")
    dst.execute("INSERT INTO raw_lines VALUES ('A')")
    note = {"kind": "other", "roles": {"a": "other", "full": "product_name"}}
    assert layer._promote_alias_tables(dst, [(SimpleNamespace(name="t"), note)]) == 0
