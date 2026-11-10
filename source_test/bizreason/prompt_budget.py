"""Lossless join references and explicitly bounded observation previews."""

from __future__ import annotations

import copy

from bizreason.contracts import JoinEdge
from bizreason.utils import digest, dumps


def join_reference(edge: JoinEdge) -> str:
    return digest(edge)[:16]


def compact_joins(edges: list[JoinEdge]) -> list[dict]:
    return [{"join_id": join_reference(edge), **edge.model_dump(mode="json", exclude={"evidence"})}
            for edge in edges]


def bounded_observations(observations: list[dict], *, characters_per_probe: int = 2000) -> list[dict]:
    result = []
    for original in observations:
        row = copy.deepcopy(original)
        preview = row.pop("preview", [])
        shown, used = [], 0
        for values in preview[:3]:
            size = len(dumps(values))
            if used + size > characters_per_probe:
                break
            shown.append(values)
            used += size
        row.update(preview=shown, preview_rows_omitted=len(preview) - len(shown),
            preview_complete=len(shown) == row.get("row_count", len(preview)),
            preview_note="Bounded preview only; full result hash and row_count retained. Compute answers with SQL on full data.")
        result.append(row)
    return result
