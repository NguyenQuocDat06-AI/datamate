"""Retrieve similar solved questions, with their correct intents, as few-shot examples for the intent parser.

Similarity is cosine over TF-IDF weights of character 3-5-grams of the normalized question, in pure Python with an
inverted index. Retrieval is deterministic: ties are broken by example id.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from bizreason.utils import normalize_lookup

# A variant suffix (__strip, __abbrev, __reorder) or any other "__<op>" suffix; a question id has no other "__".
_VARIANT_SUFFIX = re.compile(r"(?:__[A-Za-z0-9_]+)+$")
_NGRAM_SIZES = (3, 4, 5)
_REQUIRED = ("id", "business_id", "question", "intent")


def base_id(question_id: str) -> str:
    """The id of the question before any variant suffix, so every variant of one question shares it."""
    return _VARIANT_SUFFIX.sub("", question_id)


def _ngrams(question: str) -> Counter[str]:
    text = " " + normalize_lookup(question) + " "
    grams: Counter[str] = Counter()
    for size in _NGRAM_SIZES:
        for start in range(len(text) - size + 1):
            grams[text[start:start + size]] += 1
    return grams


def _shape_key(intent: dict[str, Any]) -> tuple[Any, Any]:
    return intent.get("measure"), intent.get("shape")


def _compact(value: Any) -> Any:
    """Drop None values and empty containers, recursively; lists keep their order."""
    if isinstance(value, dict):
        kept = {key: _compact(item) for key, item in value.items()}
        return {key: item for key, item in kept.items() if item is not None and item != [] and item != {}}
    if isinstance(value, list):
        return [_compact(item) for item in value]
    return value


class FewShotBank:
    """Solved questions indexed for similarity search. Rows: {"id", "business_id", "question", "intent"}."""

    def __init__(self, rows: Iterable[dict[str, Any]]):
        self._rows: list[dict[str, Any]] = []
        for number, row in enumerate(rows, 1):
            if not isinstance(row, dict) or any(key not in row for key in _REQUIRED):
                raise ValueError(f"few-shot row {number} needs the fields {', '.join(_REQUIRED)}")
            if not isinstance(row["intent"], dict):
                raise ValueError(f"few-shot row {number} has no intent object")
            self._rows.append(row)
        self._bases = [base_id(str(row["id"])) for row in self._rows]
        self._index, self._norms, self._weights = self._build_index()

    @classmethod
    def load(cls, path: Path) -> FewShotBank:
        rows: list[dict[str, Any]] = []
        with Path(path).open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{number}: invalid JSON ({exc})") from exc
        return cls(rows)

    def __len__(self) -> int:
        return len(self._rows)

    def _build_index(self) -> tuple[dict[str, list[tuple[int, float]]], list[float], list[dict[str, float]]]:
        counts = [_ngrams(str(row["question"])) for row in self._rows]
        document_frequency: Counter[str] = Counter()
        for grams in counts:
            document_frequency.update(grams.keys())
        total = len(counts)
        idf = {gram: math.log((1 + total) / (1 + df)) + 1.0 for gram, df in document_frequency.items()}
        index: dict[str, list[tuple[int, float]]] = {}
        norms: list[float] = []
        weights: list[dict[str, float]] = []
        for position, grams in enumerate(counts):
            vector = {gram: (1.0 + math.log(tf)) * idf[gram] for gram, tf in grams.items()}
            norm = math.sqrt(sum(weight * weight for weight in vector.values())) or 1.0
            norms.append(norm)
            weights.append(vector)
            for gram, weight in vector.items():
                index.setdefault(gram, []).append((position, weight / norm))
        return index, norms, weights

    def _query_scores(self, question: str) -> dict[int, float]:
        grams = _ngrams(question)
        query: dict[str, float] = {}
        for gram, tf in grams.items():
            if gram in self._index:
                query[gram] = (1.0 + math.log(tf)) * (math.log((1 + len(self._rows)) / (1 + len(self._index[gram]))) + 1.0)
        norm = math.sqrt(sum(weight * weight for weight in query.values())) or 1.0
        scores: dict[int, float] = {}
        for gram, weight in query.items():
            for position, doc_weight in self._index[gram]:
                scores[position] = scores.get(position, 0.0) + (weight / norm) * doc_weight
        return scores

    def select(self, question: str, business_id: str | None, k: int = 4,
               exclude_ids: Iterable[str] = ()) -> list[dict[str, Any]]:
        """The k most similar examples the query may use.

        Leakage rules: no example from the query's business; no example whose base id equals the base id of any id
        in exclude_ids (pass the query question's own id there). One example per base id.
        Diversity: no two examples share (measure, shape) unless k cannot be filled otherwise.
        """
        if business_id is None or business_id == "":
            raise ValueError("few-shot selection needs the query's business_id")
        if k < 1:
            raise ValueError("k must be at least 1")
        blocked = {base_id(str(item)) for item in exclude_ids if item}
        scores = self._query_scores(question)
        ordered = sorted(scores, key=lambda position: (-scores[position], str(self._rows[position]["id"])))

        pool: list[int] = []
        seen_bases: set[str] = set()
        for position in ordered:
            row = self._rows[position]
            base = self._bases[position]
            if row["business_id"] == business_id or base in blocked or base in seen_bases:
                continue
            seen_bases.add(base)
            pool.append(position)

        chosen: list[int] = []
        used_shapes: set[tuple[Any, Any]] = set()
        for position in pool:
            if len(chosen) == k:
                break
            key = _shape_key(self._rows[position]["intent"])
            if key not in used_shapes:
                used_shapes.add(key)
                chosen.append(position)
        for position in pool:
            if len(chosen) == k:
                break
            if position not in chosen:
                chosen.append(position)
        return [dict(self._rows[position]) for position in chosen]

    def provider(self, k: int = 4) -> Callable[[str, str | None, str | None], str]:
        """An examples provider for IntentParser: (question, question_id, business_id) -> formatted block."""
        if k < 1:
            raise ValueError("k must be at least 1")

        def examples(question: str, question_id: str | None, business_id: str | None) -> str:
            if business_id is None:
                raise ValueError("few-shot retrieval needs the question's business_id")
            exclude = (question_id,) if question_id else ()
            return format_examples(self.select(question, business_id, k=k, exclude_ids=exclude))

        return examples


def format_examples(examples: list[dict[str, Any]]) -> str:
    """A compact block of solved questions and their intents; "" when there are none."""
    if not examples:
        return ""
    lines = ["Similar solved questions:"]
    for example in examples:
        answer = json.dumps(_compact(example["intent"]), sort_keys=True, ensure_ascii=False)
        lines.append(f"Q: {example['question']}")
        lines.append(f"A: {answer}")
    return "\n".join(lines)
