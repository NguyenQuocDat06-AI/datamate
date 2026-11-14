"""Value linking: the stored values a question's phrases most likely name.

The intent parser writes filter values in its own words (a short name, a misspelling, a translation); the compiler
needs the stored spelling. Before parsing, the parser is shown the stored values closest to the question's phrases so
it can copy one of them.

Matching works on normalize_lookup text. A stored value is compared with every window of consecutive question words
of about its own length, by the cosine of character trigrams weighted by IDF (so misspellings and partial names still
score), then scaled by the share of the value's words the window contains. A stored value whose words are all in the
question scores its plain cosine. Candidates are pooled first through a trigram postings index, so a question costs
time proportional to its trigrams rather than to the number of stored values.

No external data or model is used.
"""

from __future__ import annotations

import bisect
import functools
import heapq
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict

from bizreason.semantic import vocabulary
from bizreason.utils import normalize_lookup, quote_identifier

# A block lists at most this many values per dimension and this many in total.
PER_DIM = 8
MAX_TOTAL = 30
# A candidate must reach this score (cosine scaled by word coverage, or prefix share) to be shown at all.
MIN_SCORE = 0.55
# Stage-one pool per dimension: the best trigram-overlap values that are scored window by window.
POOL_PER_DIM = 30
# Trigrams present in more than this share of all stored values (and more than MIN_PRUNE_DF of them) do not pool.
PRUNE_SHARE = 0.2
MIN_PRUNE_DF = 200
# Least trigram Dice at which a stored word counts as present in a question phrase (see _soft_coverage).
SOFT_DICE = 0.6
# A question phrase may be at most this many words longer than the stored value it is compared with.
WINDOW_EXTRA = 2
# Words that ask for a count, money, orders, a sale, a cancellation or a group, and generic filter words. A phrase
# made only of single stop words never names a stored value; a multi-word stop phrase also blocks its words from any
# phrase. Unit words stay usable, and "may" (how many) is not a stop word since it is
# also the word for "machine".
_STOP_BLOCKING_SOURCES = (
    "HOW_MANY_WORDS", "MONEY_ASK_WORDS", "ORDER_WORDS", "SALE_WORDS", "SHOP_SELF_WORDS", "CANCEL_WORDS",
    "PRODUCT_GROUP_WORDS", "CUSTOMER_GROUP_WORDS",
)
_UNIT_WORDS = {normalize_lookup(text) for text in vocabulary.QUANTITY_UNIT_WORDS}
_HOW_MANY_WORDS = {normalize_lookup(text) for text in vocabulary.HOW_MANY_WORDS}
_STOP_PHRASES = {
    normalize_lookup(text) for name in _STOP_BLOCKING_SOURCES for text in getattr(vocabulary, name)
} | {normalize_lookup(text) for text in vocabulary.GENERIC_FILTER_WORDS}
_STOP_BLOCKING = {tuple(p.split()) for p in _STOP_PHRASES if " " in p}
_STOP_WORDS = {p for p in _STOP_PHRASES if " " not in p and p not in _UNIT_WORDS and p not in _HOW_MANY_WORDS}
# Stored columns indexed explicitly (dimension, table, column, distinct-count ceiling or None).
SOURCES: tuple[tuple[str, str, str, int | None], ...] = (
    ("product", "lines", "product_name", None),
    ("product_group", "lines", "category", None),
    ("product_kind", "lines", "product_kind", None),
    ("unit", "lines", "unit", None),
    ("unit", "lines", "base_unit", None),
    ("employee", "orders", "employee", None),
    ("customer", "orders", "customer_name", 5000),
    ("channel", "orders", "channel", None),
    ("payment_method", "orders", "payment_method", None),
    ("customer_group", "orders", "customer_group", None),
    ("supplier", "purchases", "supplier_name", None),
)
# Text columns of the optional column_catalog table with at most this many distinct values are indexed too.
CATALOG_MAX_DISTINCT = 200
# A digit and a letter that touch are split apart, so "500ml" and "500 ml" are the same words.
_BOUNDARY = re.compile(r"(?<=[0-9])(?=[a-z])|(?<=[a-z])(?=[0-9])")
# Dimensions whose values are people or companies: a name is matched on at least two of its words.
PERSON_DIMS = frozenset({"customer", "employee", "supplier"})


