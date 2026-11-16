import csv

from bizreason.data.ingestion import BusinessBuilder
from bizreason.data.layout import ArithmeticSubtotals, is_key_role

ROLES = ["order_id", "ordered_at", "product_name", "qty_sale", "line_amount"]


def _detector() -> ArithmeticSubtotals:
    return ArithmeticSubtotals(ROLES)


def test_key_roles_are_identifiers_and_dates_only():
    assert is_key_role("order_id") and is_key_role("product_id") and is_key_role("ordered_at")
    assert not is_key_role("product_name") and not is_key_role("line_amount") and not is_key_role(None)


def test_blank_keys_matching_the_block_sum_is_a_subtotal_whatever_the_label():
    for label in ["Lũy kế", "Cộng phát sinh", None]:
        detector = _detector()
        detector.add(["O1", "2026-03-01", "A", 1, 100])
        detector.add(["O2", "2026-03-02", "B", 2, 200])
        assert detector.is_subtotal([None, None, label, 3, 300]), label


def test_tolerance_is_half_a_percent():
    detector = _detector()
    detector.add(["O1", "2026-03-01", "A", 1, 1000])
    detector.add(["O2", "2026-03-02", "B", 1, 1000])
    assert detector.is_subtotal([None, None, "Lũy kế", None, 2004])  # 0.2% off
    assert not detector.is_subtotal([None, None, "Lũy kế", None, 2100])  # 5% off


def test_filled_key_is_never_a_subtotal_even_when_the_amount_matches():
    detector = _detector()
    detector.add(["O1", "2026-03-01", "A", 1, 100])
    detector.add(["O2", "2026-03-02", "B", 2, 200])
    assert not detector.is_subtotal(["O3", "2026-03-03", "C", 3, 300])
    assert not detector.is_subtotal([None, "2026-03-03", "C", 3, 300])  # a date is a key too


def test_a_blank_key_row_that_does_not_match_is_kept():
    detector = _detector()
    detector.add(["O1", "2026-03-01", "A", 1, 100])
    detector.add(["O2", "2026-03-02", "B", 2, 200])
    assert not detector.is_subtotal([None, None, "Hàng khác", 1, 250])


def test_a_single_row_block_cannot_match_and_zero_amounts_never_match():
    detector = _detector()
    detector.add(["O1", "2026-03-01", "A", 1, 100])
    assert not detector.is_subtotal([None, None, "Lũy kế", 1, 100])  # block of one row
    zeros = _detector()
    zeros.add(["O1", "2026-03-01", "A", 0, 0])
    zeros.add(["O2", "2026-03-02", "B", 0, 0])
    assert not zeros.is_subtotal([None, None, "Lũy kế", 0, 0])


def test_reset_starts_a_new_block_after_each_subtotal():
    detector = _detector()
    detector.add(["O1", "2026-03-01", "A", 1, 100])
    detector.add(["O2", "2026-03-02", "B", 1, 200])
    assert detector.is_subtotal([None, None, "Lũy kế", 2, 300])
    detector.reset()
    detector.add(["O3", "2026-03-03", "C", 1, 50])
    assert not detector.is_subtotal([None, None, "Lũy kế", 1, 300])  # the old block no longer counts
    detector.add(["O4", "2026-03-04", "D", 1, 70])
    assert detector.is_subtotal([None, None, "Cộng phát sinh", 2, 120])


def test_unparseable_block_amount_disables_that_column():
    detector = _detector()
    detector.add(["O1", "2026-03-01", "A", 1, "không rõ"])
    detector.add(["O2", "2026-03-02", "B", 1, 200])
    assert not detector.is_subtotal([None, None, "Lũy kế", 2, 200])


def test_subtotals_are_removed_from_a_sheet_without_any_keyword(workspace_tmp, rules):
    directory = workspace_tmp / "subtotals"
    directory.mkdir()
    with (directory / "sales.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(
            [
                ["order_id", "ordered_at", "status", "product_name", "qty_sale", "line_amount"],
                ["O1", "2026-03-01", "completed", "A", 1, 100],
                ["O2", "2026-03-02", "completed", "B", 2, 200],
                [None, None, None, "Lũy kế", 3, 300],  # 4: subtotal, label not in any keyword list
                ["O3", "2026-03-03", "completed", "C", 1, 50],
                [None, None, None, None, None, 40],  # 6: blank-key row, block of one -> kept
                [None, None, None, "Cộng phát sinh", 1, 90],  # 7: subtotal of rows 5-6
                ["O4", "2026-03-04", "completed", "D", 1, 70],  # 8
                ["O5", "2026-03-04", "completed", "E", 1, 30],  # 9
                ["O6", "2026-03-05", "completed", "F", 1, 100],  # 10: keyed, kept though it equals the block
                [None, None, None, None, 1, 200],  # 11: subtotal with an empty label
            ]
        )
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    table = context.tables[0]
    subtotals = [
        (item.code, item.source.row)
        for item in context.diagnostics
        if item.code == "EXCLUDED_SUBTOTAL_ROW"
    ]
    assert subtotals == [
        ("EXCLUDED_SUBTOTAL_ROW", 4),
        ("EXCLUDED_SUBTOTAL_ROW", 7),
        ("EXCLUDED_SUBTOTAL_ROW", 11),
    ]
    assert table.excluded_rows == 3
    assert table.row_count == 7
    assert table.raw_row_count == table.row_count + table.excluded_rows
