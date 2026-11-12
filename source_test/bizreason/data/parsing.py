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
