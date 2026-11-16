from __future__ import annotations

from datetime import date
from pathlib import Path

from bizreason.contracts import (
    AnswerType,
    BusinessContext,
    Candidate,
    ColumnMeta,
    ExecutionResult,
    ExecutionStatus,
    OutputSpec,
    Program,
    QueryPlan,
    Question,
    TableMeta,
)
from bizreason.semantic_checks import semantic_plan_errors
from bizreason.utils import digest
from bizreason.verification import verify_candidate


def _make_candidate(plan: QueryPlan, rows: list[list], columns: list[str]) -> Candidate:
    program = Program(
        backend="generated_sql",
        sql=plan.sql or "SELECT 1",
        parameters=[],
        policy_refs=plan.rule_refs,
    )
    return Candidate(
        id="test_candidate",
        plan_hash=digest(plan),
        code_hash=digest(program),
        source_revision="test_rev",
        program=program,
        output=plan.output,
        execution=ExecutionResult(
            status=ExecutionStatus.SUCCESS,
            rows=rows,
            columns=columns,
            accessed_tables=plan.tables,
        ),
    )


class TestRound5SqlitePrematureRoundRelaxation:
    def test_premature_round_relaxation_allows_float_division_in_round(self, business, rules):
        tbl = business.tables[0]
        q = Question(
            id="q_avg",
            business_id=business.business_id,
            question="Doanh thu trung binh moi don",
            as_of_date=date(2026, 3, 5),
        )

        # 1. ROUND(SUM(a) * 1.0 / COUNT(b)) is allowed (ZERO errors)
        plan_round_float = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT ROUND(SUM(total_due) * 1.0 / COUNT(order_id), 0) FROM orders',
            output=OutputSpec(type=AnswerType.NUMBER, rounding="money"),
        )
        errs_round_float = semantic_plan_errors(plan_round_float, business, q)
        assert not any("SQLITE_PREMATURE_ROUND_OR_INT_DIV" in e for e in errs_round_float)

        # 2. decimal_div inside ROUND is also allowed
        plan_round_decimal_div = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT ROUND(decimal_div(SUM(total_due), COUNT(order_id))) FROM orders',
            output=OutputSpec(type=AnswerType.NUMBER, rounding="money"),
        )
        errs_round_dd = semantic_plan_errors(plan_round_decimal_div, business, q)
        assert not any("SQLITE_PREMATURE_ROUND_OR_INT_DIV" in e for e in errs_round_dd)

        # 3. Raw integer division SUM(a) / COUNT(b) without 1.0 is flagged
        plan_raw_div = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT SUM(total_due) / COUNT(order_id) FROM orders',
            output=OutputSpec(type=AnswerType.NUMBER),
        )
        errs_raw_div = semantic_plan_errors(plan_raw_div, business, q)
        assert any("SQLITE_PREMATURE_ROUND_OR_INT_DIV" in e for e in errs_raw_div)

        # 4. ROUND(SUM(a) / COUNT(b)) without 1.0 is still flagged by raw integer division check
        plan_round_raw = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT ROUND(SUM(total_due) / COUNT(order_id)) FROM orders',
            output=OutputSpec(type=AnswerType.NUMBER),
        )
        errs_round_raw = semantic_plan_errors(plan_round_raw, business, q)
        assert any("SQLITE_PREMATURE_ROUND_OR_INT_DIV" in e for e in errs_round_raw)


