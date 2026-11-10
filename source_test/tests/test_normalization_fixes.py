from __future__ import annotations

import csv
import sqlite3
from contextlib import closing
from decimal import Decimal

from bizreason.compilation import TemplateCompiler
from bizreason.contracts import UnsupportedClaim
from bizreason.data.ingestion import BusinessBuilder, infer_locale
from bizreason.data.parsing import ParseStatus, parse_number
from bizreason.decision import verify_unsupported
from bizreason.execution import execute_sql
from bizreason.data.unit_graph import UnitGraph
from bizreason.data.identities import is_retail_customer
import pytest


def write(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows(rows)


def test_currency_cell_overrides_inferred_locale_without_breaking_export_decimals():
    for raw, expected in [("7.000đ", 7000), ("105.000đ", 105000), ("50,500", 50500), ("8,000.0000", 8000)]:
        result = parse_number(raw, locale="en", unit="VND")
        assert result.status == ParseStatus.OK
        assert result.value == Decimal(expected)
    assert parse_number("1.234").status == ParseStatus.AMBIGUOUS
    assert parse_number("1.25", locale="en").value == Decimal("1.25")


def test_mid_table_totals_never_acquire_an_order(workspace_tmp, rules):
    directory = workspace_tmp / "daily"
    directory.mkdir()
    write(directory / "sales.csv", [
        ["order_id", "ordered_at", "status", "customer_name", "product_name", "qty_sale", "line_amount"],
        ["A", "2025-03-04", "completed", "Khách lẻ", "Món A", 1, 100],
        [None, None, None, "TỔNG NGÀY 04/03", None, None, 100],
        ["B", "2025-03-05", "completed", "Lan", "Cộng", 1, 200],
    ])
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    table = context.tables[0]
    assert table.row_count == 2 and table.excluded_rows == 1
    result = execute_sql(context.database, f'SELECT SUM(line_amount) FROM "{table.name}"', [], [table.name])
    assert result.rows == [[300]]


def test_public_star_succeeds_and_explicit_provenance_remains_denied(business):
    table = business.tables[0]
    result = execute_sql(business.database, f'SELECT * FROM "{table.name}" LIMIT 1', [], [table.name])
    assert result.status == "success"
    assert set(result.columns) == {c.name for c in table.columns}
    hidden = execute_sql(business.database, f'SELECT __raw_json FROM main."{table.name}"', [], [table.name])
    assert hidden.status == "policy_denied"


def test_complete_mapped_inventory_supports_missing_field(business, plan_factory):
    source = next(iter(business.manifest))
    plan = plan_factory("unsupported", output_type="unanswerable", unsupported=UnsupportedClaim(
        kind="missing_field", required=["product_category"], explanation="Verified all source headers", source_refs=[source]))
    assert verify_unsupported(plan, business).verified
    unknown = business.tables[0].columns[0].model_copy(update={"semantic_role": None})
    table = business.tables[0].model_copy(update={"columns": [unknown, *business.tables[0].columns[1:]]})
    incomplete_mapping = business.model_copy(update={"tables": [table, *business.tables[1:]]})
    assert not verify_unsupported(plan, incomplete_mapping).verified


def test_builtin_product_grouping_uses_code_template(business, rules, plan_factory):
    table = next(t for t in business.tables if t.grain == "order_line")
    plan = plan_factory("top_products", output_type="list", shape="list", group_by=[
        f"{table.name}.{table.role('product_id')}", f"{table.name}.{table.role('product_name')}"])
    program = TemplateCompiler(rules).compile(plan, business)
    assert program.backend == "template"
    assert f'GROUP BY "{table.role("product_id")}"' in program.sql


def test_business_headers_and_retail_names_are_normalized(workspace_tmp, rules):
    directory = workspace_tmp / "headers"
    directory.mkdir()
    write(directory / "sales.csv", [
        ["Đơn số", "Giờ tạo đơn", "Trạng thái giao", "Hàng hóa", "Barcode", "S.L", "Thành tiền (đ)", "Khách hàng (tên)"],
        [" O1 ", "2026-01-01 10:00:00", "Xong", "A", "P1", 1, "7.000đ", "kl"],
    ])
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    table = context.tables[0]
    assert table.grain == "order_line" and table.coverage
    assert table.role("order_id") == "don_so" and table.role("line_amount") == "thanh_tien_d"
    with closing(sqlite3.connect(context.database)) as db:
        assert db.execute(f'SELECT don_so,thanh_tien_d,khach_hang_ten FROM "{table.name}"').fetchone() == ("O1", 7000, None)


def test_product_catalog_unifies_case_variants_without_losing_source(workspace_tmp, rules):
    directory = workspace_tmp / "products"
    directory.mkdir()
    write(directory / "catalog.csv", [["product_id", "product_name"], ["P1", "Cánh gà"], ["P2", "Bia"]])
    write(directory / "sales.csv", [
        ["order_id", "ordered_at", "status", "product_id", "product_name", "qty_sale", "qty_base", "line_amount"],
        ["A", "2026-01-01", "completed", "P1", "Cánh gà", 81, 81, 100],
        ["B", "2026-01-01", "completed", "P1", "CÁNH GÀ", 18, 18, 100],
        ["C", "2026-01-01", "completed", "P1", "Cánh Gà", 13, 13, 100],
        ["D", "2026-01-01", "completed", "P2", "Bia", 108, 108, 100],
    ])
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    table = next(t for t in context.tables if t.grain == "order_line")
    with closing(sqlite3.connect(context.database)) as db:
        assert db.execute(f'SELECT product_id,SUM(qty_base) FROM "{table.name}" GROUP BY product_id,product_name ORDER BY SUM(qty_base) DESC').fetchall() == [("P1",112),("P2",108)]
        assert "CÁNH GÀ" in db.execute(f'SELECT __raw_json FROM "{table.name}" WHERE order_id=\'B\'').fetchone()[0]


def test_future_return_excludes_the_entire_original_order(workspace_tmp, rules):
    directory=workspace_tmp/"returns";directory.mkdir()
    (directory/"README.md").write_text("Trả hàng ghi số lượng âm.",encoding="utf-8")
    header=["order_id","ordered_at","status","product_name","qty_sale","line_amount"]
    write(directory/"sales_2025.csv",[header,["A","2025-12-31","completed","X",2,200]])
    write(directory/"sales_2026.csv",[header,["A","2026-01-02","Khách trả","X",-1,-100]])
    context=BusinessBuilder(workspace_tmp/"cache",rule_hash=rules.revision).prepare(directory)
    with closing(sqlite3.connect(context.database)) as db:
        assert all(db.execute(f'SELECT status FROM "{t.name}"').fetchone()[0]=="returned" for t in context.tables)


def test_conversion_chain_preserves_intermediate_units():
    graph=UnitGraph()
    graph.add("P1","thùng",Decimal(6),"viên","hộp")
    graph.add("P1","hộp",Decimal(4),"viên","vỉ")
    graph.add("P1","vỉ",Decimal(10),"viên")
    assert graph.factor("P1","thùng")==240
    assert graph.factor("P1","hộp")==40
    assert graph.factor("P1","viên")==1
    graph.add("P2","a",Decimal(2),"c","b")
    graph.add("P2","b",Decimal(2),"c","a")
    with pytest.raises(ValueError,match="Cyclic"):
        graph.factor("P2","a")


def test_retail_aliases_do_not_remove_a_real_accented_name():
    assert is_retail_customer("KL") and is_retail_customer("Khách lẻ")
    assert not is_retail_customer("Lê")


def test_currency_profile_accepts_plain_small_integers_among_grouped_prices():
    values=["4,400","5,200","10,000","6,700","500","600","800"]*10
    assert infer_locale(values,"",currency=True)=="en"
    assert infer_locale(["1.234"],"",currency=True)=="unknown"
    conflicting=["4,400","5,200","10,000","6,700","1.25","2.50"]*10
    assert infer_locale(conflicting,"",currency=True)=="unknown"
