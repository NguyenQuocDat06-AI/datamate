"""Synthetic checks: price-rise windows from the question's own day, price spellings, code-only price lists, redundant
unit filters, regional product-name variants, and the weekday of an 'average per weekday' question."""

import sqlite3
from datetime import date

from bizreason.semantic.compiler import Intent
from bizreason.semantic.solver import SemanticSolver, _weekday_from_question


def _solver(price_rows=(), line_rows=()):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE price_changes (product_id TEXT, product_name TEXT, name_key TEXT, date TEXT, "
                "old_price REAL, new_price REAL)")
    con.executemany("INSERT INTO price_changes VALUES (?,?,?,?,?,?)", price_rows)
    con.execute("CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT)")
    con.execute("CREATE TABLE lines (order_id TEXT, date TEXT, product_id TEXT, product_name TEXT, unit TEXT, "
                "base_unit TEXT, qty REAL, qty_base REAL, amount REAL)")
    con.executemany("INSERT INTO lines VALUES (?,?,?,?,?,?,?,?,?)", line_rows)
    return SemanticSolver(None, None, con)


def _intent(product="Alpha chai", extra=()):
    return Intent(measure="qty_base_sold", shape="compare_periods",
                  filters=[{"dim": "product", "value": product, "exclude": False}, *extra])


def _bounds(windows):
    return [(w.role, w.start, w.end_exclusive) for w in windows]


def test_day_written_in_the_question_sets_both_windows_without_a_price_list():
    solver = _solver()
    windows = solver._price_rise_windows(
        "Alpha chai lên giá 21.000đ từ ngày 01/09/2025; so 30 ngày kể từ ngày tăng giá với 30 ngày ngay trước, "
        "bán tăng hay giảm bao nhiêu %?", _intent())
    assert _bounds(windows) == [("target", date(2025, 9, 1), date(2025, 10, 1)),
                                ("comparison", date(2025, 8, 2), date(2025, 9, 1))]


def test_price_written_with_k_and_slang_and_two_digit_style_dates():
    solver = _solver()
    windows = solver._price_rise_windows(
        "alpha chai len 152k tu 28/5/2025 a, so chai ban 30 ngay tu bua do so vs 30 ngay trc do tang hay giam", _intent())
    assert windows[0].start == date(2025, 5, 28) and windows[1].end_exclusive == date(2025, 5, 28)


def test_new_price_with_currency_words_and_per_unit_suffix():
    rows = [("1", "Alpha chai", None, "2025-01-01", None, 4700), ("1", "Alpha chai", None, "2026-03-02", 4700, 5400)]
    solver = _solver(rows)
    for text in ("Alpha chai từ hồi lên giá 5.400 đồng/gói, 30 ngày đầu bán nhiều hay ít hơn 30 ngày liền trước?",
                 "Alpha chai tang gia len 5.400d/goi, so 7 ngay ke tu ngay tang so voi 7 ngay lien truoc"):
        windows = solver._price_rise_windows(text, _intent())
        assert windows[0].start == date(2026, 3, 2)
    assert windows[0].end_exclusive == date(2026, 3, 9) and windows[1].start == date(2026, 2, 23)


def test_changed_price_written_from_old_to_new_with_a_day():
    solver = _solver()
    windows = solver._price_rise_windows(
        "Alpha chai đổi giá từ 520.000đ lên 546.000đ vào ngày 31/10/2025. 30 ngày kể từ ngày đó bán nhiều hay ít hơn "
        "30 ngày liền trước?", _intent())
    assert windows[0].start == date(2025, 10, 31)


def test_price_list_with_codes_only_uses_the_names_on_the_lines():
    rows = [("P1", None, None, "2025-04-01", None, 17000), ("P1", None, None, "2025-08-25", None, 18000),
            ("P1", None, None, "2025-12-31", None, 19000), ("P2", None, None, "2025-04-01", None, 18000)]
    lines = [("o1", "2025-05-01", "P1", "Alpha chai", "tuýp", None, 1, 1, 10),
             ("o2", "2025-05-01", "P2", "Beta chai", "tuýp", None, 1, 1, 10)]
    solver = _solver(rows, lines)
    windows = solver._price_rise_windows(
        "Kể từ ngày Alpha chai tăng giá lên 18.000đ/tuýp, số tuýp bán trong 30 ngày đầu so với 30 ngày ngay trước đó "
        "nhiều hơn hay ít hơn?", _intent())
    assert windows[0].start == date(2025, 8, 25)  # P1 rose to 18.000 then; P2's first listing is not a rise


def test_question_day_wins_over_price_list_and_is_recorded():
    rows = [("1", "Alpha chai", None, "2025-01-01", None, 90000), ("1", "Alpha chai", None, "2025-03-01", 90000, 97500)]
    solver = _solver(rows)
    windows = solver._price_rise_windows(
        "Alpha chai lên giá 97.500đ từ ngày 10/03/2025, so 14 ngày kể từ ngày đó với 14 ngày liền trước", _intent())
    assert windows[0].start == date(2025, 3, 10)
    assert "question_overrides_price_changes" in windows[0].evidence


# Counter-examples: behaviour that must not change.
def test_no_rise_day_anywhere_gives_no_window():
    solver = _solver()
    assert solver._price_rise_windows(
        "Alpha chai lên giá 97.500đ, so 14 ngày kể từ ngày tăng với 14 ngày liền trước", _intent()) == []


def test_price_that_never_rose_gives_no_window():
    rows = [("1", "Alpha chai", None, "2025-01-01", None, 90000), ("1", "Alpha chai", None, "2025-03-01", 90000, 80000)]
    assert _solver(rows)._price_rise_windows(
        "Alpha chai lên giá 80.000đ, so 14 ngày kể từ ngày tăng với 14 ngày liền trước", _intent()) == []


