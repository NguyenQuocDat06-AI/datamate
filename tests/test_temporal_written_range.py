"""Two written days: a range read from structure (rulebook §2, both days included), else no window. Synthetic."""
from datetime import date

import pytest

from bizreason.temporal import TemporalResolver


@pytest.fixture(scope="module")
def resolver():
    return TemporalResolver()


@pytest.mark.parametrize("text", [
    "Mấy lô có hạn dùng từ 08/08/2026 tới hết 06/12/2026?",
    "tu 08/08/2026 toi het 06/12/2026 con bao nhieu",
    "từ 08/08/2026 tới 06/12/2026",
    "từ 08/08/2026 đến 06/12/2026",
])
def test_two_days_joined_by_toi_or_den_are_a_range(resolver, text):
    windows = resolver.resolve(text, date(2026, 8, 8))
    assert [(w.start, w.end_exclusive) for w in windows] == [(date(2026, 8, 8), date(2026, 12, 7))]




@pytest.mark.parametrize("text", [
    "từ 08/08/2026 cho tới 06/12/2026", "kể từ 08/08/2026 cho đến hết 06/12/2026",
    "08/08/2026 - 06/12/2026", "08/08/2026 – 06/12/2026", "giai đoạn 08/08/2026 ~ 06/12/2026",
])
def test_range_is_read_from_structure_not_from_one_connector_word(resolver, text):
    windows = resolver.resolve(text, date(2026, 8, 8))
    assert [(w.start, w.end_exclusive) for w in windows] == [(date(2026, 8, 8), date(2026, 12, 7))]


def test_two_days_compared_are_the_asked_day_and_the_comparison_day(resolver):
    windows = resolver.resolve("doanh thu ngày 01/03/2026 so với ngày 05/03/2026", date(2026, 8, 8))
    assert [(w.start, w.end_exclusive, w.role) for w in windows] == [
        (date(2026, 3, 1), date(2026, 3, 2), "target"), (date(2026, 3, 5), date(2026, 3, 6), "comparison")]


def test_two_days_listed_are_two_periods_in_written_order(resolver):
    windows = resolver.resolve("doanh thu ngày 05/03/2026 và ngày 01/03/2026 lần lượt", date(2026, 8, 8))
    assert [(w.start, w.role) for w in windows] == [(date(2026, 3, 5), "target"), (date(2026, 3, 1), "target")]


def test_between_two_days_is_a_range(resolver):
    windows = resolver.resolve("trong khoảng 08/08/2026 và 06/12/2026", date(2026, 8, 8))
    assert [(w.start, w.end_exclusive) for w in windows] == [(date(2026, 8, 8), date(2026, 12, 7))]


@pytest.mark.parametrize("text", [
    "ngày 01/03/2026 khách đặt, đến khi giao hàng thì đã là ngày 05/03/2026",  # long link: relation unknown
    "từ 06/12/2026 tới 08/08/2026",  # a range that ends before it starts
])
def test_two_days_with_an_unreadable_link_give_no_window_instead_of_the_first_day(resolver, text):
    assert resolver.resolve(text, date(2026, 8, 8)) == []


def test_one_written_day_is_unchanged_counterexample(resolver):
    windows = resolver.resolve("doanh thu ngày 01/03/2026", date(2026, 8, 8))
    assert [(w.start, w.end_exclusive) for w in windows] == [(date(2026, 3, 1), date(2026, 3, 2))]
