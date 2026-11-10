from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from bizreason.contracts import (
    BusinessContext,
    ColumnMeta,
    JoinEdge,
    OutputSpec,
    Question,
    SourceRef,
    TableMeta,
)
from bizreason.execution import _INDEXED_DBS, ensure_join_indexes, execute_sql
from bizreason.plan_input import _auto_heal_sql_syntax_and_semantics, canonicalize_plan_input


def _make_dummy_context(
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
        business_id="biz_test",
        revision="rev_1",
        database=db_path,
        readme=readme,
        tables=tables,
        joins=joins or [],
        manifest={},
    )


# ==============================================================================
# 1. Dynamic Join Indexing
# ==============================================================================

def test_join_indexing_automatic(tmp_path: Path):
    db_path = tmp_path / "index_test.sqlite"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("CREATE TABLE orders (ma_don TEXT, tong_tien REAL)")
    cur.execute("CREATE TABLE details (ma_don TEXT, thanh_tien REAL)")
    # Insert > 500 rows to cross the auto-index threshold
    cur.executemany(
        "INSERT INTO orders VALUES (?, ?)",
        [(f"ORD_{i}", float(i * 100)) for i in range(600)],
    )
    cur.executemany(
        "INSERT INTO details VALUES (?, ?)",
        [(f"ORD_{i % 600}", float(i * 50)) for i in range(1200)],
    )
    conn.commit()
    conn.close()

    # Clear cached indexed state for this path
    _INDEXED_DBS.discard(str(db_path.resolve()))

    # Verify no indexes initially
    conn = sqlite3.connect(str(db_path))
    indexes_before = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    conn.close()
    assert len(indexes_before) == 0

    # Run ensure_join_indexes
    ensure_join_indexes(db_path)

    # Verify indexes created on both tables for ma_don
    conn = sqlite3.connect(str(db_path))
    indexes_after = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()]
    conn.close()

    assert any("orders_ma_don" in idx for idx in indexes_after)
    assert any("details_ma_don" in idx for idx in indexes_after)
    assert str(db_path.resolve()) in _INDEXED_DBS


# ==============================================================================
# 2. Formatted TEXT Currency CAST vs money_vnd Wrapping
# ==============================================================================

def test_money_vnd_healing_cast_and_bare(tmp_path: Path):
    col = ColumnMeta(
        name="tong_tien_hang",
        original_name="tong_tien_hang",
        dtype="TEXT",
        semantic_role="gross_revenue",
        examples=["845,500", "120,000", "50,000"],
    )
    tbl = TableMeta(
        name="orders",
        sources=[SourceRef(path="orders.csv", table="orders")],
        columns=[col],
        grain="order_header",
        row_count=100,
        raw_row_count=100,
    )
    ctx = _make_dummy_context(tmp_path, [tbl])

    # Case A: Bare SUM(col)
    sql_bare = "SELECT SUM(tong_tien_hang) FROM orders"
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql_bare, ctx, [tbl])
    assert "money_vnd(tong_tien_hang, 'en')" in healed

    # Case B: SUM(CAST(col AS REAL))
    sql_cast = "SELECT SUM(CAST(tong_tien_hang AS REAL)) FROM orders"
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql_cast, ctx, [tbl])
    assert "money_vnd(tong_tien_hang, 'en')" in healed
    assert "CAST" not in healed


# ==============================================================================
# 3. Shift Filtering Healing ("ca chiều", "ca sáng")
# ==============================================================================

