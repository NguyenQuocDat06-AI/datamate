"""Selective-prediction gate: abstain on solve paths whose measured precision is below one half.

Scoring is +1 right, -1 wrong, 0 abstain, so answering only pays when P(correct) > 0.5. The gate groups answers by a
structural "path key" (measure, shape, grouping, filter dimensions, windows, answer type; never values or question
text), reads per-group correct/wrong counts from a table fitted offline on permitted data, and blocks groups whose
Wilson lower bound on precision is under 0.5. Unanswerable and abstain answers are never touched.
"""
from __future__ import annotations

import math
from typing import Any

MIN_SUPPORT = 8  # answered samples (correct + wrong) a group needs before its own statistics are trusted
MIN_WRONG = 2  # never block a group on fewer wrong answers than this
Z_90 = 1.6448536269514722  # one-sided 95% / two-sided 90% normal quantile for the Wilson interval
BREAK_EVEN = 0.5  # precision at which answering and abstaining score the same
PASS_THROUGH_TYPES = frozenset({"abstain", "unanswerable"})


def wilson_lower(correct: int, total: int, z: float = Z_90) -> float:
    """Lower end of the Wilson score interval for a proportion; 0.0 when there are no samples."""
    if total <= 0:
        return 0.0
    p = correct / total
    z2 = z * z
    centre = p + z2 / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total))
    return max(0.0, (centre - margin) / (1 + z2 / total))


def _evidence_flags(trace: dict[str, Any]) -> list[str]:
    text = str(trace.get("evidence") or trace.get("reason") or "")
    return [flag for flag in ("model_time", "price_changes") if flag in text]


def coarse_key(intent: dict[str, Any] | None, answer_type: str) -> str:
    intent = intent or {}
    return f"{intent.get('measure') or '-'}|{intent.get('shape') or '-'}|{answer_type}"


def path_key(intent: dict[str, Any] | None, trace: dict[str, Any] | None, answer_type: str) -> str:
    """Structural key. Contains only canonical field names, counts and roles, never filter values."""
    intent, trace = intent or {}, trace or {}
    dims = sorted({str(item.get("dim")) for item in intent.get("filters") or [] if isinstance(item, dict)})
    windows = [w for w in trace.get("windows") or [] if isinstance(w, (list, tuple)) and w]
    roles = sorted(str(w[0]) for w in windows)
    parts = [
        str(intent.get("measure") or "-"),
        str(intent.get("shape") or "-"),
        "g" if intent.get("group_by") else "-",
        "f:" + ",".join(dims),
        "cm" if intent.get("column_measure") else "-",
        f"w{len(windows)}:" + ",".join(roles),
        "ev:" + ",".join(_evidence_flags(trace)),
        answer_type,
    ]
    return "|".join(parts)


class AnswerGate:
    """table: {key: {"n": int, "correct": int, "wrong": int}} holding fine keys and coarse keys together."""

    def __init__(self, table: dict[str, dict[str, int]]):
        self.table = table

    def _blocks(self, key: str) -> bool | None:
        """True/False once the key has enough support; None when it does not."""
        row = self.table.get(key)
        if not row:
            return None
        correct, wrong = int(row.get("correct", 0)), int(row.get("wrong", 0))
        if correct + wrong < MIN_SUPPORT:
            return None
        return wrong >= MIN_WRONG and wilson_lower(correct, correct + wrong) < BREAK_EVEN

    def decide(self, key: str, coarse: str) -> bool:
        """True = the answer may be given. Unknown or thin groups keep the existing behaviour (answer)."""
        for candidate in (key, "coarse:" + coarse):
            verdict = self._blocks(candidate)
            if verdict is not None:
                return not verdict
        return True

    def allows(self, intent: dict[str, Any] | None, trace: dict[str, Any] | None, answer_type: str) -> tuple[bool, str]:
        if answer_type in PASS_THROUGH_TYPES:
            return True, ""
        key = path_key(intent, trace, answer_type)
        return self.decide(key, coarse_key(intent, answer_type)), key


def build_table(samples: list[tuple[str, str, bool]]) -> dict[str, dict[str, int]]:
    """samples: (fine key, coarse key, correct) for answered questions. Coarse keys are stored with a prefix."""
    table: dict[str, dict[str, int]] = {}
    for fine, coarse, ok in samples:
        for key in (fine, "coarse:" + coarse):
            row = table.setdefault(key, {"n": 0, "correct": 0, "wrong": 0})
            row["n"] += 1
            row["correct" if ok else "wrong"] += 1
    return table
