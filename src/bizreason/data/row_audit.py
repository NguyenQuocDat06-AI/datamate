"""Retain the source and decision for rows omitted or ambiguous during ingestion.

Ordinary records already retain __source_row and __raw_json in their source table.
Only exceptions are duplicated here, keeping the cost proportional to exceptions.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from bizreason.utils import dumps


def create_audit(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS __row_audit ("
        "source_path TEXT NOT NULL, sheet TEXT NOT NULL, region TEXT NOT NULL, "
        "source_row INTEGER NOT NULL, row_class TEXT NOT NULL, kept INTEGER NOT NULL, "
        "reason TEXT NOT NULL, raw_json TEXT NOT NULL, evidence_json TEXT NOT NULL)"
    )


def record_audit(
    connection: sqlite3.Connection, *, source_path: str, sheet: str, region: str,
    source_row: int, row_class: str, kept: bool, reason: str, raw: Any,
    evidence: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        "INSERT INTO __row_audit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source_path, sheet, region, source_row, row_class, int(kept), reason,
         dumps(list(raw)), dumps(evidence or {})),
    )
