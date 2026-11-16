"""Header choice from sheet structure, without relying on known header names."""

from __future__ import annotations

from bizreason.data.ingestion import BusinessBuilder, choose_header_row, header_score
from bizreason.data.schema import infer_role

UNKNOWN_HEADER = ("Mã phiếu", "Người phụ trách", "Ngày ký", "Lô hàng", "Điểm ưu tiên", "Số dư")
NUMERIC_DATA = [
    ("PH-001", "Nguyễn An", "2026-03-01", "L-7", 3, 120000),
    ("PH-002", "Trần Bình", "2026-03-02", "L-8", 5, 450000),
    ("PH-003", "Lê Hoa", "2026-03-03", "L-9", 2, 300000),
]


def _assert_unknown(headers):
    assert all(infer_role(str(h), "") is None for h in headers), headers


def test_title_and_blank_rows_before_unknown_header():
    _assert_unknown(UNKNOWN_HEADER)
    prefix = [
        ("Báo cáo nhập kho quý I", None, None, None, None, None),
        (None, None, None, None, None, None),
        UNKNOWN_HEADER,
        *NUMERIC_DATA,
    ]
    position, score = choose_header_row(prefix, "")
    assert position == 2
    assert score >= 2.5


def test_sparse_notes_row_is_not_chosen():
    prefix = [
        ("Ghi chú", "số liệu tạm tính"),
        (None,) * 6,
        UNKNOWN_HEADER,
        *NUMERIC_DATA,
    ]
    position, _ = choose_header_row(prefix, "")
    assert position == 2
    note_score = header_score(prefix[0], "", prefix[1:], width=6)
    header_row_score = header_score(prefix[2], "", prefix[3:], width=6)
    assert note_score < header_row_score


def test_text_data_row_is_not_chosen_over_real_header_above_it():
    prefix = [
        UNKNOWN_HEADER,
        ("PH-000", "Nguyễn Dũng", "Đã nhập", "Kho 1", "Cao", "Ghi chú"),
        *NUMERIC_DATA,
    ]
    position, _ = choose_header_row(prefix, "")
    assert position == 0


def test_numbers_below_text_label_decide_the_header():
    # The first text-only row is earlier and equally shaped, but its columns stay text below.
    prefix = [
        ("Cột một", "Cột hai", "Cột ba"),
        ("Dòng x", "Dòng y", "Dòng z"),
        ("Dòng p", "Dòng q", "Dòng r"),
        ("Dòng s", "Dòng t", "Dòng u"),
        ("Nhãn a", "Nhãn b", "Nhãn c"),
        (1, 2, 3),
        (4, 5, 6),
        (7, 8, 9),
    ]
    position, _ = choose_header_row(prefix, "")
    assert position == 4


def test_readme_mention_helps_choose_between_equal_shapes():
    readme = "Bảng có các cột Mã kho, Tên kho, Số dư tồn cuối kỳ."
    prefix = [
        ("Kiểu A", "Kiểu B", "Kiểu C"),
        ("Mã kho", "Tên kho", "Số dư"),
        ("Dòng x", "Dòng y", "Dòng z"),
        ("Dòng p", "Dòng q", "Dòng r"),
    ]
    _assert_unknown(prefix[0])
    position, _ = choose_header_row(prefix, readme)
    assert position == 1


def test_unknown_header_names_still_resolve_end_to_end(workspace_tmp, rules):
    directory = workspace_tmp / "unknown_names"
    directory.mkdir()
    lines = [
        "Báo cáo nhập kho quý I,,,,,",
        ",,,,,",
        ",".join(UNKNOWN_HEADER),
    ] + [",".join(str(v) for v in row) for row in NUMERIC_DATA]
    (directory / "stock.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    assert not [d for d in context.diagnostics if d.code == "UNRESOLVED_HEADER"]
    assert context.tables[0].header_row == 3
    assert context.tables[0].row_count == len(NUMERIC_DATA)


def test_sheet_without_plausible_header_stays_unresolved(workspace_tmp, rules):
    directory = workspace_tmp / "numbers_only"
    directory.mkdir()
    lines = [f"{i},{i * 2},{i * 3}" for i in range(1, 8)]
    (directory / "numbers.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    assert [d.code for d in context.diagnostics if d.code == "UNRESOLVED_HEADER"] == [
        "UNRESOLVED_HEADER"
    ]
    assert not context.tables
