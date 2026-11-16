from __future__ import annotations

import functools
import itertools
import re
import sqlite3
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from bizreason.contracts import BusinessContext, ColumnMeta, Diagnostic, SourceRef, TableMeta
from bizreason.data import schema
from bizreason.data.parsing import (
    ParseStatus, blank, classify_date_column, infer_date_order, parse_date, parse_date_ordered, parse_number,
    parse_status,
)
from bizreason.data.layout import ArithmeticSubtotals, BlockSums, has_record_evidence, is_note, is_summary
from bizreason.data.row_audit import create_audit, record_audit
from bizreason.data.readers import RawSheet, read_sheets
from bizreason.data.identities import (
    canonicalize_products,
    canonicalize_customers,
    clean_text,
    refresh_examples,
    is_retail_customer,
)
from bizreason.data.notes_facets import derive_status_from_notes
from bizreason.data.eligibility import propagate_order_returns
from bizreason.data.ledger_schema import contextual_roles
from bizreason.data.vehicles import derive_vehicle_columns
from bizreason.data.schema import (
    DATE_ROLES,
    NUMERIC_ROLES,
    MONEY_ROLES,
    column_descriptions,
    described_role,
    infer_grain,
    infer_role,
    sheet_year,
)
from bizreason.rules import RuleRegistry
from bizreason.data.vocabulary import (
    CURRENCY_ANCHOR_RE,
    DECLARED_END_RE,
    DECLARED_START_RE,
    NOTE_HEADER,
)
from bizreason.utils import (
    atomic_json,
    digest,
    dumps,
    file_hash,
    identifier,
    normalize_lookup,
    quote_identifier,
    writable_path,
)

PARSER_VERSION = "1.4.0"
SUPPORTED_SUFFIXES = {".csv", ".xlsx", ".sqlite", ".sqlite3", ".db"}
GROUP_ROLES = {
    "order_id",
    "ordered_at",
    "status",
    "customer_id",
    "customer_name",
    "payment_method",
    "total_due",
}


HEADER_LOOKAHEAD = 10  # Following prefix rows that show what a column holds below a candidate header.
HEADER_TEXT_LIMIT = 60  # Header cells are short labels, not sentences.
HEADER_MIN_SCORE = 2.5  # Below this the sheet has no plausible header row.
HEADER_STACKED_PENALTY = 1.0  # A solid text row directly under another solid text row is data.


@functools.lru_cache(maxsize=16)
def _readme_terms(readme: str) -> str:
    return f" {normalize_lookup(readme)} "


def _in_readme(text: str, terms: str) -> bool:
    normalized = normalize_lookup(text)
    return len(normalized) >= 3 and f" {normalized} " in terms


def _cell_kind(cell: Any) -> str | None:
    if blank(cell):
        return None
    if isinstance(cell, (int, float, Decimal)) and not isinstance(cell, bool):
        return "number"
    if isinstance(cell, date):
        return "date"
    if isinstance(cell, str):
        if parse_number(cell).status in {ParseStatus.OK, ParseStatus.AMBIGUOUS}:
            return "number"
        if parse_date(cell).status in {ParseStatus.OK, ParseStatus.AMBIGUOUS}:
            return "date"
        return "text"
    return "other"


def _contrast_share(index: int, following: tuple[tuple[Any, ...], ...]) -> float:
    """Share of the values below a label column that are numbers or dates."""
    below = [
        _cell_kind(row[index])
        for row in following
        if index < len(row) and not blank(row[index])
    ]
    if len(below) < 2:
        return 0.0
    return sum(kind in {"number", "date"} for kind in below) / len(below)


def header_score(
    row: tuple[Any, ...],
    readme: str,
    following: tuple[tuple[Any, ...], ...] = (),
    width: int | None = None,
) -> float:
    """Structural header score: short text, fill, distinct labels, numbers below labels.

    ``following`` holds the rows after ``row`` in the prefix and ``width`` the widest
    non-blank row in the prefix. Known header names only add a small bonus.
    """
    present = [(index, cell) for index, cell in enumerate(row) if not blank(cell)]
    if len(present) < 2:
        return -1
    count = len(present)
    cells = [str(cell).strip() for _, cell in present]
    short = [
        (index, text)
        for (index, cell), text in zip(present, cells)
        if _cell_kind(cell) == "text" and len(text) <= HEADER_TEXT_LIMIT
    ]
    text = len(short) / count
    if text < 0.5:
        return -1  # Mostly numbers or dates: a data row, not a header.
    fill = count / max(width or count, count)
    unique = len(set(cells)) / count
    long = sum(len(cell) > 80 for cell in cells) / count
    labels = sum(infer_role(cell, readme) is not None for cell in cells) / count
    terms = _readme_terms(readme)
    mentioned = sum(_in_readme(value, terms) for _, value in short) / count
    contrast = sum(_contrast_share(index, following) for index, _ in short) / count
    return (
        fill
        + text
        + unique
        + 3.0 * contrast
        + 1.5 * mentioned
        + 0.5 * labels
        - 2.0 * long
    )


