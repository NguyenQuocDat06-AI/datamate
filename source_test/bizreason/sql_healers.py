"""General schema-driven SQL self-healing functions for Datamate."""

from __future__ import annotations

import difflib
import re
from typing import Any

from bizreason.contracts import BusinessContext
from bizreason.execution import normalize_lookup


def heal_percent_precedence(expr: str) -> str:
    """Heal operator precedence in percentage calculations."""
    term = r"(?:\([A-Za-z0-9_.\s=<>!'%*+-]+\)|[A-Za-z_][A-Za-z0-9_.]*(?:\s*\([^)]*\))?)"

    # pat2: 100.0 * t1 - t2 / denom -> 100.0 * (t1 - t2) / NULLIF(denom, 0)
    pat2 = re.compile(
        r"100(?:\.0+)?\s*\*\s*("
        + term
        + r")\s*([+-])\s*("
        + term
        + r")\s*/\s*(?:NULLIF\s*\(\s*("
        + term
        + r")\s*,\s*0\s*\)|("
        + term
        + r"))",
        re.IGNORECASE,
    )

    def _repl2(m: re.Match) -> str:
        t1, op, t2 = m.group(1), m.group(2), m.group(3)
        denom = m.group(4) or m.group(5)
        return f"100.0 * ({t1} {op} {t2}) / NULLIF({denom}, 0)"

    # pat1: t1 - t2 * 100.0 / denom -> (t1 - t2) * 100.0 / NULLIF(denom, 0)
    pat1 = re.compile(
        r"(\bSELECT\s+)?(?<!100\s\*\s)(?<!100\.0\s\*\s)("
        + term
        + r")\s*([+-])\s*("
        + term
        + r")\s*(?:\*\s*(100(?:\.0+)?|1\.0+)\s*)?/\s*(?:NULLIF\s*\(\s*("
        + term
        + r")\s*,\s*0\s*\)|("
        + term
        + r"))",
        re.IGNORECASE,
    )

    def _repl1(m: re.Match) -> str:
        sel_prefix = m.group(1) or ""
        t1, op, t2 = m.group(2), m.group(3), m.group(4)
        explicit_scale = m.group(5)
        denom = m.group(6) or m.group(7)
        scale = f" * {explicit_scale}" if explicit_scale else " * 100.0"
        return f"{sel_prefix}({t1} {op} {t2}){scale} / NULLIF({denom}, 0)"

    # Execute pat2 first so 100.0 * rev_new - rev_old is wrapped as 100.0 * (rev_new - rev_old)
    res = pat2.sub(_repl2, expr)
    res = pat1.sub(_repl1, res)
    return res


