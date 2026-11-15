"""An order threshold on a metric the solver resolves itself is kept; anything else unusable is still refused."""
from __future__ import annotations

from typing import Any

import pytest

from bizreason.semantic.intent import _interpret, _Invalid


def reply(threshold: Any) -> dict[str, Any]:
    return {"measure": "order_count", "shape": "threshold_count", "group_by": None, "filters": [],
            "threshold": threshold}


def spec(**kw: Any) -> dict[str, Any]:
    base = {"subject": "order", "metric": "delivery_trips", "op": "gte", "value": 2}
    base.update(kw)
    return base


def test_delivery_trips_on_order_is_kept_at_any_attempt() -> None:
    for final in (False, True):
        assert _interpret(reply(spec()), final=final).threshold == spec()


def test_unlisted_identifier_metric_on_order_kept_only_on_final() -> None:
    with pytest.raises(_Invalid):
        _interpret(reply(spec(metric="visit_count")), final=False)
    assert _interpret(reply(spec(metric="visit_count")), final=True).threshold == spec(metric="visit_count")


def test_unlisted_metric_on_other_subject_refused() -> None:
    for subject in ("customer", "day", "product"):
        with pytest.raises(_Invalid):
            _interpret(reply(spec(subject=subject, metric="visit_count")), final=True)
        with pytest.raises(_Invalid):
            _interpret(reply(spec(subject=subject)), final=True)


def test_bad_op_value_subject_or_metric_shape_still_refused() -> None:
    for bad in (spec(op="eq"), spec(value="abc"), spec(subject="planet"), spec(metric="Số lần"),
                spec(metric=None), spec(metric=3)):
        with pytest.raises(_Invalid):
            _interpret(reply(bad), final=True)


def test_listed_threshold_unchanged() -> None:
    t = {"subject": "customer", "metric": "revenue", "op": "gt", "value": 5}
    assert _interpret(reply(t), final=False).threshold == t


def test_unknown_metric_on_order_is_unsupported_by_compiler() -> None:
    from bizreason.semantic.compiler import THRESHOLD_SUBJECTS
    assert "visit_count" not in THRESHOLD_SUBJECTS["order"][2]
