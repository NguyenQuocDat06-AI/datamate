from __future__ import annotations

import calendar
import csv
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any

from bizreason.contracts import TimeWindow
from bizreason.semantic import rulebook_params
from bizreason.utils import normalize_lookup


# Month names written in words, accent-free; "muoi mot" and "muoi hai" come before "muoi".
_MONTH_WORDS = {
    "gieng": 1, "muoi mot": 11, "muoi hai": 12, "chap": 12, "hai": 2, "ba": 3, "tu": 4, "bon": 4, "nam": 5,
    "sau": 6, "bay": 7, "tam": 8, "chin": 9, "muoi": 10,
}
_TELEX_TOKEN = re.compile(r"[a-z]+")
_VOWELS = set("aeiouy")


def _untelex_word(word: str) -> str:
    """Undo Telex keystrokes left in an accent-free word ("nawm" -> "nam", "xuaats" -> "xuat").

    Accent-free Vietnamese syllables never contain "w", never end in s/f/r/x/j, and never double a/e/d, so those
    can only be Telex marks. Words without a vowel ("vs", "ss") are abbreviations and are kept."""
    if not _VOWELS & set(word):
        return word
    stripped = word
    if len(stripped) > 2 and stripped[-1] in "sfrxj" and stripped[-2] in _VOWELS | set("cmnpthg"):
        stripped = stripped[:-1]
    stripped = stripped.replace("w", "")
    stripped = re.sub(r"([aed])\1", r"\1", stripped)
    return stripped or word


def _untelex(text: str) -> str:
    return _TELEX_TOKEN.sub(lambda m: _untelex_word(m.group()), text)


_MONTH_LIST = re.compile(
    r"\b(?:thang|th|t)\s?(\d{1,2})((?:\s+(?:va\s+)?(?:thang\s?|th|t)?\d{1,2}\b)+)(?:\s+(?:nam\s+)?(20\d{2}))?"
)


def _month_list(text: str, as_of: date) -> list[tuple[int, int]]:
    """Months listed with one year for all ("th7, th8, th9/2025", "các tháng 10, 11, 12 năm 2025"), in order.

    Without a year, the question must speak of months ("mỗi tháng", "tháng nào") and each month is its latest
    occurrence up to today. [] when the list is not a run of valid months."""
    match = _MONTH_LIST.search(text)
    if not match:
        return []
    numbers = [int(match.group(1))] + [int(n) for n in re.findall(r"\d{1,2}", match.group(2))]
    if len(numbers) < 2 or not all(1 <= n <= 12 for n in numbers) or len(set(numbers)) != len(numbers):
        return []
    if match.group(3):
        year = int(match.group(3))
        # a list that runs past December ends in the stated year ("th11, th12, th1/2026")
        out, y = [], year
        for n in reversed(numbers):
            out.append((y, n))
            if n == 1 and len(out) < len(numbers):
                y -= 1
        out.reverse()
        return out if all(a <= b for a, b in zip(out, out[1:])) else []
    if not re.search(r"\b(?:moi thang|cac thang|thang nao|tung thang)\b", text):
        return []
    return [(as_of.year if n <= as_of.month else as_of.year - 1, n) for n in numbers]


def next_month(day: date) -> date:
    return date(day.year + (day.month == 12), 1 if day.month == 12 else day.month + 1, 1)


def _year_month(month: str, year: str) -> tuple[int, int]:
    return (int(year) if len(year) == 4 else 2000 + int(year), int(month))


def previous_month_same_days(as_of: date) -> tuple[date, date]:
    year = as_of.year - (as_of.month == 1)
    month = 12 if as_of.month == 1 else as_of.month - 1
    return date(year, month, 1), date(
        year, month, min(as_of.day, calendar.monthrange(year, month)[1])
    )


class LunarCalendar:
    def __init__(self, path: Path):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            self.rows = list(csv.DictReader(handle))
        days = sorted(date.fromisoformat(row["date"]) for row in self.rows)
        if (
            not days
            or len(set(days)) != len(days)
            or any(b - a != timedelta(days=1) for a, b in zip(days, days[1:]))
        ):
            raise ValueError("Calendar dates must be unique and continuous")

    def tet(self, lunar_year: int, role: str = "target") -> TimeWindow:
        def find(year: int, month: int, day: int) -> date:
            found = [
                date.fromisoformat(row["date"])
                for row in self.rows
                if int(row["lunar_year"]) == year
                and int(row["lunar_month"]) == month
                and int(row["lunar_day"]) == day
                and row.get("lunar_leap", "false").lower() not in {"true", "1"}
            ]
            if len(found) != 1:
                raise ValueError("Required lunar date is not uniquely covered")
            return found[0]

        # Read at call time from the active rulebook parameters (DEFAULTS: 23/12 of the previous lunar year to 10/1).
        params = rulebook_params.active()

        start_month, start_day, offset = params.tet_start
        end_month, end_day = params.tet_end
        when = {-1: "previous_lunar_year", 0: "lunar_year"}.get(offset, f"lunar_year{offset:+d}")
        return TimeWindow(
            role=role,
            start=find(lunar_year + offset, start_month, start_day),
            end_exclusive=find(lunar_year, end_month, end_day) + timedelta(days=1),
            evidence=["calendar.csv", f"tet:{start_day}/{start_month}_{when}..{end_day}/{end_month}"],
        )