class TestRound5OutputTypeGuards:
    def test_output_type_table_to_list_guards(self, business):
        tbl = business.tables[0]

        # A. Named two-part / breakdown with explicit conjunctive categories
        q_breakdown = Question(
            id="q_bdown",
            business_id=business.business_id,
            question="doanh thu đơn tiền mặt và đơn COD mỗi loại bao nhiêu",
            as_of_date=date(2026, 3, 5),
        )
        plan_table_bdown = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[],
            sql=f'SELECT payment_method, SUM(total_due) FROM "{tbl.name}" GROUP BY payment_method',
            output=OutputSpec(type=AnswerType.TABLE, shape="table"),
        )
        errs_bdown = semantic_plan_errors(plan_table_bdown, business, q_breakdown)
        assert any("OUTPUT_TYPE_MUST_BE_LIST" in e for e in errs_bdown)
        assert any("... và ... mỗi loại bao nhiêu" in e for e in errs_bdown)

        # B. Top-N entity names WITHOUT accompanying metrics
        q_top_entities = Question(
            id="q_top_e",
            business_id=business.business_id,
            question="Top 5 khách hàng",
            as_of_date=date(2026, 3, 5),
        )
        plan_table_top = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[],
            sql=f'SELECT customer_id FROM "{tbl.name}" LIMIT 5',
            output=OutputSpec(type=AnswerType.TABLE, shape="table"),
        )
        errs_top = semantic_plan_errors(plan_table_top, business, q_top_entities)
        assert any("OUTPUT_TYPE_MUST_BE_LIST" in e for e in errs_top)
        assert any("select only the 1D entity name column" in e for e in errs_top)

        # C. Explicit table request does NOT emit OUTPUT_TYPE_MUST_BE_LIST
        q_explicit_table = Question(
            id="q_table",
            business_id=business.business_id,
            question="bảng doanh thu mỗi loại tiền mặt và COD",
            as_of_date=date(2026, 3, 5),
        )
        errs_exp_table = semantic_plan_errors(plan_table_bdown, business, q_explicit_table)
        assert not any("OUTPUT_TYPE_MUST_BE_LIST" in e for e in errs_exp_table)

    def test_output_type_table_to_number_guards(self, business):
        tbl = business.tables[0]

        # A. Accented "mọi" combined total
        q_moi_all = Question(
            id="q_all",
            business_id=business.business_id,
            question="tính mọi loại đồ uống đã bán",
            as_of_date=date(2026, 3, 5),
        )
        plan_table_all = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[],
            sql=f'SELECT SUM(total_due) FROM "{tbl.name}"',
            output=OutputSpec(type=AnswerType.TABLE, shape="table"),
        )
        errs_all = semantic_plan_errors(plan_table_all, business, q_moi_all)
        assert any("OUTPUT_TYPE_MUST_BE_NUMBER" in e for e in errs_all)

        # B. Single scalar average
        q_avg_order = Question(
            id="q_avg_order",
            business_id=business.business_id,
            question="trung bình mỗi đơn bao nhiêu tiền",
            as_of_date=date(2026, 3, 5),
        )
        plan_table_avg = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[],
            sql=f'SELECT AVG(total_due) FROM "{tbl.name}"',
            output=OutputSpec(type=AnswerType.TABLE, shape="table"),
        )
        errs_avg = semantic_plan_errors(plan_table_avg, business, q_avg_order)
        assert any("OUTPUT_TYPE_MUST_BE_NUMBER" in e for e in errs_avg)

        # C. Distinguishing "tính mọi loại" from "bảng doanh thu mỗi loại"
        q_breakdown_table = Question(
            id="q_bk_table",
            business_id=business.business_id,
            question="bảng doanh thu mỗi loại",
            as_of_date=date(2026, 3, 5),
        )
        errs_bk = semantic_plan_errors(plan_table_all, business, q_breakdown_table)
        assert not any("OUTPUT_TYPE_MUST_BE_NUMBER" in e for e in errs_bk)

    def test_output_type_list_to_string_guard(self, business):
        tbl = business.tables[0]

        # A. Singular superlative question with AnswerType.LIST triggers OUTPUT_TYPE_MUST_BE_STRING
        q_singular = Question(
            id="q_sing",
            business_id=business.business_id,
            question="tuần rồi hàng nào chạy nhất",
            as_of_date=date(2026, 3, 5),
        )
        plan_list_sing = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[],
            sql=f'SELECT product_id FROM "{tbl.name}" LIMIT 1',
            output=OutputSpec(type=AnswerType.LIST, shape="list"),
        )
        errs_sing = semantic_plan_errors(plan_list_sing, business, q_singular)
        assert any("OUTPUT_TYPE_MUST_BE_STRING" in e for e in errs_sing)

        # B. Plural or Top-N question does NOT trigger OUTPUT_TYPE_MUST_BE_STRING
        q_plural = Question(
            id="q_plur",
            business_id=business.business_id,
            question="những mặt hàng chạy nhất tuần rồi",
            as_of_date=date(2026, 3, 5),
        )
        errs_plur = semantic_plan_errors(plan_list_sing, business, q_plural)
        assert not any("OUTPUT_TYPE_MUST_BE_STRING" in e for e in errs_plur)


