"""Shop knowledge card: what each table and column of a shop's layer holds, in the shop's own words.

Built once per shop from the layer database alone (no model): for every table and column a query plan may use, the
canonical gloss (plan.GLOSS), the original header, the README's own description of that column, value type, distinct
count, a few samples, how the table links to orders or products, and the README's conventions. A question then sees
only the relevant part of it (see ShopCard.select).

README parsing is structural, not lexical: markdown headings make sections, a section whose title names a file
(`File.xlsx`) describes that file's columns, and a bullet "- **X**: text" describes the column whose original header
equals X after normalize_lookup. Bullets that describe no known column are kept as notes; sections that name no file
(conventions, notes) are the shop's conventions. No word of any language is matched.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from bizreason.semantic import vocabulary
from bizreason.utils import normalize_lookup

CANONICAL = ("orders", "lines", "purchases", "deliveries", "payments")
# A column whose distinct values occur in the other side this often is a join key to it.
LINK_MIN_SHARE = 0.5
# Dimension links need annotation evidence as well as observed overlap. Name keys are normalized,
# while customer codes retain their exact spelling.
DIMENSION_LINKS = {"employee_name": ("employee", True), "customer_id": ("customer_key", False),
                   "customer_name": ("customer_name", True)}
# Canonical text columns with at most this many distinct values show their values in the card.
CANONICAL_SAMPLE_MAX = 12
SAMPLES_SHOWN = 4
# A whole src_ table is shown when any of its columns matches the question and it has at most this many columns.
SMALL_TABLE_COLUMNS = 12
# A word present in more than this share of card entries says nothing about which entry a question means.
MAX_WORD_SHARE = 0.35
CHARS_PER_TOKEN = 3.5
MIN_OPTIONAL_SHARE = 0.4

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*\S)\s*$")
_BOLD_ITEM = re.compile(r"^\*\*(.+?)\*\*\s*(?:\([^)]*\)\s*)?[:：]\s*(.*)$")
_FILE = re.compile(r"`([^`]+?\.[A-Za-z][A-Za-z0-9]{1,4})`")
_FILE_ONLY = re.compile(r"^`[^`]+`\s*(?:\([^)]*\))?\s*$")
_STOP_SOURCES = ("HOW_MANY_WORDS", "MONEY_ASK_WORDS", "SALE_WORDS", "ORDER_WORDS", "SHOP_SELF_WORDS",
                 "AVERAGE_WORDS", "PER_ORDER_WORDS", "GENERIC_FILTER_WORDS")
_STOP = {w for name in _STOP_SOURCES for text in getattr(vocabulary, name, ()) for w in normalize_lookup(text).split()}


# ----------------------------------------------------------------------------------------------- README structure

@dataclass
class Bullet:
    key: str | None  # the bold lead of "- **X**: text", or None for a plain bullet or paragraph
    text: str


@dataclass
class Section:
    title: str
    file: str | None  # the file the section's title names, when it names one
    bullets: list[Bullet] = field(default_factory=list)

    @property
    def is_file_list(self) -> bool:
        return bool(self.bullets) and all(b.key is None and _FILE_ONLY.match(b.text) for b in self.bullets)


def parse_readme(text: str) -> tuple[list[str], list[Section]]:
    """(intro paragraphs, sections) of a markdown README. A title-level heading starts no section."""
    intro: list[str] = []
    sections: list[Section] = []
    current: Section | None = None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        heading = _HEADING.match(line)
        if heading:
            if len(heading.group(1)) == 1 and not sections and current is None:
                continue  # the document title
            match = _FILE.search(heading.group(2))
            current = Section(heading.group(2).strip(), match.group(1) if match else None)
            sections.append(current)
            continue
        bullet = _BULLET.match(line)
        body = bullet.group(1) if bullet else line.strip()
        item = _BOLD_ITEM.match(body) if bullet else None
        entry = Bullet(item.group(1).strip(), item.group(2).strip()) if item else Bullet(None, body)
        if current is None:
            intro.append(body)
        elif not bullet and current.bullets and current.bullets[-1].key is not None and raw.startswith((" ", "\t")):
            current.bullets[-1].text += " " + body  # a wrapped continuation line
        else:
            current.bullets.append(entry)
    return intro, sections


def _ident(text: str) -> str:
    return normalize_lookup(text).replace(" ", "_")


def _file_stem(name: str) -> str:
    return _ident(re.sub(r"\.[A-Za-z0-9]+$", "", name))


# ------------------------------------------------------------------------------------------------------ the card

@dataclass
class Link:
    table: str
    column: str
    kind: str  # "order", "product_name", "product_id", or an annotated DIMENSION_LINKS role
    share: float
    unique: bool  # one row of the table per key value


@dataclass
class ColumnCard:
    table: str
    column: str
    gloss: str = ""
    header: str = ""
    description: str = ""
    value_type: str = ""
    role: str = ""
    distinct: int | None = None
    samples: list[Any] = field(default_factory=list)
    null_share: float = 0.0
    core: bool = False  # a canonical column: always shown

    def words(self) -> str:
        sample_text = "" if self.value_type in ("date", "number") else " ".join(str(s) for s in self.samples)
        return " ".join((self.header, self.description, self.gloss, self.column.replace("_", " "), sample_text))

    def render(self) -> str:
        info = []
        if self.value_type:
            info.append(self.value_type)
        if self.distinct is not None and not self.core:
            info.append(f"{self.distinct} values")
        if self.null_share >= 0.5:
            info.append(f"{round(100 * self.null_share)}% empty")
        if self.samples:
            info.append("e.g. " + ", ".join(json.dumps(s, ensure_ascii=False) for s in self.samples[:SAMPLES_SHOWN]))
        text = self.column
        if self.header:
            text += f' "{self.header}"'
        meaning = "; ".join(x for x in (self.gloss, self.description) if x)
        if meaning:
            text += f": {meaning}"
        if info:
            text += f" [{'; '.join(info)}]"
        return text


@dataclass
class TableCard:
    name: str
    rows: int
    file: str = ""
    source_table: str = ""
    columns: dict[str, ColumnCard] = field(default_factory=dict)
    links: list[Link] = field(default_factory=list)

    @property
    def canonical(self) -> bool:
        return self.name in CANONICAL

    def head(self) -> str:
        bits = [f"{self.rows} rows"]
        if self.file:
            bits.insert(0, f"from {self.file}")
        for link in self.links:
            if self.canonical and link.column == "order_id":
                continue
            if link.kind in DIMENSION_LINKS:
                target = "orders." + DIMENSION_LINKS[link.kind][0]
            else:
                target = "orders.order_id" if link.kind == "order" else f"lines.{link.kind}"
            bits.append(f"{link.column} links {target}" + (" (one row each)" if link.unique else " (several rows each)"))
        return f"{self.name} ({'; '.join(bits)})"


@dataclass
class ShopCard:
    tables: dict[str, TableCard]
    intro: list[str]
    conventions: list[str]
    file_notes: list[str] = field(default_factory=list)  # README column bullets of files no table came from

    def column(self, table: str, column: str) -> ColumnCard | None:
        card = self.tables.get(table)
        return card.columns.get(column) if card else None

    def link(self, table: str, kind: str | None = None) -> Link | None:
        card = self.tables.get(table)
        for item in (card.links if card else []):
            if kind is None or item.kind == kind or (kind == "product" and item.kind.startswith("product")):
                return item
        return None

    # -------------------------------------------------------------------------------------------------- rendering

    def render(self) -> str:
        """The whole card."""
        return self._render({t: set(c.columns) for t, c in self.tables.items()}, set(range(len(self._notes()))))

    def select(self, question: str, budget_tokens: int = 1200) -> str:
        """The part of the card a question needs: the canonical tables' core columns, every src_ table's head line,
        the columns and notes that share content words with the question (a whole small src_ table when one of its
        columns matches), then the conventions and other columns while the budget allows."""
        budget = int(budget_tokens * CHARS_PER_TOKEN)
        shown: dict[str, set[str]] = {t: {c for c, col in card.columns.items() if col.core}
                                      for t, card in self.tables.items()}
        notes = self._notes()
        entries: list[tuple[str, Any, str]] = []  # (kind, key, text)
        for t, card in self.tables.items():
            for c, col in card.columns.items():
                if not col.core:
                    entries.append(("column", (t, c), col.words()))
        for i, (_, text) in enumerate(notes):
            entries.append(("note", i, text))
        scores = _scores(question, [text for _, _, text in entries])
        chosen_notes: set[int] = set()

        def size() -> int:
            return len(self._render(shown, chosen_notes))

        # the core is always shown; what the question needs gets at least MIN_OPTIONAL_SHARE of the budget besides
        budget = max(budget, size() + int(MIN_OPTIONAL_SHARE * budget))

        ranked = sorted(range(len(entries)), key=lambda i: -scores[i])
        matched = [i for i in ranked if scores[i] > 0]
        rest = [i for i in ranked if scores[i] <= 0]
        # matched entries first, then conventions, then everything else, each only while it fits
        conventions = [i for i in rest if entries[i][0] == "note" and notes[entries[i][1]][0] == "convention"]
        others = [i for i in rest if i not in set(conventions)]
        for i in matched + conventions + others:
            kind, key, _ = entries[i]
            if kind == "column":
                t, c = key
                add = {c}
                card = self.tables[t]
                if i in matched and not card.canonical and len(card.columns) <= SMALL_TABLE_COLUMNS:
                    add = set(card.columns)
                before = set(shown[t])
                shown[t] |= add
                if size() > budget:
                    shown[t] = before | {c}
                    if size() > budget:
                        shown[t] = before
            else:
                chosen_notes.add(key)
                if size() > budget:
                    chosen_notes.discard(key)
        return self._render(shown, chosen_notes)

    def _notes(self) -> list[tuple[str, str]]:
        """(kind, text) of every note: the intro and conventions, then README column bullets that name no column."""
        out = [("convention", text) for text in self.intro]
        out += [("convention", text) for text in self.conventions]
        out += [("file", text) for text in self.file_notes]
        return out

    def _render(self, shown: dict[str, set[str]], notes: set[int]) -> str:
        lines = ["Tables (write another table's column as table.column):"]
        all_notes = self._notes()
        for t, card in self.tables.items():
            columns = [card.columns[c] for c in card.columns if c in shown.get(t, set())]
            lines.append("- " + card.head() + (":" if columns else ""))
            for col in columns:
                lines.append("  - " + col.render())
        conventions = [text for i, (kind, text) in enumerate(all_notes) if i in notes and kind in ("convention", "file")]
        if conventions:
            lines.append("Shop documentation (README):")
            lines += ["- " + text for text in conventions]
        return "\n".join(lines)


def _words(text: str) -> list[str]:
    return [w for w in normalize_lookup(text).split() if len(w) > 1 and not (w.isdigit() and len(w) <= 2)]


def _scores(question: str, texts: list[str]) -> list[float]:
    """How much each text shares the question's content words: IDF of shared words, plus shared word pairs."""
    q_words = [w for w in _words(question) if w not in _STOP]
    q_set = set(q_words)
    q_pairs = {(a, b) for a, b in zip(q_words, q_words[1:])}
    docs = [_words(t) for t in texts]
    df: dict[str, int] = {}
    for words in docs:
        for w in set(words) & q_set:
            df[w] = df.get(w, 0) + 1
    n = max(len(docs), 1)
    scores = []
    for words in docs:
        shared = (set(words) & q_set)
        score = sum(math.log((n + 1) / (df[w] + 0.5)) for w in shared if df[w] <= max(1, MAX_WORD_SHARE * n))
        score += 1.5 * len({(a, b) for a, b in zip(words, words[1:])} & q_pairs)
        scores.append(score)
    return scores


