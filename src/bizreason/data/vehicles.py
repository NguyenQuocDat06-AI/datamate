"""Separate a vehicle's recorded description from its registration identity."""

from __future__ import annotations

import re
import sqlite3

from bizreason.contracts import ColumnMeta, TableMeta
from bizreason.utils import normalize_lookup, quote_identifier

PLATE = re.compile(r"(?<![A-Z0-9])(\d{2})[ .-]*([A-Z]{1,2}\d?)[ .-]*(\d{3})[ .-]*(\d{2})(?![A-Z0-9])", re.I)


def vehicle_parts(value: object) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    text = str(value).strip()
    matches = list(PLATE.finditer(text))
    if len(matches) != 1 or text[matches[0].end():].strip():
        return None, None  # Ambiguous/malformed identities are never guessed.
    match = matches[0]
    plate = "".join(match.groups()).upper()
    model = normalize_lookup(text[:match.start()]).replace(" ", "")
    return plate, model or None


def derive_vehicle_columns(connection: sqlite3.Connection, tables: list[TableMeta]) -> list[TableMeta]:
    result = []
    q = quote_identifier
    for table in tables:
        source = table.role("vehicle_description")
        if not source or table.role("vehicle_plate"):
            result.append(table)
            continue
        names = {c.name for c in table.columns}
        plate_name, model_name = "vehicle_plate_key", "vehicle_model_key"
        if {plate_name, model_name} & names:
            result.append(table)  # Never overwrite an actual source field.
            continue
        rows = connection.execute(f"SELECT rowid,{q(source)} FROM {q(table.name)}").fetchall()
        parsed = [(rowid, value, *vehicle_parts(value)) for rowid, value in rows]
        for name in (plate_name, model_name):
            connection.execute(f"ALTER TABLE {q(table.name)} ADD COLUMN {q(name)} TEXT")
        connection.executemany(
            f"UPDATE {q(table.name)} SET {q(plate_name)}=?,{q(model_name)}=? WHERE rowid=?",
            [(plate, model, rowid) for rowid, _, plate, model in parsed],
        )
        columns = list(table.columns)
        invalid = sum(value is not None and plate is None for _, value, plate, _ in parsed)
        for name, role, index in ((plate_name, "vehicle_plate", 2), (model_name, "vehicle_model", 3)):
            values = [row[index] for row in parsed]
            columns.append(ColumnMeta(name=name, original_name=name, dtype="TEXT", semantic_role=role,
                examples=list(dict.fromkeys(v for v in values if v is not None))[:3],
                null_count=sum(v is None for v in values), parse_errors=invalid,
                evidence=[f"derived_from:{source}", "registration_identity:v1; description preserved"]))
        result.append(table.model_copy(update={"columns": columns}))
    return result
