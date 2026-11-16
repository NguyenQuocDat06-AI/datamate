"""Tests verifying the 7 schema-driven auto-healing fixes in plan_input.py."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import pytest

from bizreason.contracts import (
    BusinessContext,
    ColumnMeta,
    JoinEdge,
    Question,
    SourceRef,
    TableMeta,
)
from bizreason.plan_input import (
    _auto_heal_sql_syntax_and_semantics,
    _scale_scalar_percent_sql,
    canonicalize_plan_input,
)


import sqlite3

def _make_context(
    tmp_path: Path,
    tables: list[TableMeta],
    joins: list[JoinEdge] | None = None,
    readme: str = "",
) -> BusinessContext:
    db_path = tmp_path / "test.sqlite"
    if not db_path.exists():
        conn = sqlite3.connect(str(db_path))
        conn.close()
    return BusinessContext(
        business_id="test_business",
        revision="rev_1",
        database=db_path,
        readme=readme,
        tables=tables,
        joins=joins or [],
        manifest={},
    )


def test_fix1_cte_percentage_scaling(tmp_path: Path):
    c_amt = ColumnMeta(name="amount", original_name="amount", dtype="REAL", semantic_role="line_amount")
    c_yr = ColumnMeta(name="year_val", original_name="year_val", dtype="INTEGER", semantic_role="none")
    tbl = TableMeta(
        name="sales_record",
        sources=[SourceRef(path="sales.csv", table="sales_record")],
        columns=[c_amt, c_yr],
        grain="order_line",
        row_count=100,
        raw_row_count=100,
    )
    ctx = _make_context(tmp_path, [tbl])

    sql = (
        "WITH s24 AS (SELECT SUM(amount) AS s_curr FROM sales_record WHERE year_val = 2024), "
        "s23 AS (SELECT SUM(amount) AS s_prev FROM sales_record WHERE year_val = 2023) "
        "SELECT (s_curr - s_prev) / s_prev AS growth_rate FROM s24, s23"
    )
    scaled = _scale_scalar_percent_sql(sql)
    assert "100.0 * ((s_curr - s_prev) / s_prev) AS growth_rate" in scaled


def test_fix1_percentage_operator_precedence():
    sql1 = "SELECT sales_after - sales_before * 100.0 / sales_before FROM summary_table"
    healed1 = _scale_scalar_percent_sql(sql1)
    assert "(sales_after - sales_before) * 100.0 / NULLIF(sales_before, 0)" in healed1

    sql2 = "SELECT 100.0 * rev_new - rev_old / rev_old FROM summary_table"
    healed2 = _scale_scalar_percent_sql(sql2)
    assert "100.0 * (rev_new - rev_old) / NULLIF(rev_old, 0)" in healed2

    sql3 = "SELECT ROUND(sales_after - sales_before * 100.0 / sales_before, 2) FROM summary_table"
    healed3 = _scale_scalar_percent_sql(sql3)
    assert "ROUND((sales_after - sales_before) * 100.0 / NULLIF(sales_before, 0), 2)" in healed3


def test_fix1_question_cues_trigger_percent_scale(tmp_path: Path):
    c_amt = ColumnMeta(name="amount", original_name="amount", dtype="REAL", semantic_role="line_amount")
    tbl = TableMeta(
        name="merch_sales",
        sources=[SourceRef(path="m.csv", table="merch_sales")],
        columns=[c_amt],
        grain="order_line",
        row_count=100,
        raw_row_count=100,
    )
    ctx = _make_context(tmp_path, [tbl])
    q = Question(
        id="q_percent_1",
        business_id="test_biz",
        question="Nhóm sản phẩm này chiếm bao nhiêu phần trăm doanh thu?",
        as_of_date=date(2026, 1, 1),
    )
    raw_plan = {
        "metric": "generic_sql",
        "tables": ["merch_sales"],
        "sql": "SELECT SUM(amount) / 5000000.0 FROM merch_sales",
        "output": {"type": "number", "shape": "scalar", "rounding": "two_decimals"},
    }
    res = canonicalize_plan_input(raw_plan, ctx, question=q)
    assert res["output"]["type"] == "percent"
    assert "100.0 *" in res["sql"] and "/ 5000000.0" in res["sql"]


def test_fix2_customer_identity_grouping_sql_healing(tmp_path: Path):
    c_id = ColumnMeta(name="ma_khach_hang", original_name="ma_khach_hang", dtype="TEXT", semantic_role="customer_id")
    c_name = ColumnMeta(name="ten_khach_hang", original_name="ten_khach_hang", dtype="TEXT", semantic_role="customer_name")
    c_rev = ColumnMeta(name="tong_chi_tieu", original_name="tong_chi_tieu", dtype="REAL", semantic_role="total_due")
    tbl = TableMeta(
        name="khach_hang_dim",
        sources=[SourceRef(path="c.csv", table="khach_hang_dim")],
        columns=[c_id, c_name, c_rev],
        grain="customer_dimension",
        row_count=100,
        raw_row_count=100,
    )
    ctx = _make_context(tmp_path, [tbl])

    sql = "SELECT c.ten_khach_hang, SUM(c.tong_chi_tieu) FROM khach_hang_dim c GROUP BY c.ten_khach_hang ORDER BY 2 DESC"
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [tbl])
    assert "GROUP BY c.ma_khach_hang, c.ten_khach_hang" in healed

    # Plan group_by normalization
    raw_plan = {
        "metric": "generic_sql",
        "tables": ["khach_hang_dim"],
        "sql": sql,
        "group_by": ["khach_hang_dim.ten_khach_hang"],
        "required_fields": ["khach_hang_dim.ten_khach_hang"],
    }
    canon = canonicalize_plan_input(raw_plan, ctx)
    assert "khach_hang_dim.ma_khach_hang" in canon["group_by"]
    assert "khach_hang_dim.ma_khach_hang" in canon["required_fields"]


def test_fix3_purchase_unit_cost_qualified_columns(tmp_path: Path):
    c_p_id = ColumnMeta(name="so_phieu", original_name="so_phieu", dtype="TEXT", semantic_role="order_id")
    c_p_sl = ColumnMeta(name="sl_nhap", original_name="sl_nhap", dtype="REAL", semantic_role="quantity")
    c_p_gia = ColumnMeta(name="dongianhap", original_name="dongianhap", dtype="REAL", semantic_role="unit_cost")
    dtl_tbl = TableMeta(
        name="chitietnhaphang",
        sources=[SourceRef(path="ct.csv", table="chitietnhaphang")],
        columns=[c_p_id, c_p_sl, c_p_gia],
        grain="purchase_line",
        row_count=50,
        raw_row_count=50,
    )
    ctx = _make_context(tmp_path, [dtl_tbl])
    q = Question(
        id="q_pur_1",
        business_id="test_biz",
        question="Giá nhập bình quân một kg là bao nhiêu?",
        as_of_date=date(2026, 1, 1),
    )
    sql = "SELECT AVG(dongianhap) FROM chitietnhaphang AS ct"
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [dtl_tbl], question=q)
    assert "ROUND(SUM(ct.sl_nhap * ct.dongianhap) * 1.0 / NULLIF(SUM(ct.sl_nhap), 0))" in healed


def test_fix3_purchase_detail_joined_with_sales_header_recovery(tmp_path: Path):
    c_p_id = ColumnMeta(name="ma_nhap_hang", original_name="ma_nhap_hang", dtype="TEXT", semantic_role="order_id")
    c_p_sl = ColumnMeta(name="so_luong", original_name="so_luong", dtype="REAL", semantic_role="quantity")
    c_p_gia = ColumnMeta(name="don_gia", original_name="don_gia", dtype="REAL", semantic_role="unit_cost")
    dtl_tbl = TableMeta(
        name="danhsachchitietnhaphang",
        sources=[SourceRef(path="ct.csv", table="danhsachchitietnhaphang")],
        columns=[c_p_id, c_p_sl, c_p_gia],
        grain="purchase_line",
        row_count=50,
        raw_row_count=50,
    )
    c_pur_hdr_id = ColumnMeta(name="ma_nhap_hang", original_name="ma_nhap_hang", dtype="TEXT", semantic_role="order_id")
    pur_hdr_tbl = TableMeta(
        name="danhsachnhaphang",
        sources=[SourceRef(path="nh.csv", table="danhsachnhaphang")],
        columns=[c_pur_hdr_id],
        grain="purchase_header",
        row_count=20,
        raw_row_count=20,
    )
    c_sale_hdr_id = ColumnMeta(name="ma_hoa_don", original_name="ma_hoa_don", dtype="TEXT", semantic_role="order_id")
    sales_hdr_tbl = TableMeta(
        name="danhsachhoadon",
        sources=[SourceRef(path="hd.csv", table="danhsachhoadon")],
        columns=[c_sale_hdr_id],
        grain="order_header",
        row_count=20,
        raw_row_count=20,
    )
    ctx = _make_context(tmp_path, [dtl_tbl, pur_hdr_tbl, sales_hdr_tbl])
    q = Question(
        id="q_pur_2",
        business_id="test_biz",
        question="Giá nhập bình quân các đơn hàng?",
        as_of_date=date(2026, 1, 1),
    )
    sql = "SELECT AVG(don_gia) FROM danhsachchitietnhaphang ct JOIN danhsachhoadon hd ON ct.ma_nhap_hang = hd.ma_hoa_don"
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [dtl_tbl, sales_hdr_tbl], question=q)
    assert "danhsachnhaphang" in healed
    assert "hd.ma_nhap_hang" in healed or "danhsachnhaphang.ma_nhap_hang" in healed


def test_fix4_table_hash_suffix_recovery(tmp_path: Path):
    c_id = ColumnMeta(name="order_id", original_name="order_id", dtype="TEXT", semantic_role="order_id")
    c_amt = ColumnMeta(name="line_amount", original_name="line_amount", dtype="REAL", semantic_role="line_amount")
    tbl = TableMeta(
        name="pos_chi_tiet_don_ab12cd34",
        sources=[SourceRef(path="p.csv", table="pos_chi_tiet_don_ab12cd34")],
        columns=[c_id, c_amt],
        grain="order_line",
        row_count=100,
        raw_row_count=100,
    )
    ctx = _make_context(tmp_path, [tbl])

    sql = "SELECT SUM(pos_chi_tiet_don.line_amount) FROM pos_chi_tiet_don WHERE order_id = '100'"
    healed, added = _auto_heal_sql_syntax_and_semantics(sql, ctx, [tbl])
    assert "pos_chi_tiet_don_ab12cd34" in healed
    assert "pos_chi_tiet_don_ab12cd34" in added

    raw_plan = {
        "metric": "generic_sql",
        "tables": ["pos_chi_tiet_don"],
        "sql": "SELECT line_amount FROM pos_chi_tiet_don",
    }
    canon = canonicalize_plan_input(raw_plan, ctx)
    assert "pos_chi_tiet_don_ab12cd34" in canon["tables"]


def test_fix4_hallucinated_column_synonym_recovery(tmp_path: Path):
    c_sl = ColumnMeta(name="sl_co_ban", original_name="sl_co_ban", dtype="REAL", semantic_role="base_quantity")
    c_gia = ColumnMeta(name="gia_ban", original_name="gia_ban", dtype="REAL", semantic_role="unit_price")
    c_amt = ColumnMeta(name="t_tien", original_name="t_tien", dtype="REAL", semantic_role="line_amount")
    c_id = ColumnMeta(name="ma_giao_dich", original_name="ma_giao_dich", dtype="TEXT", semantic_role="order_id")
    c_date = ColumnMeta(name="ngay_tao_don", original_name="ngay_tao_don", dtype="DATETIME", semantic_role="order_date")
    tbl = TableMeta(
        name="ban_hang_ct",
        sources=[SourceRef(path="b.csv", table="ban_hang_ct")],
        columns=[c_sl, c_gia, c_amt, c_id, c_date],
        grain="order_line",
        row_count=100,
        raw_row_count=100,
    )
    ctx = _make_context(tmp_path, [tbl])

    sql = (
        "SELECT SUM(b.slg_co_ban * b.don_gia), SUM(b.thanh_tien) "
        "FROM ban_hang_ct b WHERE b.ma_hoa_don = 'D1' AND b.thoi_gian_tao >= '2025-01-01'"
    )
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [tbl])
    assert "b.sl_co_ban" in healed
    assert "b.gia_ban" in healed
    assert "b.t_tien" in healed
    assert "b.ma_giao_dich" in healed
    assert "b.ngay_tao_don" in healed


def test_fix5_detail_date_filter_auto_join(tmp_path: Path):
    c_det_id = ColumnMeta(name="ma_hoa_don", original_name="ma_hoa_don", dtype="TEXT", semantic_role="order_id")
    c_det_amt = ColumnMeta(name="thanh_tien", original_name="thanh_tien", dtype="REAL", semantic_role="line_amount")
    dtl_tbl = TableMeta(
        name="hoa_don_ct",
        sources=[SourceRef(path="ct.csv", table="hoa_don_ct")],
        columns=[c_det_id, c_det_amt],
        grain="order_line",
        row_count=100,
        raw_row_count=100,
    )
    c_hdr_id = ColumnMeta(name="ma_hoa_don", original_name="ma_hoa_don", dtype="TEXT", semantic_role="order_id")
    c_hdr_date = ColumnMeta(name="thoi_gian_tao", original_name="thoi_gian_tao", dtype="DATETIME", semantic_role="order_date")
    hdr_tbl = TableMeta(
        name="hoa_don_hdr",
        sources=[SourceRef(path="hdr.csv", table="hoa_don_hdr")],
        columns=[c_hdr_id, c_hdr_date],
        grain="order_header",
        row_count=30,
        raw_row_count=30,
    )
    join = JoinEdge(
        left="hoa_don_ct",
        right="hoa_don_hdr",
        keys=[("ma_hoa_don", "ma_hoa_don")],
        cardinality="many_to_one",
        orphan_count=0,
        evidence=[],
    )
    ctx = _make_context(tmp_path, [dtl_tbl, hdr_tbl], joins=[join])

    sql = "SELECT SUM(thanh_tien) FROM hoa_don_ct WHERE iso_date(thoi_gian_tao) >= '2025-01-01'"
    healed, added = _auto_heal_sql_syntax_and_semantics(sql, ctx, [dtl_tbl])
    assert "JOIN hoa_don_hdr AS _hdr ON hoa_don_ct.ma_hoa_don = _hdr.ma_hoa_don" in healed
    assert "iso_date(_hdr.thoi_gian_tao)" in healed
    assert "hoa_don_hdr" in added


def test_fix6_order_discount_preserves_header_revenue(tmp_path: Path):
    c_hdr_id = ColumnMeta(name="ma_chung_tu", original_name="ma_chung_tu", dtype="TEXT", semantic_role="order_id")
    c_hdr_rev = ColumnMeta(name="tong_tien", original_name="tong_tien", dtype="REAL", semantic_role="total_due")
    c_hdr_ck = ColumnMeta(name="chiet_khau", original_name="chiet_khau", dtype="REAL", semantic_role="discount")
    hdr_tbl = TableMeta(
        name="banhang_hoadon",
        sources=[SourceRef(path="hd.csv", table="banhang_hoadon")],
        columns=[c_hdr_id, c_hdr_rev, c_hdr_ck],
        grain="order_header",
        row_count=50,
        raw_row_count=50,
    )
    c_det_id = ColumnMeta(name="ma_chung_tu", original_name="ma_chung_tu", dtype="TEXT", semantic_role="order_id")
    c_det_amt = ColumnMeta(name="amount", original_name="amount", dtype="REAL", semantic_role="line_amount")
    det_tbl = TableMeta(
        name="banhang_chitiet",
        sources=[SourceRef(path="ct.csv", table="banhang_chitiet")],
        columns=[c_det_id, c_det_amt],
        grain="order_line",
        row_count=200,
        raw_row_count=200,
    )
    join = JoinEdge(
        left="banhang_chitiet",
        right="banhang_hoadon",
        keys=[("ma_chung_tu", "ma_chung_tu")],
        cardinality="many_to_one",
        orphan_count=0,
        evidence=[],
    )
    ctx = _make_context(tmp_path, [hdr_tbl, det_tbl], joins=[join])
    q = Question(
        id="q_rev_1",
        business_id="test_biz",
        question="Tổng doanh thu tháng 5 là bao nhiêu?",
        as_of_date=date(2025, 6, 1),
    )
    sql = "SELECT SUM(tong_tien) FROM banhang_hoadon"
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [hdr_tbl, det_tbl], question=q)
    # Must preserve SUM(tong_tien) on header table because it accounts for order-level discount
    assert "SUM(tong_tien)" in healed
    assert "banhang_chitiet" not in healed


def test_fix7_ambiguous_column_qualification(tmp_path: Path):
    c_h_id = ColumnMeta(name="ma_hoa_don", original_name="ma_hoa_don", dtype="TEXT", semantic_role="order_id")
    c_h_st = ColumnMeta(name="trang_thai", original_name="trang_thai", dtype="TEXT", semantic_role="status")
    c_h_tot = ColumnMeta(name="tong_tien", original_name="tong_tien", dtype="REAL", semantic_role="total_due")
    c_h_qty = ColumnMeta(name="so_luong", original_name="so_luong", dtype="REAL", semantic_role="quantity")
    c_h_prc = ColumnMeta(name="don_gia", original_name="don_gia", dtype="REAL", semantic_role="unit_price")
    hdr_tbl = TableMeta(
        name="don_hang_hdr",
        sources=[SourceRef(path="h.csv", table="don_hang_hdr")],
        columns=[c_h_id, c_h_st, c_h_tot, c_h_qty, c_h_prc],
        grain="order_header",
        row_count=50,
        raw_row_count=50,
    )
    c_d_id = ColumnMeta(name="ma_hoa_don", original_name="ma_hoa_don", dtype="TEXT", semantic_role="order_id")
    c_d_qty = ColumnMeta(name="so_luong", original_name="so_luong", dtype="REAL", semantic_role="quantity")
    c_d_prc = ColumnMeta(name="don_gia", original_name="don_gia", dtype="REAL", semantic_role="unit_price")
    c_d_st = ColumnMeta(name="trang_thai", original_name="trang_thai", dtype="TEXT", semantic_role="status")
    det_tbl = TableMeta(
        name="don_hang_chitiet",
        sources=[SourceRef(path="d.csv", table="don_hang_chitiet")],
        columns=[c_d_id, c_d_qty, c_d_prc, c_d_st],
        grain="order_line",
        row_count=200,
        raw_row_count=200,
    )
    ctx = _make_context(tmp_path, [hdr_tbl, det_tbl])

    sql = (
        "SELECT ma_hoa_don, SUM(so_luong * don_gia) "
        "FROM don_hang_hdr h JOIN don_hang_chitiet d ON h.ma_hoa_don = d.ma_hoa_don "
        "WHERE trang_thai = 'completed' GROUP BY ma_hoa_don"
    )
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [hdr_tbl, det_tbl])
    # Ambiguous column names must be resolved with preferred alias
    assert "h.ma_hoa_don" in healed
    assert "d.so_luong" in healed
    assert "d.don_gia" in healed
    assert "h.trang_thai" in healed
