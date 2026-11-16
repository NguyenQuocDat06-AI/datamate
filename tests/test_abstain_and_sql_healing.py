import pytest
from types import SimpleNamespace
from bizreason.sql_healers import (
    inject_non_null_entity_filter,
    heal_sqlite_syntax_glitches,
    qualify_ambiguous_columns,
    scale_scalar_percent_sql,
    heal_percent_precedence,
)
from bizreason.plan_input import canonicalize_plan_input, _auto_heal_sql_syntax_and_semantics


def test_inject_non_null_entity_filter_skips_temporal_month():
    sql = "SELECT strftime('%Y-%m', ngay) AS month, sum(tien) FROM t GROUP BY month ORDER BY 2 DESC"
    healed = inject_non_null_entity_filter(sql, {}, [])
    assert "WHERE month IS NOT NULL" not in healed
    assert healed == sql


def test_inject_non_null_entity_filter_injects_entity():
    sql = "SELECT ten_khach_hang, count(*) FROM t WHERE ngay >= '2025-01-01' GROUP BY ten_khach_hang ORDER BY 2 DESC"
    healed = inject_non_null_entity_filter(sql, {}, [])
    assert "ten_khach_hang IS NOT NULL AND TRIM(CAST(ten_khach_hang AS TEXT)) != '' AND ngay >=" in healed


def test_inject_non_null_entity_filter_subquery_without_outer_where():
    sql = (
        "SELECT ten_khach_hang, sum(tong_tien) FROM ("
        "SELECT ten_khach_hang, tong_tien FROM t1 WHERE status = 'completed' "
        "UNION ALL "
        "SELECT ten_khach_hang, tong_tien FROM t2 WHERE status = 'completed'"
        ") AS combined_sales GROUP BY ten_khach_hang ORDER BY 2 DESC LIMIT 1"
    )
    healed = inject_non_null_entity_filter(sql, {}, [])
    assert (
        ") AS combined_sales WHERE ten_khach_hang IS NOT NULL AND TRIM(CAST(ten_khach_hang AS TEXT)) != '' GROUP BY"
        in healed
    )


def test_scoped_hdr_alias_healed_outside_subquery():
    sql = (
        "SELECT SUM(so_luong) FROM ("
        "SELECT * FROM dtl1 JOIN hdr AS _hdr ON dtl1.id = _hdr.id "
        "UNION ALL "
        "SELECT * FROM dtl2 JOIN hdr AS _hdr ON dtl2.id = _hdr.id"
        ") AS sub "
        "WHERE ten_hang = 'Hong tra sua (M)' AND iso_date(_hdr.thoi_gian) >= '2025-02-01'"
    )
    healed = heal_sqlite_syntax_glitches(sql)
    assert "_hdr.thoi_gian" not in healed
    assert "iso_date(thoi_gian) >= '2025-02-01'" in healed


def test_unit_conversion_inner_join_to_left_join():
    sql = (
        "SELECT * FROM san_pham T1 "
        "JOIN dulieuxuat_danhsachquydoi_a1e97529 T2 ON T1.sku = T2.sku"
    )
    healed = heal_sqlite_syntax_glitches(sql)
    assert "LEFT JOIN dulieuxuat_danhsachquydoi_a1e97529" in healed


def test_retail_customer_wrapped_in_lookup():
    sql = "SELECT * FROM t WHERE ma_kh NOT IN ('khach le', 'kl', 'le')"
    healed = heal_sqlite_syntax_glitches(sql)
    assert "lookup(ma_kh) NOT IN ('khach le', 'kl', 'le')" in healed


def test_qualify_ambiguous_columns():
    sql = (
        "SELECT COUNT(DISTINCT ma_hoa_don) FROM danhsachhoadonchitiet_1 "
        "JOIN danhsachhoadon_1 ON danhsachhoadonchitiet_1.ma_hoa_don = danhsachhoadon_1.ma_hoa_don"
    )
    c1 = SimpleNamespace(name="ma_hoa_don", semantic_role="order_id")
    t1 = SimpleNamespace(name="danhsachhoadonchitiet_1", grain="order_line", columns=[c1])
    c2 = SimpleNamespace(name="ma_hoa_don", semantic_role="order_id")
    t2 = SimpleNamespace(name="danhsachhoadon_1", grain="order_header", columns=[c2])
    a2t = {
        "danhsachhoadonchitiet_1": ("danhsachhoadonchitiet_1", t1),
        "danhsachhoadon_1": ("danhsachhoadon_1", t2),
    }
    healed = qualify_ambiguous_columns(sql, a2t)
    assert "COUNT(DISTINCT danhsachhoadon_1.ma_hoa_don)" in healed


