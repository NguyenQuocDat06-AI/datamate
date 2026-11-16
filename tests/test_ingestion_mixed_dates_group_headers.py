"""Mixed-spelling date columns (serials with text dates) and group-label header rows."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from bizreason.data.ingestion import BusinessBuilder
from bizreason.data.parsing import (
    ParseStatus,
    classify_date_column,
    infer_date_order,
    is_serial_cell,
    parse_date_ordered,
)

SERIAL_BASE = 45292  # a spreadsheet serial of a day in the early 2020s; only offsets matter here


def _serial_of(day: date) -> int:
    return (day - date(1899, 12, 30)).days


# parsing


def test_order_is_proven_by_a_day_above_twelve_in_first_place():
    assert infer_date_order(["13/02/2024", "01/03/2024"]) == "dmy"


def test_order_is_proven_by_a_day_above_twelve_in_second_place():
    assert infer_date_order(["02/13/2024", "03/01/2024"]) == "mdy"


def test_order_stays_ambiguous_when_every_part_is_at_most_twelve():
    assert infer_date_order(["01/02/2024", "03/04/2024", 45000]) == "ambiguous"


def test_order_conflict_when_both_places_exceed_twelve():
    assert infer_date_order(["13/02/2024", "02/13/2024"]) == "conflict"


def test_month_first_reading_and_invalid_pair_is_not_guessed():
    assert parse_date_ordered("02/13/2024", "mdy").value == "2024-02-13"
    assert parse_date_ordered("13/02/2024", "mdy").status == ParseStatus.INVALID
    assert parse_date_ordered("13/02/2024", "dmy").value == "2024-02-13"


def test_serial_range_rejects_small_amounts_and_booleans():
    assert is_serial_cell(45123) and is_serial_cell("45123")
    assert not is_serial_cell(1500) and not is_serial_cell(True) and not is_serial_cell("12/03/2024")


def test_mixed_serial_and_text_dates_make_a_date_column():
    values = [_serial_of(date(2024, 3, d)) for d in range(1, 9)] + ["15/03/2024", "20/03/2024", "21/03/2024"]
    assert classify_date_column(values) == "dmy"


def test_column_of_bare_amounts_is_never_a_date_column():
    assert classify_date_column([30000 + 500 * i for i in range(20)]) is None


def test_amounts_with_a_stray_text_date_are_not_a_date_column():
    values = [30000 + 500 * i for i in range(30)] + ["05/03/2024"]
    assert classify_date_column(values) is None


def test_serial_far_from_the_text_dates_rejects_the_column():
    values = ["15/03/2024"] * 3 + [_serial_of(date(1990, 1, 1))] * 3
    assert classify_date_column(values) is None


def test_ambiguous_slash_dates_without_hint_are_not_guessed_and_hint_decides():
    values = ["01/02/2024", "03/04/2024", "05/06/2024", _serial_of(date(2024, 2, 1))]
    assert classify_date_column(values) is None
    assert classify_date_column(values, hint="dmy") == "dmy"
    assert classify_date_column(values, hint="mdy") == "mdy"


def test_conflicting_order_rejects_the_column():
    assert classify_date_column(["13/02/2024", "02/13/2024", "14/03/2024", 45000]) is None


# ingestion


def _build(workspace_tmp, rules, rows, name="shop"):
    directory = workspace_tmp / name
    directory.mkdir(parents=True, exist_ok=True)
    lines = [",".join("" if cell is None else str(cell) for cell in row) for row in rows]
    (directory / "ledger.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)


def _column(context, table, name):
    connection = sqlite3.connect(context.database)
    try:
        return [row[0] for row in connection.execute(f'SELECT "{name}" FROM "{table.name}" ORDER BY __source_row')]
    finally:
        connection.close()


LEDGER_HEADER = ("Số chứng từ", "Ngày ghi sổ", "Diễn giải", "Số tiền")


def test_ledger_date_column_mixing_serials_and_text_is_normalized_per_cell(workspace_tmp, rules):
    days = [date(2024, 3, d) for d in range(1, 11)]
    cells = [_serial_of(day) if index % 2 == 0 else day.strftime("%d/%m/%Y") for index, day in enumerate(days)]
    cells[3] = "25/03/2024"  # a day above 12 proves day-first
    rows = [LEDGER_HEADER, *[(f"CT{i:03d}", cell, f"khoan {i}", 1000 * (i + 1)) for i, cell in enumerate(cells)]]
    context = _build(workspace_tmp, rules, rows)
    table = context.tables[0]
    values = _column(context, table, "ngay_ghi_so")
    assert len(values) == 10
    assert values[0] == "2024-03-01" and values[1] == "2024-03-02" and values[3] == "2024-03-25"
    assert all(isinstance(v, str) and len(v[:10]) == 10 and v[4] == "-" for v in values)
    # rows and amounts are untouched
    assert table.row_count == 10
    assert sum(int(v) for v in _column(context, table, "so_tien")) == sum(1000 * (i + 1) for i in range(10))


def test_ledger_date_column_with_only_day_first_text_dates(workspace_tmp, rules):
    rows = [LEDGER_HEADER, *[(f"CT{i}", f"{i + 10:02d}/04/2024", "x", 500) for i in range(1, 9)]]
    context = _build(workspace_tmp, rules, rows)
    values = _column(context, context.tables[0], "ngay_ghi_so")
    assert values[0] == "2024-04-11" and values[-1] == "2024-04-18"


def test_ambiguous_date_column_without_proof_is_left_as_written(workspace_tmp, rules):
    rows = [LEDGER_HEADER, *[(f"CT{i}", f"{i:02d}/02/2024", "x", 500) for i in range(1, 9)]]
    context = _build(workspace_tmp, rules, rows)
    values = _column(context, context.tables[0], "ngay_ghi_so")
    assert values[0] == "01/02/2024"  # not converted: 01/02 could be 1 Feb or 2 Jan


def test_unparseable_cell_becomes_null_not_a_guess(workspace_tmp, rules):
    cells = ["15/03/2024"] * 19 + ["khong ro"]
    rows = [LEDGER_HEADER, *[(f"CT{i}", cell, "x", 500) for i, cell in enumerate(cells)]]
    context = _build(workspace_tmp, rules, rows)
    values = _column(context, context.tables[0], "ngay_ghi_so")
    assert values[:19] == ["2024-03-15"] * 19 and values[19] is None


def test_amount_column_in_serial_range_is_untouched(workspace_tmp, rules):
    rows = [LEDGER_HEADER, *[(f"CT{i}", f"{20 + i}/03/2024", "x", 30000 + 100 * i) for i in range(8)]]
    context = _build(workspace_tmp, rules, rows)
    assert _column(context, context.tables[0], "so_tien")[:2] == ["30000", "30100"]


def test_group_label_row_above_real_header_makes_no_empty_columns(workspace_tmp, rules):
    header = ("Mã đơn", "Ngày bán", "Thành tiền", "Giảm giá", "Tên hàng", "Số lượng", "Đơn giá")
    data = [(f"DH{i}", f"{i + 12:02d}/03/2024", 1000 * i, 0, f"SP{i}", i, 1000) for i in range(1, 9)]
    rows = [("Đơn hàng", "Ngày", "Tiền", "Tiền", "Hàng hoá", "Hàng hoá", "Hàng hoá"), header, *data]
    context = _build(workspace_tmp, rules, rows)
    assert "GROUP_HEADER_ROW" in [d.code for d in context.diagnostics]
    assert len(context.tables) == 1
    assert [c.name for c in context.tables[0].columns][:2] == ["ma_don", "ngay_ban"]
    table = context.tables[0]
    assert table.row_count == 8
    for column in table.columns:
        assert any(v is not None and str(v).strip() for v in _column(context, table, column.name)), column.name


def test_plain_single_row_header_is_unchanged(workspace_tmp, rules):
    header = ("Mã đơn", "Ngày bán", "Thành tiền")
    rows = [header, *[(f"DH{i}", f"{i + 12:02d}/03/2024", 1000 * i) for i in range(1, 7)]]
    context = _build(workspace_tmp, rules, rows)
    table = context.tables[0]
    assert [c.name for c in table.columns] == ["ma_don", "ngay_ban", "thanh_tien"]
    codes = [d.code for d in context.diagnostics]
    assert "GROUP_HEADER_ROW" not in codes and "MULTI_TIER_HEADER" not in codes


def test_order_continuation_rows_are_kept_with_mixed_date_column(workspace_tmp, rules):
    header = ("Mã đơn", "Ngày bán", "Tên hàng", "Số lượng", "Thành tiền")
    rows = [header,
            ("DH1", "13/03/2024", "A", 1, 100), ("", "", "B", 2, 200),
            ("DH2", _serial_of(date(2024, 3, 14)), "C", 1, 300), ("", "", "D", 1, 400),
            ("DH3", "15/03/2024", "E", 3, 500)]
    context = _build(workspace_tmp, rules, rows)
    table = context.tables[0]
    assert table.row_count == 5
    assert _column(context, table, "ngay_ban")[2] == "2024-03-14"
