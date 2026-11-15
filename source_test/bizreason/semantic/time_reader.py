"""Model-assisted reading of time periods, with every date checked by code against the question.

The deterministic resolver (bizreason.temporal) reads known phrasings. When it finds no window at all, the model
may read the periods as typed JSON, but each boundary it returns must be grounded in the question text or in the
as-of date; one ungrounded boundary drops the whole answer, so the caller keeps its old behaviour (abstain)."""
from __future__ import annotations

import calendar
import json
import re
import unicodedata
from datetime import date, timedelta
from typing import Any

from bizreason.contracts import TimeWindow
from bizreason.utils import normalize_lookup

# A rulebook section whose heading (after normalize_lookup) contains this is the time-rule section.
RULEBOOK_TIME_HEADING = "thoi gian"
RULEBOOK_EXCERPT_LIMIT = 2500
MAX_PERIODS = 4
ROLES = ("target", "comparison")
# Words that make "yesterday" a grounded boundary (accent-free).
YESTERDAY_WORDS = ("hom qua", "hom truoc")

SYSTEM_PROMPT = (
    "You read the time periods that a Vietnamese business question talks about. Use only time the QUESTION states; "
    "never guess or add a period the question does not say. Reply with one JSON object only: "
    '{"periods": [{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "role": "target"|"comparison"}]}. '
    "end is the LAST day, INCLUDED. role target is the asked period; comparison is a period it is compared with "
    '(only when there is exactly one target). If the question states no period, reply {"periods": []}.'
)


def _fold(text: str) -> str:
    """Lowercase, accent-free text that keeps punctuation (dates need their separators)."""
    text = unicodedata.normalize("NFKD", str(text).lower().replace(chr(0x111), "d"))
    return "".join(c for c in text if not unicodedata.combining(c))


def year_without_stated_year(month: int, day: int, as_of: date) -> int:
    """Rulebook public §2: a month, quarter or day written without a year is in the current year when it is not
    after today, otherwise in the previous year. (month, day) is the first day of the period."""
    try:
        return as_of.year if date(as_of.year, month, day) <= as_of else as_of.year - 1
    except ValueError:
        return as_of.year - 1


def _full_year(text: str) -> int:
    value = int(text)
    return value if value >= 100 else 2000 + value


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


_FULL_DATE = re.compile(r"(?<!\d)(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{4}|\d{2})(?!\d)")
_MONTH_YEAR_NUM = re.compile(r"(?<![\d/.\-])(\d{1,2})\s*[/\-.]\s*(\d{4})(?!\d)")
_DAY_MONTH = re.compile(r"(?<![\d/.\-])(\d{1,2})\s*/\s*(\d{1,2})(?![\d/])")
_MONTH_WORD = re.compile(
    r"\b(?:thang|th|t)\s?(\d{1,2})(?:\s?[/\-.]\s?(\d{4}|\d{2})\b|\s+(?:nam\s+)?(\d{4})\b)?")
_QUARTER = re.compile(r"\b(?:quy|q)\s?([1-4])(?:\s?[/\-.]\s?(\d{4}|\d{2})\b|\s+(?:nam\s+)?(\d{4})\b)?")
_YEAR = re.compile(r"\bnam\s+(\d{4})\b")
_DAYS = re.compile(r"(\d{1,3})\s*ngay\b")


def grounded_dates(question: str, as_of: date) -> set[date]:
    """Every boundary day the question itself (plus as_of) supports."""
    text = _fold(question)
    out: set[date] = {as_of}
    if any(w in normalize_lookup(question) for w in YESTERDAY_WORDS):
        out.add(as_of - timedelta(days=1))

    def add_day(y: int, m: int, d: int) -> None:
        try:
            out.add(date(y, m, d))
        except ValueError:
            pass

    def add_month(y: int, m: int) -> None:
        if 1 <= m <= 12:
            out.update(_month_bounds(y, m))

    for m in _FULL_DATE.finditer(text):
        add_day(_full_year(m.group(3)), int(m.group(2)), int(m.group(1)))
    rest = _FULL_DATE.sub(" ", text)
    for m in _MONTH_YEAR_NUM.finditer(rest):
        add_month(int(m.group(2)), int(m.group(1)))
    for m in _DAY_MONTH.finditer(rest):
        d, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            add_day(year_without_stated_year(mo, d, as_of), mo, d)
    for m in _MONTH_WORD.finditer(rest):
        mo = int(m.group(1))
        if not 1 <= mo <= 12:
            continue
        stated = m.group(2) or m.group(3)
        add_month(_full_year(stated) if stated else year_without_stated_year(mo, 1, as_of), mo)
    for m in _QUARTER.finditer(rest):
        q = int(m.group(1))
        stated = m.group(2) or m.group(3)
        first = 3 * q - 2
        year = _full_year(stated) if stated else year_without_stated_year(first, 1, as_of)
        out.add(date(year, first, 1))
        out.add(_month_bounds(year, first + 2)[1])
    for m in _YEAR.finditer(text):
        year = int(m.group(1))
        out.update((date(year, 1, 1), date(year, 12, 31)))
    return out


