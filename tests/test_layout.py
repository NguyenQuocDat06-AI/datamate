import csv

from bizreason.data.ingestion import BusinessBuilder
from bizreason.data.layout import has_record_evidence, is_note, is_summary


def test_summary_before_note_never_inherits_the_last_order(workspace_tmp, rules):
    directory = workspace_tmp / "tail"
    directory.mkdir()
    with (directory / "sales.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(
            [
                ["order_id", "ordered_at", "status", "product_name", "qty_sale", "line_amount"],
                ["O1", "2026-03-01", "completed", "A", 1, 100],
                ["O2", "2026-03-02", "completed", "B", 2, 200],
                [None, None, None, "Cộng", None, "0,0003tr"],
                [None, None, None, "Ghi chú: tiền tính bằng đồng"],
            ]
        )
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)
    table = context.tables[0]
    assert table.row_count == 2
    assert table.excluded_rows == 2
    assert [(item.code, item.source.row) for item in context.diagnostics] == [
        ("EXCLUDED_SUMMARY_ROW", 4),
        ("EXCLUDED_NOTE_ROW", 5),
    ]
    assert table.raw_row_count == table.row_count + table.excluded_rows


def test_real_product_named_total_is_preserved():
    roles = ["order_id", "product_name", "qty_sale", "line_amount"]
    row = ["O1", "Cộng", 2, 200]
    assert has_record_evidence(row, roles)
    assert not is_summary(row, roles, after_last_record=True)
    assert not is_note(row, roles)
