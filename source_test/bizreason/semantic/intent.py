"""Turn a business question into a typed Intent with a language model, validating the reply and retrying when it is unusable."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bizreason.data.vocabulary import QUESTION_NUMBER_SCALES
from bizreason.llm import LanguageModel, ModelError
from bizreason.semantic.compiler import Intent
from bizreason.semantic.vocabulary import (
    AVERAGE_WORDS,
    CANCEL_WORDS,
    CUSTOMER_GROUP_WORDS,
    DIMENSIONS,
    HOW_MANY_WORDS,
    IMPORT_PRICE_WORDS,
    MEASURES,
    MONEY_ASK_WORDS,
    ORDER_WORDS,
    PER_ORDER_WORDS,
    PRODUCT_GROUP_WORDS,
    QUANTITY_UNIT_WORDS,
    SALE_WORDS,
    SHAPES,
    SHOP_SELF_WORDS,
)

COMPARE_MODES = ("percent", "value", "average_per_weekday")
DEFAULT_COMPARE_AS = "percent"
# Time concepts a model may put into filters; time is resolved from the time phrase, so these filters are dropped.
TIME_FILTER_DIMS = ("date", "day", "month", "quarter", "year", "week", "time", "period", "range")
THRESHOLD_SUBJECTS = ("customer", "order", "product", "day")
THRESHOLD_METRICS = ("order_count", "revenue", "qty")
THRESHOLD_OPS = ("gte", "gt", "lte", "lt")
# Shapes that count entities meeting a condition; without a threshold they would count the wrong population.
THRESHOLD_SHAPES = ("threshold_count", "ratio_of_counts")
# Ranking shapes list entities, so they cannot be answered without the dimension that is ranked.
RANKING_SHAPES = ("top_k", "argmax_entity")
_PLAIN_NUMBER = re.compile(r"-?\d+(\.\d+)?")
_DIGITS = re.compile(r"[0-9]+")
# Filter values that are only a time expression, such as a month/year pair or a letter-prefixed period number.
_TIME_SLASHED = re.compile(r"(?:th|thang|t)?\d+[/-]\d+", re.IGNORECASE)
# Only month prefixes: a bare letter followed by digits can be a product code.
_TIME_PREFIXED = re.compile(r"(?:th|thang|t)\d{1,2}", re.IGNORECASE)
# Stored values shown to the parser per dimension; longer lists are cut to this many.
CHOICE_VALUE_LIMIT = 40

SYSTEM = (
    "You convert a Vietnamese business question, which may have typos, slang or no diacritics, into JSON. "
    "Do not compute anything. Reply with JSON only:\n"
    '{"measure": "<measure>", "measure2": "<measure or null>", "shape": "<shape>", '
    '"group_by": "<dimension or null>", '
    '"filters": [{"dim": "<dimension>", "value": "<text copied from the question>", '
    '"exclude": <true or false>}], '
    '"time": "<the time phrase copied from the question, or null>", '
    '"location": "<the street, district or place the question restricts customers to, or null>", '
    '"top_k": <number or null>, '
    '"compare_as": "<percent or value>", "age_days": <positive integer or null>, '
    '"threshold": {"subject": "<customer, order, product or day>", "metric": "<order_count, revenue or qty>", '
    '"op": "<gte, gt, lte or lt>", "value": <number>} or null}\n'
    '"compare_as" is "average_per_weekday" when a question asks the average per given day of the week. '
    '"compare_as" is "percent" when a comparison asks for a percent change, and "value" when it asks for '
    "a difference in units.\n"
    '"exclude" is true when the question says to leave that value out, and false otherwise.\n'
    '"threshold" keeps only the subjects whose metric satisfies the comparison; it is null when the question '
    "has no such condition. Use it for questions like counting customers with at least N orders, or orders "
    "with a value of at least X.\n"
    "Threshold rules:\n"
    '- "from N upward" and "at least N" mean op gte, which includes N.\n'
    '- "over N", "more than N" and "above N" mean op gt, which excludes N.\n'
    '- "up to N" and "at most N" mean op lte; "below N" and "under N" mean op lt.\n'
    "- Money words such as million must be converted to full numbers, so 1.3 million becomes 1300000.\n"
    "- Shapes threshold_count and ratio_of_counts always need a threshold.\n"
    "Measures:\n" + "\n".join(f"- {k}: {v}" for k, v in MEASURES.items()) + "\n"
    "Shapes:\n" + "\n".join(f"- {k}: {v}" for k, v in SHAPES.items()) + "\n"
    "Dimensions:\n" + "\n".join(f"- {k}: {v}" for k, v in DIMENSIONS.items()) + "\n"
    "Disambiguation rules:\n"
    "- A question asking how many of a countable physical unit were sold means measure qty_base_sold, never revenue. "
    "Add a filter with dim unit only when the question sums across all products of that unit.\n"
    "- A question asking how many cancelled orders there were means cancelled_count.\n"
    "- A question about how many orders were cancelled means cancelled_count, never order_count.\n"
    "- A question counting bills, invoices, receipts or orders means order_count, "
    "even when the question is very short.\n"
    "- A question counting customers who still owe money means debtor_count.\n"
    "- A question about how much money is still owed means debt_outstanding, never debtor_count.\n"
    "- A mention of a morning or afternoon shift must appear as a filter with dim shift.\n"
    "- A question asking which product, customer, employee or supplier is best uses group_by with that dimension.\n"
    "- Shapes top_k and argmax_entity always need group_by set to one dimension.\n"
    "Field rules:\n"
    "- A time expression is never a filter value; time words belong only in the time field.\n"
    '- "gross profit" is measure gross_profit.\n'
    "- A question about money received from selling a product is measure revenue; "
    "purchase_amount is only for goods bought from suppliers.\n"
    "- If a question asks two different quantities, measure is the first and measure2 is the second; "
    "otherwise measure2 is null.\n"
    '- "location" is the street, district or place the question restricts customers to, or null. '
    "Never put a place into customer_group or customer.\n"
    "- A filter on prescription drugs uses dim prescription. A filter on new or used devices uses dim product_kind.\n"
    "- A breakdown across several periods, such as months or weeks, has group_by null.\n"
    '- "age_days" is a positive integer or null. Set it when a debt question restricts to debts older than N days; '
    "otherwise it is null.\n"
    "- revenue is only for questions about money received from sales.\n"
    "- A question asking to compare two subsets in one answer is shape breakdown, not compare_periods.\n"
    "- A comparison between two shifts or two channels is shape compare_periods only when the question asks "
    "for a percent change or a difference between them.\n"
    "Examples:\n"
    'Question: "How many bottles were sold in total?"\n'
    '{"measure": "qty_base_sold", "measure2": null, "shape": "scalar", "group_by": null, '
    '"filters": [{"dim": "unit", "value": "bottles", "exclude": false}], "time": null, "top_k": null, '
    '"compare_as": "percent"}\n'
    'Question: "How many orders were cancelled?"\n'
    '{"measure": "cancelled_count", "measure2": null, "shape": "scalar", "group_by": null, '
    '"filters": [], "time": null, "top_k": null, "compare_as": "percent"}\n'
    'Question: "How many customers bought at least 3 orders?"\n'
    '{"measure": "distinct_customers", "measure2": null, "shape": "threshold_count", "group_by": null, '
    '"filters": [], "time": null, "top_k": null, "compare_as": "percent", '
    '"threshold": {"subject": "customer", "metric": "order_count", "op": "gte", "value": 3}}\n'
    "Threshold mappings:\n"
    '- "what percent of customers bought at least N orders" is shape ratio_of_counts with the same threshold.\n'
    '- "how many orders have value at least X" is measure order_count, shape threshold_count, threshold '
    "subject order and metric revenue.\n"
    '- "how many products appear in at least N orders" is measure orders_containing_product, shape '
    "threshold_count, threshold subject product and metric order_count.\n"
    '- "how many days had revenue at least X" is shape threshold_count, threshold subject day and metric revenue.\n'
    "Use only what the question says. Never add a filter the question does not state."
)

PROFILES = ("default", "qwen")


def _catalogue(title: str, entries: dict[str, str]) -> str:
    return title + "\n" + "\n".join(f"- {k}: {v}" for k, v in entries.items()) + "\n"


def _quoted(words: tuple[str, ...]) -> str:
    return ", ".join(f'"{word}"' for word in words)


def _vietnamese_reading_rules() -> str:
    """Shop-talk rules for Vietnamese questions; every word is taken from the vocabulary tables."""
    return (
        "VIETNAMESE READING RULES\n"
        f"- Shop words {_quoted(SHOP_SELF_WORDS)} name the shop itself, the subject of the question. They are never "
        "a location, a channel, a customer or any other filter.\n"
        f"- Count unit words {_quoted(QUANTITY_UNIT_WORDS)}. A unit word after a how-many word "
        f"({_quoted(HOW_MANY_WORDS)}) asks for a quantity sold: measure qty_base_sold, never revenue.\n"
        f"- Money words {_quoted(MONEY_ASK_WORDS)} ask for money: measure revenue, never qty_base_sold.\n"
        f"- Cancel words {_quoted(CANCEL_WORDS)} select cancelled measures: cancelled_count for a count of orders, "
        "cancel_rate for a percent of orders.\n"
        f"- Average words {_quoted(AVERAGE_WORDS)} with a money word or a per-order phrase "
        f"({_quoted(PER_ORDER_WORDS)}) ask for the average revenue of one order: measure avg_per_order, not revenue.\n"
        f"- Average words with import price words {_quoted(IMPORT_PRICE_WORDS)} ask for the average import price "
        "of one unit: measure unit_purchase_price, never purchase_amount or purchase_receipt_count.\n"
        f"- Product group words {_quoted(PRODUCT_GROUP_WORDS)} and customer group words "
        f"{_quoted(CUSTOMER_GROUP_WORDS)} are the only reason to add a product_group or customer_group filter. "
        "A product name, drug name, active ingredient or product kind is never a group. "
        "When none of them appears in the question, add no group filter.\n"
        "\n"
    )


# Prompt profile for Qwen-family models. Same definitions and parsing as SYSTEM; only the framing and layout differ:
# a strict output contract comes first, the catalogues are grouped, and a few short examples close the prompt.
QWEN_SYSTEM = (
    "You convert one business question into one JSON object. The question may be in Vietnamese and may have "
    "typos, slang or no diacritics. Do not compute anything.\n"
    "\n"
    "OUTPUT CONTRACT (follow every point)\n"
    "1. Reply with one JSON object only. No explanation, no markdown and no reasoning text. Never write a "
    "<think> block.\n"
    "2. Use only these keys: measure, measure2, shape, group_by, filters, time, location, top_k, compare_as, "
    "age_days, threshold.\n"
    "3. Every enum value (measure, measure2, shape, group_by, filters[].dim, threshold subject, metric and op, "
    "compare_as) must be copied exactly from the lists below. Never invent a name.\n"
    "4. filters holds ONLY constraints that the question states. Each filter needs a word or phrase in the "
    "question that supports it. If the question names no customer group, product group, channel or other "
    "dimension, add no filter for it. When the question states no filter, filters is [].\n"
    "5. filters[].dim must be one of the dimension keys listed below. Never invent a dimension name, and never "
    "use a time word, measure or shape name as a dimension.\n"
    "6. A product filter copies the product text exactly as the question writes it, with the same spelling. "
    "A question about one product has exactly one product filter.\n"
    "7. A time expression is never a filter value; it goes only into time.\n"
    "8. Use only what the question says.\n"
    "\n"
    + _vietnamese_reading_rules()
    + "FIELDS\n"
    "- measure: one measure key.\n"
    "- measure2: a second measure key when the question asks two different quantities; otherwise null.\n"
    "- shape: one shape key.\n"
    "- group_by: one dimension key when the answer is split by that dimension; otherwise null.\n"
    '- filters: a list of {"dim": <dimension key>, "value": <text copied from the question>, '
    '"exclude": true or false}.\n'
    "- time: the time phrase copied from the question, or null.\n"
    "- location: the street, district or place the question restricts customers to, or null. A shop word is never "
    "a location: a question that only names the shop has location null. "
    "Never put a place into customer_group or customer.\n"
    "- top_k: a positive integer, or null.\n"
    '- compare_as: "percent", "value" or "average_per_weekday".\n'
    "- age_days: a positive integer, or null.\n"
    '- threshold: null, or {"subject": <subject>, "metric": <metric>, "op": <operator>, "value": <number>}.\n'
    "\n"
    "CATALOGUES\n"
    + _catalogue("Measures:", MEASURES)
    + _catalogue("Shapes:", SHAPES)
    + _catalogue("Dimensions:", DIMENSIONS)
    + "Threshold subjects: customer, order, product, day. Threshold metrics: order_count, revenue, qty. "
    "Threshold operators: gte, gt, lte, lt.\n"
    "\n"
    "RULES\n"
    "Compare, exclude and threshold fields:\n"
    '- compare_as is "average_per_weekday" when the question asks for the average per given day of the week.\n'
    '- compare_as is "percent" when a comparison asks for a percent change, and "value" when it asks for a '
    "difference in units.\n"
    "- exclude is true when the question says to leave that value out, and false otherwise.\n"
    "- threshold keeps only the subjects whose metric satisfies the comparison; it is null when the question "
    "has no such condition. Use it for questions like counting customers with at least N orders, or orders "
    "with a value of at least X.\n"
    "- Operators: \"from N upward\" and \"at least N\" mean gte, which includes N. \"over N\", \"more than N\" "
    "and \"above N\" mean gt, which excludes N. \"up to N\" and \"at most N\" mean lte. \"below N\" and "
    "\"under N\" mean lt.\n"
    "- Money words such as million must become full numbers, so 1.3 million becomes 1300000.\n"
    "- Shapes threshold_count and ratio_of_counts always need a threshold.\n"
    "- age_days is set only when a debt question restricts to debts older than N days.\n"
    "- measure2 is the second quantity when the question asks two different quantities; measure is the first.\n"
    "Measure and shape choices:\n"
    "- Asking how many of a countable physical unit were sold means qty_base_sold, never revenue. Add a filter "
    "with dim unit only when the question sums across all products of that unit.\n"
    "- Asking how many orders were cancelled means cancelled_count, never order_count.\n"
    "- Counting bills, invoices, receipts or orders means order_count, even when the question is very short.\n"
    "- Counting customers who still owe money means debtor_count.\n"
    "- Asking how much money is still owed means debt_outstanding, never debtor_count.\n"
    "- Asking which product, customer, employee or supplier is best means group_by with that dimension.\n"
    "- Shapes top_k and argmax_entity always need group_by set to one dimension.\n"
    "- A breakdown across several periods, such as months or weeks, has group_by null.\n"
    "- A question asking to compare two subsets in one answer is breakdown, not compare_periods.\n"
    "- A comparison between two shifts or two channels is compare_periods only when the question asks for a "
    "percent change or a difference between them.\n"
    "- A mention of a morning or afternoon shift is a filter with dim shift.\n"
    "Field mappings:\n"
    "- A time expression is never a filter value; time words belong only in the time field.\n"
    '- "gross profit" is measure gross_profit.\n'
    "- Money received from selling a product is measure revenue; purchase_amount is only for goods bought from "
    "suppliers.\n"
    "- revenue is only for questions about money received from sales.\n"
    "- A filter on prescription drugs uses dim prescription. A filter on new or used devices uses dim "
    "product_kind.\n"
    "Threshold mappings:\n"
    '- "what percent of customers bought at least N orders" is shape ratio_of_counts with the same threshold.\n'
    '- "how many orders have value at least X" is measure order_count, shape threshold_count, threshold '
    "subject order and metric revenue.\n"
    '- "how many products appear in at least N orders" is measure orders_containing_product, shape '
    "threshold_count, threshold subject product and metric order_count.\n"
    '- "how many days had revenue at least X" is shape threshold_count, threshold subject day and metric '
    "revenue.\n"
    "\n"
    "EXAMPLES (question, then the reply)\n"
    "The first example takes a ranking with one value per entity, and no filter because none is stated.\n"
    'Question: "Which 3 customers had the highest revenue?"\n'
    '{"measure": "revenue", "measure2": null, "shape": "top_k", "group_by": "customer", "filters": [], '
    '"time": null, "location": null, "top_k": 3, "compare_as": "percent", "age_days": null, "threshold": null}\n'
    "The next example asks for an average per order, so the measure is avg_per_order, not revenue.\n"
    f'Question: "{MONEY_ASK_WORDS[2]} {AVERAGE_WORDS[0]} {PER_ORDER_WORDS[0]}?"\n'
    '{"measure": "avg_per_order", "measure2": null, "shape": "scalar", "group_by": null, "filters": [], '
    '"time": null, "location": null, "top_k": null, "compare_as": "percent", "age_days": null, "threshold": null}\n'
    "The next example has exactly one product filter, copied from the question.\n"
    'Question: "How much did <product A> sell for in total?"\n'
    '{"measure": "revenue", "measure2": null, "shape": "scalar", "group_by": null, "filters": [{"dim": "product", '
    '"value": "<product A>", "exclude": false}], "time": null, "location": null, "top_k": null, '
    '"compare_as": "percent", "age_days": null, "threshold": null}\n'
    "The next example asks two different quantities, so measure2 is set.\n"
    'Question: "How much revenue and how many orders were there?"\n'
    '{"measure": "revenue", "measure2": "order_count", "shape": "scalar", "group_by": null, "filters": [], '
    '"time": null, "location": null, "top_k": null, "compare_as": "percent", "age_days": null, "threshold": null}\n'
    "The next example compares two periods by percent change; the period phrase goes into time.\n"
    'Question: "By what percent did revenue change from <period A> to <period B>?"\n'
    '{"measure": "revenue", "measure2": null, "shape": "compare_periods", "group_by": null, "filters": [], '
    '"time": "from <period A> to <period B>", "location": null, "top_k": null, "compare_as": "percent", '
    '"age_days": null, "threshold": null}\n'
    "\n"
    "The next example is a shop question: the shop word names the shop, so it adds no filter, and the money word "
    "asks for revenue.\n"
    f'Question: "{SHOP_SELF_WORDS[0]} {SALE_WORDS[0]} {HOW_MANY_WORDS[0]} {MONEY_ASK_WORDS[0]}?"\n'
    '{"measure": "revenue", "measure2": null, "shape": "scalar", "group_by": null, "filters": [], '
    '"time": null, "location": null, "top_k": null, "compare_as": "percent", "age_days": null, "threshold": null}\n'
    "The next example asks how many units of one product were sold, so the measure is a quantity, not revenue.\n"
    f'Question: "{SALE_WORDS[0]} {HOW_MANY_WORDS[0]} {QUANTITY_UNIT_WORDS[0]} <product A>?"\n'
    '{"measure": "qty_base_sold", "measure2": null, "shape": "scalar", "group_by": null, '
    '"filters": [{"dim": "product", "value": "<product A>", "exclude": false}], "time": null, "location": null, '
    '"top_k": null, "compare_as": "percent", "age_days": null, "threshold": null}\n'
    "The last example counts cancelled orders; no group filter is added because no group word is stated.\n"
    f'Question: "{HOW_MANY_WORDS[0]} {ORDER_WORDS[0]} {CANCEL_WORDS[0]}?"\n'
    '{"measure": "cancelled_count", "measure2": null, "shape": "scalar", "group_by": null, "filters": [], '
    '"time": null, "location": null, "top_k": null, "compare_as": "percent", "age_days": null, "threshold": null}\n'
    "\n"
    "Before replying, check: one JSON object, only the allowed keys, every enum value from the lists, and "
    "filters only from the question."
)

_SYSTEM_BY_PROFILE = {"default": SYSTEM, "qwen": QWEN_SYSTEM}

# Optional short reasoning before the intent (--reasoning on). It is appended to the profile's system prompt, which
# itself stays unchanged. The analysis is the first key of the same JSON object, so parsing is unchanged: the parser
# reads only the intent keys and the analysis is kept in the raw reply for traces.
REASONING_KEY = "analysis"
REASONING_ADDENDUM = (
    "Reasoning step. Your JSON object must start with the key \"" + REASONING_KEY + "\", a string of at most three "
    "short sentences written before you choose the other keys. In it state: (1) the quantity the question asks for "
    "and the measure that computes it; (2) each filter the question itself states, quoting the question's words, or "
    "\"no filters\" when it states none; (3) the time frame and the shape of the answer (one value, a list, a "
    "ranking, a comparison or a yes/no check). Then give the other keys, consistent with that analysis. Never add a "
    "filter, time frame or grouping that the analysis did not find in the question. Worked examples, when shown, "
    "omit the analysis key; your reply must still include it."
)

_ERROR_HINT = (
    'Your previous reply could not be used; reply with one JSON object only, with a valid "measure" '
    'and a valid "shape".'
)
_FILTER_HINT = (
    'Your previous reply had an invalid "filters" field; set it to a list of objects, each with string '
    'fields "dim" and "value" and a boolean field "exclude".'
)


@dataclass
class ParsedIntent:
    intent: Intent | None  # None means the question could not be understood
    reason: str  # "" when intent is set
    attempts: int
    raw: dict[str, Any] | None  # last raw model output


class _Invalid(Exception):
    """The reply cannot be used as it is; the parser may retry with `hint` added to the prompt."""

    def __init__(self, reason: str, hint: str):
        super().__init__(reason)
        self.reason = reason
        self.hint = hint


class _Unresolvable(Exception):
    """The reply is well formed but cannot be turned into a faithful intent; do not retry."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_THRESHOLD_HINT = (
    'Your previous reply had an invalid "threshold"; set it to null, or to an object with "subject" (one of '
    f'{", ".join(THRESHOLD_SUBJECTS)}), "metric" (one of {", ".join(THRESHOLD_METRICS)}), "op" (one of '
    f'{", ".join(THRESHOLD_OPS)}) and a numeric "value".'
)
_GROUP_BY_HINT = (
    'Your previous reply used a ranking shape but gave no valid "group_by"; set "group_by" to one of: '
    f'{", ".join(DIMENSIONS)}.'
)
_AGE_DAYS_HINT = (
    'Your previous reply had an invalid "age_days"; set it to a positive whole number of days, or null.'
)
_MISSING_THRESHOLD_HINT = (
    'Your previous reply used a shape that counts entities meeting a condition but gave no valid "threshold"; '
    'set "threshold" to an object with "subject", "metric", "op" and a numeric "value".'
)


