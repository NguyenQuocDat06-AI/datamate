"""Synthetic checks of the stock split into whole large units and loose small units."""
import sqlite3

import pytest

from bizreason.semantic.stock_split import SplitUnsupported, split_quantity, split_stock, unit_ladder

Q = "cuoi ngay hom nay {} con may {} chan, may {} le"


def build(stock, conv, conv_cols=("name", "big", "ratio", "xn_ratio", "small"), ids=None):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE stock (name_key TEXT, product_name TEXT, unit TEXT, qty_base REAL, large_factor REAL)")
    db.executemany("INSERT INTO stock VALUES (?,?,?,?,?)", stock)
    db.execute("CREATE TABLE column_catalog (table_name TEXT, column_name TEXT, role TEXT)")
    db.execute(f"CREATE TABLE src_conv ({','.join(conv_cols)})")
    db.executemany(f"INSERT INTO src_conv VALUES ({','.join('?' * len(conv_cols))})", conv)
    roles = dict(zip(conv_cols, ["product_name", "large_unit", "conversion_factor", "conversion_factor", "base_unit"]))
    if ids:
        roles["name"] = "product_id"
        db.execute("CREATE TABLE lines (product_id TEXT, product_key TEXT)")
        db.executemany("INSERT INTO lines VALUES (?,?)", ids)
    db.executemany("INSERT INTO column_catalog VALUES ('src_conv', ?, ?)", list(roles.items()))
    return db


BOX = [("alpha 10mg hop 3 vi x 10 vien", "Alpha 10mg hộp 3 vỉ x 10 viên", "viên", 163, None)]
BOX_CONV = [("Alpha 10mg hộp 3 vỉ x 10 viên", "vỉ", "10", 10.0, "viên"),
            ("Alpha 10mg hộp 3 vỉ x 10 viên", "hộp", "30", 30.0, "viên")]


def test_split_quantity():
    assert split_quantity(163, 30, 1) == (5, 13)
    # 163 = 5 boxes + 1 strip + 3 pieces: the 3 pieces are a third quantity the question did not ask, so no answer.
    with pytest.raises(SplitUnsupported):
        split_quantity(163, 30, 10)
    assert split_quantity(160, 30, 10) == (5, 1)
    with pytest.raises(SplitUnsupported):
        split_quantity(10, 7, 2)


def test_box_and_loose_base_unit():
    db = build(BOX, BOX_CONV)
    assert split_stock(db, "Tồn kho Alpha 10mg hộp 3 vỉ x 10 viên cuối ngày được mấy hộp chẵn và mấy viên lẻ", []) == (5, 13)


def test_intermediate_unit_is_the_small_one():
    db = build(BOX, BOX_CONV)
    assert split_stock(db, "ton kho alpha 10mg hop 3 vi x 10 vien la may vi chan, may vien le", []) == (16, 3)
    with pytest.raises(SplitUnsupported):  # 163 pieces leave 3 below one strip: not dropped silently
        split_stock(db, "ton kho alpha 10mg hop 3 vi x 10 vien la may hop chan, may vi le", [])


def test_base_unit_other_than_smallest_written_unit():
    stock = [("beta hop 10 vi", "Beta hộp 10 vỉ", "vỉ", 161, None)]
    conv = [("Beta hộp 10 vỉ", "hộp", "10", 10.0, "vỉ")]
    assert split_stock(build(stock, conv), "beta hop 10 vi con may hop chan, may vi le", []) == (16, 1)


def test_multiword_unit_and_product_found_by_filter():
    stock = [("luoi 1 5m", "Lưới 1.5m", "m", 56, None)]
    conv = [("Lưới 1.5m", "cuộn 10m", "10", 10.0, "m")]
    text = "cuoi ngay luoi 1.5m con may cuon 10m chan, may m le"
    assert split_stock(build(stock, conv), text, []) == (5, 6)


def test_product_not_written_in_full_uses_filter():
    stock = [("gamma 4mg hop 10 vi x 20 vien", "Gamma 4mg hộp 10 vỉ x 20 viên", "viên", 3220, None)]
    conv = [("Gamma 4mg hộp 10 vỉ x 20 viên", "hộp", "200", 200.0, "viên"),
            ("Gamma 4mg hộp 10 vỉ x 20 viên", "vỉ", "20", 20.0, "viên")]
    text = "con bao nhieu hop gamma 4mg nguyen va bao nhieu vi le"
    filters = [{"dim": "product", "value": "gamma 4mg", "exclude": False}]
    assert split_stock(build(stock, conv), text, filters) == (16, 1)


def test_id_keyed_conversion_list():
    stock = [("delta", "Delta", "cái", 80, None)]
    conv = [("SP1", "hộp", "6", 6.0, "cái")]
    db = build(stock, conv, ids=[("SP1", "delta")])
    assert split_stock(db, "delta con may hop chan, may cai le", []) == (13, 2)


def test_one_unit_question_is_left_to_plain_path():
    db = build(BOX, BOX_CONV)
    assert split_stock(db, "alpha 10mg hop 3 vi x 10 vien con may hop", []) is None
    assert split_stock(db, "alpha 10mg hop 3 vi x 10 vien con may hop chan, le bao nhieu", []) is None


def test_unit_not_of_the_product_is_not_guessed():
    db = build(BOX, BOX_CONV)
    assert split_stock(db, "alpha 10mg hop 3 vi x 10 vien con may thung chan, may vien le", []) is None


def test_unknown_product_is_left_alone():
    assert split_stock(build(BOX, BOX_CONV), "omega con may hop chan, may vien le", []) is None


def test_wrong_order_and_conflicting_factors_unsupported():
    db = build(BOX, BOX_CONV)
    with pytest.raises(SplitUnsupported):
        split_stock(db, "alpha 10mg hop 3 vi x 10 vien con may vien chan, may hop le", [])
    conflict = BOX_CONV + [("Alpha 10mg hộp 3 vỉ x 10 viên", "hộp", "20", 20.0, "viên")]
    assert "hộp" not in unit_ladder(build(BOX, conflict), BOX[0][0], "viên")
    assert split_stock(build(BOX, conflict), "alpha 10mg hop 3 vi x 10 vien may hop chan, may vien le", []) is None


def test_conversion_with_other_base_unit_is_ignored():
    conv = [("Alpha 10mg hộp 3 vỉ x 10 viên", "hộp", "3", 3.0, "vỉ")]
    assert unit_ladder(build(BOX, conv), BOX[0][0], "viên") == {"viên": 1}


def test_fractional_stock_unsupported():
    stock = [("alpha", "Alpha", "viên", 10.5, None)]
    conv = [("Alpha", "hộp", "4", 4.0, "viên")]
    with pytest.raises(SplitUnsupported):
        split_stock(build(stock, conv), "alpha con may hop chan, may vien le", [])


def test_two_products_named_unsupported():
    stock = [("alpha", "Alpha", "viên", 10, None), ("beta", "Beta", "viên", 10, None)]
    conv = [("Alpha", "hộp", "4", 4.0, "viên")]
    with pytest.raises(SplitUnsupported):
        split_stock(build(stock, conv), "alpha beta con may hop chan, may vien le", [])
