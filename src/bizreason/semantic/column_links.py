"""The kept columns a question may need (column_catalog), shown to the parser with how to use them.

The layer keeps every source column (x_<name>, src_<table>) and describes it in column_catalog with its original
header. A column is offered when the content words of its header appear in the question, so a private shop's own
columns ("Thu máy cũ", "Ngày phải trả", "Biển số") become usable without anyone naming them in advance.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from bizreason.utils import normalize_lookup

MAX_COLUMNS = 5
MIN_WORD = 2

INSTRUCTIONS = (
    "This shop keeps more columns than the measures above. If the question is about one of these columns, add to "
    "your JSON: \"column_measure\": {\"op\": \"sum\"|\"count\"|\"count_distinct\", \"column\": <name>} (sum of the "
    "column, count of rows/orders, or count of different values), and/or \"column_filters\": [{\"column\": <name>, "
    "\"op\": \"contains\"|\"not_contains\"|\"eq\"|\"nonempty\"|\"after_today\"|\"before_today\"|"
    "\"on_or_before_today\"|\"on_or_after_today\", \"value\": <text or null>}], keep \"shape\": \"scalar\", and set "
    "\"measure\" to the closest measure (or \"outside\"). Use only column names listed here:"
)


class ColumnIndex:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows

    @classmethod
    def from_connection(cls, connection: sqlite3.Connection) -> "ColumnIndex":
        try:
            raw = connection.execute(
                "SELECT table_name, column_name, source_header, value_type, distinct_count, sample_values "
                "FROM column_catalog WHERE column_name NOT LIKE 'xn\\_%' ESCAPE '\\' "
                "AND column_name NOT LIKE 'xd\\_%' ESCAPE '\\'").fetchall()
        except sqlite3.Error:
            return cls([])
        rows = []
        for table, column, header, kind, distinct, samples in raw:
            words = {w for w in normalize_lookup(str(header or column)).split() if len(w) >= MIN_WORD}
            try:
                sample = json.loads(samples)[:4] if samples else []
            except (TypeError, ValueError):
                sample = []
            rows.append({"table": table, "column": column, "header": header, "type": kind, "words": words,
                         "distinct": distinct, "samples": sample})
        return cls(rows)

    def block(self, question: str) -> str:
        asked = set(normalize_lookup(question).split())
        scored = []
        for row in self.rows:
            words = row["words"]
            hit = words & asked
            # every header word, or at least two of them, must be in the question
            if words and (hit == words or len(hit) >= 2):
                scored.append((len(hit), row))
        if not scored:
            return ""
        scored.sort(key=lambda item: -item[0])
        lines = [f"- {r['column']} (table {r['table']}; header {r['header']!r}; {r['type']}; "
                 f"examples {json.dumps(r['samples'], ensure_ascii=False)})" for _, r in scored[:MAX_COLUMNS]]
        return INSTRUCTIONS + "\n" + "\n".join(lines)
