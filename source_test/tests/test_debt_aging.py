import pytest

from bizreason.semantic.solver import age_buckets


@pytest.mark.parametrize("question", [
    "Đến hết ngày 04/12/2025, số tiền còn nợ chia theo tuổi nợ 0–7, 8–15 và trên 15 ngày là bao nhiêu",
    "tuoi no khach den het ngay 24/10/2025: nhom 0-7 ngay, 8-15 ngay, tren 15 ngay",
])
def test_age_buckets_read_in_order(question):
    assert age_buckets(question) == [(0, 7), (8, 15), (16, None)]


@pytest.mark.parametrize("question", [
    "khách nợ trên 15 ngày bao nhiêu",                 # no debt-age wording
    "tuổi nợ 0–7 và 10–15 ngày, trên 15 ngày",          # gap between buckets
    "tuổi nợ 0–7 ngày và 8–15 ngày",                    # no open last bucket
])
def test_age_buckets_refuse_unclear_splits(question):
    assert age_buckets(question) == []