class TestRound5QueryGuards:
    def test_best_seller_ranked_by_money(self, business):
        # lines table has line_amount (monetary) and qty_base / qty_sale (quantity)
        lines_tbl = next(t for t in business.tables if "line" in t.name.lower())
        q = Question(
            id="q_bs",
            business_id=business.business_id,
            question="mặt hàng nào bán chạy nhất tháng 3",
            as_of_date=date(2026, 3, 5),
        )

        # 1. ORDER BY monetary column (line_amount) triggers error
        plan_bad_order = QueryPlan(
            metric="generic_sql",
            tables=[lines_tbl.name],
            rule_refs=[],
            sql=f'SELECT product_name FROM "{lines_tbl.name}" GROUP BY product_name ORDER BY SUM(line_amount) DESC LIMIT 1',
            output=OutputSpec(type=AnswerType.STRING, shape="scalar"),
        )
        errs_bad = semantic_plan_errors(plan_bad_order, business, q)
        assert any("BEST_SELLER_RANKED_BY_MONEY" in e for e in errs_bad)

        # 2. ORDER BY quantity column (qty_base) passes with ZERO error
        plan_good_order = QueryPlan(
            metric="generic_sql",
            tables=[lines_tbl.name],
            rule_refs=[],
            sql=f'SELECT product_name FROM "{lines_tbl.name}" GROUP BY product_name ORDER BY SUM(qty_base) DESC LIMIT 1',
            output=OutputSpec(type=AnswerType.STRING, shape="scalar"),
        )
        errs_good = semantic_plan_errors(plan_good_order, business, q)
        assert not any("BEST_SELLER_RANKED_BY_MONEY" in e for e in errs_good)

        # 3. Question explicitly asking for money/revenue does not trigger
        q_rev = Question(
            id="q_rev",
            business_id=business.business_id,
            question="mặt hàng nào mang lại nhiều doanh thu nhất",
            as_of_date=date(2026, 3, 5),
        )
        errs_rev = semantic_plan_errors(plan_bad_order, business, q_rev)
        assert not any("BEST_SELLER_RANKED_BY_MONEY" in e for e in errs_rev)

    def test_shift_end_minute_truncation(self, business):
        tbl = business.tables[0]
        q = Question(
            id="q_shift",
            business_id=business.business_id,
            question="Doanh thu ca sang",
            as_of_date=date(2026, 3, 5),
        )

        # 1. Upper bound <= '14:59' triggers SHIFT_END_MINUTE_TRUNCATION
        plan_trunc1 = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[],
            sql=f"SELECT SUM(total_due) FROM \"{tbl.name}\" WHERE time(ordered_at) <= '14:59'",
            output=OutputSpec(type=AnswerType.NUMBER),
        )
        assert any("SHIFT_END_MINUTE_TRUNCATION" in e for e in semantic_plan_errors(plan_trunc1, business, q))

        # 2. BETWEEN '06:00' AND '14:59' triggers SHIFT_END_MINUTE_TRUNCATION
        plan_trunc2 = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[],
            sql=f"SELECT SUM(total_due) FROM \"{tbl.name}\" WHERE time(ordered_at) BETWEEN '06:00' AND '14:59'",
            output=OutputSpec(type=AnswerType.NUMBER),
        )
        assert any("SHIFT_END_MINUTE_TRUNCATION" in e for e in semantic_plan_errors(plan_trunc2, business, q))

        # 3. Half-open interval < '15:00' passes with ZERO errors
        plan_clean = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[],
            sql=f"SELECT SUM(total_due) FROM \"{tbl.name}\" WHERE time(ordered_at) >= '06:00' AND time(ordered_at) < '15:00'",
            output=OutputSpec(type=AnswerType.NUMBER),
        )
        assert not any("SHIFT_END_MINUTE_TRUNCATION" in e for e in semantic_plan_errors(plan_clean, business, q))

    def test_missing_outer_from_clause(self, business):
        tbl = business.tables[0]
        q = Question(
            id="q_cte",
            business_id=business.business_id,
            question="Doanh thu chenh lech",
            as_of_date=date(2026, 3, 5),
        )

        # 1. Outer SELECT references cte aliases without FROM
        plan_bad_cte = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[],
            sql=f'WITH cte1 AS (SELECT SUM(total_due) AS rev1 FROM "{tbl.name}"), cte2 AS (SELECT SUM(total_due) AS rev2 FROM "{tbl.name}") SELECT cte1.rev1 - cte2.rev2',
            output=OutputSpec(type=AnswerType.NUMBER),
        )
        errs_bad_cte = semantic_plan_errors(plan_bad_cte, business, q)
        assert any("MISSING_OUTER_FROM_CLAUSE" in e for e in errs_bad_cte)
        assert any("add 'FROM cte1, cte2' to the final SELECT" in e for e in errs_bad_cte)

        # 2. Outer SELECT with proper FROM clause passes
        plan_good_cte = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[],
            sql=f'WITH cte1 AS (SELECT SUM(total_due) AS rev1 FROM "{tbl.name}"), cte2 AS (SELECT SUM(total_due) AS rev2 FROM "{tbl.name}") SELECT cte1.rev1 - cte2.rev2 FROM cte1, cte2',
            output=OutputSpec(type=AnswerType.NUMBER),
        )
        errs_good_cte = semantic_plan_errors(plan_good_cte, business, q)
        assert not any("MISSING_OUTER_FROM_CLAUSE" in e for e in errs_good_cte)


