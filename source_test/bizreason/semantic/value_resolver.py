"""Fuzzy resolution of a value named in a question to the values a shop stores (record linkage).

Used only after exact / contained / whole-word matching found nothing. Returns the stored spellings of one name,
or None when the match is not certain: a wrong match scores -1, no match scores 0.

Method: both sides are tokenised (accents removed, "500ml" split into "500" and "ml"). Each question token is
paired with the most similar stored token (edit-distance ratio); a token counts for more the rarer it is among the
shop's values (IDF). The score mixes how much of the question is covered and how much of the stored name is covered.
A candidate is rejected when a number or a one/two-letter token of the question has no counterpart in it
("500ml" is not "1.5L", "(M)" is not "(L)"). The best candidate wins only when it is clearly ahead of every
candidate that is a different name.
"""
from __future__ import annotations

import math
import re
from typing import Any

from bizreason.utils import normalize_lookup

# Words that introduce the entity in a question ("món X", "khách Y"), as normalize_lookup writes them. They are
# dropped only as an alternative reading: the reading without them must still match.
LEADING_WORDS = frozenset({
    "mon", "mat hang", "san pham", "khach hang", "khach", "nhan vien", "nv", "nhom", "loai", "ten",
    "cua hang", "do uong", "thuc uong",
})
SHORT_TOKEN = 2          # tokens up to this length must match exactly (sizes, units, "m"/"l")
FUZZY_MIN_LEN = 4        # shorter tokens never match approximately
FUZZY_MIN_SIM = 0.75     # one slip in a 4-letter word; two in 8
MIN_SCORE = 0.80         # the best candidate must reach this
MARGIN = 0.08            # and lead every other name by this much
HEAD_BONUS = 0.1         # the question is exactly the first words of the stored name (the item kind comes first)
COVER_WEIGHT = 0.6       # share of the score from covering the question (rest: covering the stored name)

_SPLIT = re.compile(r"\d+|[a-z]+")


def _tokens(text: Any) -> list[str]:
    return _SPLIT.findall(normalize_lookup(text))


def _name_key(value: Any) -> str:
    return normalize_lookup(value).replace(" ", "")


def _similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if len(a) <= SHORT_TOKEN or len(b) <= SHORT_TOKEN or min(len(a), len(b)) < FUZZY_MIN_LEN:
        return 0.0
    if a.isdigit() or b.isdigit():
        return 0.0
    if abs(len(a) - len(b)) > 2:
        return 0.0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        row = [i]
        for j, cb in enumerate(b, 1):
            row.append(min(previous[j] + 1, row[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = row
    sim = 1 - previous[-1] / max(len(a), len(b))
    return sim if sim >= FUZZY_MIN_SIM else 0.0


def _strict(token: str) -> bool:
    """A token whose absence from a candidate rules the candidate out."""
    return token.isdigit() or len(token) <= SHORT_TOKEN


def _score(needle: list[str], value: list[str], weight: dict[str, float], top: float) -> float | None:
    """Score of one stored name against the question tokens; None when a strict token has no counterpart."""
    if len(needle) == 1 and len(value) > 1:
        return None  # one word is a category or a part of many names, never enough to pick one
    free = list(value)
    covered = 0.0
    for token in sorted(needle, key=lambda t: -weight.get(t, top)):
        best, at = 0.0, -1
        for i, other in enumerate(free):
            sim = _similarity(token, other)
            if sim > best:
                best, at = sim, i
        if at < 0:
            if _strict(token):
                return None
            continue
        covered += best * weight.get(token, top)
        free.pop(at)
    # A number the stored name has and the question lacks is a different item only when the question has numbers.
    if any(t.isdigit() for t in needle) and any(t.isdigit() for t in free):
        return None
    need_total = sum(weight.get(t, top) for t in needle)
    value_total = sum(weight.get(t, top) for t in value)
    if need_total <= 0 or value_total <= 0:
        return None
    unmatched_weight = sum(weight.get(t, top) for t in free)
    cover_value = max(0.0, (value_total - unmatched_weight)) / value_total
    score = COVER_WEIGHT * covered / need_total + (1 - COVER_WEIGHT) * cover_value
    if len(needle) < len(value) and value[:len(needle)] == needle:
        score += HEAD_BONUS
    return score


def _readings(needle: str) -> list[list[str]]:
    text = normalize_lookup(needle)
    readings = [_tokens(text)]
    for lead in sorted(LEADING_WORDS, key=lambda w: -len(w)):
        if text.startswith(lead + " "):
            readings.append(_tokens(text[len(lead) + 1:]))
            break
    return [r for r in readings if r]


def resolve(needle: str, values: list[Any], dim: str = "") -> list[Any] | None:
    """Stored values (all spellings of one name) that `needle` names, or None when not certain."""
    names = {}
    for value in values:
        if value is not None and normalize_lookup(value):
            names.setdefault(_name_key(value), []).append(value)
    readings = _readings(needle)
    if not names or not readings:
        return None
    tokenised = {key: _tokens(spellings[0]) for key, spellings in names.items()}
    df: dict[str, int] = {}
    for tokens in tokenised.values():
        for token in set(tokens):
            df[token] = df.get(token, 0) + 1
    count = len(names)
    weight = {token: math.log(1 + count / n) for token, n in df.items()}
    top = math.log(1 + count)
    scores: dict[str, float] = {}
    for key, tokens in tokenised.items():
        results = [s for r in readings if (s := _score(r, tokens, weight, top)) is not None]
        if results:
            scores[key] = max(results)
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    best_key, best = ranked[0]
    if best < MIN_SCORE:
        return None
    if len(ranked) > 1 and best - ranked[1][1] < MARGIN:
        return None
    return list(names[best_key])
