"""bizreason.semantic.fewshot: retrieval of similar solved questions, leakage rules, diversity and formatting."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bizreason.semantic.fewshot import FewShotBank, base_id, format_examples


def row(qid: str, business: str, question: str, measure: str = "revenue", shape: str = "scalar") -> dict[str, Any]:
    return {"id": qid, "business_id": business, "question": question,
            "intent": {"measure": measure, "shape": shape, "filters": [], "group_by": None}}


REVENUE_TODAY = "tổng doanh thu tháng này"


def ids(examples: list[dict[str, Any]]) -> list[str]:
    return [example["id"] for example in examples]


def test_base_id_strips_variant_and_operator_suffixes() -> None:
    assert base_id("q_1") == "q_1"
    assert base_id("q_1__strip") == "q_1"
    assert base_id("q_1__abbrev") == "q_1"
    assert base_id("q_1__reorder") == "q_1"
    assert base_id("q_1__abbrev__reorder") == "q_1"
    assert base_id("shop_b-q1") == "shop_b-q1"  # a single dash is part of the id, not a suffix


def test_same_business_is_never_returned() -> None:
    bank = FewShotBank([row("a1", "b1", REVENUE_TODAY), row("a2", "b2", REVENUE_TODAY)])
    assert ids(bank.select(REVENUE_TODAY, "b1", k=4)) == ["a2"]
    assert ids(bank.select(REVENUE_TODAY, "b3", k=4)) == ["a1", "a2"]  # another shop may use both


def test_same_base_id_including_variants_is_never_returned() -> None:
    bank = FewShotBank([
        row("q_9", "b2", REVENUE_TODAY),
        row("q_9__strip", "b2", REVENUE_TODAY),
        row("q_9__abbrev", "b2", REVENUE_TODAY),
        row("q_9__reorder", "b2", REVENUE_TODAY),
        row("q_9__other_op", "b2", REVENUE_TODAY),
        row("q_8", "b2", REVENUE_TODAY),
    ])
    # The query is q_9 (or any variant of it): every variant of q_9 is blocked, q_8 remains.
    assert ids(bank.select(REVENUE_TODAY, "b1", k=4, exclude_ids=("q_9",))) == ["q_8"]
    assert ids(bank.select(REVENUE_TODAY, "b1", k=4, exclude_ids=("q_9__abbrev",))) == ["q_8"]


def test_one_example_per_base_id() -> None:
    bank = FewShotBank([
        row("q_5", "b2", REVENUE_TODAY),
        row("q_5__strip", "b2", REVENUE_TODAY),
        row("q_5__reorder", "b2", REVENUE_TODAY),
    ])
    chosen = bank.select(REVENUE_TODAY, "b1", k=4)
    assert ids(chosen) == ["q_5"]  # the same question three times is one example


def test_deterministic_and_ties_break_by_id() -> None:
    rows = [row("z9", "b2", REVENUE_TODAY, shape="scalar"), row("a1", "b2", REVENUE_TODAY, shape="top_k")]
    first = FewShotBank(rows).select(REVENUE_TODAY, "b1", k=2)
    again = FewShotBank(list(reversed(rows))).select(REVENUE_TODAY, "b1", k=2)
    assert first == again
    assert ids(first) == ["a1", "z9"]  # identical scores, so the smaller id comes first


def test_repeated_calls_return_the_same_examples() -> None:
    bank = FewShotBank([row(f"x{i}", "b2", f"doanh thu tháng {i}") for i in range(10)])
    assert bank.select("doanh thu tháng 3", "b1", k=4) == bank.select("doanh thu tháng 3", "b1", k=4)


def test_diversity_skips_a_second_example_with_the_same_measure_and_shape() -> None:
    bank = FewShotBank([
        row("a1", "b2", REVENUE_TODAY, "revenue", "scalar"),
        row("a2", "b2", "tong doanh thu thang nay", "revenue", "scalar"),
        row("a3", "b2", "doanh thu thang nay theo ngay", "revenue", "breakdown"),
    ])
    assert ids(bank.select(REVENUE_TODAY, "b1", k=2)) == ["a1", "a3"]


def test_diversity_fills_from_repeats_when_k_cannot_be_filled_otherwise() -> None:
    bank = FewShotBank([row(f"a{i}", "b2", REVENUE_TODAY, "revenue", "scalar") for i in range(3)])
    assert ids(bank.select(REVENUE_TODAY, "b1", k=2)) == ["a0", "a1"]


def test_question_sharing_no_character_grams_gives_no_examples() -> None:
    bank = FewShotBank([row("a1", "b2", "xyzw")])
    assert bank.select("qq", "b1", k=4) == []


def test_select_refuses_missing_business_and_bad_k() -> None:
    bank = FewShotBank([row("a1", "b2", REVENUE_TODAY)])
    with pytest.raises(ValueError, match="business_id"):
        bank.select(REVENUE_TODAY, None)
    with pytest.raises(ValueError, match="k must be"):
        bank.select(REVENUE_TODAY, "b1", k=0)


def test_bank_loads_jsonl_and_rejects_bad_rows(tmp_path: Path) -> None:
    good = tmp_path / "bank.jsonl"
    good.write_text(json.dumps(row("a1", "b2", REVENUE_TODAY)) + "\n", encoding="utf-8")
    assert len(FewShotBank.load(good)) == 1

    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"id": "a1", "question": "no business"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="needs the fields"):
        FewShotBank.load(bad)

    broken = tmp_path / "broken.jsonl"
    broken.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        FewShotBank.load(broken)


def test_provider_excludes_the_question_own_id() -> None:
    bank = FewShotBank([row("q_1__strip", "b2", REVENUE_TODAY), row("q_2", "b2", REVENUE_TODAY)])
    provider = bank.provider(k=4)
    block = provider(REVENUE_TODAY, "q_1", "b1")
    assert "Q: " + REVENUE_TODAY in block
    assert block.count("Q: ") == 1  # only q_2; q_1__strip shares q_1's base id
    assert provider(REVENUE_TODAY, "q_1", "b2") == ""  # shop b2 is the query's own shop, so nothing is left
    with pytest.raises(ValueError, match="business_id"):
        provider(REVENUE_TODAY, "q_1", None)


def test_format_examples_is_compact_and_sorted() -> None:
    examples = [{
        "id": "a1", "business_id": "b2", "question": "Q one?",
        "intent": {"measure": "revenue", "shape": "scalar", "filters": [], "group_by": None, "threshold": None,
                   "compare_as": "percent", "measure2": None, "top_k": None},
    }]
    assert format_examples(examples) == (
        'Similar solved questions:\nQ: Q one?\nA: {"compare_as": "percent", "measure": "revenue", "shape": "scalar"}'
    )


def test_format_examples_keeps_filter_values_and_false_flags() -> None:
    examples = [{
        "id": "a1", "business_id": "b2", "question": "Q?",
        "intent": {"measure": "qty_base_sold", "shape": "scalar", "group_by": None,
                   "filters": [{"dim": "product", "value": "Tea", "exclude": False}]},
    }]
    assert format_examples(examples).endswith(
        'A: {"filters": [{"dim": "product", "exclude": false, "value": "Tea"}], '
        '"measure": "qty_base_sold", "shape": "scalar"}'
    )


def test_format_examples_of_nothing_is_empty() -> None:
    assert format_examples([]) == ""
