"""Turn a business question into a typed Intent with a language model, validating the reply and retrying when it is unusable."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from bizreason.llm import LanguageModel, ModelError
from bizreason.semantic.compiler import Intent
from bizreason.semantic.vocabulary import DIMENSIONS, MEASURES, SHAPES

COMPARE_MODES = ("percent", "value")
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

SYSTEM = (
    "You convert a Vietnamese business question, which may have typos, slang or no diacritics, into JSON. "
    "Do not compute anything. Reply with JSON only:\n"
    '{"measure": "<measure>", "measure2": "<measure or null>", "shape": "<shape>", '
    '"group_by": "<dimension or null>", '
    '"filters": [{"dim": "<dimension>", "value": "<text copied from the question>", '
    '"exclude": <true or false>}], '
    '"time": "<the time phrase copied from the question, or null>", "top_k": <number or null>, '
    '"compare_as": "<percent or value>", "age_days": <positive integer or null>, '
    '"threshold": {"subject": "<customer, order, product or day>", "metric": "<order_count, revenue or qty>", '
    '"op": "<gte, gt, lte or lt>", "value": <number>} or null}\n'
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
    )


class IntentParser:
    def __init__(self, model: LanguageModel, *, timeout: float = 200.0, max_attempts: int = 2):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.model = model
        self.timeout = timeout
        self.max_attempts = max_attempts

    def parse(self, question: str) -> ParsedIntent:
        base_prompt = "Question: " + question
        prompt = base_prompt
        raw: dict[str, Any] | None = None
        reason = ""
        attempts = 0
        for attempts in range(1, self.max_attempts + 1):
            try:
                output = self.model.generate_json(SYSTEM, prompt, timeout=self.timeout)
            except ModelError as exc:
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
                    return ParsedIntent(intent=intent, reason="", attempts=attempts, raw=raw)
            prompt = base_prompt + " " + hint
        return ParsedIntent(intent=None, reason=reason, attempts=attempts, raw=raw)