def _grams(text: str) -> Counter:
    """Character trigrams of a normalized text, with a space at each end so word starts and ends count."""
    padded = f" {text} "
    return Counter(padded[i:i + 3] for i in range(len(padded) - 2))


def _canonical(text: str) -> str:
    """Normalized text with a space at each digit and letter boundary, so "500ml" and "500 ml" are the same words."""
    return _BOUNDARY.sub(" ", normalize_lookup(text))


@functools.lru_cache(maxsize=65536)
def _word_grams(word: str) -> frozenset[str]:
    return frozenset(_grams(word))


def _soft_coverage(value_words: list[str], window: tuple[str, ...]) -> float:
    """Share of the value's words the window has, each word counted by its best trigram Dice (0 below SOFT_DICE).

    An exact word counts 1; a misspelled or run-together word ("widjet", "128 gb" for "128gb") counts its Dice.
    """
    total = 0.0
    for word in value_words:
        best = 0.0
        grams = _word_grams(word)
        for other in window:
            if other == word:
                best = 1.0
                break
            other_grams = _word_grams(other)
            dice = 2.0 * len(grams & other_grams) / (len(grams) + len(other_grams))
            if dice >= SOFT_DICE and dice > best:
                best = dice
        total += best
    return total / len(value_words)


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({quote_identifier(table)})")}


def _distinct_values(
    connection: sqlite3.Connection, table: str, column: str, limit: int | None = None,
) -> list[str]:
    """Distinct non-blank values, most frequent first. Empty when the column is missing or over the limit."""
    quoted_table, quoted = quote_identifier(table), quote_identifier(column)
    if limit is not None:
        distinct = connection.execute(f"SELECT COUNT(DISTINCT {quoted}) FROM {quoted_table}").fetchone()[0]
        if distinct > limit:
            return []
    rows = connection.execute(
        f"SELECT CAST({quoted} AS TEXT) AS v, COUNT(*) AS n FROM {quoted_table} "
        f"WHERE {quoted} IS NOT NULL GROUP BY v ORDER BY n DESC, v"
    )
    return [str(value) for value, _ in rows if str(value).strip()]


def _catalog_sources(connection: sqlite3.Connection, tables: set[str], explicit: set[tuple[str, str]]):
    """(dimension, table, column) for text columns the column_catalog table lists with few distinct values.

    The table is optional and another component maintains it; any layout it does not have is skipped.
    """
    if "column_catalog" not in tables:
        return []
    columns = _columns(connection, "column_catalog")
    table_col = next((c for c in ("table_name", "table") if c in columns), None)
    column_col = next((c for c in ("column_name", "column") if c in columns), None)
    if not table_col or not column_col or not {"value_type", "distinct_count"} <= columns:
        return []
    rows = connection.execute(
        f"SELECT {quote_identifier(table_col)}, {quote_identifier(column_col)}, value_type, distinct_count "
        "FROM column_catalog"
    ).fetchall()
    found = []
    for table, column, value_type, distinct in rows:
        if str(value_type).lower() != "text" or distinct is None or int(distinct) > CATALOG_MAX_DISTINCT:
            continue
        if (table, column) in explicit or table not in tables:
            continue
        if column not in _columns(connection, table):
            continue
        found.append((f"{table}.{column}", table, column))
    return found


