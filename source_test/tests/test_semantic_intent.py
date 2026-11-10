from __future__ import annotations

from typing import Any

import pytest

from bizreason.llm import ModelError, ProviderError
from bizreason.semantic.intent import SYSTEM, IntentParser
from bizreason.semantic.vocabulary import DIMENSIONS, MEASURES, SHAPES


class FakeModel:
    """Returns scripted outputs in order; an exception instance is raised instead of returned."""

    def __init__(self, *script: Any):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]:
        self.calls.append({"system": system, "prompt": prompt, "timeout": timeout})
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def good(**overrides: Any) -> dict[str, Any]:
    reply: dict[str, Any] = {
        "measure": "revenue",
        "measure2": None,
        "shape": "scalar",
        "group_by": None,
        "filters": [],
        "time": None,
        "top_k": None,
        "compare_as": "percent",
    }
    reply.update(overrides)
    return reply


def test_valid_output_becomes_intent() -> None:
    model = FakeModel(good(measure="order_count", shape="top_k", group_by="product", top_k=3, compare_as="value"))
    result = IntentParser(model).parse("question one")

    assert result.intent is not None
    assert result.reason == ""
    assert result.attempts == 1
    assert result.intent.measure == "order_count"
    assert result.intent.shape == "top_k"
    assert result.intent.group_by == "product"
    assert result.intent.top_k == 3
    assert result.intent.compare_as == "value"
    assert result.intent.filters == []
    assert result.raw is not None and result.raw["measure"] == "order_count"


def test_outside_measure_is_a_valid_parse() -> None:
    result = IntentParser(FakeModel(good(measure="outside"))).parse("question one")

    assert result.intent is not None
    assert result.intent.measure == "outside"


def test_parser_sends_prototype_prompt_with_new_fields_and_timeout() -> None:
    model = FakeModel(good())
    IntentParser(model, timeout=12.5).parse("question one")

    call = model.calls[0]
    assert call["timeout"] == 12.5
    assert call["prompt"] == "Question: question one"
    assert '"compare_as"' in SYSTEM
    assert '"exclude"' in SYSTEM
    assert all(key in SYSTEM for key in MEASURES)
    assert all(key in SYSTEM for key in SHAPES)
    assert all(key in SYSTEM for key in DIMENSIONS)


def test_missing_shape_then_success_on_retry_names_the_field() -> None:
    reply_without_shape = good()
    del reply_without_shape["shape"]
    model = FakeModel(reply_without_shape, good(shape="breakdown", group_by="weekday"))

    result = IntentParser(model).parse("question one")

    assert result.intent is not None
    assert result.intent.shape == "breakdown"
    assert result.attempts == 2
    assert result.reason == ""
    retry_prompt = model.calls[1]["prompt"]
    assert retry_prompt.startswith("Question: question one ")
    assert "shape" in retry_prompt
    assert "compare_periods" in retry_prompt
    assert "breakdown" in retry_prompt


def test_invalid_shape_then_failure_gives_none() -> None:
    model = FakeModel(good(shape="count"), good(shape="count"))

    result = IntentParser(model).parse("question one")

    assert result.intent is None
    assert result.attempts == 2
    assert "shape" in result.reason
    assert result.raw is not None and result.raw["shape"] == "count"


def test_invalid_measure_is_retried() -> None:
    model = FakeModel(good(measure="profit"), good(measure="revenue"))

    result = IntentParser(model).parse("question one")

    assert result.intent is not None
    assert result.intent.measure == "revenue"
    assert result.attempts == 2
    assert "measure" in model.calls[1]["prompt"]


def test_model_error_then_success() -> None:
    model = FakeModel(
        ModelError("bad json"),
        good(measure="cancel_rate", shape="ratio_of_counts", threshold=threshold(metric="revenue", value=1)),
    )

    result = IntentParser(model).parse("question one")

    assert result.intent is not None
    assert result.intent.measure == "cancel_rate"
    assert result.attempts == 2
    assert result.reason == ""
    assert model.calls[1]["prompt"] != model.calls[0]["prompt"]