class TestRound5TwoColumnListVerification:
    def test_two_column_list_extraction(self, business, rules):
        tbl = business.tables[0]

        # 1. 2-column string ranking: [["Customer A", 500], ["Customer B", 300]] -> ["Customer A", "Customer B"]
        plan_names = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT customer_id, SUM(total_due) FROM orders GROUP BY customer_id LIMIT 2',
            output=OutputSpec(type=AnswerType.LIST, element_type="string"),
        )
        cand_names = _make_candidate(
            plan_names,
            [["Customer A", 500], ["Customer B", 300]],
            ["customer_id", "total_rev"],
        )
        ver_names = verify_candidate(cand_names, plan_names, business)
        assert ver_names.verified is True
        assert ver_names.value == ["Customer A", "Customer B"]

        # 2. 2-column category numbers question: "doanh thu tiền mặt và COD mỗi loại bao nhiêu"
        # with rows [["cash", 100], ["cod", 200]] -> [100, 200]
        q_breakdown = Question(
            id="q_bdown_list",
            business_id=business.business_id,
            question="doanh thu tiền mặt và COD mỗi loại bao nhiêu",
            as_of_date=date(2026, 3, 5),
        )
        plan_numbers = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT payment_method, SUM(total_due) FROM orders GROUP BY payment_method',
            output=OutputSpec(type=AnswerType.LIST),  # element_type is None, detected via question intent
        )
        cand_numbers = _make_candidate(
            plan_numbers,
            [["cash", 100], ["cod", 200]],
            ["payment_method", "rev"],
        )
        ver_numbers = verify_candidate(cand_numbers, plan_numbers, business, question=q_breakdown)
        assert ver_numbers.verified is True
        assert ver_numbers.value == [100, 200]

        # 3. Single-row mixed [["Prod A", 500]] STILL fails (backward compatibility safeguard)
        plan_single_mixed = QueryPlan(
            metric="generic_sql",
            tables=[tbl.name],
            rule_refs=[next(iter(rules.by_id))],
            sql='SELECT product_name, 500 LIMIT 1',
            output=OutputSpec(type=AnswerType.LIST),
        )
        cand_single_mixed = _make_candidate(
            plan_single_mixed,
            [["Prod A", 500]],
            ["product_name", "price"],
        )
        ver_single_mixed = verify_candidate(cand_single_mixed, plan_single_mixed, business)
        assert ver_single_mixed.verified is False
        assert any("List output requires one result column" in c for c in ver_single_mixed.checks)


