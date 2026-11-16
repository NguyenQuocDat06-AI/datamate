from datetime import date
from pathlib import Path

import pytest

from bizreason.temporal import LunarCalendar, TemporalResolver

CALENDAR_PATH = Path(__file__).resolve().parents[1] / "data" / "public_test" / "calendar.csv"


@pytest.fixture
def lunar_calendar() -> LunarCalendar:
    return LunarCalendar(CALENDAR_PATH)


@pytest.fixture
def resolver(lunar_calendar: LunarCalendar) -> TemporalResolver:
    return TemporalResolver(lunar_calendar)


def test_tet_vs_truoc_tet_standard(resolver: TemporalResolver):
    windows = resolver.resolve("Doanh thu dịp Tết 2025 so với trước Tết", date(2025, 3, 1))
    assert len(windows) == 2
    target, comparison = windows[0], windows[1]
    assert target.role == "target"
    assert comparison.role == "comparison"
    assert (target.start.isoformat(), target.end_exclusive.isoformat()) == (
        "2025-01-22",
        "2025-02-08",
    )
    assert (comparison.start.isoformat(), comparison.end_exclusive.isoformat()) == (
        "2025-01-05",
        "2025-01-22",
    )


def test_tet_vs_truoc_tet_with_explicit_days(resolver: TemporalResolver):
    windows = resolver.resolve(
        "Dịp Tết âm lịch 2026 có doanh thu hơn hay kém 17 ngày trước Tết bao nhiêu %?",
        date(2026, 8, 8),
    )
    assert len(windows) == 2
    target, comparison = windows[0], windows[1]
    assert target.role == "target"
    assert comparison.role == "comparison"
    assert (target.start.isoformat(), target.end_exclusive.isoformat()) == (
        "2026-02-10",
        "2026-02-27",
    )
    assert (comparison.start.isoformat(), comparison.end_exclusive.isoformat()) == (
        "2026-01-24",
        "2026-02-10",
    )


def test_truoc_tet_vs_tet_reversed_phrasing(resolver: TemporalResolver):
    windows = resolver.resolve("trước Tết 2025 so với Tết", date(2025, 3, 1))
    assert len(windows) == 2
    target, comparison = windows[0], windows[1]
    assert target.role == "target"
    assert comparison.role == "comparison"
    assert (target.start.isoformat(), target.end_exclusive.isoformat()) == (
        "2025-01-05",
        "2025-01-22",
    )
    assert (comparison.start.isoformat(), comparison.end_exclusive.isoformat()) == (
        "2025-01-22",
        "2025-02-08",
    )


def test_standalone_truoc_tet(resolver: TemporalResolver):
    windows = resolver.resolve("Doanh thu kỳ trước Tết 2025 là bao nhiêu?", date(2025, 3, 1))
    assert len(windows) == 1
    target = windows[0]
    assert target.role == "target"
    assert (target.start.isoformat(), target.end_exclusive.isoformat()) == (
        "2025-01-05",
        "2025-01-22",
    )


def test_tet_vs_previous_year_tet(resolver: TemporalResolver):
    windows = resolver.resolve("Tết 2026 so với Tết năm ngoái", date(2026, 8, 8))
    assert len(windows) == 2
    target, comparison = windows[0], windows[1]
    assert target.role == "target"
    assert comparison.role == "comparison"
    assert (target.start.isoformat(), target.end_exclusive.isoformat()) == (
        "2026-02-10",
        "2026-02-27",
    )
    assert (comparison.start.isoformat(), comparison.end_exclusive.isoformat()) == (
        "2025-01-22",
        "2025-02-08",
    )


def test_tet_previous_year_graceful_fallback(resolver: TemporalResolver):
    # 2024 is the earliest covered tet year in calendar.csv (2023 requires 2022 lunar rows)
    windows = resolver.resolve("Tết 2024 so với Tết năm ngoái", date(2024, 8, 8))
    assert len(windows) == 1
    target = windows[0]
    assert target.role == "target"
    assert (target.start.isoformat(), target.end_exclusive.isoformat()) == (
        "2024-02-02",
        "2024-02-20",
    )


def test_explicit_month_vs_previous_month_uncorrupted_by_as_of(resolver: TemporalResolver):
    # Context bug #2: question has explicit month 5/2025, but as_of is 2025-07-15
    windows = resolver.resolve("Doanh thu tháng 5/2025 so với tháng trước", date(2025, 7, 15))
    assert len(windows) == 2
    target, comparison = windows[0], windows[1]
    assert target.role == "target"
    assert comparison.role == "comparison"
    assert (target.start.isoformat(), target.end_exclusive.isoformat()) == (
        "2025-05-01",
        "2025-06-01",
    )
    assert (comparison.start.isoformat(), comparison.end_exclusive.isoformat()) == (
        "2025-04-01",
        "2025-05-01",
    )


