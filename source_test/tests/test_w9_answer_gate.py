from bizreason.run_v2 import load_answer_gate, parse_args
from bizreason.semantic.answer_gate import MIN_SUPPORT, AnswerGate, build_table, coarse_key, path_key, wilson_lower

INTENT = {"measure": "m1", "shape": "scalar", "group_by": None, "column_measure": None,
          "filters": [{"dim": "d1", "value": "SECRET_VALUE", "exclude": False}]}
TRACE = {"windows": [["target", "2026-01-01", "2026-02-01"]], "reason": ""}


def test_key_stable_and_value_free():
    key = path_key(INTENT, TRACE, "number")
    assert key == path_key(INTENT, dict(TRACE), "number")
    assert "SECRET_VALUE" not in key and "2026" not in key
    assert "d1" in key and key.endswith("number")


def test_key_distinguishes_structure():
    other = dict(INTENT, group_by="x")
    assert path_key(other, TRACE, "number") != path_key(INTENT, TRACE, "number")
    assert coarse_key(INTENT, "number") == coarse_key(other, "number")


def test_wilson():
    assert wilson_lower(0, 0) == 0.0
    assert wilson_lower(10, 10) < 1.0
    assert wilson_lower(2, 20) < 0.5 < wilson_lower(18, 20)


def _gate(fine, coarse):
    t = {"k": {"n": sum(fine), "correct": fine[0], "wrong": fine[1]},
         "coarse:c": {"n": sum(coarse), "correct": coarse[0], "wrong": coarse[1]}}
    return AnswerGate(t)


def test_blocks_bad_group():
    assert _gate((2, 12), (50, 50)).decide("k", "c") is False


def test_good_group_and_few_wrong_pass():
    assert _gate((30, 2), (1, 1)).decide("k", "c") is True
    assert _gate((0, 1), (50, 1)).decide("k", "c") is True  # wrong < 2 and thin


def test_thin_group_falls_back_to_coarse():
    assert _gate((1, 3), (3, 30)).decide("k", "c") is False
    assert _gate((1, 3), (30, 3)).decide("k", "c") is True
    assert MIN_SUPPORT > 4


def test_unknown_keys_answer():
    assert AnswerGate({}).decide("a", "b") is True


def test_never_changes_abstain_or_unanswerable():
    key = path_key(INTENT, TRACE, "unanswerable")
    gate = AnswerGate({key: {"n": 20, "correct": 0, "wrong": 20}})
    assert gate.allows(INTENT, TRACE, "unanswerable") == (True, "")
    assert gate.allows(INTENT, TRACE, "abstain") == (True, "")


def test_allows_blocks_answered_type():
    key = path_key(INTENT, TRACE, "number")
    ok, k = AnswerGate({key: {"n": 20, "correct": 3, "wrong": 17}}).allows(INTENT, TRACE, "number")
    assert ok is False and k == key


def test_build_table_counts():
    t = build_table([("f", "c", True), ("f", "c", False)])
    assert t["f"] == {"n": 2, "correct": 1, "wrong": 1} and t["coarse:c"]["n"] == 2


def test_hook_off_by_default():
    args = parse_args(["--data", "d", "--out", "o", "--connection", "c"])
    assert args.answer_gate is None and load_answer_gate(args.answer_gate) is None
