"""Customer names stay as written in the data (rulebook section 9) and one phone number is one customer key."""
from __future__ import annotations

import sqlite3

import pytest

from bizreason.contracts import ColumnMeta, TableMeta
from bizreason.data.identities import canonicalize_customers, split_embedded_phone


def _table(name: str, with_code: bool) -> TableMeta:
    columns = [ColumnMeta(name="oid", original_name="oid", dtype="TEXT", semantic_role="order_id")]
    if with_code:
        columns.append(ColumnMeta(name="cid", original_name="cid", dtype="TEXT", semantic_role="customer_id"))
    columns.append(ColumnMeta(name="cname", original_name="cname", dtype="TEXT", semantic_role="customer_name"))
    return TableMeta(name=name, sources=[], columns=columns, row_count=0, raw_row_count=0)


def _dimension() -> TableMeta:
    columns = [ColumnMeta(name="cid", original_name="cid", dtype="TEXT", semantic_role="customer_id"),
               ColumnMeta(name="cname", original_name="cname", dtype="TEXT", semantic_role="customer_name")]
    return TableMeta(name="dim", sources=[], columns=columns, row_count=0, raw_row_count=0)


def _names_without_code(names: list[str]) -> list[str]:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE orders (oid TEXT, cname TEXT)")
    con.executemany("INSERT INTO orders VALUES (?, ?)", [(str(i), n) for i, n in enumerate(names)])
    canonicalize_customers(con, [_table("orders", False)])
    return [r[0] for r in con.execute("SELECT cname FROM orders ORDER BY CAST(oid AS INTEGER)")]


def test_spaced_and_joined_phone_are_one_spelling():
    out = _names_without_code(["Lê Văn An 0353 838 285", "Lê Văn An 0353838285", "Lê Văn An 0353838285"])
    assert len(set(out)) == 1
    assert out[0] == "Lê Văn An 0353838285"  # the more frequent written form


def test_dash_and_bracket_forms_unify():
    out = _names_without_code(["Trần Thị Hoa - 0912 345 678", "Trần Thị Hoa (0912345678)", "Trần Thị Hoa - 0912 345 678"])
    assert len(set(out)) == 1


def test_international_prefix_is_same_number():
    out = _names_without_code(["Phạm Văn Bình 0912345678", "Phạm Văn Bình +84912345678"])
    assert len(set(out)) == 1


def test_diacritic_variants_of_same_name_and_phone_unify():
    out = _names_without_code(["Ngô Hồng Duy 0912345678", "Ngo Hong Duy 0912 345 678"])
    assert len(set(out)) == 1
    assert out[0].startswith("Ngô")


def test_different_names_sharing_a_number_stay_apart():
    names = ["Lê Văn An 0353838285", "Nguyễn Thị Mai 0353 838 285"]
    assert _names_without_code(names) == names


def test_same_name_different_numbers_stay_apart():
    names = ["Lê Văn An 0353838285", "Lê Văn An 0912345678"]
    assert _names_without_code(names) == names


def test_names_without_phone_are_unchanged():
    names = ["Lê Văn An", "Khách lẻ", "Trần Hoa", "LÊ VĂN AN"]
    assert _names_without_code(names) == names


def test_number_like_but_not_a_phone_is_unchanged():
    names = ["Cửa hàng số 12345", "Lê Văn An 123456"]
    assert _names_without_code(names) == names


def test_walk_in_and_null_names_are_untouched():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE orders (oid TEXT, cname TEXT)")
    con.executemany("INSERT INTO orders VALUES (?, ?)", [("1", None), ("2", "Khách lẻ")])
    canonicalize_customers(con, [_table("orders", False)])
    assert [r[0] for r in con.execute("SELECT cname FROM orders ORDER BY oid")] == [None, "Khách lẻ"]


