"""A stable answer cannot pass rulebook changes; abstains cannot fake invariance."""

import csv
from collections import defaultdict

import pytest

from scripts.generalization_probe import generate, load_lines, probe_ingestion, score, select_scenarios, verify_suite


@pytest.fixture
def suite(workspace_tmp):
    root = workspace_tmp / "suite"
    generate(root, shops=2, seed=79)
    return root


def predictions(cases):
    return [{key: row[key] for key in ("id", "type", "answer")} for row in cases]


def test_counterfactuals_keep_input_data_and_question_wording_identical(suite):
    manifest = verify_suite(suite)
    for layout in ("plain", "permuted", "continuation", "split_sheets", "prose"):
        base = suite / "inputs" / f"base__{layout}"
        base_data = {str(p.relative_to(base)): p.read_bytes() for p in (base / "data").rglob("*") if p.is_file()}
        base_texts = [row["question"] for row in load_lines(base / "questions.jsonl")]
        for policy in ("shipping", "tax", "delivery"):
            other = suite / "inputs" / f"{policy}__{layout}"
            other_data = {str(p.relative_to(other)): p.read_bytes() for p in (other / "data").rglob("*") if p.is_file()}
            assert other_data == base_data
            assert [row["question"] for row in load_lines(other / "questions.jsonl")] == base_texts
            assert (other / "rulebook.md").read_bytes() != (base / "rulebook.md").read_bytes()
    assert manifest["questions"] == 200
    assert not any(p.name in {"references.jsonl", "folds.json", "original_records.json"}
                   for p in (suite / "inputs").rglob("*"))


def test_layout_writer_preserves_amount_quantity_and_row_count(suite):
    manifest = verify_suite(suite)
    for scenario in manifest["scenarios"]:
        for shop, expected in manifest["profiles"].items():
            amount = qty = rows = 0
            bindings = manifest["bindings"][shop]
            for path in (suite / scenario["input"] / "data" / shop).glob("*.csv"):
                with path.open(encoding="utf-8") as handle:
                    data = list(csv.reader(handle))
                offset = next(i for i, row in enumerate(data) if set(row) == set(bindings))
                positions = {bindings[label]: i for i, label in enumerate(data[offset])}
                for row in data[offset+1:]:
                    rows += 1; qty += int(row[positions["qty"]]); amount += int(row[positions["amount"]])
            assert (rows, qty, amount) == (expected["rows"], expected["quantity"], expected["amount"])


def test_source_family_stays_in_one_fold_across_all_transformations(suite):
    assignments = defaultdict(set)
    for row in load_lines(suite / "references.jsonl"):
        assignments[row["business_id"]].add(row["fold"])
    assert all(len(folds) == 1 for folds in assignments.values())
    assert len(assignments) == 2


def test_reference_predictions_pass_and_frozen_rules_fail(suite):
    cases = load_lines(suite / "references.jsonl")
    exact = score(cases, predictions(cases))
    assert exact["counts"]["all"]["pa"] == 1
    assert exact["pairs"]["rule_change"]["total"] > 0
    assert exact["pairs"]["rule_change"]["both_correct"] == exact["pairs"]["rule_change"]["total"]
    base = {(row["business_id"], row["metric"], row["layout"]): row for row in cases if row["policy"] == "base"}
    frozen = []
    for case in cases:
        answer = base[case["business_id"], case["metric"], case["layout"]]
        frozen.append({"id": case["id"], "type": answer["type"], "answer": answer["answer"]})
    result = score(cases, frozen)
    assert result["counts"]["all"]["pa"] < 1
    assert result["pairs"]["rule_change"]["both_correct"] == 0
    assert result["pairs"]["layout_invariance"]["both_answered"] > 0


def test_two_abstains_or_missing_answers_do_not_pass_pairs(suite):
    cases = load_lines(suite / "references.jsonl")
    for pred in ([], [{"id": row["id"], "type": "abstain"} for row in cases]):
        result = score(cases, pred)
        assert result["counts"]["all"]["pa"] == 0
        assert result["counts"]["all"]["abstain"] == len(cases)
        assert all(pair["both_answered"] == pair["both_correct"] == 0 for pair in result["pairs"].values())


def test_bad_format_duplicates_and_unknown_ids_are_not_scored_as_correct(suite):
    cases = load_lines(suite / "references.jsonl")
    first = predictions(cases)[0]
    for value in (True, str(first["answer"]), float("nan")):
        result = score(cases, [{**first, "answer": value}])
        assert result["counts"]["all"]["wrong"] == 1
    with pytest.raises(ValueError, match="duplicate"):
        score(cases, [first, first])
    with pytest.raises(ValueError, match="unknown"):
        score(cases, [{"id": "not-in-suite", "type": "abstain"}])


def test_manifest_detects_changed_rulebook(suite):
    path = suite / "inputs" / "base__plain" / "rulebook.md"
    path.write_text("Changed", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        verify_suite(suite)


def test_short_pilot_scores_only_selected_questions_and_keeps_pairs(suite):
    cases = select_scenarios(load_lines(suite / "references.jsonl"), ["base__plain", "shipping__plain"])
    assert len(cases) == 20
    result = score(cases, predictions(cases))
    assert result["counts"]["all"]["total"] == 20
    assert result["counts"]["all"]["missing"] == 0
    assert result["pairs"]["rule_change"]["total"] > 0


def test_short_pilot_refuses_missing_anchors(suite):
    cases = load_lines(suite / "references.jsonl")
    with pytest.raises(ValueError, match="anchors"):
        select_scenarios(cases, ["shipping__prose"])
    with pytest.raises(ValueError, match="unknown"):
        select_scenarios(cases, ["misspelled-scenario"])


def test_generation_is_reproducible_and_actual_ingestion_preserves_profiles(workspace_tmp):
    a, b = workspace_tmp / "a", workspace_tmp / "b"
    first, second = generate(a, 1, 83), generate(b, 1, 83)
    assert first["sha256"] == second["sha256"]
    report = probe_ingestion(a, workspace_tmp / "probe")
    assert report["model_calls"] == 0
    assert report["mode"] == "ingestion_with_oracle_schema_bindings"
    assert report["total"] == report["passed"] == 20