class ValueIndex:
    """Stored values grouped by dimension, with a trigram index for matching questions to them."""

    def __init__(self, dimensions: dict[str, list[str]]) -> None:
        self._display: list[str] = []
        self._norm: list[str] = []
        self._dim_of: list[str] = []
        self._dim_ids: dict[str, list[int]] = {}
        self._dim_sorted: dict[str, list[tuple[str, int]]] = {}
        grams_of: list[Counter] = []
        df: Counter = Counter()
        for dim, values in dimensions.items():
            seen: set[str] = set()
            ids: list[int] = []
            for value in values:
                text = str(value)
                norm = _canonical(text)
                if not norm or norm in seen:
                    continue
                seen.add(norm)
                gid = len(self._display)
                self._display.append(text)
                self._norm.append(norm)
                self._dim_of.append(dim)
                grams = _grams(norm)
                grams_of.append(grams)
                df.update(grams.keys())
                ids.append(gid)
            if ids:
                self._dim_ids[dim] = ids
                self._dim_sorted[dim] = sorted((self._norm[g], g) for g in ids)
        total = len(self._display)
        self._idf = {g: math.log((total + 1) / (c + 1)) + 1.0 for g, c in df.items()}
        self._unknown_idf = math.log(total + 1) + 1.0
        self._vec: list[dict[str, float]] = []
        self._post: dict[str, list[tuple[int, float]]] = defaultdict(list)
        prune_df = max(MIN_PRUNE_DF, int(PRUNE_SHARE * total))
        self._prune_df = prune_df
        for gid, grams in enumerate(grams_of):
            vec = {g: (1.0 + math.log(c)) * self._idf[g] for g, c in grams.items()}
            length = math.sqrt(sum(w * w for w in vec.values())) or 1.0
            vec = {g: w / length for g, w in vec.items()}
            self._vec.append(vec)
            for g, w in vec.items():
                self._post[g].append((gid, w))

    @classmethod
    def from_connection(cls, connection: sqlite3.Connection) -> "ValueIndex":
        """Index the stored values of a layer database. Missing tables and columns are skipped."""
        tables = _tables(connection)
        dimensions: dict[str, list[str]] = defaultdict(list)
        explicit: set[tuple[str, str]] = set()
        for dim, table, column, limit in SOURCES:
            if table not in tables or column not in _columns(connection, table):
                continue
            explicit.add((table, column))
            dimensions[dim].extend(_distinct_values(connection, table, column, limit))
        for dim, table, column in _catalog_sources(connection, tables, explicit):
            dimensions[dim].extend(_distinct_values(connection, table, column, CATALOG_MAX_DISTINCT))
        return cls(dict(dimensions))

    def _window_vector(self, words: tuple[str, ...]) -> tuple[dict[str, float], float]:
        grams = _grams(" ".join(words))
        vec = {g: (1.0 + math.log(c)) * self._idf.get(g, self._unknown_idf) for g, c in grams.items()}
        length = math.sqrt(sum(w * w for w in vec.values())) or 1.0
        return vec, length

    def candidates(self, question: str, per_dim: int = PER_DIM, max_total: int = MAX_TOTAL) -> dict[str, list[str]]:
        """Stored values per dimension that the question's phrases most likely name, best first.

        Only dimensions with at least one value above MIN_SCORE appear. Values are the stored spellings.
        """
        words = tuple(_canonical(question).split())
        if not words or not self._display:
            return {}
        # Stage one: pool values by trigram overlap with the whole question.
        question_grams = _grams(" ".join(words))
        overlap: dict[int, float] = defaultdict(float)
        for gram, count in question_grams.items():
            postings = self._post.get(gram)
            if not postings or len(postings) > self._prune_df:
                continue
            weight = (1.0 + math.log(count)) * self._idf[gram]
            for gid, value_weight in postings:
                overlap[gid] += weight * value_weight
        by_dim: dict[str, list[tuple[float, int]]] = defaultdict(list)
        for gid, score in overlap.items():
            by_dim[self._dim_of[gid]].append((score, gid))
        pools: dict[str, list[int]] = {}
        for dim, ids in self._dim_ids.items():
            if len(ids) <= POOL_PER_DIM:
                pools[dim] = list(ids)
            elif dim in by_dim:
                pools[dim] = [gid for _, gid in heapq.nlargest(POOL_PER_DIM, by_dim[dim])]
        # Stage two: score each pooled value against every window of question words of a similar length.
        state = self._question_state(words)
        window_cache: dict[tuple[int, int], tuple[dict[str, float], float]] = {}
        scored: dict[str, list[tuple[float, int, str]]] = defaultdict(list)
        for dim, ids in pools.items():
            for gid in ids:
                best = self._best_window(gid, words, state, window_cache)
                if best is not None and best[0] >= MIN_SCORE:
                    scored[dim].append((best[0], gid, best[1]))
        # Family: stored values that begin with the phrase that matched (capacity or size variants of one name).
        chosen: list[tuple[float, str, str]] = []
        for dim, items in scored.items():
            items.sort(key=lambda item: (-item[0], self._norm[item[1]]))
            taken: dict[int, float] = {}
            for score, gid, phrase in items:
                taken.setdefault(gid, score)
                for member in (self._family(dim, phrase) if len(phrase.split()) >= 2 else ()):
                    taken.setdefault(member, score - 1e-6)
            ordered = sorted(taken.items(), key=lambda item: (-item[1], self._norm[item[0]]))
            for gid, score in ordered[:per_dim]:
                chosen.append((score, dim, self._display[gid]))
        chosen.sort(key=lambda item: -item[0])
        result: dict[str, list[str]] = {}
        for _, dim, value in chosen[:max_total]:
            result.setdefault(dim, []).append(value)
        return result

    @staticmethod
    def _question_state(words: tuple[str, ...]) -> tuple[list[bool], list[bool], list[list[str]]]:
        """Per question word: blocked by a multi-word stop phrase, whether it is a stop word, and its trigrams."""
        blocked = [False] * len(words)
        for phrase in _STOP_BLOCKING:
            size = len(phrase)
            for start in range(len(words) - size + 1):
                if words[start:start + size] == phrase:
                    for k in range(start, start + size):
                        blocked[k] = True
        stop = [word in _STOP_WORDS for word in words]
        grams = [list(_grams(word).keys()) for word in words]
        return blocked, stop, grams

    def _best_window(
        self,
        gid: int,
        words: tuple[str, ...],
        state: tuple[list[bool], list[bool], list[list[str]]],
        cache: dict[tuple[int, int], tuple[dict[str, float], float]],
    ) -> tuple[float, str] | None:
        """Best score of a stored value against any run of question words, with the phrase that gave it.

        A run starts and ends on a word that shares a trigram with the value. It scores either its trigram cosine
        with the value scaled by the share of value words it has, or, when the run is a word-prefix of the value,
        the share of the value its words cover.
        """
        blocked, stop, grams = state
        value = self._norm[gid]
        value_words = value.split()
        size = len(value_words)
        vec = self._vec[gid]
        # A person's name is never matched by one of its words alone (a common word such as a month or an average
        # word is also a name part); the compiler does not partial-match people either.
        need = min(2, size) if self._dim_of[gid] in PERSON_DIMS else 1
        anchors = [i for i in range(len(words)) if not blocked[i] and any(g in vec for g in grams[i])]
        best: tuple[float, str] | None = None
        for a in anchors:
            for b in anchors:
                if b < a or b - a + 1 > size + WINDOW_EXTRA or b - a + 1 < need:
                    continue
                if any(blocked[a:b + 1]) or all(stop[a:b + 1]):
                    continue
                window = words[a:b + 1]
                phrase = " ".join(window)
                key = (a, b)
                if key not in cache:
                    cache[key] = self._window_vector(window)
                wvec, wlen = cache[key]
                dot = sum(w * vec[g] for g, w in wvec.items() if g in vec)
                score = 0.0
                cosine = dot / wlen if dot > 0.0 else 0.0
                # Coverage only scales the cosine down, so it is computed only when the cosine could still win.
                if cosine >= MIN_SCORE and (best is None or cosine > best[0]):
                    score = cosine * (0.6 + 0.4 * _soft_coverage(value_words, window))
                if len(window) >= 2 and (value == phrase or value.startswith(phrase + " ")):
                    score = max(score, 0.6 + 0.4 * len(window) / size)
                if score > 0.0 and (best is None or score > best[0]):
                    best = (score, phrase)
        return best

    def _family(self, dim: str, phrase: str) -> list[int]:
        """Stored values of a dimension that start with the phrase, as whole words."""
        entries = self._dim_sorted.get(dim, [])
        start = bisect.bisect_left(entries, (phrase, -1))
        members = []
        for norm, gid in entries[start:]:
            if norm == phrase or norm.startswith(phrase + " "):
                members.append(gid)
            elif not norm.startswith(phrase):
                break
        return members

    def block(self, question: str) -> str:
        """Prompt section listing the candidate values, or "" when no stored value is near the question."""
        found = self.candidates(question)
        if not found:
            return ""
        lines = [
            "Stored values near phrases in the question (spelled as stored):",
            "When a phrase in the question means one of these values, write that value exactly as listed, "
            "not your own spelling or translation. Ignore the list when no phrase means one of them.",
        ]
        for dim, values in found.items():
            lines.append(f"{dim}: {json.dumps(values, ensure_ascii=False)}")
        return "\n".join(lines)
