from __future__ import annotations

import csv
import sqlite3
from contextlib import closing

from bizreason.data.ingestion import BusinessBuilder
from bizreason.data.identities import strip_embedded_phone
from bizreason.data.notes_facets import classify_note_status
from bizreason.rules import RuleRegistry


def write(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows(rows)


def test_strip_embedded_phone_helper():
    assert strip_embedded_phone("Nguyen Van A (0901234567)") == ("Nguyen Van A", "0901234567")
    assert strip_embedded_phone("Nguyen Van A - 0901.234.567") == ("Nguyen Van A", "0901.234.567")
    assert strip_embedded_phone("Nguyen Van A 0901234567") == ("Nguyen Van A", "0901234567")
    assert strip_embedded_phone("Nguyễn Văn A (+84 901 234 567)") == ("Nguyễn Văn A", "+84 901 234 567")
    assert strip_embedded_phone("Nguyễn Văn A - 090-123-4567") == ("Nguyễn Văn A", "090-123-4567")
    assert strip_embedded_phone("Nguyễn Văn A") == ("Nguyễn Văn A", None)
    assert strip_embedded_phone("Cửa hàng 123") == ("Cửa hàng 123", None)
    assert strip_embedded_phone("KH01 (0901234567)") == ("KH01 (0901234567)", None)
    assert strip_embedded_phone("Bia Saigon 330ml") == ("Bia Saigon 330ml", None)
    assert strip_embedded_phone("Anh 0901234567") == ("Anh 0901234567", None)


def test_classify_note_status_helper():
    assert classify_note_status("Đã giao hàng") == ("completed", False)
    assert classify_note_status("Khách huỷ") == ("cancelled", False)
    assert classify_note_status("Khách trả hàng") == ("returned", False)
    assert classify_note_status("Đang ship") == ("shipping", False)
    assert classify_note_status("Giao giờ hành chính, ít đá") == ("completed", False)
    assert classify_note_status("") == ("completed", False)
    assert classify_note_status(None) == ("completed", False)
    assert classify_note_status("bom hang") == ("completed", False)
    assert classify_note_status("Đã giao nhưng khách huỷ") == (None, True)


def test_purchase_line_base_quantity_conversion_and_conflict_rejection(workspace_tmp):
    rules_text = (
        "# Business rules\n"
        "## Đơn vị và quy đổi\n"
        "Quy đổi về đơn vị cơ bản theo hệ số quy đổi. Đơn vị không quy đổi coi như hệ số 1.\n"
    )
    rules = RuleRegistry(rules_text)

    # 1. Normal conversion case
    directory = workspace_tmp / "purchases_ok"
    directory.mkdir()
    write(directory / "conversions.csv", [
        ["mã hàng", "đơn vị quy đổi", "hệ số quy đổi", "đơn vị cơ bản"],
        ["P1", "thùng", 24, "viên"],
        ["P1", "hộp", 6, "viên"],
        ["P2", "thùng", 10, "gói"],
    ])
    write(directory / "purchases.csv", [
        ["mã phiếu nhập", "ngày nhập", "mã hàng", "số lượng", "đơn vị", "đơn giá"],
        ["PO1", "2026-01-01", "P1", 5, "thùng", 100000],
        ["PO2", "2026-01-02", "P1", 2, "hộp", 20000],
        ["PO3", "2026-01-03", "P2", 3, "thùng", 5000],
    ])
    builder = BusinessBuilder(workspace_tmp / "cache_p1", rule_hash=rules.revision, rules=rules)
    context = builder.prepare(directory)
    purchase_table = next(t for t in context.tables if t.grain == "purchase_line")
    assert purchase_table.role("qty_base") == "derived_qty_base"
    with closing(sqlite3.connect(context.database)) as db:
        rows = db.execute(
            f'SELECT ma_phieu_nhap, derived_qty_base FROM "{purchase_table.name}" ORDER BY ma_phieu_nhap'
        ).fetchall()
        assert rows == [("PO1", 120), ("PO2", 12), ("PO3", 30)]

    # 2. Conflicting conversion rejection
    bad_dir = workspace_tmp / "purchases_bad"
    bad_dir.mkdir()
    write(bad_dir / "conversions.csv", [
        ["mã hàng", "đơn vị quy đổi", "hệ số quy đổi", "đơn vị cơ bản"],
        ["P1", "thùng", 24, "viên"],
        ["P1", "thùng", 12, "viên"],
    ])
    write(bad_dir / "purchases.csv", [
        ["mã phiếu nhập", "ngày nhập", "mã hàng", "số lượng", "đơn vị", "đơn giá"],
        ["PO1", "2026-01-01", "P1", 5, "thùng", 100000],
    ])
    bad_builder = BusinessBuilder(workspace_tmp / "cache_p2", rule_hash=rules.revision, rules=rules)
    bad_context = bad_builder.prepare(bad_dir)
    assert any(d.code == "UNRESOLVED_CONVERSION" for d in bad_context.diagnostics)
    bad_table = next(t for t in bad_context.tables if t.grain == "purchase_line")
    assert bad_table.role("qty_base") is None


def test_customer_canonicalization_and_embedded_phone(workspace_tmp, rules):
    directory = workspace_tmp / "customers"
    directory.mkdir()
    write(directory / "customers.csv", [
        ["mã khách hàng", "tên khách hàng"],
        ["C01", "Nguyễn Văn A (0901234567)"],
        ["C02", "Trần Thị B - 0912.345.678"],
    ])
    write(directory / "sales.csv", [
        ["mã đơn", "ngày bán", "trạng thái", "mã khách hàng", "tên khách hàng", "mã hàng", "số lượng", "thành tiền"],
        ["O1", "2026-01-01", "completed", "C01", "Nguyễn Văn A - 0901234567", "P1", 1, 100],
        ["O2", "2026-01-02", "completed", "C01", "Nguyễn Văn A", "P1", 1, 100],
        ["O3", "2026-01-03", "completed", "C02", "Trần Thị B 0912345678", "P1", 1, 100],
        ["O4", "2026-01-04", "completed", "C03", "Lê Văn C", "P1", 1, 100],
        ["O5", "2026-01-05", "completed", "C03", "Lê Văn C (0988888888)", "P1", 1, 100],
        ["O6", "2026-01-06", "completed", "C04", "Lê Văn C", "P1", 1, 100],
        ["O7", "2026-01-07", "completed", None, "Khách lẻ", "P1", 1, 100],
    ])
    context = BusinessBuilder(workspace_tmp / "cache_c", rule_hash=rules.revision, rules=rules).prepare(directory)
    cust_dim = next(t for t in context.tables if t.role("customer_id") and not t.role("order_id"))
    sales_tbl = next(t for t in context.tables if t.grain == "order_line")

    with closing(sqlite3.connect(context.database)) as db:
        dim_rows = dict(db.execute(f'SELECT ma_khach_hang, ten_khach_hang FROM "{cust_dim.name}"').fetchall())
        # Rulebook section 9: names are given as the data writes them, so the phone number stays in the name
        # (the old behaviour cut it off, which no longer matched the reference answers of customer rankings).
        assert dim_rows["C01"] == "Nguyễn Văn A (0901234567)"
        assert dim_rows["C02"] == "Trần Thị B - 0912.345.678"

        sales_rows = db.execute(f'SELECT ma_don, ma_khach_hang, ten_khach_hang FROM "{sales_tbl.name}" ORDER BY ma_don').fetchall()
        row_map = {r[0]: (r[1], r[2]) for r in sales_rows}
        assert row_map["O1"] == ("C01", "Nguyễn Văn A (0901234567)")
        assert row_map["O2"] == ("C01", "Nguyễn Văn A (0901234567)")
        assert row_map["O3"] == ("C02", "Trần Thị B - 0912.345.678")

        # Intra-customer unification without separate catalog
        assert row_map["O4"] == ("C03", "Lê Văn C (0988888888)")
        assert row_map["O5"] == ("C03", "Lê Văn C (0988888888)")

        # Distinct customer IDs with same name strictly preserved
        assert row_map["O6"] == ("C04", "Lê Văn C")
        grouped = db.execute(
            f'SELECT ma_khach_hang, COUNT(*) FROM "{sales_tbl.name}" WHERE ma_khach_hang IN ("C03", "C04") GROUP BY ma_khach_hang'
        ).fetchall()
        assert set(grouped) == {("C03", 2), ("C04", 1)}

        # Retail customer with empty ID remains None
        assert row_map["O7"] == (None, None)

        # Raw json preserves raw cells
        raw_o1 = db.execute(f'SELECT __raw_json FROM "{sales_tbl.name}" WHERE ma_don="O1"').fetchone()[0]
        assert "Nguyễn Văn A - 0901234567" in raw_o1

    cname_col = next(c for c in sales_tbl.columns if c.semantic_role == "customer_name")
    # Examples show the names as the data writes them, phone numbers included (rulebook section 9).
    assert any("09" in str(ex) for ex in cname_col.examples)


def test_status_from_notes_rulebook_governed(workspace_tmp):
    rules_with_note = RuleRegistry(
        "# Business rules\n"
        "## Đơn hàng\n"
        "Sổ không có cột trạng thái thì lấy từ ghi chú. Ghi chú không nhắc trạng thái là đơn hoàn thành.\n"
    )

    directory = workspace_tmp / "notes_ok"
    directory.mkdir()
    write(directory / "sales.csv", [
        ["mã đơn", "ngày đặt", "tên khách", "mã hàng", "số lượng", "thành tiền", "ghi chú"],
        ["O1", "2026-01-01", "Khách 1", "P1", 1, 100, "Đã giao hàng"],
        ["O2", "2026-01-02", "Khách 2", "P1", 1, 100, "Khách huỷ"],
        ["O3", "2026-01-03", "Khách 3", "P1", 1, 100, "Khách trả hàng"],
        ["O4", "2026-01-04", "Khách 4", "P1", 1, 100, "Đang ship"],
        ["O5", "2026-01-05", "Khách 5", "P1", 1, 100, "Giao giờ hành chính, ít đá"],
        ["O6", "2026-01-06", "Khách 6", "P1", 1, 100, ""],
        ["O7", "2026-01-07", "Khách 7", "P1", 1, 100, "Đã giao nhưng khách huỷ"],
    ])

    context = BusinessBuilder(workspace_tmp / "cache_n1", rule_hash=rules_with_note.revision, rules=rules_with_note).prepare(directory)
    table = context.tables[0]
    assert table.role("status") == "derived_status"

    with closing(sqlite3.connect(context.database)) as db:
        rows = dict(db.execute(f'SELECT ma_don, derived_status FROM "{table.name}"').fetchall())
        assert rows["O1"] == "completed"
        assert rows["O2"] == "cancelled"
        assert rows["O3"] == "returned"
        assert rows["O4"] == "shipping"
        assert rows["O5"] == "completed"
        assert rows["O6"] == "completed"
        assert rows["O7"] is None

    col = table.column("derived_status")
    assert col.parse_errors == 1
    assert col.null_count == 1
    assert "rulebook:note_status_fallback" in col.evidence


def test_status_from_notes_propagates_returns(workspace_tmp):
    rules = RuleRegistry("Sổ không có cột trạng thái thì lấy từ ghi chú. Ghi chú không nhắc trạng thái là đơn hoàn thành.")
    directory = workspace_tmp / "notes_returns"
    directory.mkdir()
    write(directory / "sales.csv", [
        ["mã đơn", "ngày đặt", "mã hàng", "số lượng", "thành tiền", "ghi chú"],
        ["O8", "2026-01-08", "P1", 1, 100, "Đã giao"],
        ["O8", "2026-01-08", "P2", 1, 50, "Khách đổi trả"],
    ])
    context = BusinessBuilder(workspace_tmp / "cache_n2", rule_hash=rules.revision, rules=rules).prepare(directory)
    table = context.tables[0]
    with closing(sqlite3.connect(context.database)) as db:
        statuses = db.execute(f'SELECT derived_status FROM "{table.name}" WHERE ma_don="O8"').fetchall()
        assert statuses == [("returned",), ("returned",)]


def test_status_from_notes_skipped_when_rulebook_lacks_clause(workspace_tmp):
    rules = RuleRegistry("Đơn không huỷ và không trả hàng. Đơn vị không quy đổi coi như hệ số 1.")
    directory = workspace_tmp / "notes_skip"
    directory.mkdir()
    write(directory / "sales.csv", [
        ["mã đơn", "ngày đặt", "mã hàng", "số lượng", "thành tiền", "ghi chú"],
        ["O1", "2026-01-01", "P1", 1, 100, "Đã giao hàng"],
    ])
    context = BusinessBuilder(workspace_tmp / "cache_n3", rule_hash=rules.revision, rules=rules).prepare(directory)
    table = context.tables[0]
    assert table.role("status") is None
    assert table.column("derived_status") is None


def test_status_from_notes_skipped_when_table_has_status(workspace_tmp):
    rules = RuleRegistry("Sổ không có cột trạng thái thì lấy từ ghi chú. Ghi chú không nhắc trạng thái là đơn hoàn thành.")
    directory = workspace_tmp / "notes_has_status"
    directory.mkdir()
    write(directory / "sales.csv", [
        ["mã đơn", "ngày đặt", "trạng thái", "mã hàng", "số lượng", "thành tiền", "ghi chú"],
        ["O1", "2026-01-01", "completed", "P1", 1, 100, "Khách huỷ"],
    ])
    context = BusinessBuilder(workspace_tmp / "cache_n4", rule_hash=rules.revision, rules=rules).prepare(directory)
    table = context.tables[0]
    assert table.role("status") == "trang_thai"
    assert table.column("derived_status") is None