def _solid_text_row(row: tuple[Any, ...], width: int) -> bool:
    present = [cell for cell in row if not blank(cell)]
    return (
        len(present) >= 2
        and len(present) >= 0.8 * max(width, len(present))
        and all(
            _cell_kind(cell) == "text" and len(str(cell).strip()) <= HEADER_TEXT_LIMIT
            for cell in present
        )
    )


def choose_header_row(prefix: list[tuple[Any, ...]], readme: str) -> tuple[int, float]:
    """Return the index and score of the best header candidate; earliest row wins ties."""
    width = max((sum(not blank(cell) for cell in row) for row in prefix), default=0)
    scored = []
    for index, row in enumerate(prefix):
        following = tuple(prefix[index + 1 : index + 1 + HEADER_LOOKAHEAD])
        score = header_score(row, readme, following, width)
        # A header usually follows a title, blank or other non-table row. A text row under
        # another text row is more likely the first data row.
        if (
            score > 0
            and index > 0
            and _solid_text_row(row, width)
            and _solid_text_row(prefix[index - 1], width)
        ):
            score -= HEADER_STACKED_PENALTY
        scored.append((score, index))
    score, position = max(scored, key=lambda item: (item[0], -item[1]))
    return position, score


def _alias_shape_score(row: tuple[Any, ...], readme: str) -> float:
    """Previous alias-driven score, kept for region splits where known names are present."""
    cells = [str(cell).strip() for cell in row if not blank(cell)]
    if len(cells) < 2:
        return -1
    labels = sum(infer_role(cell, readme) is not None for cell in cells) / len(cells)
    texts = sum(isinstance(cell, str) for cell in row if not blank(cell)) / len(cells)
    unique = len(set(cells)) / len(cells)
    long = sum(len(cell) > 80 for cell in cells) / len(cells)
    return 4 * labels + texts + unique - 2 * long


def _readme_region_header(row: tuple[Any, ...], readme: str, width: int) -> bool:
    """A region header without known names: strong text-only, high-fill, README-confirmed row."""
    present = [cell for cell in row if not blank(cell)]
    if len(present) < 2:
        return False
    if any(
        _cell_kind(cell) != "text" or len(str(cell).strip()) > HEADER_TEXT_LIMIT
        for cell in present
    ):
        return False
    if len(present) < 0.8 * max(width, len(present)):
        return False
    terms = _readme_terms(readme)
    return sum(_in_readme(str(cell).strip(), terms) for cell in present) >= 2


_UNIT_STATEMENT = re.compile(
    r"\b(?:don vi tinh|don vi|dvt|unit|units)\s+\b(nghin|ngan(?!\s+hang)|thousand|trieu|million)\b"
)  # Applied to normalize_lookup text, so punctuation is already a single space.
AMOUNT_SCALES = {
    "nghin": 1_000,
    "ngan": 1_000,
    "thousand": 1_000,
    "trieu": 1_000_000,
    "million": 1_000_000,
}


def _amount_scale(rows: list[tuple[Any, ...]]) -> int | None:
    """Money unit stated in the preamble, e.g. "don vi tinh: nghin dong". None if absent or conflicting."""
    factors: set[int] = set()
    for row in rows:
        text = normalize_lookup(" ".join(str(cell) for cell in row if not blank(cell)))
        factors.update(AMOUNT_SCALES[match.group(1)] for match in _UNIT_STATEMENT.finditer(text))
    return factors.pop() if len(factors) == 1 else None


def _multi_tier_labels(upper: tuple[Any, ...], header: tuple[Any, ...]) -> tuple[Any, ...] | None:
    """Prefix each header label with the group label above it; None if the row above is not a group row."""
    upper_present = [index for index, cell in enumerate(upper) if not blank(cell)]
    # One cell is a title. Labels over every header column are a second header row, not groups.
    if len(upper_present) < 2:
        return None
    if any(
        _cell_kind(upper[index]) != "text" or len(str(upper[index]).strip()) > HEADER_TEXT_LIMIT
        for index in upper_present
    ):
        return None
    cover = (*upper, *([None] * max(0, len(header) - len(upper))))
    if all(not blank(cover[index]) for index, cell in enumerate(header) if not blank(cell)):
        return None
    labels: list[Any] = []
    group: str | None = None
    for index, cell in enumerate(header):
        if not blank(cover[index]):
            group = str(cover[index]).strip()  # Carries across blank cells to the right.
        if blank(cell) or group is None or _cell_kind(cell) != "text":
            labels.append(cell)
        else:
            labels.append(f"{group}_{str(cell).strip()}")
    return tuple(labels) if labels != list(header) else None


def _is_repeated_header(row: list[Any], headers: tuple[Any, ...], share: float = 0.5) -> bool:
    """A page-break copy of the header: ``share`` of its labels recur and the row holds no number."""
    if any(_cell_kind(cell) == "number" for cell in row):
        return False
    positions = [index for index, cell in enumerate(headers) if not blank(cell)]
    if len(positions) < 2:
        return False
    matches = sum(
        index < len(row)
        and not blank(row[index])
        and normalize_lookup(row[index]) == normalize_lookup(headers[index])
        for index in positions
    )
    return matches >= share * len(positions)