def test_shift_filtering_afternoon_and_morning(tmp_path: Path):
    col_dt = ColumnMeta(
        name="thoi_gian_tao",
        original_name="thoi_gian_tao",
        dtype="TEXT",
        semantic_role="ordered_at",
        examples=["2026-05-01 15:30:00", "2026-05-01 10:15:00"],
    )
    col_amt = ColumnMeta(
        name="tong_tien",
        original_name="tong_tien",
        dtype="REAL",
        semantic_role="gross_revenue",
    )
    tbl = TableMeta(
        name="orders",
        sources=[SourceRef(path="orders.csv", table="orders")],
        columns=[col_dt, col_amt],
        grain="order_header",
        row_count=100,
        raw_row_count=100,
    )
    ctx = _make_dummy_context(tmp_path, [tbl])

    # Afternoon shift question
    q_afternoon = Question(
        id="q_test_1",
        business_id="biz_test",
        question="Doanh thu ca chiều hôm nay là bao nhiêu?",
        as_of_date=date(2026, 5, 1),
    )
    sql = "SELECT SUM(tong_tien) FROM orders WHERE ngay = '2026-05-01'"
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [tbl], question=q_afternoon)
    assert "TIME(thoi_gian_tao) >= '14:00:00'" in healed

    # Morning shift question
    q_morning = Question(
        id="q_test_2",
        business_id="biz_test",
        question="Doanh thu ca sáng hôm nay là bao nhiêu?",
        as_of_date=date(2026, 5, 1),
    )
    healed_m, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [tbl], question=q_morning)
    assert "TIME(thoi_gian_tao) < '14:00:00'" in healed_m


# ==============================================================================
# 4. Merchandise Revenue Column Resolution (tong_sau_giam_gia -> tong_tien_hang)
# ==============================================================================

def test_merchandise_revenue_column_resolution(tmp_path: Path):
    c_good = ColumnMeta(
        name="tong_tien_hang",
        original_name="tong_tien_hang",
        dtype="REAL",
        semantic_role="gross_revenue",
    )
    c_wrong = ColumnMeta(
        name="tong_sau_giam_gia",
        original_name="tong_sau_giam_gia",
        dtype="REAL",
        semantic_role="net_revenue",
    )
    tbl = TableMeta(
        name="invoices",
        sources=[SourceRef(path="invoices.csv", table="invoices")],
        columns=[c_good, c_wrong],
        grain="order_header",
        row_count=100,
        raw_row_count=100,
    )
    ctx = _make_dummy_context(tmp_path, [tbl])

    q = Question(
        id="q_test_3",
        business_id="biz_test",
        question="Tổng doanh thu bán hàng tháng 5 là bao nhiêu?",
        as_of_date=date(2026, 5, 31),
    )
    sql = "SELECT SUM(tong_sau_giam_gia) FROM invoices"
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [tbl], question=q)
    assert "tong_tien_hang" in healed
    assert "tong_sau_giam_gia" not in healed


# ==============================================================================
# 5. Order Average Revenue & Line-Item Merchandise Disambiguation
# ==============================================================================

def test_order_average_revenue_disambiguation(tmp_path: Path):
    c_hdr_id = ColumnMeta(name="ma_chung_tu", original_name="ma_chung_tu", dtype="TEXT", semantic_role="order_id")
    c_hdr_tot = ColumnMeta(name="tong_don", original_name="tong_don", dtype="REAL", semantic_role="total_due")
    hdr_tbl = TableMeta(
        name="don_hang",
        sources=[SourceRef(path="orders.csv", table="don_hang")],
        columns=[c_hdr_id, c_hdr_tot],
        grain="order_header",
        row_count=100,
        raw_row_count=100,
    )

    c_det_id = ColumnMeta(name="ma_chung_tu", original_name="ma_chung_tu", dtype="TEXT", semantic_role="order_id")
    c_det_amt = ColumnMeta(name="amount", original_name="amount", dtype="REAL", semantic_role="line_amount")
    det_tbl = TableMeta(
        name="chi_tiet_don",
        sources=[SourceRef(path="details.csv", table="chi_tiet_don")],
        columns=[c_det_id, c_det_amt],
        grain="order_line",
        row_count=500,
        raw_row_count=500,
    )

    join = JoinEdge(
        left="chi_tiet_don",
        right="don_hang",
        keys=[("ma_chung_tu", "ma_chung_tu")],
        cardinality="many_to_one",
        orphan_count=0,
        evidence=[],
    )
    ctx = _make_dummy_context(tmp_path, [hdr_tbl, det_tbl], joins=[join])

    q = Question(
        id="q_test_4",
        business_id="biz_test",
        question="Doanh thu trung bình trên một đơn là bao nhiêu?",
        as_of_date=date(2026, 5, 31),
    )
    sql = "SELECT AVG(tong_don) FROM don_hang"
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [hdr_tbl, det_tbl], question=q)

    assert "chi_tiet_don.amount" in healed or "amount" in healed
    assert "COUNT(DISTINCT" in healed
    assert "AVG(tong_don)" not in healed


