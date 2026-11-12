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


def test_choices_section_lists_stored_values_and_says_to_copy_them() -> None:
    model = FakeModel(good())
    IntentParser(model).parse("question one", choices={"channel": ["Shop A", "Online"]})

    prompt = model.calls[0]["prompt"]
    assert prompt.startswith("Question: question one\n\n")
    assert 'channel: ["Shop A", "Online"]' in prompt
    assert "copy the stored value exactly" in prompt
    assert "keep the question's words" in prompt


def test_choices_are_truncated_to_forty_values() -> None:
    values = [f"value {i:02d}" for i in range(50)]
    model = FakeModel(good())
    IntentParser(model).parse("question one", choices={"product_group": values})

    prompt = model.calls[0]["prompt"]
    assert '"value 39"' in prompt
    assert '"value 40"' not in prompt


def test_empty_choices_leave_the_prompt_unchanged() -> None:
    model = FakeModel(good(), good())
    IntentParser(model).parse("question one", choices={})
    IntentParser(model).parse("question one", choices={"channel": []})

    assert model.calls[0]["prompt"] == "Question: question one"
    assert model.calls[1]["prompt"] == "Question: question one"


def test_choices_stay_in_the_retry_prompt() -> None:
    model = FakeModel(good(measure="profit"), good())
    IntentParser(model).parse("question one", choices={"channel": ["Shop A"]})

    retry = model.calls[1]["prompt"]
    assert retry.startswith("Question: question one ")
    assert "measure" in retry
    assert 'channel: ["Shop A"]' in retry


@pytest.mark.parametrize("value", ["T6/2025", "th5/2025", "8/25", "T6", "th12", "3-2025"])
def test_time_only_filter_value_is_dropped(value: str) -> None:
    reply = good(filters=[{"dim": "product", "value": value, "exclude": False}])

    result = IntentParser(FakeModel(reply)).parse("question one")

    assert result.intent is not None
    assert result.intent.filters == []


@pytest.mark.parametrize("value", ["Sting 500ml", "Coca 330", "Item 2", "2 kg bag", "100", "A4 paper"])
def test_product_names_with_digits_are_kept(value: str) -> None:
    reply = good(filters=[{"dim": "product", "value": value, "exclude": False}])

    result = IntentParser(FakeModel(reply)).parse("question one")

    assert result.intent is not None
    assert result.intent.filters == [{"dim": "product", "value": value, "exclude": False}]


def test_measure2_is_parsed_into_intent() -> None:
    result = IntentParser(FakeModel(good(measure2="order_count"))).parse("question one")

    assert result.intent is not None
    assert result.intent.measure2 == "order_count"


def test_measure2_absent_or_null_is_none() -> None:
    assert IntentParser(FakeModel(good())).parse("q").intent.measure2 is None
    assert IntentParser(FakeModel(good(measure2=None))).parse("q").intent.measure2 is None


@pytest.mark.parametrize("value", ["profit", 7, "", ["revenue"], "outside"])
def test_invalid_measure2_is_dropped_without_retry(value: Any) -> None:
    model = FakeModel(good(measure2=value))

    result = IntentParser(model).parse("question one")

    assert result.intent is not None
    assert result.intent.measure2 is None
    assert result.intent.measure == "revenue"
    assert result.attempts == 1
    assert len(model.calls) == 1


def test_location_is_parsed_and_trimmed() -> None:
    result = IntentParser(FakeModel(good(location="  Main street  "))).parse("question one")

    assert result.intent is not None
    assert result.intent.location == "Main street"
    assert result.intent.group_by is None
    assert result.intent.filters == []


@pytest.mark.parametrize("value", [None, "", "   ", 12, ["Main street"]])
def test_unusable_location_is_none(value: Any) -> None:
    result = IntentParser(FakeModel(good(location=value))).parse("question one")

    assert result.intent is not None
    assert result.intent.location is None
    assert result.attempts == 1


def test_system_prompt_has_location_measure2_and_mapping_rules() -> None:
    assert '"location"' in SYSTEM
    assert "measure2" in SYSTEM
    assert "gross_profit" in SYSTEM
    assert "purchase_amount is only for goods bought from suppliers" in SYSTEM
    assert "prescription" in SYSTEM
    assert "product_kind" in SYSTEM
    assert "Never put a place into customer_group or customer." in SYSTEM
    assert "time expression is never a filter value" in SYSTEM


# ---- prompt profiles -------------------------------------------------------------------------------------------

# Digest of the default system prompt as it was before profiles existed; the default must stay byte-identical.
DEFAULT_SYSTEM_SHA256 = "e87e6491d704839ae5eea12fefa929156a10fa02aa126f436ed7ddb8c571a544"


def test_default_profile_system_prompt_is_unchanged() -> None:
    import hashlib

    from bizreason.semantic import intent as intent_module

    assert len(SYSTEM) == 9067
    assert hashlib.sha256(SYSTEM.encode("utf-8")).hexdigest() == DEFAULT_SYSTEM_SHA256
    assert intent_module.PROFILES == ("default", "qwen")

    model = FakeModel(good(), good())
    IntentParser(model).parse("question one")
    IntentParser(model, profile="default").parse("question one")

    assert model.calls[0]["system"] == SYSTEM
    assert model.calls[1]["system"] == SYSTEM
    assert model.calls[0]["prompt"] == model.calls[1]["prompt"] == "Question: question one"


