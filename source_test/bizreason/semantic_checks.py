"""Conservative semantic checks for known identity and ledger hazards.

This is a narrow guard for direct SQL expressions, not a general SQL AST verifier.
The executor's SQLite authorizer separately enforces read-only/table/function scope.
"""

from __future__ import annotations

import re

from bizreason.contracts import AnswerType, BusinessContext, QueryPlan, Question
from bizreason.utils import normalize_lookup
from bizreason.verification import _split_top_level_select_expressions


def distinct_count_arguments(code: str) -> list[str]:
    arguments = []
    for match in re.finditer(r"\bcount\s*\(\s*distinct\b", code, re.I):
        depth, end = 1, match.end()
        while end < len(code) and depth:
            if code[end] == "(":
                depth += 1
            elif code[end] == ")":
                depth -= 1
            end += 1
        if not depth:
            arguments.append(code[match.end():end - 1])
    return arguments


def _extract_query_blocks(code: str) -> tuple[list[str], str]:
    subqueries: list[str] = []
    outer_parts: list[str] = []
    i = 0
    n = len(code)
    while i < n:
        if code[i] == "(":
            rest = code[i + 1 :]
            if re.match(r"^\s*SELECT\b", rest, re.I):
                depth = 1
                j = i + 1
                while j < n and depth > 0:
                    if code[j] == "(":
                        depth += 1
                    elif code[j] == ")":
                        depth -= 1
                    j += 1
                if depth == 0:
                    inner = code[i + 1 : j - 1]
                    subqueries.append(inner)
                    outer_parts.append("(__SUBQUERY__)")
                    i = j
                    continue
        outer_parts.append(code[i])
        i += 1
    return subqueries, "".join(outer_parts)