def test_explicit_month_year_boundary_comparison(resolver: TemporalResolver):
    windows = resolver.resolve(
        "Doanh thu tháng 1/2026 so với tháng liền trước", date(2026, 5, 25)
    )
    assert len(windows) == 2
    target, comparison = windows[0], windows[1]
    assert target.role == "target"
    assert comparison.role == "comparison"
    assert (target.start.isoformat(), target.end_exclusive.isoformat()) == (
        "2026-01-01",
        "2026-02-01",
    )
    assert (comparison.start.isoformat(), comparison.end_exclusive.isoformat()) == (
        "2025-12-01",
        "2026-01-01",
    )


def test_explicit_month_vs_same_period_last_year(resolver: TemporalResolver):
    windows = resolver.resolve(
        "Doanh thu tháng 4/2026 so với cùng kỳ năm ngoái thay đổi bao nhiêu phần trăm?",
        date(2026, 8, 21),
    )
    assert len(windows) == 2
    target, comparison = windows[0], windows[1]
    assert target.role == "target"
    assert comparison.role == "comparison"
    assert (target.start.isoformat(), target.end_exclusive.isoformat()) == (
        "2026-04-01",
        "2026-05-01",
    )
    assert (comparison.start.isoformat(), comparison.end_exclusive.isoformat()) == (
        "2025-04-01",
        "2025-05-01",
    )


def test_two_explicit_months_so_voi(resolver: TemporalResolver):
    windows = resolver.resolve(
        "Th 6/26 dthu so với th 6/25 tăng hay giảm mấy %?", date(2026, 9, 6)
    )
    assert len(windows) == 2
    target, comparison = windows[0], windows[1]
    assert target.role == "target"
    assert comparison.role == "comparison"
    assert (target.start.isoformat(), target.end_exclusive.isoformat()) == (
        "2026-06-01",
        "2026-07-01",
    )
    assert (comparison.start.isoformat(), comparison.end_exclusive.isoformat()) == (
        "2025-06-01",
        "2025-07-01",
    )


def test_two_explicit_months_tu_sang(resolver: TemporalResolver):
    windows = resolver.resolve("từ tháng 5/2025 sang tháng 6/2025", date(2025, 8, 1))
    assert len(windows) == 2
    target, comparison = windows[0], windows[1]
    assert target.role == "target"
    assert comparison.role == "comparison"
    assert (target.start.isoformat(), target.end_exclusive.isoformat()) == (
        "2025-06-01",
        "2025-07-01",
    )
    assert (comparison.start.isoformat(), comparison.end_exclusive.isoformat()) == (
        "2025-05-01",
        "2025-06-01",
    )


def test_unyeared_month_wraparound(resolver: TemporalResolver):
    # as_of = 2026-03-04 -> month 12 is in 2025
    windows = resolver.resolve("tháng 12 so với tháng trước", date(2026, 3, 4))
    assert len(windows) == 2
    target, comparison = windows[0], windows[1]
    assert (target.start.isoformat(), target.end_exclusive.isoformat()) == (
        "2025-12-01",
        "2026-01-01",
    )
    assert (comparison.start.isoformat(), comparison.end_exclusive.isoformat()) == (
        "2025-11-01",
        "2025-12-01",
    )


def test_unyeared_month_current_year(resolver: TemporalResolver):
    # as_of = 2026-03-04 -> month 2 is in 2026
    windows = resolver.resolve("tháng 2 so với tháng trước", date(2026, 3, 4))
    assert len(windows) == 2
    target, comparison = windows[0], windows[1]
    assert (target.start.isoformat(), target.end_exclusive.isoformat()) == (
        "2026-02-01",
        "2026-03-01",
    )
    assert (comparison.start.isoformat(), comparison.end_exclusive.isoformat()) == (
        "2026-01-01",
        "2026-02-01",
    )


@pytest.mark.parametrize(
    "as_of,target_start,target_end,comp_start,comp_end",
    [
        ("2026-03-31", "2026-03-01", "2026-04-01", "2026-02-01", "2026-03-01"),
        ("2024-03-31", "2024-03-01", "2024-04-01", "2024-02-01", "2024-03-01"),
    ],
)
def test_relative_mtd_leap_and_nonleap_clamping(
    resolver: TemporalResolver,
    as_of: str,
    target_start: str,
    target_end: str,
    comp_start: str,
    comp_end: str,
):
    windows = resolver.resolve(
        "tháng này so với cùng kỳ tháng trước", date.fromisoformat(as_of)
    )
    assert len(windows) == 2
    target, comparison = windows[0], windows[1]
    assert (target.start.isoformat(), target.end_exclusive.isoformat()) == (
        target_start,
        target_end,
    )
    assert (comparison.start.isoformat(), comparison.end_exclusive.isoformat()) == (
        comp_start,
        comp_end,
    )


