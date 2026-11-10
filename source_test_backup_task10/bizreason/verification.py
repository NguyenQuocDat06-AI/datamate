from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from bizreason.contracts import AnswerType, Candidate, ExecutionStatus, QueryPlan
from bizreason.data.schema import MONEY_ROLES, NUMERIC_ROLES, ROLE_LOOKUP
from bizreason.formatting import json_number, validate_answer
from bizreason.utils import digest, normalize_lookup


def _numeric(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("Expected numeric SQL result")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("SQL result is not numeric") from error
    if not number.is_finite():
        raise ValueError("SQL result is not finite")
    return number


def _has_more_than_two_decimals(val: Any) -> bool:
    if isinstance(val, (int, float, Decimal)) and not isinstance(val, bool):
        try:
            return Decimal(str(val)).as_tuple().exponent < -2
        except InvalidOperation:
            return False
    return False


def _any_has_more_than_two_decimals(value: Any, output_type: AnswerType) -> bool:
    if output_type == AnswerType.NUMBER:
        return _has_more_than_two_decimals(value)
    if output_type == AnswerType.LIST and isinstance(value, list):
        return any(_has_more_than_two_decimals(v) for v in value)
    if output_type == AnswerType.TABLE and isinstance(value, dict) and "rows" in value:
        return any(
            _has_more_than_two_decimals(cell)
            for row in value.get("rows", [])
            for cell in row
        )
    return False


def _split_top_level_select_expressions(sql: str | None) -> list[str]:
    if not sql:
        return []
    clean = re.sub(r"'(?:''|[^'])*'|--[^\n]*|/\*[\s\S]*?\*/", " ", sql)
    depth = 0
    select_pos = None
    i = 0
    n = len(clean)
    while i < n:
        char = clean[i]
        if char == "(":
            depth += 1
            i += 1
        elif char == ")":
            depth = max(0, depth - 1)
            i += 1
        elif depth == 0:
            match = re.match(r"\bSELECT\b", clean[i:], re.I)
            if match:
                select_pos = i + match.end()
                break
            i += 1
        else:
            i += 1

    if select_pos is None:
        return []

    rest = clean[select_pos:]
    opt_match = re.match(r"^\s*(?:DISTINCT|ALL)\b", rest, re.I)
    if opt_match:
        select_pos += opt_match.end()

    from_pos = None
    depth = 0
    i = select_pos
    while i < n:
        char = clean[i]
        if char == "(":
            depth += 1
            i += 1
        elif char == ")":
            depth = max(0, depth - 1)
            i += 1
        elif depth == 0:
            match = re.match(r"\bFROM\b", clean[i:], re.I)
            if match:
                from_pos = i
                break
            i += 1
        else:
            i += 1

    projection = clean[select_pos:from_pos] if from_pos is not None else clean[select_pos:]

    exprs: list[str] = []
    current: list[str] = []
    depth = 0
    for char in projection:
        if char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            current.append(char)
        elif char == "," and depth == 0:
            expr = "".join(current).strip()
            if expr:
                exprs.append(expr)
            current = []
        else:
            current.append(char)
    expr = "".join(current).strip()
    if expr:
        exprs.append(expr)
    return exprs


def _is_pure_count_expr(expr: str) -> bool:
    clean = re.sub(r"\s+AS\s+[\w\"`]+$", "", expr.strip(), flags=re.I)
    has_count = bool(re.search(r"\bcount\s*\(", clean, re.I))
    has_forbidden = bool(
        "/" in clean
        or re.search(
            r"\b(avg|sum|min|max|total|decimal_sum|decimal_div|decimal_mul)\s*\(",
            clean,
            re.I,
        )
    )
    return has_count and not has_forbidden


def _is_strict_count(sql: str | None) -> bool:
    exprs = _split_top_level_select_expressions(sql)
    return bool(exprs) and all(_is_pure_count_expr(e) for e in exprs)


def _resolve_column_role(field: str) -> str | None:
    col_name = field.split(".")[-1]
    norm = normalize_lookup(col_name)
    role = ROLE_LOOKUP.get(norm)
    if role:
        return role
    if col_name in MONEY_ROLES or col_name in NUMERIC_ROLES:
        return col_name
    return None


def verify_candidate(
    candidate: Candidate,
    plan: QueryPlan,
    context: Any = None,
    question: Any = None,
) -> Candidate:
    result = candidate.execution
    if result.status not in {ExecutionStatus.SUCCESS, ExecutionStatus.EMPTY_SUCCESS}:
        return candidate.model_copy(update={"checks": [result.status.value]})
    checks = []
    try:
        if not result.accessed_tables:
            raise ValueError("Answer query did not read business data")
        if not set(result.accessed_tables) <= set(plan.tables):
            raise ValueError("Execution accessed a table absent from the plan")
        rows = result.rows
        if plan.metric == "cancellation_rate" and candidate.program.backend == "template":
            if len(rows) != 1 or len(rows[0]) != 2:
                raise ValueError("Cancellation template must return numerator and denominator")
            numerator, denominator = map(_numeric, rows[0])
            if denominator == 0:
                raise ValueError("Cancellation rate denominator is zero")
            if not 0 <= numerator <= denominator:
                raise ValueError("Invalid order counts")
            value = numerator * 100 / denominator
        elif plan.metric == "gross_profit" and candidate.program.backend == "template":
            if len(rows) != 1 or len(rows[0]) != 2:
                raise ValueError("Profit template must return revenue and cost")
            value = _numeric(rows[0][0]) - _numeric(rows[0][1])
        elif plan.output.shape == "ratio":
            if len(rows) != 1 or len(rows[0]) != 2 or _numeric(rows[0][1]) == 0:
                raise ValueError("Ratio requires a nonzero denominator")
            value = _numeric(rows[0][0]) / _numeric(rows[0][1])
            if plan.output.type == AnswerType.PERCENT:
                value *= 100
        elif plan.output.type == AnswerType.LIST:
            transposed = False
            if len(rows) == 1 and len(rows[0]) >= 2:
                row0 = rows[0]
                if all(c is not None for c in row0):
                    all_numeric = all(
                        isinstance(c, (int, float, Decimal)) and not isinstance(c, bool)
                        for c in row0
                    )
                    all_string = all(isinstance(c, str) for c in row0)
                    if all_numeric and plan.output.element_type in {"number", None}:
                        value = [_numeric(item) for item in row0]
                        transposed = True
                    elif all_string and plan.output.element_type in {"string", None} and plan.limit != 1:
                        value = list(row0)
                        transposed = True
            if not transposed:
                is_two_col_str_num = (
                    len(rows) >= 2
                    and all(len(row) == 2 for row in rows)
                    and all(
                        isinstance(r[0], str)
                        and isinstance(r[1], (int, float, Decimal))
                        and not isinstance(r[1], bool)
                        for r in rows
                    )
                )
                if is_two_col_str_num:
                    q_raw = getattr(question, "question", "") if question else (question if isinstance(question, str) else "")
                    q_norm = normalize_lookup(q_raw) if q_raw else ""
                    asks_category_numbers = bool(
                        re.search(
                            r"\bmoi\s+(?:loai|kenh|nhom|hinh\s+thuc|thang|quy|nam|ngay)\s+(?:la\s+)?(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\b|\b(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\s+moi\s+(?:thang|quy|nam|ngay|loai|kenh|nhom)\b|\blan\s+luot\b|\bla\s+(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\s*.*\bla\s+(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\b",
                            q_norm,
                        )
                    )
                    if plan.output.element_type == "number" or (
                        plan.output.element_type is None and asks_category_numbers
                    ):
                        value = [_numeric(row[1]) for row in rows]
                    else:
                        value = [row[0] for row in rows]
                else:
                    if any(len(row) != 1 for row in rows):
                        raise ValueError("List output requires one result column")
                    value = [row[0] for row in rows]
                    if plan.output.element_type == "number":
                        value = [_numeric(item) for item in value]
        elif plan.output.type == AnswerType.TABLE:
            table_sql = candidate.program.sql or plan.sql or ""
            table_exprs = _split_top_level_select_expressions(table_sql)
            if plan.output.column_types:
                if len(plan.output.column_types) != len(result.columns):
                    raise ValueError("Table column type count does not match result")
            new_rows = []
            for row in rows:
                new_row = []
                for col_idx, cell in enumerate(row):
                    col_kind = (
                        plan.output.column_types[col_idx]
                        if plan.output.column_types and col_idx < len(plan.output.column_types)
                        else None
                    )
                    is_numeric = (col_kind == "number") or (
                        isinstance(cell, (int, float, Decimal)) and not isinstance(cell, bool)
                    )
                    if is_numeric:
                        cell_dec = _numeric(cell)
                        if col_idx < len(table_exprs) and _is_pure_count_expr(table_exprs[col_idx]):
                            if cell_dec != cell_dec.to_integral_value():
                                raise ValueError("Count/integer output cannot be fractional")
                            new_row.append(int(cell_dec))
                        else:
                            new_row.append(cell_dec)
                    else:
                        new_row.append(cell)
                new_rows.append(new_row)
            rows = new_rows
            value = {"columns": plan.output.columns or result.columns, "rows": rows}
        else:
            if len(rows) == 1 and len(rows[0]) == 1 and rows[0][0] is None:
                raise ValueError(
                    "Scalar SQL returned NULL (empty aggregation); verify period coverage, and use COALESCE(SUM(...), 0) ONLY for valid covered periods where zero transactions occurred, never for AVG or zero-denominator ratios"
                )
            cell = None
            has_order_by = bool(
                re.search(r"\border\s+by\b", (candidate.program.sql or plan.sql or "").lower())
            )
            if len(rows) == 1 and len(rows[0]) == 1 and rows[0][0] is not None:
                cell = rows[0][0]
            elif len(rows) > 1 and all(len(r) == 1 for r in rows):
                non_null = [r[0] for r in rows if r[0] is not None]
                if non_null and all(x == non_null[0] for x in non_null):
                    cell = non_null[0]
                elif (
                    plan.output.type == AnswerType.STRING
                    and isinstance(rows[0][0], str)
                    and rows[0][0].strip()
                    and has_order_by
                ):
                    cell = rows[0][0]
            elif len(rows) >= 1 and len(rows[0]) == 2 and (len(rows) == 1 or has_order_by):
                col0, col1 = rows[0][0], rows[0][1]
                col1_is_num = isinstance(col1, (int, float, Decimal)) and not isinstance(col1, bool)
                if (
                    plan.output.type == AnswerType.DATE
                    and isinstance(col0, str)
                    and re.match(r"^\d{4}-\d{2}(?:-\d{2})?$", col0.strip())
                    and col1_is_num
                ):
                    cell = col0.strip()
                elif (
                    plan.output.type == AnswerType.STRING
                    and isinstance(col0, str)
                    and col0.strip()
                    and col1_is_num
                    and (
                        plan.limit == 1
                        or has_order_by
                        or bool(re.search(r"\blimit\s+1\b", (candidate.program.sql or plan.sql or "").lower()))
                    )
                ):
                    cell = col0
            if cell is None:
                raise ValueError("Scalar output requires exactly one non-null cell")
            value = cell
            if plan.output.type in {AnswerType.NUMBER, AnswerType.PERCENT}:
                value = _numeric(value)
            elif plan.output.type == AnswerType.BOOLEAN:
                if value not in {0, 1}:
                    raise ValueError("Boolean SQL result must be 0 or 1")
                value = bool(value)
        monetary_template = candidate.program.backend == "template" and plan.metric in {
            "sales_revenue",
            "debt_total",
            "gross_profit",
        }
        effective_rounding = plan.output.rounding
        if effective_rounding == "none":
            sql_candidate = candidate.program.sql or plan.sql or ""
            if plan.output.type == AnswerType.NUMBER and sql_candidate:
                has_div_or_avg = (
                    bool(re.search(r"/|\bdecimal_div\s*\(|\bavg\s*\(", sql_candidate, re.I))
                    or plan.output.shape == "ratio"
                )
                is_scalar_fractional = (
                    isinstance(value, Decimal)
                    and value != value.to_integral_value()
                )
                if has_div_or_avg and is_scalar_fractional:
                    monetary_target_roles = {
                        "line_amount", "net_revenue", "gross_revenue", "total_amount",
                        "unit_price", "cost_price", "paid_amount", "discount_amount",
                        "shipping_fee", "tax_amount", "order_revenue", "total_due",
                    }
                    has_monetary_col = False
                    if context and hasattr(context, "tables"):
                        for tbl in context.tables:
                            for col in tbl.columns:
                                if col.semantic_role in monetary_target_roles:
                                    if re.search(r"(?<![\w])" + re.escape(col.name) + r"(?![\w])", sql_candidate, re.I) or (
                                        re.search(r"(?<![\w])" + re.escape(f"{tbl.name}.{col.name}") + r"(?![\w])", sql_candidate, re.I)
                                    ):
                                        has_monetary_col = True
                                        break
                            if has_monetary_col:
                                break
                    if not has_monetary_col:
                        tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", sql_candidate)
                        for tok in tokens:
                            r = _resolve_column_role(tok)
                            if r in monetary_target_roles or tok.lower() in {
                                "thanh_tien", "thanhtien", "tri_gia", "trigia", "dongia",
                                "don_gia", "gia_ban", "giaban", "tien", "sotien", "so_tien",
                            }:
                                has_monetary_col = True
                                break
                    q_text = getattr(question, "question", "") if question else ""
                    norm_q = normalize_lookup(q_text) if q_text else ""
                    is_rate_or_qty = bool(re.search(
                        r"\b(?:ty\s+le|ti\s+le|phan\s+tram|so\s+luong|bao\s+nhieu\s+don|may\s+lan)\b|%",
                        norm_q,
                    ))
                    if has_monetary_col and not is_rate_or_qty:
                        effective_rounding = "money"

            if effective_rounding == "none":
                if (
                    plan.metric in {"sales_revenue", "debt_total", "gross_profit"}
                    or plan.units.get("value", "").upper() == "VND"
                    or "VND" in {v.upper() for v in plan.units.values()}
                ):
                    effective_rounding = "money"
                elif plan.output.type == AnswerType.PERCENT:
                    effective_rounding = "two_decimals"
                elif plan.output.type in {AnswerType.NUMBER, AnswerType.LIST}:
                    sql = candidate.program.sql or plan.sql or ""
                    if _is_strict_count(sql):
                        if plan.output.type == AnswerType.NUMBER:
                            if isinstance(value, Decimal) and value != value.to_integral_value():
                                raise ValueError("Count/integer output cannot be fractional")
                        elif plan.output.type == AnswerType.LIST and isinstance(value, list):
                            if any(isinstance(v, Decimal) and v != v.to_integral_value() for v in value):
                                raise ValueError("Count/integer output cannot be fractional")
                        effective_rounding = "integer"
                    else:
                        referenced_numeric = [
                            role
                            for f in plan.required_fields
                            if (role := _resolve_column_role(f)) in NUMERIC_ROLES
                        ]
                        exprs = _split_top_level_select_expressions(sql)
                        expr_numeric_roles = []
                        if exprs:
                            for expr in exprs:
                                tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", expr)
                                for tok in tokens:
                                    role = _resolve_column_role(tok)
                                    if role and role in NUMERIC_ROLES and role not in expr_numeric_roles:
                                        expr_numeric_roles.append(role)
                        all_numeric_roles = referenced_numeric or expr_numeric_roles
                        if not all_numeric_roles and context and hasattr(context, "tables"):
                            for tbl in context.tables:
                                for col in tbl.columns:
                                    if col.semantic_role in NUMERIC_ROLES:
                                        if re.search(r"(?<![\w])" + re.escape(col.name) + r"(?![\w])", sql, re.I):
                                            all_numeric_roles.append(col.semantic_role)
                        is_ratio = bool(exprs and "/" in exprs[0])
                        q_raw = getattr(question, "question", "") if question else (question if isinstance(question, str) else "")
                        norm_q = normalize_lookup(q_raw) if q_raw else ""
                        is_monetary_q = bool(re.search(r"\b(?:doanh\s+thu|tien|chi\s+phi|gia|loi\s+nhuan|dong|vnd)\b", norm_q))
                        has_num_vals = isinstance(value, list) and any(
                            isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) for v in value
                        )
                        if not is_ratio and ((all_numeric_roles and all(r in MONEY_ROLES for r in all_numeric_roles)) or (has_num_vals and is_monetary_q)):
                            effective_rounding = "money"
                        elif _any_has_more_than_two_decimals(value, plan.output.type):
                            effective_rounding = "two_decimals"
                elif plan.output.type == AnswerType.TABLE:
                    pass
        resolved_spec = plan.output.model_copy(update={"rounding": effective_rounding})
        if monetary_template:
            value = json_number(value, "money")
        value = validate_answer(value, resolved_spec)
        checks = [
            "execution_success",
            "result_shape",
            "catalog_scope",
            "typed_output",
            "rule_refs_validated",
        ]
        if candidate.program.backend == "generated_sql":
            checks.append("semantic_correctness_not_proven_by_execution")
        if monetary_template or effective_rounding == "money":
            checks.append("vnd_half_up_rounding")
        signature = digest(
            [plan.output.type, value, candidate.program.policy_refs, candidate.source_revision]
        )
        return candidate.model_copy(
            update={
                "output": resolved_spec,
                "value": value,
                "verified": True,
                "checks": checks,
                "signature": signature,
            }
        )
    except (ValueError, TypeError, ArithmeticError) as error:
        checks.append(str(error))
        return candidate.model_copy(update={"checks": checks, "verified": False})