# ==============================================================================
# 6. Purchase Unit Cost Formula Healing ("giá nhập bình quân")
# ==============================================================================

def test_purchase_unit_cost_formula_healing(tmp_path: Path):
    c_hdr_id = ColumnMeta(name="so_phieu", original_name="so_phieu", dtype="TEXT", semantic_role="order_id")
    c_hdr_pay = ColumnMeta(name="can_tra_ncc", original_name="can_tra_ncc", dtype="REAL", semantic_role="total_due")
    hdr_tbl = TableMeta(
        name="nhap_hang",
        sources=[SourceRef(path="nhap.csv", table="nhap_hang")],
        columns=[c_hdr_id, c_hdr_pay],
        grain="order_header",
        row_count=50,
        raw_row_count=50,
    )

    c_det_id = ColumnMeta(name="so_phieu", original_name="so_phieu", dtype="TEXT", semantic_role="order_id")
    c_det_qty = ColumnMeta(name="so_luong", original_name="so_luong", dtype="REAL", semantic_role="quantity")
    c_det_prc = ColumnMeta(name="don_gia", original_name="don_gia", dtype="REAL", semantic_role="unit_price")
    det_tbl = TableMeta(
        name="chi_tiet_nhap_hang",
        sources=[SourceRef(path="nhap_ct.csv", table="chi_tiet_nhap_hang")],
        columns=[c_det_id, c_det_qty, c_det_prc],
        grain="order_line",
        row_count=200,
        raw_row_count=200,
    )

    join = JoinEdge(
        left="chi_tiet_nhap_hang",
        right="nhap_hang",
        keys=[("so_phieu", "so_phieu")],
        cardinality="many_to_one",
        orphan_count=0,
        evidence=[],
    )
    ctx = _make_dummy_context(tmp_path, [hdr_tbl, det_tbl], joins=[join])

    q = Question(
        id="q_test_5",
        business_id="biz_test",
        question="Giá nhập bình quân một kg thịt là bao nhiêu?",
        as_of_date=date(2026, 5, 31),
    )
    sql = "SELECT AVG(can_tra_ncc) FROM nhap_hang"
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [hdr_tbl, det_tbl], question=q)

    assert "SUM(so_luong * don_gia)" in healed
    assert "NULLIF(SUM(so_luong), 0)" in healed
    assert "can_tra_ncc" not in healed


# ==============================================================================
# 7. Anonymous & Walk-In Customer Exclusion in Frequency Queries
# ==============================================================================

def test_anonymous_customer_exclusion_in_frequency(tmp_path: Path):
    c_cust = ColumnMeta(name="khach_hang", original_name="khach_hang", dtype="TEXT", semantic_role="customer_id")
    tbl = TableMeta(
        name="orders",
        sources=[SourceRef(path="orders.csv", table="orders")],
        columns=[c_cust],
        grain="order_header",
        row_count=100,
        raw_row_count=100,
    )
    ctx = _make_dummy_context(tmp_path, [tbl])

    q = Question(
        id="q_test_6",
        business_id="biz_test",
        question="Bao nhiêu % số khách đã mua từ 2 đơn trở lên?",
        as_of_date=date(2026, 5, 31),
    )
    sql = "SELECT COUNT(DISTINCT khach_hang) FROM orders WHERE so_don >= 2"
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [tbl], question=q)

    assert "lookup(khach_hang) NOT IN ('khach le', 'kl', 'le', 'khach vang lai')" in healed
    assert "khach_hang IS NOT NULL" in healed


