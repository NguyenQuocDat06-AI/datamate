import json
import os
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_perturb_questions as g  # noqa: E402


def test_strip_keeps_codes_and_digits_and_removes_diacritics():
    assert g.make_strip("Tháng 6/2025 doanh thu bao nhiêu?") == "Thang 6/2025 doanh thu bao nhieu?"
    assert g.make_strip("th3/2025 (M) T6/2025 1.000.000đ") == "th3/2025 (M) T6/2025 1.000.000d"
    assert g.make_strip("Đơn đặt") == "Don dat"


def test_abbrev_basic_rules():
    out = g.make_abbrev("Hôm nay doanh thu bao nhiêu?")
    assert "hnay" in out and "dthu" in out and "bnhiu" in out


def test_abbrev_accepts_unaccented_input():
    assert g.make_abbrev("hom nay doanh thu bao nhieu?") == "hnay dthu bnhiu?"


def test_abbrev_tháng_only_before_digit():
    assert g.make_abbrev("Tháng 3/2025 đơn") == "th 3/2025 don"
    assert g.make_abbrev("tháng này đơn") == "tháng này don"


def test_abbrev_does_not_touch_other_accented_words():
    out = g.make_abbrev("Tiến độ đơn hàng")
    assert "Tiến" in out and "don" in out


def test_abbrev_fallback_appends_filler_before_question_mark():
    assert g.make_abbrev("Mì Omachi là gì?") == "Mì Omachi là gì vậy?"
    assert g.make_abbrev("Mì Omachi là gì") == "Mì Omachi là gì vậy"


def test_reorder_start_to_end_keeps_case_of_moved_text():
    assert (
        g.make_reorder("Tháng 6/2025 doanh thu quán bao nhiêu?")[0]
        == "doanh thu quán bao nhiêu tháng 6/2025?"
    )


def test_reorder_end_to_start():
    assert (
        g.make_reorder("doanh thu quán bao nhiêu tháng 6/2025?")[0]
        == "tháng 6/2025 doanh thu quán bao nhiêu?"
    )


def test_reorder_range_and_code_case():
    assert (
        g.make_reorder("Từ tháng 1/2025 đến tháng 6/2025, doanh thu bao nhiêu?")[0]
        == "doanh thu bao nhiêu từ tháng 1/2025 đến tháng 6/2025?"
    )
    assert g.make_reorder("T6/2025 doanh thu bao nhiêu?")[0] == "doanh thu bao nhiêu T6/2025?"


def test_reorder_skipped_when_time_in_middle():
    assert g.make_reorder("Doanh thu tháng 6/2025 quán bao nhiêu?") == (None, "middle")


def test_reorder_skipped_when_no_time():
    assert g.make_reorder("Mì Omachi bán được bao nhiêu?") == (None, "no_time")


def test_full_date_not_split_into_month_year():
    # "19/08/2026" in the middle must not be read as the time "08/2026"
    assert g.make_reorder("Tính đến hết ngày 19/08/2026, có bao nhiêu khách nợ?") == (None, "middle")


def test_sampling_is_deterministic_and_stratified():
    records = []
    for biz, n in [("biz_a", 100), ("biz_b", 100), ("biz_c", 10)]:
        for i in range(n):
            records.append({"id": f"{biz}_{i}", "business_id": biz, "question": "q", "as_of_date": "2026-01-01"})
    first = g.sample_indices(records, 60, 0)
    assert first == g.sample_indices(records, 60, 0)
    counts = Counter(records[i]["business_id"] for i in first)
    assert counts == {"biz_a": 25, "biz_b": 25, "biz_c": 10}
    assert first != g.sample_indices(records, 60, 1)


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_cli_ids_and_mapping_consistent(tmp_path):
    rows = [
        {"id": "q1", "business_id": "biz_1", "question": "Tháng 6/2025 doanh thu quán bao nhiêu?", "as_of_date": "2026-01-01"},
        {"id": "q2", "business_id": "biz_1", "question": "Doanh thu tháng 6/2025 quán bao nhiêu?", "as_of_date": "2026-01-02"},
        {"id": "q3", "business_id": "biz_2", "question": "Mì Omachi là gì?", "as_of_date": "2026-01-03"},
    ]
    src = tmp_path / "in.jsonl"
    _write_jsonl(src, rows)
    out = tmp_path / "out"
    assert g.main(["--questions", str(src), "--out", str(out), "--sample", "3", "--seed", "0"]) == 0

    lines = [json.loads(l) for l in (out / "questions.jsonl").read_text(encoding="utf-8").splitlines()]
    mapping = json.loads((out / "variants.json").read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in lines}
    originals = {r["id"] for r in rows}

    assert originals <= set(by_id)
    assert set(mapping) == {i for i in by_id if i not in originals}
    for vid, orig_id in mapping.items():
        kind = vid.split("__", 1)[1]
        assert kind in g.KINDS
        assert vid == f"{orig_id}__{kind}"
        v, o = by_id[vid], by_id[orig_id]
        assert v["business_id"] == o["business_id"]
        assert v["as_of_date"] == o["as_of_date"]
        assert v["question"] != o["question"]
    # q2 has its time in the middle, so no reorder variant
    assert "q2__reorder" not in mapping
    assert "q1__reorder" in mapping
    assert set(mapping.values()) <= originals
