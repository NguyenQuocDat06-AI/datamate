"""A place given as a filter dimension is read as the question's location instead of failing as an unknown dimension."""
import pytest

from bizreason.semantic.intent import _interpret


def raw(**extra):
    base = {"measure": "revenue", "shape": "scalar", "filters": [], "time": None, "location": None}
    base.update(extra)
    return base


def test_location_filter_becomes_the_location() -> None:
    intent = _interpret(raw(filters=[{"dim": "location", "value": "Phố X", "exclude": False}]), final=True)
    assert intent.location == "Phố X"
    assert intent.filters == []


def test_other_filters_are_kept_beside_the_location() -> None:
    items = [{"dim": "address", "value": "Phố X", "exclude": False},
             {"dim": "product", "value": "tea", "exclude": False}]
    intent = _interpret(raw(filters=items), final=True)
    assert intent.location == "Phố X"
    assert [f["dim"] for f in intent.filters] == ["product"]


def test_explicit_location_key_wins() -> None:
    intent = _interpret(raw(location="Phố Y", filters=[{"dim": "location", "value": "Phố X"}]), final=False)
    assert intent.location == "Phố Y"


def test_excluded_or_empty_location_filter_is_not_taken() -> None:
    with pytest.raises(Exception):
        _interpret(raw(filters=[{"dim": "location", "value": "Phố X", "exclude": True}]), final=True)
    intent = _interpret(raw(filters=[{"dim": "location", "value": "", "exclude": False}]), final=True)
    assert intent.location is None and intent.filters == []