def test_provider_error_is_retried_like_model_error() -> None:
    model = FakeModel(ProviderError("HTTP 503"), good())

    result = IntentParser(model).parse("question one")

    assert result.intent is not None
    assert result.attempts == 2


def test_both_attempts_fail_gives_none_with_reason() -> None:
    model = FakeModel(ModelError("first"), ProviderError("second"))

    result = IntentParser(model).parse("question one")

    assert result.intent is None
    assert result.attempts == 2
    assert result.reason
    assert result.raw is None
    assert len(model.calls) == 2


def test_max_attempts_one_does_not_retry() -> None:
    model = FakeModel(good(shape="count"))

    result = IntentParser(model, max_attempts=1).parse("question one")

    assert result.intent is None
    assert result.attempts == 1
    assert len(model.calls) == 1


def test_non_object_reply_is_invalid() -> None:
    model = FakeModel(["not", "an", "object"], good())

    result = IntentParser(model).parse("question one")

    assert result.intent is not None
    assert result.attempts == 2


def test_unknown_group_by_becomes_none() -> None:
    result = IntentParser(FakeModel(good(group_by="planet"))).parse("question one")

    assert result.intent is not None
    assert result.intent.group_by is None


def test_exclude_flag_is_kept() -> None:
    reply = good(
        filters=[
            {"dim": "product", "value": "item a", "exclude": True},
            {"dim": "channel", "value": "shop", "exclude": False},
            {"dim": "shift", "value": "morning"},
        ]
    )

    result = IntentParser(FakeModel(reply)).parse("question one")

    assert result.intent is not None
    assert result.intent.filters == [
        {"dim": "product", "value": "item a", "exclude": True},
        {"dim": "channel", "value": "shop", "exclude": False},
        {"dim": "shift", "value": "morning", "exclude": False},
    ]


def test_empty_value_filter_is_dropped() -> None:
    reply = good(filters=[{"dim": "product", "value": "  ", "exclude": False}, {"dim": "brand", "value": None}])

    result = IntentParser(FakeModel(reply)).parse("question one")

    assert result.intent is not None
    assert result.intent.filters == []


def test_unknown_filter_dimension_returns_none_with_reason_without_retry() -> None:
    model = FakeModel(good(filters=[{"dim": "planet", "value": "mars", "exclude": False}]), good())

    result = IntentParser(model).parse("question one")

    assert result.intent is None
    assert result.reason == "unknown filter dimension: planet"
    assert result.attempts == 1
    assert len(model.calls) == 1


def test_unknown_filter_dimension_with_empty_value_is_dropped() -> None:
    reply = good(filters=[{"dim": "planet", "value": ""}])

    result = IntentParser(FakeModel(reply)).parse("question one")

    assert result.intent is not None
    assert result.intent.filters == []


@pytest.mark.parametrize("top_k", [0, -1, "5", True, 2.5, None])
def test_bad_top_k_is_dropped(top_k: Any) -> None:
    result = IntentParser(FakeModel(good(shape="top_k", group_by="product", top_k=top_k))).parse("question one")

    assert result.intent is not None
    assert result.intent.top_k is None


def test_positive_integer_top_k_is_kept() -> None:
    result = IntentParser(FakeModel(good(shape="top_k", group_by="product", top_k=5))).parse("question one")

    assert result.intent is not None
    assert result.intent.top_k == 5


@pytest.mark.parametrize("compare_as", [None, "", "ratio", 3])
def test_default_compare_as_when_absent_or_invalid(compare_as: Any) -> None:
    reply = good(compare_as=compare_as)

    result = IntentParser(FakeModel(reply)).parse("question one")

    assert result.intent is not None
    assert result.intent.compare_as == "percent"


def test_default_compare_as_when_key_absent() -> None:
    reply = good()
    del reply["compare_as"]

    result = IntentParser(FakeModel(reply)).parse("question one")

    assert result.intent is not None
    assert result.intent.compare_as == "percent"


def test_parser_rejects_zero_attempts() -> None:
    with pytest.raises(ValueError):
        IntentParser(FakeModel(), max_attempts=0)