def test_qwen_profile_sends_qwen_system_prompt() -> None:
    from bizreason.semantic.intent import QWEN_SYSTEM

    model = FakeModel(good())
    result = IntentParser(model, profile="qwen").parse("question one", choices={"channel": ["Shop A"]})

    assert result.intent is not None
    assert model.calls[0]["system"] == QWEN_SYSTEM
    assert model.calls[0]["system"] != SYSTEM
    assert model.calls[0]["prompt"].startswith("Question: question one\n\n")
    assert 'channel: ["Shop A"]' in model.calls[0]["prompt"]


def test_qwen_contract_comes_first_and_has_all_points() -> None:
    from bizreason.semantic.intent import QWEN_SYSTEM

    contract = QWEN_SYSTEM.index("OUTPUT CONTRACT")
    assert contract < QWEN_SYSTEM.index("FIELDS") < QWEN_SYSTEM.index("CATALOGUES")
    # The heading is matched on its own line, because "VIETNAMESE READING RULES" also contains the word RULES.
    assert QWEN_SYSTEM.index("CATALOGUES") < QWEN_SYSTEM.index("\nRULES\n") < QWEN_SYSTEM.index("EXAMPLES")
    for phrase in (
        "one JSON object only",
        "<think>",
        "Use only these keys",
        "copied exactly from the lists below",
        "filters holds ONLY constraints that the question states",
        "add no filter for it",
        "Never invent a dimension name",
        "copies the product text exactly as the question writes it",
        "exactly one product filter",
        "A time expression is never a filter value",
    ):
        assert phrase in QWEN_SYSTEM, phrase
    assert contract < 400, "the contract must come before the long explanations"


def test_qwen_profile_keeps_every_measure_shape_and_dimension_of_default() -> None:
    from bizreason.semantic.intent import QWEN_SYSTEM

    for key in MEASURES:
        assert key in SYSTEM and key in QWEN_SYSTEM, key
    for key in SHAPES:
        assert key in SYSTEM and key in QWEN_SYSTEM, key
    for key in DIMENSIONS:
        assert key in SYSTEM and key in QWEN_SYSTEM, key
    for definition in list(MEASURES.values()) + list(SHAPES.values()) + list(DIMENSIONS.values()):
        assert definition in QWEN_SYSTEM, definition


def test_qwen_profile_keeps_disambiguation_and_threshold_rules() -> None:
    from bizreason.semantic.intent import QWEN_SYSTEM

    for rule in (
        "never revenue",
        "dim unit",
        "cancelled_count, never order_count",
        "Counting customers who still owe money means debtor_count",
        "Asking how much money is still owed means debt_outstanding, never debtor_count",
        "morning or afternoon shift is a filter with dim shift",
        "Shapes threshold_count and ratio_of_counts always need a threshold",
        "1.3 million becomes 1300000",
        "gross_profit",
        "purchase_amount is only for goods bought from suppliers",
        "Never put a place into customer_group or customer.",
        "how many days had revenue at least X",
    ):
        assert rule in QWEN_SYSTEM, rule


def test_qwen_examples_cover_the_observed_failure_classes() -> None:
    from bizreason.semantic.intent import QWEN_SYSTEM

    examples = QWEN_SYSTEM.split("EXAMPLES (question, then the reply)\n", 1)[1]
    questions = [line for line in examples.splitlines() if line.startswith("Question: ")]
    replies = [line for line in examples.splitlines() if line.startswith("{")]
    assert 4 <= len(questions) <= 8
    assert len(questions) == len(replies)
    assert '"filters": []' in examples
    assert '"shape": "top_k"' in examples
    assert '"measure2": "order_count"' in examples
    assert '"shape": "compare_periods"' in examples
    assert "<product A>" in examples
    assert "<period A>" in examples


def test_qwen_examples_are_parseable_and_valid_intents() -> None:
    import json

    from bizreason.semantic.intent import QWEN_SYSTEM, _interpret

    examples = QWEN_SYSTEM.split("EXAMPLES (question, then the reply)\n", 1)[1]
    replies = [line for line in examples.splitlines() if line.startswith("{")]
    for line in replies:
        raw = json.loads(line)
        assert set(raw) == {
            "measure", "measure2", "shape", "group_by", "filters", "time", "location",
            "top_k", "compare_as", "age_days", "threshold",
        }
        _interpret(raw, final=True)


def test_invalid_profile_raises_value_error() -> None:
    with pytest.raises(ValueError):
        IntentParser(FakeModel(), profile="gemma")


def test_profiles_tuple_matches_registered_prompts() -> None:
    from bizreason.semantic import intent as intent_module

    assert set(intent_module.PROFILES) == set(intent_module._SYSTEM_BY_PROFILE)


