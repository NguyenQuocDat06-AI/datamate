"""Rulebook definitions that apply to one question, shown to the intent parser.

A rulebook defines terms in its tables ("| Khách trung thành nhất | khách có nhiều ngày có mua nhất |"). A private
rulebook may define terms the training rulebook did not, so the definitions are read from the test set's own
rulebook at run time. A row applies to a question when every content word of its term appears in the question.
"""
from __future__ import annotations

from collections.abc import Callable

from bizreason.semantic.evidence import extract_definitions
from bizreason.semantic.vocabulary import DEFINITION_GENERIC_WORDS, DEFINITION_RANKING_WORDS
from bizreason.utils import normalize_lookup

MAX_DEFINITIONS = 3
MIN_TERM_WORDS = 3


def rulebook_definitions(rulebook_text: str, *, include_all: bool = False
                         ) -> list[tuple[str, str, frozenset[str]]]:
    """Preserve the tuple interface; include_all removes legacy eligibility restrictions."""
    out = []
    for definition in extract_definitions(rulebook_text).definitions:
        term, text = definition.term, definition.body
        words = normalize_lookup(term).split()
        content = frozenset(w for w in words if w not in DEFINITION_GENERIC_WORDS)
        # Ranking terms ("... nhất", "... chính", "giờ cao điểm") anywhere, and every term of an industry section: their
        # meaning is what the parser cannot guess from the training rulebook.
        ranking = len(words) >= MIN_TERM_WORDS and content & DEFINITION_RANKING_WORDS
        industry = any(span.section_depth >= 3 for span in definition.evidence) and len(content) >= 2
        if content and (include_all or ranking or industry):
            out.append((term, text.replace("**", ""), content))
    return out


def provider(rulebook_text: str, *, include_all: bool = False) -> Callable[[str], str] | None:
    """question -> a block listing the rulebook definitions whose term the question uses, or "".
    None when the rulebook defines no ranking term, so the parser is built exactly as without definitions."""
    rows = rulebook_definitions(rulebook_text, include_all=include_all)
    if not rows:
        return None
    conflicts = set(extract_definitions(rulebook_text).conflicts)

    def block(question: str) -> str:
        asked = set(normalize_lookup(question).split())
        hits = [(len(content), term, text) for term, text, content in rows if content <= asked]
        if not hits:
            return ""
        hits.sort(key=lambda item: -item[0])
        lines = [f"- {term}: {text}" for _, term, text in hits[:MAX_DEFINITIONS]]
        conflicting = sorted({normalize_lookup(term) for _, term, _ in hits} & conflicts)
        if conflicting:
            lines.insert(0, "CONFLICT: multiple definitions for " + ", ".join(conflicting)
                         + "; do not choose without an explicit precedence rule.")
        return ("This shop's rulebook defines terms the question uses; choose the measure, grouping and shape "
                "that compute the definition:\n" + "\n".join(lines))

    return block
