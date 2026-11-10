"""Compact physical schemas without hiding partitions, units, or parsing problems."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from bizreason.contracts import Diagnostic, TableMeta
from bizreason.utils import dumps


def compact_diagnostics(diagnostics: list[Diagnostic]) -> list[dict[str, Any]]:
    """Keep every condition/file/table; aggregate repeated per-row occurrences."""
    grouped = {}
    for item in diagnostics:
        source = item.source
        identity = (item.code, item.severity, item.message,
                    source.path if source else "", source.table if source else "")
        row = grouped.setdefault(identity, {
            "code": item.code, "severity": item.severity, "message": item.message,
            "path": source.path if source else "", "table": source.table if source else "",
            "occurrences": 0, "source_samples": [],
        })
        row["occurrences"] += 1
        if source and len(row["source_samples"]) < 3:
            row["source_samples"].append(source.model_dump(mode="json"))
    return list(grouped.values())


def compact_catalog(tables: list[TableMeta], *, samples: bool = True) -> list[dict[str, Any]]:
    groups: dict[tuple, list[TableMeta]] = defaultdict(list)
    for table in tables:
        signature = (
            table.grain,
            tuple(
                (column.name, column.original_name, column.semantic_role, column.unit)
                for column in table.columns
            ),
        )
        groups[signature].append(table)
    result = []
    for group in groups.values():
        columns = []
        for index, first in enumerate(group[0].columns):
            item: dict[str, Any] = {
                "name": first.name,
                "original_name": first.original_name,
                "dtype": "|".join(sorted({table.columns[index].dtype for table in group})),
            }
            if first.semantic_role:
                item["semantic_role"] = first.semantic_role
            if first.unit:
                item["unit"] = first.unit
            missing = sum(table.columns[index].null_count for table in group)
            errors = sum(table.columns[index].parse_errors for table in group)
            if missing:
                item["null_count_across_partitions"] = missing
            if errors:
                item["parse_errors_across_partitions"] = errors
            if samples:
                seen = set()
                examples = []
                for table in group:
                    for value in table.columns[index].examples:
                        identity = dumps(value)
                        if identity not in seen and len(identity) <= 160:
                            seen.add(identity)
                            examples.append(value)
                        if len(examples) == 3:
                            break
                    if len(examples) == 3:
                        break
                item["examples"] = examples
            columns.append(item)
        physical = [
            {
                "name": table.name,
                "rows": table.row_count,
                "coverage": table.coverage,
                "sources": [source.model_dump(mode="json") for source in table.sources],
            }
            for table in group
        ]
        if len(group) == 1:
            result.append({**physical[0], "grain": group[0].grain, "columns": columns})
        else:
            result.append(
                {
                    "physical_tables": physical,
                    "grain": group[0].grain,
                    "shared_columns": columns,
                    "note": "Each physical table has these columns. This is a schema representation, not a UNION or a proof of identical grain/keys.",
                }
            )
    return result