# ==============================================================================
# 8. Single-Scalar Aggregate GROUP BY 1 Stripping
# ==============================================================================

def test_group_by_1_stripping_on_single_scalar(tmp_path: Path):
    col = ColumnMeta(name="amount", original_name="amount", dtype="REAL", semantic_role="gross_revenue")
    tbl = TableMeta(
        name="sales",
        sources=[SourceRef(path="sales.csv", table="sales")],
        columns=[col],
        grain="order_header",
        row_count=100,
        raw_row_count=100,
    )
    ctx = _make_dummy_context(tmp_path, [tbl])

    sql = "SELECT SUM(amount) FROM sales GROUP BY 1;"
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [tbl])

    assert "GROUP BY 1" not in healed
    assert healed.strip() == "SELECT SUM(amount) FROM sales"


# ==============================================================================
# 9. Drink Size Suffix Normalization in LIKE Filter
# ==============================================================================

def test_drink_size_suffix_healing(tmp_path: Path):
    col = ColumnMeta(name="ten_mon", original_name="ten_mon", dtype="TEXT", semantic_role="product_name")
    tbl = TableMeta(
        name="items",
        sources=[SourceRef(path="items.csv", table="items")],
        columns=[col],
        grain="item",
        row_count=50,
        raw_row_count=50,
    )
    ctx = _make_dummy_context(tmp_path, [tbl])

    sql = "SELECT SUM(so_luong) FROM items WHERE lookup(ten_mon) LIKE '%trà đào (l)%'"
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [tbl])

    assert "%(l)%" not in healed
    assert "%tra dao%" in healed


# ==============================================================================
# 10. Diacritic Column Name Recovery
# ==============================================================================

def test_diacritic_column_recovery(tmp_path: Path):
    c1 = ColumnMeta(name="don_gia", original_name="Đơn Giá", dtype="REAL", semantic_role="unit_price")
    c2 = ColumnMeta(name="thanh_tien", original_name="Thành Tiền", dtype="REAL", semantic_role="line_amount")
    c3 = ColumnMeta(name="so_luong", original_name="Số Lượng", dtype="REAL", semantic_role="quantity")
    tbl = TableMeta(
        name="order_items",
        sources=[SourceRef(path="items.csv", table="order_items")],
        columns=[c1, c2, c3],
        grain="order_line",
        row_count=100,
        raw_row_count=100,
    )
    ctx = _make_dummy_context(tmp_path, [tbl])

    sql = "SELECT SUM(thành_tiền) FROM order_items WHERE đơn_giá > 1000 AND số_lượng >= 1"
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [tbl])

    assert "thanh_tien" in healed
    assert "don_gia" in healed
    assert "so_luong" in healed
    assert "thành_tiền" not in healed
    assert "đơn_giá" not in healed
    assert "số_lượng" not in healed


# ==============================================================================
# 11. Partitioned Monthly Active Table Discovery
# ==============================================================================

def test_partitioned_active_tables_resolution(tmp_path: Path):
    col = ColumnMeta(
        name="tong_tien_hang",
        original_name="tong_tien_hang",
        dtype="TEXT",
        semantic_role="gross_revenue",
        examples=["500,000", "250,000"],
    )
    partition_tbl = TableMeta(
        name="danhsachhoadon_08_2026_xyz123",
        sources=[SourceRef(path="part.csv", table="danhsachhoadon_08_2026_xyz123")],
        columns=[col],
        grain="order_header",
        row_count=200,
        raw_row_count=200,
    )
    ctx = _make_dummy_context(tmp_path, [partition_tbl])

    # SQL mentions the partitioned table directly, but target_tables is empty
    sql = "SELECT SUM(CAST(tong_tien_hang AS REAL)) FROM danhsachhoadon_08_2026_xyz123"
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [])

    assert "money_vnd(tong_tien_hang, 'en')" in healed
    assert "CAST" not in healed


