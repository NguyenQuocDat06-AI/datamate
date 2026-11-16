from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFC", value).lower()
    return " ".join(
        "".join(char if char.isalnum() or char.isspace() else " " for char in text).split()
    )


def numeric(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def cell_matches(predicted: Any, expected: Any, aliases: list[Any] | None = None) -> bool:
    if isinstance(expected, str):
        return isinstance(predicted, str) and normalize_name(predicted) in {
            normalize_name(item) for item in [expected, *(aliases or [])] if isinstance(item, str)
        }
    if isinstance(expected, bool):
        return type(predicted) is bool and predicted == expected
    left, right = numeric(predicted), numeric(expected)
    return left is not None and right is not None and left == right


def _row_aliases(aliases: list[Any], index: int) -> list[Any]:
    return aliases[index] if index < len(aliases) and isinstance(aliases[index], list) else []


def _table_matches(predicted: Any, expected: Any, aliases: list[Any]) -> bool:
    if not isinstance(predicted, dict) or not isinstance(expected, dict):
        return False
    columns, rows = predicted.get("columns"), predicted.get("rows")
    target_columns, target_rows = expected.get("columns"), expected.get("rows")
    if (
        not isinstance(columns, list)
        or not isinstance(rows, list)
        or not isinstance(target_columns, list)
        or not isinstance(target_rows, list)
        or len(columns) != len(target_columns)
        or not all(isinstance(column, str) for column in columns)
    ):
        return False
    names = [normalize_name(column) for column in columns]
    target_names = [normalize_name(column) for column in target_columns]
    if len(set(names)) == len(names) and set(names) == set(target_names):
        positions = [names.index(name) for name in target_names]
    else:
        positions = list(range(len(columns)))
    reordered = []
    seen = set()
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            return False
        aligned = [row[position] for position in positions]
        signature = tuple(
            ("s", normalize_name(cell)) if isinstance(cell, str) else ("n", str(numeric(cell)))
            for cell in aligned
        )
        if signature not in seen:
            reordered.append(aligned)
            seen.add(signature)
    if len(reordered) != len(target_rows):
        return False
    # Bipartite matching handles aliases and unordered rows without greedy misassignment.
    edges = []
    for row in reordered:
        edges.append(
            [
                index
                for index, target in enumerate(target_rows)
                if len(row) == len(target)
                and all(
                    cell_matches(
                        cell, expected_cell, _row_aliases(aliases, index) if column == 0 else []
                    )
                    for column, (cell, expected_cell) in enumerate(zip(row, target))
                )
            ]
        )
    assigned: dict[int, int] = {}

    def augment(index: int, visited: set[int]) -> bool:
        for target in edges[index]:
            if target in visited:
                continue
            visited.add(target)
            if target not in assigned or augment(assigned[target], visited):
                assigned[target] = index
                return True
        return False

    return all(augment(index, set()) for index in range(len(reordered)))


def matches(prediction: dict[str, Any], reference: dict[str, Any]) -> bool:
    kind = reference.get("type")
    if prediction.get("type") != kind:
        return False
    value, expected = prediction.get("answer"), reference.get("answer")
    aliases = reference.get("alias", [])
    aliases = aliases if isinstance(aliases, list) else []
    if kind == "unanswerable":
        return True
    if kind == "percent":
        left, right = numeric(value), numeric(expected)
        return (
            left is not None
            and right is not None
            and left.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
            == right.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        )
    if kind in {"number", "string", "boolean"}:
        return cell_matches(value, expected, aliases)
    if kind == "date":
        return (
            isinstance(value, str)
            and re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", value) is not None
            and value == expected
        )
    if kind == "list":
        return (
            isinstance(value, list)
            and isinstance(expected, list)
            and len(value) == len(expected)
            and all(
                cell_matches(item, target, _row_aliases(aliases, index))
                for index, (item, target) in enumerate(zip(value, expected))
            )
        )
    if kind == "table":
        return _table_matches(value, expected, aliases)
    return False


def score_records(
    predictions: list[dict[str, Any]],
    references: list[dict[str, Any]],
    questions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    def index_unique(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        indexed = {}
        for record in records:
            if record["id"] in indexed:
                raise ValueError("Duplicate answer ID")
            indexed[record["id"]] = record
        return indexed

    submitted, targets = index_unique(predictions), index_unique(references)
    question_index = index_unique(questions or [])
    details = []
    for identity, reference in targets.items():
        prediction = submitted.get(identity)
        invalid_reason = (
            prediction and "reason" in prediction and not isinstance(prediction["reason"], str)
        )
        if prediction is None or prediction.get("type") == "abstain" or invalid_reason:
            outcome = "abstain"
        else:
            outcome = "correct" if matches(prediction, reference) else "wrong"
        details.append(
            {
                "id": identity,
                "outcome": outcome,
                "type": reference["type"],
                "business_id": question_index.get(identity, {}).get("business_id", "unknown"),
            }
        )

    def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
        counts = {
            name: sum(row["outcome"] == name for row in rows)
            for name in ["correct", "wrong", "abstain"]
        }
        answered = counts["correct"] + counts["wrong"]
        return {
            **counts,
            "n": len(rows),
            "pa": (counts["correct"] - counts["wrong"]) / len(rows) if rows else 0,
            "coverage": answered / len(rows) if rows else 0,
            "answered_accuracy": counts["correct"] / answered if answered else None,
        }

    return {
        **summarize(details),
        "by_type": {
            key: summarize([row for row in details if row["type"] == key])
            for key in sorted({row["type"] for row in details})
        },
        "by_business": {
            key: summarize([row for row in details if row["business_id"] == key])
            for key in sorted({row["business_id"] for row in details})
        },
        "false_positive_unanswerable": sum(
            submitted.get(identity, {}).get("type") == "unanswerable"
            and reference["type"] != "unanswerable"
            for identity, reference in targets.items()
        ),
        "extra_ids": sorted(set(submitted) - set(targets)),
        "details": details,
    }