@pytest.mark.parametrize("profile", ["default", "qwen"])
def test_parse_of_same_reply_is_identical_for_both_profiles(profile: str) -> None:
    reply = good(
        measure="order_count",
        shape="top_k",
        group_by="product",
        top_k=3,
        compare_as="value",
        filters=[{"dim": "product", "value": "<product A>", "exclude": True}],
        location="  Main street ",
    )
    reference = IntentParser(FakeModel(reply)).parse("question one")
    result = IntentParser(FakeModel(reply), profile=profile).parse("question one")

    assert result.intent == reference.intent
    assert result.reason == reference.reason == ""
    assert result.attempts == reference.attempts == 1
    assert result.raw == reference.raw


@pytest.mark.parametrize("profile", ["default", "qwen"])
def test_retry_and_unresolved_paths_are_identical_for_both_profiles(profile: str) -> None:
    script = [good(measure="profit"), good(filters=[{"dim": "planet", "value": "mars", "exclude": False}])]

    reference = IntentParser(FakeModel(*script)).parse("question one")
    result = IntentParser(FakeModel(*script), profile=profile).parse("question one")

    assert result.intent is None
    assert result.reason == reference.reason == "unknown filter dimension: planet"
    assert result.attempts == reference.attempts == 2


def test_qwen_vietnamese_reading_rules_sit_after_the_contract_and_name_every_word() -> None:
    from bizreason.semantic import vocabulary as vocab
    from bizreason.semantic.intent import QWEN_SYSTEM

    start = QWEN_SYSTEM.index("VIETNAMESE READING RULES")
    section = QWEN_SYSTEM[start:QWEN_SYSTEM.index("FIELDS\n")]
    assert QWEN_SYSTEM.index("OUTPUT CONTRACT") < start
    for table in (
        vocab.SHOP_SELF_WORDS, vocab.QUANTITY_UNIT_WORDS, vocab.HOW_MANY_WORDS, vocab.MONEY_ASK_WORDS,
        vocab.CANCEL_WORDS, vocab.PRODUCT_GROUP_WORDS, vocab.CUSTOMER_GROUP_WORDS,
    ):
        for word in table:
            assert f'"{word}"' in section, word
    for phrase in (
        "name the shop itself, the subject of the question",
        "never a location, a channel, a customer or any other filter",
        "measure qty_base_sold, never revenue",
        "measure revenue, never qty_base_sold",
        "cancelled_count for a count of orders",
        "cancel_rate for a percent of orders",
        "add no group filter",
    ):
        assert phrase in section, phrase


def test_qwen_examples_cover_shop_unit_and_cancel_classes() -> None:
    from bizreason.semantic.intent import QWEN_SYSTEM
    from bizreason.semantic.vocabulary import CANCEL_WORDS, QUANTITY_UNIT_WORDS, SHOP_SELF_WORDS

    examples = QWEN_SYSTEM.split("EXAMPLES (question, then the reply)\n", 1)[1]
    questions = [line for line in examples.splitlines() if line.startswith("Question: ")]
    assert any(SHOP_SELF_WORDS[0] in line for line in questions)
    assert any(QUANTITY_UNIT_WORDS[0] in line for line in questions)
    assert any(CANCEL_WORDS[0] in line for line in questions)
    assert '"measure": "qty_base_sold"' in examples
    assert '"measure": "cancelled_count"' in examples


def test_default_prompt_has_no_vietnamese_reading_rules() -> None:
    from bizreason.semantic.intent import QWEN_SYSTEM

    assert "VIETNAMESE READING RULES" not in SYSTEM
    assert "VIETNAMESE READING RULES" in QWEN_SYSTEM


def test_qwen_iteration_three_rules_sit_in_the_reading_rules_and_contract_layout() -> None:
    from bizreason.semantic import vocabulary as vocab
    from bizreason.semantic.intent import QWEN_SYSTEM

    start = QWEN_SYSTEM.index("VIETNAMESE READING RULES")
    section = QWEN_SYSTEM[start:QWEN_SYSTEM.index("FIELDS\n")]
    for table in (vocab.AVERAGE_WORDS, vocab.PER_ORDER_WORDS, vocab.IMPORT_PRICE_WORDS):
        for word in table:
            assert f'"{word}"' in section, word
    for phrase in (
        "measure avg_per_order, not revenue",
        "measure unit_purchase_price, never purchase_amount or purchase_receipt_count",
        "A product name, drug name, active ingredient or product kind is never a group.",
        "A shop word is never a location: a question that only names the shop has location null.",
    ):
        assert phrase in QWEN_SYSTEM, phrase
    examples = QWEN_SYSTEM.split("EXAMPLES (question, then the reply)\n", 1)[1]
    assert '"measure": "avg_per_order"' in examples
    assert QWEN_SYSTEM.index("OUTPUT CONTRACT") < start < QWEN_SYSTEM.index("FIELDS\n")


def test_qwen_prompt_length_grows_by_at_most_fifteen_percent() -> None:
    from bizreason.semantic.intent import QWEN_SYSTEM

    # The prompt was 13612 characters before the iteration-three rules; the budget is +15 percent.
    assert len(QWEN_SYSTEM) <= 13612 * 1.15
