"""Subtotal detection must survive column movement without deleting real records."""

import csv
from datetime import date
from itertools import permutations

from bizreason.data.ingestion import BusinessBuilder
from bizreason.data.layout import BlockSums


def test_subtotal_label_and_columns_can_move_without_semantic_roles():
    records = [["R1", "aa", 2, 120], ["R2", "bb", 3, 180]]
    for order in permutations(range(4)):
        permute = lambda row: [row[index] for index in order]
        for label_column in range(4):
            # A label may replace any source cell, including an amount or identifier.
            subtotal = [None, None, 5, 300]
            subtotal[label_column] = "Accumulated"
            detector = BlockSums(4)
            for row in records:
                detector.add(permute(row))
            assert detector.is_subtotal(permute(subtotal)), (order, label_column)


def test_role_free_totals_require_all_present_numeric_columns_to_agree():
    detector = BlockSums(4)
    detector.add(["R1", "aa", 2, 120])
    detector.add(["R2", "bb", 3, 180])
    assert not detector.is_subtotal([None, "Accumulated", 9, 300])
    assert not detector.is_subtotal([None, "Accumulated", 5, 500])


def test_continuation_with_product_and_unit_is_never_a_total():
    detector = BlockSums(5)
    detector.add(["R1", "aa", "piece", 2, 120])
    detector.add(["R2", "bb", "piece", 3, 180])
    continuation = [None, "cc", "piece", 5, 300]
    assert not detector.looks_like_total(continuation)
    assert not detector.is_subtotal(continuation)


def test_numeric_record_key_is_not_reinterpreted_as_a_sum():
    detector = BlockSums(3, roles=["order_id", "product_name", "line_amount"])
    detector.add([1, "aa", 120])
    detector.add([2, "bb", 180])
    assert not detector.is_subtotal([3, "cc", 300])
    assert not detector.is_subtotal(["3", "cc", 300])


def test_dates_are_record_evidence_with_or_without_roles():
    for day in [date(2024, 8, 3), "2024-08-03", "03/08/2024"]:
        detector = BlockSums(3)
        detector.add(["R1", "aa", 120])
        detector.add(["R2", "bb", 180])
        assert not detector.is_subtotal([day, None, 300])


def test_role_based_shape_gate_accepts_sparse_blocks_but_not_multi_text_records():
    detector = BlockSums(4)
    detector.add(["R1", "aa", 1, 50])
    detector.add([None, None, None, 40])
    assert detector.looks_like_total([None, "Accumulated", 1, 90])
    # No column has dense nonnumeric identity evidence: the role-free path abstains.
    assert not detector.is_subtotal([None, "Accumulated", 1, 90])
    assert not detector.looks_like_total([None, "aa", "piece", 90])


def test_running_total_is_compared_with_all_previous_data_blocks():
    detector = BlockSums(3)
    detector.add(["R1", "aa", 100])
    detector.add(["R2", "bb", 200])
    assert detector.is_subtotal([None, None, 300])
    detector.reset()
    detector.add(["R3", "cc", 50])
    detector.add(["R4", "dd", 70])
    assert detector.is_subtotal([None, "Accumulated", 420])
    assert detector.is_subtotal([None, None, 120])
    assert not detector.is_subtotal([None, None, 4210])


def test_filling_the_only_text_identity_is_not_evidence_of_a_total():
    detector = BlockSums(2)
    detector.add(["aa", 100])
    detector.add(["bb", 200])
    assert not detector.is_subtotal(["cc", 300])
    assert detector.is_subtotal([None, 300])


def test_alias_off_ingestion_keeps_an_ambiguous_name_and_amount_record(workspace_tmp, rules, monkeypatch):
    from bizreason.data import schema

    monkeypatch.setattr(schema, "ROLE_LOOKUP", {})
    directory = workspace_tmp / "unknown_names"
    directory.mkdir()
    with (directory / "sales.csv").open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows([
            ["Unfamiliar label", "Unfamiliar value"],
            ["aa", 100], ["bb", 200], ["cc", 300],
        ])
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    assert context.tables[0].row_count == 3
    assert not any(d.code == "EXCLUDED_SUBTOTAL_ROW" for d in context.diagnostics)


def test_ambiguous_numeric_separator_does_not_create_arithmetic_evidence():
    detector = BlockSums(2)
    detector.add(["aa", "1,000"])
    detector.add(["bb", "2,000"])
    assert not detector.is_subtotal([None, "3,000"])
    for locale in ["vi", "en"]:
        detector = BlockSums(2, ["unknown", locale])
        detector.add(["aa", "1,000"])
        detector.add(["bb", "2,000"])
        assert detector.is_subtotal([None, "3,000"])


def test_a_single_record_and_zero_totals_are_not_enough_evidence():
    detector = BlockSums(2)
    detector.add(["aa", 100])
    assert not detector.is_subtotal([None, 100])
    detector.add(["bb", -100])
    assert not detector.is_subtotal([None, 0])


def test_ingestion_keeps_a_numeric_order_key_matching_the_block_sum(workspace_tmp, rules):
    directory = workspace_tmp / "numeric_keys"
    directory.mkdir()
    with (directory / "sales.csv").open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows([
            ["order_id", "product_name", "line_amount"],
            [1, "aa", 120],
            [2, "bb", 180],
            [3, "cc", 300],
        ])
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    assert context.tables[0].row_count == 3
    assert not any(d.code == "EXCLUDED_SUBTOTAL_ROW" for d in context.diagnostics)


def test_ingestion_keeps_product_continuation_even_when_its_amount_matches(workspace_tmp, rules):
    directory = workspace_tmp / "continuation"
    directory.mkdir()
    (directory / "README.md").write_text(
        "Ô trống cùng một nhóm lấy dòng đầu nhóm.", encoding="utf-8"
    )
    with (directory / "sales.csv").open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows([
            ["order_id", "product_name", "unit", "qty_sale", "line_amount"],
            ["R1", "aa", "piece", 1, 100],
            ["R2", "bb", "piece", 2, 200],
            [None, "cc", "piece", 3, 300],
        ])
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    assert context.tables[0].row_count == 3
    assert not any(d.code == "EXCLUDED_SUBTOTAL_ROW" for d in context.diagnostics)
