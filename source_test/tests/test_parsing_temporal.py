from datetime import date
from decimal import Decimal

import pytest

from bizreason.data.parsing import ParseStatus, parse_date, parse_number, parse_status
from bizreason.temporal import TemporalResolver, previous_month_same_days


@pytest.mark.parametrize(
    "raw,locale,expected",
    [
        ("396k", "unknown", "396000"),
        ("79.000đ", "unknown", "79000"),
        ("1,5 kg", "vi", "1.5"),
        ("1.234", "vi", "1234"),
        ("1.234", "en", "1.234"),
        ("1,234.50", "en", "1234.50"),
        ("1.234,50", "vi", "1234.50"),
        ("(150)", "unknown", "-150"),
        (0, "unknown", "0"),
    ],
)
def test_decimal_parsing(raw, locale, expected):
    result = parse_number(raw, locale=locale)
    assert result.status == ParseStatus.OK
    assert result.value == Decimal(expected)


@pytest.mark.parametrize(
    "raw,status",
    [
        (None, ParseStatus.EMPTY),
        ("1.234", ParseStatus.AMBIGUOUS),
        ("oops", ParseStatus.INVALID),
        (True, ParseStatus.INVALID),
        ("NaN", ParseStatus.INVALID),
    ],
)
def test_invalid_numbers_are_not_zero(raw, status):
    result = parse_number(raw)
    assert result.status == status
    assert result.value is None


def test_dates_require_a_documented_year():
    assert parse_date("04/03").status == ParseStatus.AMBIGUOUS
    assert parse_date("04/03", year=2026).value == "2026-03-04"
    assert parse_date("31/02/2026").status == ParseStatus.INVALID
    assert parse_date("2026-03-04 12:30:00").value == "2026-03-04 12:30:00"


def test_unknown_status_is_not_completed():
    assert parse_status("Đang giao").value == "shipping"
    assert parse_status("Trạng thái mới").status == ParseStatus.INVALID


@pytest.mark.parametrize(
    "as_of,expected",
    [("2026-03-31", "2026-02-28"), ("2024-03-31", "2024-02-29"), ("2026-01-15", "2025-12-15")],
)
def test_previous_month_clamps(as_of, expected):
    assert previous_month_same_days(date.fromisoformat(as_of))[1].isoformat() == expected


@pytest.mark.parametrize(
    "query,start,end",
    [
        ("tuần rồi", "2026-02-23", "2026-03-02"),
        ("tuần này", "2026-03-02", "2026-03-05"),
        ("tháng này", "2026-03-01", "2026-03-05"),
        ("tháng trước", "2026-02-01", "2026-03-01"),
        ("3 ngày gần nhất", "2026-03-02", "2026-03-05"),
        ("tháng 12", "2025-12-01", "2026-01-01"),
        ("quý 4", "2025-10-01", "2026-01-01"),
    ],
)
def test_temporal_windows(query, start, end):
    window = TemporalResolver().resolve(query, date(2026, 3, 4))[0]
    assert (window.start.isoformat(), window.end_exclusive.isoformat()) == (start, end)
