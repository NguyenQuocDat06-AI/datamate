"""Propagate released evidence of a return to every row of the same order."""

from __future__ import annotations

import sqlite3

from bizreason.contracts import TableMeta
from bizreason.utils import normalize_lookup, quote_identifier


def propagate_order_returns(connection: sqlite3.Connection, tables: list[TableMeta], readme: str) -> None:
    q = quote_identifier
    negative_means_return = "so luong am" in normalize_lookup(readme)
    returned = set()
    for table in tables:
        order = table.role("order_id")
        if not order:
            continue
        conditions = []
        if table.role("status"):
            conditions.append(f"{q(table.role('status'))}='returned'")
        if table.role("qty_returned"):
            conditions.append(f"CAST({q(table.role('qty_returned'))} AS REAL)>0")
        if negative_means_return and table.role("qty_sale"):
            conditions.append(f"CAST({q(table.role('qty_sale'))} AS REAL)<0")
        if not conditions:
            continue
        scope = q(table.role("scope")) if table.role("scope") else "NULL"
        returned.update(connection.execute(
            f"SELECT DISTINCT {scope},{q(order)} FROM {q(table.name)} WHERE {q(order)} IS NOT NULL AND ({' OR '.join(conditions)})"))
    for table in tables:
        order, status = table.role("order_id"), table.role("status")
        if not order or not status:
            continue
        scope = table.role("scope")
        if scope:
            connection.executemany(
                f"UPDATE {q(table.name)} SET {q(status)}='returned' WHERE {q(scope)}=? AND {q(order)}=? AND {q(status)}<>'cancelled'",
                [(s, o) for s, o in returned if s is not None],
            )
        else:
            connection.executemany(
                f"UPDATE {q(table.name)} SET {q(status)}='returned' WHERE {q(order)}=? AND {q(status)}<>'cancelled'",
                [(o,) for s, o in returned if s is None],
            )
