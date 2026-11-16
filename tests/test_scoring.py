import pytest

from bizreason.evaluation.scorer import matches, score_records
from bizreason.evaluation.splits import grouped_split


@pytest.mark.parametrize(
    "prediction,reference,expected",
    [
        ({"type": "number", "answer": 10}, {"type": "number", "answer": 10}, True),
        ({"type": "number", "answer": "10"}, {"type": "number", "answer": 10}, False),
        ({"type": "number", "answer": True}, {"type": "number", "answer": 1}, False),
        ({"type": "percent", "answer": 12.345}, {"type": "percent", "answer": 12.35}, True),
        (
            {"type": "string", "answer": " CÀ-PHÊ  sữa "},
            {"type": "string", "answer": "Cà phê sữa"},
            True,
        ),
        (
            {"type": "string", "answer": "Ca phe sua"},
            {"type": "string", "answer": "Cà phê sữa"},
            False,
        ),
        (
            {"type": "string", "answer": "SP01"},
            {"type": "string", "answer": "Cà phê", "alias": ["SP01"]},
            True,
        ),
        ({"type": "list", "answer": ["B", "A"]}, {"type": "list", "answer": ["A", "B"]}, False),
        (
            {"type": "list", "answer": ["SP1", "B"]},
            {"type": "list", "answer": ["A", "B"], "alias": [["SP1"], []]},
            True,
        ),
        ({"type": "boolean", "answer": 1}, {"type": "boolean", "answer": True}, False),
    ],
)
def test_exact_typed_matching(prediction, reference, expected):
    assert matches(prediction, reference) is expected


def test_table_column_alignment_row_sets_and_aliases():
    reference = {
        "type": "table",
        "answer": {"columns": ["khach_hang", "so_tien_no"], "rows": [["An", 10], ["Bình", 20]]},
        "alias": [["K1"], ["K2"]],
    }
    prediction = {
        "type": "table",
        "answer": {"columns": ["so_tien_no", "khach_hang"], "rows": [[20, "K2"], [10, "K1"]]},
    }
    assert matches(prediction, reference)
    prediction["answer"]["rows"].append([10, "K1"])
    assert matches(prediction, reference)


def test_pa_and_missing_predictions():
    references = [{"id": str(i), "type": "number", "answer": 1} for i in range(100)]
    predictions = [
        {"id": str(i), "type": "number", "answer": 1 if i < 72 else 2} for i in range(80)
    ]
    report = score_records(predictions, references)
    assert (report["correct"], report["wrong"], report["abstain"]) == (72, 8, 20)
    assert report["pa"] == 0.64


def test_business_split_is_disjoint_and_reproducible():
    questions = [{"id": f"{i}-{j}", "business_id": f"b{i}"} for i in range(30) for j in range(2)]
    first = grouped_split(questions)
    assert grouped_split(questions) == first
    groups = [set(group["businesses"]) for group in first["splits"].values()]
    assert [len(group) for group in groups] == [18, 6, 6]
    assert all(not left & right for i, left in enumerate(groups) for right in groups[i + 1 :])