# ==============================================================================
# 12. Broadened Purchase Unit Cost & Header Redirection Healing
# ==============================================================================

def test_purchase_unit_cost_broadened_regex_and_redirection(tmp_path: Path):
    c_qty = ColumnMeta(name="so_luong", original_name="so_luong", dtype="INTEGER", semantic_role="quantity")
    c_price = ColumnMeta(name="don_gia", original_name="don_gia", dtype="INTEGER", semantic_role="unit_price")
    c_mnh = ColumnMeta(name="ma_nhap_hang", original_name="ma_nhap_hang", dtype="TEXT", semantic_role="order_id")
    c_prod = ColumnMeta(name="ma_hang", original_name="ma_hang", dtype="TEXT", semantic_role="product_id")
    dtl_tbl = TableMeta(
        name="danhsachchitietnhaphang",
        sources=[SourceRef(path="p_dtl.csv", table="danhsachchitietnhaphang")],
        columns=[c_mnh, c_prod, c_qty, c_price],
        grain="order_line",
        row_count=100,
        raw_row_count=100,
    )
    c_time = ColumnMeta(name="thoi_gian", original_name="thoi_gian", dtype="TEXT", semantic_role="ordered_at")
    pur_hdr = TableMeta(
        name="danhsachnhaphang",
        sources=[SourceRef(path="p_hdr.csv", table="danhsachnhaphang")],
        columns=[c_mnh, c_time],
        grain="order_header",
        row_count=50,
        raw_row_count=50,
    )
    sales_hdr = TableMeta(
        name="danhsachhoadon",
        sources=[SourceRef(path="s_hdr.csv", table="danhsachhoadon")],
        columns=[ColumnMeta(name="ma_hoa_don", original_name="ma_hoa_don", dtype="TEXT"), c_time],
        grain="order_header",
        row_count=50,
        raw_row_count=50,
    )
    sales_dtl = TableMeta(
        name="danhsachhoadonchitiet",
        sources=[SourceRef(path="s_dtl.csv", table="danhsachhoadonchitiet")],
        columns=[ColumnMeta(name="ma_hoa_don", original_name="ma_hoa_don", dtype="TEXT"), c_prod, ColumnMeta(name="ten_hang", original_name="ten_hang", dtype="TEXT")],
        grain="order_line",
        row_count=100,
        raw_row_count=100,
    )
    prod_tbl = TableMeta(
        name="danhsachsanpham",
        sources=[SourceRef(path="prod.csv", table="danhsachsanpham")],
        columns=[c_prod, ColumnMeta(name="ten_hang", original_name="ten_hang", dtype="TEXT")],
        grain="product_dimension",
        row_count=30,
        raw_row_count=30,
    )

    ctx = _make_dummy_context(tmp_path, [dtl_tbl, pur_hdr, sales_hdr, sales_dtl, prod_tbl])
    q = Question(
        id="q_test_pur",
        business_id="biz_test",
        as_of_date=date(2025, 2, 1),
        question="Trong tháng 1/2025, Mì Omachi sườn hầm nhập vào giá bình quân một thùng là bao nhiêu ạ?",
    )

    sql = (
        "SELECT SUM(c.can_tra_ncc / c.so_luong) "
        "FROM danhsachchitietnhaphang AS c "
        "JOIN danhsachhoadonchitiet AS d ON c.ma_hang = d.ma_hang "
        "JOIN danhsachsanpham AS s ON c.ma_hang = s.ma_hang "
        "JOIN danhsachhoadon AS h ON h.ma_hoa_don = d.ma_hoa_don "
        "WHERE d.ten_hang = 'Mì Omachi sườn hầm' "
        "GROUP BY c.ma_nhap_hang"
    )
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [dtl_tbl, sales_hdr, sales_dtl, prod_tbl], question=q)

    assert "ROUND(SUM(c.so_luong * c.don_gia) * 1.0 / NULLIF(SUM(c.so_luong), 0))" in healed
    assert "danhsachnhaphang" in healed
    assert "danhsachhoadonchitiet" not in healed
    assert "s.ten_hang" in healed
    assert "GROUP BY" not in healed


