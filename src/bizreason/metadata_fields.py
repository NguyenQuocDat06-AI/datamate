"""Resolve redundant plan-field annotations against selected physical schemas."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from bizreason.contracts import BusinessContext
from bizreason.data.parsing import parse_status
from bizreason.data.schema import DATE_ROLES
from bizreason.sql_parameters import table_aliases
from bizreason.utils import normalize_lookup

CANONICAL_STATUSES = {"completed", "shipping", "cancelled", "returned", "received"}


def _find_unquoted_question_marks(sql: str) -> list[int]:
    offsets = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif c == "-" and i + 1 < n and sql[i + 1] == "-":
            i += 2
            while i < n and sql[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and sql[i + 1] == "*":
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2
        elif c == "?":
            offsets.append(i)
            i += 1
        else:
            i += 1
    return offsets


def _sync_sql_status_literals_and_params(
    sql_text: str, params: list[Any] | None, status_cols: set[str]
) -> tuple[str, list[Any], bool]:
    if not status_cols or not sql_text:
        return sql_text, list(params) if isinstance(params, list) else [], False

    escaped_cols = "|".join(re.escape(c) for c in sorted(status_cols, key=len, reverse=True))
    lhs = (
        r"(?:(?:lookup|lower|upper|trim)\s*\(\s*)*"
        r"(?:[\"`\[]?[A-Za-z_]\w*[\"`\]]?\s*\.\s*)?"
        r"[\"`\[]?(?<![A-Za-z0-9_])(?:" + escaped_cols + r")(?![A-Za-z0-9_])[\"`\]]?"
        r"(?:\s*\)){0,2}"
    )
    in_rhs = r"\((?:[^()']|'(?:''|[^'])*'|(?:\([^()]*\)))*\)"
    scalar_rhs = (
        r"(?:(?:(?:lookup|lower|upper|trim)\s*\(\s*)+(?:'(?:''|[^'])*'|\?)(?:\s*\))+"
        r"|'(?:''|[^'])*'|\?)"
    )
    comp_pattern = re.compile(
        rf"({lhs})\s*(?:(\bnot\s+in\b|\bin\b)\s*({in_rhs})|(=|!=|<>)\s*({scalar_rhs}))",
        re.IGNORECASE,
    )

    matches = list(comp_pattern.finditer(sql_text))
    if not matches:
        return sql_text, list(params) if isinstance(params, list) else [], False

    new_params = list(params) if isinstance(params, list) else []
    q_offsets = _find_unquoted_question_marks(sql_text)
    offset_to_param_idx = {offset: idx for idx, offset in enumerate(q_offsets)}

    new_sql = sql_text
    changed = False

    for m in reversed(matches):
        if m.group(3) is not None:
            rhs_start, rhs_end = m.span(3)
        else:
            rhs_start, rhs_end = m.span(5)
        rhs_str = sql_text[rhs_start:rhs_end]

        # 1a. Rewrite single-quoted string literals '...' inside rhs_str
        lit_changed = False
        pieces = []
        last_end = 0
        for lit_m in re.finditer(r"'((?:''|[^'])*)'", rhs_str):
            s, e = lit_m.span()
            pieces.append(rhs_str[last_end:s])
            raw_escaped = lit_m.group(1)
            raw_lit = raw_escaped.replace("''", "'")
            res = parse_status(raw_lit)
            parsed = res.value if hasattr(res, "value") else res
            if parsed in CANONICAL_STATUSES and raw_lit != parsed:
                pieces.append(f"'{parsed}'")
                lit_changed = True
            else:
                pieces.append(rhs_str[s:e])
            last_end = e
        pieces.append(rhs_str[last_end:])
        new_rhs_str = "".join(pieces)

        if lit_changed:
            new_sql = new_sql[:rhs_start] + new_rhs_str + new_sql[rhs_end:]
            changed = True

        # 1b. Rewrite ? bound parameters inside rhs_str
        if new_params:
            i = 0
            n = len(rhs_str)
            q_positions = []
            while i < n:
                c = rhs_str[i]
                if c == "'":
                    i += 1
                    while i < n:
                        if rhs_str[i] == "'":
                            if i + 1 < n and rhs_str[i + 1] == "'":
                                i += 2
                                continue
                            i += 1
                            break
                        i += 1
                elif c == "?":
                    q_positions.append(i)
                    i += 1
                else:
                    i += 1

            for q_pos in q_positions:
                char_offset = rhs_start + q_pos
                param_idx = offset_to_param_idx.get(char_offset)
                if param_idx is not None and 0 <= param_idx < len(new_params):
                    val = new_params[param_idx]
                    if isinstance(val, str):
                        res = parse_status(val)
                        parsed = res.value if hasattr(res, "value") else res
                        if parsed in CANONICAL_STATUSES and val != parsed:
                            new_params[param_idx] = parsed
                            changed = True

    return new_sql, new_params, changed


def _find_unique_norm_col(tbl, col_name: str):
    norm = normalize_lookup(col_name)
    matches = [
        c for c in tbl.columns
        if normalize_lookup(c.name) == norm or normalize_lookup(c.original_name) == norm
    ]
    return matches[0] if len(matches) == 1 else None


def canonical_metadata_fields(response: dict, context: BusinessContext) -> dict:
    selected = set(response.get("tables", []))
    aliases = table_aliases(response.get("sql") or "", selected)
    changes = []

    def resolve(field, allow_multi=False):
        if not isinstance(field, str):
            return field
        prefix, separator, column = field.rpartition(".")
        if not separator:
            prefix, column = "", field
        if prefix and (tbl := context.table(prefix)):
            if tbl.column(column):
                return field
            if tbl.name in selected:
                if (role_col := tbl.role(column)) is not None:
                    canonical = f"{tbl.name}.{role_col}"
                    changes.append(f"metadata_field:{field}->{canonical}")
                    return canonical
                if (norm_col := _find_unique_norm_col(tbl, column)) is not None:
                    canonical = f"{tbl.name}.{norm_col.name}"
                    changes.append(f"metadata_field:{field}->{canonical}")
                    return canonical
            return field  # Validator handles missing fields or out-of-scope tables.
        candidates = []
        for table in context.tables:
            if table.name not in selected:
                continue
            matched_col = table.column(column) or _find_unique_norm_col(table, column)
            if not matched_col:
                continue
            if prefix:
                sources = {normalize_lookup(PurePosixPath(s.path).stem).replace(" ", "") for s in table.sources}
                source_alias = normalize_lookup(prefix).replace(" ", "") in sources
                if table.name not in aliases.get(prefix.casefold(), set()) and not source_alias:
                    continue
            candidates.append((table, matched_col))
        if not candidates:
            if not prefix:
                role_tables = [
                    (t, r_col)
                    for t in context.tables
                    if t.name in selected and (r_col := t.role(column)) is not None
                ]
                if role_tables:
                    first_rcol = role_tables[0][1]
                    if all(rc[1] == first_rcol for rc in role_tables):
                        signatures = {(t.grain, tuple((c.original_name, c.semantic_role) for c in t.columns)) for t, _ in role_tables}
                        if len(signatures) == 1:
                            canonical = f"{role_tables[0][0].name}.{first_rcol}"
                            changes.append(f"metadata_field:{field}->{canonical}; equivalent_physical_sources={len(role_tables)}")
                            return canonical
            return field
        first_col_name = candidates[0][1].name
        if not all(c[1].name == first_col_name for c in candidates):
            if allow_multi and not prefix and len(candidates) > 1:
                expanded = [f"{t.name}.{col.name}" for t, col in candidates]
                changes.append(f"metadata_field:{field}->expanded; grains={len(candidates)}")
                return expanded
            return field
        signatures = {(t.grain, tuple((c.original_name, c.semantic_role) for c in t.columns)) for t, _ in candidates}
        if len(signatures) != 1:
            if allow_multi and not prefix and len(candidates) > 1:
                expanded = [f"{t.name}.{col.name}" for t, col in candidates]
                changes.append(f"metadata_field:{field}->expanded; grains={len(candidates)}")
                return expanded
            return field  # Same column name across different relations is ambiguous.
        canonical = f"{candidates[0][0].name}.{first_col_name}"
        changes.append(f"metadata_field:{field}->{canonical}; equivalent_physical_sources={len(candidates)}")
        return canonical

    if isinstance(response.get("required_fields"), list):
        resolved_req = []
        for field in response["required_fields"]:
            res = resolve(field, allow_multi=True)
            if isinstance(res, list):
                for r in res:
                    if r not in resolved_req:
                        resolved_req.append(r)
            else:
                resolved_req.append(res)
        response["required_fields"] = resolved_req

    if isinstance(response.get("group_by"), list):
        response["group_by"] = [resolve(field) for field in response["group_by"]]
    canonical_statuses = {"completed", "shipping", "cancelled", "returned", "received"}
    for item in response.get("filters", []):
        if isinstance(item, dict) and "field" in item:
            item["field"] = resolve(item["field"])
            fld = item["field"]
            if isinstance(fld, str) and "." in fld:
                tbl_name, col_name = fld.rsplit(".", 1)
                tbl = context.table(tbl_name)
                col = tbl.column(col_name) if tbl else None
                if col and col.semantic_role == "status" and isinstance(item.get("values"), list):
                    new_vals = []
                    for v in item["values"]:
                        if isinstance(v, str):
                            res = parse_status(v)
                            parsed = res.value if hasattr(res, "value") else res
                            if parsed in canonical_statuses:
                                new_vals.append(parsed)
                            else:
                                new_vals.append(v)
                        else:
                            new_vals.append(v)
                    item["values"] = new_vals
    for window in response.get("windows", []):
        if not isinstance(window, dict):
            continue
        if window.get("field"):
            window["field"] = resolve(window["field"])
        else:
            dated = []
            selected_date_roles = {column.semantic_role for table in context.tables if table.name in selected
                                   for column in table.columns if column.semantic_role in DATE_ROLES}
            for field in response.get("required_fields", []):
                table_name, _, column_name = str(field).rpartition(".")
                table = context.table(table_name)
                column = table.column(column_name) if table else None
                if table and table.name in selected and column and column.semantic_role in DATE_ROLES:
                    dated.append((field, column.semantic_role, table))
            if dated and len({role for _, role, _ in dated}) == 1 and selected_date_roles == {dated[0][1]}:
                signatures = {(t.grain, tuple((c.original_name, c.semantic_role) for c in t.columns)) for _, _, t in dated}
                if len(signatures) == 1:
                    window["field"] = dated[0][0]
                    changes.append("time_field_recovered_from_unambiguous_required_fields:" + dated[0][0])
            if not window.get("field"):
                sig_to_tables: dict[tuple, list] = {}
                for t in context.tables:
                    sig = (t.grain, tuple((c.original_name, c.semantic_role) for c in t.columns))
                    sig_to_tables.setdefault(sig, []).append(t)
                partitioned_sigs = {sig for sig, tbls in sig_to_tables.items() if len(tbls) > 1}
                part_selected = [
                    t for t in context.tables
                    if t.name in selected and (t.grain, tuple((c.original_name, c.semantic_role) for c in t.columns)) in partitioned_sigs
                ]
                if part_selected:
                    pref_col = None
                    part_table = part_selected[0]
                    if part_table.role("ordered_at"):
                        pref_col = part_table.role("ordered_at")
                    else:
                        date_cols = [c.name for c in part_table.columns if c.semantic_role in DATE_ROLES]
                        if len(date_cols) == 1:
                            pref_col = date_cols[0]
                    if pref_col:
                        recovered_field = f"{part_table.name}.{pref_col}"
                        window["field"] = recovered_field
                        changes.append("time_field_recovered_from_partitioned_table:" + recovered_field)
    if (
        response.get("metric") == "generic_sql"
        and isinstance(response.get("sql"), str)
        and bool(response["sql"].strip())
    ):
        def _is_valid_selected_col(f: Any) -> bool:
            return (
                isinstance(f, str)
                and "." in f
                and (tbl := context.table(f.rsplit(".", 1)[0])) is not None
                and tbl.name in selected
                and tbl.column(f.rsplit(".", 1)[1]) is not None
            )

        old_group_by = response.get("group_by", [])
        old_filters = response.get("filters", [])
        new_group_by = [g for g in old_group_by if _is_valid_selected_col(g)]
        valid_filters = [
            it for it in old_filters
            if isinstance(it, dict) and _is_valid_selected_col(it.get("field"))
        ]
        if len(new_group_by) != len(old_group_by) or len(valid_filters) != len(old_filters):
            changes.append("pruned_decorative_expression_metadata")

        pruned_status_filter = False
        final_filters = []
        for it in valid_filters:
            fld = it.get("field")
            tbl_name, col_name = fld.rsplit(".", 1)
            tbl = context.table(tbl_name)
            col = tbl.column(col_name) if tbl else None
            op = it.get("operator")
            if col and col.semantic_role == "status" and op in {"eq", "ne", "in", "not_in"}:
                vals = it.get("values", [])
                if not isinstance(vals, list) or any(v not in canonical_statuses for v in vals):
                    pruned_status_filter = True
                    continue
            final_filters.append(it)
        if pruned_status_filter:
            changes.append("pruned_non_canonical_decorative_status_filter")

        response["group_by"] = new_group_by
        response["filters"] = final_filters

    target_tables = [t for t in context.tables if t.name in selected] if selected else context.tables
    if not target_tables:
        target_tables = context.tables

    col_roles: dict[str, set[str | None]] = {}
    for t in target_tables:
        for c in t.columns:
            col_roles.setdefault(c.name.lower(), set()).add(c.semantic_role)
            if c.original_name:
                col_roles.setdefault(c.original_name.lower(), set()).add(c.semantic_role)

    unambiguous_status_cols = {
        c_name for c_name, roles in col_roles.items()
        if roles == {"status"}
    }

    if (
        unambiguous_status_cols
        and isinstance(response.get("sql"), str)
        and bool(response["sql"].strip())
    ):
        new_sql, new_params, sync_changed = _sync_sql_status_literals_and_params(
            response["sql"], response.get("parameters"), unambiguous_status_cols
        )
        response["sql"] = new_sql
        if isinstance(response.get("parameters"), list):
            response["parameters"] = new_params
        if sync_changed:
            changes.append("canonicalized_sql_status_literals")

    if changes:
        response["derivation_notes"] = [*response.get("derivation_notes", []), *dict.fromkeys(changes)]
    return response