# -------------------------------------------------------------------------------------------------- building it

def _tables(db: sqlite3.Connection) -> list[str]:
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return [t for t in CANONICAL if t in names] + sorted(n for n in names if n.startswith("src_"))


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _cols(db: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in db.execute(f"PRAGMA table_info({_q(table)})")]


def _catalog_rows(db: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = db.execute("SELECT table_name, column_name, source_table, source_header, role, value_type, "
                          "distinct_count, sample_values, join_key_to FROM column_catalog").fetchall()
    except sqlite3.Error:
        return []
    out = []
    for t, c, src, header, role, kind, distinct, samples, join in rows:
        try:
            sample = json.loads(samples) if samples else []
        except (TypeError, ValueError):
            sample = []
        out.append({"table": t, "column": c, "source_table": src or "", "header": header or "", "role": role or "",
                    "type": kind or "", "distinct": distinct, "samples": sample if isinstance(sample, list) else [],
                    "join": join})
    return out


def _readme(db: sqlite3.Connection) -> str:
    try:
        row = db.execute("SELECT text FROM shop_docs WHERE key='readme'").fetchone()
    except sqlite3.Error:
        return ""
    return str(row[0]) if row and row[0] else ""


def _is_twin(column: str, columns: set[str]) -> bool:
    """xn_/xd_ numeric and date twins of a kept column, and *_raw copies: the compiler uses them by itself."""
    if column.endswith("_raw"):
        return True
    if column.startswith(("xn_", "xd_")):
        base = column[3:]
        return f"x_{base}" in columns or base in columns
    return False


def _ensure_lookup(db: sqlite3.Connection) -> None:
    db.create_function("lookup", 1, lambda v: None if v is None else normalize_lookup(str(v)), deterministic=True)


def _distinct_texts(db: sqlite3.Connection, table: str, column: str) -> list[str]:
    return [str(r[0]) for r in db.execute(
        f"SELECT DISTINCT CAST({_q(column)} AS TEXT) FROM {_q(table)} "
        f"WHERE NULLIF(TRIM(CAST({_q(column)} AS TEXT)), '') IS NOT NULL")]


def _unique(db: sqlite3.Connection, table: str, column: str, normalize: bool) -> bool:
    if normalize:
        values = [r[0] for r in db.execute(f"SELECT CAST({_q(column)} AS TEXT) FROM {_q(table)} "
                                           f"WHERE {_q(column)} IS NOT NULL")]
        keys = [normalize_lookup(v) for v in values]
        return bool(keys) and len(set(keys)) == len(keys)
    distinct, filled = db.execute(f"SELECT COUNT(DISTINCT {_q(column)}), COUNT({_q(column)}) FROM {_q(table)}"
                                  ).fetchone()
    return bool(filled) and distinct == filled


class _Targets:
    """The order ids and the product names and ids of sale lines, read once per shop."""

    def __init__(self, db: sqlite3.Connection) -> None:
        names = set(_tables(db))
        self.orders: set[str] = set()
        self.product_names: set[str] = set()
        self.product_ids: set[str] = set()
        self.dimensions: dict[str, set[str]] = {}
        self.customer_names: dict[str, set[str]] = {}
        if "orders" in names:
            self.orders = {str(r[0]) for r in db.execute("SELECT DISTINCT order_id FROM orders "
                                                         "WHERE order_id IS NOT NULL")}
            order_cols = set(_cols(db, "orders"))
            for role, (column, normalize) in DIMENSION_LINKS.items():
                if column in order_cols:
                    values = _distinct_texts(db, "orders", column)
                    self.dimensions[role] = {normalize_lookup(v) if normalize else v for v in values}
            if {"customer_key", "customer_name"} <= order_cols:
                for key, name in db.execute("SELECT DISTINCT customer_key,customer_name FROM orders "
                                             "WHERE customer_key IS NOT NULL AND customer_name IS NOT NULL"):
                    if normalize_lookup(str(name)):
                        self.customer_names.setdefault(str(key), set()).add(normalize_lookup(str(name)))
        line_cols = set(_cols(db, "lines")) if "lines" in names else set()
        if "product_name" in line_cols:
            self.product_names = {normalize_lookup(r[0]) for r in db.execute(
                "SELECT DISTINCT product_name FROM lines WHERE product_name IS NOT NULL")}
        if "product_id" in line_cols:
            self.product_ids = {str(r[0]) for r in db.execute(
                "SELECT DISTINCT product_id FROM lines WHERE product_id IS NOT NULL")}


def _share(values: list[str], target: set[str]) -> float:
    if len(values) < 2 or not target:
        return 0.0
    return sum(v in target for v in values) / len(values)


def detect_links(db: sqlite3.Connection, table: str, columns: list[str],
                 catalog_join: dict[str, str | None] | None = None, targets: _Targets | None = None,
                 catalog_roles: dict[str, str] | None = None) -> list[Link]:
    """How a table links to orders (a column of order ids) and to the products of sale lines (a column of their
    names or ids): the column whose distinct values occur there at least LINK_MIN_SHARE of the time."""
    targets = targets or _Targets(db)
    if table in CANONICAL:
        if table != "orders" and "order_id" in columns and targets.orders:
            return [Link(table, "order_id", "order", 1.0, _unique(db, table, "order_id", False))]
        return []
    best: dict[str, Link] = {}
    has_dimension_roles = any(role in DIMENSION_LINKS for role in (catalog_roles or {}).values())
    for column in columns:
        if column.startswith(("xn_", "xd_")) or column.endswith("_raw"):
            continue
        if has_dimension_roles and (catalog_roles or {}).get(column) not in ("order_id", "product_id", "product_name"):
            continue  # a commission/rate in a staff list is not an order or product code
        values = _distinct_texts(db, table, column)
        shares = [("order", 1.0 if (catalog_join or {}).get(column) == "orders.order_id"
                   else _share(values, targets.orders), False),
                  ("product_name", _share(sorted({normalize_lookup(v) for v in values}), targets.product_names), True),
                  ("product_id", _share(values, targets.product_ids), False)]
        for kind, share, normalize in shares:
            if share >= LINK_MIN_SHARE and (kind not in best or share > best[kind].share):
                best[kind] = Link(table, column, kind, share, _unique(db, table, column, normalize))
    out = [best[k] for k in ("order", "product_id", "product_name") if k in best]
    if "product_id" in best and "product_name" in best:  # one product link, by id when both match
        out = [x for x in out if x.kind != "product_name"]
    for kind, (_, normalize) in DIMENSION_LINKS.items():
        candidates = []
        target = targets.dimensions.get(kind, set())
        if not target:
            continue
        for column in columns:
            if (catalog_roles or {}).get(column) != kind:
                continue
            values = _distinct_texts(db, table, column)
            keys = {normalize_lookup(v) if normalize else v for v in values}
            if not keys or (normalize and not any(any(ch.isalpha() for ch in v) for v in keys)):
                continue
            share = sum(key in target for key in keys) / len(keys)
            if share >= LINK_MIN_SHARE:
                candidates.append(Link(table, column, kind, share, _unique(db, table, column, normalize)))
        candidates.sort(key=lambda link: link.share, reverse=True)
        if candidates and (len(candidates) == 1 or candidates[0].share > candidates[1].share):
            out.append(candidates[0])
    customer_id = next((link for link in out if link.kind == "customer_id"), None)
    customer_name = next((link for link in out if link.kind == "customer_name"), None)
    if customer_id and customer_name:
        # Codes and names describe the same customer dimension. Prefer the code, but only
        # after checking that a row naming both does not contradict the recorded orders.
        contradictory = False
        for key, name in db.execute(f"SELECT {_q(customer_id.column)}, {_q(customer_name.column)} "
                                    f"FROM {_q(table)}"):
            known = targets.customer_names.get(str(key), set()) if key is not None else set()
            if known and name is not None and normalize_lookup(str(name)) \
                    and normalize_lookup(str(name)) not in known:
                contradictory = True
                break
        out = [link for link in out if link.kind != "customer_name"
               and not (contradictory and link.kind == "customer_id")]
    return out


def _canonical_columns(db: sqlite3.Connection, table: str, columns: list[str], rows: int,
                       gloss: dict[str, str]) -> dict[str, ColumnCard]:
    out: dict[str, ColumnCard] = {}
    for column in columns:
        if column not in gloss:
            continue
        filled = db.execute(f"SELECT COUNT({_q(column)}) FROM {_q(table)}").fetchone()[0]
        if not filled:
            continue  # a canonical field this shop never fills
        col = ColumnCard(table, column, gloss=gloss[column], core=True, null_share=1 - filled / rows)
        distinct = db.execute(f"SELECT COUNT(DISTINCT {_q(column)}) FROM {_q(table)}").fetchone()[0]
        kind = db.execute(f"SELECT typeof({_q(column)}) FROM {_q(table)} WHERE {_q(column)} IS NOT NULL "
                          "LIMIT 1").fetchone()[0]
        if kind == "text" and distinct <= CANONICAL_SAMPLE_MAX and column not in ("date", "ts") \
                and not column.endswith(("_id", "_key")):
            col.samples = [r[0] for r in db.execute(
                f"SELECT {_q(column)}, COUNT(*) AS n FROM {_q(table)} WHERE {_q(column)} IS NOT NULL "
                f"GROUP BY {_q(column)} ORDER BY n DESC LIMIT {CANONICAL_SAMPLE_MAX}")]
        out[column] = col
    return out


def build_card(db: sqlite3.Connection) -> ShopCard:
    """The knowledge card of the shop whose layer db is open (read only is enough)."""
    from bizreason.semantic.plan import GLOSS  # plan imports this module

    _ensure_lookup(db)
    catalog = _catalog_rows(db)
    by_table: dict[str, dict[str, dict[str, Any]]] = {}
    for row in catalog:
        by_table.setdefault(row["table"], {})[row["column"]] = row
    tables: dict[str, TableCard] = {}
    targets = _Targets(db)
    for t in _tables(db):
        columns = _cols(db, t)
        rows = db.execute(f"SELECT COUNT(*) FROM {_q(t)}").fetchone()[0]
        if not columns or not rows:
            continue
        card = TableCard(t, rows)
        names = set(columns)
        if t in CANONICAL:
            card.columns.update(_canonical_columns(db, t, columns, rows, GLOSS.get(t, {})))
        meta = by_table.get(t, {})
        for column, col in card.columns.items():  # a canonical column the catalog describes (its source header)
            if column in meta:
                col.header, col.role = meta[column]["header"], meta[column]["role"]
                card.source_table = card.source_table or meta[column]["source_table"]
        for column in columns:
            if column in card.columns or _is_twin(column, names):
                continue
            row = meta.get(column)
            if row is None and t in CANONICAL:
                continue  # an internal column of the layer
            filled = db.execute(f"SELECT COUNT(NULLIF(TRIM(CAST({_q(column)} AS TEXT)), '')) FROM {_q(t)}"
                                ).fetchone()[0]
            col = ColumnCard(t, column, null_share=1 - filled / rows)
            if row is not None:
                col.header, col.value_type, col.role = row["header"], row["type"], row["role"]
                col.distinct, col.samples = row["distinct"], row["samples"][:SAMPLES_SHOWN]
                if col.value_type == "date":
                    col.samples = [str(v)[:10] for v in col.samples]
                card.source_table = card.source_table or row["source_table"]
            else:
                col.distinct = db.execute(f"SELECT COUNT(DISTINCT {_q(column)}) FROM {_q(t)}").fetchone()[0]
                col.samples = [r[0] for r in db.execute(
                    f"SELECT DISTINCT {_q(column)} FROM {_q(t)} WHERE {_q(column)} IS NOT NULL LIMIT {SAMPLES_SHOWN}")]
            if f"xd_{column[2:] if column.startswith('x_') else column}" in names and not col.value_type:
                col.value_type = "date"
            card.columns[column] = col
        card.links = detect_links(db, t, columns, {c: r["join"] for c, r in meta.items()}, targets,
                                  catalog_roles={c: r["role"] for c, r in meta.items()})
        tables[t] = card
    intro, conventions, file_notes = _attach_readme(_readme(db), tables, catalog)
    return ShopCard(tables, intro, conventions, file_notes)


def _attach_readme(text: str, tables: dict[str, TableCard],
                   catalog: list[dict[str, Any]]) -> tuple[list[str], list[str], list[str]]:
    """Put each README column description on its column; return (intro, conventions, file notes). A described
    header found in no column is kept as a file note ("(File.xlsx) Header: text")."""
    intro, sections = parse_readme(text)
    by_header: dict[str, list[dict[str, Any]]] = {}
    for row in catalog:
        if row["table"] in tables and row["column"] in tables[row["table"]].columns:
            by_header.setdefault(normalize_lookup(row["header"]), []).append(row)
    # the file a source table came from: its identifier starts with the file's stem
    files = [s.file for s in sections if s.file]
    for card in tables.values():
        source = card.source_table or ""
        stems = [f for f in files if source.startswith(_file_stem(f))]
        if stems:
            card.file = max(stems, key=len)
    conventions: list[str] = []
    file_notes: list[str] = []
    for section in sections:
        if section.is_file_list:
            continue
        stem = _file_stem(section.file) if section.file else None
        for bullet in section.bullets:
            text_line = f"{bullet.key}: {bullet.text}" if bullet.key else bullet.text
            rows = by_header.get(normalize_lookup(bullet.key), []) if bullet.key else []
            if stem is not None:
                in_file = [r for r in rows if r["source_table"].startswith(stem)]
                rows = in_file or ([] if any(r["source_table"] for r in rows) else rows)
            for row in rows:
                col = tables[row["table"]].columns[row["column"]]
                if not col.description:
                    col.description = bullet.text
            if section.file is None:
                conventions.append(text_line)
            elif not rows:
                file_notes.append(f"({section.file}) {text_line}")
    return intro, conventions, file_notes
