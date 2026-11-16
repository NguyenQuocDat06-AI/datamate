"""A held source family/industry cannot affect either examples or retrieval IDF."""

import pytest

from bizreason.semantic.fewshot import FewShotBank
from bizreason.run_v2 import parse_args


def example(qid, business, text):
    return {"id": qid, "business_id": business, "question": text,
            "intent": {"measure": "order_count", "shape": "scalar"}}


def test_group_exclusion_refits_index_and_removes_all_related_shops():
    permitted = [example("a", "shop-a", "count tickets this month"),
                 example("b", "shop-b", "count sales last month")]
    withheld = [example("c", "shop-c", "count tickets"),
                example("d", "shop-c-variant", "count sales")]
    groups = {"shop-a": ["industry-a"], "shop-b": ["industry-b"],
              "shop-c": ["industry-c", "source-c"],
              "shop-c-variant": ["industry-c", "source-c"]}
    filtered = FewShotBank(permitted + withheld).excluding(groups=["source-c"], business_groups=groups)
    clean = FewShotBank(permitted)
    assert len(filtered) == 2
    for query in ("count sales", "tickets this month", "last month"):
        assert filtered._query_scores(query) == clean._query_scores(query)
        assert filtered.select(query, "query-shop") == clean.select(query, "query-shop")
    assert len(FewShotBank(permitted + withheld).excluding(
        groups=["industry-c"], business_groups=groups)) == 2


def test_held_question_wording_cannot_change_permitted_example_ranking():
    base = [example("a", "a", "revenue from sales"), example("b", "b", "count sales")]
    first = FewShotBank(base + [example("x", "held", "sales sales sales")]).excluding(businesses=["held"])
    second = FewShotBank(base + [example("x", "held", "unrelated wording")]).excluding(businesses=["held"])
    assert first.select("sales", "query") == second.select("sales", "query")


@pytest.mark.parametrize("metadata", [{}, {"shop-a": []}, {"shop-a": "industry"}])
def test_group_holdout_requires_complete_explicit_metadata(metadata):
    bank = FewShotBank([example("a", "shop-a", "count orders")])
    with pytest.raises(ValueError, match="metadata"):
        bank.excluding(groups=["industry"], business_groups=metadata)


def test_misspelled_exclusion_group_is_an_error():
    bank = FewShotBank([example("a", "shop-a", "count orders")])
    with pytest.raises(ValueError, match="absent"):
        bank.excluding(groups=["typo"], business_groups={"shop-a": ["industry"]})


def test_cli_accepts_explicit_fold_metadata():
    args = parse_args(["--data", "/tmp/inputs", "--out", "/tmp/outputs",
                       "--connection", "/tmp/not-read.json", "--fewshot-groups", "/tmp/groups.json",
                       "--fewshot-exclude-groups", "source-a", "industry-b",
                       "--fewshot-exclude-businesses", "shop-z"])
    assert args.fewshot_exclude_groups == ["source-a", "industry-b"]
    assert args.fewshot_exclude_businesses == ["shop-z"]
