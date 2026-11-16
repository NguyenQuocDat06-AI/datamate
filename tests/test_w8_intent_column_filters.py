"""A filter that names a kept column of the shop becomes a column condition instead of dropping the intent."""
from __future__ import annotations

from typing import Any

import pytest

from bizreason.semantic.intent import _interpret, _Unresolvable


def reply(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"measure": "order_count", "shape": "scalar", "group_by": None, "filters": []}
    base.update(overrides)
    return base


def test_period_value_on_column_name_becomes_in_period_with_count() -> None:
    intent = _interpret(reply(filters=[{"dim": "due_date_x", "value": "4/2026"}]), final=True)
    assert intent.filters == []
    assert intent.column_filters == [{"column": "due_date_x", "op": "in_period", "value": None}]
    assert intent.column_measure == {"op": "count", "column": "due_date_x"}
    assert intent.measure == "order_count"


def test_text_value_on_column_name_becomes_eq() -> None:
    intent = _interpret(reply(filters=[{"dim": "xd_note_col", "value": "abc"}]), final=True)
    assert intent.column_filters == [{"column": "xd_note_col", "op": "eq", "value": "abc"}]


def test_explicit_column_measure_is_kept_and_allows_move() -> None:
    raw = reply(measure="revenue", column_measure={"op": "sum", "column": "amount_col"},
                filters=[{"dim": "other_col", "value": "x"}])
    intent = _interpret(raw, final=True)
    assert intent.column_measure == {"op": "sum", "column": "amount_col"}
    assert intent.column_filters == [{"column": "other_col", "op": "eq", "value": "x"}]


def test_non_identifier_dim_is_still_unresolvable() -> None:
    for dim in ("Ngày phải trả", "due date", "Due_Date"):
        with pytest.raises(_Unresolvable):
            _interpret(reply(filters=[{"dim": dim, "value": "4/2026"}]), final=True)


def test_identifier_dim_with_other_measure_is_still_unresolvable() -> None:
    with pytest.raises(_Unresolvable):
        _interpret(reply(measure="revenue", filters=[{"dim": "planet", "value": "mars"}]), final=True)


def test_known_dimension_unchanged() -> None:
    intent = _interpret(reply(filters=[{"dim": "channel", "value": "web"}]), final=True)
    assert intent.filters == [{"dim": "channel", "value": "web", "exclude": False}]
    assert intent.column_filters == [] and intent.column_measure is None


def test_measure_and_time_names_still_ignored() -> None:
    intent = _interpret(reply(filters=[{"dim": "revenue", "value": "5"}, {"dim": "month", "value": "4/2026"}]),
                        final=True)
    assert intent.filters == [] and intent.column_filters == [] and intent.column_measure is None


def test_existing_column_filter_not_duplicated() -> None:
    cf = {"column": "due_date_x", "op": "in_period", "value": None}
    intent = _interpret(reply(filters=[{"dim": "due_date_x", "value": "4/2026"}], column_filters=[cf]), final=True)
    assert intent.column_filters == [cf]