def test_quarter_anchors_and_comparisons(resolver: TemporalResolver):
    # Explicit quarter with year
    w = resolver.resolve("quý 4/2025", date(2026, 1, 2))
    assert len(w) == 1
    assert (w[0].start.isoformat(), w[0].end_exclusive.isoformat()) == ("2025-10-01", "2026-01-01")

    # Unyeared quarter wrap-around
    w = resolver.resolve("quý 4", date(2026, 3, 4))
    assert len(w) == 1
    assert (w[0].start.isoformat(), w[0].end_exclusive.isoformat()) == ("2025-10-01", "2026-01-01")

    # Unyeared quarter same year
    w = resolver.resolve("quý 1", date(2026, 3, 4))
    assert len(w) == 1
    assert (w[0].start.isoformat(), w[0].end_exclusive.isoformat()) == ("2026-01-01", "2026-04-01")

    # Relative quarter vs previous quarter
    w = resolver.resolve("quý này so với quý trước", date(2026, 3, 4))
    assert len(w) == 2
    assert (w[0].start.isoformat(), w[0].end_exclusive.isoformat()) == ("2026-01-01", "2026-03-05")
    assert (w[1].start.isoformat(), w[1].end_exclusive.isoformat()) == ("2025-10-01", "2026-01-01")

    # Explicit quarter vs previous quarter
    w = resolver.resolve("quý 2/2026 so với quý trước", date(2026, 8, 31))
    assert len(w) == 2
    assert (w[0].start.isoformat(), w[0].end_exclusive.isoformat()) == ("2026-04-01", "2026-07-01")
    assert (w[1].start.isoformat(), w[1].end_exclusive.isoformat()) == ("2026-01-01", "2026-04-01")

    # Explicit quarter vs same quarter last year
    w = resolver.resolve("quý 2/2026 so với cùng kỳ năm ngoái", date(2026, 8, 31))
    assert len(w) == 2
    assert (w[0].start.isoformat(), w[0].end_exclusive.isoformat()) == ("2026-04-01", "2026-07-01")
    assert (w[1].start.isoformat(), w[1].end_exclusive.isoformat()) == ("2025-04-01", "2025-07-01")


def test_week_and_day_comparisons(resolver: TemporalResolver):
    # Week comparison
    w = resolver.resolve("tuần này so với tuần trước", date(2026, 3, 4))
    assert len(w) == 2
    assert (w[0].start.isoformat(), w[0].end_exclusive.isoformat()) == ("2026-03-02", "2026-03-05")
    assert (w[1].start.isoformat(), w[1].end_exclusive.isoformat()) == ("2026-02-23", "2026-03-02")

    # Day comparison
    w = resolver.resolve("hôm nay so với hôm qua", date(2026, 3, 4))
    assert len(w) == 2
    assert (w[0].start.isoformat(), w[0].end_exclusive.isoformat()) == ("2026-03-04", "2026-03-05")
    assert (w[1].start.isoformat(), w[1].end_exclusive.isoformat()) == ("2026-03-03", "2026-03-04")


def test_intraday_shift_does_not_create_spurious_temporal_comparison(
    resolver: TemporalResolver,
):
    # Shift comparison within a week should remain a single week target window
    w = resolver.resolve(
        "tuần trước ca chiều so với ca sáng thì doanh thu chênh bao nhiêu %?",
        date(2026, 3, 12),
    )
    assert len(w) == 1
    assert w[0].role == "target"
    assert (w[0].start.isoformat(), w[0].end_exclusive.isoformat()) == ("2026-03-02", "2026-03-09")

    # Shift comparison within a month should remain a single month target window
    w = resolver.resolve(
        "Tháng 1 năm 2026 dthu ca chiều so với ca sáng chênh bao nhiêu %?",
        date(2026, 6, 10),
    )
    assert len(w) == 1
    assert w[0].role == "target"
    assert (w[0].start.isoformat(), w[0].end_exclusive.isoformat()) == ("2026-01-01", "2026-02-01")


@pytest.mark.parametrize("question", [
    "doanh thu tet nam nay so voi tet nam truoc thay doi may phan tram",
    "Doanh thu dịp Tết năm nay thay đổi bao nhiêu % so với dịp Tết năm ngoái?",
])
def test_tet_this_year_vs_last_year(resolver: TemporalResolver, question: str):
    target, comparison = resolver.resolve(question, date(2026, 8, 8))
    assert (target.start.isoformat(), target.end_exclusive.isoformat()) == ("2026-02-10", "2026-02-27")
    assert (comparison.start.isoformat(), comparison.end_exclusive.isoformat()) == ("2025-01-22", "2025-02-08")


@pytest.mark.parametrize("question", [
    "Tính từ ngày 1/1 đến hết hôm nay, doanh thu năm nay là bao nhiêu?",
    "tinh tu ngay 1/1 den het hnay, dthu nam nay la bn",
    "từ 1/1 tới hôm nay bán được bao nhiêu",
])
def test_from_day_month_through_today(resolver: TemporalResolver, question: str):
    (window,) = resolver.resolve(question, date(2026, 6, 18))
    assert (window.start.isoformat(), window.end_exclusive.isoformat()) == ("2026-01-01", "2026-06-19")