@pytest.mark.parametrize("dim", ["date", "day", "month", "quarter", "year", "week", "time", "period", "range"])
def test_time_filter_is_dropped(dim: str) -> None:
    reply = good(filters=[{"dim": dim, "value": "some phrase", "exclude": False}])
    model = FakeModel(reply)

    result = IntentParser(model).parse("question one")

    assert result.intent is not None
    assert result.intent.filters == []
    assert result.attempts == 1


@pytest.mark.parametrize("dim", ["revenue", "order_count", "breakdown", "trend_check"])
def test_measure_or_shape_named_filter_is_dropped(dim: str) -> None:
    reply = good(filters=[{"dim": dim, "value": "100", "exclude": False}])

    result = IntentParser(FakeModel(reply)).parse("question one")

    assert result.intent is not None
    assert result.intent.filters == []


def test_dropped_filters_keep_valid_ones() -> None:
    reply = good(
        filters=[
            {"dim": "date", "value": "some phrase", "exclude": False},
            {"dim": "product", "value": "item a", "exclude": True},
            {"dim": "revenue", "value": "x", "exclude": False},
        ]
    )

    result = IntentParser(FakeModel(reply)).parse("question one")

    assert result.intent is not None
    assert result.intent.filters == [{"dim": "product", "value": "item a", "exclude": True}]


def test_other_unknown_filter_dimension_is_still_rejected() -> None:
    model = FakeModel(good(filters=[{"dim": "colour", "value": "red", "exclude": False}]), good())

    result = IntentParser(model).parse("question one")

    assert result.intent is None
    assert result.reason == "unknown filter dimension: colour"
    assert len(model.calls) == 1


def test_system_prompt_has_unit_and_cancelled_order_rules() -> None:
    assert "qty_base_sold" in SYSTEM
    assert "never revenue" in SYSTEM
    assert "dim unit" in SYSTEM
    assert "cancelled_count" in SYSTEM
    assert "order_count" in SYSTEM


def test_null_shape_on_final_attempt_falls_back_to_scalar() -> None:
    model = FakeModel(good(shape="count"), good(shape=None))

    result = IntentParser(model).parse("question one")

    assert result.intent is not None
    assert result.intent.shape == "scalar"
    assert result.attempts == 2
    assert result.reason == ""


def test_missing_shape_on_final_attempt_falls_back_to_scalar() -> None:
    reply = good()
    del reply["shape"]

    result = IntentParser(FakeModel(good(shape="count"), reply)).parse("question one")

    assert result.intent is not None
    assert result.intent.shape == "scalar"


def test_null_shape_on_first_attempt_is_retried_not_defaulted() -> None:
    model = FakeModel(good(shape=None), good(shape="breakdown", group_by="weekday"))

    result = IntentParser(model).parse("question one")

    assert result.intent is not None
    assert result.intent.shape == "breakdown"
    assert result.attempts == 2


def test_null_shape_with_group_by_gives_none() -> None:
    model = FakeModel(good(shape=None, group_by="weekday"), good(shape=None, group_by="weekday"))

    result = IntentParser(model).parse("question one")

    assert result.intent is None
    assert "shape" in result.reason
    assert result.attempts == 2


@pytest.mark.parametrize("top_k", [3])
def test_null_shape_with_top_k_gives_none(top_k: int) -> None:
    model = FakeModel(good(shape=None, top_k=top_k), good(shape=None, top_k=top_k))

    result = IntentParser(model).parse("question one")

    assert result.intent is None
    assert "shape" in result.reason


def test_null_shape_with_invalid_measure_on_final_attempt_gives_none() -> None:
    model = FakeModel(good(shape="count"), good(shape=None, measure="profit"))

    result = IntentParser(model).parse("question one")

    assert result.intent is None
    assert "measure" in result.reason


def threshold(**overrides: Any) -> dict[str, Any]:
    condition: dict[str, Any] = {"subject": "customer", "metric": "order_count", "op": "gte", "value": 3}
    condition.update(overrides)
    return condition


