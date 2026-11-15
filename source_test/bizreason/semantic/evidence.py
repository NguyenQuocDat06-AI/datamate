"""Structural definition candidates with exact, verifiable document provenance."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata

from bizreason.semantic.vocabulary import DEFINITION_PROSE_CONNECTORS
from bizreason.utils import normalize_lookup

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_CONNECTOR = re.compile(r"\s+(?:" + "|".join(
    re.escape(word) for word in sorted(DEFINITION_PROSE_CONNECTORS, key=len, reverse=True)
) + r")\s+", re.IGNORECASE)
_PAIR = re.compile(r"\s*[:=]\s*")


def _clean(value: str) -> str:
    return " ".join(value.replace("**", "").replace("`", "").replace(r"\|", "|").split())


def _body_key(value: str) -> str:
    # Preserve arithmetic operators and decimal separators: a+b and a-b differ.
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split()).rstrip(".")


@dataclass(frozen=True)
class EvidenceSpan:
    source_id: str
    document_sha256: str
    start: int
    end: int
    quote: str
    section_depth: int = 0

    def validate(self, document: str) -> bool:
        return (0 <= self.start < self.end <= len(document)
                and document[self.start:self.end] == self.quote
                and hashlib.sha256(document.encode("utf-8")).hexdigest() == self.document_sha256)


@dataclass(frozen=True)
class DefinitionEvidence:
    definition_id: str
    term: str
    body: str
    evidence: tuple[EvidenceSpan, ...]


@dataclass(frozen=True)
class EvidenceDocument:
    source_id: str
    document_sha256: str
    definitions: tuple[DefinitionEvidence, ...]
    conflicts: tuple[str, ...]


def _cells(line: str) -> list[str]:
    """Markdown cells respecting escaped pipes and pipes within inline code."""
    out, current = [], []
    escaped, code = False, False
    for char in line.strip():
        if escaped:
            current.extend(("\\", char))
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "`":
            code = not code
            current.append(char)
        elif char == "|" and not code:
            out.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    out.append("".join(current).strip())
    if out and not out[0]:
        out.pop(0)
    if out and not out[-1]:
        out.pop()
    return out


def _table_separator(line: str) -> bool:
    cells = _cells(line)
    return len(cells) >= 2 and all(_SEPARATOR.fullmatch(c.replace(" ", "")) for c in cells)


def _pair(value: str) -> tuple[str, str] | None:
    candidates = [m for m in (_PAIR.search(value), _CONNECTOR.search(value)) if m]
    if not candidates:
        return None
    match = min(candidates, key=lambda m: m.start())
    term, body = _clean(value[:match.start()]), _clean(value[match.end():])
    if not term or not body or len(term) > 120 or len(term.split()) > 12:
        return None
    if not normalize_lookup(term) or not any(c.isalpha() for c in term):
        return None
    return term, body


def extract_definitions(text: str, source_id: str = "rulebook") -> EvidenceDocument:
    """Extract candidates at any heading depth; spans use Python string offsets.

    Equivalent repeats share an ID and retain all source spans. Conflicting bodies
    remain separate. This parser does not certify business meaning or precedence.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    lines = text.splitlines(keepends=True)
    offsets, position = [], 0
    for line in lines:
        offsets.append(position)
        position += len(line)
    found: dict[str, tuple[str, str, list[EvidenceSpan]]] = {}
    terms: dict[str, set[str]] = {}
    depth, fence, index = 0, None, 0
    table_open = False
    while index < len(lines):
        line = lines[index].rstrip("\r\n")
        stripped = line.strip()
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            char = marker[1][0]
            fence = None if fence == char else char if fence is None else fence
            table_open = False
            index += 1
            continue
        if fence or not stripped:
            table_open = False
            index += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            depth = len(heading[1])
            table_open = False
            index += 1
            continue
        start, end = offsets[index], offsets[index] + len(line)
        pair = None
        table_header = ("|" in stripped and index + 1 < len(lines)
                        and _table_separator(lines[index + 1]))
        if stripped.startswith("|") or table_header or (table_open and "|" in stripped):
            table_open = True
            if (_table_separator(line) or
                    (index + 1 < len(lines) and _table_separator(lines[index + 1]))):
                index += 1
                continue
            cells = _cells(line)
            if len(cells) == 2:
                pair = (_clean(cells[0]), _clean(cells[1]))
        else:
            table_open = False
            bullet = _BULLET.match(line)
            pair = _pair(line[bullet.end():] if bullet else stripped)
            if pair:
                term, body = pair
                following = index + 1
                while following < len(lines):
                    continuation = lines[following].rstrip("\r\n")
                    if (not continuation.strip() or _HEADING.match(continuation)
                            or _BULLET.match(continuation)
                            or continuation.lstrip().startswith(("|", "```", "~~~"))
                            or (bullet and not continuation.startswith((" ", "\t")))
                            or _pair(continuation.strip())):
                        break
                    body += " " + _clean(continuation)
                    end = offsets[following] + len(continuation)
                    following += 1
                pair = term, body
                index = following - 1
        if pair and all(pair) and len(pair[0]) <= 120 and any(c.isalpha() for c in pair[0]):
            term, body = pair
            term_key, body_key = normalize_lookup(term), _body_key(body)
            identifier = hashlib.sha256(json.dumps([term_key, body_key], ensure_ascii=False).encode()).hexdigest()
            span = EvidenceSpan(source_id, digest, start, end, text[start:end], depth)
            if identifier not in found:
                found[identifier] = term, body, []
            found[identifier][2].append(span)
            terms.setdefault(term_key, set()).add(body_key)
        index += 1
    definitions = tuple(DefinitionEvidence(key, term, body, tuple(spans))
                        for key, (term, body, spans) in found.items())
    return EvidenceDocument(source_id, digest, definitions,
                            tuple(sorted(term for term, bodies in terms.items() if len(bodies) > 1)))
