"""Ranking terms a rulebook defines, translated once into the parts of an intent.

A rulebook can define a ranking word ("khách sộp nhất" = highest average order value among customers with at least
3 orders). The private rulebook may define words the training rulebook never had, so the definitions are read from
the test set's own rulebook at run time: one model call turns each ranking row into {measure, group_by, min_orders},
checked against what the compiler can rank. When a ranking question uses a translated term, the term's parts replace
the parser's choice of measure and grouping, so the rulebook, not the parser's guess, decides what is ranked.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from bizreason.semantic.compiler import Intent
from bizreason.semantic.definitions import rulebook_definitions
from bizreason.semantic.vocabulary import MEASURES
from bizreason.utils import normalize_lookup

# What the compiler can rank, and by which grouping (see Compiler._ranked and its supplier / debtor paths).
RANKABLE: dict[str, set[str]] = {
    "revenue": {"customer", "employee", "product", "product_group", "channel", "payment_method", "hour", "date"},
    "order_count": {"customer", "employee", "product", "product_group", "channel", "payment_method", "hour", "date"},
    "qty_base_sold": {"product", "product_group", "customer", "employee", "date"},
    "orders_containing_product": {"product", "product_group"},
    "gross_profit": {"product", "product_group"},
    "cancelled_count": {"customer", "employee", "product", "date"},
    "active_days": {"customer", "employee"},
    "avg_per_order": {"customer", "employee"},
    "purchase_amount": {"supplier"},
    "purchase_receipt_count": {"supplier"},
}
RANKING_SHAPES = {"argmax_entity", "top_k", "argmax_date"}
EXTRACT_TIMEOUT = 240.0

SYSTEM = (
    "You translate a shop rulebook's ranking definitions into a fixed query vocabulary. For each numbered term, "
    "read its definition and return the measure ranked, the entity grouped by, and the minimum number of orders an "
    "entity needs to be considered (null when the definition sets none). Use only these measures:\n"
    + "\n".join(f"- {m}: {MEASURES.get(m, '')}" for m in RANKABLE)
    + "\nand these groupings: " + ", ".join(sorted({g for gs in RANKABLE.values() for g in gs}))
    + " (date = a day, hour = an hour of the day). If a definition needs anything else (a different condition, a "
    "lowest instead of highest, a measure not listed), set \"measure\" to null. Reply with one JSON object: "
    '{"terms": [{"n": <number>, "measure": <name or null>, "group_by": <name or null>, "min_orders": <int or null>}]}'
)


class _Model(Protocol):
    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]: ...


@dataclass(frozen=True)
class TermRule:
    term: str
    words: frozenset[str]
    measure: str
    group_by: str
    min_orders: int | None


_active: tuple[TermRule, ...] = ()


def set_active(rules: list[TermRule] | tuple[TermRule, ...]) -> None:
    global _active
    _active = tuple(rules)


def active() -> tuple[TermRule, ...]:
    return _active


def _validate(reply: Any, rows: list[tuple[str, str, frozenset[str]]]) -> list[TermRule]:
    out: list[TermRule] = []
    items = reply.get("terms") if isinstance(reply, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        n, measure, group = item.get("n"), item.get("measure"), item.get("group_by")
        if not isinstance(n, int) or not 1 <= n <= len(rows) or measure not in RANKABLE:
            continue
        if group not in RANKABLE[measure]:
            continue
        min_orders = item.get("min_orders")
        if min_orders is not None and (not isinstance(min_orders, int) or isinstance(min_orders, bool)
                                       or not 1 <= min_orders <= 1000):
            continue
        term, _, words = rows[n - 1]
        out.append(TermRule(term=term, words=words, measure=measure, group_by=group, min_orders=min_orders))
    return out


def extract(rulebook_text: str, model: _Model, timeout: float = EXTRACT_TIMEOUT) -> tuple[list[TermRule], bool]:
    """(rules, ok). ok is False when the model call failed, so the result is not cached."""
    rows = rulebook_definitions(rulebook_text)
    ranking = [row for row in rows if row[2] & _ranking_words()]
    if not ranking:
        return [], True
    prompt = "\n".join(f"{i}. {term}: {text}" for i, (term, text, _) in enumerate(ranking, 1))
    try:
        reply = model.generate_json(SYSTEM, prompt, timeout=timeout)
    except Exception:
        return [], False
    return _validate(reply, ranking), True


def _ranking_words() -> frozenset[str]:
    from bizreason.semantic.vocabulary import DEFINITION_RANKING_WORDS
    return DEFINITION_RANKING_WORDS


def load_or_extract(rulebook_path: Path, model: _Model, cache_path: Path) -> list[TermRule]:
    raw = Path(rulebook_path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        cached = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        if cached.get("sha256") == digest:
            return [TermRule(term=r["term"], words=frozenset(r["words"]), measure=r["measure"], group_by=r["group_by"],
                             min_orders=r["min_orders"]) for r in cached["rules"]]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    rules, ok = extract(raw.decode("utf-8"), model)
    if ok:
        Path(cache_path).write_text(json.dumps({"sha256": digest, "rules": [
            {**dataclasses.asdict(r), "words": sorted(r.words)} for r in rules]}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    return rules


def apply(intent: Intent, question: str) -> tuple[Intent, str | None]:
    """The intent with the measure, grouping and minimum orders of the longest rulebook term the question uses, when
    the question is a ranking; (intent, term) or (intent, None) when no term applies."""
    if intent.shape not in RANKING_SHAPES:
        return intent, None
    asked = set(normalize_lookup(question).split())
    hits = [rule for rule in _active if rule.words <= asked]
    if not hits:
        return intent, None
    rule = max(hits, key=lambda r: len(r.words))
    if rule.group_by == "date":
        patched = replace(intent, measure=rule.measure, measure2=None, group_by=None, shape="argmax_date")
    else:
        shape = intent.shape if intent.shape != "argmax_date" else "argmax_entity"
        patched = replace(intent, measure=rule.measure, measure2=None, group_by=rule.group_by, shape=shape)
    if rule.min_orders:
        patched = replace(patched, min_orders=rule.min_orders)
    return patched, rule.term