def test_valid_threshold_is_kept() -> None:
    model = FakeModel(good(measure="distinct_customers", shape="threshold_count", threshold=threshold()))

    result = IntentParser(model).parse("question one")

    assert result.intent is not None
    assert result.intent.threshold == {"subject": "customer", "metric": "order_count", "op": "gte", "value": 3}
    assert result.attempts == 1


def test_null_threshold_gives_none_and_scalar_needs_no_threshold() -> None:
    result = IntentParser(FakeModel(good(threshold=None))).parse("question one")

    assert result.intent is not None
    assert result.intent.threshold is None


def test_missing_threshold_key_on_scalar_is_fine() -> None:
    result = IntentParser(FakeModel(good())).parse("question one")

    assert result.intent is not None
    assert result.intent.threshold is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("3", 3), (" 130000 ", 130000), ("1.5", 1.5), (4.0, 4.0), (250, 250)],
)
def test_threshold_number_is_converted_or_kept(value: Any, expected: Any) -> None:
    model = FakeModel(good(shape="threshold_count", threshold=threshold(value=value)))

    result = IntentParser(model).parse("question one")

    assert result.intent is not None
    assert result.intent.threshold is not None
    assert result.intent.threshold["value"] == expected
    assert type(result.intent.threshold["value"]) is type(expected)


def test_invalid_op_then_retry_then_none() -> None:
    model = FakeModel(
        good(shape="threshold_count", threshold=threshold(op="around")),
        good(shape="threshold_count", threshold=threshold(op="equal")),
    )

    result = IntentParser(model).parse("question one")

    assert result.intent is None
    assert result.reason == "invalid threshold"
    assert result.attempts == 2
    assert len(model.calls) == 2
    assert "threshold" in model.calls[1]["prompt"]
    assert result.raw is not None and result.raw["threshold"]["op"] == "equal"


def test_invalid_threshold_then_valid_retry_succeeds() -> None:
    model = FakeModel(
        good(shape="threshold_count", threshold=threshold(subject="planet")),
        good(shape="threshold_count", threshold=threshold(metric="qty")),
    )

    result = IntentParser(model).parse("question one")

    assert result.intent is not None
    assert result.intent.threshold is not None and result.intent.threshold["metric"] == "qty"
    assert result.attempts == 2


@pytest.mark.parametrize("value", [True, False, "many", "1,000", "", None, float("nan"), float("inf"), [3]])
def test_unusable_threshold_value_is_rejected(value: Any) -> None:
    bad = good(shape="threshold_count", threshold=threshold(value=value))
    model = FakeModel(bad, bad)

    result = IntentParser(model).parse("question one")

    assert result.intent is None
    assert result.reason == "invalid threshold"
    assert len(model.calls) == 2


def test_threshold_that_is_not_an_object_is_rejected() -> None:
    result = IntentParser(FakeModel(good(threshold="3"), good(threshold=3))).parse("question one")

    assert result.intent is None
    assert result.reason == "invalid threshold"


def test_threshold_count_without_threshold_is_rejected_then_none() -> None:
    model = FakeModel(good(shape="threshold_count"), good(shape="threshold_count", threshold=None))

    result = IntentParser(model).parse("question one")

    assert result.intent is None
    assert result.reason == "missing threshold"
    assert result.attempts == 2
    assert "threshold" in model.calls[1]["prompt"]


def test_ratio_of_counts_without_threshold_is_retried() -> None:
    model = FakeModel(good(measure="distinct_customers", shape="ratio_of_counts"), good(
        measure="distinct_customers", shape="ratio_of_counts", threshold=threshold(op="lt", value=2)
    ))

    result = IntentParser(model).parse("question one")

    assert result.intent is not None
    assert result.intent.shape == "ratio_of_counts"
    assert result.intent.threshold == {"subject": "customer", "metric": "order_count", "op": "lt", "value": 2}
    assert result.attempts == 2


def test_threshold_is_kept_on_non_threshold_shape() -> None:
    result = IntentParser(FakeModel(good(threshold=threshold()))).parse("question one")

    assert result.intent is not None
    assert result.intent.shape == "scalar"
    assert result.intent.threshold == threshold()


