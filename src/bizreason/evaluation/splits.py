from __future__ import annotations

import random
from typing import Any


def grouped_split(questions: list[dict[str, Any]], seed: int = 2026) -> dict[str, Any]:
    businesses = sorted({item["business_id"] for item in questions})
    if len(businesses) < 3:
        raise ValueError("At least three businesses are required for disjoint splits")
    random.Random(seed).shuffle(businesses)
    fit_count = max(1, min(len(businesses) - 2, round(len(businesses) * 0.6)))
    calibration_count = max(1, min(len(businesses) - fit_count - 1, round(len(businesses) * 0.2)))
    groups = {
        "fit": businesses[:fit_count],
        "calibration": businesses[fit_count : fit_count + calibration_count],
        "evaluation": businesses[fit_count + calibration_count :],
    }
    return {
        "seed": seed,
        "group_by": "business_id",
        "splits": {
            name: {
                "businesses": sorted(values),
                "question_ids": sorted(
                    item["id"] for item in questions if item["business_id"] in values
                ),
            }
            for name, values in groups.items()
        },
    }


def compare_reports(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    first = {item["id"]: item["outcome"] for item in before["details"]}
    second = {item["id"]: item["outcome"] for item in after["details"]}
    if first.keys() != second.keys():
        raise ValueError("A/B reports must score the same question IDs")
    transitions: dict[str, int] = {}
    for identity, outcome in first.items():
        key = f"{outcome}->{second[identity]}"
        transitions[key] = transitions.get(key, 0) + 1
    return {
        "pa_before": before["pa"],
        "pa_after": after["pa"],
        "pa_delta": after["pa"] - before["pa"],
        "transitions": transitions,
    }
