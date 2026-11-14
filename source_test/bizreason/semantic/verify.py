"""Back-translation check: show the model what the system understood, in plain words, next to the question.

Two independent readings of a question tend to make the same mistake (both read 'commission' as 'revenue'), so
agreement says little. Reading the system's own understanding back against the question catches exactly that: the
model compares a concrete statement ("the total revenue of orders by employee X in August 2025") with the question
("how much commission did X get in August 2025") and reports a mismatch. A mismatch abstains.
"""
from __future__ import annotations

from typing import Any

from bizreason.contracts import TimeWindow
from bizreason.semantic.compiler import Intent
from bizreason.semantic.vocabulary import DIMENSIONS, MEASURES, SHAPES

SYSTEM = (
    "You check a data assistant. You get a shop owner's question (Vietnamese, often informal or without accents) and "
    "the assistant's understanding of it, written as a plan. Decide whether the plan computes what the question asks. "
    "Judge only meaning: the quantity measured, every condition the question states (product, person, group, "
    "status, unit, date range, threshold), the grouping or ranking, and the form of the answer. Spelling and accents "
    "do not matter; a stored value written differently but naming the same thing is fine. Ignore definitions the plan "
    "takes from the shop's rules. Reply with one JSON object: {\"same\": true|false, \"issue\": \"<short reason when "
    "false>\"}."
)


def render(intent: Intent, windows: list[TimeWindow]) -> str:
    """The plan in plain English, from the intent and the resolved time windows."""
    parts = [f"Measure: {intent.measure} ({MEASURES.get(intent.measure, '')})"]
    if intent.measure2:
        parts.append(f"Second measure: {intent.measure2} ({MEASURES.get(intent.measure2, '')})")
    parts.append(f"Answer form: {intent.shape} ({SHAPES.get(intent.shape, '')})")
    if intent.group_by:
        parts.append(f"Grouped by: {intent.group_by} ({DIMENSIONS.get(intent.group_by, '')})")
    if intent.top_k:
        parts.append(f"Top: {intent.top_k}")
    for item in intent.filters:
        verb = "excluding" if item.get("exclude") else "only"
        parts.append(f"Condition: {verb} {item.get('dim')} = {item.get('value')!r}")
    if intent.threshold:
        t = intent.threshold
        parts.append(f"Threshold: {t.get('subject')} whose {t.get('metric')} {t.get('op')} {t.get('value')}")
    if getattr(intent, "min_orders", None):
        parts.append(f"Only entities with at least {intent.min_orders} orders")
    if intent.age_days is not None:
        parts.append(f"Only debt older than {intent.age_days} days")
    for w in windows:
        last = w.end_exclusive.toordinal() - 1
        from datetime import date as _date
        parts.append(f"Period ({w.role}): {w.start.isoformat()} to {_date.fromordinal(last).isoformat()}")
    if intent.compare_as and intent.shape == "compare_periods":
        parts.append(f"Comparison as: {intent.compare_as}")
    return "\n".join(parts)


def check(model: Any, question: str, intent: Intent, windows: list[TimeWindow], *, timeout: float = 120.0
          ) -> tuple[bool, str]:
    """(same, issue). Any failure of the model call counts as same, so an outage never blocks answers."""
    prompt = f"Question: {question}\n\nPlan:\n{render(intent, windows)}"
    try:
        reply = model.generate_json(SYSTEM, prompt, timeout=timeout)
    except Exception:
        return True, "check unavailable"
    if not isinstance(reply, dict) or not isinstance(reply.get("same"), bool):
        return True, "check unreadable"
    return reply["same"], str(reply.get("issue") or "")[:200]