def test_system_prompt_describes_threshold_fields_and_rules() -> None:
    for token in ("threshold", "subject", "metric", "op", "gte", "gt", "lte", "lt", "order_count", "revenue", "qty"):
        assert token in SYSTEM
    assert "at least N" in SYSTEM
    assert "more than N" in SYSTEM
    assert "million" in SYSTEM
    assert "threshold_count" in SYSTEM
    assert "ratio_of_counts" in SYSTEM
    assert "distinct_customers" in SYSTEM
    assert "orders_containing_product" in SYSTEM


def test_system_prompt_has_counting_debt_shift_and_ranking_rules() -> None:
    for rule in (
        "bills, invoices, receipts or orders means order_count",
        "how many orders were cancelled means cancelled_count, never order_count",
        "counting customers who still owe money means debtor_count",
        "how much money is still owed means debt_outstanding",
        "morning or afternoon shift must appear as a filter with dim shift",
        "which product, customer, employee or supplier is best uses group_by",
        "Shapes top_k and argmax_entity always need group_by",
        '"age_days" is a positive integer or null',
    ):
        assert rule in SYSTEM, rule
    assert '"age_days": <positive integer or null>' in SYSTEM


def test_age_days_is_parsed_into_intent() -> None:
    result = IntentParser(FakeModel(good(measure="debt_outstanding", age_days=15))).parse("question one")

    assert result.intent is not None
    assert result.intent.age_days == 15


def test_age_days_digit_string_is_converted() -> None:
    result = IntentParser(FakeModel(good(measure="debt_outstanding", age_days=" 030 "))).parse("question one")

    assert result.intent is not None
    assert result.intent.age_days == 30
    assert type(result.intent.age_days) is int


def test_age_days_absent_or_null_is_none() -> None:
    assert IntentParser(FakeModel(good())).parse("q").intent.age_days is None
    assert IntentParser(FakeModel(good(age_days=None))).parse("q").intent.age_days is None


@pytest.mark.parametrize("value", [0, -3, "abc", "", "1.5", 2.5, 15.0, True, [15]])
def test_invalid_age_days_is_retried_then_none(value: Any) -> None:
    bad = good(measure="debt_outstanding", age_days=value)
    model = FakeModel(bad, bad)

    result = IntentParser(model).parse("question one")

    assert result.intent is None
    assert result.reason == "invalid age_days"
    assert result.attempts == 2
    assert "age_days" in model.calls[1]["prompt"]


def test_invalid_age_days_then_valid_retry_succeeds() -> None:
    model = FakeModel(good(measure="debt_outstanding", age_days=-1), good(measure="debt_outstanding", age_days=7))

    result = IntentParser(model).parse("question one")

    assert result.intent is not None
    assert result.intent.age_days == 7
    assert result.attempts == 2


@pytest.mark.parametrize("shape", ["top_k", "argmax_entity"])
def test_ranking_without_group_by_is_retried_then_none(shape: str) -> None:
    model = FakeModel(good(shape=shape, top_k=3), good(shape=shape, group_by="planet"))

    result = IntentParser(model).parse("question one")

    assert result.intent is None
    assert result.reason == "missing group_by"
    assert result.attempts == 2
    assert len(model.calls) == 2
    retry_prompt = model.calls[1]["prompt"]
    assert "group_by" in retry_prompt
    assert all(dim in retry_prompt for dim in DIMENSIONS)


def test_ranking_without_group_by_then_valid_retry_succeeds() -> None:
    model = FakeModel(good(shape="argmax_entity"), good(shape="argmax_entity", group_by="employee"))

    result = IntentParser(model).parse("question one")

    assert result.intent is not None
    assert result.intent.shape == "argmax_entity"
    assert result.intent.group_by == "employee"
    assert result.attempts == 2


def test_ranking_with_group_by_is_accepted() -> None:
    model = FakeModel(good(shape="argmax_entity", group_by="customer"))

    result = IntentParser(model).parse("question one")

    assert result.intent is not None
    assert result.intent.group_by == "customer"
    assert result.attempts == 1
    assert len(model.calls) == 1