class Granularity(Enum):
    RANGE = auto()
    DATE = auto()
    RECENT_DAYS = auto()
    TET = auto()
    TRUOC_TET = auto()
    QUARTER = auto()
    MONTH = auto()
    RELATIVE_MONTH = auto()
    WEEK = auto()
    DAY = auto()
    YEAR = auto()


class ComparisonOperator(Enum):
    NONE = auto()
    PREVIOUS_PERIOD = auto()
    SAME_PERIOD_LAST_YEAR = auto()
    TET_VS_TRUOC_TET = auto()
    TRUOC_TET_VS_TET = auto()
    TET_YEAR = auto()
    EXPLICIT_WINDOW = auto()


@dataclass
class AnchorPeriod:
    window: TimeWindow
    granularity: Granularity
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class MonthMatch:
    month: int
    year: int
    start_pos: int
    end_pos: int


def _extract_explicit_months(question: str, text: str) -> list[MonthMatch]:
    results: list[MonthMatch] = []
    # 1. Slashed months in original question: M/YYYY or M/YY
    for m in re.finditer(
        r"(?<![\d/])(?:(?P<pfx>th[aá]ng|th|t)\s*)?(?P<m>\d{1,2})/(?P<y>20\d{2}|\d{2})(?![\d/])",
        question,
        re.I,
    ):
        prefix = question[max(0, m.start() - 6) : m.start()].lower()
        if re.search(r"qu[yý]", prefix):
            continue
        month = int(m.group("m"))
        year_str = m.group("y")
        pfx = m.group("pfx")
        if not (1 <= month <= 12):
            continue
        if len(year_str) == 4:
            year = int(year_str)
        else:
            yy = int(year_str)
            if pfx or yy in range(20, 36):
                year = 2000 + yy
            else:
                continue
        results.append(MonthMatch(month, year, m.start(), m.end()))

    # 2. Text without slash: thang M nam YYYY, thang M YYYY, th M YYYY, tM YYYY
    for m in re.finditer(r"\b(?:thang|th|t)\s*(\d{1,2})\s*(?:nam\s*)?(20\d{2})\b", text):
        prefix = text[max(0, m.start() - 6) : m.start()]
        if "quy" in prefix:
            continue
        month = int(m.group(1))
        year = int(m.group(2))
        if 1 <= month <= 12:
            if not any(r.month == month and r.year == year for r in results):
                results.append(MonthMatch(month, year, m.start(), m.end()))

    # 3. Comma-separated or 'và'/'đến' month sequences sharing a year:
    # e.g., "tháng 4, 5, 6/2025", "tháng 4, 5 và 6 năm 2025", "tháng 2 đến tháng 4 năm 2026"
    for m in re.finditer(
        r"\b(?:th[aá]ng|th|t)\s*(\d{1,2}(?:\s*(?:[,;&/]|v[aà]|đ[eế]n|t[oớ]i|-)\s*(?:(?:th[aá]ng|th|t)\s*)?\d{1,2})+)\s*(?:/|\s*(?:n[aă]m\s*)?)(20\d{2})\b",
        question,
        re.I,
    ):
        raw_list = m.group(1)
        year = int(m.group(2))
        month_nums = [int(n) for n in re.findall(r"\b\d{1,2}\b", raw_list)]
        for mn in month_nums:
            if 1 <= mn <= 12:
                if not any(r.month == mn and r.year == year for r in results):
                    results.append(MonthMatch(mn, year, m.start(), m.end()))

    # 4. Dash form "M-YYYY" (e.g. "6-2025"), used only as the sole month named in the question:
    # with two or more such tokens the question is ambiguous (range or comparison), so no month.
    if not results:
        dashed = [
            m
            for m in re.finditer(r"(?<![\d/.\-])(\d{1,2})-(20\d{2})(?![\d/\-])", question)
            if 1 <= int(m.group(1)) <= 12
            and not re.search(r"qu[yý]", question[max(0, m.start() - 6) : m.start()].lower())
        ]
        if len(dashed) == 1:
            m = dashed[0]
            results.append(MonthMatch(int(m.group(1)), int(m.group(2)), m.start(), m.end()))

    results.sort(key=lambda r: (r.year, r.month, r.start_pos))
    return results