class TestRound5SemanticEdgeCases:
    def test_non_canonical_status_in_sql(self, business, rules):
        ctx = BusinessContext(
            business_id="b",
            revision="1",
            database=Path("."),
            readme="",
            tables=[
                TableMeta(
                    name="don_hang",
                    sources=[],
                    columns=[
                        ColumnMeta(name="ma_don", original_name="Mã đơn", dtype="text", semantic_role="order_id"),
                        ColumnMeta(name="trang_thai", original_name="Trạng thái", dtype="text", semantic_role="status"),
                    ],
                    row_count=1,
                    raw_row_count=1,
                )
            ],
            manifest={},
        )
        q = Question(
            id="q_status_test",
            business_id="b",
            question="Số đơn hủy trong kỳ",
            as_of_date=date(2026, 3, 5),
        )
        plan = QueryPlan(
            metric="generic_sql",
            tables=["don_hang"],
            rule_refs=[],
            sql="SELECT COUNT(*) FROM don_hang WHERE trang_thai LIKE '%huy%'",
            output=OutputSpec(type=AnswerType.NUMBER),
        )
        errs = semantic_plan_errors(plan, ctx, q)
        assert any("NON_CANONICAL_STATUS_IN_SQL" in e for e in errs)

    def test_money_question_sums_quantity_or_count(self, business, rules):
        q = Question(
            id="q_money_qty",
            business_id=business.business_id,
            question="tiền nhập hàng tháng 2/2025",
            as_of_date=date(2026, 3, 5),
        )
        ctx = BusinessContext(
            business_id=business.business_id,
            revision="1",
            database=Path("."),
            readme="",
            tables=[
                TableMeta(
                    name="phieu_nhap",
                    sources=[],
                    columns=[
                        ColumnMeta(name="ma_phieu", original_name="Mã phiếu", dtype="text", semantic_role="purchase_id"),
                        ColumnMeta(name="so_luong", original_name="Số lượng", dtype="real", semantic_role="qty_base"),
                        ColumnMeta(name="don_gia", original_name="Đơn giá", dtype="real", semantic_role="cost_price"),
                    ],
                    row_count=5,
                    raw_row_count=5,
                )
            ],
            manifest={},
        )
        plan = QueryPlan(
            metric="generic_sql",
            tables=["phieu_nhap"],
            rule_refs=[],
            sql="SELECT SUM(so_luong) FROM phieu_nhap",
            output=OutputSpec(type=AnswerType.NUMBER),
        )
        errs = semantic_plan_errors(plan, ctx, q)
        assert any("MONEY_QUESTION_SUMS_QUANTITY_OR_COUNT" in e for e in errs)

        plan_valid = QueryPlan(
            metric="generic_sql",
            tables=["phieu_nhap"],
            rule_refs=[],
            sql="SELECT SUM(so_luong * don_gia) FROM phieu_nhap",
            output=OutputSpec(type=AnswerType.NUMBER),
        )
        errs_valid = semantic_plan_errors(plan_valid, ctx, q)
        assert not any("MONEY_QUESTION_SUMS_QUANTITY_OR_COUNT" in e for e in errs_valid)

    def test_output_type_must_be_list_on_ordered_sequence(self, business, rules):
        q = Question(
            id="q_ordered_seq",
            business_id=business.business_id,
            question="ghi lần lượt theo thứ tự đó",
            as_of_date=date(2026, 3, 5),
        )
        plan = QueryPlan(
            metric="generic_sql",
            tables=["orders"],
            rule_refs=[next(iter(rules.by_id))],
            sql="SELECT 100, 50",
            output=OutputSpec(type=AnswerType.TABLE, shape="table"),
        )
        errs = semantic_plan_errors(plan, business, q)
        assert any("OUTPUT_TYPE_MUST_BE_LIST" in e for e in errs)