def semantic_plan_errors(plan: QueryPlan, context: BusinessContext, question: Question) -> list[str]:
    sql = plan.sql or ""
    # Remove quoted values and comments, preserving quoted identifier spellings.
    code = re.sub(r"'(?:''|[^'])*'|--[^\n]*|/\*[\s\S]*?\*/", " ", sql)
    text = normalize_lookup(question.question)
    raw_q_lower = str(question.question).lower()
    pct_text = normalize_lookup(question.question.replace("%", " phan tram "))
    words = set(text.split())
    vehicle_count = bool(re.search(r"\b(chiec xe|xe khac nhau|unique vehicles|distinct vehicles)\b", text)) or (
        bool(words & {"xe", "chiec"}) and "khac nhau" in text
    )
    if "cach ghi" in text or "chuoi" in words:
        vehicle_count = False  # A question explicitly counting labels has a different grain.
    asks_street_address = bool(re.search(r"\b(o\s+duong|tai\s+duong|tuyen\s+duong|dia\s+chi)\b", text))
    has_address_col = any(
        col.semantic_role == "customer_address"
        or normalize_lookup(col.original_name) in {"dia chi", "dia chi khach hang", "dia chi giao hang", "address"}
        for t in context.tables
        for col in t.columns
    )
    errors = []
    if plan.metric != "unsupported":
        if (
            plan.output.shape == "scalar"
            and plan.output.type not in {AnswerType.PERCENT, AnswerType.LIST, AnswerType.TABLE, AnswerType.BOOLEAN}
        ):
            percent_pattern = (
                r"(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu|may)\s*(?:%|phan\s+tram)\b|"
                r"\b(?:ty|ti)\s+le\s+.*(?:%|phan\s+tram)\b|"
                r"\b(?:tang|giam|thay\s+doi|chenh\s+lech|chiem)\s+(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu|may)\s*(?:%|phan\s+tram)\b|"
                r"\b(?:ty|ti)\s+le\s+(?:(?:don|don\s+hang)\s+)?(?:bi\s+)?huy\b"
            )
            if re.search(percent_pattern, pct_text) or re.search(percent_pattern, text):
                errors.append(
                    "OUTPUT_TYPE_MUST_BE_PERCENT:question asks for a percentage/rate, set output.type='percent' and rounding='two_decimals'"
                )
        if plan.output.type == AnswerType.PERCENT and not plan.template_eligible and plan.sql:
            if re.search(r"/|\bdecimal_div\s*\(", code, re.I) and not re.search(r"\b100(?:\.0+)?\b", code):
                errors.append(
                    "PERCENT_MISSING_100_SCALE:output.type='percent' requires percentage points on a 0..100 scale (e.g. 100.0 * numerator / denominator or decimal_mul('100', decimal_div(...))), NOT an unscaled 0..1 ratio"
                )
        if plan.output.type == AnswerType.STRING and plan.output.shape == "scalar":
            if re.search(r"\b(?:vao\s+)?(?:ngay|thang|hom)\s+nao\b", text):
                not_date = bool(re.search(
                    r"\b(?:thu\s+may|quy\s+nao|ca\s+nao|ai|khach\s+nao|san\s+pham\s+nao|mon\s+nao|kenh\s+nao)\b",
                    text,
                ))
                if not not_date:
                    errors.append(
                        "OUTPUT_TYPE_MUST_BE_DATE:question asks which date or month ('ngày nào' / 'tháng nào'), set output.type='date' and return ISO YYYY-MM-DD or YYYY-MM"
                    )
        if plan.output.type == AnswerType.NUMBER and plan.sql:
            is_qty_q = bool(re.search(
                r"\b(?:may|bao\s+nhieu|bn|bnhiu|tong\s+so|tong\s+cong\s+may)\s+(?:ly|cai|chiec|hop|vien|vi|chai|lon|thung|cuon|phan|suat|to|tui|goi|bo|cap|doi|con|cay|qua|trai|kg|gam|lit|m3|xe)\b",
                text,
            ))
            has_money_kw = bool(re.search(
                r"\b(?:tien|doanh\s+thu|doanh\s+so|gia|chi\s+phi|loi\s+nhuan|lai|lo|no|vnd|dong|dthu)\b",
                text,
            ))
            if is_qty_q and not has_money_kw:
                monetary_roles = {
                    "line_amount", "net_revenue", "gross_revenue", "total_amount",
                    "unit_price", "cost_price", "paid_amount", "shipping_fee", "discount_amount"
                }
                quantity_roles = {"qty_sale", "qty_base", "stock"}
                monetary_cols = set()
                quantity_cols = set()
                for t_name in plan.tables:
                    table = context.table(t_name)
                    if not table:
                        continue
                    for col in table.columns:
                        if col.semantic_role in monetary_roles:
                            monetary_cols.add(col.name)
                            monetary_cols.add(f"{table.name}.{col.name}")
                        if col.semantic_role in quantity_roles:
                            quantity_cols.add(col.name)
                            quantity_cols.add(f"{table.name}.{col.name}")
                top_exprs = _split_top_level_select_expressions(plan.sql)
                if top_exprs:
                    target_expr = top_exprs[0]
                    sums_monetary = False
                    for m in re.finditer(r"\b(?:sum|total|decimal_sum)\s*\(([^)]+)\)", target_expr, re.I):
                        inner = m.group(1)
                        if any(re.search(r"(?<![\w])" + re.escape(c) + r"(?![\w])", inner, re.I) for c in monetary_cols):
                            sums_monetary = True
                            break
                    has_qty_ref = any(re.search(r"(?<![\w])" + re.escape(c) + r"(?![\w])", target_expr, re.I) for c in quantity_cols)
                    if sums_monetary and not has_qty_ref:
                        errors.append(
                            "QUANTITY_QUESTION_SUMS_MONEY_COLUMN:question asks for physical item quantity/count, do not SUM a monetary column—SUM the quantity column (qty_base / qty_sale) instead"
                        )

        if (
            plan.metric == "generic_sql"
            and plan.sql
            and plan.output.type == AnswerType.NUMBER
        ):
            asks_money = bool(re.search(
                r"\b(?:tien\s+nhap(?:\s+hang)?|chi\s+phi\s+nhap|tong\s+tien\s+nhap|gia\s+tri\s+nhap|doanh\s+thu|dthu|doanh\s+so)\b",
                text,
            ))
            asks_count_or_rate = (
                bool(re.search(
                    r"\b(?:so\s+luong|bao\s+nhieu\s+(?:don|sp|san\s+pham|mon|mat\s+hang|lan|phieu|hoa\s+don)|may\s+(?:don|sp|san\s+pham|mon|lan|phieu)|ty\s+le|ti\s+le|phan\s+tram)\b",
                    text,
                ))
                or "%" in raw_q_lower
            )
            if asks_money and not asks_count_or_rate:
                top_exprs = _split_top_level_select_expressions(plan.sql)
                if len(top_exprs) == 1:
                    expr0 = top_exprs[0]
                    qty_or_id_roles = {"qty_base", "qty_sale", "order_id", "purchase_id", "product_id"}
                    money_or_price_roles = {
                        "line_amount", "net_revenue", "gross_revenue", "total_amount",
                        "order_revenue", "total_due", "unit_price", "cost_price", "paid_amount",
                    }
                    qty_or_id_cols = set()
                    money_or_price_cols = set()
                    for t_name in plan.tables:
                        table = context.table(t_name)
                        if not table:
                            continue
                        for col in table.columns:
                            if col.semantic_role in qty_or_id_roles:
                                qty_or_id_cols.add(col.name)
                                qty_or_id_cols.add(f"{table.name}.{col.name}")
                            if col.semantic_role in money_or_price_roles:
                                money_or_price_cols.add(col.name)
                                money_or_price_cols.add(f"{table.name}.{col.name}")

                    aggregates_qty_or_id = False
                    if re.search(r"\bcount\s*\(\s*\*\s*\)", expr0, re.I):
                        aggregates_qty_or_id = True
                    else:
                        for m in re.finditer(r"\b(?:sum|total|count|decimal_sum)\s*\(([^)]+)\)", expr0, re.I):
                            inner = m.group(1)
                            if any(re.search(r"(?<![\w])" + re.escape(c) + r"(?![\w])", inner, re.I) for c in qty_or_id_cols):
                                aggregates_qty_or_id = True
                                break

                    expr0_no_count_star = re.sub(r"\bcount\s*\(\s*\*\s*\)", " ", expr0, flags=re.I)
                    has_mul = "*" in expr0_no_count_star
                    has_money_vnd = "money_vnd" in plan.sql.lower()
                    has_money_col_ref = any(
                        re.search(r"(?<![\w])" + re.escape(c) + r"(?![\w])", plan.sql, re.I)
                        for c in money_or_price_cols
                    )

                    if aggregates_qty_or_id and not has_mul and not has_money_vnd and not has_money_col_ref:
                        errors.append(
                            "MONEY_QUESTION_SUMS_QUANTITY_OR_COUNT:question asks for a monetary VND amount ('tiền nhập hàng' / 'chi phí' / 'doanh thu'), do not SUM quantity or COUNT rows alone—use SUM(qty * unit_price) on detail lines or SUM(money_vnd(header_amount_col)) on headers"
                        )

        if plan.output.type == AnswerType.TABLE:
            if (
                re.search(r"\bngay\s+nao\b.*\bnhat\b", text)
                and not re.search(r"\b(?:nhung|cac|bang|liet\s+ke|danh\s+sach|kem)\b", text)
            ):
                errors.append(
                    "OUTPUT_TYPE_MUST_BE_DATE:question asks 'ngày nào ... nhất' (a single date), set output.type='date', output.shape='scalar', group by ISO date, and LIMIT 1"
                )

            tuple_pattern = (
                r"\blan\s+luot\s+la\s+(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\b|"
                r"\b(?:ghi\s+)?lan\s+luot\s+theo\s+thu\s+tu\b|"
                r"\bchan\s*,?\s*le\s+(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\b"
            )
            table_keywords = r"\b(?:liet\s+ke|bang|danh\s+sach|moi\s+|tung\s+)\b"
            if re.search(tuple_pattern, text) and not re.search(table_keywords, text):
                errors.append(
                    "OUTPUT_TYPE_MUST_BE_LIST:question asks for an ordered pair/tuple ('lần lượt là bao nhiêu' / 'chẵn, lẻ bao nhiêu'), set output.type='list' and output.shape='list'"
                )

            named_two_part = bool(re.search(
                r"\bmoi\s+(?:loai|kenh|nhom|hinh\s+thuc)\s+(?:la\s+)?(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\b|\bla\s+(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\s*,\s*.*\bla\s+(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\b",
                text,
            ))
            has_table_intent = bool(re.search(r"\b(?:bang|danh\s+sach|liet\s+ke|kem|gom|chi\s+tiet)\b", text))
            has_conjunctive = bool(re.search(r"\bva\b", text)) or "," in raw_q_lower
            if named_two_part and not has_table_intent and has_conjunctive:
                errors.append(
                    "OUTPUT_TYPE_MUST_BE_LIST:question asks for numeric values of specific named categories ('... và ... mỗi loại bao nhiêu'), set output.type='list', output.shape='list', element_type='number', and return values in the exact order mentioned in the question"
                )

            asks_top_n_entities = bool(re.search(
                r"\b(?:top\s*\d+|\d+\s+(?:khach|khach\s+hang|san\s+pham|mat\s+hang|mon|sp))\b",
                text,
            ))
            has_metrics_or_breakdown = bool(re.search(
                r"\b(?:bang|kem|gom|chi\s+tiet|bao\s+nhieu|bao\s+nhiu|bn|bnhiu|moi\s+|tung\s+|so\s+tien|doanh\s+thu\s+cua\s+tung)\b",
                text,
            ))
            if asks_top_n_entities and not has_metrics_or_breakdown:
                errors.append(
                    "OUTPUT_TYPE_MUST_BE_LIST:question asks only for top entity names without accompanying metrics ('kèm...' / 'bảng...'), set output.type='list', output.shape='list', and select only the 1D entity name column"
                )

            accented_moi_all = bool(
                re.search(r"\b(?:cộng\s+mọi|tính\s+mọi|gộp\s+mọi|cộng\s+tất\s+cả|tổng\s+cộng\s+mọi)\b", raw_q_lower)
                or re.search(r"\bcong\s+moi\s+(?:mon|mat\s+hang|loai|san\s+pham)\b.*\b(?:may|bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\b", text)
            )
            single_scalar_avg = bool(
                re.search(r"\b(?:trung\s+binh|binh\s+quan)\s+moi\s+(?:don|hoa\s+don|khach|ngay|thang|chuyen)\b.*\b(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\b", text)
                and not re.search(r"\b(?:bang|liet\s+ke|danh\s+sach|kem|gom|tung\s+nhom|tung\s+loai|moi\s+loai)\b", text)
            )
            if accented_moi_all or single_scalar_avg:
                errors.append(
                    "OUTPUT_TYPE_MUST_BE_NUMBER:question asks for a single combined total or single overall average, set output.type='number' and output.shape='scalar' returning one scalar number (no GROUP BY breakdown or extra columns)"
                )

        if plan.output.type == AnswerType.LIST:
            singular_top_entity = bool(
                re.search(
                    r"\b(?:ai|khach\s+nao|khach\s+hang\s+nao|hang\s+nao|mat\s+hang\s+nao|san\s+pham\s+nao|mon\s+nao|sp\s+nao)\b.*\bnhat\b|\b(?:mat\s+hang|san\s+pham|mon|khach\s+hang|sp)\s+(?:ban\s+chay|chay|mua\s+nhieu|chi\s+tieu\s+nhieu)\s+nhat\b.*\bla\s+(?:gi|ai|(?:mat\s+hang|san\s+pham|mon|sp|khach)\s+nao)\b",
                    text,
                )
                and not re.search(
                    r"\b(?:nhung|cac|liet\s+ke|danh\s+sach|top\s*\d*|\d+\s+(?:khach|khach\s+hang|hang|mat\s+hang|san\s+pham|mon|sp)|lan\s+luot|moi\s+loai)\b",
                    text,
                )
            )
            if singular_top_entity:
                errors.append(
                    "OUTPUT_TYPE_MUST_BE_STRING:question asks for a single top entity ('ai / mặt hàng nào ... nhất'), set output.type='string', output.shape='scalar', and LIMIT 1"
                )

            singular_price_pattern = (
                r"\b(?:don\s+gia|gia\s+ban|gia\s+nhap|gia\s+von)\b.*\b(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\b|"
                r"\b(?:voi\s+)?(?:don\s+gia|gia)\s+(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\s+(?:mot|1)\s+\w+\b|"
                r"\b(?:don\s+gia|gia)\b.*\b(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\s+(?:mot|1)\s+\w+\b"
            )
            multi_item_pattern = (
                r"\b(?:liet\s+ke|danh\s+sach|nhung|cac|lan\s+luot|chan\s*,?\s*le|top|moi|tung)\b"
            )
            if re.search(singular_price_pattern, text) and not re.search(multi_item_pattern, text):
                errors.append(
                    "OUTPUT_TYPE_MUST_BE_NUMBER:question asks for a single unit price ('đơn giá bao nhiêu'), set output.type='number' and output.shape='scalar'"
                )

        if plan.output.type == AnswerType.TABLE and plan.sql:
            id_q_pattern = r"\b(?:ma\s+khach|ma\s+kh|ma\s+sp|ma\s+san\s+pham|ma\s+hang|ma\s+don|sku|id)\b"
            if not re.search(id_q_pattern, text):
                top_exprs = _split_top_level_select_expressions(plan.sql)
                if len(top_exprs) >= 3:
                    cust_id_cols = set()
                    cust_name_cols = set()
                    prod_id_cols = set()
                    prod_name_cols = set()
                    for t_name in plan.tables:
                        tbl_meta = context.table(t_name)
                        if not tbl_meta:
                            continue
                        for col in tbl_meta.columns:
                            if col.semantic_role == "customer_id":
                                cust_id_cols.add(col.name.lower())
                            elif col.semantic_role == "customer_name":
                                cust_name_cols.add(col.name.lower())
                            elif col.semantic_role == "product_id":
                                prod_id_cols.add(col.name.lower())
                            elif col.semantic_role == "product_name":
                                prod_name_cols.add(col.name.lower())

                    def _projects_any(expr: str, targets: set[str]) -> bool:
                        if not targets:
                            return False
                        clean_e = re.sub(r"\s+AS\s+[\w\"`]+$", "", expr.strip(), flags=re.I)
                        if re.search(
                            r"\b(count|sum|avg|min|max|total|decimal_sum|decimal_mul|decimal_div)\s*\(",
                            clean_e,
                            re.I,
                        ):
                            return False
                        tokens = {tok.lower() for tok in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", clean_e)}
                        return bool(tokens & targets)

                    has_cid = any(_projects_any(e, cust_id_cols) for e in top_exprs)
                    has_cname = any(_projects_any(e, cust_name_cols) for e in top_exprs)
                    has_pid = any(_projects_any(e, prod_id_cols) for e in top_exprs)
                    has_pname = any(_projects_any(e, prod_name_cols) for e in top_exprs)
                    if (has_cid and has_cname) or (has_pid and has_pname):
                        errors.append(
                            "UNREQUESTED_ID_COLUMN_IN_TABLE_OUTPUT:keep customer_id/product_id in GROUP BY for identity deduplication, but do NOT project the unrequested ID column in the final SELECT list when the question only asks for name and measure"
                        )

        if plan.sql and plan.sql.strip():
            col_roles_plan: dict[str, set[str | None]] = {}
            for t_name in plan.tables:
                tbl = context.table(t_name)
                if not tbl:
                    continue
                for c in tbl.columns:
                    col_roles_plan.setdefault(c.name.lower(), set()).add(c.semantic_role)
                    if c.original_name:
                        col_roles_plan.setdefault(c.original_name.lower(), set()).add(c.semantic_role)
            unambiguous_status_cols = {
                c_name for c_name, roles in col_roles_plan.items()
                if roles == {"status"}
            }
            if unambiguous_status_cols:
                escaped_status_cols = "|".join(re.escape(c) for c in sorted(unambiguous_status_cols, key=len, reverse=True))
                vn_status_terms = (
                    r"huy|hủy|huỷ|tra\s*hang|trả\s*hàng|hoan\s*thanh|hoàn\s*thành|"
                    r"dang\s*giao|đang\s*giao|da\s*nhan|đã\s*nhận"
                )
                non_canonical_status_pattern = (
                    r"(?:(?:lookup|lower|upper|trim)\s*\(\s*)*(?:[A-Za-z_]\w*\s*\.\s*)?"
                    r"[\"`\[]?(?<![A-Za-z0-9_])(?:" + escaped_status_cols + r")(?![A-Za-z0-9_])[\"`\]]?(?:\s*\))*"
                    r"\s+(?:(?:not\s+)?like\s+'[^']*(?:" + vn_status_terms + r")[^']*'|"
                    r"(?:=|!=|<>|\bin\b|\bnot\s+in\b)\s*[^;\n]*?'[^']*(?:" + vn_status_terms + r")[^']*')"
                )
                if re.search(non_canonical_status_pattern, plan.sql, re.I):
                    errors.append(
                        "NON_CANONICAL_STATUS_IN_SQL:status column has semantic_role=status so SQLite cells are canonical English ('completed', 'shipping', 'cancelled', 'returned', 'received'), NOT Vietnamese strings; use exact canonical equality/IN (e.g. = 'cancelled' or NOT IN ('cancelled', 'returned'))"
                    )

        if plan.sql and re.search(r"\bdecimal_(?:sum|mul|div)\b", plan.sql, re.I):
            order_by_matches = re.finditer(
                r"\border\s+by\b([\s\S]+?)(?=\blimit\b|\bhaving\b|\bunion\b|\)|$)",
                code,
                re.I,
            )
            order_by_clauses = [m.group(1) for m in order_by_matches]
            has_text_sort = False
            for ob in order_by_clauses:
                for dm in re.finditer(r"\bdecimal_(?:sum|mul|div)\s*\(", ob, re.I):
                    prefix = ob[:dm.start()]
                    if not re.search(r"\bcast\s*\(\s*$", prefix, re.I):
                        has_text_sort = True
                        break
                if has_text_sort:
                    break

            if not has_text_sort and order_by_clauses:
                for alias_match in re.finditer(
                    r"\bdecimal_(?:sum|mul|div)\s*\([^)]*\)\s+AS\s+([a-zA-Z_]\w*)",
                    code,
                    re.I,
                ):
                    alias = alias_match.group(1)
                    proj_prefix = code[:alias_match.start()]
                    if re.search(r"\bcast\s*\(\s*$", proj_prefix, re.I):
                        continue
                    for ob in order_by_clauses:
                        for am in re.finditer(rf"\b{re.escape(alias)}\b", ob, re.I):
                            ob_prefix = ob[:am.start()]
                            if not re.search(r"\bcast\s*\(\s*$", ob_prefix, re.I):
                                has_text_sort = True
                                break
                        if has_text_sort:
                            break
                    if has_text_sort:
                        break

            if has_text_sort:
                errors.append(
                    "TEXT_SORT_ON_DECIMAL_HELPER:decimal_sum/decimal_mul/decimal_div return TEXT which sorts lexicographically in SQLite; use SUM(...) directly or wrap in CAST(... AS REAL) in ORDER BY"
                )

        if plan.sql:
            for rm in re.finditer(
                r"(?:(!=|=|<>|\bNOT\s+IN|\bIN)\s*(\([^)]*\)|'[^']*'|\"[^\"]*\"))",
                plan.sql,
                re.I,
            ):
                op_and_arg = rm.group(0)
                start_idx = rm.start()
                prefix = plan.sql[max(0, start_idx - 80) : start_idx]
                if re.search(r"\b(?:NOT\s+)?LIKE\s*$", prefix, re.I):
                    continue
                sub_clause = re.split(r"[\n,;]\s*|\b(?:WHERE|AND|OR|ON)\b\s*", prefix, flags=re.I)[-1]
                if re.search(r"\b(?:lookup|lower)\s*\(", sub_clause, re.I):
                    continue
                has_lowercase_retail = bool(re.search(
                    r"['\"](?:khách\s+lẻ|khach\s+le|lẻ)['\"]",
                    op_and_arg,
                ))
                has_titlecase_retail = "'Khách lẻ'" in op_and_arg or '"Khách lẻ"' in op_and_arg or "'Khách Lẻ'" in op_and_arg
                if has_lowercase_retail and not has_titlecase_retail:
                    errors.append(
                        "RAW_RETAIL_EXCLUSION_WITHOUT_LOOKUP:SQLite string equality/IN is case-sensitive for Vietnamese ('Khách lẻ' vs 'khách lẻ'); write lookup(col) NOT IN ('le', 'kl', 'khach le') in SQL"
                    )
                    break

        if plan.sql and plan.output.type in {AnswerType.NUMBER, AnswerType.PERCENT}:
            has_premature = False
            for m in re.finditer(
                r"\b(?:sum|total|count)\s*\([^)]*\)\s*/\s*(?:nullif\s*\(\s*)?count\s*\(",
                code,
                re.I,
            ):
                ctx_start = max(0, m.start() - 40)
                ctx_end = min(len(code), m.end() + 40)
                window_str = code[ctx_start:ctx_end]
                if not re.search(r"\b\d+\.\d+\b|\bcast\s*\([^)]+\bas\s+(?:real|float|double)\b|\bdecimal_div\b", window_str, re.I):
                    has_premature = True
                    break

            if has_premature:
                errors.append(
                    "SQLITE_PREMATURE_ROUND_OR_INT_DIV:Do not use SQLite integer division SUM()/COUNT() which truncates fractional remainders; multiply by 1.0 (e.g. SUM(x) * 1.0 / COUNT(y)) or use decimal_div(numerator, denominator)"
                )

        if plan.metric == "generic_sql" and plan.sql:
            is_best_seller = bool(re.search(r"\b(?:ban\s+chay|chay\s+nhat|ban\s+(?:duoc\s+)?nhieu\s+nhat|hot\s+nhat)\b", text))
            has_explicit_money = bool(re.search(r"\b(?:doanh\s+thu|dthu|doanh\s+so|nhieu\s+tien|tien\s+nhat|gia\s+tri)\b", text))
            if is_best_seller and not has_explicit_money:
                has_catalog_qty = any(
                    col.semantic_role in {"qty_base", "qty_sale"}
                    for t in context.tables
                    for col in t.columns
                )
                if has_catalog_qty:
                    bs_monetary_roles = {"line_amount", "net_revenue", "gross_revenue", "total_amount", "order_revenue", "total_due"}
                    bs_qty_roles = {"qty_base", "qty_sale"}
                    bs_monetary_cols = set()
                    bs_qty_cols = set()
                    for t_name in plan.tables:
                        t = context.table(t_name)
                        if not t:
                            continue
                        for col in t.columns:
                            if col.semantic_role in bs_monetary_roles:
                                bs_monetary_cols.add(col.name)
                                bs_monetary_cols.add(f"{t.name}.{col.name}")
                            if col.semantic_role in bs_qty_roles:
                                bs_qty_cols.add(col.name)
                                bs_qty_cols.add(f"{t.name}.{col.name}")
                    for m in re.finditer(r"([A-Za-z0-9_().*+\-\/\s]+)\s+AS\s+([a-zA-Z_]\w*)", code, re.I):
                        expr_text = m.group(1)
                        alias_name = m.group(2)
                        if any(re.search(r"(?<![\w])" + re.escape(c) + r"(?![\w])", expr_text, re.I) for c in bs_monetary_cols):
                            bs_monetary_cols.add(alias_name)
                        if any(re.search(r"(?<![\w])" + re.escape(c) + r"(?![\w])", expr_text, re.I) for c in bs_qty_cols):
                            bs_qty_cols.add(alias_name)
                    ob_matches = re.finditer(
                        r"\border\s+by\b([\s\S]+?)(?=\blimit\b|\bhaving\b|\bunion\b|\)|$)",
                        code,
                        re.I,
                    )
                    ob_clauses = [m.group(1) for m in ob_matches]
                    ob_has_monetary = False
                    ob_has_qty = False
                    for ob in ob_clauses:
                        if any(re.search(r"(?<![\w])" + re.escape(c) + r"(?![\w])", ob, re.I) for c in bs_monetary_cols):
                            ob_has_monetary = True
                        if any(re.search(r"(?<![\w])" + re.escape(c) + r"(?![\w])", ob, re.I) for c in bs_qty_cols):
                            ob_has_qty = True
                    if ob_has_monetary and not ob_has_qty:
                        errors.append(
                            "BEST_SELLER_RANKED_BY_MONEY:Rulebook §3 defines 'bán chạy nhất' / 'bán nhiều nhất' by base quantity sold (qty_base / qty_sale), NOT monetary revenue; order by SUM(quantity) DESC"
                        )

        if plan.sql:
            if re.search(r"(?:<=|\bbetween\b\s+'[^']*'\s+\band\b)\s*'(?:\d{4}-\d{2}-\d{2}[T ])?(?:0?\d|1\d|2[0-3]):59'", plan.sql, re.I):
                errors.append(
                    "SHIFT_END_MINUTE_TRUNCATION:Upper-bounding HH:MM:SS timestamps with <= 'HH:59' or BETWEEN ... AND 'HH:59' drops timestamps from HH:59:01 to HH:59:59; use strict half-open < 'NextHour:00' (e.g. < '14:00' or < '22:00')"
                )

        if plan.sql and re.match(r"^\s*with\b", code, re.I):
            _, outer_code = _extract_query_blocks(code)
            cte_names = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s+as\s*\(", code, re.I)
            if cte_names and not re.search(r"\bfrom\b", outer_code, re.I):
                if any(re.search(r"\b" + re.escape(cte) + r"\s*\.", outer_code, re.I) for cte in cte_names):
                    errors.append(
                        "MISSING_OUTER_FROM_CLAUSE:Outer SELECT references CTE aliases without a FROM clause; add 'FROM "
                        + ", ".join(dict.fromkeys(cte_names[:3]))
                        + "' to the final SELECT"
                    )

        if plan.sql:
            if re.search(r"\blike\s+['\"][^'\"]*?\b\d{1,2}\s+thg\s+\d{4}[^'\"]*?['\"]", plan.sql, re.I):
                errors.append(
                    "INVALID_VN_TEXT_DATE_PATTERN:Vietnamese text dates have format '<day> thg <month> <year>' (e.g. '15 thg 9 2025'), never '<month> thg <year>'; use iso_date(col) >= 'YYYY-MM-DD' AND iso_date(col) < 'YYYY-MM-DD' (or LIKE '%thg <M> <YYYY>%')"
                )
    any_cid_keys = set()
    first_cid_col = None
    for t_name in plan.tables:
        t = context.table(t_name)
        if t:
            for col in t.columns:
                if col.semantic_role == "customer_id" or col.name.lower() in {
                    "customer_id", "makh", "ma_kh", "ma_khach_hang", "id_kh", "id_khach_hang", "ma_khach", "customer_code",
                }:
                    if first_cid_col is None:
                        first_cid_col = col.name
                    any_cid_keys.add(col.name.lower())
                    any_cid_keys.add(f"{t.name}.{col.name}".lower())

    all_grouped = list(plan.group_by)
    for match in re.findall(
        r"\bgroup\s+by\s+(.+?)(?=\border\s+by\b|\blimit\b|\bhaving\b|\bunion\b|\)|$)",
        code,
        re.I | re.S,
    ):
        for raw_col in match.split(","):
            clean_col = re.sub(r'["`]', '', raw_col).strip()
            if clean_col:
                all_grouped.append(clean_col)
                if "." in clean_col:
                    all_grouped.append(clean_col.split(".")[-1].strip())
    grouped_fields = {re.sub(r'["`]', '', g).strip().lower() for g in all_grouped}

    for name in plan.tables:
        table = context.table(name)
        if not table:
            continue
        descriptor = table.role("vehicle_description")
        plate = table.role("vehicle_plate")
        if vehicle_count and descriptor and plate:
            field = r"(?<![\w])" + re.escape(descriptor) + r"(?![\w])"
            if any(re.search(field, expression, re.I) for expression in distinct_count_arguments(code)):
                errors.append(f"ENTITY_IDENTITY_REQUIRED:{name}.{plate}; count plates, not vehicle descriptions")
        if table.grain == "installment_ledger":
            if re.search(r"\brow_number\s*\(\s*\)\s*over\s*\(\s*(?:order\s+by\s*\(?\s*select\s+1\s*\)?)?\s*\)", code, re.I):
                errors.append("UNSTABLE_LEDGER_GROUPING:use recorded contract_id, not arbitrary row ordering")
            if re.search(r"\browid\b", code, re.I) and not table.column("rowid"):
                errors.append("UNDECLARED_ROW_ID:use the recorded contract_id")
        cid = table.role("customer_id")
        if not cid:
            for col in table.columns:
                if col.semantic_role == "customer_id" or col.name.lower() in {
                    "customer_id", "makh", "ma_kh", "ma_khach_hang", "id_kh", "id_khach_hang", "ma_khach", "customer_code",
                }:
                    cid = col.name
                    break
        cname = table.role("customer_name")
        if cname and any_cid_keys:
            cname_keys = {cname.lower(), f"{table.name}.{cname}".lower()}
            if (grouped_fields & cname_keys) and not (grouped_fields & any_cid_keys):
                target_cid = cid or first_cid_col or "customer_id"
                err_msg = (
                    f"CUSTOMER_IDENTITY_GROUPING_REQUIRED:group by customer_id (e.g. {target_cid}) in SQL GROUP BY (and plan.group_by) before selecting display customer_name"
                )
                if err_msg not in errors:
                    errors.append(err_msg)
        if asks_street_address and not has_address_col and cname:
            cname_pattern = r"(?<![\w])" + re.escape(cname) + r"(?![\w])"
            if re.search(cname_pattern, code, re.I):
                subqueries, outer_code = _extract_query_blocks(code)
                all_blocks = [outer_code]
                to_scan = list(subqueries)
                while to_scan:
                    curr = to_scan.pop()
                    all_blocks.append(curr)
                    inner_blocks, _ = _extract_query_blocks(curr)
                    to_scan.extend(inner_blocks)
                filter_pattern = (
                    r"(?<![\w])" + re.escape(cname) + r"(?![\w])[\s\S]{0,60}?(?:=|\blike\b)|"
                    r"(?:=|\blike\b)[\s\S]{0,60}?(?<![\w])" + re.escape(cname) + r"(?![\w])"
                )
                is_where_filter = False
                for block in all_blocks:
                    for m in re.finditer(
                        r"\bwhere\b([\s\S]+?)(?=\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|\bunion\b|$)",
                        block,
                        re.I,
                    ):
                        if re.search(filter_pattern, m.group(1), re.I):
                            is_where_filter = True
                            break
                    if is_where_filter:
                        break
                if is_where_filter:
                    err = (
                        "ADDRESS_FILTER_ON_CUSTOMER_NAME:catalog has no customer_address column; "
                        "do not match street address against customer_name—declare metric=unsupported "
                        "with kind=missing_field and required=['customer_address']"
                    )
                    if err not in errors:
                        errors.append(err)
    if plan.sql:
        norm_code = re.sub(r"\s+", " ", code)
        uc_tables = {}
        for t_name in plan.tables:
            t = context.table(t_name)
            if t and t.grain == "unit_conversion":
                uc_tables[t.name] = t
        for t in context.tables:
            if t.grain == "unit_conversion":
                uc_tables[t.name] = t

        for uc_name, uc_table in uc_tables.items():
            pat = r"(?<!\bleft\s)(?<!\bleft\souter\s)\bjoin\s+[\"`\[]?" + re.escape(uc_name) + r"\b"
            if re.search(pat, code, re.I) or (
                re.search(r"\bjoin\s+[\"`\[]?" + re.escape(uc_name) + r"\b", norm_code, re.I)
                and not re.search(r"\bleft(?:\s+outer)?\s+join\s+[\"`\[]?" + re.escape(uc_name) + r"\b", norm_code, re.I)
            ):
                errors.append(
                    f"INNER_JOIN_ON_UNIT_CONVERSION:table {uc_table.name} only lists products with multi-unit conversions; use LEFT JOIN (with COALESCE multiplier 1), never INNER JOIN, so standard items without conversion rows are not dropped"
                )
    order_line_tables = [
        t for t in plan.tables
        if (tbl := context.table(t)) and tbl.grain == "order_line"
    ]
    payment_event_tables = [
        t for t in plan.tables
        if (tbl := context.table(t)) and tbl.grain == "payment_event"
    ]
    if order_line_tables and payment_event_tables:
        def _mentions_any(text: str, tables: list[str]) -> bool:
            return any(
                re.search(r"(?<![\w])" + re.escape(t) + r"(?![\w])", text, re.I)
                for t in tables
            )

        order_line_in_sql = _mentions_any(code, order_line_tables)
        payment_event_in_sql = _mentions_any(code, payment_event_tables)
        if order_line_in_sql and payment_event_in_sql:
            subqueries, outer_code = _extract_query_blocks(code)
            fanout_err = (
                "MULTI_CHILD_FANOUT_JOIN:aggregate payment_event or order_line by order_id in a CTE/subquery before joining"
            )
            # 1. If outer_code directly mentions BOTH order_line and payment_event
            if _mentions_any(outer_code, order_line_tables) and _mentions_any(outer_code, payment_event_tables):
                if fanout_err not in errors:
                    errors.append(fanout_err)
            # 2. If any single subquery directly mentions BOTH without an inner subquery aggregating one of them
            for sq in subqueries:
                if _mentions_any(sq, order_line_tables) and _mentions_any(sq, payment_event_tables):
                    inner_subs, sq_outer = _extract_query_blocks(sq)
                    if _mentions_any(sq_outer, order_line_tables) and _mentions_any(sq_outer, payment_event_tables):
                        if fanout_err not in errors:
                            errors.append(fanout_err)
                    else:
                        has_agg_inner = any(
                            (_mentions_any(inner, order_line_tables) or _mentions_any(inner, payment_event_tables))
                            and bool(re.search(r"\bgroup\s+by\b|\bdistinct\b", inner, re.I))
                            for inner in inner_subs
                        )
                        if not has_agg_inner and fanout_err not in errors:
                            errors.append(fanout_err)
            # 3. Verify at least one of order_line or payment_event is referenced inside an aggregating subquery
            all_subs: list[str] = []
            to_scan = list(subqueries)
            while to_scan:
                curr = to_scan.pop()
                all_subs.append(curr)
                inner_blocks, _ = _extract_query_blocks(curr)
                to_scan.extend(inner_blocks)
            has_aggregated_subquery = any(
                (_mentions_any(sq, order_line_tables) or _mentions_any(sq, payment_event_tables))
                and bool(re.search(r"\bgroup\s+by\b|\bdistinct\b", sq, re.I))
                for sq in all_subs
            )
            if not has_aggregated_subquery and fanout_err not in errors:
                errors.append(fanout_err)
    if (
        "so luong am" in normalize_lookup(context.readme)
        and plan.metric in {"cancellation_rate", "generic_sql"}
        and not plan.template_eligible
    ):
        cancel_pat = r"\b(ty|ti)\s+le\s+(don\s+)?(bi\s+)?huy\b|\bhuy\s+don\b|(?:%|phan\s+tram)\s+don\s+(?:hang\s+)?(?:bi\s+)?huy\b"
        if re.search(cancel_pat, pct_text) or re.search(cancel_pat, text):
            for t_name in plan.tables:
                table = context.table(t_name)
                if not table or table.grain != "order_line":
                    continue
                qty_col = table.role("qty_sale")
                if qty_col and re.search(r"\bcount\s*\(\s*distinct\b", code, re.I):
                    if not re.search(r"(?<![\w])" + re.escape(qty_col) + r"(?![\w])", code):
                        errors.append(
                            f"RETURN_LINE_IN_ORDER_COHORT:filter CAST({table.name}.{qty_col} AS REAL) > 0 when counting placed orders on a ledger with negative return quantities"
                        )
    return errors
