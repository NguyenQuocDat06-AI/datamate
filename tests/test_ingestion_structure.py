"""Structural header and preamble features: two-tier headers, page-break repeats, amount scale."""

from __future__ import annotations

from bizreason.data.ingestion import (
    BusinessBuilder,
    _amount_scale,
    _is_repeated_header,
    _multi_tier_labels,
)

HEADER = ("Mã", "Số lượng", "Đơn giá", "Số lượng", "Đơn giá")
DATA = [
    ("SP-01", 3, 120, 4, 130),
    ("SP-02", 5, 450, 2, 460),
    ("SP-03", 2, 300, 7, 310),
]


def _write(directory, lines):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sales.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return directory


def _csv(rows):
    return [",".join("" if cell is None else str(cell) for cell in row) for row in rows]


def _build(workspace_tmp, rules, name, rows):
    directory = _write(workspace_tmp / name, _csv(rows))
    return BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)


def _codes(context):
    return [diagnostic.code for diagnostic in context.diagnostics]


# Two-tier headers


def test_group_labels_prefix_their_sub_columns():
    upper = (None, "Bán hàng", None, "Kho", None)
    labels = _multi_tier_labels(upper, HEADER)
    assert labels == ("Mã", "Bán hàng_Số lượng", "Bán hàng_Đơn giá", "Kho_Số lượng", "Kho_Đơn giá")


def test_group_label_carries_across_blank_cells_until_next_label():
    upper = ("Thông tin", None, None, "Kho", None)
    header = ("Mã", "Số lượng", "Đơn giá", "Số lượng", "Đơn giá")
    labels = _multi_tier_labels(upper, header)
    assert labels[1:3] == ("Thông tin_Số lượng", "Thông tin_Đơn giá")
    assert labels[3:] == ("Kho_Số lượng", "Kho_Đơn giá")


def test_title_row_above_header_is_not_merged():
    title = ("BÁO CÁO BÁN HÀNG QUÝ I", None, None, None, None)
    assert _multi_tier_labels(title, HEADER) is None


def test_two_tier_names_are_unique_end_to_end(workspace_tmp, rules):
    rows = [
        (None, "Bán hàng", None, "Kho", None),
        HEADER,
        *DATA,
    ]
    context = _build(workspace_tmp, rules, "two_tier", rows)
    table = context.tables[0]
    names = [column.name for column in table.columns]
    assert len(names) == len(set(names))
    assert "ban_hang_so_luong" in names
    assert "kho_don_gia" in names
    assert "MULTI_TIER_HEADER" in _codes(context)
    assert table.row_count == len(DATA)


def test_title_above_header_end_to_end_keeps_plain_names(workspace_tmp, rules):
    rows = [
        ("BÁO CÁO BÁN HÀNG QUÝ I", None, None, None, None),
        ("Mã", "Số lượng", "Đơn giá", "Thành tiền", "Ghi chú"),
        *[(sku, qty, price, qty * price, "") for sku, qty, price, _, _ in DATA],
    ]
    context = _build(workspace_tmp, rules, "title_only", rows)
    table = context.tables[0]
    assert "MULTI_TIER_HEADER" not in _codes(context)
    assert "so_luong" in [column.name for column in table.columns]


# Page-break repeats


def test_page_break_repeat_is_excluded_and_counted(workspace_tmp, rules):
    # The printed copy differs in case from the header, so the exact-copy rule does not apply.
    repeat = ("MÃ", "SỐ LƯỢNG", "ĐƠN GIÁ", "SỐ LƯỢNG", "ĐƠN GIÁ")
    rows = [
        HEADER,
        DATA[0],
        DATA[1],
        repeat,
        DATA[2],
    ]
    context = _build(workspace_tmp, rules, "page_break", rows)
    table = context.tables[0]
    assert table.row_count == len(DATA)
    assert table.excluded_rows == 1
    assert table.raw_row_count == table.row_count + table.excluded_rows + table.duplicate_rows
    assert "EXCLUDED_REPEATED_HEADER_ROW" in _codes(context)


def test_repeat_check_needs_half_the_header_labels_and_no_numbers():
    assert _is_repeated_header(["Mã", "Số lượng", "Đơn giá", "Số lượng", "Đơn giá"], HEADER)
    assert _is_repeated_header(["Mã", "Số lượng", "x", "y", "z"], HEADER) is False
    assert _is_repeated_header(["Mã", "Số lượng", "Đơn giá", "Số lượng", 130], HEADER) is False
    assert _is_repeated_header(["SP-01", 3, 120, 4, 130], HEADER) is False


# Amount scale in the preamble


def test_thousand_unit_statement_is_detected(workspace_tmp, rules):
    rows = [
        ("Đơn vị tính: nghìn đồng", None, None, None, None),
        (None,) * 5,
        HEADER,
        *DATA,
    ]
    context = _build(workspace_tmp, rules, "thousand", rows)
    scales = [d for d in context.diagnostics if d.code == "AMOUNT_SCALE"]
    assert [d.message for d in scales] == ["1000"]
    assert scales[0].severity == "info"
    assert scales[0].source.table == "sales"
    assert scales[0].source.path == "sales.csv"


def test_million_unit_statement_is_detected(workspace_tmp, rules):
    rows = [
        ("ĐVT: triệu đồng", None, None, None, None),
        HEADER,
        *DATA,
    ]
    context = _build(workspace_tmp, rules, "million", rows)
    assert [d.message for d in context.diagnostics if d.code == "AMOUNT_SCALE"] == ["1000000"]


def test_unit_words_are_read_from_helper_rows():
    assert _amount_scale([("Unit: thousand",)]) == 1000
    assert _amount_scale([("ĐVT: triệu đồng", None)]) == 1_000_000
    assert _amount_scale([("Đơn vị tính: nghìn đồng",)]) == 1000


def test_ordinary_preamble_has_no_scale(workspace_tmp, rules):
    rows = [
        ("Báo cáo bán hàng tháng 3", None, None, None, None),
        ("Kho trung tâm, ngân hàng dữ liệu", None, None, None, None),
        HEADER,
        *DATA,
    ]
    context = _build(workspace_tmp, rules, "plain_preamble", rows)
    assert "AMOUNT_SCALE" not in _codes(context)
    assert _amount_scale([("Ngân hàng: Kho trung tâm",)]) is None
    assert _amount_scale([("Đơn vị tính: cái",)]) is None