def test_unification_spans_tables_without_a_code():
    con = sqlite3.connect(":memory:")
    for name, rows in (("a", ["Lê Văn An 0353 838 285"]), ("b", ["Lê Văn An 0353838285", "Lê Văn An 0353838285"])):
        con.execute(f"CREATE TABLE {name} (oid TEXT, cname TEXT)")
        con.executemany(f"INSERT INTO {name} VALUES (?, ?)", [(str(i), n) for i, n in enumerate(rows)])
    canonicalize_customers(con, [_table("a", False), _table("b", False)])
    values = {r[0] for t in "ab" for r in con.execute(f"SELECT cname FROM {t}")}
    assert values == {"Lê Văn An 0353838285"}


def test_code_shop_keeps_dimension_name_verbatim_with_phone():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE dim (cid TEXT, cname TEXT)")
    con.execute("CREATE TABLE orders (oid TEXT, cid TEXT, cname TEXT)")
    con.executemany("INSERT INTO dim VALUES (?, ?)", [("K1", "Dương Giang Hà - 0701 466 949"), ("K2", "Lê Văn An")])
    con.executemany("INSERT INTO orders VALUES (?, ?, ?)",
                    [("1", "K1", "Dương Giang Hà"), ("2", "K1", "Dương Giang Hà"), ("3", "K2", "Lê Văn An")])
    mapping = canonicalize_customers(con, [_dimension(), _table("orders", True)])
    assert mapping["K1"] == "Dương Giang Hà - 0701 466 949"
    assert mapping["K2"] == "Lê Văn An"
    assert {r[0] for r in con.execute("SELECT cname FROM orders WHERE cid='K1'")} == {"Dương Giang Hà - 0701 466 949"}


def test_code_shop_without_dimension_keeps_most_common_verbatim():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE orders (oid TEXT, cid TEXT, cname TEXT)")
    con.executemany("INSERT INTO orders VALUES (?, ?, ?)",
                    [("1", "K1", "Trần Hoa (0707632283)"), ("2", "K1", "Trần Hoa (0707632283)"), ("3", "K1", "Trần Hoa")])
    mapping = canonicalize_customers(con, [_table("orders", True)])
    assert mapping["K1"] == "Trần Hoa (0707632283)"


def test_code_shop_most_common_spelling_wins_even_when_bare():
    """No spelling is preferred for carrying a phone: the most written one is the customer's name."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE orders (oid TEXT, cid TEXT, cname TEXT)")
    rows = [("1", "K1", "Nguyễn Văn Mẫu")] * 4 + [("2", "K1", "Nguyễn Văn Mẫu (0900000001)")]
    con.executemany("INSERT INTO orders VALUES (?, ?, ?)", rows)
    assert canonicalize_customers(con, [_table("orders", True)]) == {"K1": "Nguyễn Văn Mẫu"}


def test_code_shop_bare_names_stay_bare_when_no_spelling_has_a_phone():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE orders (oid TEXT, cid TEXT, cname TEXT)")
    con.executemany("INSERT INTO orders VALUES (?, ?, ?)", [("1", "K1", "Lê Văn An"), ("2", "K1", "Lê Văn An")])
    assert canonicalize_customers(con, [_table("orders", True)]) == {"K1": "Lê Văn An"}


def test_alias_candidates_include_verbatim_form():
    """Every spelling seen for a code is a spelling the layer can still match: the canonical one is among them."""
    spellings = ["Trần Hoa (0707632283)", "Trần Hoa 0707 632 283", "Trần Hoa"]
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE orders (oid TEXT, cid TEXT, cname TEXT)")
    con.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(str(i), "K1", s) for i, s in enumerate(spellings)])
    mapping = canonicalize_customers(con, [_table("orders", True)])
    assert mapping["K1"] in spellings


@pytest.mark.parametrize("text,clean,digits", [
    ("Lê Văn An - 0353 838 285", "Lê Văn An", "0353838285"),
    ("Lê Văn An (+84 353 838 285)", "Lê Văn An", "0353838285"),
    ("Lê Văn An", "Lê Văn An", None),
    ("An 0353838285", "An 0353838285", None),  # one word before the number is not a name plus phone
])
def test_split_embedded_phone(text, clean, digits):
    assert split_embedded_phone(text) == (clean, digits)