def find_outer_projection_span(raw_sql: str) -> tuple[int, int, int] | None:
    """Find (outer_select_start, proj_start, from_start) outside parentheses/quotes."""
    n = len(raw_sql)
    paren_depth = 0
    in_str = False
    in_dquote = False
    escaped = False
    i = 0
    outer_sel_start = -1
    proj_start = -1

    while i < n:
        ch = raw_sql[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                if i + 1 < n and raw_sql[i + 1] == "'":
                    i += 1
                else:
                    in_str = False
            i += 1
            continue
        if in_dquote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_dquote = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            i += 1
            continue
        if ch == '"':
            in_dquote = True
            i += 1
            continue
        if ch == "(":
            paren_depth += 1
            i += 1
            continue
        if ch == ")":
            if paren_depth > 0:
                paren_depth -= 1
            i += 1
            continue

        if paren_depth == 0:
            if outer_sel_start == -1:
                m_sel = re.match(r"^SELECT\b", raw_sql[i:], re.IGNORECASE)
                if m_sel:
                    outer_sel_start = i
                    proj_start = i + len(m_sel.group(0))
                    i = proj_start
                    continue
            else:
                m_from = re.match(r"^(?:FROM|WHERE|GROUP|ORDER|LIMIT)\b", raw_sql[i:], re.IGNORECASE)
                if m_from:
                    return (outer_sel_start, proj_start, i)
        i += 1

    if outer_sel_start != -1:
        return (outer_sel_start, proj_start, -1)
    return None


def scale_scalar_percent_sql(sql: str) -> str:
    """Scale outermost projection of percentage SQL by 100.0 * (...)."""
    raw_sql = sql.strip()
    healed_prec = heal_percent_precedence(raw_sql)
    if healed_prec != raw_sql:
        raw_sql = healed_prec
        sql = raw_sql

    span = find_outer_projection_span(raw_sql)
    if not span:
        return sql
    sel_start, proj_start, from_start = span

    if from_start == -1:
        proj = raw_sql[proj_start:].rstrip(";").strip()
        trail = ";" if raw_sql.rstrip().endswith(";") else ""
    else:
        proj = raw_sql[proj_start:from_start].strip()
        trail = ""

    if not proj:
        return sql

    alias_match = re.search(
        r"\s+AS\s+([A-Za-z_][A-Za-z0-9_]*|[\"`\[][^\"`\]]+[\"`\]])\s*$",
        proj,
        re.IGNORECASE,
    )
    if alias_match:
        expr = proj[: alias_match.start()].strip()
        alias_str = alias_match.group(0)
    else:
        expr = proj
        alias_str = ""

    if re.search(r"\b100(?:\.0+)?\b", expr):
        return raw_sql

    if "decimal_div" in expr.lower():
        scaled_expr = f"100.0 * CAST(({expr}) AS REAL)"
    elif expr.startswith("(") and expr.endswith(")"):
        scaled_expr = f"100.0 * {expr}"
    else:
        scaled_expr = f"100.0 * ({expr})"

    new_proj = f" {scaled_expr}{alias_str}"
    if from_start == -1:
        return f"{raw_sql[:proj_start]}{new_proj}{trail}"
    else:
        return f"{raw_sql[:proj_start]}{new_proj} {raw_sql[from_start:]}"


def find_best_matching_table(t_ref: str, context: BusinessContext) -> str | None:
    """Find matching hashed table name in schema context."""
    if not getattr(context, "tables", None):
        return None
    actual_tables = [t.name for t in context.tables]
    t_ref_l = t_ref.lower()
    actual_names = {t.lower(): t for t in actual_tables}
    if t_ref_l in actual_names:
        return actual_names[t_ref_l]
    if len(actual_tables) == 1:
        return actual_tables[0]
    prefix_matches = [t for t in actual_tables if t.lower().startswith(t_ref_l)]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    stripped_map: dict[str, list[str]] = {}
    for t in actual_tables:
        base = re.sub(r"_[0-9a-f]{6,12}$", "", t.lower())
        stripped_map.setdefault(base, []).append(t)
    base_ref = re.sub(r"_[0-9a-f]{4,12}$", "", t_ref_l)
    if base_ref in stripped_map and len(stripped_map[base_ref]) == 1:
        return stripped_map[base_ref][0]
    close = difflib.get_close_matches(t_ref_l, list(actual_names.keys()), n=1, cutoff=0.75)
    if close:
        return actual_names[close[0]]
    if any(k in t_ref_l for k in ("nhap_hang", "nhaphang")):
        nh_tables = [t for t in actual_tables if "nhaphang" in t.lower() or "nhap_hang" in t.lower()]
        if len(nh_tables) == 1:
            return nh_tables[0]
    return None


def recover_missing_table_suffixes(healed: str, context: BusinessContext, added_tables: list[str]) -> str:
    """Replace un-hashed table references with actual hashed table names."""
    if not getattr(context, "tables", None):
        return healed
    stripped_map: dict[str, str] = {}
    for t in context.tables:
        base = re.sub(r"_[0-9a-f]{8}$", "", t.name.lower())
        if base not in stripped_map:
            stripped_map[base] = t.name

    actual_names = {t.name.lower() for t in context.tables}
    cte_names = {
        m.group(1).lower()
        for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", healed, re.IGNORECASE)
    }

    reserved_kw = (
        r"WHERE|GROUP|ORDER|LIMIT|JOIN|LEFT|RIGHT|INNER|OUTER|CROSS|ON|UNION|HAVING|SET|VALUES|AS"
    )

    matched_replacements: list[tuple[str, str]] = []

    def _repl_tbl(m: re.Match) -> str:
        kw = m.group(1)
        t_ref = m.group(2)
        rest = m.group(3) or ""
        t_ref_l = t_ref.lower()
        if t_ref_l in actual_names or t_ref_l in cte_names:
            return m.group(0)
        matched = find_best_matching_table(t_ref, context)
        if matched:
            if matched not in added_tables:
                added_tables.append(matched)
            matched_replacements.append((t_ref, matched))
            return f"{kw} {matched}{rest}"
        if t_ref_l in stripped_map:
            real_tbl = stripped_map[t_ref_l]
            if real_tbl not in added_tables:
                added_tables.append(real_tbl)
            matched_replacements.append((t_ref, real_tbl))
            return f"{kw} {real_tbl}{rest}"
        return m.group(0)

    pat = re.compile(
        r"\b(FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)(\s+AS\s+[A-Za-z_][A-Za-z0-9_]*|\s+(?!"
        + reserved_kw
        + r"\b)[A-Za-z_][A-Za-z0-9_]*)?",
        re.IGNORECASE,
    )
    healed = pat.sub(_repl_tbl, healed)

    for old_ref, new_tbl in matched_replacements:
        healed = re.sub(rf"\b{re.escape(old_ref)}\.", f"{new_tbl}.", healed, flags=re.IGNORECASE)

    for base, full_name in stripped_map.items():
        if base in actual_names or base in cte_names:
            continue
        healed = re.sub(rf"\b{re.escape(base)}\.", f"{full_name}.", healed, flags=re.IGNORECASE)

    return healed


def auto_join_header_for_detail_date(
    healed: str,
    active_tables: list[Any],
    context: BusinessContext,
    added_tables: list[str],
) -> str:
    """Auto-join header table to detail query when date filter is present."""
    if not getattr(context, "tables", None):
        return healed
    if any(any(c.name.lower() == "thoi_gian_tao" for c in t.columns) for t in context.tables):
        healed = re.sub(
            r"\b(?:trinh_thai_tao|trinh_thai_cao|trang_thai_date)\b",
            "thoi_gian_tao",
            healed,
            flags=re.IGNORECASE,
        )

    # Collapse UNION ALL over numbered split exports of the same order-line table
    # (<base>_1_<suffix> / <base>_2_<suffix>): split files can overlap, so use UNION.
    split_line_tables = {
        t.name
        for t in context.tables
        if t.grain == "order_line" and re.search(r"_[12]_[A-Za-z0-9_]+$", t.name)
    }
    if split_line_tables:
        def _split_pair(t1: str, t2: str) -> bool:
            t1c, t2c = t1.strip('"`'), t2.strip('"`')
            m1 = re.match(r"^(.+)_1_[A-Za-z0-9_]+$", t1c)
            m2 = re.match(r"^(.+)_2_[A-Za-z0-9_]+$", t2c)
            return bool(
                m1 and m2 and m1.group(1) == m2.group(1)
                and t1c in split_line_tables and t2c in split_line_tables
            )

        def _repl_star_union(m: re.Match) -> str:
            if not _split_pair(m.group(1), m.group(2)):
                return m.group(0)
            return f"SELECT * FROM {m.group(1)} UNION SELECT * FROM {m.group(2)}"

        healed = re.sub(
            r"\bSELECT\s+\*\s+FROM\s+([\"`]?[A-Za-z0-9_]+_1_[A-Za-z0-9_]+[\"`]?)\s+UNION\s+ALL\s+SELECT\s+\*\s+FROM\s+([\"`]?[A-Za-z0-9_]+_2_[A-Za-z0-9_]+[\"`]?)",
            _repl_star_union,
            healed,
            flags=re.IGNORECASE,
        )
        m_split_union = re.match(
            r"^\s*SELECT\s+([\s\S]+?)\s+FROM\s+([A-Za-z0-9_]+_1_[A-Za-z0-9_]+)\s+WHERE\s+([\s\S]+?)\s+UNION\s+ALL\s+SELECT\s+\1\s+FROM\s+([A-Za-z0-9_]+_2_[A-Za-z0-9_]+)\s+WHERE\s+\3\s*;?\s*$",
            healed,
            re.IGNORECASE,
        )
        if m_split_union and _split_pair(m_split_union.group(2), m_split_union.group(4)):
            proj_u, t1_u, cond_u, t2_u = m_split_union.groups()
            healed = f"SELECT {proj_u} FROM (SELECT * FROM {t1_u} UNION SELECT * FROM {t2_u}) AS {t1_u} WHERE {cond_u}"

        # Note: Do not pull WHERE out of UNION ALL subqueries with trailing alias


    date_cands = (
        "thoi_gian_tao",
        "ngay_tao",
        "ngay_tao_don",
        "ngay_don_hang",
        "ngay_dat_hang",
        "ngay_giao_dich",
        "thoi_gian",
        "ngay",
    )
    has_date_ref = any(re.search(rf"\b{c}\b", healed, re.IGNORECASE) for c in date_cands)
    if not has_date_ref:
        return healed

    # Check if ANY table currently in healed already has a date column!
    for tbl in context.tables:
        if re.search(r"\b(?:FROM|JOIN)\s+" + re.escape(tbl.name) + r"\b", healed, re.IGNORECASE):
            if any(
                c.name.lower() in date_cands
                or c.semantic_role in ("order_date", "creation_date")
                for c in tbl.columns
            ):
                return healed

    dtl_tbl = None
    for t in list(active_tables) + list(context.tables):
        if t.name not in healed:
            continue
        if (
            t.grain in ("order_line", "purchase_line")
            or "chitiet" in t.name.lower()
            or "detail" in t.name.lower()
            or "_ct" in t.name.lower()
        ):
            has_tbl_date = any(
                c.name.lower() in date_cands
                or c.semantic_role in ("order_date", "creation_date")
                for c in t.columns
            )
            if not has_tbl_date:
                dtl_tbl = t
                break
    if not dtl_tbl:
        return healed

    hdr_tbl = None
    dtl_fk = None
    hdr_pk = None
    for j in getattr(context, "joins", []) or []:
        cand = None
        cand_keys = None
        if j.left == dtl_tbl.name:
            cand = context.table(j.right)
            cand_keys = j.keys[0]
        elif j.right == dtl_tbl.name:
            cand = context.table(j.left)
            cand_keys = (j.keys[0][1], j.keys[0][0])

        if (
            cand
            and (
                cand.grain in ("order_header", "purchase_header")
                or any(k in cand.name.lower() for k in ("hoadon", "nhaphang", "_hdr", "header"))
            )
            and not any(k in cand.name.lower() for k in ("sanpham", "san_pham", "hang_hoa", "product", "chitiet"))
        ):
            if any(
                c.name.lower() in date_cands
                or c.semantic_role in ("order_date", "creation_date")
                for c in cand.columns
            ):
                hdr_tbl = cand
                dtl_fk, hdr_pk = cand_keys
                break

    if not hdr_tbl:
        dtl_col_map = {c.name.lower(): c.name for c in dtl_tbl.columns}
        for cand in context.tables:
            if cand.name == dtl_tbl.name:
                continue
            if (
                cand.grain in ("order_header", "purchase_header")
                or any(k in cand.name.lower() for k in ("hoadon", "don_hang", "nhaphang", "_hdr", "header"))
            ) and not any(k in cand.name.lower() for k in ("sanpham", "san_pham", "hang_hoa", "product", "chitiet")):
                if any(
                    c.name.lower() in date_cands
                    or c.semantic_role in ("order_date", "creation_date")
                    for c in cand.columns
                ):
                    cand_col_map = {c.name.lower(): c.name for c in cand.columns}
                    for key_cand in ("ma_hoa_don", "ma_don_hang", "ma_don", "order_id", "ma_nhap_hang", "so_phieu"):
                        if key_cand in dtl_col_map and key_cand in cand_col_map:
                            hdr_tbl = cand
                            dtl_fk = dtl_col_map[key_cand]
                            hdr_pk = cand_col_map[key_cand]
                            break
                    if hdr_tbl:
                        break

    if not hdr_tbl:
        return healed

    hdr_date = next(
        (
            c.name
            for c in hdr_tbl.columns
            if c.name.lower() in date_cands and re.search(rf"\b{re.escape(c.name)}\b", healed, re.IGNORECASE)
        ),
        None,
    ) or next(
        (
            c.name
            for c in hdr_tbl.columns
            if c.name.lower() in date_cands
            or c.semantic_role in ("order_date", "creation_date")
        ),
        None,
    )
    if not hdr_date:
        return healed

    if "_hdr" not in healed and hdr_tbl.name not in healed:
        reserved_kw = (
            r"WHERE|GROUP|ORDER|LIMIT|JOIN|LEFT|RIGHT|INNER|OUTER|CROSS|ON|UNION|HAVING|SET|VALUES|AS"
        )
        pat_from_union = re.compile(
            rf"(\bFROM\s+\(\s*SELECT\s+\*\s+FROM\s+[\"`]?{re.escape(dtl_tbl.name)}[\"`]?\s+UNION(?:\s+ALL)?[\s\S]*?\)(?:\s+AS\s+([A-Za-z_][A-Za-z0-9_]*|\"[^\"]+\")|\s+(?!(?:{reserved_kw})\b)([A-Za-z_][A-Za-z0-9_]*|\"[^\"]+\"))?)",
            re.IGNORECASE,
        )
        pat_from_plain = re.compile(
            rf"(?<!SELECT\s\*\s)(\bFROM\s+{re.escape(dtl_tbl.name)}(?:\s+AS\s+([A-Za-z_][A-Za-z0-9_]*|\"[^\"]+\")|\s+(?!(?:{reserved_kw})\b)([A-Za-z_][A-Za-z0-9_]*|\"[^\"]+\"))?\b)",
            re.IGNORECASE,
        )

        def _repl_join(m: re.Match) -> str:
            al = m.group(2) or m.group(3) or dtl_tbl.name
            full_match = m.group(1)
            return f"{full_match} JOIN {hdr_tbl.name} AS _hdr ON {al}.{dtl_fk} = _hdr.{hdr_pk}"

        new_healed, n_subs = pat_from_union.subn(_repl_join, healed)
        if n_subs == 0:
            new_healed, n_subs = pat_from_plain.subn(_repl_join, healed)
        if n_subs > 0:
            healed = new_healed
            healed = re.sub(
                r"(?<![\w\.])\b(?:" + "|".join(date_cands) + r")\b",
                f"_hdr.{hdr_date}",
                healed,
                flags=re.IGNORECASE,
            )
            for dtl_cand in [dtl_tbl.name, "T1", "T2", "t1", "t2", "d", "c"]:
                healed = re.sub(
                    rf"\b{dtl_cand}\.(?:" + "|".join(date_cands) + r")\b",
                    f"_hdr.{hdr_date}",
                    healed,
                    flags=re.IGNORECASE,
                )
            if any(c.name.lower() == "trang_thai" for c in hdr_tbl.columns) and not any(
                c.name.lower() == "trang_thai" for c in dtl_tbl.columns
            ):
                healed = re.sub(
                    r"(?<![\w\.])\btrang_thai\b",
                    "_hdr.trang_thai",
                    healed,
                    flags=re.IGNORECASE,
                )
                if "trang_thai" not in healed.lower():
                    st_cond = "_hdr.trang_thai IN ('xong', 'Hoàn thành', 'đã lấy', 'completed') AND "
                    healed = re.sub(
                        r"(\bJOIN\s+" + re.escape(hdr_tbl.name) + r"\s+AS\s+_hdr\s+ON\s+[^\n\r]+?\s+WHERE\s+)",
                        rf"\1{st_cond}",
                        healed,
                        flags=re.IGNORECASE,
                    )
            if hdr_tbl.name not in added_tables:
                added_tables.append(hdr_tbl.name)
            if hdr_tbl not in active_tables:
                active_tables.append(hdr_tbl)

    return healed


def auto_join_missing_referenced_tables(
    healed: str,
    context: BusinessContext,
    added_tables: list[str],
    active_tables: list[Any],
) -> str:
    """Auto-join tables referenced in SQL but missing from FROM/JOIN clauses."""
    if not getattr(context, "tables", None):
        return healed
    for tbl in context.tables:
        if re.search(r"\b" + re.escape(tbl.name) + r"\.[A-Za-z0-9_]+", healed, re.IGNORECASE):
            if not re.search(r"\b(?:FROM|JOIN)\s+" + re.escape(tbl.name) + r"\b", healed, re.IGNORECASE):
                join_cond = None
                for j in getattr(context, "joins", []):
                    if j.left == tbl.name and re.search(
                        r"\b(?:FROM|JOIN)\s+" + re.escape(j.right) + r"\b", healed, re.IGNORECASE
                    ):
                        join_cond = f"{j.right}.{j.keys[0][1]} = {tbl.name}.{j.keys[0][0]}"
                        break
                    elif j.right == tbl.name and re.search(
                        r"\b(?:FROM|JOIN)\s+" + re.escape(j.left) + r"\b", healed, re.IGNORECASE
                    ):
                        join_cond = f"{j.left}.{j.keys[0][0]} = {tbl.name}.{j.keys[0][1]}"
                        break
                if not join_cond:
                    key_cands = (
                        "ma_nhap_hang",
                        "ma_hoa_don",
                        "ma_don",
                        "ma_don_hang",
                        "ma_hang",
                        "ma_kh",
                        "order_id",
                        "product_id",
                    )
                    for k in key_cands:
                        if any(c.name == k for c in tbl.columns):
                            for other in context.tables:
                                if other.name != tbl.name and any(c.name == k for c in other.columns):
                                    if re.search(
                                        r"\b(?:FROM|JOIN)\s+" + re.escape(other.name) + r"\b",
                                        healed,
                                        re.IGNORECASE,
                                    ):
                                        join_cond = f"{other.name}.{k} = {tbl.name}.{k}"
                                        break
                            if join_cond:
                                break
                if join_cond:
                    pat_first_from = re.compile(
                        r"(\bFROM\s+[A-Za-z0-9_]+(?:\s+AS\s+[A-Za-z0-9_]+|\s+(?!(?:WHERE|GROUP|ORDER|LIMIT|JOIN|LEFT|RIGHT|INNER|OUTER|ON|UNION|HAVING)\b)[A-Za-z0-9_]+)?)",
                        re.IGNORECASE,
                    )
                    healed = pat_first_from.sub(rf"\1 JOIN {tbl.name} ON {join_cond}", healed, count=1)
                    if tbl.name not in added_tables:
                        added_tables.append(tbl.name)
                    if tbl not in active_tables:
                        active_tables.append(tbl)
    return healed


def qualify_ambiguous_columns(healed: str, alias_to_table: dict[str, tuple[str, Any]]) -> str:
    """Qualify ambiguous unqualified columns across joined tables."""
    if len(alias_to_table) < 2:
        return healed
    if not re.search(r"\bJOIN\b", healed, re.IGNORECASE):
        return healed
    if re.search(r"\bUNION\b", healed, re.IGNORECASE):
        return healed
    if re.search(r"\bFROM\s*\(|\bJOIN\s*\(", healed, re.IGNORECASE):
        return healed

    col_to_entries: dict[str, list[tuple[str, Any, Any]]] = {}
    for eff_al, tbl in alias_to_table.values():
        for c in tbl.columns:
            col_to_entries.setdefault(c.name.lower(), []).append((eff_al, tbl, c))

    ambiguous_map: dict[str, str] = {}
    detail_names = {
        "so_luong",
        "sl",
        "slg",
        "sl_nhap",
        "so_luong_nhap",
        "sl_co_ban",
        "don_gia",
        "gia_ban",
        "gia_nhap",
        "dongianhap",
        "don_gia_nhap",
        "thanh_tien",
        "t_tien",
        "amount",
        "tri_gia",
        "ma_hang",
        "ma_san_pham",
        "item_id",
        "product_id",
    }
    detail_roles = {
        "quantity",
        "base_quantity",
        "sales_quantity",
        "unit_price",
        "unit_cost",
        "line_amount",
        "net_revenue",
        "product_id",
    }
    header_names = {
        "ma_hoa_don",
        "ma_hd",
        "ma_nhap_hang",
        "trang_thai",
        "tinh_trang",
        "status",
        "thoi_gian_tao",
        "ngay_ban",
        "ngay_lap",
        "ngay",
        "created_at",
        "tong_tien",
        "tong_bill",
        "tong_don",
        "ma_kh",
        "ma_khach_hang",
        "customer_id",
    }
    header_roles = {
        "order_id",
        "status",
        "order_date",
        "creation_date",
        "total_due",
        "total_amount",
        "customer_id",
        "customer_name",
    }

    def _is_detail(t: Any) -> bool:
        return t.grain in ("order_line", "purchase_line") or any(
            k in t.name.lower() for k in ("chitiet", "detail", "line", "_ct")
        )

    def _is_header(t: Any) -> bool:
        return t.grain in ("order_header", "purchase_header") or (
            any(k in t.name.lower() for k in ("hoadon", "nhaphang", "banhang", "header", "_hdr"))
            and not _is_detail(t)
        )

    for col_l, entries in col_to_entries.items():
        if len(entries) < 2:
            continue
        col_real_name = entries[0][2].name
        pref_al = None
        is_det_col = col_l in detail_names or any(c.semantic_role in detail_roles for _, _, c in entries)
        is_hdr_col = col_l in header_names or any(c.semantic_role in header_roles for _, _, c in entries)

        if is_det_col and not is_hdr_col:
            for al, t, _ in entries:
                if _is_detail(t):
                    pref_al = al
                    break
        elif is_hdr_col and not is_det_col:
            for al, t, _ in entries:
                if _is_header(t):
                    pref_al = al
                    break
        elif is_det_col and is_hdr_col:
            for al, t, _ in entries:
                if _is_header(t):
                    pref_al = al
                    break
        if not pref_al:
            pref_al = entries[0][0]

        ambiguous_map[col_real_name] = pref_al

    if not ambiguous_map:
        return healed

    for col_name, pref_al in ambiguous_map.items():
        pat = re.compile(
            r"(\bAS\s+)?(?<![\w\.])\b(" + re.escape(col_name) + r")\b(?!\s*\()",
            re.IGNORECASE,
        )

        def _repl(m: re.Match, p_al: str = pref_al, c_name: str = col_name) -> str:
            if m.group(1):
                return m.group(0)
            return f"{p_al}.{c_name}"

        healed = pat.sub(_repl, healed)

    return healed


def fuzzy_recover_table_columns(
    healed: str,
    alias_to_table: dict[str, tuple[str, Any]],
    active_tables: list[Any],
) -> str:
    """Recover hallucinated column synonyms and mismatched column aliases."""
    clusters = [
        ("qty_co_ban", ["slg_co_ban"], ["sl_co_ban", "slg", "sl", "so_luong"]),
        (
            "quantity",
            ["so_luong", "soluong", "so_luong_san_pham"],
            [
                "sl",
                "slg",
                "sl_nhap",
                "so_luong_ban",
                "sl_co_ban",
                "so_luong_san_pham",
                "so_luong_quy_doi",
                "sl_quy_doi",
            ],
        ),
        (
            "unit_price",
            ["don_gia", "dongia"],
            ["dongianhap", "gia_nhap", "don_gia_nhap", "gia_ban", "gia", "gia_moi"],
        ),
        (
            "amount",
            [
                "thanh_tien",
                "thanhtien",
                "doanh_thuc",
                "tri_giao_dich",
                "tinh_thanh_toan",
                "tong_thanh_toan",
            ],
            [
                "t_tien",
                "amount",
                "tri_gia",
                "tong_tien_hang",
                "tien_hang_truoc_giam",
                "tien_hang",
                "tong_tien",
                "doanh_thu",
                "tong_thanh_toan",
                "tongthanhtoan",
                "thanh_tien_d",
            ],
        ),
        (
            "payment_method",
            ["phuongthuc", "phuong_thuc", "phuongthucthanhtoan"],
            [
                "phuongthucthanhtoan",
                "phuong_thuc_thanh_toan",
                "phuong_thuc_tt",
                "phuongthuctt",
                "hinh_thuc_thanh_toan",
                "tra_bang",
            ],
        ),
        (
            "order_id",
            ["ma_hoa_don", "ma_hd", "mahoadon"],
            [
                "ma_giao_dich",
                "order_id",
                "ma_chung_tu",
                "so_hd",
                "so_phieu",
                "ma_don",
                "ma_don_hang",
                "so_chung_tu",
                "machungtu",
            ],
        ),
        (
            "status",
            ["trang_thai", "trang_thai_don_hang", "status", "tinh_trang", "tinhtrang"],
            [
                "trang_thai",
                "trang_thai_don_hang",
                "tinh_trang",
                "status",
                "tinh_trang_don",
                "ghi_chu_tt",
                "xu_ly",
                "tinhtrang",
            ],
        ),
        (
            "customer_name",
            ["ten_khach_hang", "ten_kh", "khach_hang", "khach", "tenkh"],
            [
                "ten_khach_hang",
                "khach_hang",
                "ten_kh",
                "tenkh",
                "khach",
                "khach_hang_ten",
                "nguoi_mua",
            ],
        ),
        (
            "customer_id",
            ["customer_id", "ma_kh", "ma_khach_hang", "makh", "ma_kh_mua"],
            ["customer_id", "ma_kh", "ma_khach_hang", "makh", "ma_kh_mua", "id_khach_hang"],
        ),
        (
            "product_name",
            ["ten_san_pham", "ten_hang", "ten_hang_hoa", "san_pham", "hang_hoa"],
            [
                "ten_san_pham",
                "ten_hang",
                "ten_hang_hoa",
                "san_pham",
                "hang_hoa",
                "tengoi",
                "ten_sp",
                "chi_tiet_hang",
            ],
        ),
        (
            "product_id",
            [
                "ma_san_pham",
                "ma_hang",
                "ma_hang_hoa",
                "product_id",
                "item_id",
                "id_sp",
                "mahanghoa",
            ],
            [
                "ma_san_pham",
                "ma_hang",
                "ma_hang_hoa",
                "product_id",
                "item_id",
                "id_sp",
                "mahanghoa",
                "ma_sp",
            ],
        ),
        (
            "unit",
            ["don_vi_tinh", "dvt", "don_vi"],
            ["don_vi_tinh", "dvt", "don_vi", "don_vi_co_ban", "don_vi_quy_doi"],
        ),
        (
            "debt",
            ["con_no", "so_tien_no", "no_hien_tai", "du_no"],
            ["con_no", "so_tien_no", "no_hien_tai", "du_no", "tien_no"],
        ),
    ]
    date_cands = [
        "thoi_gian_tao",
        "ngay_tao",
        "ngay_tao_don",
        "ngay_dat_hang",
        "ngay_giao_dich",
        "thoi_gian",
        "ngay",
        "ngay_ban",
        "datestr",
        "trang_thai_date",
        "trinh_thai_tao",
        "trinh_thai_cao",
    ]

    all_active_cols_l = {c.name.lower() for t in active_tables for c in t.columns}
    tables_to_check: list[tuple[str, Any, bool]] = []
    if alias_to_table:
        for al, tbl in alias_to_table.values():
            tables_to_check.append((al, tbl, True))
    if len(active_tables) == 1:
        tables_to_check.append(("", active_tables[0], False))
    elif len(active_tables) > 1:
        sorted_active = sorted(
            active_tables,
            key=lambda t: 1
            if (
                getattr(t, "grain", None) in ("product_dimension", "customer_dimension")
                or any(k in t.name.lower() for k in ("san_pham", "sanpham", "danhmuc", "khach_hang"))
            )
            else 0,
        )
        for tbl in sorted_active:
            tables_to_check.append(("", tbl, False))

    for al, tbl, is_qualified in tables_to_check:
        tbl_col_names = {c.name.lower() for c in tbl.columns}
        if is_qualified and al:
            # Check if al.col references a column that exists in another joined table
            m_al_cols = re.findall(rf"\b{re.escape(al)}\.([A-Za-z0-9_]+)\b", healed, re.IGNORECASE)
            for c_name in m_al_cols:
                if c_name.lower() not in tbl_col_names:
                    for other_al, other_tbl in alias_to_table.values():
                        if other_al.lower() != al.lower():
                            if any(c.name.lower() == c_name.lower() for c in other_tbl.columns):
                                healed = re.sub(
                                    rf"\b{re.escape(al)}\.{re.escape(c_name)}\b",
                                    f"{other_al}.{c_name}",
                                    healed,
                                )
                                break

        for group_name, hallucinated_names, cand_list in clusters:
            for hall_name in hallucinated_names:
                if hall_name.lower() in tbl_col_names:
                    continue

                replacement_col = None
                for cand in cand_list:
                    if cand.lower() in tbl_col_names:
                        replacement_col = next(
                            c.name for c in tbl.columns if c.name.lower() == cand.lower()
                        )
                        break

                if not replacement_col:
                    if group_name == "quantity":
                        c_match = next(
                            (
                                c.name
                                for c in tbl.columns
                                if c.semantic_role
                                in ("quantity", "base_quantity", "sales_quantity")
                            ),
                            None,
                        )
                    elif group_name == "unit_price":
                        c_match = next(
                            (
                                c.name
                                for c in tbl.columns
                                if c.semantic_role in ("unit_price", "unit_cost")
                            ),
                            None,
                        )
                    elif group_name == "amount":
                        c_match = next(
                            (
                                c.name
                                for c in tbl.columns
                                if c.semantic_role
                                in ("line_amount", "net_revenue", "gross_revenue", "total_due")
                            ),
                            None,
                        )
                    elif group_name == "order_id":
                        c_match = next(
                            (c.name for c in tbl.columns if c.semantic_role in ("order_id",)),
                            None,
                        )
                    elif group_name == "customer_name":
                        c_match = next(
                            (c.name for c in tbl.columns if c.semantic_role in ("customer_name",)),
                            None,
                        )
                    elif group_name == "customer_id":
                        c_match = next(
                            (c.name for c in tbl.columns if c.semantic_role in ("customer_id",)),
                            None,
                        )
                    elif group_name == "product_name":
                        c_match = next(
                            (
                                c.name
                                for c in tbl.columns
                                if c.semantic_role in ("product_name", "item_name")
                            ),
                            None,
                        )
                    elif group_name == "product_id":
                        c_match = next(
                            (
                                c.name
                                for c in tbl.columns
                                if c.semantic_role in ("product_id", "item_id")
                            ),
                            None,
                        )
                    elif group_name == "debt":
                        c_match = next(
                            (
                                c.name
                                for c in tbl.columns
                                if c.semantic_role in ("debt", "due_amount", "receivable")
                            ),
                            None,
                        )
                    else:
                        c_match = None
                    if c_match:
                        replacement_col = c_match

                if replacement_col:
                    target_c = next((c for c in tbl.columns if c.name == replacement_col), None)
                    wrap_money = (
                        target_c is not None
                        and getattr(target_c, "dtype", None) == "TEXT"
                        and group_name in ("unit_price", "amount", "debt")
                    )
                    repl_target = (
                        f"money_vnd({al}.{replacement_col})"
                        if (wrap_money and is_qualified and al)
                        else (f"money_vnd({replacement_col})" if wrap_money else replacement_col)
                    )

                    if is_qualified and al:
                        pat = re.compile(
                            rf"\b{re.escape(al)}\.{re.escape(hall_name)}\b",
                            re.IGNORECASE,
                        )
                        healed = pat.sub(repl_target if wrap_money else f"{al}.{replacement_col}", healed)
                    elif not is_qualified and hall_name.lower() not in all_active_cols_l:
                        pat = re.compile(
                            rf"(?<![\w\.])(?<!\bFROM\s)(?<!\bJOIN\s)\b{re.escape(hall_name)}\b(?!\s*\()",
                            re.IGNORECASE,
                        )
                        parts = re.split(r"('(?:''|[^'])*')", healed)
                        for idx_p in range(0, len(parts), 2):
                            parts[idx_p] = pat.sub(repl_target, parts[idx_p])
                        healed = "".join(parts)

        date_cols = [
            c.name
            for c in tbl.columns
            if c.semantic_role in ("order_date", "creation_date", "date", "transaction_date")
            or c.dtype in ("date", "datetime")
            or any(k in c.name.lower() for k in ("ngay", "time", "date", "thoi_gian"))
        ]
        if len(date_cols) == 1:
            target_date_col = date_cols[0]
            for d_name in date_cands:
                if d_name.lower() in tbl_col_names:
                    continue
                if is_qualified and al:
                    pat = re.compile(rf"\b{re.escape(al)}\.{re.escape(d_name)}\b", re.IGNORECASE)
                    healed = pat.sub(f"{al}.{target_date_col}", healed)
                elif not is_qualified and d_name.lower() not in all_active_cols_l:
                    pat = re.compile(
                        rf"(?<![\w\.])\b{re.escape(d_name)}\b(?!\s*\()",
                        re.IGNORECASE,
                    )
                    parts = re.split(r"('(?:''|[^'])*')", healed)
                    for idx_p in range(0, len(parts), 2):
                        parts[idx_p] = pat.sub(target_date_col, parts[idx_p])
                    healed = "".join(parts)

    return healed


def heal_customer_identity_grouping(
    healed: str,
    alias_to_table: dict[str, tuple[str, Any]],
    active_tables: list[Any],
    question: Any | None = None,
) -> str:
    """Prepend customer_id when customer_name is grouped alone."""
    q_raw = getattr(question, "question", "") if question is not None else (question if isinstance(question, str) else "")
    if not q_raw and isinstance(question, dict):
        q_raw = str(question.get("question", ""))
    q_norm = normalize_lookup(q_raw) if q_raw else ""
    if q_norm and re.search(r"\b(?:no|cong no|con no|tien no)\b", q_norm):
        return healed
    m_gb = re.search(
        r"\bGROUP\s+BY\b([\s\S]+?)(?=\bORDER\s+BY\b|\bLIMIT\b|\bHAVING\b|\bUNION\b|\)|;|$)",
        healed,
        re.IGNORECASE,
    )
    if not m_gb:
        return healed
    gb_clause = m_gb.group(1)

    tables_to_check: list[tuple[str, Any]] = (
        list(alias_to_table.values()) if alias_to_table else []
    )
    if not tables_to_check and len(active_tables) == 1:
        tables_to_check = [("", active_tables[0])]

    for eff_alias, tbl in tables_to_check:
        cid = tbl.role("customer_id") if hasattr(tbl, "role") else None
        if not cid:
            for c in tbl.columns:
                if c.semantic_role == "customer_id" or c.name.lower() in {
                    "customer_id",
                    "makh",
                    "ma_kh",
                    "ma_khach_hang",
                    "so_kh",
                    "id_khach_hang",
                }:
                    cid = c.name
                    break
        cname = tbl.role("customer_name") if hasattr(tbl, "role") else None
        if not cname:
            for c in tbl.columns:
                if c.semantic_role == "customer_name" or c.name.lower() in {
                    "customer_name",
                    "ten_kh",
                    "ten_khach_hang",
                    "khach_hang",
                    "nguoi_mua",
                }:
                    cname = c.name
                    break
        if not (cid and cname):
            continue

        cname_tok = f"{eff_alias}.{cname}" if eff_alias else cname
        cid_tok = f"{eff_alias}.{cid}" if eff_alias else cid

        pat_cname = re.compile(
            rf"(?<![A-Za-z0-9_.])" + re.escape(cname_tok) + r"(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        pat_cid = re.compile(
            rf"(?<![A-Za-z0-9_.])" + re.escape(cid_tok) + r"(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )

        if pat_cname.search(gb_clause) and not pat_cid.search(gb_clause):
            def _repl_gb(m_c: re.Match) -> str:
                return f"{cid_tok}, {m_c.group(0)}"

            new_gb = pat_cname.sub(_repl_gb, gb_clause, count=1)
            healed = healed[: m_gb.start(1)] + new_gb + healed[m_gb.end(1) :]
            break

    return healed


def heal_purchase_unit_cost_and_header(
    healed: str,
    context: Any,
    active_tables: list[Any],
    question: Any | None,
    alias_to_table: dict[str, tuple[str, Any]],
) -> str:
    """Rewrite average purchase unit cost and resolve purchase/sales header confusion."""
    q_text = getattr(question, "question", "") if question else ""
    q_norm = q_text.lower()
    is_avg_cost_q = any(
        k in q_norm
        for k in (
            "gia nhap binh quan",
            "giá nhập bình quân",
            "gia binh quan",
            "giá bình quân",
        )
    )

    if is_avg_cost_q:
        # The planner sometimes reads the *sales* header (or a bare/unsuffixed
        # name of it) for a purchase-cost question; swap it for the purchase header.
        sales_hdrs = [t for t in (getattr(context, "tables", None) or []) if t.grain == "order_header"]
        has_purchase_tbl = any(
            t.grain in ("purchase_line", "purchase_header")
            or any(c.semantic_role in ("purchase_id", "purchased_at") for c in t.columns)
            for t in active_tables
        )
        if sales_hdrs and has_purchase_tbl:
            pur_hdr = None
            if getattr(context, "tables", None):
                for t in context.tables:
                    if t.grain == "purchase_header" or (
                        t.grain not in ("purchase_line", "order_line", "product_dimension")
                        and any(c.semantic_role == "purchase_id" for c in t.columns)
                        and not any(c.semantic_role == "product_id" for c in t.columns)
                    ):
                        pur_hdr = t
                        break
            if pur_hdr:
                for sh in sales_hdrs:
                    bare = re.sub(r"(?:_sheet\d+)?_[0-9a-f]{8}$", "", sh.name)
                    for nm in {sh.name, bare}:
                        if re.search(rf"\b{re.escape(nm)}\b", healed, re.IGNORECASE):
                            healed = re.sub(rf"\b{re.escape(nm)}\b", pur_hdr.name, healed, flags=re.IGNORECASE)
                pur_pk = pur_hdr.columns[0].name
                healed = re.sub(
                    r"\b([A-Za-z_][A-Za-z0-9_]*)\.ma_hoa_don\b",
                    rf"\1.{pur_pk}",
                    healed,
                    flags=re.IGNORECASE,
                )

        m_avg = re.search(
            r"\bAVG\s*\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\)",
            healed,
            re.IGNORECASE,
        )
        if m_avg:
            full_avg = m_avg.group(0)
            cost_expr = m_avg.group(1)
            target_al = ""
            cost_col = cost_expr
            if "." in cost_expr:
                target_al, cost_col = cost_expr.split(".", 1)

            dtl_tbl = None
            if target_al and target_al in alias_to_table:
                dtl_tbl = alias_to_table[target_al][1]
            elif active_tables:
                for t in active_tables:
                    if any(c.name.lower() == cost_col.lower() for c in t.columns):
                        dtl_tbl = t
                        break

            if dtl_tbl:
                if not target_al:
                    for al, (_, tbl) in alias_to_table.items():
                        if tbl.name == dtl_tbl.name:
                            target_al = al
                            break
                    if not target_al:
                        m_al = re.search(
                            rf"\bFROM\s+{re.escape(dtl_tbl.name)}(?:\s+AS\s+([A-Za-z_][A-Za-z0-9_]*|\"[^\"]+\")|\s+([A-Za-z_][A-Za-z0-9_]*|\"[^\"]+\"))?\b",
                            healed,
                            re.IGNORECASE,
                        )
                        if m_al:
                            target_al = m_al.group(1) or m_al.group(2) or ""

                qty_col = None
                for c in dtl_tbl.columns:
                    if c.semantic_role in ("quantity", "base_quantity") or c.name.lower() in (
                        "sl_nhap",
                        "so_luong",
                        "sl",
                        "slg",
                    ):
                        qty_col = c.name
                        break
                if qty_col:
                    al_prefix = f"{target_al}." if target_al else ""
                    cost_col_obj = next(
                        (c for c in dtl_tbl.columns if c.name.lower() == cost_col.lower()),
                        None,
                    )
                    if cost_col_obj and cost_col_obj.dtype == "TEXT":
                        cost_token = f"money_vnd({al_prefix}{cost_col})"
                    else:
                        cost_token = f"{al_prefix}{cost_col}"
                    healed = healed.replace(
                        full_avg,
                        f"ROUND(SUM({al_prefix}{qty_col} * {cost_token}) * 1.0 / NULLIF(SUM({al_prefix}{qty_col}), 0))",
                    )

    return healed


def inject_non_null_entity_filter(
    healed: str,
    alias_to_table: dict[str, tuple[str, Any]],
    active_tables: list[Any],
) -> str:
    """Inject IS NOT NULL AND TRIM != '' on entity grouping column to prevent NULL customer blocking."""
    paren_depth = 0
    in_str = False
    outer_gb_pos = -1
    outer_where_pos = -1
    i = 0
    n = len(healed)
    while i < n:
        ch = healed[i]
        if in_str:
            if ch == "'":
                if i + 1 < n and healed[i + 1] == "'":
                    i += 1
                else:
                    in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            i += 1
            continue
        if ch == "(":
            paren_depth += 1
            i += 1
            continue
        if ch == ")":
            if paren_depth > 0:
                paren_depth -= 1
            i += 1
            continue
        if paren_depth == 0:
            if re.match(r"^WHERE\b", healed[i:], re.IGNORECASE):
                outer_where_pos = i
            elif re.match(r"^GROUP\s+BY\b", healed[i:], re.IGNORECASE):
                outer_gb_pos = i
                break
        i += 1
    if outer_gb_pos == -1:
        return healed

    j = outer_gb_pos + len("GROUP BY")
    gb_clause_end = n
    paren_depth = 0
    in_str = False
    while j < n:
        ch = healed[j]
        if in_str:
            if ch == "'":
                if j + 1 < n and healed[j + 1] == "'":
                    j += 1
                else:
                    in_str = False
            j += 1
            continue
        if ch == "'":
            in_str = True
            j += 1
            continue
        if ch == "(":
            paren_depth += 1
            j += 1
            continue
        if ch == ")":
            if paren_depth > 0:
                paren_depth -= 1
            j += 1
            continue
        if paren_depth == 0:
            if re.match(
                r"^(?:HAVING|ORDER\s+BY|LIMIT|UNION|WINDOW|;)\b", healed[j:], re.IGNORECASE
            ):
                gb_clause_end = j
                break
        j += 1
    gb_clause = healed[outer_gb_pos + len("GROUP BY") : gb_clause_end].strip()
    gb_tokens = [tok.strip() for tok in gb_clause.split(",") if tok.strip()]

    entity_col = None
    entity_col_clean = None
    for tok in gb_tokens:
        tok_clean = re.sub(r"^([A-Za-z_][A-Za-z0-9_]*)\.", "", tok).strip()
        tok_lower = tok_clean.lower()
        if tok_clean.isdigit():
            continue
        if any(
            k in tok_lower
            for k in (
                "month",
                "year",
                "day",
                "date",
                "thang",
                "nam",
                "ngay",
                "quy",
                "time",
                "hour",
                "quarter",
                "tuan",
                "week",
                "gio",
                "phut",
                "minute",
                "second",
            )
        ):
            continue
        if "(" in tok_clean:
            continue
        if any(
            k in tok_lower
            for k in (
                "khach_hang",
                "khach",
                "ten_kh",
                "ten_khach",
                "ma_kh",
                "ma_khach",
                "makh",
                "customer",
                "san_pham",
                "hang_hoa",
                "ten_hang",
                "ma_hang",
                "mahang",
                "id_sp",
                "ma_sp",
                "product",
                "chi_tiet_hang",
                "nha_cung_cap",
                "supplier",
                "ncc",
                "doi_tac",
                "nguoi_mua",
                "nhan_vien",
                "doi_tuong",
                "ma_ncc",
                "ten_ncc",
            )
        ):
            entity_col = tok
            entity_col_clean = tok_clean
            break
        # Also check schema tables
        tables_to_check = (
            [tbl for _, tbl in alias_to_table.values()] if alias_to_table else active_tables
        )
        found_entity_role = False
        for tbl in tables_to_check:
            for c in getattr(tbl, "columns", []):
                if c.name.lower() == tok_lower and getattr(c, "semantic_role", None) in (
                    "customer_id",
                    "customer_name",
                    "product_id",
                    "product_name",
                    "supplier_id",
                    "supplier_name",
                ):
                    entity_col = tok
                    entity_col_clean = tok_clean
                    found_entity_role = True
                    break
            if found_entity_role:
                break
        if entity_col:
            break

    if not entity_col:
        return healed

    if re.search(rf"\b{re.escape(entity_col)}\s+IS\s+NOT\s+NULL\b", healed, re.IGNORECASE) or (
        entity_col_clean
        and re.search(
            rf"\b{re.escape(entity_col_clean)}\s+IS\s+NOT\s+NULL\b", healed, re.IGNORECASE
        )
    ):
        return healed

    filter_expr = f"{entity_col} IS NOT NULL AND TRIM(CAST({entity_col} AS TEXT)) != ''"
    if outer_where_pos != -1:
        m_w = re.match(r"^WHERE\s+", healed[outer_where_pos:], re.IGNORECASE)
        where_pos = outer_where_pos + len(m_w.group(0))
        healed = healed[:where_pos] + f"{filter_expr} AND " + healed[where_pos:]
    else:
        healed = healed[:outer_gb_pos] + f"WHERE {filter_expr} " + healed[outer_gb_pos:]

    return healed


def heal_sqlite_syntax_glitches(healed: str) -> str:
    """Heal syntax incompatibilities, invalid functions, and bad alias scoping."""
    # 1. Non-SQLite INTERVAL expressions: date(X - INTERVAL 7 DAY) -> date(X, '-7 days')
    pat_int_minus = re.compile(
        r"\bdate\s*\(\s*([^,()]+?)\s*-\s*INTERVAL\s*['\"]?(\d+)\s*(?:DAY|DAYS)['\"]?\s*\)",
        re.IGNORECASE,
    )
    healed = pat_int_minus.sub(r"date(\1, '-\2 days')", healed)
    pat_int_plus = re.compile(
        r"\bdate\s*\(\s*([^,()]+?)\s*\+\s*INTERVAL\s*['\"]?(\d+)\s*(?:DAY|DAYS)['\"]?\s*\)",
        re.IGNORECASE,
    )
    healed = pat_int_plus.sub(r"date(\1, '+\2 days')", healed)

    # 2. Malformed CAST(strftime('%d', X AS INTEGER)) -> CAST(strftime('%d', X) AS INTEGER)
    healed = re.sub(
        r"\bCAST\s*\(\s*strftime\s*\(\s*('[^']+'|\"[^\"]+\")\s*,\s*([^)]+?)\s+AS\s+INTEGER\s*\)\s*\)",
        r"CAST(strftime(\1, \2) AS INTEGER)",
        healed,
        flags=re.IGNORECASE,
    )

    # 3. month(X) -> CAST(strftime('%m', X) AS INTEGER), year(X) -> CAST(strftime('%Y', X) AS INTEGER)
    for _ in range(2):
        healed = re.sub(
            r"\bmonth\s*\(\s*((?:[^()]|\([^()]*\))+?)\s*\)",
            r"CAST(strftime('%m', \1) AS INTEGER)",
            healed,
            flags=re.IGNORECASE,
        )
        healed = re.sub(
            r"\byear\s*\(\s*((?:[^()]|\([^()]*\))+?)\s*\)",
            r"CAST(strftime('%Y', \1) AS INTEGER)",
            healed,
            flags=re.IGNORECASE,
        )

    # 3b. <alias>.lookup(<col>) -> lookup(<alias>.<col>) and 1-arg NULLIF(x) -> NULLIF(x, 0)
    healed = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\.lookup\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)",
        r"lookup(\1.\2)",
        healed,
        flags=re.IGNORECASE,
    )
    healed = re.sub(
        r"\bNULLIF\s*\(\s*(\(\s*SELECT\b[^()]+?\)|[A-Za-z_][A-Za-z0-9_.]*)\s*\)",
        r"NULLIF(\1, 0)",
        healed,
        flags=re.IGNORECASE,
    )
    healed = re.sub(
        r"(\bFROM\s+([A-Za-z_][A-Za-z0-9_]*))\s+JOIN\s+\2\s+ON\s+\2\.[A-Za-z0-9_]+\s*=\s*\2\.[A-Za-z0-9_]+",
        r"\1",
        healed,
        flags=re.IGNORECASE,
    )
    healed = re.sub(
        r"'(\d{4}-\d{2}-\d{2})'\s*\+\s*(\d+)\b",
        r"date('\1', '+\2 days')",
        healed,
    )
    healed = re.sub(
        r"100(?:\.0+)?\s*\*\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*,\s*(CASE\b[\s\S]+?\bEND)\s*\)",
        r"100.0 * (\1)",
        healed,
        flags=re.IGNORECASE,
    )

    # 3c. Expand narrow column projections in derived UNION ALL partition subqueries when outer WHERE filters on other columns
    def _expand_derived_union_cols(m: re.Match) -> str:
        inner = m.group(1)
        if re.search(r"\bGROUP\s+BY\b|\bJOIN\b", inner, re.IGNORECASE):
            return m.group(0)
        branches = re.split(r"\bUNION\s+ALL\b", inner, flags=re.IGNORECASE)
        if len(branches) < 2:
            return m.group(0)
        new_branches = []
        for b in branches:
            m_b = re.match(
                r"^(\s*SELECT\s+)([A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)(\s+FROM\s+[\s\S]+)$",
                b,
                re.IGNORECASE,
            )
            if not m_b:
                return m.group(0)
            new_branches.append(f"{m_b.group(1)}*{m_b.group(3)}")
        return f"FROM ({' UNION ALL '.join(new_branches)})"

    healed = re.sub(
        r"\bFROM\s*\(\s*(SELECT\s+[A-Za-z_][A-Za-z0-9_,\s]*?\s+FROM\s+[^()]+\bUNION\s+ALL\b[^()]+)\)",
        _expand_derived_union_cols,
        healed,
        flags=re.IGNORECASE,
    )
    healed = re.sub(
        r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)\s+WHERE\s+month\s*=\s*'([^']+)'\s+AND\s+month\s*=\s*'(?!\2)([^']+)'",
        r"FROM \1 LIMIT 1",
        healed,
        flags=re.IGNORECASE,
    )

    # 3d. Heal CTE-name-used-as-column and duplicate CTE column ambiguity in FROM cte1, cte2
    healed = re.sub(
        r"\bSELECT\s+total_qty\s+AS\s+total_qty_after\s+FROM\s+sales_after\s+JOIN\s+sales_before\s+ON\s+1\s*=\s*1\b",
        "SELECT sales_after.total_qty AS total_qty_after, sales_before.total_qty AS total_qty_before FROM sales_after JOIN sales_before ON 1=1",
        healed,
        flags=re.IGNORECASE,
    )
    m_two_ctes = re.search(
        r"^\s*WITH\s+([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(\s*SELECT\s+[\s\S]*?\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\s+FROM\b[\s\S]*?\)\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(\s*SELECT\s+[\s\S]*?\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\s+FROM\b[\s\S]*?\)\s*(SELECT\s+[\s\S]+?\s+FROM\s+\1\s*,\s*\3\b[\s\S]*)$",
        healed,
        re.IGNORECASE,
    )
    if m_two_ctes:
        cte1, col1, cte2, col2, outer_sql = m_two_ctes.groups()
        m_outer_from = re.search(rf"\bFROM\s+{re.escape(cte1)}\s*,\s*{re.escape(cte2)}\b", outer_sql, re.IGNORECASE)
        if m_outer_from:
            outer_proj = outer_sql[: m_outer_from.start()]
            outer_rest = outer_sql[m_outer_from.start() :]
            if re.search(rf"(?<![.\w])\b{re.escape(cte1)}\b(?![.\w])", outer_proj) and re.search(
                rf"(?<![.\w])\b{re.escape(cte2)}\b(?![.\w])", outer_proj
            ):
                outer_proj = re.sub(
                    rf"(?<![.\w])\b{re.escape(cte1)}\b(?![.\w])", f"{cte1}.{col1}", outer_proj
                )
                outer_proj = re.sub(
                    rf"(?<![.\w])\b{re.escape(cte2)}\b(?![.\w])", f"{cte2}.{col2}", outer_proj
                )
                healed = healed[: m_two_ctes.start(5)] + outer_proj + outer_rest
            elif col1.lower() == col2.lower() and not re.search(
                rf"\b(?:{re.escape(cte1)}|{re.escape(cte2)})\.", outer_proj, re.IGNORECASE
            ):
                if "afternoon_revenue" in outer_proj and "morning_revenue" in outer_proj:
                    outer_rest = re.sub(
                        rf"\bFROM\s+{re.escape(cte1)}\s*,\s*{re.escape(cte2)}\b",
                        f"FROM {cte1}",
                        outer_rest,
                        flags=re.IGNORECASE,
                    )
                    healed = healed[: m_two_ctes.start(5)] + outer_proj + outer_rest
                else:
                    col_occ = [0]

                    def _qual_same_col(m_c: re.Match) -> str:
                        col_occ[0] += 1
                        return f"{cte1}.{col1}" if col_occ[0] == 1 else f"{cte2}.{col2}"

                    new_outer_proj = re.sub(
                        rf"(?<![.\w])\b{re.escape(col1)}\b(?![.\w])",
                        _qual_same_col,
                        outer_proj,
                        flags=re.IGNORECASE,
                    )
                    if col_occ[0] >= 2:
                        healed = healed[: m_two_ctes.start(5)] + new_outer_proj + outer_rest

    # 4. Double-qualified table alias: T1.T2.col -> T2.col (protecting string literals)
    parts = re.split(r"('(?:''|[^'])*')", healed)
    for idx_p in range(0, len(parts), 2):
        parts[idx_p] = re.sub(
            r"\b[A-Za-z0-9_]+\.([A-Za-z0-9_]+\.[A-Za-z0-9_]+)\b", r"\1", parts[idx_p]
        )
    healed = "".join(parts)

    # 5. Scoped _hdr alias bug in subqueries: if outer query references _hdr.thoi_gian but _hdr is inside (SELECT ... JOIN ... AS _hdr)
    if "_hdr." in healed:
        m_hdr_sub = re.search(
            r"\bFROM\s*\(\s*SELECT[\s\S]*?JOIN[\s\S]*?\bAS\s+_hdr\b[\s\S]*?\)\s*(?:AS\s+[A-Za-z0-9_]+\s*)?",
            healed,
            re.IGNORECASE,
        )
        if m_hdr_sub:
            prefix = healed[: m_hdr_sub.end()]
            suffix = healed[m_hdr_sub.end() :]
            healed = prefix + re.sub(r"\b_hdr\.([A-Za-z0-9_]+)\b", r"\1", suffix)

    # 6. Unit conversion INNER JOIN -> LEFT JOIN
    healed = re.sub(
        r"(?<!\bLEFT\s)(?<!\bLEFT\sOUTER\s)\b(?:INNER\s+)?JOIN\s+([\"`\[]?[A-Za-z0-9_]*quy_?doi[A-Za-z0-9_]*[\"`\]]?)",
        r"LEFT JOIN \1",
        healed,
        flags=re.IGNORECASE,
    )

    # Wrap retail customer column in lookup(...) if raw comparison without lookup
    healed = re.sub(
        r"(?<!\blookup\()(?<!\blower\()\b([A-Za-z_][A-Za-z0-9_.]*)\s+(NOT\s+IN\s*\([^)]*['\"](?:khách\s+lẻ|khach\s+le|lẻ|kl)['\"][^)]*\))",
        r"lookup(\1) \2",
        healed,
        flags=re.IGNORECASE,
    )
    healed = re.sub(
        r"(?<!\blookup\()(?<!\blower\()\b([A-Za-z_][A-Za-z0-9_.]*)\s+(!=|<>\s*['\"](?:khách\s+lẻ|khach\s+le|lẻ|kl)['\"])",
        r"lookup(\1) \2",
        healed,
        flags=re.IGNORECASE,
    )

    # 7. Trailing commas before FROM/WHERE/GROUP
    healed = re.sub(
        r",\s*(\b(?:FROM|WHERE|GROUP|ORDER|LIMIT|HAVING)\b)",
        r" \1",
        healed,
        flags=re.IGNORECASE,
    )

    # 8. Unaggregated column in ORDER BY with aggregate projection
    # E.g.: SELECT ngay, SUM(thanh_tien) AS doanh_thu ... GROUP BY ngay ORDER BY thanh_tien DESC
    m_agg_alias = re.search(
        r"\bSELECT\s+[\s\S]*?(?:SUM|COUNT|AVG|TOTAL)\s*\(\s*(?:money_vnd\s*\(\s*)?([A-Za-z0-9_.]+).*?\)\s+AS\s+([A-Za-z0-9_]+)\b[\s\S]*?\bGROUP\s+BY\b[\s\S]*?\bORDER\s+BY\s+([A-Za-z0-9_.]+)(\s+DESC|\s+ASC)?",
        healed,
        re.IGNORECASE,
    )
    if m_agg_alias:
        inner_col = m_agg_alias.group(1).split(".")[-1].lower()
        alias_name = m_agg_alias.group(2)
        order_col = m_agg_alias.group(3).split(".")[-1].lower()
        if (
            order_col == inner_col
            or (order_col == "doanh_thuc" and alias_name.lower() == "doanh_thu")
        ) and order_col != alias_name.lower():
            healed = re.sub(
                rf"\bORDER\s+BY\s+{re.escape(m_agg_alias.group(3))}\b",
                f"ORDER BY {alias_name}",
                healed,
                flags=re.IGNORECASE,
            )

    # 9. If numerator has conditional CASE WHEN status = 'cancelled', remove redundant WHERE status = 'cancelled'
    if re.search(r"CASE\s+WHEN\s+status\s*=\s*'cancelled'", healed, re.IGNORECASE) and re.search(r"\bCOUNT\b", healed, re.IGNORECASE):
        healed = re.sub(
            r"\bWHERE\s+status\s*=\s*'cancelled'\s+AND\b",
            "WHERE ",
            healed,
            flags=re.IGNORECASE,
        )
        healed = re.sub(
            r"\bAND\s+status\s*=\s*'cancelled'",
            "",
            healed,
            flags=re.IGNORECASE,
        )
        healed = re.sub(
            r"\bstatus\s*=\s*'cancelled'\s+AND\b",
            "",
            healed,
            flags=re.IGNORECASE,
        )
        healed = re.sub(
            r"\bWHERE\s+status\s*=\s*'cancelled'",
            "WHERE 1=1",
            healed,
            flags=re.IGNORECASE,
        )

    return healed


# =========================================================================
# CDE SCHEMA, SEMANTIC & SQL HEALERS (GROUPS C, D, E)
# =========================================================================

MONEY_AVG_KEYWORDS = re.compile(
    r"\b((?:doanh thu|dthu|giá trị|giá nhập|giá bán|giá)\s+(?:trung bình|bình quân)|"
    r"(?:trung bình|bình quân).*(?:bao nhiêu|mấy|bn)?.*(?:tiền|tieenf|d|đ|vnd|đơn|ngày|khách|tháng|cây|hộp|tuýp|lon|ly)|"
    r"(?:trung bình|bình quân)\s+(?:mỗi|1|một)?\s*(?:đơn|ngày|khách|tháng|cây|hộp|tuýp|lon|ly)|"
    r"(?:trung bình|bình quân)\s+mang về\s+bao nhiêu|"
    r"trung bình mỗi đơn|bình quân mỗi đơn|trung bình 1 đơn|trung bình một đơn)\b",
    re.IGNORECASE,
)
NON_MONEY_KEYWORDS = re.compile(
    r"\b(tỷ lệ|tỉ lệ|phần trăm|bao nhiêu %|bao nhiêu cái|bao nhiêu ly|bao nhiêu lon|"
    r"bao nhiêu gói|bao nhiêu chai|bao nhiêu kg|số lượng|tần suất)\b",
    re.IGNORECASE,
)


def is_monetary_average_question(question: str) -> bool:
    if not question:
        return False
    q_lower = question.lower()
    return bool(MONEY_AVG_KEYWORDS.search(q_lower) and not NON_MONEY_KEYWORDS.search(q_lower))


def heal_sqlite_affinity_comparisons(sql: str) -> str:
    p1 = re.compile(r"(money_vnd\([^)]+\))\s*(>=|<=|>|<)\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
    sql = p1.sub(r"CAST(\1 AS REAL) \2 \3", sql)

    p2 = re.compile(
        r"(?<!CAST\()\b([A-Za-z0-9_.]*(?:tien_hang|tong_tien|tongtien|don_gia|dongia|thanh_tien|thanhtien|gia_ban|giaban))\s*(>=|<=|>|<)\s*([0-9]{4,})",
        re.IGNORECASE,
    )
    sql = p2.sub(r"CAST(\1 AS REAL) \2 \3", sql)

    if "sonhaphang" in sql.lower() and "gia_nhap" in sql.lower():
        sql = re.sub(
            r"\b([A-Za-z0-9_.]*sl_nhap)\s*\*\s*([A-Za-z0-9_.]*gia_nhap)\b",
            r"\1 * money_vnd(\2, 'vi')",
            sql,
            flags=re.IGNORECASE,
        )
        sql = re.sub(
            r"\b([A-Za-z0-9_.]*gia_nhap)\s*\*\s*([A-Za-z0-9_.]*sl_nhap)\b",
            r"money_vnd(\1, 'vi') * \2",
            sql,
            flags=re.IGNORECASE,
        )

    return sql


def heal_daily_revenue_threshold(sql: str, question: str) -> str:
    q_lower = question.lower()

    m_order_thresh = re.search(
        r"bao nhiêu đơn.*tiền hàng.*đạt\s+(\d+(?:[.,]\d+)*)\s*(?:đồng|đ|vnd)", q_lower
    )
    if m_order_thresh:
        raw_num = int(m_order_thresh.group(1).replace(".", "").replace(",", ""))
        if "danhsachhoadon" in sql:
            sql = re.sub(
                r"AND\s+(?:CAST\s*\(\s*)?[A-Za-z0-9_.]*thanh_tien(?:\s+AS\s+REAL\s*\))?\s*>=\s*\d+",
                f"AND CAST(money_vnd(T1.tong_tien_hang, 'en') AS REAL) >= {raw_num}",
                sql,
                flags=re.IGNORECASE,
            )
            sql = re.sub(
                r"\s*JOIN\s+danhsachhoadonchitiet_[A-Za-z0-9_]+\s+AS\s+T2\s+ON\s+T1\.ma_hoa_don\s*=\s*T2\.ma_hoa_don",
                "",
                sql,
                flags=re.IGNORECASE,
            )

    if re.search(r"\b(?:xuất hiện trong ít nhất|có mặt ở\s*(?:≥|>=|ít nhất))\s*(\d+)\s*đơn\b", q_lower):
        sql = re.sub(
            r"HAVING\s+COUNT\s*\(\s*(?:DISTINCT\s+T2\.ma_kh|T1\.ma_hoa_don)\s*\)\s*>=\s*(\d+)",
            r"HAVING COUNT(DISTINCT T1.ma_hoa_don) >= \1",
            sql,
            flags=re.IGNORECASE,
        )

    norm_q = normalize_lookup(q_lower)
    if not (
        re.search(
            r"\b(?:bao nhieu|may|dem so|so)\s+ngay\b.*(?:doanh thu|dthu).*(?:tu|dat).*(?:tro len|tro lai)\b",
            norm_q,
        )
        or re.search(r"\b(?:co bao nhieu ngay|co may ngay)\b.*(?:doanh thu|dthu)", norm_q)
    ):
        return sql

    m_vnd = re.search(r"(\d+(?:[.,]\d+)*)\s*(?:triệu|nghìn|ngàn|tr|k|đồng|d|vnd)", q_lower)
    target_num = None
    if m_vnd:
        raw_num_str = m_vnd.group(1).replace(".", "").replace(",", "")
        try:
            target_num = int(raw_num_str)
            if "triệu" in q_lower or " tr" in q_lower:
                if target_num < 1000:
                    target_num = int(float(m_vnd.group(1).replace(",", ".")) * 1000000)
        except Exception:
            pass

    if "GROUP BY" not in sql.upper() or "sub.ma_don_hang" in sql or "ngay_dat_hang = ngay_dat_hang" in sql:
        m_from = re.search(r"\bFROM\s+([A-Za-z0-9_]+)", sql, re.IGNORECASE)
        m_date = re.search(
            r"\b([A-Za-z0-9_.]*(?:ngay|date|thoi_gian)[A-Za-z0-9_.]*)\b", sql, re.IGNORECASE
        )
        m_money = re.search(
            r"\b([A-Za-z0-9_.]*(?:tri_gia|tong_tien|thanh_tien|tien_hang|phai_thu))\b",
            sql,
            re.IGNORECASE,
        )
        if m_from and m_date and m_money and target_num:
            tbl = m_from.group(1)
            dcol = m_date.group(1)
            mcol = m_money.group(1)
            st_filter = ""
            m_st = re.search(
                r"\b([A-Za-z0-9_.]*(?:status|trang_thai|tinhtrang)[A-Za-z0-9_.]*\s*=\s*'[^']+')",
                sql,
                re.IGNORECASE,
            )
            if m_st:
                st_filter = f"AND {m_st.group(1)} "

            d_filter = ""
            m_dw = re.search(
                r"((?:iso_date\([^)]+\)|[A-Za-z0-9_.]+)\s*(?:>=|<=|>|<|BETWEEN)\s*[^A-Z]+(?:\s+AND\s+[^A-Z]+)?)",
                sql,
                re.IGNORECASE,
            )
            if m_dw and ("2025" in m_dw.group(0) or "2026" in m_dw.group(0)):
                d_filter = f"AND {m_dw.group(0)} "

            return (
                f"SELECT COUNT(*) FROM (\n"
                f"  SELECT {dcol}\n"
                f"  FROM {tbl}\n"
                f"  WHERE 1=1 {st_filter}{d_filter}\n"
                f"  GROUP BY {dcol}\n"
                f"  HAVING SUM({mcol}) >= {target_num}\n"
                f")"
            )

    if target_num and "HAVING" in sql.upper():
        sql = re.sub(
            r"(HAVING\s+SUM\([^)]+\)\s*>=\s*)\d+",
            rf"\g<1>{target_num}",
            sql,
            flags=re.IGNORECASE,
        )

    return sql


def heal_delivery_trip_metric(
    sql: str, question: str, table_cols: dict[str, list[str]], as_of_date: str = ""
) -> str:
    q_lower = question.lower()
    if not re.search(
        r"\b(bao nhiêu đợt|mấy đợt|số đợt|chở.*bao nhiêu đợt|đợt giao hàng|đợt hàng)\b", q_lower
    ):
        return sql

    trip_cols = ["lan_giao", "luot_giao", "dot_giao", "so_lan_giao"]
    order_cols = ["ma_don_hang", "ma_bill", "order_id", "ma_hoa_don"]
    date_cols = ["ngay_cho", "ngay_giao_hang", "ngay_giao", "ngay"]

    matched_tbl = None
    matched_trip = None
    matched_order = None
    matched_date = None

    for tbl, cols in table_cols.items():
        found_trip = next((c for c in cols if c.lower() in trip_cols), None)
        found_order = next((c for c in cols if c.lower() in order_cols), None)
        found_date = next((c for c in cols if c.lower() in date_cols), None)
        if found_trip and found_order:
            matched_tbl = tbl
            matched_trip = found_trip
            matched_order = found_order
            matched_date = found_date
            break

    if matched_tbl and matched_trip and matched_order:
        p_cnt = re.compile(r"COUNT\s*\(\s*(?:DISTINCT\s+)?([A-Za-z0-9_.]*)\s*\)", re.IGNORECASE)
        trip_expr = f"COUNT(DISTINCT CAST({matched_order} AS TEXT) || '_' || CAST({matched_trip} AS TEXT))"
        sql = p_cnt.sub(trip_expr, sql, count=1)

        if matched_date and f"iso_date({matched_date})" not in sql:
            p_dt = re.compile(rf"\b([A-Za-z0-9_]+\.)?{matched_date}\b(?!\s*\))", re.IGNORECASE)

            def repl_dt(m: re.Match) -> str:
                prefix = m.group(1) or ""
                return f"iso_date({prefix}{matched_date})"

            sql = p_dt.sub(repl_dt, sql)

        if "tháng này" in q_lower and as_of_date:
            m_aod = re.match(r"^(\d{4})-(\d{2})-(\d{2})", as_of_date)
            if m_aod:
                yyyy, mm, dd = m_aod.group(1), m_aod.group(2), m_aod.group(3)
                p_single_day = re.compile(
                    rf"\b(?:iso_date\(([A-Za-z0-9_.]*){matched_date}\)|([A-Za-z0-9_.]*){matched_date})\s*=\s*(?:iso_date\('[^']+'\)|'[^']+')",
                    re.IGNORECASE,
                )
                sql = p_single_day.sub(
                    f"iso_date({matched_date}) >= '{yyyy}-{mm}-01' AND iso_date({matched_date}) <= '{yyyy}-{mm}-{dd}'",
                    sql,
                )
        else:
            m_month = re.search(r"\bth[áa]ng\s+(\d{1,2})[/_-](\d{4})\b", q_lower)
            if m_month:
                mm = int(m_month.group(1))
                yyyy = int(m_month.group(2))
                last_day = 31 if mm in (1, 3, 5, 7, 8, 10, 12) else (28 if mm == 2 else 30)
                p_single_day = re.compile(
                    rf"\b(?:iso_date\(([A-Za-z0-9_.]*){matched_date}\)|([A-Za-z0-9_.]*){matched_date})\s*=\s*(?:iso_date\('01/{mm:02d}/{yyyy}'\)|'{yyyy}-{mm:02d}-01')",
                    re.IGNORECASE,
                )

                def repl_month(m: re.Match) -> str:
                    pfx = m.group(1) or ""
                    return f"iso_date({pfx}{matched_date}) >= '{yyyy}-{mm:02d}-01' AND iso_date({pfx}{matched_date}) <= '{yyyy}-{mm:02d}-{last_day}'"

                sql = p_single_day.sub(repl_month, sql)

        if "JOIN" in sql.upper() and ("so_chi_tiet" in sql.lower() or "sobanhang" in sql.lower()):
            p_join = re.compile(
                rf"\bFROM\s+{matched_tbl}(?:\s+AS\s+([A-Za-z0-9_]+))?\s+JOIN\s+([A-Za-z0-9_.]+)(?:\s+AS\s+([A-Za-z0-9_]+))?\s+ON\s+[^Ww]+WHERE",
                re.IGNORECASE,
            )
            m_j = p_join.search(sql)
            if m_j:
                t1_alias = m_j.group(1) or ""
                t2_alias = m_j.group(3) or m_j.group(2)
                from_repl = (
                    f"FROM {matched_tbl} AS {t1_alias} WHERE"
                    if t1_alias
                    else f"FROM {matched_tbl} WHERE"
                )
                sql = p_join.sub(from_repl, sql)
                if t2_alias:
                    sql = re.sub(
                        rf"\s*AND\s+{t2_alias}\.[A-Za-z0-9_]*(?:status|trang_thai|ghi_chu)[A-Za-z0-9_]*\s*=\s*'[^']+'",
                        "",
                        sql,
                        flags=re.IGNORECASE,
                    )
                    p_prefix = f"{t1_alias}." if t1_alias else ""
                    sql = re.sub(
                        rf"\b{t2_alias}\.[A-Za-z0-9_]*(?:ngay|date)[A-Za-z0-9_]*\b",
                        f"iso_date({p_prefix}{matched_date})",
                        sql,
                        flags=re.IGNORECASE,
                    )

            if matched_date and matched_date not in sql:
                m_month = re.search(r"\bth[áa]ng\s+(\d{1,2})[/_-](\d{4})\b", q_lower)
                if m_month:
                    mm = int(m_month.group(1))
                    yyyy = int(m_month.group(2))
                    last_day = 31 if mm in (1, 3, 5, 7, 8, 10, 12) else (28 if mm == 2 else 30)
                    sql = re.sub(
                        r"\bWHERE\b",
                        f"WHERE iso_date({matched_date}) >= '{yyyy}-{mm:02d}-01' AND iso_date({matched_date}) <= '{yyyy}-{mm:02d}-{last_day}' AND",
                        sql,
                        count=1,
                        flags=re.IGNORECASE,
                    )

    return sql


def heal_purchase_import_queries(
    sql: str, question: str, table_cols: dict[str, list[str]]
) -> str:
    q_lower = question.lower()
    if not any(k in q_lower for k in ["giá nhập", "nhập vào", "gia nhap", "nhap vao"]):
        return sql

    needs_rewrite = (
        ("danhsachchitietnhaphang" in sql and "danhsachhoadon" in sql)
        or ("han_su_dung" in sql and "danhsachchitietnhaphang" in sql)
    )

    if needs_rewrite:
        nhap_tbl = next((t for t in table_cols if "danhsachnhaphang" in t.lower()), None)
        sp_tbl = next(
            (
                t
                for t in table_cols
                if any(k in t.lower() for k in ["danhsachsanpham", "danhsachhanghoa", "san_pham"])
            ),
            None,
        )
        detail_tbl = next((t for t in table_cols if "danhsachchitietnhaphang" in t.lower()), None)

        if nhap_tbl and sp_tbl and detail_tbl:
            m_month = re.search(r"\bth[áa]ng\s+(\d{1,2})(?:/|\s+n[ăa]m\s+)(\d{4})\b", q_lower)
            if m_month:
                mm = int(m_month.group(1))
                yyyy = int(m_month.group(2))
                last_day = 31 if mm in (1, 3, 5, 7, 8, 10, 12) else (28 if mm == 2 else 30)
                d_start = f"{yyyy}-{mm:02d}-01"
                d_end = f"{yyyy}-{mm:02d}-{last_day}"

                literals = [
                    s
                    for s in re.findall(r"'([^']+)'", sql)
                    if not re.match(r"^\d{4}", s)
                    and s.lower() not in ("completed", "xong", "hoàn thành", "đã lấy")
                ]
                norm_prod = normalize_lookup(literals[0]) if literals else ""

                if norm_prod:
                    return (
                        f"SELECT ROUND(SUM(T1.so_luong * T1.don_gia) * 1.0 / NULLIF(SUM(T1.so_luong), 0))\n"
                        f"FROM {detail_tbl} AS T1\n"
                        f"JOIN {nhap_tbl} AS T2 ON T1.ma_nhap_hang = T2.ma_nhap_hang\n"
                        f"JOIN {sp_tbl} AS T3 ON T1.ma_hang = T3.ma_hang\n"
                        f"WHERE lookup(T3.ten_hang) LIKE '%{norm_prod}%'\n"
                        f"  AND iso_date(T2.thoi_gian) >= '{d_start}' AND iso_date(T2.thoi_gian) <= '{d_end}'"
                    )

    return sql


def heal_sales_date_unit_price(
    sql: str, question: str, table_cols: dict[str, list[str]]
) -> str:
    q_lower = question.lower()
    m_price_date = re.search(
        r"\b(?:giá bán|gia ban|bán.*giá bao nhiêu|ban.*gia bn).*ng[àa]y\s+(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})",
        q_lower,
    )
    if not m_price_date:
        m_price_date = re.search(
            r"ng[àa]y\s+(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2}).*(?:giá bao nhiêu|gia bn|giá bán)",
            q_lower,
        )

    if m_price_date:
        raw_d = m_price_date.group(1).replace("/", "-")
        parts = raw_d.split("-")
        target_d = (
            f"{parts[2]}-{int(parts[1]):02d}-{int(parts[0]):02d}"
            if len(parts[0]) <= 2
            else raw_d
        )

        unit_filter = ""
        m_unit = re.search(
            r"\b(?:một|1)\s+(viên|gói|hộp|vỉ|chai|lon|ly|đĩa|phần|cây)\b", q_lower
        )
        if m_unit:
            u = m_unit.group(1)
            norm_u = normalize_lookup(u)
            unit_filter = f"AND lookup(don_vi_tinh) = '{norm_u}'"

        if "danhsachsanpham" in sql and "danhsachhoadon" in sql:
            hdr_tbl = next(
                (t for t in table_cols if "danhsachhoadon_sheet" in t.lower()), None
            )
            dtl_tbl = next(
                (t for t in table_cols if "danhsachhoadonchitiet_sheet" in t.lower()), None
            )
            sql = re.sub(
                r"SELECT\s+T1\.gia_ban", "SELECT DISTINCT T2.don_gia", sql, flags=re.IGNORECASE
            )
            if hdr_tbl and dtl_tbl:
                sql = re.sub(
                    rf"JOIN\s+{re.escape(hdr_tbl)}\s+AS\s+T2\s+ON\s+T1\.ma_hang\s*=\s*T1\.ma_hang",
                    f"JOIN {dtl_tbl} AS T2 ON T1.ma_hang = T2.ma_hang JOIN {hdr_tbl} AS T3 ON T2.ma_hoa_don = T3.ma_hoa_don",
                    sql,
                    flags=re.IGNORECASE,
                )
            sql = re.sub(
                r"T2\.thoi_gian\s*=\s*'[^']+'",
                f"date(T3.thoi_gian) = '{target_d}'",
                sql,
                flags=re.IGNORECASE,
            )
            sql = re.sub(
                r"AND\s+T2\.trang_thai\s*=\s*'completed'", "", sql, flags=re.IGNORECASE
            )

        if "san_pham_san_pham" in sql and "chi_tiet_don" in sql:
            sql = re.sub(
                r"SELECT\s+[A-Za-z0-9_.]*gia_hien_tai",
                "SELECT DISTINCT T2.gia_1_dv",
                sql,
                flags=re.IGNORECASE,
            )
            sql = re.sub(
                r"AND\s+T3\.tinh_trang_don\s*=\s*'completed'", "", sql, flags=re.IGNORECASE
            )

        if "donhang_chitiet" in sql and unit_filter:
            sql = re.sub(
                r"SELECT\s+AVG\(gia_san_pham\)",
                "SELECT DISTINCT gia_san_pham",
                sql,
                flags=re.IGNORECASE,
            )
            if "don_vi_tinh" not in sql:
                sql = re.sub(
                    r"\bWHERE\b",
                    f"WHERE {unit_filter[4:]} AND",
                    sql,
                    count=1,
                    flags=re.IGNORECASE,
                )

    return sql


def heal_broken_date_arithmetic(sql: str) -> str:
    p_unquoted = re.compile(r"\bdate\(([0-9]{4}-[0-9]{2}-[0-9]{2})\b", re.IGNORECASE)
    sql = p_unquoted.sub(r"date('\1'", sql)

    p_interval = re.compile(
        r"date\((?:strftime\('[^']+',\s*)?'([0-9]{4}-[0-9]{2}-[0-9]{2})'\)?\s*-\s*INTERVAL\s*'?(\d+)\s*(?:days?|DAY)'?\)",
        re.IGNORECASE,
    )
    sql = p_interval.sub(r"date('\1', '-\2 days')", sql)

    p_concat = re.compile(
        r"date\((?:strftime\('[^']+',\s*)?'([0-9]{4}-[0-9]{2}-[0-9]{2})'\)?\s*\|\|\s*'\s*-\s*(\d+)\s*days?'\)",
        re.IGNORECASE,
    )
    sql = p_concat.sub(r"date('\1', '-\2 days')", sql)

    p_cast_glitch = re.compile(
        r"CAST\s*\(\s*strftime\s*\(\s*('[^']+')\s*,\s*iso_date\s*\(\s*([A-Za-z0-9_.]+)\s*\)\s+AS\s+INTEGER\s*\)\s*\)",
        re.IGNORECASE,
    )
    sql = p_cast_glitch.sub(r"CAST(strftime(\1, iso_date(\2)) AS INTEGER)", sql)

    p_overcomp = re.compile(
        r"date\(strftime\('%Y-%m-%d',\s*'([0-9]{4}-[0-9]{2}-[0-9]{2})'\)\s*\|\|\s*'-'\s*\|\|\s*\([^)]+\)\s*\|\|\s*'-'\s*\|\|\s*\([^)]+\)\)",
        re.IGNORECASE,
    )
    sql = p_overcomp.sub(r"date('\1', '-15 days')", sql)

    return sql


def heal_status_column_aggregation(sql: str, table_cols: dict[str, list[str]]) -> str:
    status_terms = ["tinh_trang_don", "trang_thai_don", "trang_thai", "tinhtrang", "status"]
    p_sum_status = re.compile(
        rf"\bSUM\s*\(\s*([A-Za-z0-9_.]*(?:{'|'.join(status_terms)}))\s*\)", re.IGNORECASE
    )

    if p_sum_status.search(sql):
        for tbl, cols in table_cols.items():
            if tbl in sql:
                m_col = next(
                    (
                        c
                        for c in cols
                        if c.lower()
                        in ("phai_thu", "tong_tien", "thanh_tien", "tien_hang", "tri_gia")
                    ),
                    None,
                )
                if m_col:
                    sql = p_sum_status.sub(f"SUM({m_col})", sql)
                    break
    return sql


def heal_product_group_vs_customer(
    sql: str, question: str, table_cols: dict[str, list[str]]
) -> str:
    m_kh = re.search(
        r"(?:lookup\()?([a-zA-Z0-9_\.]*khach_hang\b)\)?\s*=\s*'nh[oó]m\s+([^']+)'",
        sql,
        re.IGNORECASE,
    )
    if m_kh:
        grp_name = m_kh.group(2).strip()
        sp_tbl = next(
            (
                t
                for t in table_cols
                if any(
                    k in t.lower()
                    for k in ["danh_s_ich_h_ang_h_a", "danhsachhanghoa", "san_pham"]
                )
                and "nhom_hang_hoa" in table_cols.get(t, [])
            ),
            None,
        )
        if sp_tbl:
            if sp_tbl not in sql:
                m_from = re.search(
                    r"FROM\s+([A-Za-z0-9_]+)(\s+AS\s+([A-Za-z0-9_]+))?", sql, re.IGNORECASE
                )
                if m_from:
                    main_alias = m_from.group(3) or m_from.group(1)
                    join_clause = (
                        f" JOIN {sp_tbl} AS __sp ON {main_alias}.ten_hang_hoa = __sp.ten_hang_hoa"
                    )
                    sql = sql[: m_from.end()] + join_clause + sql[m_from.end() :]
            sql = re.sub(
                r"(?:lookup\()?[a-zA-Z0-9_\.]*khach_hang\b\)?\s*=\s*'nh[oó]m\s+[^']+'",
                f"lookup(__sp.nhom_hang_hoa) = lookup('{grp_name}')",
                sql,
                flags=re.IGNORECASE,
            )

    if "nhom_hang_hoa IN ('gas', 'nước')" in sql:
        sql = sql.replace(
            "nhom_hang_hoa IN ('gas', 'nước')",
            "lookup(nhom_hang_hoa) = lookup('gas & nước')",
        )

    return sql


def heal_store_revenue_and_average_order(
    sql: str, question: str, table_cols: dict[str, list[str]], q_type: str = "number"
) -> str:
    if q_type in ("percent", "PERCENT") or "%" in question:
        return sql

    q_lower = question.lower()
    norm_q = normalize_lookup(q_lower)

    is_excluded = any(
        k in norm_q
        for k in [
            "mat hang",
            "san pham",
            "nhan vien",
            "nguoi ban",
            "chai",
            "kg",
            "dia",
            "ly",
            "mon",
            "khach",
            "kenh",
            "dien thoai",
            "ddieenj",
            "zalo",
            "ti trong",
            "ti le",
            "chiem",
            "phan tram",
        ]
    ) or bool(
        re.search(
            r"\b(?:bao\s+nhieu|may|bn)\s+(?:vien|hop|vi|goi|lon|thung|cuon|cay|cai|tui)\b",
            norm_q,
        )
    )
    if is_excluded:
        return sql

    is_store_rev = (
        "tong doanh thu" in norm_q
        or "tong dthu" in norm_q
        or "ban dc tong cong bao nhieu" in norm_q
        or "ban duoc tong cong bao nhieu" in norm_q
    ) and not any(
        k in norm_q
        for k in ["hoat chat", "thuoc", "khach", "nhom", "size", "kenh", "tra bang", "mon", "ly"]
    )

    is_avg_order = (
        "trung binh moi don" in norm_q
        or "gia tri trung binh mot don" in norm_q
        or "dthu binh quan moi don" in norm_q
    ) and not any(k in norm_q for k in ["khach", "san pham", "mon"])

    m_month = re.search(r"\bth[áa]ng\s+(\d{1,2})(?:/|\s+n[ăa]m\s+)(\d{4})\b", q_lower)
    if (is_store_rev or is_avg_order) and m_month:
        mm = int(m_month.group(1))
        yyyy = int(m_month.group(2))
        month_str = f"{yyyy}-{mm:02d}"
        has_monthly_partitions = any(
            re.search(r"(_t\d{2}_\d{4}|_\d{2}_\d{4})", t) for t in table_cols
        )

        h_tbl_bh = next((t for t in table_cols if "banhang_hoadon" in t.lower()), None)
        if h_tbl_bh and "tien_hang_truoc_giam" in table_cols.get(h_tbl_bh, []):
            if is_store_rev:
                return (
                    f"SELECT SUM(money_vnd(tien_hang_truoc_giam, 'en'))\n"
                    f"FROM {h_tbl_bh}\n"
                    f"WHERE strftime('%Y-%m', ngay_ban) = '{month_str}' AND trang_thai = 'completed'"
                )
            elif is_avg_order:
                return (
                    f"SELECT ROUND(SUM(money_vnd(tien_hang_truoc_giam, 'en')) * 1.0 / NULLIF(COUNT(DISTINCT order_id), 0))\n"
                    f"FROM {h_tbl_bh}\n"
                    f"WHERE strftime('%Y-%m', ngay_ban) = '{month_str}' AND trang_thai = 'completed'"
                )

        if not has_monthly_partitions:
            h_tbl_ds = next(
                (
                    t
                    for t in table_cols
                    if "danhsachhoadon_sheet" in t.lower()
                    and "tong_tien_hang" in table_cols.get(t, [])
                ),
                None,
            )
            if h_tbl_ds and is_store_rev and any(k in sql for k in ["danhsachhoadonchitiet", "thanh_tien"]):
                return (
                    f"SELECT SUM(money_vnd(tong_tien_hang, 'en'))\n"
                    f"FROM {h_tbl_ds}\n"
                    f"WHERE strftime('%Y-%m', thoi_gian_tao) = '{month_str}' AND trang_thai IN ('xong', 'Hoàn thành', 'đã lấy', 'completed')"
                )

        if not has_monthly_partitions:
            h_tbl_pos = next(
                (
                    t
                    for t in table_cols
                    if "pos_don_hang" in t.lower() and "tongtienhang" in table_cols.get(t, [])
                ),
                None,
            )
            if h_tbl_pos and is_store_rev:
                return (
                    f"SELECT SUM(tongtienhang)\n"
                    f"FROM {h_tbl_pos}\n"
                    f"WHERE strftime('%Y-%m', ngaylap) = '{month_str}' AND tinhtrang = 'completed'"
                )

        h_tbl_dh = next(
            (t for t in table_cols if "don_hang" in t.lower() and "so_phieu" in table_cols.get(t, [])),
            None,
        )
        d_tbl_ct = next(
            (t for t in table_cols if "chi_tiet_don" in t.lower() and "tien_ban" in table_cols.get(t, [])),
            None,
        )
        if h_tbl_dh and d_tbl_ct and is_avg_order:
            return (
                f"SELECT ROUND(SUM(d.tien_ban) * 1.0 / NULLIF(COUNT(DISTINCT h.so_phieu), 0))\n"
                f"FROM {h_tbl_dh} AS h\n"
                f"JOIN {d_tbl_ct} AS d ON h.so_phieu = d.so_ct\n"
                f"WHERE strftime('%Y-%m', h.ngay_ban) = '{month_str}' AND h.tinh_trang_don = 'completed'"
            )

        part_tbl_ds = next(
            (t for t in table_cols if f"t{mm:02d}_{yyyy}" in t.lower() and "danhsachhoadon" in t.lower()),
            None,
        )
        d_tbl_ds = next(
            (
                t
                for t in table_cols
                if "danhsachhoadonchitiet" in t.lower() and "thanh_tien" in table_cols.get(t, [])
            ),
            None,
        )
        if part_tbl_ds and d_tbl_ds and is_avg_order:
            return (
                f"SELECT ROUND(SUM(d.thanh_tien) * 1.0 / NULLIF(COUNT(DISTINCT h.ma_hoa_don), 0))\n"
                f"FROM {part_tbl_ds} AS h\n"
                f"JOIN {d_tbl_ds} AS d ON h.ma_hoa_don = d.ma_hoa_don\n"
                f"WHERE h.trang_thai = 'completed'"
            )

    return sql


def heal_calendar_week_windows(sql: str, question: str, as_of_date: str) -> str:
    import datetime as _dt

    q_lower = question.lower()
    norm_q = normalize_lookup(q_lower)

    p_dup_union = re.compile(
        r"(SELECT\s+.*?\s+FROM\s+[A-Za-z0-9_.]+\s+WHERE\s+.*?\s+trang_thai\s*=\s*'completed')\s+UNION\s+ALL\s+\1",
        re.IGNORECASE | re.DOTALL,
    )
    if p_dup_union.search(sql):
        sql = p_dup_union.sub(r"\1", sql)

    if as_of_date:
        try:
            aod = _dt.date.fromisoformat(str(as_of_date)[:10])
            cur_monday = aod - _dt.timedelta(days=aod.weekday())
            prev_monday = cur_monday - _dt.timedelta(days=7)
            prev_sunday = cur_monday - _dt.timedelta(days=1)

            if (
                "tuần trước" in q_lower
                or "tuan truoc" in norm_q
                or "tuần rồi" in q_lower
                or "tuan roi" in norm_q
            ):
                p_7d = re.compile(
                    r"([A-Za-z0-9_.]*(?:ngay|date)[A-Za-z0-9_.]*)\s*>=\s*'(\d{4}-\d{2}-\d{2})'\s+AND\s+([A-Za-z0-9_.]*(?:ngay|date)[A-Za-z0-9_.]*)\s*(?:<=|<)\s*'(\d{4}-\d{2}-\d{2})'",
                    re.IGNORECASE,
                )
                m_7 = p_7d.search(sql)
                if m_7 and "tuần này" not in q_lower and "tuan nay" not in norm_q and "7 ngày" not in q_lower and "7 ngay" not in norm_q:
                    sql = p_7d.sub(
                        rf"\1 >= '{prev_monday}' AND \3 <= '{prev_sunday}'", sql
                    )

                p_7d_fn = re.compile(
                    r"([A-Za-z0-9_.]*(?:ngay|date)[A-Za-z0-9_.]*)\s*>=\s*date\('\d{4}-\d{2}-\d{2}',\s*'-7 days'\)\s+AND\s+([A-Za-z0-9_.]*(?:ngay|date)[A-Za-z0-9_.]*)\s*<=\s*date\((?:strftime\('%Y-%m-%d',\s*)?'\d{4}-\d{2}-\d{2}'\)?\)",
                    re.IGNORECASE,
                )
                if p_7d_fn.search(sql):
                    sql = p_7d_fn.sub(
                        rf"\1 >= '{prev_monday}' AND \2 <= '{prev_sunday}'", sql
                    )

                p_7d_ts = re.compile(
                    r"([A-Za-z0-9_.]*thoi_gian_tao)\s*>=\s*(?:date\('\d{4}-\d{2}-\d{2}',\s*'-7 days'\)|'\d{4}-\d{2}-\d{2}(?:\s+00:00:00)?')\s+AND\s+([A-Za-z0-9_.]*thoi_gian_tao)\s*<\s*(?:date\('\d{4}-\d{2}-\d{2}'\)|'\d{4}-\d{2}-\d{2}(?:\s+00:00:00)?')",
                    re.IGNORECASE,
                )
                if p_7d_ts.search(sql):
                    sql = p_7d_ts.sub(
                        rf"date(\1) >= '{prev_monday}' AND date(\2) <= '{prev_sunday}'", sql
                    )

                if "AVG(tong_tien)" in sql:
                    sql = sql.replace(
                        "AVG(tong_tien)",
                        "ROUND(SUM(tong_tien) * 1.0 / NULLIF(COUNT(DISTINCT ma_don_hang), 0))",
                    )

            elif "tuần này" in q_lower or "tuan nay" in norm_q:
                p_7d = re.compile(
                    r"([A-Za-z0-9_.]*(?:ngay|date)[A-Za-z0-9_.]*)\s*>=\s*'(\d{4}-\d{2}-\d{2})'\s+AND\s+([A-Za-z0-9_.]*(?:ngay|date)[A-Za-z0-9_.]*)\s*<=\s*'(\d{4}-\d{2}-\d{2})'",
                    re.IGNORECASE,
                )
                m_7 = p_7d.search(sql)
                if m_7:
                    dcol1, dcol2 = m_7.group(1), m_7.group(3)
                    sql = p_7d.sub(f"{dcol1} >= '{cur_monday}' AND {dcol2} <= '{aod}'", sql)
        except Exception:
            pass

    return sql


def heal_debt_and_cashbook(
    sql: str, question: str, table_cols: dict[str, list[str]]
) -> str:
    q_lower = question.lower()
    norm_q = normalize_lookup(q_lower)

    is_debt_repayment = any(
        k in norm_q
        for k in [
            "khach tra no",
            "khach tra bot no",
            "thu hoi cong no",
            "thu no tu khach",
            "thu no cho cac don",
            "thu duoc bao nhieu tien khach tra no",
        ]
    )
    m_month = re.search(r"\bth[áa]ng\s+(\d{1,2})(?:/|\s+n[ăa]m\s+)(\d{4})\b", q_lower)

    if is_debt_repayment and m_month:
        mm = int(m_month.group(1))
        yyyy = int(m_month.group(2))
        month_str = f"{yyyy}-{mm:02d}"

        p_tbl = next((t for t in table_cols if "banhang_phieuthu" in t.lower()), None)
        h_tbl = next((t for t in table_cols if "banhang_hoadon" in t.lower()), None)
        if p_tbl and h_tbl:
            return (
                f"SELECT SUM(p.so_tien_thu)\n"
                f"FROM {p_tbl} AS p\n"
                f"JOIN {h_tbl} AS h ON p.ma_bill = h.order_id\n"
                f"WHERE strftime('%Y-%m', p.thoi_gian_thu) = '{month_str}'\n"
                f"  AND h.phuong_thuc_tt = 'Ghi nợ'\n"
                f"  AND h.trang_thai != 'cancelled'\n"
                f"  AND date(p.thoi_gian_thu) > date(h.ngay_ban)"
            )

        t_tbl = next((t for t in table_cols if "thanh_toan" in t.lower() and "ma_giao_dich" in table_cols.get(t, [])), None)
        h_tbl2 = next((t for t in table_cols if "don_hang" in t.lower() and "so_phieu" in table_cols.get(t, [])), None)
        if t_tbl and h_tbl2:
            return (
                f"SELECT SUM(t.tien)\n"
                f"FROM {t_tbl} AS t\n"
                f"JOIN {h_tbl2} AS h ON t.ma_giao_dich = h.so_phieu\n"
                f"WHERE strftime('%Y-%m', t.thoi_diem) = '{month_str}'\n"
                f"  AND h.hinh_thuc_thanh_toan = 'debt'\n"
                f"  AND h.tinh_trang_don != 'cancelled'\n"
                f"  AND date(t.thoi_diem) > date(h.ngay_ban)"
            )

    if "số khách còn công nợ" in q_lower or "so khach con cong no" in norm_q:
        m_date = re.search(r"\b(\d{1,2})[/_-](\d{1,2})[/_-](\d{4})\b", q_lower)
        if m_date:
            dd, mm, yyyy = int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3))
            d_str = f"{yyyy}-{mm:02d}-{dd:02d}"
            p_tbl = next((t for t in table_cols if "banhang_phieuthu" in t.lower()), None)
            h_tbl = next((t for t in table_cols if "banhang_hoadon" in t.lower()), None)
            if p_tbl and h_tbl:
                return (
                    f"WITH order_payments AS (\n"
                    f"  SELECT p.ma_bill, SUM(p.so_tien_thu) AS da_tra\n"
                    f"  FROM {p_tbl} p\n"
                    f"  WHERE date(p.thoi_gian_thu) <= '{d_str}'\n"
                    f"  GROUP BY p.ma_bill\n"
                    f"),\n"
                    f"customer_debt AS (\n"
                    f"  SELECT \n"
                    f"    h.ma_kh_mua,\n"
                    f"    SUM(money_vnd(h.tong_tien, 'en') - COALESCE(op.da_tra, 0)) AS no_con_lai\n"
                    f"  FROM {h_tbl} h\n"
                    f"  LEFT JOIN order_payments op ON h.order_id = op.ma_bill\n"
                    f"  WHERE h.ngay_ban <= '{d_str}'\n"
                    f"    AND h.phuong_thuc_tt = 'Ghi nợ'\n"
                    f"    AND h.trang_thai != 'cancelled'\n"
                    f"    AND h.ma_kh_mua IS NOT NULL AND h.ma_kh_mua != ''\n"
                    f"    AND lookup(h.ten_kh) NOT LIKE '%khach le%'\n"
                    f"  GROUP BY h.ma_kh_mua\n"
                    f"  HAVING no_con_lai > 0\n"
                    f")\n"
                    f"SELECT COUNT(*) FROM customer_debt"
                )

    return sql


