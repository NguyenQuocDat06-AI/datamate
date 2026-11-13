from datetime import date

from bizreason.temporal import TemporalResolver


def _spans(windows):
    return [(w.role, w.start.isoformat(), w.end_exclusive.isoformat()) for w in windows]


def test_item1_periods_span_expands_every_month():
    resolver = TemporalResolver()
    windows = resolver.resolve_periods(
        "Dthu từng tháng từ 02/2025 tới 04/2025 lần lượt bnhiu giùm?", date(2026, 11, 10)
    )
    assert _spans(windows) == [
        ("target", "2025-02-01", "2025-03-01"),
        ("target", "2025-03-01", "2025-04-01"),
        ("target", "2025-04-01", "2025-05-01"),
    ]


def test_item1_periods_span_with_tháng_prefixes():
    resolver = TemporalResolver()
    windows = resolver.resolve_periods(
        "Từ tháng 4/2025 tới tháng 6/2025, tháng nào doanh thu cao nhất?", date(2026, 11, 10)
    )
    assert _spans(windows) == [
        ("target", "2025-04-01", "2025-05-01"),
        ("target", "2025-05-01", "2025-06-01"),
        ("target", "2025-06-01", "2025-07-01"),
    ]


def test_item2_from_day_month_to_today_uses_as_of_year():
    resolver = TemporalResolver()
    windows = resolver.resolve(
        "Tính từ 1/1 tới hết hôm nay, năm nay quán bán được bao nhiêu tiền?", date(2025, 4, 10)
    )
    assert _spans(windows) == [("target", "2025-01-01", "2025-04-11")]


def test_item3_single_month_still_resolves():
    resolver = TemporalResolver()
    windows = resolver.resolve("Tháng 2/2026 doanh thu bao nhiêu?", date(2026, 11, 10))
    assert _spans(windows) == [("target", "2026-02-01", "2026-03-01")]


def test_item4_month_comparison_previous_month_variants():
    resolver = TemporalResolver()
    expected = [
        ("target", "2025-07-01", "2025-08-01"),
        ("comparison", "2025-06-01", "2025-07-01"),
    ]
    for question in [
        "dthu thang 7/2025 tang hay giam bao nhieu % so voi th truoc",
        "Doanh thu tháng 7/2025 so với tháng trước là bao nhiêu?",
        "Doanh thu tháng 7/2025 so với kỳ trước là bao nhiêu?",
    ]:
        assert _spans(resolver.resolve(question, date(2026, 11, 10))) == expected, question


def test_item5_n_days_from_date_vs_n_days_before():
    resolver = TemporalResolver()
    windows = resolver.resolve(
        "30 ngày kể từ ngày 11/05/2026 doanh thu so với 30 ngày ngay trước đó bao nhiêu?",
        date(2026, 11, 10),
    )
    assert _spans(windows) == [
        ("target", "2026-05-11", "2026-06-10"),
        ("comparison", "2026-04-11", "2026-05-11"),
    ]


def test_item6_this_month_vs_previous_month_same_days():
    resolver = TemporalResolver()
    windows = resolver.resolve("tháng này so với tháng trước doanh thu", date(2026, 3, 15))
    assert _spans(windows) == [
        ("target", "2026-03-01", "2026-03-16"),
        ("comparison", "2026-02-01", "2026-02-16"),
    ]
    # Previous month has no day 31: comparison runs to the end of that month.
    windows = resolver.resolve("tháng này so với tháng trước doanh thu", date(2026, 3, 31))
    assert _spans(windows) == [
        ("target", "2026-03-01", "2026-04-01"),
        ("comparison", "2026-02-01", "2026-03-01"),
    ]


def test_n_days_from_date_named_earlier_as_ngay_do_with_n_days_lien_truoc():
    resolver = TemporalResolver()
    windows = resolver.resolve(
        "Vào ngày 05/03/2026 có đợt tăng giá. Số lượng bán trong 10 ngày kể từ ngày đó "
        "nhiều hay ít hơn 10 ngày liền trước bao nhiêu %?",
        date(2026, 7, 9),
    )
    assert _spans(windows) == [
        ("target", "2026-03-05", "2026-03-15"),
        ("comparison", "2026-02-23", "2026-03-05"),
    ]
    # Several full dates and "ngày đó": the start day is ambiguous, so no window.
    windows = resolver.resolve(
        "Ngày 05/03/2026 và ngày 06/03/2026: 10 ngày kể từ ngày đó bán được bao nhiêu?",
        date(2026, 7, 9),
    )
    assert windows == []


def test_n_days_from_date_vs_n_days_truoc_do_short_forms():
    resolver = TemporalResolver()
    windows = resolver.resolve(
        "tu ngay 12/04/2026, sản phẩm tăng giá. 7 ngày kể từ ngày 12/04/2026 so vs 7 ngay truoc do thay doi bao nhieu %",
        date(2026, 7, 9),
    )
    assert _spans(windows) == [
        ("target", "2026-04-12", "2026-04-19"),
        ("comparison", "2026-04-05", "2026-04-12"),
    ]


def test_informal_today_spellings_hnay_hoom_nay_bua_nay():
    resolver = TemporalResolver()
    for question in [
        "số đơn hnay là bao nhiêu",
        "số đơn hoom nay là bao nhiêu",
        "bữa nay cửa hàng bán được bao nhiêu tiền",
    ]:
        assert _spans(resolver.resolve(question, date(2026, 5, 20))) == [
            ("target", "2026-05-20", "2026-05-21")
        ], question