def unique_columns(headers: tuple[Any, ...]) -> list[str]:
    names = []
    used: set[str] = set()
    for index, header in enumerate(headers, 1):
        base = identifier(str(header)) if not blank(header) else f"unnamed_{index}"
        name = base
        suffix = 2
        while name in used or name.startswith("__"):
            name = f"{base}_{suffix}"
            suffix += 1
        names.append(name)
        used.add(name)
    return names


def infer_locale(values: list[Any], readme: str, *, currency: bool = False) -> str:
    text = normalize_lookup(readme)
    if "dau phay thap phan" in text or "decimal comma" in text:
        return "vi"
    if "decimal point" in text or "dau cham thap phan" in text:
        return "en"
    strings = [str(value).strip() for value in values if isinstance(value, str)]
    # Mixed native/exported values can establish the column locale without guessing
    # from one ambiguous cell: e.g. native 45000 occurs alongside text "45,000".
    anchors = {
        Decimal(str(value))
        for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    anchors.update(Decimal(value) for value in strings if re.fullmatch(r"-?\d+", value))
    if currency:
        for value in strings:
            if re.search(CURRENCY_ANCHOR_RE, value, re.IGNORECASE):
                parsed = parse_number(value, unit="VND")
                if parsed.status == ParseStatus.OK:
                    anchors.add(parsed.value)
    votes = {}
    for separator, locale in [(".", "vi"), (",", "en")]:
        grouped = [
            value
            for value in strings
            if re.fullmatch(r"-?\d{1,3}(?:" + re.escape(separator) + r"\d{3})+", value)
        ]
        matches = sum(Decimal(value.replace(separator, "")) in anchors for value in grouped)
        if matches >= 3 and matches >= len(grouped) * 0.5:
            votes[locale] = matches
    if len(votes) == 1:
        return next(iter(votes))
    if any(re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+,\d+", value) for value in strings):
        return "vi"
    if any(re.fullmatch(r"-?\d{1,3}(?:,\d{3})+\.\d+", value) for value in strings):
        return "en"
    for separator, locale in [(".", "vi"), (",", "en")]:
        if any(
            re.fullmatch(r"-?\d{1,3}(?:" + re.escape(separator) + r"\d{3}){2,}", value)
            for value in strings
        ):
            return locale
        # Plain integers such as 500 do not contradict the thousands spelling
        # 4,400. Compare separator conventions among formatted values only.
        numeric_strings = [
            value for value in strings
            if re.fullmatch(r"-?[\d.,]+", value) and ("," in value or "." in value)
        ]
        grouped = [
            value
            for value in numeric_strings
            if re.fullmatch(r"-?\d{1,3}(?:" + re.escape(separator) + r"\d{3})+", value)
        ]
        if (
            currency
            and len(grouped) >= 20
            and len(set(grouped)) >= 4
            and len(grouped) >= 0.95 * len(numeric_strings)
        ):
            return locale
    return "unknown"


def _sum_locales(rows: list[tuple[int, tuple[Any, ...]]], locales: list[str], readme: str) -> list[str]:
    """Separator reading of each column for subtotal arithmetic. A column whose role gave no reading is read as an
    amount column would be; one that still has none takes the reading most columns share."""
    sample = [row for _, row in rows[:2000]]
    out = [
        locale if locale != "unknown"
        else infer_locale([row[index] for row in sample if len(row) > index], readme, currency=True)
        for index, locale in enumerate(locales)
    ]
    votes = Counter(locale for locale in out if locale != "unknown")
    if len(votes) == 1 or (votes and votes.most_common(2)[0][1] > votes.most_common(2)[-1][1]):
        common = votes.most_common(1)[0][0]
        out = [common if locale == "unknown" else locale for locale in out]
    return out


def _date_column_orders(
    rows: list[tuple[int, tuple[Any, ...]]], roles: list[str | None], width: int
) -> dict[int, str]:
    """Day/month order per date column of a region.

    A date-role column is read day-first (the parser default) unless its own cells prove month-first. A roleless
    column is added only when classify_date_column recognizes it as dates (serials mixed with text dates, say); the
    order the shop's date-role columns prove is its hint. Columns carrying numeric roles are never touched.
    """
    sample = rows[:20000]
    columns = {
        index: [row[index] for _, row in sample if len(row) > index and not blank(row[index])]
        for index in range(width)
    }
    orders: dict[int, str] = {}
    proven: list[str] = []
    for index, role in enumerate(roles[:width]):
        if role in DATE_ROLES:
            order = infer_date_order(columns[index])
            if order == "mdy":
                orders[index] = "mdy"
            if order in {"dmy", "mdy"}:
                proven.append(order)
    hint = proven[0] if proven and len(set(proven)) == 1 else "ambiguous"
    for index, role in enumerate(roles[:width]):
        if role is None and columns[index]:
            order = classify_date_column(columns[index], hint=hint)
            if order is not None:
                orders[index] = order
    return orders


def _stored_number(number: Decimal) -> int | str:
    if number == number.to_integral_value() and -(2**63) <= number < 2**63:
        return int(number)
    return str(number)  # Fractional quantities stay exact; SQL decimal helpers are available.


class BusinessBuilder:
    def __init__(
        self,
        cache_root: Path,
        *,
        rule_hash: str,
        inheritance_allowed: bool = True,
        rules: RuleRegistry | None = None,
    ):
        self.cache_root = writable_path(cache_root)
        self.rule_hash = rule_hash
        self.inheritance_allowed = inheritance_allowed
        self.rules = rules
        package = Path(__file__).resolve().parents[1]
        implementation_files = sorted((package / "data").glob("*.py")) + [
            package / "contracts.py",
            package / "rules.py",
        ]
        self.implementation_hash = digest(
            {path.relative_to(package).as_posix(): file_hash(path) for path in implementation_files}
        )

    def prepare(self, business_dir: Path) -> BusinessContext:
        business_dir = business_dir.resolve()
        if not business_dir.is_dir():
            raise FileNotFoundError("Business directory does not exist")
        readme_path = business_dir / "README.md"
        readme = readme_path.read_text(encoding="utf-8-sig") if readme_path.exists() else ""
        paths = sorted(
            path
            for path in business_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        )
        manifest = {path.relative_to(business_dir).as_posix(): file_hash(path) for path in paths}
        if readme_path.exists():
            manifest["README.md"] = file_hash(readme_path)
        revision = digest(
            {
                "files": manifest,
                "rule_hash": self.rule_hash,
                "parser": PARSER_VERSION,
                "inheritance": self.inheritance_allowed,
                "derive_units": bool(self.rules),
                "implementation": self.implementation_hash,
                # Alias-off runs change the live mapping without changing any source file.
                # Read it at prepare time so a builder cannot reuse another mode's cache.
                "header_roles": dict(schema.ROLE_LOOKUP),
            }
        )
        directory = writable_path(self.cache_root / identifier(business_dir.name) / revision[:20])
        metadata = directory / "catalog.json"
        database = directory / "business.sqlite"
        if metadata.exists() and database.exists():
            context = BusinessContext.model_validate_json(metadata.read_text(encoding="utf-8"))
            if context.revision == revision and context.database == database:
                return context
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "business.building.sqlite"
        temporary.unlink(missing_ok=True)
        diagnostics: list[Diagnostic] = []
        tables = []
        connection = sqlite3.connect(temporary)
        try:
            create_audit(connection)
            for path in paths:
                relative = path.relative_to(business_dir).as_posix()
                try:
                    for sheet in read_sheets(path):
                        tables.extend(
                            self._ingest_sheet(
                                connection, sheet, relative, manifest[relative], readme, diagnostics
                            )
                        )
                except Exception as error:
                    diagnostics.append(
                        Diagnostic(
                            code="SOURCE_READ_ERROR",
                            message=f"Reader failed: {type(error).__name__}",
                            source=SourceRef(path=relative, sha256=manifest[relative]),
                            severity="error",
                        )
                    )
            from bizreason.data.catalog import discover_joins

            aliases = canonicalize_products(connection, tables)
            canonicalize_customers(connection, tables)
            tables = derive_vehicle_columns(connection, tables)
            tables = derive_status_from_notes(connection, tables, self.rules)
            propagate_order_returns(connection, tables, readme)
            tables = refresh_examples(connection, tables)
            atomic_json(directory / "product_aliases.json", aliases)

            if self.rules:
                from bizreason.data.normalization import derive_base_quantities

                tables, conversion_diagnostics = derive_base_quantities(
                    connection, tables, self.rules
                )
                diagnostics.extend(conversion_diagnostics)
            joins = discover_joins(connection, tables)
            for edge in joins:
                for k in edge.keys:
                    if len(k) >= 2:
                        l_col, r_col = k[0], k[1]
                        try:
                            connection.execute(
                                f"CREATE INDEX IF NOT EXISTS {quote_identifier('idx_jn_' + edge.left + '_' + l_col)[:60]} ON {quote_identifier(edge.left)} ({quote_identifier(l_col)})"
                            )
                        except Exception:
                            pass
                        try:
                            connection.execute(
                                f"CREATE INDEX IF NOT EXISTS {quote_identifier('idx_jn_' + edge.right + '_' + r_col)[:60]} ON {quote_identifier(edge.right)} ({quote_identifier(r_col)})"
                            )
                        except Exception:
                            pass
            connection.commit()
        finally:
            connection.close()
        temporary.replace(database)
        match = re.search(DECLARED_END_RE, readme, re.IGNORECASE)
        declared_end = None
        if match:
            parsed = parse_date(match.group(1))
            if parsed.status == ParseStatus.OK:
                declared_end = date.fromisoformat(parsed.value)
        start_match = re.search(DECLARED_START_RE, readme, re.IGNORECASE)
        declared_start = None
        if start_match:
            parsed = parse_date(start_match.group(1))
            if parsed.status == ParseStatus.OK:
                declared_start = date.fromisoformat(parsed.value)
        context = BusinessContext(
            business_id=business_dir.name,
            revision=revision,
            database=database,
            readme=readme,
            tables=tables,
            joins=joins,
            diagnostics=diagnostics,
            manifest=manifest,
            schema_complete=not any(d.code in {"SOURCE_READ_ERROR", "UNRESOLVED_HEADER"} for d in diagnostics),
            declared_end=declared_end,
            declared_start=declared_start,
        )
        atomic_json(metadata, context)
        atomic_json(
            directory / "business_manifest.json",
            {
                "revision": revision,
                "parser": PARSER_VERSION,
                "rule_hash": self.rule_hash,
                "files": manifest,
            },
        )
        return context

    def _ingest_sheet(
        self,
        connection: sqlite3.Connection,
        sheet: RawSheet,
        relative: str,
        source_hash: str,
        readme: str,
        diagnostics: list[Diagnostic],
    ) -> list[TableMeta]:
        prefix = list(itertools.islice(sheet.rows, 40))
        if not prefix:
            return []
        width = max((sum(not blank(cell) for cell in row) for row in prefix), default=0)
        labels: tuple[Any, ...] | None = None
        if sheet.declared_columns:
            headers = tuple(sheet.declared_columns)
            header_row = 0
            rows = enumerate(itertools.chain(prefix, sheet.rows), 1)
        else:
            position, score = choose_header_row(prefix, readme)
            if score < HEADER_MIN_SCORE:
                diagnostics.append(
                    Diagnostic(
                        code="UNRESOLVED_HEADER",
                        message="Insufficient header evidence",
                        source=SourceRef(path=relative, table=sheet.name),
                        severity="error",
                    )
                )
                return []
            header_row = position + 1
            headers = prefix[position]
            if position > 0:
                labels = _multi_tier_labels(prefix[position - 1], headers)
                if labels is not None:
                    diagnostics.append(
                        Diagnostic(
                            code="MULTI_TIER_HEADER",
                            message="Column names combine the group label row above the header",
                            source=SourceRef(path=relative, table=sheet.name, row=header_row),
                            severity="info",
                        )
                    )
            scale = _amount_scale(prefix[:position])
            if scale is not None:
                diagnostics.append(
                    Diagnostic(
                        code="AMOUNT_SCALE",
                        message=str(scale),
                        source=SourceRef(path=relative, table=sheet.name),
                        severity="info",
                    )
                )
            rows = enumerate(itertools.chain(prefix[position + 1 :], sheet.rows), header_row + 1)
        # Split later regions with a different, strongly recognized header.
        regions: list[tuple[tuple[Any, ...], int, list[tuple[int, tuple[Any, ...]]]]] = []
        buffered: list[tuple[int, tuple[Any, ...]]] = []
        current_headers, current_header_row = headers, header_row
        for row_number, row in rows:
            known = sum(
                infer_role(str(cell), readme) is not None for cell in row if not blank(cell)
            )
            # Rows with known names keep the previous split rule exactly. Rows without them
            # may split only on README-confirmed, strong text-only, high-fill shape.
            if known >= 2:
                splits = _alias_shape_score(row, readme) >= 4.0
            else:
                splits = _readme_region_header(row, readme, width)
            # A page-break copy of the current header repeats it; it is excluded, not a new region.
            repeat = _is_repeated_header(row, current_headers, share=1.0)
            if splits and not repeat and tuple(row) != tuple(current_headers):
                regions.append((current_headers, current_header_row, buffered))
                current_headers, current_header_row, buffered = row, row_number, []
            else:
                buffered.append((row_number, row))
        regions.append((current_headers, current_header_row, buffered))
        # A header with no rows whose next row is itself a header is a group-label row ("Money", "Goods") above
        # the real header, not a table: it becomes the group prefix of the next region's names, not empty columns.
        merged: list[tuple[tuple[Any, ...], int, list[tuple[int, tuple[Any, ...]]]]] = []
        group_labels: list[tuple[Any, ...] | None] = []
        pending: tuple[Any, ...] | None = None
        for position_, (region_headers, region_header_row, region_rows) in enumerate(regions):
            following = regions[position_ + 1] if position_ + 1 < len(regions) else None
            if not region_rows and following is not None and following[1] == region_header_row + 1:
                pending = region_headers
                diagnostics.append(
                    Diagnostic(
                        code="GROUP_HEADER_ROW",
                        message="Row of group labels above the real header; kept as a name prefix, not a table",
                        source=SourceRef(path=relative, table=sheet.name, row=region_header_row),
                        severity="info",
                    )
                )
                continue
            merged.append((region_headers, region_header_row, region_rows))
            group_labels.append(_multi_tier_labels(pending, region_headers) if pending is not None else None)
            pending = None
        regions = merged
        if group_labels and group_labels[0] is not None and labels is None:
            labels = group_labels[0]
        return [
            self._write_region(
                connection,
                sheet,
                relative,
                source_hash,
                readme,
                region_headers,
                region_header_row,
                region_rows,
                index,
                diagnostics,
                # Only the first region starts at the chosen header, which the labels describe.
                labels=labels if index == 0 else group_labels[index],
            )
            for index, (region_headers, region_header_row, region_rows) in enumerate(regions)
        ]

    def _write_region(
        self,
        connection: sqlite3.Connection,
        sheet: RawSheet,
        relative: str,
        source_hash: str,
        readme: str,
        headers: tuple[Any, ...],
        header_row: int,
        rows: list[tuple[int, tuple[Any, ...]]],
        region_index: int,
        diagnostics: list[Diagnostic],
        labels: tuple[Any, ...] | None = None,
    ) -> TableMeta:
        width = max(
            (index + 1 for index, value in enumerate(headers) if not blank(value)),
            default=len(headers),
        )
        headers = headers[:width]
        names = unique_columns(headers if labels is None else labels[:width])
        descriptions = column_descriptions(readme, sheet.path.name, sheet.name)
        table_label = sheet.path.stem + " " + sheet.name
        roles = [
            described_role(str(header), descriptions.get(normalize_lookup(header), ""), table_label)
            or infer_role(str(header), readme)
            for header in headers
        ]
        roles = contextual_roles(headers, roles, descriptions, readme)
        clock_columns = [i for i, header in enumerate(headers) if normalize_lookup(header) in
                         {"gio tao don", "gio ban", "thoigianlap", "thoi gian lap", "ngay gio"}]
        if len(clock_columns) == 1 and roles[clock_columns[0]] == "ordered_at":
            roles = ["ordered_date" if role == "ordered_at" and i != clock_columns[0] else role
                     for i, role in enumerate(roles)]
        for index, header in enumerate(headers):
            normalized = normalize_lookup(header)
            if normalized == "ngay mua" and "chi phi" in normalize_lookup(table_label):
                roles[index] = "expense_at"
            if normalized in {"ten goi", "tengoi"}:
                label = normalize_lookup(table_label)
                if "san pham" in label or "san_pham" in table_label:
                    roles[index] = "product_name"
                elif "khach hang" in label or "khach_hang" in table_label:
                    roles[index] = "customer_name"
            if normalized in {"tinhtrang", "tinh trang"} and any(
                role in roles for role in ["order_id", "purchase_id"]
            ):
                roles[index] = "status"
        if "order_id" not in roles and "purchase_id" not in roles:
            roles = [
                (
                    None
                    if role == "status"
                    and not normalize_lookup(
                        descriptions.get(normalize_lookup(header), "")
                    ).startswith("trang thai don")
                    else role
                )
                for header, role in zip(headers, roles)
            ]
        grain = infer_grain(set(filter(None, roles)), sheet.name, readme)
        name = (
            identifier(f"{sheet.path.stem}_{sheet.name}")[:65]
            + "_"
            + digest([relative, sheet.name, region_index])[:8]
        )
        source = SourceRef(path=relative, table=sheet.name, row=header_row, sha256=source_hash)
        q = quote_identifier
        definitions = ", ".join(f"{q(column)}" for column in names)
        connection.execute(
            f"CREATE TABLE {q(name)} ({definitions}, __source_row INTEGER, __raw_json TEXT)"
        )
        placeholders = ",".join("?" for _ in range(width + 2))
        insert = f"INSERT INTO {q(name)} VALUES ({placeholders})"
        locales = [
            infer_locale(
                [row[index] for _, row in rows[:2000] if len(row) > index],
                readme,
                currency=roles[index]
                in MONEY_ROLES,
            )
            for index in range(width)
        ]
        date_orders = _date_column_orders(rows, roles, width)
        nulls: Counter[int] = Counter()
        errors: Counter[int] = Counter()
        types: dict[int, set[str]] = {index: set() for index in range(width)}
        examples: dict[int, list[Any]] = {index: [] for index in range(width)}
        coverage: dict[str, list[str]] = {}
        seen: set[str] = set()
        current_group: dict[int, Any] | None = None
        order_index = roles.index("order_id") if "order_id" in roles else None
        inherited_fields = [
            index
            for index, role in enumerate(roles)
            if role in GROUP_ROLES or normalize_lookup(headers[index]) == NOTE_HEADER
        ]
        raw_count, excluded, duplicate, kept = len(rows), 0, 0, 0
        year = sheet_year(sheet.path.stem + " " + sheet.name)
        batch = []
        last_record = max(
            (
                index
                for index, (_, raw) in enumerate(rows)
                if has_record_evidence(list(raw[:width]) + [None] * max(0, width - len(raw)), roles)
            ),
            default=-1,
        )
        subtotals = ArithmeticSubtotals(roles, locales)
        block_sums = BlockSums(width, _sum_locales(rows, locales, readme), roles=roles)
        for position, (row_number, raw_row) in enumerate(rows):
            row = list(raw_row[:width]) + [None] * max(0, width - len(raw_row))
            ambiguous_evidence = None
            def audit(row_class: str, reason: str, *, retained: bool = False, evidence=None) -> None:
                record_audit(
                    connection, source_path=relative, sheet=sheet.name, region=name,
                    source_row=row_number, row_class=row_class, kept=retained,
                    reason=reason, raw=raw_row, evidence=evidence,
                )
            if all(blank(cell) for cell in row) or tuple(row) == tuple(headers):
                audit("blank" if all(blank(cell) for cell in row) else "header", "structural row")
                excluded += 1
                current_group = None
                continue
            if _is_repeated_header(row, headers):
                audit("header", "repeated header")
                excluded += 1
                current_group = None
                diagnostics.append(
                    Diagnostic(
                        code="EXCLUDED_REPEATED_HEADER_ROW",
                        message="Page-break copy of the header inside transaction rows",
                        source=SourceRef(path=relative, table=sheet.name, row=row_number),
                        severity="info",
                    )
                )
                continue
            kind = (
                "summary"
                if is_summary(
                    row, roles, after_last_record=last_record >= 0 and position > last_record
                )
                else (
                    "note"
                    if is_note(row, roles)
                    else None
                )
            )
            if kind:
                audit(kind, "explicit row label and shape")
                excluded += 1
                current_group = None
                if kind == "summary":
                    subtotals.reset()
                    block_sums.reset()
                diagnostics.append(
                    Diagnostic(
                        code=f"EXCLUDED_{kind.upper()}_ROW",
                        message=f"Confirmed export {kind} outside transaction rows",
                        source=SourceRef(path=relative, table=sheet.name, row=row_number),
                        severity="info",
                    )
                )
                continue
            # The role-based rule also needs a total's shape: a continuation line of an order has blank keys too.
            if (subtotals.is_subtotal(row) and block_sums.looks_like_total(row)) or block_sums.is_subtotal(row):
                if block_sums.ambiguous_single_amount(row):
                    ambiguous_evidence = block_sums.evidence(row)
                    diagnostics.append(Diagnostic(
                        code="AMBIGUOUS_SUBTOTAL_ROW",
                        message="Kept possible continuation: one numeric equality does not identify a subtotal",
                        source=SourceRef(path=relative, table=sheet.name, row=row_number), severity="info",
                    ))
                else:
                    audit("subtotal", "arithmetic candidate with additional column or label evidence",
                          evidence=block_sums.evidence(row))
                    excluded += 1
                    current_group = None
                    subtotals.reset()
                    block_sums.reset()
                    diagnostics.append(
                        Diagnostic(
                            code="EXCLUDED_SUBTOTAL_ROW",
                            message="Arithmetic subtotal outside transaction rows: empty keys, amount equals the rows above",
                            source=SourceRef(path=relative, table=sheet.name, row=row_number),
                            severity="info",
                        )
                    )
                    continue
            subtotals.add(row)
            block_sums.add(row)
            if grain == "order_line" and order_index is not None:
                if not blank(row[order_index]):
                    row[order_index] = clean_text(row[order_index])
                key = row[order_index]
                if not blank(key):
                    if current_group is None or key != current_group[order_index]:
                        current_group = {index: row[index] for index in inherited_fields}
                elif current_group is None or not self.inheritance_allowed:
                    diagnostics.append(
                        Diagnostic(
                            code="ORPHAN_CONTINUATION",
                            message="Order detail without a documented parent group",
                            source=SourceRef(path=relative, table=sheet.name, row=row_number),
                            severity="error",
                        )
                    )
                if current_group is not None and self.inheritance_allowed:
                    for index in inherited_fields:
                        if blank(row[index]):
                            row[index] = current_group.get(index)
            # A repeated exported header is the same order too. Independent
            # payment/ledger events must retain identical rows as separate events.
            if grain in {"order_line", "order_header"} and order_index is not None and not blank(row[order_index]):
                row[order_index] = clean_text(row[order_index])
                fingerprint = digest(row)  # Includes identity, scope and every source cell.
                if fingerprint in seen:
                    audit("duplicate", "exact repeated order record")
                    duplicate += 1
                    if grain == "order_header":
                        diagnostics.append(Diagnostic(
                            code="DUPLICATE_ORDER_ROW", severity="info",
                            message="Exact repeated order header; counted once",
                            source=SourceRef(path=relative, table=sheet.name, row=row_number, sha256=source_hash),
                        ))
                    continue
                seen.add(fingerprint)
            if ambiguous_evidence is not None:
                audit("ambiguous_total", "single numeric equality without an explicit summary label",
                      retained=True, evidence=ambiguous_evidence)
            parsed = []
            for index, (value, role) in enumerate(zip(row, roles)):
                result = None
                if role in NUMERIC_ROLES:
                    result = parse_number(
                        value,
                        locale=locales[index],
                        unit=(
                            "VND"
                            if role
                            in MONEY_ROLES
                            else (
                                str(row[roles.index("unit")])
                                if role == "qty_sale"
                                and "unit" in roles
                                and not blank(row[roles.index("unit")])
                                else None
                            )
                        ),
                    )
                elif role in DATE_ROLES:
                    result = parse_date_ordered(value, date_orders.get(index, "dmy"), year=year)
                elif index in date_orders:
                    # A roleless column that reads as dates: each cell is normalized on its own.
                    result = parse_date_ordered(value, date_orders[index], year=year)
                elif role == "status":
                    purchase_domain = (
                        grain in {"purchase_header", "purchase_line"}
                        or "purchased_at" in roles
                        or "nhaphang" in normalize_lookup(table_label).replace(" ", "")
                    )
                    result = parse_status(value, domain="purchase" if purchase_domain else "order")
                if result:
                    if result.status in {ParseStatus.INVALID, ParseStatus.AMBIGUOUS}:
                        errors[index] += 1
                        value = (
                            None  # Raw value remains recoverable via __raw_json/source position.
                        )
                    else:
                        value = (
                            _stored_number(result.value)
                            if isinstance(result.value, Decimal)
                            else result.value
                        )
                elif not blank(value):
                    value = (
                        clean_text(value)
                        if role and (role.endswith("_id") or role.endswith("_name"))
                        else value
                    )
                    if not isinstance(value, (str, int, float, bytes)):
                        value = str(value)
                else:
                    value = None
                if role == "customer_name" and value is not None and is_retail_customer(value):
                    customer_id = row[roles.index("customer_id")] if "customer_id" in roles else None
                    if blank(customer_id):
                        value = None
                if value is None:
                    nulls[index] += 1
                else:
                    types[index].add(type(value).__name__)
                    if len(examples[index]) < 3 and value not in examples[index]:
                        examples[index].append(value)
                    if role in DATE_ROLES:
                        bounds = coverage.setdefault(names[index], [str(value), str(value)])
                        bounds[0], bounds[1] = min(bounds[0], str(value)), max(
                            bounds[1], str(value)
                        )
                parsed.append(value)
            batch.append((*parsed, row_number, dumps(list(raw_row))))
            kept += 1
            if len(batch) >= 2000:
                connection.executemany(insert, batch)
                batch.clear()
        if batch:
            connection.executemany(insert, batch)
        for index, count in errors.items():
            diagnostics.append(
                Diagnostic(
                    code="PARSE_ERROR",
                    message=f"{name}.{names[index]}: {count} invalid/ambiguous cells",
                    source=source,
                    severity="error",
                )
            )
        columns = [
            ColumnMeta(
                name=column,
                original_name=str(headers[index] or ""),
                dtype=(
                    "INTEGER"
                    if types[index] <= {"int"} and types[index]
                    else (
                        "TEXT"
                        if roles[index] in DATE_ROLES or types[index] <= {"str"}
                        else "NUMERIC"
                    )
                ),
                semantic_role=roles[index],
                examples=examples[index],
                null_count=nulls[index],
                parse_errors=errors[index],
                unit=(
                    "VND"
                    if roles[index]
                    in MONEY_ROLES
                    else None
                ),
                evidence=[
                    f"{relative}:{sheet.name}:header:{header_row}",
                    *(
                        [f"numeric_locale:{locales[index]}:column_profile"]
                        if roles[index] in NUMERIC_ROLES
                        else []
                    ),
                ],
            )
            for index, column in enumerate(names)
        ]
        for index, role in enumerate(roles):
            col_l = names[index].lower()
            orig_l = normalize_lookup(str(headers[index] or "")).replace(" ", "_")
            is_join_id = (
                col_l.startswith("ma_")
                or col_l.startswith("so_")
                or col_l.startswith("id_")
                or col_l.endswith("_id")
                or col_l
                in {
                    "ma_giao_dich",
                    "so_chung_tu",
                    "so_hd",
                    "ma_bill",
                    "so_phieu",
                    "ma_don",
                    "ma_nhap_hang",
                    "ma_hang",
                    "ma_mat_hang",
                    "ma_sp",
                    "ma_san_pham_ban",
                    "ma_kh",
                    "so_kh",
                    "order_id",
                    "customer_id",
                }
                or orig_l
                in {
                    "ma_giao_dich",
                    "so_chung_tu",
                    "so_hd",
                    "ma_bill",
                    "so_phieu",
                    "ma_don",
                    "ma_nhap_hang",
                    "ma_hang",
                    "ma_mat_hang",
                    "ma_sp",
                    "ma_san_pham_ban",
                    "ma_kh",
                    "so_kh",
                }
            )
            if (role and (role.endswith("_id") or role in DATE_ROLES or role == "status")) or is_join_id:
                try:
                    connection.execute(
                        f"CREATE INDEX IF NOT EXISTS {q(name + '_' + str(index))} ON {q(name)} ({q(names[index])})"
                    )
                except Exception:
                    pass
        return TableMeta(
            name=name,
            sources=[source],
            columns=columns,
            grain=grain,
            row_count=kept,
            raw_row_count=raw_count,
            excluded_rows=excluded,
            duplicate_rows=duplicate,
            header_row=header_row,
            coverage={key: tuple(value) for key, value in coverage.items()},
        )