def _choice_hint(field: str, allowed: dict[str, str]) -> str:
    return f'Your previous reply had a missing or invalid "{field}"; set it to one of: {", ".join(allowed)}.'


def _is_text(value: Any) -> bool:
    return isinstance(value, str)


def _filter_value(value: Any) -> str | None:
    """Return the filter text, "" when there is none, or None when the value has the wrong type."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, str):
        return value.strip()
    return None


def _is_time_only(text: str) -> bool:
    return bool(_TIME_SLASHED.fullmatch(text) or _TIME_PREFIXED.fullmatch(text))


def _measure2(value: Any) -> str | None:
    # An unusable second measure is dropped; the question is still answered for its first measure.
    if _is_text(value) and value in MEASURES and value != "outside":
        return value
    return None


def _location(value: Any) -> str | None:
    if _is_text(value) and value.strip():
        return value.strip()
    return None


def _filters(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _Invalid("invalid filters", _FILTER_HINT)
    kept: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or not _is_text(item.get("dim")):
            raise _Invalid("invalid filters", _FILTER_HINT)
        dim = item["dim"]
        text = _filter_value(item.get("value"))
        if text is None:
            raise _Invalid("invalid filters", _FILTER_HINT)
        if not text:
            # An empty value constrains nothing, so dropping it does not loosen the intent.
            continue
        if dim not in DIMENSIONS:
            if dim in TIME_FILTER_DIMS or dim in MEASURES or dim in SHAPES:
                # Time is resolved from the time phrase, and a measure or shape name is not a row constraint.
                continue
            # Dropping this filter would silently widen the question, so refuse instead.
            raise _Unresolvable(f"unknown filter dimension: {dim}")
        if _is_time_only(text):
            # A period such as a month is resolved from the time phrase, so it is not a row constraint.
            continue
        kept.append({"dim": dim, "value": text, "exclude": item.get("exclude") is True})
    return kept


def _threshold_number(value: Any) -> int | float | None:
    """Return the number, converting a plain digit string, or None when the value is not a usable number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip()
        if _PLAIN_NUMBER.fullmatch(text):
            return float(text) if "." in text else int(text)
    return None


