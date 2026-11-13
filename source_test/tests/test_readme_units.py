import sqlite3

from bizreason.semantic.layer import _load_readme_unit_factors


def _db():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE raw_lines (product_name TEXT)")
    con.executemany("INSERT INTO raw_lines VALUES (?)", [("Ống nhựa A50 cây 4m",), ("Ống nhựa A90 cây 4m",), ("Keo",)])
    con.execute("CREATE TEMP TABLE unit_factors (product_id, name_key, large_key, factor)")
    con.execute("CREATE TEMP TABLE product_facts (product_id, name_key, category, base_unit, unit_cost)")
    return con


def test_readme_factor_for_named_products():
    con = _db()
    readme = "- **Hàng theo bó**: Ống nhựa A50 cây 4m, Ống nhựa A90 cây 4m bán theo bó, 1 bó = 10 cây.\n- Khác: 2 hộp"
    assert _load_readme_unit_factors(con, readme) == 2
    rows = sorted(con.execute("SELECT name_key, large_key, factor FROM temp.unit_factors").fetchall())
    assert ("ong nhua a50 cay 4m", "bo", 10.0) in rows and ("ong nhua a50 cay 4m", "cay", 1.0) in rows


def test_readme_factor_without_named_product_adds_nothing():
    con = _db()
    assert _load_readme_unit_factors(con, "Quy ước: 1 thùng = 24 lon") == 0
