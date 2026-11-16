import csv
import sqlite3
import zipfile
from contextlib import closing

import pytest
from openpyxl import Workbook

from bizreason.data.ingestion import BusinessBuilder
from bizreason.data.readers import sqlite_readonly, unpack_dataset
from bizreason.utils import file_hash, writable_path


def test_group_inheritance_does_not_leak_customers(workspace_tmp, rules):
    directory = workspace_tmp / "grouped"
    directory.mkdir()
    source = directory / "sales.csv"
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(
            [
                [
                    "Mã đơn",
                    "Ngày bán",
                    "Trạng thái",
                    "Tên khách",
                    "Tên sản phẩm",
                    "Số lượng",
                    "Thành tiền",
                ],
                ["0001", "2026-03-01", "hoàn thành", "An", "A", 1, 10],
                ["", "", "", "", "B", 2, 20],
                ["0002", "2026-03-02", "hoàn thành", "", "A", 1, 10],
                ["", "", "", "", "B", 2, 20],
                ["", "", "", "", "B", 2, 20],
            ]
        )
    before = file_hash(source)
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    table = context.tables[0]
    with closing(sqlite_readonly(context.database)) as connection:
        rows = connection.execute(
            f'SELECT ma_don,ten_khach FROM "{table.name}" ORDER BY __source_row'
        ).fetchall()
    assert rows == [("0001", "An"), ("0001", "An"), ("0002", None), ("0002", None)]
    assert table.duplicate_rows == 1
    assert table.raw_row_count == table.row_count + table.duplicate_rows + table.excluded_rows
    assert file_hash(source) == before


def test_identical_independent_ledger_events_are_preserved(workspace_tmp, rules):
    directory = workspace_tmp / "ledger"
    directory.mkdir()
    (directory / "ledger.csv").write_text(
        "Ngày thu,Số tiền thu\n01/03/2026,100\n01/03/2026,100\n", encoding="utf-8"
    )
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    assert context.tables[0].row_count == 2


def test_late_excel_header_and_multiple_sheets(workspace_tmp, rules):
    directory = workspace_tmp / "excel"
    directory.mkdir()
    workbook = Workbook()
    first = workbook.active
    first.title = "2026"
    first.append(["Cửa hàng"])
    first.append([])
    first.append(["Mã đơn", "Ngày bán", "Tên sản phẩm", "Số lượng"])
    first.append(["001", "04/03", "A", 1])
    second = workbook.create_sheet("Danh mục")
    second.append(["Mã sản phẩm", "Tên sản phẩm"])
    second.append(["P01", "A"])
    workbook.save(directory / "export.xlsx")
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    assert len(context.tables) == 2
    table = next(table for table in context.tables if table.grain == "order_line")
    assert table.header_row == 3
    assert table.coverage["ngay_ban"] == ("2026-03-04", "2026-03-04")


def test_sqlite_source_is_unchanged_and_cache_invalidates(workspace_tmp, rules):
    directory = workspace_tmp / "database"
    directory.mkdir()
    source = directory / "pos.sqlite"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE products (product_id TEXT, product_name TEXT)")
        connection.execute("INSERT INTO products VALUES ('P1','A')")
        connection.commit()
    before = file_hash(source)
    builder = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision)
    first = builder.prepare(directory)
    assert builder.prepare(directory).revision == first.revision
    assert file_hash(source) == before
    (directory / "README.md").write_text("Changed documentation", encoding="utf-8")
    assert builder.prepare(directory).revision != first.revision


def test_output_boundary_rejects_external_paths(workspace_tmp):
    with pytest.raises(ValueError):
        writable_path(workspace_tmp.parents[4] / "outside.json")


def test_zip_slip_is_rejected_before_any_extraction(workspace_tmp):
    package = workspace_tmp / "bad.zip"
    with zipfile.ZipFile(package, "w") as handle:
        handle.writestr("safe/file.txt", "safe")
        handle.writestr("../escape.txt", "escape")
    target = workspace_tmp / "unpacked"
    with pytest.raises(ValueError):
        unpack_dataset(package, target)
    assert not (target / "safe" / "file.txt").exists()