def written_day_counts(question: str) -> set[int]:
    return {int(n) for n in _DAYS.findall(_fold(question)) if int(n) > 0}


def _parse_day(value: Any) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        raise ValueError(f"bad date {value!r}")
    return date.fromisoformat(value.strip())


def rulebook_time_section(rulebook_text: str | None) -> str | None:
    """The rulebook section whose heading mentions time, up to the next heading of the same or higher level."""
    if not rulebook_text:
        return None
    lines = rulebook_text.splitlines()
    for i, line in enumerate(lines):
        head = re.match(r"^(#+)\s+(.*)$", line)
        if head and RULEBOOK_TIME_HEADING in normalize_lookup(head.group(2)):
            level, body = len(head.group(1)), [line]
            for nxt in lines[i + 1:]:
                h2 = re.match(r"^(#+)\s", nxt)
                if h2 and len(h2.group(1)) <= level:
                    break
                body.append(nxt)
            return "\n".join(body).strip()[:RULEBOOK_EXCERPT_LIMIT]
    return None


def validate_periods(payload: Any, question: str, as_of: date) -> list[TimeWindow]:
    """The model's periods as windows, or ValueError when any rule is broken or any boundary is ungrounded."""
    if not isinstance(payload, dict) or not isinstance(payload.get("periods"), list):
        raise ValueError("payload is not {'periods': [...]}")
    raw = payload["periods"]
    if not 1 <= len(raw) <= MAX_PERIODS:
        raise ValueError(f"{len(raw)} periods")
    periods = []
    for item in raw:
        if not isinstance(item, dict) or item.get("role") not in ROLES:
            raise ValueError("bad period or role")
        start, end = _parse_day(item.get("start")), _parse_day(item.get("end"))
        if start > end:
            raise ValueError("start after end")
        periods.append((item["role"], start, end))
    targets = [p for p in periods if p[0] == "target"]
    if not targets or (len(targets) != 1 and len(targets) != len(periods)):
        raise ValueError("needs targets; comparison only beside exactly one target")
    anchors = grounded_dates(question, as_of)
    for n in written_day_counts(question):
        anchors.add(as_of - timedelta(days=n - 1))
        anchors.add(as_of - timedelta(days=n))
    counts = written_day_counts(question)
    for role, start, end in periods:
        starts, ends = set(anchors), set(anchors)
        if role == "comparison":
            starts.update(targets[0][1] - timedelta(days=n) for n in counts)
            if counts:
                ends.add(targets[0][1] - timedelta(days=1))
        if start not in starts or end not in ends:
            raise ValueError("boundary not grounded in the question")
    return [TimeWindow(role=role, start=start, end_exclusive=end + timedelta(days=1),
                       evidence=["question", "as_of_date", "model_time"]) for role, start, end in periods]


class TimeReader:
    """Asks the model for typed periods and keeps them only when validate_periods accepts all of them."""

    def __init__(self, model: Any, *, rulebook_text: str | None = None, timeout: float = 60.0):
        self.model = model
        self.timeout = timeout
        self.rulebook_section = rulebook_time_section(rulebook_text)
        self.last_reason = ""
        self.calls = 0
        self._cache: dict[tuple[str, date], list[TimeWindow] | None] = {}

    def _prompt(self, question: str, as_of: date) -> str:
        parts = [f"as_of_date (today): {as_of.isoformat()}"]
        if self.rulebook_section:
            parts.append("Time rules of the rulebook in force:\n" + self.rulebook_section)
        parts.append(f"Question: {question}")
        return "\n\n".join(parts)

    def read(self, question: str, as_of: date) -> list[TimeWindow] | None:
        key = (question, as_of)
        if key in self._cache:
            return self._cache[key]
        result: list[TimeWindow] | None = None
        try:
            self.calls += 1
            payload = self.model.generate_json(SYSTEM_PROMPT, self._prompt(question, as_of), timeout=self.timeout)
            result = validate_periods(payload, question, as_of)
            self.last_reason = "ok"
        except Exception as exc:  # noqa: BLE001 - any failure keeps the caller's old behaviour
            self.last_reason = f"{type(exc).__name__}: {exc}"[:200]
            result = None
        self._cache[key] = result
        return result
