"""Protect source records when arithmetic cannot distinguish a line from a total."""

import csv
import json
import sqlite3

import pytest

from bizreason.data import schema
from bizreason.data.ingestion import BusinessBuilder


@pytest.mark.parametrize("aliases", [True, False])
@pytest.mark.parametrize("label", ["Replacement filter", "Inspection work", "", "Cộng"])
@pytest.mark.parametrize("order", [(0, 1, 2, 3, 4), (4, 2, 0, 3, 1)])
def test_single_amount_continuation_and_explicit_total_have_distinct_decisions(
    workspace_tmp, rules, monkeypatch, aliases, label, order,
):
    if not aliases:
        monkeypatch.setattr(schema, "ROLE_LOOKUP", {})
    shop = workspace_tmp / "service_ledger"
    shop.mkdir()
    headers = ["Reference", "Recorded on", "Asset", "Description", "Value"]
    rows = [["T-a", "2024-04-01", "Asset-a", "Service a", 70],
            ["T-b", "2024-04-02", "Asset-b", "Service b", 130],
            ["", "", "", label, 200]]
    rows += [[f"T-{i}", "2024-04-03", f"Asset-{i}", "Service c", 11+i] for i in range(12)]
    with (shop / "ledger.csv").open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows([[row[i] for i in order] for row in [headers, *rows]])
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(shop)
    expected_removed = int(label == "Cộng")
    assert len(context.tables) == 1
    table = context.tables[0]
    assert table.row_count == len(rows) - expected_removed
    with sqlite3.connect(context.database) as db:
        records = db.execute(f'SELECT __raw_json FROM "{table.name}"').fetchall()
        amount_index = order.index(4)
        assert sum(int(json.loads(raw)[amount_index]) for (raw,) in records) == (
            sum(row[4] for row in rows) - 200 * expected_removed
        )
        audit = db.execute(
            "SELECT row_class, kept, raw_json, evidence_json FROM __row_audit WHERE source_row=4"
        ).fetchall()
        assert len(audit) == 1
        row_class, kept, raw, evidence = audit[0]
        assert kept == 1 - expected_removed
        assert json.loads(raw) == [str(rows[2][i]) for i in order]
        if not expected_removed:
            assert row_class == "ambiguous_total"
            assert json.loads(evidence)["comparisons"][0]["observed"] == "200"
        removed = db.execute("SELECT COUNT(*) FROM __row_audit WHERE kept=0").fetchone()[0]
        assert table.raw_row_count == table.row_count + removed


def test_excluded_source_values_remain_available_in_audit(workspace_tmp, rules):
    shop = workspace_tmp / "audit_source"
    shop.mkdir()
    with (shop / "ledger.csv").open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows([
            ["order_id", "ordered_at", "line_amount"],
            ["A", "2024-04-01", 17], ["B", "2024-04-02", 23],
            [None, None, None], ["Note: source export", None, None],
            ["Cộng", None, 40],
        ])
    context = BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(shop)
    table = context.tables[0]
    with sqlite3.connect(context.database) as db:
        excluded = db.execute(
            "SELECT source_row, row_class, raw_json FROM __row_audit WHERE kept=0 ORDER BY source_row"
        ).fetchall()
    assert {row[1] for row in excluded} == {"blank", "note", "summary"}
    assert table.raw_row_count == table.row_count + len(excluded)
    assert json.loads(excluded[-1][2])[0] == "Cộng"
