from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from bizreason.contracts import BusinessContext, TableMeta
from bizreason.data.readers import sqlite_readonly
from bizreason.utils import normalize_lookup, quote_identifier


def tokens(text: str) -> list[str]:
    return normalize_lookup(text).split()


class BM25:
    def __init__(self, documents: list[str], *, k1: float = 1.2, b: float = 0.75):
        self.documents = [Counter(tokens(document)) for document in documents]
        self.lengths = [sum(document.values()) for document in self.documents]
        self.average = sum(self.lengths) / len(self.lengths) if self.lengths else 1
        self.frequency = Counter(token for document in self.documents for token in document)
        self.k1, self.b = k1, b

    def rank(self, query: str) -> list[tuple[int, float]]:
        scores = []
        for index, document in enumerate(self.documents):
            score = 0.0
            for token in set(tokens(query)):
                tf = document[token]
                frequency = self.frequency[token]
                idf = math.log(1 + (len(self.documents) - frequency + 0.5) / (frequency + 0.5))
                denominator = tf + self.k1 * (
                    1 - self.b + self.b * self.lengths[index] / (self.average or 1)
                )
                if tf:
                    score += idf * tf * (self.k1 + 1) / denominator
            scores.append((index, score))
        return sorted(scores, key=lambda item: (-item[1], item[0]))


@dataclass(frozen=True)
class EntityCandidate:
    table: str
    column: str
    value: str
    match: str
    retrieval_score: float


class Grounder:
    def __init__(self, context: BusinessContext):
        self.context = context
        self.index = BM25(
            [
                " ".join(
                    [
                        table.name,
                        table.grain,
                        *(
                            column.original_name + " " + (column.semantic_role or "")
                            for column in table.columns
                        ),
                    ]
                )
                for table in context.tables
            ]
        )

    def select_tables(self, question: str, limit: int = 8) -> list[TableMeta]:
        selected = {
            self.context.tables[index].name for index, _ in self.index.rank(question)[:limit]
        }
        # Bring every physical partition of an already selected schema into the context.
        signatures = {
            (
                table.grain,
                tuple((column.original_name, column.semantic_role) for column in table.columns),
            )
            for table in self.context.tables
            if table.name in selected
        }
        for table in self.context.tables:
            if (
                table.grain,
                tuple((column.original_name, column.semantic_role) for column in table.columns),
            ) in signatures:
                selected.add(table.name)
        for edge in self.context.joins:
            if edge.left in selected:
                selected.add(edge.right)
        return [table for table in self.context.tables if table.name in selected]

    def entity_candidates(
        self, question: str, tables: list[TableMeta], limit: int = 20
    ) -> list[EntityCandidate]:
        query = normalize_lookup(question)
        query_tokens = set(query.split())
        found = []
        connection = sqlite_readonly(self.context.database)
        try:
            for table in tables:
                for column in table.columns:
                    if column.semantic_role not in {
                        "product_id",
                        "product_name",
                        "customer_id",
                        "customer_name",
                    }:
                        continue
                    # This bounded index is a recall aid. Absence from it never proves missing data.
                    rows = connection.execute(
                        f"SELECT DISTINCT {quote_identifier(column.name)} FROM {quote_identifier(table.name)} "
                        f"WHERE {quote_identifier(column.name)} IS NOT NULL LIMIT 5000"
                    )
                    for (value,) in rows:
                        value = str(value)
                        normalized = normalize_lookup(value)
                        if len(normalized) < 2:
                            continue
                        if f" {normalized} " in f" {query} ":
                            found.append(
                                EntityCandidate(table.name, column.name, value, "exact_phrase", 1.0)
                            )
                        elif len(set(normalized.split()) & query_tokens) >= 2:
                            overlap = len(set(normalized.split()) & query_tokens) / max(
                                1, len(normalized.split())
                            )
                            found.append(
                                EntityCandidate(
                                    table.name, column.name, value, "token_overlap", overlap
                                )
                            )
        finally:
            connection.close()
        return sorted(
            found,
            key=lambda candidate: (-candidate.retrieval_score, candidate.table, candidate.value),
        )[:limit]
