"""Conservative identifier repair for typed plans.

The transcript follows Instructor's validation reask protocol: original user input,
previous assistant object, then deterministic validator feedback. A compiler-valid
repair still needs semantic evaluation; this module never supplies an answer or alias.
"""
from __future__ import annotations

import json
from typing import Any

ReferencePath = tuple[str | int, ...]


def references(plan: dict[str, Any]) -> dict[ReferencePath, str]:
    """Paths to actual column references, with the compiler's value/filter purpose."""
    found: dict[ReferencePath, str] = {}

    def expression(node: Any, path: ReferencePath) -> None:
        if isinstance(node, str):
            found[path] = "value"
        elif isinstance(node, dict):
            if node.get("op") in ("+", "-", "*", "/"):
                expression(node.get("left"), (*path, "left"))
                expression(node.get("right"), (*path, "right"))
            elif isinstance(node.get("days"), list):
                for i, value in enumerate(node["days"]):
                    if isinstance(value, str) and value not in ("today", "period_start", "period_end"):
                        found[(*path, "days", i)] = "value"

    def filters(items: Any, path: ReferencePath) -> None:
        if not isinstance(items, list):
            return
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("column"), str):
                found[(*path, i, "column")] = "filter"
            if isinstance(item.get("column2"), str):
                found[(*path, i, "column2")] = "value"
            expression(item.get("expr"), (*path, i, "expr"))

    def measure(node: Any, path: ReferencePath) -> None:
        if not isinstance(node, dict):
            return
        if node.get("agg"):
            expression(node.get("expr"), (*path, "expr"))
            filters(node.get("filters"), (*path, "filters"))
        elif node.get("op") in ("+", "-", "*", "/"):
            measure(node.get("left"), (*path, "left"))
            measure(node.get("right"), (*path, "right"))

    measure(plan.get("measure"), ("measure",))
    filters(plan.get("filters"), ("filters",))
    filters(plan.get("share_filters"), ("share_filters",))
    if isinstance(plan.get("group_by"), str):
        found[("group_by",)] = "value"
    per = plan.get("per")
    if isinstance(per, dict):
        if isinstance(per.get("by"), str):
            found[("per", "by")] = "value"
        measure(per.get("having"), ("per", "having"))
    measure(plan.get("having"), ("having",))
    return found


def reference_at(value: Any, path: ReferencePath) -> Any:
    for key in path:
        value = value[key]
    return value


def _differences(left: Any, right: Any, path: ReferencePath = ()) -> list[ReferencePath]:
    if type(left) is not type(right):
        return [path]
    if isinstance(left, dict):
        changed = [(*path, k) for k in left.keys() ^ right.keys()]
        for key in left.keys() & right.keys():
            changed.extend(_differences(left[key], right[key], (*path, key)))
        return changed
    if isinstance(left, list):
        if len(left) != len(right):
            return [path]
        return [p for i, (a, b) in enumerate(zip(left, right))
                for p in _differences(a, b, (*path, i))]
    return [] if left == right else [path]


def binding_changes_only(before: dict[str, Any], after: Any,
                         invalid_paths: set[ReferencePath]) -> tuple[bool, list[ReferencePath]]:
    """Allow only string replacements at references already rejected by the compiler."""
    if not isinstance(after, dict):
        return False, [()]
    changed = _differences(before, after)
    try:
        accepted = all(path in invalid_paths and isinstance(reference_at(before, path), str)
                       and isinstance(reference_at(after, path), str) for path in changed)
    except (KeyError, IndexError, TypeError):
        accepted = False
    return accepted, changed


def binding_feedback(error: Exception, card: Any, prompt: str) -> str:
    """Repeat only identifiers and descriptions already visible in the initial card."""
    symbols = {table.name: [
        {"identifier": col.column, "gloss": col.gloss, "header": col.header, "description": col.description}
        for col in table.columns.values() if ("  - " + col.render()) in prompt]
        for table in card.tables.values()}
    return ("The plan failed identifier validation: " + str(error)
            + "\nAvailable column identifiers and their existing descriptions are listed below. "
              "Source headers in README are labels, not column identifiers. Return the same plan with only "
              "column-reference identifier leaves corrected. Preserve operators, constants, scopes, periods, "
              "filter values, output shape and all other fields exactly. Do not add or remove conditions.\n"
            + json.dumps(symbols, ensure_ascii=False))