def test_singular_top_entity_canonicalization():
    plan = {
        "metric": "generic_sql",
        "sql": "SELECT ten_khach_hang, count(*) FROM t GROUP BY ten_khach_hang ORDER BY 2 DESC",
        "output": {"type": "number", "shape": "scalar"},
        "tables": ["t"],
    }
    col = SimpleNamespace(
        name="ten_khach_hang",
        original_name="ten_khach_hang",
        semantic_role="customer_name",
        examples=[],
        dtype="TEXT",
    )
    tbl = SimpleNamespace(name="t", columns=[col], grain="order_line", coverage=None)
    ctx = SimpleNamespace(tables=[tbl], joins=[], manifest={}, readme=None, table=lambda x: tbl)
    q = SimpleNamespace(question="Khách nào mua nhiều đơn hàng nhất tháng 10/2025?")
    res = canonicalize_plan_input(plan, ctx, question=q)
    assert res["output"]["type"] == "string"
    assert res["limit"] == 1
    assert "LIMIT 1" in res["sql"]


def test_cde_heal_sqlite_affinity_and_broken_date_arithmetic():
    from bizreason.sql_healers import (
        heal_broken_date_arithmetic,
        heal_sqlite_affinity_comparisons,
    )

    sql = (
        "SELECT * FROM sonhaphang_sheet1 "
        "WHERE money_vnd(thanh_tien, 'en') >= 500000 "
        "AND ngay_nhap >= date(2025-05-10, '-7 days')"
    )
    healed = heal_broken_date_arithmetic(sql)
    healed = heal_sqlite_affinity_comparisons(healed)
    assert "date('2025-05-10', '-7 days')" in healed
    assert "CAST(money_vnd(thanh_tien, 'en') AS REAL) >= 500000" in healed


def test_cde_heal_calendar_week_windows():
    from bizreason.sql_healers import heal_calendar_week_windows

    sql = (
        "SELECT COUNT(DISTINCT ma_don_hang) FROM don_hang "
        "WHERE ngay >= '2025-06-20' AND ngay <= '2025-06-27'"
    )
    healed = heal_calendar_week_windows(sql, "Cho biết số đơn tuần trước", "2025-06-27")
    assert "ngay >= '2025-06-16' AND ngay <= '2025-06-22'" in healed


def test_cde_heal_percent_formulas_and_cancelled_filter():
    from bizreason.sql_healers import heal_percent_formulas

    sql = (
        "SELECT 100.0 * COUNT(DISTINCT CASE WHEN status = 'cancelled' THEN ma_hoa_don END) "
        "/ NULLIF(COUNT(DISTINCT ma_hoa_don), 0) "
        "FROM sobanhang WHERE CAST(so_luong AS REAL) > 0 AND status = 'cancelled'"
    )
    healed = heal_percent_formulas(
        sql, "Tháng 11/2025 cửa hàng có bao nhiêu % đơn bị huỷ?", "percent", {}
    )
    assert "AND status = 'cancelled'" not in healed


def test_cde_heal_shift_and_lunar_windows():
    from bizreason.sql_healers import heal_shift_and_time_windows

    sql = (
        "SELECT COUNT(*) FROM hoadon "
        "WHERE TIME(T1.thoi_gian_tao) < '14:00:00'"
    )
    healed = heal_shift_and_time_windows(sql, "Đếm số đơn bán trong ca sáng tháng 9/2025.")
    assert "TIME(T1.thoi_gian_tao) >= '06:00:00' AND TIME(T1.thoi_gian_tao) <= '13:59:59'" in healed


def test_cde_monetary_average_question_detection():
    from bizreason.sql_healers import is_monetary_average_question

    assert is_monetary_average_question("Doanh thu trung bình mỗi đơn tháng 5/2025 là bao nhiêu?")
    assert not is_monetary_average_question("Tỷ lệ đơn bị hủy tháng 5/2025 là bao nhiêu %?")