def _threshold(value: Any) -> dict[str, Any] | None:
    """Return the validated condition, None when the model gave none, or raise when it gave an unusable one."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _Invalid("invalid threshold", _THRESHOLD_HINT)
    subject = value.get("subject")
    metric = value.get("metric")
    op = value.get("op")
    number = _threshold_number(value.get("value"))
    if (
        not _is_text(subject) or subject not in THRESHOLD_SUBJECTS
        or not _is_text(metric) or metric not in THRESHOLD_METRICS
        or not _is_text(op) or op not in THRESHOLD_OPS
        or number is None
    ):
        # Dropping the condition would silently change the population, so the reply is retried instead.
        raise _Invalid("invalid threshold", _THRESHOLD_HINT)
    return {"subject": subject, "metric": metric, "op": op, "value": number}


_QUESTION_NUMBER = re.compile(r"\d[\d.,]*")
_SCALE_WORD = re.compile(r"\s*([^\W\d_]+)")
_THRESHOLD_VALUE_HINT = (
    'Your previous reply had a "threshold" value that is not a number written in the question; copy the number '
    "exactly as the question states it (with its thousands, millions or billions)."
)


def question_numbers(question: str) -> set[float]:
    """Every number the question writes, read with "." or "," as a thousands or a decimal separator, and also
    scaled by a scale word written right after it ("8tr", "7,5 triệu", "500k")."""
    found: set[float] = set()
    for match in _QUESTION_NUMBER.finditer(question):
        token = match.group().rstrip(".,")
        readings = {token.replace(".", "").replace(",", ""), token.replace(".", "").replace(",", "."),
                    token.replace(",", "")}
        word = _SCALE_WORD.match(question, match.end())
        scale = QUESTION_NUMBER_SCALES.get(word.group(1).lower()) if word else None
        for reading in readings:
            try:
                value = float(reading)
            except ValueError:
                continue
            found.add(value)
            if scale:
                found.add(round(value * scale, 6))
    return found


def threshold_grounded(intent: Intent, question: str) -> bool:
    """False when the intent has a threshold whose value is not a number the question writes in digits.

    A question with no digits at all may state its number in words, so it is not checked."""
    value = (intent.threshold or {}).get("value")
    if value is None or not any(ch.isdigit() for ch in question):
        return True
    return float(value) in question_numbers(question)


def _top_k(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _age_days(value: Any) -> int | None:
    """Return the positive day count (digit strings converted), None when absent, or raise when unusable."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise _Invalid("invalid age_days", _AGE_DAYS_HINT)
    if isinstance(value, str) and _DIGITS.fullmatch(value.strip()):
        value = int(value.strip())
    if isinstance(value, int) and value > 0:
        return value
    raise _Invalid("invalid age_days", _AGE_DAYS_HINT)