def test_not_a_price_rise_question_is_left_to_the_time_resolver():
    solver = _solver()
    assert solver._price_rise_windows(
        "Doanh thu 30 ngày kể từ ngày 01/09/2025 so với 30 ngày trước thay đổi bao nhiêu %?", _intent()) == []


def test_two_different_days_or_two_different_counts_are_not_guessed():
    solver = _solver()
    assert solver._price_rise_windows(
        "Alpha chai lên giá 21.000đ từ 01/09/2025 hoặc 05/09/2025, so 30 ngày với 30 ngày trước", _intent()) == []
    assert solver._price_rise_windows(
        "Alpha chai lên giá 21.000đ từ ngày 01/09/2025, so 30 ngày với 60 ngày trước", _intent()) == []


def test_same_period_last_year_is_not_the_days_before():
    solver = _solver()
    assert solver._price_rise_windows(
        "Alpha chai lên giá 21.000đ từ ngày 01/09/2025, so 30 ngày kể từ ngày tăng với cùng kỳ năm trước", _intent()) == []


# Unit filters.
LINES = [("o1", "2025-05-01", "P1", "Alpha chai", "bình", None, 2, 2, 10),
         ("o2", "2025-05-02", "P1", "Alpha chai", "bình", None, 1, 1, 5),
         ("o3", "2025-05-02", "P2", "Beta thung", "thùng", "chai", 1, 12, 5),
         ("o4", "2025-05-02", "P2", "Beta thung", "chai", "chai", 3, 3, 5),
         ("o5", "2025-05-02", "P3", "Gamma", "hộp", "gói", 1, 10, 5)]


def test_unit_the_product_is_only_sold_in_is_dropped():
    solver = _solver(line_rows=LINES)
    intent = _intent(extra=[{"dim": "unit", "value": "bình", "exclude": False}])
    out = solver._drop_redundant_units(intent)
    assert out is not None and [f["dim"] for f in out.filters] == ["product"]


def test_unit_that_differs_for_some_line_is_kept():
    solver = _solver(line_rows=LINES)
    # Beta thung lines are counted in chai: asking for the thùng is not redundant, and neither is a wrong unit.
    assert solver._drop_redundant_units(_intent("Beta thung", [{"dim": "unit", "value": "thùng", "exclude": False}])) is None
    assert solver._drop_redundant_units(_intent("Alpha chai", [{"dim": "unit", "value": "hộp", "exclude": False}])) is None


def test_unit_filter_is_kept_for_other_measures_and_excluded_units():
    solver = _solver(line_rows=LINES)
    other = Intent(measure="revenue", shape="scalar", filters=[
        {"dim": "product", "value": "Alpha chai", "exclude": False}, {"dim": "unit", "value": "bình", "exclude": False}])
    assert solver._drop_redundant_units(other) is None
    excluded = _intent(extra=[{"dim": "unit", "value": "bình", "exclude": True}])
    assert solver._drop_redundant_units(excluded) is None
    scalar = Intent(measure="qty_base_sold", shape="scalar", filters=_intent(extra=[
        {"dim": "unit", "value": "bình", "exclude": False}]).filters)
    assert solver._drop_redundant_units(scalar) is None


# Regional product-name variants.
def test_regional_variant_is_used_when_only_it_matches():
    lines = [("o1", "2025-05-01", "P1", "Đậu phộng rang 200g", "gói", None, 1, 1, 5)]
    solver = _solver(line_rows=lines)
    out = solver._product_variants(_intent("Lạc rang 200g"))
    assert out is not None and out.filters[0]["value"] == "dau phong rang 200g"


def test_regional_variant_leaves_a_name_that_already_matches_or_matches_nothing():
    lines = [("o1", "2025-05-01", "P1", "Lạc rang 200g", "gói", None, 1, 1, 5),
             ("o2", "2025-05-01", "P2", "Đậu phộng rang 200g", "gói", None, 1, 1, 5)]
    solver = _solver(line_rows=lines)
    assert solver._product_variants(_intent("Lạc rang 200g")) is None  # already exact
    assert solver._product_variants(_intent("Xoài sấy")) is None  # no variant word, nothing to try


# Weekday of an average.
def _average(filters=()):
    return Intent(measure="order_count", shape="scalar", compare_as="average_per_weekday", filters=list(filters))


def test_weekday_named_in_an_average_question_is_found():
    for text, value in (("Th3/2026 trung bình mỗi thứ Tư tiệm xử mấy lượt xe?", "thu tu"),
                        ("kac ngay thứ 3 thang nay trung binh 1 ngay co bao nhieu luot xe", "thu 3"),
                        ("trung bình mỗi chủ nhật có bao nhiêu đơn", "chu nhat")):
        found = _weekday_from_question(text, _average())
        assert found is not None and found["value"] == value and found["dim"] == "weekday"


def test_rank_or_order_phrases_are_not_a_weekday():
    assert _weekday_from_question("trung bình mỗi ngày, xếp thứ 3 theo thứ tự giảm dần?", _average()) is None
    assert _weekday_from_question("top 3 hôm nào trung bình đơn nhiều nhất, hạng thứ 3", _average()) is None


def test_weekday_not_added_when_not_an_average_or_already_filtered_or_two_days():
    plain = Intent(measure="order_count", shape="scalar")
    assert _weekday_from_question("Có bao nhiêu đơn thứ 3?", plain) is None
    assert _weekday_from_question("trung bình mỗi thứ 3", _average([{"dim": "weekday", "value": "thu 3"}])) is None
    assert _weekday_from_question("trung bình mỗi thứ 3 và thứ 5", _average()) is None