# ==============================================================================
# 13. Average Order Revenue Join Keyword Lookahead Healing
# ==============================================================================

def test_average_order_revenue_keyword_exclusion(tmp_path: Path):
    c_mct = ColumnMeta(name="ma_chung_tu", original_name="ma_chung_tu", dtype="TEXT", semantic_role="order_id")
    c_bill = ColumnMeta(name="tong_don", original_name="tong_don", dtype="REAL", semantic_role="total_due")
    hdr_tbl = TableMeta(
        name="don_hang",
        sources=[SourceRef(path="orders.csv", table="don_hang")],
        columns=[c_mct, c_bill],
        grain="order_header",
        row_count=50,
        raw_row_count=50,
    )
    c_mdh = ColumnMeta(name="ma_don_hang", original_name="ma_don_hang", dtype="TEXT", semantic_role="order_id")
    c_amt = ColumnMeta(name="amount", original_name="amount", dtype="REAL", semantic_role="line_amount")
    line_tbl = TableMeta(
        name="chi_tiet_don",
        sources=[SourceRef(path="lines.csv", table="chi_tiet_don")],
        columns=[c_mdh, c_amt],
        grain="order_line",
        row_count=100,
        raw_row_count=100,
    )
    join_edge = JoinEdge(
        left="chi_tiet_don",
        right="don_hang",
        keys=[("ma_don_hang", "ma_chung_tu")],
        cardinality="many_to_one",
        orphan_count=0,
        evidence=["foreign_key"],
    )
    ctx = _make_dummy_context(tmp_path, [hdr_tbl, line_tbl], joins=[join_edge])

    q = Question(
        id="q_test_avg_order",
        business_id="biz_test",
        as_of_date=date(2025, 4, 1),
        question="Doanh thu trung bình trên một đơn hàng trong tháng 3 là bao nhiêu?",
    )

    sql = "SELECT AVG(tong_don) FROM don_hang WHERE ngay >= '2025-03-01' AND ngay < '2025-04-01'"
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [hdr_tbl], question=q)

    assert "ROUND(SUM(_line.amount) * 1.0 / NULLIF(COUNT(DISTINCT don_hang.ma_chung_tu), 0))" in healed
    assert "JOIN chi_tiet_don _line ON don_hang.ma_chung_tu = _line.ma_don_hang" in healed
    assert "WHERE.ma_chung_tu" not in healed


# ==============================================================================
# 14. Product / Category Revenue Share Healing
# ==============================================================================

