from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from bizreason.contracts import AnswerType, OutputSpec, Prediction
from bizreason.utils import dumps, writable_path


def json_number(value: Any, rounding: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("Expected a numeric value, not a numeric string")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Non-finite answers are forbidden")
    if rounding == "integer":
        if number != number.to_integral_value():
            raise ValueError("Count/integer output cannot be fractional")
        return int(number)
    if rounding == "money":
        number = number.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return int(number)
    if rounding == "two_decimals":
        number = number.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        return int(number) if number == number.to_integral_value() else float(number)
    return int(number) if number == number.to_integral_value() else float(number)


def validate_answer(value: Any, spec: OutputSpec) -> Any:
    kind = spec.type
    if kind in {AnswerType.NUMBER, AnswerType.PERCENT}:
        return json_number(value, "two_decimals" if kind == AnswerType.PERCENT else spec.rounding)
    if kind == AnswerType.BOOLEAN:
        if type(value) is not bool:
            raise ValueError("Boolean must be true/false")
        return value
    if kind in {AnswerType.STRING, AnswerType.DATE}:
        if not isinstance(value, str):
            raise ValueError("Expected a string")
        if kind == AnswerType.DATE:
            date.fromisoformat(value + "-01" if len(value) == 7 else value)
            if len(value) not in {7, 10}:
                raise ValueError("Invalid date format")
        return value
    if kind == AnswerType.LIST:
        if not isinstance(value, list):
            raise ValueError("List answers must be lists")
        return [_validate_cell(cell, spec.rounding) for cell in value]
    if kind == AnswerType.TABLE:
        if not isinstance(value, dict) or set(value) != {"columns", "rows"}:
            raise ValueError("Expected columns/rows table")
        columns, rows = value["columns"], value["rows"]
        if not isinstance(columns, list) or not all(isinstance(column, str) for column in columns):
            raise ValueError("Invalid table columns")
        if not isinstance(rows, list) or any(
            not isinstance(row, list) or len(row) != len(columns) for row in rows
        ):
            raise ValueError("Invalid table shape")
        return {
            "columns": columns,
            "rows": [[_validate_cell(cell, spec.rounding) for cell in row] for row in rows],
        }
    raise ValueError("Value answers require a value type")


def _has_more_than_two_decimals(val: Any) -> bool:
    if isinstance(val, (int, float, Decimal)) and not isinstance(val, bool):
        try:
            return Decimal(str(val)).as_tuple().exponent < -2
        except InvalidOperation:
            return False
    return False


def _validate_cell(value: Any, rounding: str) -> str | int | float:
    if isinstance(value, str):
        return value
    cell_rounding = rounding
    if rounding == "none" and _has_more_than_two_decimals(value):
        cell_rounding = "two_decimals"
    return json_number(value, cell_rounding)


class SubmissionWriter:
    def __init__(self, path: Path):
        self.path = writable_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8", newline="\n")
        self._seen: set[str] = set()

    def write(self, prediction: Prediction) -> None:
        if prediction.id in self._seen:
            raise ValueError("Question ID already written")
        if prediction.reason is not None and not isinstance(prediction.reason, str):
            raise ValueError("Reason must be a string")
        payload = {"id": prediction.id, "type": prediction.type.value}
        if prediction.type not in {AnswerType.ABSTAIN, AnswerType.UNANSWERABLE}:
            payload["answer"] = validate_answer(
                prediction.answer, OutputSpec(type=prediction.type, rounding=prediction.rounding)
            )
        if prediction.reason is not None:
            payload["reason"] = prediction.reason
        self._handle.write(dumps(payload) + "\n")
        self._handle.flush()
        self._seen.add(prediction.id)

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> SubmissionWriter:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
