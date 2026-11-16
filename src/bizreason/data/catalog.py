from __future__ import annotations

import sqlite3

from bizreason.contracts import JoinEdge, TableMeta
from bizreason.utils import quote_identifier


def discover_joins(connection: sqlite3.Connection, tables: list[TableMeta]) -> list[JoinEdge]:
    q = quote_identifier
    result = []
    # Only explicit identity roles; never join approximate names or invented row numbers.
    for role in ["order_id", "product_id", "customer_id", "purchase_id", "supplier_id", "batch_id"]:
        candidates = [(table, table.role(role)) for table in tables if table.role(role)]
        parents = []
        for table, column in candidates:
            key_columns = [column]
            if table.role("scope"):
                key_columns.insert(0, table.role("scope"))
            names = ",".join(q(name) for name in key_columns)
            notnull = " AND ".join(q(name) + " IS NOT NULL" for name in key_columns)
            count, nonnull = connection.execute(
                f"SELECT COUNT(*),SUM(CASE WHEN {notnull} THEN 1 ELSE 0 END) FROM {q(table.name)}"
            ).fetchone()
            distinct = connection.execute(
                f"SELECT COUNT(*) FROM (SELECT {names} FROM {q(table.name)} GROUP BY {names})"
            ).fetchone()[0]
            if count and count == nonnull == distinct:
                parents.append((table, column))
        for left, left_column in candidates:
            for right, right_column in parents:
                if left.name == right.name:
                    continue
                keys = [(left_column, right_column)]
                if left.role("scope") and right.role("scope"):
                    keys.insert(0, (left.role("scope"), right.role("scope")))
                elif left.role("scope") or right.role("scope"):
                    continue
                condition = " AND ".join(f"l.{q(a)}=r.{q(b)}" for a, b in keys)
                orphan = connection.execute(
                    f"SELECT COUNT(*) FROM {q(left.name)} l LEFT JOIN {q(right.name)} r ON {condition} "
                    f"WHERE l.{q(left_column)} IS NOT NULL AND r.{q(right_column)} IS NULL"
                ).fetchone()[0]
                if orphan == 0:
                    result.append(
                        JoinEdge(
                            left=left.name,
                            right=right.name,
                            keys=keys,
                            cardinality="many_to_one",
                            orphan_count=0,
                            evidence=[
                                f"unique_nonnull:{right.name}.{right_column}",
                                f"orphan_check:{left.name}:{right.name}",
                            ],
                        )
                    )
    return result