def test_category_revenue_share_healing(tmp_path: Path):
    db_path = tmp_path / "share_test.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE san_pham (ma_sp TEXT, hang_hoa TEXT, bia TEXT)")
    conn.execute("INSERT INTO san_pham VALUES ('SP01', 'Hàng hóa', 'hải sản')")
    conn.execute("INSERT INTO san_pham VALUES ('SP02', 'Hàng hóa', 'nướng')")
    conn.commit()
    conn.close()

    c_sp = ColumnMeta(name="ma_sp", original_name="ma_sp", dtype="TEXT")
    c_hh = ColumnMeta(name="hang_hoa", original_name="hang_hoa", dtype="TEXT")
    c_bia = ColumnMeta(name="bia", original_name="bia", dtype="TEXT")
    sp_tbl = TableMeta(
        name="san_pham",
        sources=[SourceRef(path="sp.csv", table="san_pham")],
        columns=[c_sp, c_hh, c_bia],
        grain="product_dimension",
        row_count=2,
        raw_row_count=2,
    )
    c_mct = ColumnMeta(name="ma_chung_tu", original_name="ma_chung_tu", dtype="TEXT")
    c_bill = ColumnMeta(name="tong_don", original_name="tong_don", dtype="REAL", semantic_role="total_due")
    hdr_tbl = TableMeta(
        name="don_hang",
        sources=[SourceRef(path="dh.csv", table="don_hang")],
        columns=[c_mct, c_bill],
        grain="order_header",
        row_count=10,
        raw_row_count=10,
    )
    c_amt = ColumnMeta(name="amount", original_name="amount", dtype="REAL", semantic_role="line_amount")
    line_tbl = TableMeta(
        name="chi_tiet_don",
        sources=[SourceRef(path="ct.csv", table="chi_tiet_don")],
        columns=[c_mct, c_sp, c_amt],
        grain="order_line",
        row_count=20,
        raw_row_count=20,
    )

    ctx = BusinessContext(
        business_id="biz_test",
        revision="rev_1",
        database=db_path,
        readme="",
        tables=[sp_tbl, hdr_tbl, line_tbl],
        joins=[],
        manifest={},
    )

    q = Question(
        id="q_test_share",
        business_id="biz_test",
        as_of_date=date(2026, 3, 1),
        question="Xem giúp tháng 2/2026 nhóm hải sản chiếm bao nhiêu % doanh thu?",
    )

    sql = (
        "SELECT 100.0 * SUM(CASE WHEN T2.hang_hoa LIKE '%hải sản%' THEN T1.tong_don ELSE 0 END) / NULLIF(SUM(T1.tong_don), 0) "
        "FROM don_hang AS T1 "
        "JOIN chi_tiet_don AS T3 ON T1.ma_chung_tu = T3.ma_don_hang "
        "JOIN san_pham AS T2 ON T3.ma_mat_hang = T2.ma_sp "
        "GROUP BY 1"
    )
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [hdr_tbl, line_tbl, sp_tbl], question=q)

    assert "T2.bia LIKE '%hải sản%'" in healed
    assert "LEFT JOIN san_pham" in healed
    assert "T3.amount" in healed
    assert "GROUP BY 1" not in healed
    assert healed.startswith("SELECT ROUND(")


# ==============================================================================
# 15. Truncated CTE Condition & Formula Healing
# ==============================================================================

def test_truncated_cte_condition_and_formula(tmp_path: Path):
    c_status = ColumnMeta(name="trang_thai", original_name="trang_thai", dtype="TEXT", semantic_role="status")
    tbl = TableMeta(
        name="banhang_hoadon_sheet1_7188020c",
        sources=[SourceRef(path="h.csv", table="banhang_hoadon_sheet1_7188020c")],
        columns=[c_status],
        grain="order_header",
        row_count=50,
        raw_row_count=50,
    )
    ctx = _make_dummy_context(tmp_path, [tbl])

    q = Question(
        id="q_test_cte",
        business_id="biz_test",
        as_of_date=date(2025, 3, 24),
        question="So 7 ngày gần đây với 7 ngày trước đó, số viên bán ra thay đổi bao nhiêu phần trăm nhỉ?",
    )

    sql = (
        "WITH period_data AS (\n"
        "  SELECT sales_after, sales_before FROM sales\n"
        "  WHERE item = 'Fugacar'\n"
        "  AND banhang_hoadon_sheet1_71880) SELECT * FROM period_data"
    )
    healed, _ = _auto_heal_sql_syntax_and_semantics(sql, ctx, [tbl], question=q)

    assert "AND banhang_hoadon_sheet1_7188020c.trang_thai = 'completed'" in healed
    assert "ROUND(100.0 * ABS(sales_after - sales_before) / NULLIF(sales_before, 0), 2)" in healed