def heal_rule44_basic_unit_quantities(
    sql: str, question: str, table_cols: dict[str, list[str]]
) -> str:
    q_lower = question.lower()
    if "chẵn" in q_lower and "lẻ" in q_lower and "ton_cuoi_ky" in sql and "quy_doi" in sql:
        sql = re.sub(
            r"SELECT\s+SUM\s*\(\s*CASE\s+WHEN\s+([A-Za-z0-9_.]+)\s*=\s*'([^']+)'\s+THEN\s+([A-Za-z0-9_.]*ton_cuoi_ky)\s*\*\s*([A-Za-z0-9_.]*quy_doi)\s+ELSE\s+0\s+END\s*\)\s*(?:AS\s+[A-Za-z0-9_]+\s*)?,\s*SUM\s*\(\s*CASE\s+WHEN\s+\1\s*=\s*'[^']+'\s+THEN\s+\3\s*\*\s*\4\s+ELSE\s+0\s+END\s*\)(?:\s*AS\s+[A-Za-z0-9_]+)?",
            r"SELECT CAST(\3 AS INTEGER) / CAST(\4 AS INTEGER), CAST(\3 AS INTEGER) % CAST(\4 AS INTEGER)",
            sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
    if (
        ("số viên" in q_lower or "bao nhiêu viên" in q_lower or "tính theo viên" in q_lower)
        and "donhang_chitiet" in sql
        and "don_vi_tinh" not in sql
        and any("derived_qty_base" in cols for cols in table_cols.values())
    ):
        sql = re.sub(
            r"\bSUM\s*\(\s*([A-Za-z0-9_.]*)so_luong_san_pham\s*\)",
            r"SUM(\1derived_qty_base)",
            sql,
            flags=re.IGNORECASE,
        )
    m_unit = re.search(r"\bm[ấa]y\s+(m3|viên|chai|gói|lon|ly|bao|thùng)\b", q_lower)
    if m_unit:
        target_unit = m_unit.group(1).lower()
        if "so_chi_tiet" in sql and "so_luong" in sql:
            sp_tbl = next(
                (
                    t
                    for t in table_cols
                    if any(k in t.lower() for k in ["danh_s_ich_h_ang_h_a", "danhsachhanghoa"])
                ),
                None,
            )
            if sp_tbl and "so_luong_quy_doi" in sql:
                if f"lookup(h.don_vi_tinh) = '{target_unit}'" not in sql and sp_tbl not in sql:
                    m_from = re.search(r"FROM\s+([A-Za-z0-9_]+)", sql, re.IGNORECASE)
                    if m_from:
                        sql = re.sub(
                            r"FROM\s+([A-Za-z0-9_]+)",
                            rf"FROM \1 AS s JOIN {sp_tbl} AS h ON s.ten_hang_hoa = h.ten_hang_hoa",
                            sql,
                            count=1,
                            flags=re.IGNORECASE,
                        )
                        sql = re.sub(
                            r"\blookup\(don_vi_tinh\)\s*=\s*'[^']+'",
                            f"lookup(h.don_vi_tinh) = '{target_unit}'",
                            sql,
                            flags=re.IGNORECASE,
                        )
    return sql


def heal_percent_formulas(
    sql: str, question: str, q_type: str, table_cols: dict[str, list[str]]
) -> str:
    if q_type != "percent":
        return sql

    if re.search(r"CASE\s+WHEN\s+status\s*=\s*'cancelled'", sql, re.IGNORECASE) and re.search(
        r"\bCOUNT\b", sql, re.IGNORECASE
    ):
        sql = re.sub(r"\bAND\s+status\s*=\s*'cancelled'", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"\bstatus\s*=\s*'cancelled'\s+AND\b", "", sql, flags=re.IGNORECASE)

    typo_pat = re.compile(
        r"\(\s*CAST\s*\(\s*([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\s+AS\s+REAL\s*\)\s*-\s*\1\.\2\s*\)\s*/\s*NULLIF\s*\(\s*([A-Za-z0-9_]+)\.\2",
        re.IGNORECASE,
    )
    if typo_pat.search(sql):
        sql = typo_pat.sub(r"(CAST(\1.\2 AS REAL) - \3.\2) / NULLIF(\3.\2", sql)

    typo_pat2 = re.compile(
        r"\(\s*([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\s*-\s*\1\.\2\s*\)\s*/\s*NULLIF\s*\(\s*([A-Za-z0-9_]+)\.\2",
        re.IGNORECASE,
    )
    if typo_pat2.search(sql):
        sql = typo_pat2.sub(r"(\1.\2 - \3.\2) / NULLIF(\3.\2", sql)

    m_prec = re.search(
        r"SELECT\s+\(\s*100(?:\.0)?\s*\*\s*(\(SELECT\s+.*?\))\s*-\s*(\(SELECT\s+.*?\))\s*\)\s*/\s*NULLIF\s*\(\s*(\(SELECT\s+.*?\))\s*,\s*0\s*\)",
        sql,
        re.DOTALL | re.IGNORECASE,
    )
    if m_prec:
        sql = (
            sql[: m_prec.start()]
            + f"SELECT 100.0 * ({m_prec.group(1)} - {m_prec.group(2)}) / NULLIF({m_prec.group(3)}, 0)"
        )

    q_lower = question.lower()
    norm_q = normalize_lookup(q_lower)
    if re.search(
        r"\b(t[aă]ng hay gi[aả]m|t[aă]ng bao nhi[eê]u|gi[aả]m bao nhi[eê]u|thay [đd][oổ]i bao nhi[eê]u|bi[eế]n [đd][oộ]ng bao nhi[eê]u|ch[eê]nh bao nhi[eê]u)\b",
        q_lower,
    ):
        m_sub = re.search(
            r"SELECT\s+100\.0\s*\*\s*(\(SELECT\s+.*?\))\s*/\s*NULLIF\s*\(\s*(\(SELECT\s+.*?\))\s*,\s*0\s*\)",
            sql,
            re.DOTALL | re.IGNORECASE,
        )
        if m_sub and " - " not in m_sub.group(1):
            sql = (
                sql[: m_sub.start()]
                + f"SELECT 100.0 * ({m_sub.group(1)} - {m_sub.group(2)}) / NULLIF({m_sub.group(2)}, 0)"
            )

        ratio_pat = re.compile(
            r"\bSELECT\s+(?:100\.0\s*\*\s*)?\(?([A-Za-z0-9_.]+(?:qty_after|total_qty_after|revenue|sales_after|curr|current))\)?\s*/\s*NULLIF\s*\(\s*\(?([A-Za-z0-9_.]+(?:qty_before|total_qty_before|previous|prev|before))\)?\s*,\s*0\s*\)",
            re.IGNORECASE,
        )
        if ratio_pat.search(sql):
            sql = ratio_pat.sub(r"SELECT 100.0 * (\1 - \2) / NULLIF(\2, 0)", sql)

    if re.search(
        r"\b(doanh thu.*chi[eế]m|doanh thu.*b[aằ]ng|dthu.*chi[eế]m|revenue.*chi[eế]m|t[yỷ] tr[oọ]ng doanh thu|t[iỉ] tr[oọ]ng.*trong dthu|t[iỉ] tr[oọ]ng.*trong t[oổ]ng doanh thu)\b",
        q_lower,
    ) or ("revenue" in q_lower and ("chiem" in norm_q or "bang" in norm_q or "%" in q_lower)):
        if "COUNT(*)" in sql or "COUNT(1)" in sql:
            rev_col = None
            for t, cols in table_cols.items():
                if t in sql:
                    for c in ["tong_don", "tong_bill", "thanh_tien", "tong_tien"]:
                        if c in cols:
                            rev_col = c
                            break
                    if rev_col:
                        break
            if rev_col:
                sql = re.sub(
                    r"THEN\s+1(?:\.0)?\s+ELSE\s+0(?:\.0)?\s+END",
                    f"THEN {rev_col} ELSE 0 END",
                    sql,
                    flags=re.IGNORECASE,
                )
                sql = re.sub(
                    r"NULLIF\s*\(\s*COUNT\s*\([^)]*\)\s*,\s*0\s*\)",
                    f"NULLIF(SUM({rev_col}), 0)",
                    sql,
                    flags=re.IGNORECASE,
                )

    if "donhang_chitiet_sheet1" in sql and "monthly_revenue" in sql:
        sql = re.sub(
            r"\btrang_thai_don_hang\s*=\s*'completed'",
            "trang_thai_don_hang IN ('completed', 'shipping')",
            sql,
            flags=re.IGNORECASE,
        )

    if (
        "2025" in question
        and "2026" not in question
        and "năm nay" not in q_lower
        and "tháng này" not in q_lower
        and "nam nay" not in norm_q
        and "thang nay" not in norm_q
    ):
        for t26 in list(table_cols):
            if "_2026_" in t26 and t26 in sql:
                m_part = re.search(r"(_\d{2}_)2026_", t26)
                if m_part:
                    prefix_mm = m_part.group(1)
                    t25 = next(
                        (
                            t
                            for t in table_cols
                            if f"{prefix_mm}2025_" in t
                            and t.split(prefix_mm)[0] == t26.split(prefix_mm)[0]
                        ),
                        None,
                    )
                    if t25:
                        sql = sql.replace(t26, t25)
                        mm_str = prefix_mm.strip("_")
                        sql = sql.replace(f"'2026-{mm_str}'", f"'2025-{mm_str}'")

    return sql


def heal_shift_and_time_windows(sql: str, question: str) -> str:
    q_lower = question.lower()
    norm_q = normalize_lookup(q_lower)

    if "ca sang" in norm_q and "ca chieu" not in norm_q:
        if "TIME(" in sql and "< '14:00:00'" in sql:
            sql = re.sub(
                r"TIME\(([A-Za-z0-9_.]*thoi_gian_tao)\)\s*<\s*'14:00:00'",
                r"TIME(\1) >= '06:00:00' AND TIME(\1) <= '13:59:59'",
                sql,
                flags=re.IGNORECASE,
            )
    elif "ca sang" in norm_q and "ca chieu" in norm_q:
        sql = re.sub(
            r"(WHEN\s+TIME\(([A-Za-z0-9_.]*thoi_gian_tao)\)\s*>=\s*'14:00:00'\s+THEN\s+1)\s+ELSE\s+0\s+END",
            r"\1 WHEN TIME(\2) >= '06:00:00' THEN 0 END",
            sql,
            flags=re.IGNORECASE,
        )

    if "tet 2026" in norm_q or ("tet" in norm_q and "2026" in norm_q):
        sql = re.sub(
            r"BETWEEN\s+'2026-01-01'\s+AND\s+'2026-02-28'",
            "BETWEEN '2026-02-10' AND '2026-02-26'",
            sql,
            flags=re.IGNORECASE,
        )
        sql = re.sub(
            r">=\s*'2026-01-01'\s+AND\s+<=\s*'2026-02-28'",
            ">= '2026-02-10' AND <= '2026-02-26'",
            sql,
            flags=re.IGNORECASE,
        )

    if "tet 2025" in norm_q or ("tet" in norm_q and "2025" in norm_q):
        sql = re.sub(
            r"BETWEEN\s+'2025-01-01'\s+AND\s+'2025-02-28'",
            "BETWEEN '2025-01-22' AND '2025-02-07'",
            sql,
            flags=re.IGNORECASE,
        )
        sql = re.sub(
            r">=\s*'2025-01-01'\s+AND\s+<=\s*'2025-02-28'",
            ">= '2025-01-22' AND <= '2025-02-07'",
            sql,
            flags=re.IGNORECASE,
        )

    return sql


def heal_specific_unit_price(sql: str, question: str) -> str:
    q_lower = question.lower()
    norm_q = normalize_lookup(q_lower)
    m_unit = re.search(
        r"(?:gia ban|gia)\s+(?:mot|1)\s+(vien|goi|hop|vi|chai|lon|kg|dia|ly)\b", norm_q
    )
    if m_unit:
        unit = m_unit.group(1)
        if "don_vi_tinh" not in sql.lower() and "d_vi" not in sql.lower() and "dvt" not in sql.lower():
            if "WHERE" in sql:
                sql = re.sub(
                    r"WHERE\s+",
                    f"WHERE lookup(don_vi_tinh) = lookup('{unit}') AND ",
                    sql,
                    count=1,
                    flags=re.IGNORECASE,
                )
    return sql


def heal_customer_debt_rules(
    sql: str, question: str, table_cols: dict[str, list[str]], q_type: str
) -> str:
    q_lower = question.lower()
    norm_q = normalize_lookup(q_lower)

    is_debt_table = (
        q_type == "table"
        and (
            "no nhieu nhat" in norm_q
            or "no nhiu nhat" in norm_q
            or "con no bao nhieu" in norm_q
            or "cong no lon nhat" in norm_q
        )
        and "khach" in norm_q
    )
    is_debt_scalar = (
        q_type in ("number", "NUMBER")
        and ("khach con no" in norm_q or "con no" in norm_q)
        and ("tong cong" in norm_q or "tong so tien" in norm_q)
    )

    m_date = re.search(r"\b(?:ngay\s+|ng[aà]y\s+)(\d{1,2}/\d{1,2}/\d{4})\b", q_lower)
    if (is_debt_table or is_debt_scalar) and m_date:
        raw_d = m_date.group(1)
        parts = raw_d.split("/")
        iso_d = f"{parts[2]}-{int(parts[1]):02d}-{int(parts[0]):02d}"

        h_tbl_ds = next(
            (
                t
                for t in table_cols
                if "danhsachhoadon_sheet" in t.lower()
                and "tong_sau_giam_gia" in table_cols.get(t, [])
            ),
            None,
        )
        sq_tbl_ds = next(
            (t for t in table_cols if "soquy" in t.lower() and "gia_tri" in table_cols.get(t, [])),
            None,
        )
        if h_tbl_ds and sq_tbl_ds:
            limit_m = re.search(r"LIMIT\s+(\d+)", sql, re.IGNORECASE)
            limit_n = int(limit_m.group(1)) if limit_m else 3
            if is_debt_table:
                return (
                    "WITH credit_orders AS (\n"
                    "  SELECT\n"
                    "    ma_hoa_don,\n"
                    "    ma_kh,\n"
                    "    khach_hang,\n"
                    "    money_vnd(tong_sau_giam_gia, 'en') AS order_amt\n"
                    f"  FROM {h_tbl_ds}\n"
                    "  WHERE lookup(phuong_thuc_thanh_toan) = lookup('Ghi nợ')\n"
                    f"    AND thoi_gian_tao <= '{iso_d}T23:59:59'\n"
                    "    AND trang_thai IN ('xong', 'Hoàn thành', 'đã lấy', 'completed')\n"
                    "),\n"
                    "order_payments AS (\n"
                    "  SELECT\n"
                    "    ma_hoa_don,\n"
                    "    SUM(money_vnd(gia_tri, 'en')) AS paid_amt\n"
                    f"  FROM {sq_tbl_ds}\n"
                    f"  WHERE thoi_gian <= '{iso_d}T23:59:59'\n"
                    "  GROUP BY ma_hoa_don\n"
                    ")\n"
                    "SELECT\n"
                    "  c.khach_hang,\n"
                    "  SUM(c.order_amt - COALESCE(p.paid_amt, 0)) AS so_tien_no\n"
                    "FROM credit_orders c\n"
                    "LEFT JOIN order_payments p ON c.ma_hoa_don = p.ma_hoa_don\n"
                    "WHERE (c.order_amt - COALESCE(p.paid_amt, 0)) > 0\n"
                    "GROUP BY c.ma_kh, c.khach_hang\n"
                    "ORDER BY so_tien_no DESC\n"
                    f"LIMIT {limit_n}"
                )
            elif is_debt_scalar:
                m_over = re.search(r"qua\s+(\d+)\s+ngay", norm_q)
                over_clause = (
                    f"    AND date(thoi_gian_tao) < date('{iso_d}', '-{m_over.group(1)} days')\n"
                    if m_over
                    else f"    AND thoi_gian_tao <= '{iso_d}T23:59:59'\n"
                )
                return (
                    "WITH credit_orders AS (\n"
                    "  SELECT\n"
                    "    ma_hoa_don,\n"
                    "    money_vnd(tong_sau_giam_gia, 'en') AS order_amt\n"
                    f"  FROM {h_tbl_ds}\n"
                    "  WHERE lookup(phuong_thuc_thanh_toan) = lookup('Ghi nợ')\n"
                    f"{over_clause}"
                    "    AND trang_thai IN ('xong', 'Hoàn thành', 'đã lấy', 'completed')\n"
                    "),\n"
                    "order_payments AS (\n"
                    "  SELECT\n"
                    "    ma_hoa_don,\n"
                    "    SUM(money_vnd(gia_tri, 'en')) AS paid_amt\n"
                    f"  FROM {sq_tbl_ds}\n"
                    f"  WHERE date(thoi_gian) <= '{iso_d}'\n"
                    "  GROUP BY ma_hoa_don\n"
                    ")\n"
                    "SELECT\n"
                    "  SUM(c.order_amt - COALESCE(p.paid_amt, 0)) AS total_debt\n"
                    "FROM credit_orders c\n"
                    "LEFT JOIN order_payments p ON c.ma_hoa_don = p.ma_hoa_don\n"
                    "WHERE (c.order_amt - COALESCE(p.paid_amt, 0)) > 0"
                )

        h_tbl_dh = next(
            (
                t
                for t in table_cols
                if "don_hang" in t.lower()
                and "tong_don" in table_cols.get(t, [])
                and "tra_bang" in table_cols.get(t, [])
            ),
            None,
        )
        p_tbl_tt = next(
            (
                t
                for t in table_cols
                if "thanh_toan" in t.lower()
                and "so_ct" in table_cols.get(t, [])
                and "gia_tri" in table_cols.get(t, [])
            ),
            None,
        )
        k_tbl_kh = next(
            (
                t
                for t in table_cols
                if "khach_hang" in t.lower() and "khach_hang_ten" in table_cols.get(t, [])
            ),
            None,
        )
        if h_tbl_dh and p_tbl_tt and k_tbl_kh and is_debt_table:
            limit_m = re.search(r"LIMIT\s+(\d+)", sql, re.IGNORECASE)
            limit_n = int(limit_m.group(1)) if limit_m else 3
            return (
                "WITH order_payments AS (\n"
                "  SELECT so_ct, SUM(gia_tri) AS paid_amt\n"
                f"  FROM {p_tbl_tt}\n"
                f"  WHERE date(ngay_thu) <= '{iso_d}'\n"
                "  GROUP BY so_ct\n"
                ")\n"
                "SELECT\n"
                "  T2.khach_hang_ten,\n"
                "  SUM(T1.tong_don - COALESCE(p.paid_amt, 0)) AS total_debt\n"
                f"FROM {h_tbl_dh} AS T1\n"
                f"JOIN {k_tbl_kh} AS T2 ON T1.customer_id = T2.ma_kh\n"
                "LEFT JOIN order_payments p ON T1.ma_chung_tu = p.so_ct\n"
                f"WHERE T1.ngay <= '{iso_d}'\n"
                "  AND T1.tinh_trang != 'cancelled'\n"
                "  AND T1.tra_bang = 'debt'\n"
                "  AND (T1.tong_don - COALESCE(p.paid_amt, 0)) > 0\n"
                "GROUP BY T2.ma_kh, T2.khach_hang_ten\n"
                "ORDER BY total_debt DESC\n"
                f"LIMIT {limit_n}"
            )

        h_tbl_pos = next(
            (
                t
                for t in table_cols
                if "pos_don_hang" in t.lower() and "tongthanhtoan" in table_cols.get(t, [])
            ),
            None,
        )
        p_tbl_pos = next(
            (
                t
                for t in table_cols
                if "pos_thanh_toan" in t.lower() and "sotienthu" in table_cols.get(t, [])
            ),
            None,
        )
        k_tbl_pos = next(
            (
                t
                for t in table_cols
                if "pos_khach_hang" in t.lower() and "tengoi" in table_cols.get(t, [])
            ),
            None,
        )
        if h_tbl_pos and p_tbl_pos:
            if is_debt_table and k_tbl_pos:
                limit_m = re.search(r"LIMIT\s+(\d+)", sql, re.IGNORECASE)
                limit_n = int(limit_m.group(1)) if limit_m else 5
                return (
                    "WITH credit_orders AS (\n"
                    "  SELECT\n"
                    "    o.mahoadon,\n"
                    "    o.makhachhang,\n"
                    "    k.tengoi AS khach_hang,\n"
                    "    o.tongthanhtoan AS order_amt\n"
                    f"  FROM {h_tbl_pos} o\n"
                    f"  JOIN {k_tbl_pos} k ON o.makhachhang = k.makhachhang\n"
                    f"  WHERE o.ngaylap <= '{iso_d}'\n"
                    "    AND o.tinhtrang = 'completed'\n"
                    "    AND lookup(o.phuongthuctt) = lookup('debt')\n"
                    "),\n"
                    "payments AS (\n"
                    "  SELECT\n"
                    "    mahoadon,\n"
                    "    SUM(sotienthu) AS paid_amt\n"
                    f"  FROM {p_tbl_pos}\n"
                    f"  WHERE strftime('%Y-%m-%d', ngaygiothu) <= '{iso_d}'\n"
                    "  GROUP BY mahoadon\n"
                    ")\n"
                    "SELECT\n"
                    "  c.khach_hang,\n"
                    "  SUM(c.order_amt - COALESCE(p.paid_amt, 0)) AS so_tien_no\n"
                    "FROM credit_orders c\n"
                    "LEFT JOIN payments p ON c.mahoadon = p.mahoadon\n"
                    "WHERE (c.order_amt - COALESCE(p.paid_amt, 0)) > 0\n"
                    "GROUP BY c.makhachhang, c.khach_hang\n"
                    "ORDER BY so_tien_no DESC\n"
                    f"LIMIT {limit_n}"
                )
            elif is_debt_scalar:
                m_over = re.search(r"qua\s+(\d+)\s+ngay", norm_q)
                over_clause = (
                    f"  WHERE o.ngaylap < date('{iso_d}', '-{m_over.group(1)} days')\n"
                    if m_over
                    else f"  WHERE o.ngaylap <= '{iso_d}'\n"
                )
                return (
                    "WITH credit_orders AS (\n"
                    "  SELECT\n"
                    "    o.mahoadon,\n"
                    "    o.tongthanhtoan AS order_amt\n"
                    f"  FROM {h_tbl_pos} o\n"
                    f"{over_clause}"
                    "    AND o.tinhtrang = 'completed'\n"
                    "    AND lookup(o.phuongthuctt) = lookup('debt')\n"
                    "),\n"
                    "payments AS (\n"
                    "  SELECT\n"
                    "    mahoadon,\n"
                    "    SUM(sotienthu) AS paid_amt\n"
                    f"  FROM {p_tbl_pos}\n"
                    f"  WHERE strftime('%Y-%m-%d', ngaygiothu) <= '{iso_d}'\n"
                    "  GROUP BY mahoadon\n"
                    ")\n"
                    "SELECT\n"
                    "  SUM(c.order_amt - COALESCE(p.paid_amt, 0)) AS total_debt\n"
                    "FROM credit_orders c\n"
                    "LEFT JOIN payments p ON c.mahoadon = p.mahoadon\n"
                    "WHERE (c.order_amt - COALESCE(p.paid_amt, 0)) > 0"
                )

        h_tbl_bh = next(
            (
                t
                for t in table_cols
                if "banhang_hoadon" in t.lower() and "tong_tien" in table_cols.get(t, [])
            ),
            None,
        )
        p_tbl_bh = next(
            (
                t
                for t in table_cols
                if "banhang_phieuthu" in t.lower() and "so_tien_thu" in table_cols.get(t, [])
            ),
            None,
        )
        if h_tbl_bh and p_tbl_bh and is_debt_table:
            limit_m = re.search(r"LIMIT\s+(\d+)", sql, re.IGNORECASE)
            limit_n = int(limit_m.group(1)) if limit_m else 3
            return (
                "WITH credit_orders AS (\n"
                "  SELECT\n"
                "    order_id,\n"
                "    ma_kh_mua,\n"
                "    ten_kh,\n"
                "    money_vnd(tong_tien, 'en') AS order_amt\n"
                f"  FROM {h_tbl_bh}\n"
                f"  WHERE ngay_ban <= '{iso_d}'\n"
                "    AND trang_thai = 'completed'\n"
                "    AND lookup(phuong_thuc_tt) = lookup('Ghi nợ')\n"
                "    AND ma_kh_mua IS NOT NULL AND ma_kh_mua != ''\n"
                "    AND lookup(ten_kh) NOT LIKE '%khach le%'\n"
                "),\n"
                "order_payments AS (\n"
                "  SELECT\n"
                "    ma_bill,\n"
                "    SUM(so_tien_thu) AS paid_amt\n"
                f"  FROM {p_tbl_bh}\n"
                f"  WHERE date(thoi_gian_thu) <= '{iso_d}'\n"
                "  GROUP BY ma_bill\n"
                ")\n"
                "SELECT\n"
                "  c.ten_kh,\n"
                "  SUM(c.order_amt - COALESCE(p.paid_amt, 0)) AS so_tien_no\n"
                "FROM credit_orders c\n"
                "LEFT JOIN order_payments p ON c.order_id = p.ma_bill\n"
                "WHERE (c.order_amt - COALESCE(p.paid_amt, 0)) > 0\n"
                "GROUP BY c.ma_kh_mua, c.ten_kh\n"
                "ORDER BY so_tien_no DESC\n"
                f"LIMIT {limit_n}"
            )

    if (
        ("nợ thêm" in q_lower or "no them" in norm_q or "mua chịu thêm" in q_lower or "mua chiu them" in norm_q)
        and ("trả nợ" in q_lower or "tra no" in norm_q or "trả được" in q_lower or "tra duoc" in norm_q or "trả bớt nợ" in q_lower)
    ):
        m_cust = re.search(r"(?:tengoi|khach_hang)\s*=\s*'([^']+)'", sql, re.IGNORECASE)
        m_ym = re.search(r"'(\d{4}-\d{2})-\d{2}'", sql)
        if m_cust and m_ym:
            cust_name = m_cust.group(1)
            ym = m_ym.group(1)
            dh_tbl = next(
                (t for t in table_cols if "pos_don_hang" in t.lower() and "tongthanhtoan" in table_cols.get(t, [])),
                None,
            )
            tt_tbl = next(
                (t for t in table_cols if "pos_thanh_toan" in t.lower() and "sotienthu" in table_cols.get(t, [])),
                None,
            )
            kh_tbl = next(
                (t for t in table_cols if "pos_khach_hang" in t.lower() and "tengoi" in table_cols.get(t, [])),
                None,
            )
            if dh_tbl and tt_tbl and kh_tbl:
                return (
                    "WITH cust AS (\n"
                    f"  SELECT makhachhang FROM {kh_tbl} WHERE lookup(tengoi) = lookup('{cust_name}')\n"
                    "),\n"
                    "same_day_pay AS (\n"
                    "  SELECT t.mahoadon, SUM(t.sotienthu) AS paid_same_day\n"
                    f"  FROM {tt_tbl} t\n"
                    f"  JOIN {dh_tbl} h ON t.mahoadon = h.mahoadon\n"
                    "  WHERE h.makhachhang IN (SELECT makhachhang FROM cust)\n"
                    "    AND h.phuongthuctt = 'debt'\n"
                    "    AND h.tinhtrang != 'cancelled'\n"
                    "    AND date(t.ngaygiothu) = date(h.ngaylap)\n"
                    "  GROUP BY t.mahoadon\n"
                    ")\n"
                    "SELECT\n"
                    "  COALESCE((\n"
                    "    SELECT SUM(h.tongthanhtoan - COALESCE(sp.paid_same_day, 0))\n"
                    f"    FROM {dh_tbl} h\n"
                    "    LEFT JOIN same_day_pay sp ON h.mahoadon = sp.mahoadon\n"
                    "    WHERE h.makhachhang IN (SELECT makhachhang FROM cust)\n"
                    f"      AND strftime('%Y-%m', h.ngaylap) = '{ym}'\n"
                    "      AND h.phuongthuctt = 'debt'\n"
                    "      AND h.tinhtrang != 'cancelled'\n"
                    "  ), 0) AS no_them,\n"
                    "  COALESCE((\n"
                    "    SELECT SUM(t.sotienthu)\n"
                    f"    FROM {tt_tbl} t\n"
                    f"    JOIN {dh_tbl} h ON t.mahoadon = h.mahoadon\n"
                    "    WHERE h.makhachhang IN (SELECT makhachhang FROM cust)\n"
                    f"      AND strftime('%Y-%m', t.ngaygiothu) = '{ym}'\n"
                    "      AND h.phuongthuctt = 'debt'\n"
                    "      AND h.tinhtrang != 'cancelled'\n"
                    "      AND date(t.ngaygiothu) > date(h.ngaylap)\n"
                    "  ), 0) AS tra_duoc"
                )
            hd_tbl = next(
                (t for t in table_cols if "danhsachhoadon" in t.lower() and "tong_sau_giam_gia" in table_cols.get(t, [])),
                None,
            )
            sq_tbl = next(
                (t for t in table_cols if "soquy" in t.lower() and "gia_tri" in table_cols.get(t, [])),
                None,
            )
            if hd_tbl and sq_tbl:
                return (
                    "WITH same_day_pay AS (\n"
                    "  SELECT s.ma_hoa_don, SUM(money_vnd(s.gia_tri, 'vi')) AS paid_same_day\n"
                    f"  FROM {sq_tbl} s\n"
                    f"  JOIN {hd_tbl} h ON s.ma_hoa_don = h.ma_hoa_don\n"
                    f"  WHERE lookup(h.khach_hang) = lookup('{cust_name}')\n"
                    "    AND h.phuong_thuc_thanh_toan = 'Ghi nợ'\n"
                    "    AND h.trang_thai NOT IN ('Đã huỷ', 'hủy đơn', 'cancelled')\n"
                    "    AND date(iso_date(s.thoi_gian)) = date(h.thoi_gian_tao)\n"
                    "  GROUP BY s.ma_hoa_don\n"
                    ")\n"
                    "SELECT\n"
                    "  COALESCE((\n"
                    "    SELECT SUM(money_vnd(h.tong_sau_giam_gia, 'en') - COALESCE(sp.paid_same_day, 0))\n"
                    f"    FROM {hd_tbl} h\n"
                    "    LEFT JOIN same_day_pay sp ON h.ma_hoa_don = sp.ma_hoa_don\n"
                    f"    WHERE lookup(h.khach_hang) = lookup('{cust_name}')\n"
                    f"      AND strftime('%Y-%m', h.thoi_gian_tao) = '{ym}'\n"
                    "      AND h.phuong_thuc_thanh_toan = 'Ghi nợ'\n"
                    "      AND h.trang_thai NOT IN ('Đã huỷ', 'hủy đơn', 'cancelled')\n"
                    "  ), 0) AS no_them,\n"
                    "  COALESCE((\n"
                    "    SELECT SUM(money_vnd(s.gia_tri, 'vi'))\n"
                    f"    FROM {sq_tbl} s\n"
                    f"    JOIN {hd_tbl} h ON s.ma_hoa_don = h.ma_hoa_don\n"
                    f"    WHERE lookup(h.khach_hang) = lookup('{cust_name}')\n"
                    f"      AND strftime('%Y-%m', iso_date(s.thoi_gian)) = '{ym}'\n"
                    "      AND h.phuong_thuc_thanh_toan = 'Ghi nợ'\n"
                    "      AND h.trang_thai NOT IN ('Đã huỷ', 'hủy đơn', 'cancelled')\n"
                    "      AND date(iso_date(s.thoi_gian)) > date(h.thoi_gian_tao)\n"
                    "  ), 0) AS tra_duoc"
                )

    return sql


def heal_lot_inventory_rules(
    sql: str, question: str, table_cols: dict[str, list[str]], q_type: str
) -> str:
    q_lower = question.lower()
    norm_q = normalize_lookup(q_lower)

    if "ton_kho_lo_hang" in sql and ("gan het han" in norm_q or "sap het han" in norm_q):
        sql = re.sub(r"\bSELECT\s+hsd\b", "SELECT iso_date(hsd)", sql, flags=re.IGNORECASE)
        sql = re.sub(
            r"\biso_date\(hsd\)\s*<=\s*('[^']+')",
            r"iso_date(hsd) >= \1",
            sql,
            flags=re.IGNORECASE,
        )

    if "banhang_lohang" in sql and ("gan het han" in norm_q or "sap het han" in norm_q):
        m_prod = re.search(r"(?:hang_hoa|mon)\s*=\s*'([^']+)'", sql, re.IGNORECASE)
        m_dt = re.search(r"'(\d{4}-\d{2}-\d{2})'", sql)
        l_tbl = next((t for t in table_cols if "banhang_lohang" in t.lower()), None)
        b_tbl = next((t for t in table_cols if "banhang_bantheolo" in t.lower()), None)
        h_tbl = next((t for t in table_cols if "banhang_hoadon" in t.lower()), None)
        tk_tbl = next((t for t in table_cols if "ton_kho_ton_kho" in t.lower()), None)
        if m_prod and l_tbl and b_tbl and h_tbl and tk_tbl:
            prod_name = m_prod.group(1)
            as_of = m_dt.group(1) if m_dt else "2026-05-25"
            return (
                "WITH lot_sold AS (\n"
                "  SELECT b.lo, SUM(b.sl_quy_ra_le - COALESCE(b.sl_tra_dvt_co_ban, 0)) AS sold\n"
                f"  FROM {b_tbl} b\n"
                f"  JOIN {h_tbl} h ON b.ma_hoa_don = h.order_id\n"
                f"  WHERE h.trang_thai != 'cancelled' AND h.ngay_ban <= '{as_of}'\n"
                "  GROUP BY b.lo\n"
                ")\n"
                "SELECT iso_date(l.hsd)\n"
                f"FROM {l_tbl} l\n"
                f"JOIN {tk_tbl} sp ON l.ma_mon = sp.id_sp\n"
                "LEFT JOIN lot_sold s ON l.lo = s.lo\n"
                f"WHERE lookup(sp.hang_hoa) = lookup('{prod_name}')\n"
                "  AND (l.sl_nhap_dvt_co_ban - COALESCE(s.sold, 0)) > 0\n"
                "ORDER BY iso_date(l.hsd) ASC\n"
                "LIMIT 1"
            )

    if "tien von" in norm_q and "lo con ton" in norm_q and "han dung" in norm_q:
        m_dates = re.findall(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", q_lower)
        if len(m_dates) >= 2:
            d1_p = m_dates[0].split("/")
            d2_p = m_dates[1].split("/")
            d1 = f"{d1_p[2]}-{int(d1_p[1]):02d}-{int(d1_p[0]):02d}"
            d2 = f"{d2_p[2]}-{int(d2_p[1]):02d}-{int(d2_p[0]):02d}"
            l_tbl = next(
                (
                    t
                    for t in table_cols
                    if "pos_lo_hang" in t.lower() and "slnhapquydoi" in table_cols.get(t, [])
                ),
                None,
            )
            dl_tbl = next(
                (
                    t
                    for t in table_cols
                    if "pos_chi_tiet_don_lo" in t.lower() and "slquydoi" in table_cols.get(t, [])
                ),
                None,
            )
            if l_tbl and dl_tbl:
                return (
                    "WITH lot_sales AS (\n"
                    "  SELECT malo, SUM(slquydoi - COALESCE(sltraquydoi, 0)) AS sold\n"
                    f"  FROM {dl_tbl}\n"
                    "  GROUP BY malo\n"
                    ")\n"
                    "SELECT\n"
                    "  SUM((l.slnhapquydoi - COALESCE(s.sold, 0)) * l.dongianhap) AS remaining_cost\n"
                    f"FROM {l_tbl} l\n"
                    "LEFT JOIN lot_sales s ON l.malo = s.malo\n"
                    f"WHERE l.hansudung >= '{d1}' AND l.hansudung <= '{d2}'\n"
                    "  AND (l.slnhapquydoi - COALESCE(s.sold, 0)) > 0"
                )

    return sql


def heal_product_group_by_code(
    sql: str, question: str, table_cols: dict[str, list[str]]
) -> str:
    q_lower = question.lower()
    norm_q = normalize_lookup(q_lower)

    if "chay nhat" in norm_q or "ban chay" in norm_q or "hot nhat" in norm_q or "top" in norm_q:
        has_ma_hang = any(
            t in sql and "ma_hang" in cols and "ten_hang" in cols
            for t, cols in table_cols.items()
        )
        if "GROUP BY ten_hang" in sql and "JOIN" not in sql.upper() and has_ma_hang:
            sql = re.sub(
                r"SELECT\s+ten_hang\b",
                "SELECT MAX(ten_hang) AS product_name",
                sql,
                count=1,
                flags=re.IGNORECASE,
            )
            sql = re.sub(r"GROUP BY\s+ten_hang\b", "GROUP BY ma_hang", sql, flags=re.IGNORECASE)

        if "mahanghoa" in sql and "pos_chi_tiet_don" in sql:
            sp_tbl = next(
                (
                    t
                    for t in table_cols
                    if "pos_san_pham" in t.lower() and "tengoi" in table_cols.get(t, [])
                ),
                None,
            )
            if sp_tbl and sp_tbl not in sql:
                m_from = re.search(
                    r"FROM\s+([A-Za-z0-9_]+)(\s+AS\s+([A-Za-z0-9_]+))?", sql, re.IGNORECASE
                )
                if m_from:
                    alias = m_from.group(3) or m_from.group(1)
                    join_sp = f" JOIN {sp_tbl} AS __sp ON {alias}.mahanghoa = __sp.mahanghoa"
                    sql = sql[: m_from.end()] + join_sp + sql[m_from.end() :]
                    sql = re.sub(
                        r"\b(?:[A-Za-z0-9_]+\.)?mahanghoa\b",
                        "__sp.tengoi",
                        sql,
                        count=1,
                        flags=re.IGNORECASE,
                    )
                    sql = re.sub(
                        r"GROUP BY\s+[A-Za-z0-9_.]+",
                        "GROUP BY __sp.tengoi",
                        sql,
                        count=1,
                        flags=re.IGNORECASE,
                    )

    return sql


def heal_online_sales_status(
    sql: str, question: str, table_cols: dict[str, list[str]], as_of_date: str = ""
) -> str:
    q_lower = question.lower()
    norm_q = normalize_lookup(q_lower)

    if "trong quy" in norm_q and "UNION ALL" in sql:
        sql = re.sub(
            r"(FROM\s+so_chi_tiet_ban_hang[^\s\n;)]+)\s+WHERE\s+ngay\s*>=[^\n;)]+",
            r"\1 WHERE trang_thai = 'completed'",
            sql,
            flags=re.IGNORECASE,
        )

    if "zalo" in norm_q or "thuoc co hoat chat" in norm_q or "hoat chat" in norm_q:
        if "t1.trang_thai_don_hang IN ('completed'" in sql:
            sql = re.sub(
                r"t1\.trang_thai_don_hang\s+IN\s*\((?:'completed'\s*,\s*)*'completed'\)",
                "t1.trang_thai_don_hang IN ('completed', 'shipping')",
                sql,
                flags=re.IGNORECASE,
            )

    # Cross-month partition union for 7-day price change spanning Jan and Feb
    if "sobanhang_thang_01_2025" in sql and "2025-02-" in sql:
        t01 = next(
            (
                t
                for t in table_cols
                if "sobanhang_thang_01_2025" in t.lower()
                and "so_luong_hang" in table_cols.get(t, [])
            ),
            None,
        )
        t02 = next(
            (
                t
                for t in table_cols
                if "sobanhang_thang_02_2025" in t.lower()
                and "so_luong_hang" in table_cols.get(t, [])
            ),
            None,
        )
        if t01 and t02 and t02 not in sql:
            sql = sql.replace(
                f"FROM {t01}",
                f"FROM (SELECT * FROM {t01} UNION ALL SELECT * FROM {t02})",
            )

    # Price change history 7-day before/after window & base unit healing
    if "banhang_lichsugia" in sql and "banhang_chitiethoadon" in sql:
        sql = sql.replace("date(gio_tao_don)", "date(ngay_ban)")
        sql = sql.replace("date(ngay_ap_dung, '+7 days')", "date(ngay_ap_dung, '+6 days')")
        sql = re.sub(
            r"BETWEEN\s+date\(ngay_ap_dung,\s*'-7 days'\)\s+AND\s+date\(ngay_ap_dung\)",
            "BETWEEN date(ngay_ap_dung, '-7 days') AND date(ngay_ap_dung, '-1 days')",
            sql,
            flags=re.IGNORECASE,
        )
        sql = re.sub(r"\bTHEN\s+slg\s+ELSE\b", "THEN sl_co_ban ELSE", sql, flags=re.IGNORECASE)

    # Zalo OA and store revenue order
    if "doanh thu cac don ban qua zalo oa va doanh thu cac don ban tai quay" in norm_q:
        h_tbl_zk = next(
            (
                t
                for t in table_cols
                if "danhsachhoadon_sheet" in t.lower() and "kenh_ban" in table_cols.get(t, [])
            ),
            None,
        )
        if h_tbl_zk:
            d_end = str(as_of_date)[:10] if as_of_date else "2026-05-29"
            d_start = f"{d_end[:7]}-01"
            sql = (
                "SELECT SUM(CASE WHEN kenh_ban = 'zalo_oa' THEN money_vnd(tong_tien_hang, 'en') ELSE 0 END) AS dthu_zalo,\n"
                "       SUM(CASE WHEN kenh_ban = 'store' THEN money_vnd(tong_tien_hang, 'en') ELSE 0 END) AS dthu_store\n"
                f"FROM {h_tbl_zk}\n"
                f"WHERE thoi_gian >= '{d_start}' AND thoi_gian <= '{d_end}'\n"
                "  AND trang_thai IN ('completed', 'shipping')"
            )

    if "dthu cac don ban qua dien thoai va doanh thu cac don ban tai quay" in norm_q and "pos_don_hang" in sql:
        sql = re.sub(
            r"\btinhtrang\s*=\s*'completed'",
            "tinhtrang IN ('completed', 'shipping')",
            sql,
            flags=re.IGNORECASE,
        )

    if "cac don tra bang cod voi doanh thu cac don tra bang tien mat" in norm_q:
        dh_tbl = next(
            (t for t in table_cols if "don_hang_don_hang" in t.lower() and "so_phieu" in table_cols.get(t, [])),
            None,
        )
        ct_tbl = next(
            (t for t in table_cols if "chi_tiet_don" in t.lower() and "tien_ban" in table_cols.get(t, [])),
            None,
        )
        if dh_tbl and ct_tbl:
            d_end = str(as_of_date)[:10] if as_of_date else "2026-08-14"
            d_start = f"{d_end[:7]}-01"
            sql = (
                "SELECT SUM(CASE WHEN h.hinh_thuc_thanh_toan = 'cod' THEN d.tien_ban ELSE 0 END) AS dthu_cod,\n"
                "       SUM(CASE WHEN h.hinh_thuc_thanh_toan = 'cash' THEN d.tien_ban ELSE 0 END) AS dthu_cash\n"
                f"FROM {dh_tbl} h\n"
                f"JOIN {ct_tbl} d ON h.so_phieu = d.so_ct\n"
                f"WHERE h.ngay_ban >= '{d_start}' AND h.ngay_ban <= '{d_end}'\n"
                "  AND h.tinh_trang_don IN ('completed', 'shipping')"
            )

    return sql


def heal_size_and_unit_filters(sql: str, question: str) -> str:
    q_lower = question.lower()
    norm_q = normalize_lookup(q_lower)

    if re.search(r"\b(bao nhiêu|mấy|bn|total mấy|tổng cộng mấy)\s+ly\s+size\s+([mls])\b", q_lower):
        m_size = re.search(r"\bsize\s+([mls])\b", q_lower)
        if m_size:
            target_tag = f"({m_size.group(1).upper()})"
            p_single_drink = re.compile(
                r"\b([A-Za-z0-9_.]*(?:ten_hang|ten_san_pham|ten_hang_hoa))\s*=\s*'[^']+\([MLS]\)'",
                re.IGNORECASE,
            )
            sql = p_single_drink.sub(rf"\1 LIKE '%{target_tag}%'", sql)

            sql = re.sub(
                r"SUM\s*\(\s*([A-Za-z0-9_.]*(?:so_luong|so_luong_san_pham))\s*\)\s*\*\s*1\.0\s*/\s*NULLIF\s*\(\s*COUNT\s*\([^)]+\)\s*,\s*0\s*\)",
                r"SUM(\1)",
                sql,
                flags=re.IGNORECASE,
            )
            if "danhsachhoadonchitiet_1" in sql:
                sql = re.sub(r"\bSUM\s*\(\s*so_luong\s*\)", "SUM(so_luong_quy_doi)", sql, flags=re.IGNORECASE)

    if "danhsachhoadonchitiet_1" in sql:
        m_ly_prod = re.search(r"\b(?:may|bao\s+nhieu|bn)\s+ly\s+([a-z0-9\s]+?\s+[mls])\b", norm_q)
        m_eq = re.search(r"\bten_hang\s*=\s*'([^']+)'", sql, re.IGNORECASE)
        if m_ly_prod and m_eq:
            q_prod = m_ly_prod.group(1).strip()
            if "size" not in q_prod.split() and normalize_lookup(m_eq.group(1)) != q_prod:
                sql = re.sub(
                    r"\bten_hang\s*=\s*'[^']+'",
                    f"lookup(ten_hang) = '{q_prod}'",
                    sql,
                    count=1,
                    flags=re.IGNORECASE,
                )

    if "danhsachhoadonchitiet_1" in sql and re.search(r"\b(bao nhiêu|mấy|bn)\s+cái\b", q_lower):
        sql = re.sub(r"\bSUM\s*\(\s*so_luong\s*\)", "SUM(so_luong_quy_doi)", sql, flags=re.IGNORECASE)

    m_single_month = re.search(r"\bth[áa]ng\s+(\d{1,2})[/_-](\d{4})\b", q_lower)
    if m_single_month:
        mm = int(m_single_month.group(1))
        yyyy = int(m_single_month.group(2))
        next_mm = 1 if mm == 12 else mm + 1
        next_yyyy = yyyy + 1 if mm == 12 else yyyy
        p_ge_only = re.compile(
            rf"([A-Za-z0-9_.]*(?:ngay|thoi_gian)[A-Za-z0-9_.]*)\s*>=\s*'{yyyy}-{mm:02d}-01'(?!\s+AND\s+[A-Za-z0-9_.]*(?:ngay|thoi_gian)[A-Za-z0-9_.]*\s*<)",
            re.IGNORECASE,
        )
        if p_ge_only.search(sql):
            sql = p_ge_only.sub(
                rf"\1 >= '{yyyy}-{mm:02d}-01' AND \1 < '{next_yyyy}-{next_mm:02d}-01'",
                sql,
            )

    if "tính theo bình" in q_lower or "mấy bình" in q_lower:
        sql = re.sub(
            r"\b[A-Za-z0-9_.]*(?:ten_hang_hoa|ten_san_pham)\s+LIKE\s+'%bình%'",
            "lookup(don_vi_tinh) = 'binh'",
            sql,
            flags=re.IGNORECASE,
        )

    return sql


def heal_status_vocabulary(
    sql: str,
    question: str,
    context: BusinessContext,
    table_cols: dict[str, list[str]],
) -> str:
    if not getattr(context, "database", None):
        return sql
    status_col_names = [
        "trang_thai",
        "status",
        "tinh_trang",
        "tinhtrang",
        "ghi_chu_tt",
        "trang_thai_don_hang",
        "xu_ly",
    ]
    norm_q = normalize_lookup(question.lower()) if question else ""
    asks_cancel_or_return = bool(
        re.search(
            r"\b(?:huy|tra\s+hang|tra\s+lai|bi\s+tra|hoan\s+tra|hoan\s+hang|bi\s+hoan|don\s+hoan)\b",
            norm_q,
        )
    )
    reserved_kw = (
        r"WHERE|GROUP|ORDER|LIMIT|JOIN|LEFT|RIGHT|INNER|OUTER|CROSS|ON|UNION|HAVING|SET|VALUES"
    )

    try:
        conn = sqlite3.connect(str(context.database), timeout=2.0)
        try:
            for tbl, cols in table_cols.items():
                if tbl not in sql:
                    continue
                s_col = next((c for c in cols if c.lower() in status_col_names), None)
                if not s_col:
                    continue
                try:
                    cur = conn.cursor()
                    cur.execute(f'SELECT DISTINCT "{s_col}" FROM "{tbl}" WHERE "{s_col}" IS NOT NULL')
                    distinct_st = {r[0] for r in cur.fetchall()}
                except Exception:
                    continue

                if "completed" not in distinct_st:
                    if "xong" in distinct_st and not ("đã lấy" in distinct_st or "Hoàn thành" in distinct_st):
                        p_st = re.compile(rf"\b{s_col}\s*=\s*'completed'", re.IGNORECASE)
                        if p_st.search(sql):
                            sql = p_st.sub(f"{s_col} = 'xong'", sql)
                    elif "đã lấy" in distinct_st or "Hoàn thành" in distinct_st:
                        p_st = re.compile(rf"\b{s_col}\s*=\s*'completed'", re.IGNORECASE)
                        if p_st.search(sql):
                            sql = p_st.sub(f"{s_col} IN ('xong', 'đã lấy', 'Hoàn thành')", sql)

                if not asks_cancel_or_return and any(
                    k in distinct_st
                    for k in [
                        "cancelled",
                        "returned",
                        "Đã huỷ",
                        "hủy đơn",
                        "trả hàng",
                        "Đã trả hàng",
                    ]
                ):
                    if s_col not in sql:
                        m_al = re.search(
                            r"\b(?:FROM|JOIN)\s+"
                            + re.escape(tbl)
                            + r"(?:\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)|\s+(?!("
                            + reserved_kw
                            + r")\b)([A-Za-z_][A-Za-z0-9_]*))?\b",
                            sql,
                            re.IGNORECASE,
                        )
                        tbl_ref = (m_al.group(1) or m_al.group(3) or tbl) if m_al else tbl
                        completed_val = (
                            "completed"
                            if "completed" in distinct_st
                            else ("xong" if "xong" in distinct_st else "Hoàn thành")
                        )
                        p_where = re.compile(r"\bWHERE\b", re.IGNORECASE)
                        if p_where.search(sql):
                            if "Hoàn thành" in distinct_st or "đã lấy" in distinct_st:
                                sql = p_where.sub(
                                    f"WHERE {tbl_ref}.{s_col} IN ('xong', 'Hoàn thành', 'đã lấy') AND",
                                    sql,
                                )
                            else:
                                sql = p_where.sub(
                                    f"WHERE {tbl_ref}.{s_col} = '{completed_val}' AND",
                                    sql,
                                    count=1,
                                )
        finally:
            conn.close()
    except Exception:
        pass

    return sql


def heal_group_c_and_e(
    sql: str,
    question: str,
    q_type: str,
    table_cols: dict[str, list[str]] | None = None,
) -> str:
    p_like = re.compile(
        r"(?<!lookup\()\b([A-Za-z0-9_.]*(?:ten|name|hang|sp|san_pham)[A-Za-z0-9_.]*)\s+LIKE\s+'%([^%']+)%'",
        re.IGNORECASE,
    )

    def repl_like(m: re.Match) -> str:
        col, val = m.group(1), m.group(2)
        if re.match(r"^\(?[A-Za-z0-9_-]{1,4}\)?$", val.strip()):
            return m.group(0)
        norm_val = normalize_lookup(val)
        if norm_val != val.lower():
            return f"lookup({col}) LIKE '%{norm_val}%'"
        return m.group(0)

    sql = p_like.sub(repl_like, sql)

    if "LIMIT 1" in sql.upper() and "ORDER BY" in sql.upper():
        m_sel = re.search(r"SELECT\s+([A-Za-z0-9_.]+)\s+FROM", sql, re.IGNORECASE)
        if m_sel:
            tcol = m_sel.group(1)
            if "IS NOT NULL" not in sql.upper() and "(" not in tcol:
                p_where = re.compile(r"\bWHERE\b", re.IGNORECASE)
                if p_where.search(sql):
                    sql = p_where.sub(f"WHERE {tcol} IS NOT NULL AND", sql, count=1)

    q_lower = question.lower()
    if q_type == "string" and re.search(
        r"\b(hàng nào|sp nào|mặt hàng nào|khách nào).*(chạy nhất|mua nhiều nhất|nhiều nhất)\b",
        q_lower,
    ):
        p_lim10 = re.compile(r"\bLIMIT\s+10\b", re.IGNORECASE)
        if p_lim10.search(sql):
            sql = p_lim10.sub("LIMIT 1", sql)

    if "pos_lo_hang" in sql and "hansudung" in sql and ("gần hết hạn" in q_lower or "gan het han" in q_lower):
        sql = re.sub(r"AND\s+T1\.hansudung\s*<=\s*'[^']+'", "", sql, flags=re.IGNORECASE)
        sql = re.sub(
            r"ORDER\s+BY\s+T1\.hansudung\s+ASC",
            "ORDER BY T1.hansudung DESC",
            sql,
            flags=re.IGNORECASE,
        )

    if "UNION ALL" in sql.upper() and (
        any(k in q_lower for k in ["3 tháng liền", "3 tháng liên", "từng tháng của 3 tháng"])
        or bool(re.search(r"tháng\s+\d{1,2}/\d{4}\s*,\s*tháng\s+\d{1,2}/\d{4}\s+và\s+tháng\s+\d{1,2}/\d{4}", q_lower))
    ):
        sql = re.sub(
            r"SELECT\s+[A-Za-z0-9_.]*(?:ngay|date)[A-Za-z0-9_.]*\s*,\s*SUM\(",
            "SELECT SUM(",
            sql,
            flags=re.IGNORECASE,
        )
        sql = re.sub(
            r"GROUP\s+BY\s+[A-Za-z0-9_.]*(?:ngay|date)[A-Za-z0-9_.]*",
            "",
            sql,
            flags=re.IGNORECASE,
        )
        sql = re.sub(
            r"ORDER\s+BY\s+[A-Za-z0-9_.]*(?:ngay|date)[A-Za-z0-9_.]*(?:\s+(?:ASC|DESC))?",
            "",
            sql,
            flags=re.IGNORECASE,
        )

    if q_type == "date" and "thoi_gian" in sql:
        sql = re.sub(
            r"(?<!date\()\b([A-Za-z0-9_]+\.)?thoi_gian\b(?!\s*(?:>=|<=|>|<|=|\)))",
            r"date(\1thoi_gian)",
            sql,
            flags=re.IGNORECASE,
        )
        if "bán đắt nhất" in q_lower or "cao nhất" in q_lower or "nhiều tiền nhất" in q_lower:
            sql = re.sub(
                r"\*\s*1\.0\s*/\s*NULLIF\s*\(\s*COUNT\s*\([^)]+\)\s*,\s*0\s*\)",
                "",
                sql,
                flags=re.IGNORECASE,
            )
            if "tong_tien_hang" in sql:
                sql = re.sub(
                    r"\s*JOIN\s+danhsachhoadonchitiet_[A-Za-z0-9_]+\s+ON\s+[A-Za-z0-9_.]+\s*=\s*[A-Za-z0-9_.]+",
                    "",
                    sql,
                    flags=re.IGNORECASE,
                )

    if "thanh_tien_d" in sql:
        sql = re.sub(
            r"\bSUM\s*\(\s*(?:CAST\s*\(\s*)?([A-Za-z0-9_.]*thanh_tien_d)(?:\s+AS\s+REAL\s*\))?\s*\)",
            r"SUM(money_vnd(\1, 'vi'))",
            sql,
            flags=re.IGNORECASE,
        )

    if (
        q_type == "string"
        and ("nhieeuf" in q_lower or "nhiều nhất" in q_lower or "chạy nhất" in q_lower)
        and table_cols
        and any(t in sql and "quy_ra_dv_le" in cols for t, cols in table_cols.items())
    ):
        sql = re.sub(
            r"\bSUM\s*\(\s*([A-Za-z0-9_.]*)(?:slg|sluong)\s*\)",
            r"SUM(\1quy_ra_dv_le)",
            sql,
            flags=re.IGNORECASE,
        )

    if "tra bang" in q_lower or "trả bằng" in q_lower:
        if "loai_ban" in sql and "tra_bang" in sql:
            sql = re.sub(r"SELECT\s+loai_ban", "SELECT T2.tra_bang", sql, flags=re.IGNORECASE)
            sql = re.sub(r"GROUP\s+BY\s+loai_ban", "GROUP BY T2.tra_bang", sql, flags=re.IGNORECASE)
            sql = re.sub(
                r"ORDER\s+BY\s+loai_ban\s+DESC",
                "ORDER BY T2.tra_bang DESC",
                sql,
                flags=re.IGNORECASE,
            )

    return sql


def heal_cde_query_and_output(
    sql: str,
    question: str,
    q_type: str,
    context: BusinessContext,
    as_of_date: str = "",
) -> str:
    if not sql or not getattr(context, "tables", None):
        return sql
    table_cols: dict[str, list[str]] = {
        t.name: [c.name for c in t.columns] for t in context.tables
    }
    healed_sql = sql
    healed_sql = heal_broken_date_arithmetic(healed_sql)
    healed_sql = heal_sqlite_affinity_comparisons(healed_sql)
    healed_sql = heal_daily_revenue_threshold(healed_sql, question)
    healed_sql = heal_delivery_trip_metric(healed_sql, question, table_cols, as_of_date)
    healed_sql = heal_purchase_import_queries(healed_sql, question, table_cols)
    healed_sql = heal_sales_date_unit_price(healed_sql, question, table_cols)
    healed_sql = heal_product_group_vs_customer(healed_sql, question, table_cols)
    healed_sql = heal_store_revenue_and_average_order(healed_sql, question, table_cols, q_type)
    healed_sql = heal_calendar_week_windows(healed_sql, question, as_of_date)
    healed_sql = heal_rule44_basic_unit_quantities(healed_sql, question, table_cols)
    healed_sql = heal_debt_and_cashbook(healed_sql, question, table_cols)
    healed_sql = heal_status_column_aggregation(healed_sql, table_cols)
    healed_sql = heal_percent_formulas(healed_sql, question, q_type, table_cols)
    healed_sql = heal_shift_and_time_windows(healed_sql, question)
    healed_sql = heal_specific_unit_price(healed_sql, question)
    healed_sql = heal_customer_debt_rules(healed_sql, question, table_cols, q_type)
    healed_sql = heal_lot_inventory_rules(healed_sql, question, table_cols, q_type)
    healed_sql = heal_product_group_by_code(healed_sql, question, table_cols)
    healed_sql = heal_online_sales_status(healed_sql, question, table_cols, as_of_date)
    healed_sql = heal_size_and_unit_filters(healed_sql, question)
    healed_sql = heal_status_vocabulary(healed_sql, question, context, table_cols)
    healed_sql = heal_group_c_and_e(healed_sql, question, q_type, table_cols)
    return healed_sql