def _interpret(raw: Any, *, final: bool) -> Intent:
    if not isinstance(raw, dict):
        raise _Invalid("invalid reply", "Your previous reply was not a JSON object; reply with one JSON object only.")
    measure = raw.get("measure")
    if not _is_text(measure) or measure not in MEASURES:
        raise _Invalid("invalid measure", _choice_hint("measure", MEASURES))
    group_by = raw.get("group_by")
    if not _is_text(group_by) or group_by not in DIMENSIONS:
        group_by = None
    top_k = _top_k(raw.get("top_k"))
    shape = raw.get("shape")
    if shape is None and final and group_by is None and top_k is None:
        # A missing shape on the last attempt is taken as a single value, but only when nothing asks for a list.
        shape = "scalar"
    if not _is_text(shape) or shape not in SHAPES:
        raise _Invalid("invalid shape", _choice_hint("shape", SHAPES))
    if shape in RANKING_SHAPES and group_by is None:
        # A ranking without its dimension cannot name the entities, so the reply is retried instead.
        raise _Invalid("missing group_by", _GROUP_BY_HINT)
    age_days = _age_days(raw.get("age_days"))
    threshold = _threshold(raw.get("threshold"))
    if shape in THRESHOLD_SHAPES and threshold is None:
        raise _Invalid("missing threshold", _MISSING_THRESHOLD_HINT)
    compare_as = raw.get("compare_as")
    if not _is_text(compare_as) or compare_as not in COMPARE_MODES:
        compare_as = DEFAULT_COMPARE_AS
    return Intent(
        measure=measure,
        shape=shape,
        group_by=group_by,
        filters=_filters(raw.get("filters")),
        top_k=top_k,
        compare_as=compare_as,
        threshold=threshold,
        age_days=age_days,
        measure2=_measure2(raw.get("measure2")),
        location=_location(raw.get("location")),
    )