def test_round2_date_and_boolean_verification():
    from bizreason.contracts import (
        AnswerType,
        Candidate,
        ExecutionResult,
        ExecutionStatus,
        OutputSpec,
        Program,
        QueryPlan,
    )
    from bizreason.verification import verify_candidate

    prog = Program(backend="generated_sql", sql="SELECT 1", parameters=[], policy_refs=[])
    plan_date = QueryPlan(
        metric="generic_sql",
        sql="SELECT '10/2025'",
        output=OutputSpec(type=AnswerType.DATE, shape="scalar"),
        tables=["t"],
        rule_refs=["r"],
    )
    res_date = ExecutionResult(
        status=ExecutionStatus.SUCCESS, columns=["m"], rows=[["10/2025"]], row_count=1, accessed_tables=["t"]
    )
    cand_date = Candidate(
        id="c1",
        plan_hash="p1",
        code_hash="c1",
        source_revision="s1",
        program=prog,
        execution=res_date,
        output=plan_date.output,
    )
    v_date = verify_candidate(cand_date, plan_date)
    assert v_date.verified
    assert v_date.value == "2025-10"

    plan_bool = QueryPlan(
        metric="generic_sql",
        sql="SELECT 'yes'",
        output=OutputSpec(type=AnswerType.BOOLEAN, shape="scalar"),
        tables=["t"],
        rule_refs=["r"],
    )
    res_bool = ExecutionResult(
        status=ExecutionStatus.SUCCESS, columns=["b"], rows=[["yes"]], row_count=1, accessed_tables=["t"]
    )
    cand_bool = Candidate(
        id="c2",
        plan_hash="p2",
        code_hash="c2",
        source_revision="s2",
        program=prog,
        execution=res_bool,
        output=plan_bool.output,
    )
    v_bool = verify_candidate(cand_bool, plan_bool)
    assert v_bool.verified
    assert v_bool.value is True


def test_round2_recover_list_execution_preserves_single_row_numeric_pair():
    from bizreason.contracts import AnswerType, ExecutionResult, ExecutionStatus, OutputSpec, QueryPlan
    from bizreason.pipeline import _recover_list_execution

    plan = QueryPlan(
        metric="generic_sql",
        sql="SELECT 6739400, 149738300",
        output=OutputSpec(type=AnswerType.LIST, shape="list", element_type="number", rounding="money"),
        tables=[],
        rule_refs=[],
    )
    exec_res = ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        columns=["c1", "c2"],
        rows=[[6739400, 149738300]],
        row_count=1,
    )
    recovered = _recover_list_execution(exec_res, plan, None)
    assert recovered.rows == [[6739400, 149738300]]


def test_round2_heal_pack_loose_and_lot_inventory_rules():
    from bizreason.sql_healers import heal_lot_inventory_rules, heal_rule44_basic_unit_quantities

    sql_pack = (
        "SELECT SUM(CASE WHEN dt.dvt = 'hộp' THEN bt.ton_cuoi_ky * dt.quy_doi ELSE 0 END) AS ham_chan, "
        "SUM(CASE WHEN dt.dvt = 'cái' THEN bt.ton_cuoi_ky * dt.quy_doi ELSE 0 END) AS ham_le "
        "FROM baocaotonkho AS bt LEFT JOIN danhsachdonvitinh AS dt ON bt.ma_hang = dt.ma_hang"
    )
    healed_pack = heal_rule44_basic_unit_quantities(
        sql_pack, "Cuối ngày hôm nay còn mấy hộp chẵn, mấy cái lẻ?", {}
    )
    assert "CAST(bt.ton_cuoi_ky AS INTEGER) / CAST(dt.quy_doi AS INTEGER)" in healed_pack
    assert "CAST(bt.ton_cuoi_ky AS INTEGER) % CAST(dt.quy_doi AS INTEGER)" in healed_pack

    sql_lot = (
        "SELECT hsd FROM ton_kho_lo_hang WHERE iso_date(hsd) <= '2026-06-30' "
        "ORDER BY iso_date(hsd) ASC LIMIT 1"
    )
    healed_lot = heal_lot_inventory_rules(
        sql_lot, "Lô nào gần hết hạn nhất tính đến ngày 30/06/2026?", {}, "date"
    )
    assert "SELECT iso_date(hsd)" in healed_lot
    assert "iso_date(hsd) >= '2026-06-30'" in healed_lot