class TemporalResolver:
    def __init__(self, lunar: LunarCalendar | None = None):
        self.lunar = lunar

    def resolve(self, question: str, as_of: date) -> list[TimeWindow]:
        """Windows of the question; see _resolve_windows. Every full date the question writes must be inside some
        window: two or more written days that the windows leave out are read as one range by their structure
        (_written_day_windows: a range, a comparison or a list), or not at all, so a phrasing no rule knows never
        shrinks to its first day."""
        windows = self._resolve_windows(question, as_of)
        days = _written_days(question)
        if len(days) < 2 or all(any(w.start <= d < w.end_exclusive for w in windows) for d in days):
            return windows
        return _written_day_windows(question, days)

    def _resolve_windows(self, question: str, as_of: date) -> list[TimeWindow]:
        raw_text = _untelex(normalize_lookup(question))
        text = re.sub(r"\btrc\b", "truoc", raw_text)
        text = re.sub(r"\bvs\b", "voi", text)
        text = re.sub(r"\b(?:hqua|hom\s+qa|bua\s+qua)\b", "hom qua", text)
        # Informal spellings of "hôm nay" (hnay, hoom nay, bữa nay) and "tháng này" (th nay, thangs nay, tháng ni).
        text = re.sub(r"\bq([1-4])\b", r"quy \1", text)  # "Q1/2026" is the first quarter
        text = re.sub(r"\bhoom\s+qua\b", "hom qua", text)
        text = re.sub(r"\b(?:hnay|hoom\s+nay|hum\s+nay|bua\s+nay)\b", "hom nay", text)
        text = re.sub(r"\b(?:thangs?|th)\s+(?:nay|ni)\b", "thang nay", text)
        text = re.sub(r"\bnam\s+ni\b", "nam nay", text)
        # Central-dialect "ni" for "nay": hôm ni, bữa ni, tuần ni.
        text = re.sub(r"\b(?:hom|bua)\s+ni\b", "hom nay", text)
        text = re.sub(r"\btuan\s+ni\b", "tuan nay", text)
        # A month written in words ("tháng ba", "tháng giêng", "tháng chạp") is that month number.
        # "tháng năm trước / năm nay" is a month of a year, not May.
        month_alternatives = "|".join(_MONTH_WORDS)  # outside the f-string: nested same-type quotes need Python 3.12
        text = re.sub(rf"\bthang ({month_alternatives})\b(?! \d{{1,2}}\b)(?! (?:truoc|ngoai|nay|ni|roi|sau|kia|qua|toi)\b)",
                      lambda m: f"thang {_MONTH_WORDS[m.group(1)]}", text)
        today_end = as_of + timedelta(days=1)
        evidence = ["question", "as_of_date"]

        def window(start: date, end: date, role: str = "target") -> TimeWindow:
            return TimeWindow(role=role, start=start, end_exclusive=end, evidence=evidence)

        # Rulebook §2: from the start of this year through the end of today.
        if re.search(r"\bdau nam\b(?! \d)(?! ngoai)(?! truoc)", text) or re.search(
            r"\bnam (?:nay|ni)\b.*\b(?:toi|den)\b.*\bhom nay\b", text
        ):
            return [window(date(as_of.year, 1, 1), today_end)]

        # Rulebook §2: "từ D/M tới (hết) hôm nay" = from D/M of as_of's year through today.
        span = re.search(r"\btu (?:ngay )?(\d{1,2}) (\d{1,2}) (?:toi|den) (?:het )?(?:ngay )?hom nay\b", text)
        if span:
            return [window(date(as_of.year, int(span.group(2)), int(span.group(1))), today_end)]

        # Stage 1: Anchor Resolution
        anchor = self._resolve_anchor(question, text, as_of, window, today_end)
        if anchor is None:
            return []

        # Stage 2: Comparison Operator Detection
        op, op_meta = self._detect_comparison(anchor, question, text, as_of)

        # Stage 3: Comparison Window Derivation
        if op == ComparisonOperator.NONE:
            return [anchor.window]

        comp_window = self._derive_comparison(anchor, op, op_meta, as_of, window)
        if comp_window is not None:
            return [anchor.window, comp_window]
        return [anchor.window]

    def resolve_periods(self, question: str, as_of: date) -> list[TimeWindow]:
        """Several whole months named or implied by one question, in the order asked."""
        text = _untelex(normalize_lookup(question))
        # "T4/2025", "th5/2025": a month written with a short prefix right before its year.
        text = re.sub(r"\b(?:th|t)(\d{1,2}) (?=\d{4}\b)", r"thang \1 ", text)
        months: list[tuple[int, int]] = []
        for month, year in re.findall(r"(?<![\d/-])(\d{1,2})[/-](\d{4}|\d{2})(?![\d/-])", question):
            value = (int(year) if len(year) == 4 else 2000 + int(year), int(month))
            if 1 <= value[1] <= 12 and value not in months:
                months.append(value)
        run = re.search(r"\b(\d{1,2}) thang lien", text)
        if run and len(months) == 1:
            year, month = months[0]
            months = []
            for offset in range(int(run.group(1)) - 1, -1, -1):
                index = year * 12 + month - 1 - offset
                months.append((index // 12, index % 12 + 1))
        # Rulebook §2: "từ M/Y tới|đến M/Y" = every month from the first to the last, in order.
        span = re.search(
            r"\btu (?:thang )?(\d{1,2}) (\d{4}|\d{2}) (?:toi|den) (?:thang )?(\d{1,2}) (\d{4}|\d{2})\b",
            text,
        )
        if span and 1 <= int(span.group(1)) <= 12 and 1 <= int(span.group(3)) <= 12:
            first = _year_month(span.group(1), span.group(2))
            last = _year_month(span.group(3), span.group(4))
            if first <= last:
                expanded: list[tuple[int, int]] = []
                index = first[0] * 12 + first[1] - 1
                while index <= last[0] * 12 + last[1] - 1:
                    expanded.append((index // 12, index % 12 + 1))
                    index += 1
                months = expanded + [value for value in months if value not in expanded]
        if len(months) < 2:
            months = _month_list(text, as_of) or months
        if len(months) < 2:
            return []
        return [
            TimeWindow(
                role="target", start=date(y, m, 1), end_exclusive=next_month(date(y, m, 1)),
                evidence=["question", "as_of_date"],
            )
            for y, m in months
        ]

    def _resolve_anchor(
        self,
        question: str,
        text: str,
        as_of: date,
        window: Any,
        today_end: date,
    ) -> AnchorPeriod | None:
        # Rulebook §2: "N ngày kể từ ngày D" = D through D+N-1; "so với N ngày ngày trước đó" = the N days before D.
        # The start day is either written in the same phrase, or ("ngày đó") the single full date named elsewhere.
        # "kể từ ngày <event>" (e.g. the day a price changed) also refers to the single full date named elsewhere.
        counted = re.search(
            r"\b(\d{1,2}) ngay ke tu (?:ngay )?(?:(\d{1,2}) (\d{1,2}) (20\d{2})|do|nay|[a-z]+)\b", text
        )
        day_parts: list[tuple[str, str, str]] = []
        if counted and int(counted.group(1)) > 0 and not counted.group(2):
            day_parts = list(set(re.findall(r"(?<!\d)(\d{1,2})/(\d{1,2})/(20\d{2})(?!\d)", question)))
            if len(day_parts) != 1:
                if re.search(r"\bke tu (?:ngay )?(?:do|nay)\b", text):
                    return None
                # "kể từ ngày <event>" with no single date to stand for it: other patterns may still apply.
                counted = None
        if counted and int(counted.group(1)) > 0:
            n = int(counted.group(1))
            if counted.group(2):
                day_parts = [(counted.group(2), counted.group(3), counted.group(4))]
            try:
                start = date(int(day_parts[0][2]), int(day_parts[0][1]), int(day_parts[0][0]))
            except ValueError:
                return None
            target = window(start, start + timedelta(days=n))
            # Comparison only when the question asks for it: "so với / vs / hơn" N days "liền trước" the start.
            if re.search(rf"\b(?:so voi|voi|hon) {n} ngay (?:lien )?(?:ngay )?truoc\b", text):
                comp = window(start - timedelta(days=n), start, role="comparison")
                return AnchorPeriod(target, Granularity.RANGE, {"comp_window": comp})
            return AnchorPeriod(target, Granularity.RANGE)

        explicit_days = re.findall(r"(?<!\d)(\d{1,2})/(\d{1,2})/(20\d{2})(?!\d)", question)
        if len(explicit_days) == 2 and "tu " in text and " den " in f" {text} ":
            first, last = [
                date(int(year), int(month), int(day)) for day, month, year in explicit_days
            ]
            return AnchorPeriod(window(first, last + timedelta(days=1)), Granularity.RANGE)

        if "tet" in text:
            if self.lunar is None:
                return None
            years = [int(y) for y in re.findall(r"\b(20\d{2})\b", text)]
            if years:
                lunar_year = years[0]
            elif "nam nay" in text:
                # "Tết năm nay so với Tết năm trước": this year's Tết is the target; "năm trước" names the comparison.
                lunar_year = as_of.year
            elif any(k in text for k in ["nam ngoai", "nam truoc", "nam roi"]):
                lunar_year = as_of.year - 1
            else:
                lunar_year = as_of.year

            has_truoc_tet = "truoc tet" in text
            text_without_truoc_tet = re.sub(
                r"(?:\d+\s*ngay\s*)?(?:ngay\s*)?truoc\s*tet", "", text
            )
            has_other_tet = "tet" in text_without_truoc_tet

            if has_truoc_tet and has_other_tet:
                so_voi_pos = text.find("so voi")
                if so_voi_pos != -1:
                    truoc_tet_pos = text.find("truoc tet")
                    tet_after = "tet" in text[so_voi_pos + 6 :]
                    if truoc_tet_pos < so_voi_pos and tet_after:
                        tet_win = self.lunar.tet(lunar_year)
                        duration = tet_win.end_exclusive - tet_win.start
                        pre_win = window(tet_win.start - duration, tet_win.start, role="target")
                        return AnchorPeriod(
                            pre_win,
                            Granularity.TRUOC_TET,
                            {"lunar_year": lunar_year, "comp_type": "tet"},
                        )
                tet_win = self.lunar.tet(lunar_year)
                duration = tet_win.end_exclusive - tet_win.start
                return AnchorPeriod(
                    tet_win,
                    Granularity.TET,
                    {"lunar_year": lunar_year, "comp_type": "truoc_tet", "duration": duration},
                )
            elif has_truoc_tet:
                tet_win = self.lunar.tet(lunar_year)
                duration = tet_win.end_exclusive - tet_win.start
                pre_win = window(tet_win.start - duration, tet_win.start, role="target")
                return AnchorPeriod(
                    pre_win,
                    Granularity.TRUOC_TET,
                    {"lunar_year": lunar_year, "comp_type": None},
                )
            else:
                comp_year = None
                if len(years) >= 2:
                    comp_year = years[1]
                elif any(k in text for k in ["nam ngoai", "nam truoc", "nam roi"]):
                    comp_year = lunar_year - 1
                tet_win = self.lunar.tet(lunar_year)
                return AnchorPeriod(
                    tet_win,
                    Granularity.TET,
                    {
                        "lunar_year": lunar_year,
                        "comp_type": "lunar_year" if comp_year else None,
                        "comp_year": comp_year,
                    },
                )

        # Quarters
        q_match = re.search(r"\bquy\s*([1-4])\b(?:\s*(?:nam\s*)?(20\d{2}))?", text)
        if q_match:
            quarter = int(q_match.group(1))
            year_str = q_match.group(2)
            current_q = (as_of.month - 1) // 3 + 1
            year = (
                int(year_str)
                if year_str
                else (as_of.year if quarter <= current_q else as_of.year - 1)
            )
            start = date(year, (quarter - 1) * 3 + 1, 1)
            end = date(year + 1, 1, 1) if quarter == 4 else date(year, quarter * 3 + 1, 1)
            return AnchorPeriod(
                window(start, end),
                Granularity.QUARTER,
                {"quarter": quarter, "year": year, "is_relative": False},
            )

        if "quy nay" in text:
            current_q = (as_of.month - 1) // 3 + 1
            start = date(as_of.year, (current_q - 1) * 3 + 1, 1)
            return AnchorPeriod(
                window(start, today_end),
                Granularity.QUARTER,
                {"quarter": current_q, "year": as_of.year, "is_relative": True},
            )

        if "quy truoc" in text or "quy lien truoc" in text:
            current_q = (as_of.month - 1) // 3 + 1
            prev_q = 4 if current_q == 1 else current_q - 1
            prev_y = as_of.year - 1 if current_q == 1 else as_of.year
            start = date(prev_y, (prev_q - 1) * 3 + 1, 1)
            end = date(prev_y + 1, 1, 1) if prev_q == 4 else date(prev_y, prev_q * 3 + 1, 1)
            return AnchorPeriod(
                window(start, end),
                Granularity.QUARTER,
                {"quarter": prev_q, "year": prev_y, "is_relative": False},
            )

        # Explicit Months
        explicit_months = _extract_explicit_months(question, text)
        if len(explicit_months) >= 3:
            m_first, m_last = explicit_months[0], explicit_months[-1]
            start = date(m_first.year, m_first.month, 1)
            end = next_month(date(m_last.year, m_last.month, 1))
            return AnchorPeriod(window(start, end), Granularity.RANGE)
        elif len(explicit_months) == 2:
            m0, m1 = explicit_months[0], explicit_months[1]
            q_lower = question.lower()
            text_between = q_lower[m0.end_pos : m1.start_pos]
            so_voi_before = any(k in q_lower[: m0.start_pos] for k in ["so voi", "so vs", "so "])
            is_range = (
                ("tu " in text[:m0.start_pos] or "tu " in text)
                and any(k in f" {text} " or k in text_between for k in [" den ", " toi ", " - "])
            )
            if is_range and not so_voi_before:
                start = date(m0.year, m0.month, 1)
                end = next_month(date(m1.year, m1.month, 1))
                return AnchorPeriod(window(start, end), Granularity.RANGE)
            elif "sang" in text_between:
                start = date(m1.year, m1.month, 1)
                comp_start = date(m0.year, m0.month, 1)
                return AnchorPeriod(
                    window(start, next_month(start)),
                    Granularity.MONTH,
                    {
                        "month": m1.month,
                        "year": m1.year,
                        "comp_window": window(
                            comp_start, next_month(comp_start), role="comparison"
                        ),
                    },
                )
            elif so_voi_before:
                start = date(m1.year, m1.month, 1)
                comp_start = date(m0.year, m0.month, 1)
                return AnchorPeriod(
                    window(start, next_month(start)),
                    Granularity.MONTH,
                    {
                        "month": m1.month,
                        "year": m1.year,
                        "comp_window": window(
                            comp_start, next_month(comp_start), role="comparison"
                        ),
                    },
                )
            else:
                start = date(m0.year, m0.month, 1)
                comp_start = date(m1.year, m1.month, 1)
                return AnchorPeriod(
                    window(start, next_month(start)),
                    Granularity.MONTH,
                    {
                        "month": m0.month,
                        "year": m0.year,
                        "comp_window": window(
                            comp_start, next_month(comp_start), role="comparison"
                        ),
                    },
                )
        elif len(explicit_months) == 1:
            m = explicit_months[0]
            start = date(m.year, m.month, 1)
            return AnchorPeriod(
                window(start, next_month(start)),
                Granularity.MONTH,
                {"month": m.month, "year": m.year},
            )

        # "tháng M năm rồi / năm ngoái / năm trước": month M of the previous calendar year.
        last_year_month = re.search(r"\b(?:thang|th|t)\s*(\d{1,2}) nam (?:roi|ngoai|truoc)\b", text)
        if last_year_month and 1 <= int(last_year_month.group(1)) <= 12:
            month = int(last_year_month.group(1))
            start = date(as_of.year - 1, month, 1)
            return AnchorPeriod(
                window(start, next_month(start)),
                Granularity.MONTH,
                {"month": month, "year": as_of.year - 1, "year_word": True},
            )

        # Unyeared month: "tháng M", "th M"
        m_match = re.search(r"\b(?:thang|th)\s*(\d{1,2})\b", text)
        if m_match:
            month = int(m_match.group(1))
            if 1 <= month <= 12:
                year = as_of.year if month <= as_of.month else as_of.year - 1
                start = date(year, month, 1)
                return AnchorPeriod(
                    window(start, next_month(start)),
                    Granularity.MONTH,
                    {"month": month, "year": year},
                )

        # Relative month (tháng này / implied current month MTD)
        if "thang nay" in text:
            return AnchorPeriod(
                window(as_of.replace(day=1), today_end),
                Granularity.RELATIVE_MONTH,
                {"as_of": as_of},
            )

        if any(token in text for token in ["so voi", "so thang truoc", "cung ky", "tang truong"]):
            if ("thang" in text and "truoc" in text) or ("nam ngoai" in text and "thang" in text):
                return AnchorPeriod(
                    window(as_of.replace(day=1), today_end),
                    Granularity.RELATIVE_MONTH,
                    {"as_of": as_of},
                )

        # Standalone "tháng trước" / "tháng rồi"
        if "thang truoc" in text or "thang roi" in text:
            last = as_of.replace(day=1) - timedelta(days=1)
            return AnchorPeriod(
                window(last.replace(day=1), as_of.replace(day=1)),
                Granularity.MONTH,
                {"month": last.month, "year": last.year},
            )

        # Rulebook §6: "trong N ngày tới" = from today through today + N, both included.
        ahead = re.search(r"\b(\d{1,4}) ngay toi\b", text)
        if ahead and int(ahead.group(1)) > 0:
            return AnchorPeriod(window(as_of, as_of + timedelta(days=int(ahead.group(1)) + 1)), Granularity.RANGE)

        # Recent days
        days = re.search(r"(\d+) ngay (?:gan nhat|qua|vua qua)", text)
        if days and int(days.group(1)) > 0:
            return AnchorPeriod(
                window(as_of - timedelta(days=int(days.group(1)) - 1), today_end),
                Granularity.RECENT_DAYS,
            )

        # Week
        monday = as_of - timedelta(days=as_of.weekday())
        if "tuan nay" in text:
            return AnchorPeriod(
                window(monday, today_end),
                Granularity.WEEK,
                {"monday": monday, "is_current": True},
            )
        if "tuan truoc" in text or "tuan roi" in text:
            return AnchorPeriod(
                window(monday - timedelta(days=7), monday),
                Granularity.WEEK,
                {"monday": monday, "is_current": False},
            )

        # Day
        if "hom nay" in text:
            return AnchorPeriod(
                window(as_of, today_end),
                Granularity.DAY,
                {"as_of": as_of, "is_today": True},
            )
        if "hom qua" in text:
            return AnchorPeriod(
                window(as_of - timedelta(days=1), as_of),
                Granularity.DAY,
                {"as_of": as_of, "is_today": False},
            )

        # Year
        if "nam nay" in text:
            return AnchorPeriod(
                window(date(as_of.year, 1, 1), today_end),
                Granularity.YEAR,
                {"year": as_of.year, "is_current": True},
            )
        if "nam ngoai" in text or "nam truoc" in text:
            return AnchorPeriod(
                window(date(as_of.year - 1, 1, 1), date(as_of.year, 1, 1)),
                Granularity.YEAR,
                {"year": as_of.year - 1, "is_current": False},
            )

        # Explicit single day DD/MM/YYYY
        match = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})/(20\d{2})(?!\d)", question)
        if match:
            day, month, year = map(int, match.groups())
            start = date(year, month, day)
            return AnchorPeriod(window(start, start + timedelta(days=1)), Granularity.DATE)

        return None

    def _detect_comparison(
        self,
        anchor: AnchorPeriod,
        question: str,
        text: str,
        as_of: date,
    ) -> tuple[ComparisonOperator, Any]:
        if "comp_window" in anchor.meta:
            return ComparisonOperator.EXPLICIT_WINDOW, anchor.meta["comp_window"]

        comp_type = anchor.meta.get("comp_type")
        if comp_type == "truoc_tet":
            return ComparisonOperator.TET_VS_TRUOC_TET, None
        if comp_type == "tet":
            return ComparisonOperator.TRUOC_TET_VS_TET, None
        if comp_type == "lunar_year":
            return ComparisonOperator.TET_YEAR, anchor.meta.get("comp_year")

        if anchor.granularity == Granularity.MONTH:
            if any(
                k in text
                for k in [
                    "thang truoc",
                    "th truoc",
                    "thang lien truoc",
                    "th lien truoc",
                    "thang truoc do",
                    "thang ngay truoc",
                    "so thang truoc",
                    "thang roi",
                    "ky truoc",
                    "ky lien truoc",
                ]
            ):
                return ComparisonOperator.PREVIOUS_PERIOD, None
            # "tháng M năm rồi" names the year itself; its "năm rồi" is not a comparison request.
            if not anchor.meta.get("year_word") and any(
                k in text
                for k in [
                    "cung ky nam ngoai",
                    "cung ky nam truoc",
                    "cung ky nam roi",
                    "cung thang nam ngoai",
                    "cung thang nam truoc",
                    "cung thang nam roi",
                    "nam ngoai",
                    "nam truoc",
                    "nam roi",
                ]
            ):
                return ComparisonOperator.SAME_PERIOD_LAST_YEAR, None

        elif anchor.granularity == Granularity.RELATIVE_MONTH:
            if any(
                k in text
                for k in [
                    "thang truoc",
                    "th truoc",
                    "cung ky thang truoc",
                    "cung so ngay dau thang",
                    "thang roi",
                    "so thang truoc",
                ]
            ):
                return ComparisonOperator.PREVIOUS_PERIOD, None
            if any(
                k in text
                for k in [
                    "nam ngoai",
                    "nam truoc",
                    "nam roi",
                    "cung ky nam ngoai",
                    "cung ky nam truoc",
                ]
            ):
                return ComparisonOperator.SAME_PERIOD_LAST_YEAR, None

        elif anchor.granularity == Granularity.QUARTER:
            if any(k in text for k in ["quy truoc", "quy lien truoc"]):
                return ComparisonOperator.PREVIOUS_PERIOD, None
            if any(
                k in text
                for k in [
                    "cung ky nam ngoai",
                    "cung ky nam truoc",
                    "cung ky nam roi",
                    "nam ngoai",
                    "nam truoc",
                    "nam roi",
                ]
            ):
                return ComparisonOperator.SAME_PERIOD_LAST_YEAR, None

        elif anchor.granularity == Granularity.WEEK and anchor.meta.get("is_current"):
            if any(k in text for k in ["tuan truoc", "tuan roi"]):
                return ComparisonOperator.PREVIOUS_PERIOD, None

        elif anchor.granularity == Granularity.DAY and anchor.meta.get("is_today"):
            if "hom qua" in text:
                return ComparisonOperator.PREVIOUS_PERIOD, None

        elif anchor.granularity == Granularity.YEAR and anchor.meta.get("is_current"):
            if any(k in text for k in ["nam ngoai", "nam truoc", "nam roi"]):
                return ComparisonOperator.SAME_PERIOD_LAST_YEAR, None

        return ComparisonOperator.NONE, None

    def _derive_comparison(
        self,
        anchor: AnchorPeriod,
        op: ComparisonOperator,
        op_meta: Any,
        as_of: date,
        window: Any,
    ) -> TimeWindow | None:
        if op == ComparisonOperator.EXPLICIT_WINDOW:
            return op_meta

        if op == ComparisonOperator.TET_VS_TRUOC_TET:
            tet_win = anchor.window
            duration = tet_win.end_exclusive - tet_win.start
            return TimeWindow(
                role="comparison",
                start=tet_win.start - duration,
                end_exclusive=tet_win.start,
                evidence=["calendar.csv", "truoc_tet:same_duration_before_23/12"],
            )

        if op == ComparisonOperator.TRUOC_TET_VS_TET:
            if self.lunar is None:
                return None
            year = anchor.meta["lunar_year"]
            return self.lunar.tet(year, role="comparison")

        if op == ComparisonOperator.TET_YEAR:
            if self.lunar is None:
                return None
            comp_year = op_meta
            try:
                return self.lunar.tet(comp_year, role="comparison")
            except (ValueError, KeyError, IndexError):
                return None

        if op == ComparisonOperator.PREVIOUS_PERIOD:
            if anchor.granularity == Granularity.MONTH:
                m, y = anchor.meta["month"], anchor.meta["year"]
                prev_y = y - 1 if m == 1 else y
                prev_m = 12 if m == 1 else m - 1
                start = date(prev_y, prev_m, 1)
                return window(start, date(y, m, 1), role="comparison")

            if anchor.granularity == Granularity.RELATIVE_MONTH:
                start, end = previous_month_same_days(as_of)
                return window(start, end + timedelta(days=1), role="comparison")

            if anchor.granularity == Granularity.QUARTER:
                q, y = anchor.meta["quarter"], anchor.meta["year"]
                prev_q = 4 if q == 1 else q - 1
                prev_y = y - 1 if q == 1 else y
                start = date(prev_y, (prev_q - 1) * 3 + 1, 1)
                end = date(prev_y + 1, 1, 1) if prev_q == 4 else date(prev_y, prev_q * 3 + 1, 1)
                return window(start, end, role="comparison")

            if anchor.granularity == Granularity.WEEK:
                monday = anchor.meta["monday"]
                return window(monday - timedelta(days=7), monday, role="comparison")

            if anchor.granularity == Granularity.DAY:
                return window(as_of - timedelta(days=1), as_of, role="comparison")

        if op == ComparisonOperator.SAME_PERIOD_LAST_YEAR:
            if anchor.granularity == Granularity.MONTH:
                m, y = anchor.meta["month"], anchor.meta["year"]
                start = date(y - 1, m, 1)
                return window(start, next_month(start), role="comparison")

            if anchor.granularity == Granularity.RELATIVE_MONTH:
                clamped_day = min(as_of.day, calendar.monthrange(as_of.year - 1, as_of.month)[1])
                start = date(as_of.year - 1, as_of.month, 1)
                end = date(as_of.year - 1, as_of.month, clamped_day) + timedelta(days=1)
                return window(start, end, role="comparison")

            if anchor.granularity == Granularity.QUARTER:
                q, y = anchor.meta["quarter"], anchor.meta["year"]
                start = date(y - 1, (q - 1) * 3 + 1, 1)
                end = date(y, 1, 1) if q == 4 else date(y - 1, q * 3 + 1, 1)
                return window(start, end, role="comparison")

            if anchor.granularity == Granularity.YEAR:
                y = anchor.meta["year"]
                return window(date(y - 1, 1, 1), date(y, 1, 1), role="comparison")

        return None

# A full date written day first ("08/08/2026", "8-8-2026").
_WRITTEN_DAY_RE = re.compile(r"(?<![\d/.\-])(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(20\d{2})(?![\d/.\-])")
# Only a dash-like sign between two days: a range ("08/08/2026 – 06/12/2026").
_RANGE_SIGN_RE = re.compile(r"^\s*[-\u2013\u2014~\u2192>]+\s*$")
# The three ways two written days relate, read from the words around them (after normalize_lookup):
# a range opened before the first day ("từ A … B", "giữa A và B"), a comparison ("A so với B"), or a list ("A và B").
_RANGE_OPENERS = ("tu", "giua", "khoang")
_COMPARE_WORDS = ("so voi", "so", "voi", "hon", "kem")
_LIST_WORDS = ("va", "hay", "hoac", "cung", "lan luot")
_MAX_LINK_WORDS = 4  # words between two days that still link them


def _written_days(question: str) -> list[date]:
    days = []
    for day, month, year in _WRITTEN_DAY_RE.findall(question):
        try:
            value = date(int(year), int(month), int(day))
        except ValueError:
            continue
        if value not in days:
            days.append(value)
    return days


def _day(start: date, role: str = "target", end: date | None = None) -> TimeWindow:
    return TimeWindow(role=role, start=start, end_exclusive=(end or start) + timedelta(days=1),
                      evidence=["question", "as_of_date"])


def _written_day_windows(question: str, days: list[date]) -> list[TimeWindow]:
    """Windows for two written days that the rules left out, from how the text links them:
    - a range when only a dash-like sign stands between them, or a range opener ("từ", "giữa", "khoảng") comes just
      before the first day and the link holds no comparison word: [first, last], both days included (rulebook §2);
    - a comparison ("A so với B"): A is the asked period, B the comparison;
    - a list ("A và B", "lần lượt"): each day is its own period, in the order written.
    Anything else (more than two days, a long or unknown link, a range that ends before it starts) gives no window,
    so the solver abstains rather than answering for one day."""
    found = list(_WRITTEN_DAY_RE.finditer(question))
    if len(days) != 2 or len(found) != 2:
        return []
    first, last = days
    between = question[found[0].end():found[1].start()]
    words = normalize_lookup(between).split()
    if len(words) > _MAX_LINK_WORDS:
        return []
    link = f" {' '.join(words)} "
    before = normalize_lookup(question[:found[0].start()]).split()[-2:]
    has = lambda vocabulary: any(f" {w} " in link for w in vocabulary)
    if _RANGE_SIGN_RE.match(between) or (any(w in before for w in _RANGE_OPENERS) and not has(_COMPARE_WORDS)):
        return [_day(first, end=last)] if first <= last else []
    if re.search(r"\b\d{1,3} ngay\b", normalize_lookup(question)):
        # A day count ("10 ngày kể từ ngày đó") hangs on one of the written days: which one is ambiguous.
        return []
    if has(_COMPARE_WORDS):
        return [_day(first), _day(last, role="comparison")]
    if has(_LIST_WORDS):
        return [_day(first), _day(last)]
    return []