def test_informal_this_month_spellings_th_nay_thangs_nay_thang_ni():
    resolver = TemporalResolver()
    for question in [
        "Th nay khách trả nợ được bao nhiêu?",
        "Thangs nay NV001 bán được bao nhiêu tiền?",
        "Khách thân thiết tháng ni bình quân mỗi đơn bao nhiêu?",
    ]:
        assert _spans(resolver.resolve(question, date(2026, 8, 21))) == [
            ("target", "2026-08-01", "2026-08-22")
        ], question


def test_month_year_with_dash_as_sole_month():
    resolver = TemporalResolver()
    assert _spans(resolver.resolve("6-2025 bán được bao nhiêu chiếc?", date(2026, 7, 31))) == [
        ("target", "2025-06-01", "2025-07-01")
    ]
    assert _spans(resolver.resolve("1-2026 món nào lãi gộp cao nhất?", date(2026, 8, 21))) == [
        ("target", "2026-01-01", "2026-02-01")
    ]
    # Two dash-form months could be a range or a comparison: no guess.
    assert resolver.resolve("doanh thu 2-2025 đến 4-2025 bao nhiêu", date(2026, 8, 21)) == []


def test_month_with_last_year_word_is_previous_calendar_year_without_comparison():
    resolver = TemporalResolver()
    assert _spans(resolver.resolve("Doanh thu tháng 7 năm rồi bao nhiêu?", date(2026, 11, 10))) == [
        ("target", "2025-07-01", "2025-08-01")
    ]
    assert _spans(resolver.resolve("Doanh thu tháng 3 năm ngoái bao nhiêu?", date(2026, 11, 10))) == [
        ("target", "2025-03-01", "2025-04-01")
    ]


def test_telex_leftovers_are_read_as_plain_words():
    from datetime import date as _date
    from bizreason.temporal import TemporalResolver as _Resolver, _untelex
    assert _untelex("thang 9 nawm 2025") == "thang 9 nam 2025"
    assert _untelex("thangs 6 quys 3 gaanf") == "thang 6 quy 3 gan"
    assert _untelex("tuan nay vs tuan truoc ss") == "tuan nay vs tuan truoc ss"
    (window,) = _Resolver().resolve("lời gộp tháng 9 nawm 2025", _date(2026, 6, 1))
    assert (window.start.isoformat(), window.end_exclusive.isoformat()) == ("2025-09-01", "2025-10-01")


def test_counted_days_from_an_event_use_the_single_date_named():
    from datetime import date as _date
    from bizreason.temporal import TemporalResolver as _Resolver
    target, comparison = _Resolver().resolve(
        "X lên giá từ ngày 28/07/2025; so 7 ngày kể từ ngày tăng giá với 7 ngày ngay trước", _date(2026, 6, 1))
    assert (target.start.isoformat(), target.end_exclusive.isoformat()) == ("2025-07-28", "2025-08-04")
    assert (comparison.start.isoformat(), comparison.end_exclusive.isoformat()) == ("2025-07-21", "2025-07-28")


def test_counted_days_from_an_event_without_a_date_do_not_invent_one():
    from datetime import date as _date
    from bizreason.temporal import TemporalResolver as _Resolver
    assert _Resolver().resolve("X tăng giá: so 7 ngày kể từ ngày tăng với 7 ngày ngay trước", _date(2026, 6, 1)) == []


def test_hoom_qua_is_yesterday():
    from datetime import date as _date
    from bizreason.temporal import TemporalResolver as _Resolver
    (window,) = _Resolver().resolve("hoom qua bán được bao nhiêu", _date(2026, 6, 1))
    assert window.start.isoformat() == "2026-05-31"


def test_short_month_prefix_in_a_span_of_months():
    from datetime import date as _date
    from bizreason.temporal import TemporalResolver as _Resolver
    starts = [w.start.isoformat() for w in _Resolver().resolve_periods("dthu 3 tháng liền từ T4/2025 tới T6/2025",
                                                                         _date(2026, 6, 1))]
    assert starts == ["2025-04-01", "2025-05-01", "2025-06-01"]


def test_q_prefix_is_a_quarter():
    from datetime import date as _date
    from bizreason.temporal import TemporalResolver as _Resolver
    (window,) = _Resolver().resolve("Q1/2026 tiệm có bao nhiêu đơn", _date(2026, 6, 15))
    assert (window.start.isoformat(), window.end_exclusive.isoformat()) == ("2026-01-01", "2026-04-01")


def test_dialect_month_words_and_days_ahead():
    from datetime import date as _date
    from bizreason.temporal import TemporalResolver as _Resolver
    r = _Resolver()
    as_of = _date(2026, 6, 15)
    span = lambda q: [(w.start.isoformat(), w.end_exclusive.isoformat()) for w in r.resolve(q, as_of)]
    assert span("Bữa ni tiệm bán được bao nhiêu") == [("2026-06-15", "2026-06-16")]
    assert span("Tháng ba quán có bao nhiêu đơn huỷ") == [("2026-03-01", "2026-04-01")]
    assert span("tháng năm 2025 doanh thu") == [("2025-05-01", "2025-06-01")]
    assert span("lô hết hạn trong 90 ngày tới") == [("2026-06-15", "2026-09-14")]
    assert ("2026-05-01", "2026-06-01") not in span("so với cùng tháng năm trước, doanh thu tháng 4/2026")
