from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from bizreason.data.vocabulary import (
    CURRENCY_OR_PERCENT_STRIP_RE,
    CURRENCY_STRIP_RE,
    CURRENCY_SUFFIX_RE,
    MILLION_DECIMAL_RE,
    MILLION_RE,
    STATUS_BASE,
    STATUS_SYNONYMS,
    VIETNAMESE_DATE_RE,
)
from bizreason.utils import normalize_lookup


class ParseStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    AMBIGUOUS = "ambiguous"
    INVALID = "invalid"


@dataclass(frozen=True)
class ParseResult:
    value: Any
    status: ParseStatus
    raw: Any
    unit: str | None = None
    reason: str = ""


def blank(value: Any) -> bool:
    return value is None or isinstance(value, str) and not value.strip()


def parse_number(raw: Any, *, locale: str = "unknown", unit: str | None = None) -> ParseResult:
    if blank(raw):
        return ParseResult(None, ParseStatus.EMPTY, raw, unit)
    if isinstance(raw, bool):
        return ParseResult(None, ParseStatus.INVALID, raw, unit, "Boolean is not a quantity")
    if isinstance(raw, (int, float, Decimal)):
        number = Decimal(str(raw))
        return ParseResult(
            number if number.is_finite() else None,
            ParseStatus.OK if number.is_finite() else ParseStatus.INVALID,
            raw,
            unit,
        )
    text = str(raw).strip().lower().replace("\u00a0", " ")
    million_decimal = re.fullmatch(
        MILLION_DECIMAL_RE,
        re.sub(CURRENCY_STRIP_RE, "", text).strip(),
    )
    if million_decimal:
        return ParseResult(
            Decimal(million_decimal.group(1).replace(",", ".")) * 1_000_000,
            ParseStatus.OK,
            raw,
            unit or "VND",
        )
    million = re.fullmatch(
        MILLION_RE,
        re.sub(CURRENCY_STRIP_RE, "", text).strip(),
    )
    if million:
        whole, thousands = million.groups()
        sign = -1 if whole.startswith("-") else 1
        number = Decimal(whole) * 1_000_000 + sign * Decimal(thousands or 0) * 1000
        return ParseResult(number, ParseStatus.OK, raw, unit or "VND")
    money_suffix = re.search(CURRENCY_SUFFIX_RE, text)
    text = re.sub(CURRENCY_OR_PERCENT_STRIP_RE, "", text).strip()
    scale = Decimal(1)
    if re.search(r"\d\s*k$", text):
        scale = Decimal(1000)
        text = text[:-1].strip()
    match = re.fullmatch(r"([+\-()\d.,\s]+)\s*([^\d.,+\-()\s].*)?", text)
    if not match:
        return ParseResult(None, ParseStatus.INVALID, raw, unit, "Unrecognized numeric syntax")
    text, detected_unit = match.groups()
    if detected_unit and unit and normalize_lookup(detected_unit) != normalize_lookup(unit):
        return ParseResult(None, ParseStatus.INVALID, raw, unit, "Unexpected unit")
    unit = detected_unit or unit
    text = text.replace(" ", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    # An explicit Vietnamese currency spelling is cell-level evidence. A locale
    # inferred from another value/column must not turn 7.000đ into seven dong.
    currency_grouped_dot = money_suffix and re.fullmatch(r"[+-]?\d{1,3}(?:\.\d{3})+", text)
    if currency_grouped_dot or locale == "unknown" and money_suffix:
        locale = "vi"
    if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+\.\d+", text):
        locale = "en"
    elif re.fullmatch(r"[+-]?\d{1,3}(?:\.\d{3})+,\d+", text):
        locale = "vi"
    if "," in text and "." in text:
        if locale == "unknown":
            return ParseResult(None, ParseStatus.AMBIGUOUS, raw, unit, "Numeric locale required")
        text = text.replace(".", "").replace(",", ".") if locale == "vi" else text.replace(",", "")
    elif "," in text or "." in text:
        separator = "," if "," in text else "."
        parts = text.lstrip("+-").split(separator)
        grouped = (
            len(parts) > 1 and 1 <= len(parts[0]) <= 3 and all(len(part) == 3 for part in parts[1:])
        )
        if len(parts) > 2:
            if not grouped:
                return ParseResult(
                    None, ParseStatus.INVALID, raw, unit, "Malformed numeric separators"
                )
            text = text.replace(separator, "")
        elif grouped and locale == "unknown":
            return ParseResult(
                None, ParseStatus.AMBIGUOUS, raw, unit, "Decimal or thousands separator"
            )
        elif (
            locale == "vi"
            and separator == "."
            and grouped
            or locale == "en"
            and separator == ","
            and grouped
        ):
            text = text.replace(separator, "")
        elif separator == ",":
            text = text.replace(",", ".")
    try:
        number = Decimal(text) * scale
    except InvalidOperation:
        return ParseResult(None, ParseStatus.INVALID, raw, unit, "Invalid decimal")
    return ParseResult(
        number if number.is_finite() else None,
        ParseStatus.OK if number.is_finite() else ParseStatus.INVALID,
        raw,
        unit,
    )


SLASH_DATE_RE = re.compile(r"^\s*(\d{1,2})([/.-])(\d{1,2})\2(\d{2}|\d{4})(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\s*$")
SERIAL_MIN, SERIAL_MAX = 20000, 80000  # spreadsheet day serials: 1954 to 2119


def infer_date_order(values: Any) -> str:
    """Day/month order of the slash-separated dates in a column: "dmy", "mdy", "ambiguous" or "conflict".

    Only the cells themselves count: a first part above 12 proves day-first, a second part above 12 proves
    month-first. When no cell proves either the order is "ambiguous"; when both kinds occur it is "conflict".
    A column without slash dates is "ambiguous" as well.
    """
    first_big = second_big = False
    for value in values:
        if not isinstance(value, str):
            continue
        match = SLASH_DATE_RE.fullmatch(value)
        if not match:
            continue
        first, second = int(match.group(1)), int(match.group(3))
        first_big = first_big or first > 12
        second_big = second_big or second > 12
    if first_big and second_big:
        return "conflict"
    return "dmy" if first_big else "mdy" if second_big else "ambiguous"


def parse_date_ordered(raw: Any, order: str, *, year: int | None = None) -> ParseResult:
    """parse_date, but a slash date is read in the given column order ("dmy" or "mdy"; any other value reads it
    day-first, like parse_date). A day/month pair that is not a real date stays INVALID, never guessed."""
    if order == "mdy" and isinstance(raw, str):
        match = SLASH_DATE_RE.fullmatch(raw)
        if match:
            month, _, day, explicit_year = match.groups()
            if len(explicit_year) == 2:
                return ParseResult(None, ParseStatus.INVALID, raw, reason="Two-digit year")
            clock = raw.strip().split(None, 1)[1] if len(raw.strip().split(None, 1)) > 1 else ""
            try:
                value = date(int(explicit_year), int(month), int(day)).isoformat()
            except ValueError:
                return ParseResult(None, ParseStatus.INVALID, raw, reason="Invalid date")
            return ParseResult(value + (" " + clock if clock else ""), ParseStatus.OK, raw)
    return parse_date(raw, year=year)


def is_serial_cell(raw: Any) -> bool:
    """A number, or a bare digit string, in the spreadsheet day-serial range (not a bool)."""
    if isinstance(raw, str):
        text = raw.strip()
        return bool(re.fullmatch(r"\d{5}(?:\.\d+)?", text)) and SERIAL_MIN <= float(text) <= SERIAL_MAX
    return (isinstance(raw, (int, float, Decimal)) and not isinstance(raw, bool)
            and SERIAL_MIN <= raw <= SERIAL_MAX)


def parse_date(raw: Any, *, year: int | None = None) -> ParseResult:
    if blank(raw):
        return ParseResult(None, ParseStatus.EMPTY, raw)
    if isinstance(raw, (date, datetime)):
        return ParseResult(
            raw.isoformat(sep=" ") if isinstance(raw, datetime) else raw.isoformat(),
            ParseStatus.OK,
            raw,
        )
    text = str(raw).strip()
    if re.fullmatch(r"\d{5}(?:\.\d+)?", text) and 20000 <= float(text) <= 80000:
        from openpyxl.utils.datetime import from_excel

        converted = from_excel(float(text))
        return ParseResult(
            (
                converted.isoformat(sep=" ")
                if converted.time().isoformat() != "00:00:00"
                else converted.date().isoformat()
            ),
            ParseStatus.OK,
            raw,
        )
    vietnamese = re.fullmatch(
        VIETNAMESE_DATE_RE,
        text,
        re.IGNORECASE,
    )
    if vietnamese:
        day, month, explicit_year, clock = vietnamese.groups()
        try:
            parsed_day = date(int(explicit_year), int(month), int(day))
            value = parsed_day.isoformat() + (" " + clock if clock else "")
            return ParseResult(value, ParseStatus.OK, raw)
        except ValueError:
            return ParseResult(None, ParseStatus.INVALID, raw, reason="Invalid Vietnamese date")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return ParseResult(
            parsed.isoformat(sep=" ") if "T" in text or " " in text else parsed.date().isoformat(),
            ParseStatus.OK,
            raw,
        )
    except ValueError:
        pass
    for pattern in [
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d/%m/%y",
        "%Y/%m/%d",
    ]:
        try:
            parsed = datetime.strptime(text, pattern)
            return ParseResult(
                parsed.isoformat(sep=" ") if "%H" in pattern else parsed.date().isoformat(),
                ParseStatus.OK,
                raw,
            )
        except ValueError:
            continue
    if re.fullmatch(r"\d{1,2}[/-]\d{1,2}", text):
        if year is None:
            return ParseResult(None, ParseStatus.AMBIGUOUS, raw, reason="Missing sheet year")
        try:
            day, month = map(int, re.split(r"[/-]", text))
            return ParseResult(date(year, month, day).isoformat(), ParseStatus.OK, raw)
        except ValueError:
            pass
    return ParseResult(None, ParseStatus.INVALID, raw, reason="Invalid date")


DATE_COLUMN_SHARE = 0.9  # share of a column's cells that must read as dates (or serials) to call it a date column
DATE_COLUMN_MIXED_SHARE = 0.1  # with serial cells present, share of genuine date spellings that must confirm it
SERIAL_SPAN_YEARS = 10  # serial dates must fall this close to the genuine dates of the same column


def classify_date_column(values: Any, *, hint: str = "ambiguous") -> str | None:
    """Decide whether a column without a date role is a date column that mixes spellings; return its day/month order.

    Returns "dmy" or "mdy" (the order used to read its slash dates) when the column is dated, else None.
    Rules, all per column and none tied to a header name:

    - cells are native dates, text dates, or spreadsheet serials; at least DATE_COLUMN_SHARE of the non-blank
      cells must be one of those, and at least one must be a genuine date (a column of bare numbers is never
      called a date: it is indistinguishable from amounts);
    - with serial cells present, the genuine dates must be at least DATE_COLUMN_MIXED_SHARE of the cells and
      every serial must fall within SERIAL_SPAN_YEARS of the genuine dates' range;
    - slash dates must have a provable order: "conflict" (both a day above 12 in first and in second place)
      rejects the column; when no cell proves an order, the hint from the shop's other date columns decides, and
      with no hint the column is rejected rather than guessed. A column whose slash dates are all unambiguous
      (e.g. ISO, or no slash dates) needs no order.
    """
    cells = [value for value in values if not blank(value)]
    if not cells:
        return None
    order = infer_date_order(cells)
    if order == "conflict":
        return None
    slash_cells = [v for v in cells if isinstance(v, str) and SLASH_DATE_RE.fullmatch(v)]
    if order == "ambiguous" and slash_cells:
        if hint not in {"dmy", "mdy"}:
            return None
        order = hint
    genuine: list[str] = []
    serials: list[Decimal] = []
    other = 0
    for value in cells:
        if is_serial_cell(value):
            serials.append(Decimal(str(value)))
        elif isinstance(value, (date, datetime)):
            genuine.append(value.isoformat()[:10])
        elif isinstance(value, (int, float, Decimal)) or (isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)?", value.strip())):
            other += 1
        else:
            result = parse_date_ordered(value, order)
            if result.status == ParseStatus.OK:
                genuine.append(str(result.value)[:10])
            else:
                other += 1
    if not genuine or (len(genuine) + len(serials)) < DATE_COLUMN_SHARE * len(cells):
        return None
    if serials:
        if len(genuine) < DATE_COLUMN_MIXED_SHARE * len(cells):
            return None
        from openpyxl.utils.datetime import from_excel

        low, high = int(min(genuine)[:4]) - SERIAL_SPAN_YEARS, int(max(genuine)[:4]) + SERIAL_SPAN_YEARS
        if any(not low <= from_excel(float(serial)).year <= high for serial in serials):
            return None
    return order if order in {"dmy", "mdy"} else "dmy"


STATUS_ALIASES = dict(STATUS_BASE)
STATUS_ALIASES.update(
    {
        normalize_lookup(alias): status
        for status, aliases in STATUS_SYNONYMS.items()
        for alias in aliases.split("|")
    }
)


def parse_status(raw: Any, *, domain: str = "order") -> ParseResult:
    if blank(raw):
        return ParseResult(None, ParseStatus.EMPTY, raw)
    result = STATUS_ALIASES.get(normalize_lookup(raw))
    if domain == "purchase":
        if normalize_lookup(raw) in {
            "da nhap hang",
            "da nhap",
            "nhap",
            "received",
            "completed",
            "hoan thanh",
        }:
            result = "received"
        elif result == "completed":
            result = "received"
    return ParseResult(
        result,
        ParseStatus.OK if result else ParseStatus.INVALID,
        raw,
        reason="" if result else "Unmapped recorded status",
    )