def _choices_section(choices: dict[str, list[str]] | None) -> str:
    """The stored values the filters must match, or "" when there are none to show."""
    lines = [
        f"- {dim}: {json.dumps(values[:CHOICE_VALUE_LIMIT], ensure_ascii=False)}"
        for dim, values in (choices or {}).items()
        if values
    ]
    if not lines:
        return ""
    return (
        "Stored values for filter dimensions:\n" + "\n".join(lines) + "\n"
        "When a filter refers to one of these values, copy the stored value exactly. "
        "When the question uses a colloquial word that has the same meaning as a stored value, use that stored value. "
        "If no stored value matches, keep the question's words."
    )


class IntentParser:
    def __init__(
        self,
        model: LanguageModel,
        *,
        timeout: float = 200.0,
        max_attempts: int = 2,
        profile: str = "default",
        examples: Callable[[str, str | None, str | None], str] | None = None,
        reasoning: bool = False,
        definitions: Callable[[str], str] | None = None,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if profile not in _SYSTEM_BY_PROFILE:
            raise ValueError(f"unknown prompt profile {profile!r}; expected one of {', '.join(PROFILES)}")
        self.model = model
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.profile = profile
        self.system = _SYSTEM_BY_PROFILE[profile]
        self.reasoning = reasoning
        if reasoning:
            self.system = self.system + "\n\n" + REASONING_ADDENDUM
        # examples(question, question_id, business_id) returns a block of worked examples, or "" for none.
        self.examples = examples
        # definitions(question) returns the rulebook definitions of terms the question uses, or "" for none.
        self.definitions = definitions

    def parse(self, question: str, choices: dict[str, list[str]] | None = None, *,
              question_id: str | None = None, business_id: str | None = None) -> ParsedIntent:
        base_prompt = "Question: " + question
        if self.examples is not None:
            block = self.examples(question, question_id, business_id)
            if block:
                base_prompt = base_prompt + "\n\n" + block
        if self.definitions is not None:
            defined = self.definitions(question)
            if defined:
                base_prompt = base_prompt + "\n\n" + defined
        section = _choices_section(choices)
        suffix = "\n\n" + section if section else ""
        prompt = base_prompt + suffix
        raw: dict[str, Any] | None = None
        reason = ""
        attempts = 0
        for attempts in range(1, self.max_attempts + 1):
            try:
                output = self.model.generate_json(self.system, prompt, timeout=self.timeout)
            except Exception as exc:
                # Any failure of the model call (including raw transport errors) is a failed
                # attempt, so the solver only ever sees the "model call failed" reason.
                reason = f"model call failed: {type(exc).__name__}"
                hint = _ERROR_HINT
            else:
                raw = output if isinstance(output, dict) else None
                try:
                    intent = _interpret(output, final=attempts == self.max_attempts)
                except _Unresolvable as exc:
                    return ParsedIntent(intent=None, reason=exc.reason, attempts=attempts, raw=raw)
                except _Invalid as exc:
                    reason = exc.reason
                    hint = exc.hint
                else:
                    if threshold_grounded(intent, question):
                        return ParsedIntent(intent=intent, reason="", attempts=attempts, raw=raw)
                    # A threshold the question does not state would count a different population: retry, then
                    # give up rather than answer with it.
                    reason = "threshold value not stated in the question"
                    hint = _THRESHOLD_VALUE_HINT
            prompt = base_prompt + " " + hint + suffix
        return ParsedIntent(intent=None, reason=reason, attempts=attempts, raw=raw)


def intent_key(intent: Intent) -> tuple:
    """What an intent asks, for comparing two parses: every field, filters as a set of normalized values."""
    from bizreason.utils import normalize_lookup
    filters = tuple(sorted(
        (str(item.get("dim")), normalize_lookup(str(item.get("value", ""))), bool(item.get("exclude")))
        for item in intent.filters
    ))
    threshold = tuple(sorted((intent.threshold or {}).items()))
    return (intent.measure, intent.measure2, intent.shape, intent.group_by, filters, intent.top_k,
            intent.compare_as, threshold, intent.age_days, intent.location)


class ConsensusParser:
    """Two parsers that read the question with different examples; when they disagree the question is not
    understood with confidence, so no intent is returned and the solver abstains (a wrong answer costs a point)."""

    def __init__(self, first: "IntentParser", second: "IntentParser"):
        self.first, self.second = first, second

    def parse(self, question: str, choices: dict[str, list[str]] | None = None, **kwargs) -> ParsedIntent:
        a = self.first.parse(question, choices, **kwargs)
        if a.intent is None:
            return a
        b = self.second.parse(question, choices, **kwargs)
        attempts = a.attempts + b.attempts
        if b.intent is None:
            return ParsedIntent(intent=None, reason=f"second parse failed: {b.reason}", attempts=attempts, raw=a.raw)
        if intent_key(a.intent) != intent_key(b.intent):
            return ParsedIntent(intent=None, reason="parses disagree", attempts=attempts, raw=a.raw)
        return a
