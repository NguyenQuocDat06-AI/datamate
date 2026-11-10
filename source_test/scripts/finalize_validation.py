"""Combine the full pass with scoped tail fixes and an independent EDA comparison."""

from __future__ import annotations

import json
from pathlib import Path

from bizreason.contracts import BusinessContext
from bizreason.utils import WORKSPACE, atomic_json, file_hash


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    artifacts = WORKSPACE / "artifacts"
    base = read(artifacts / "ingestion_validation_release.json")
    audit = read(artifacts / "export_tail_audit.json")
    fixes = [
        read(artifacts / "ingestion_tail_fix.json"),
        read(artifacts / "ingestion_tail_fix_117.json"),
    ]
    updated = {record["business_id"]: record for report in fixes for record in report["businesses"]}
    if (
        set(updated) != set(audit["affected_businesses"])
        or audit["tables_checked"] != base["tables"]
    ):
        raise ValueError("Targeted re-ingestion does not cover every affected business/table")
    if any(report["source_hash_mismatches"] for report in [base, *fixes]):
        raise ValueError("Source hash mismatch")
    records = [updated.get(record["business_id"], record) for record in base["businesses"]]
    rechecked = 0
    for record in records:
        for relative, expected_hash in record["source_manifest"].items():
            path = WORKSPACE / "data/train/data" / record["business_id"] / relative
            if file_hash(path) != expected_hash:
                raise ValueError("A source file changed after the full-pass validation")
            rechecked += 1
    contexts = []
    for record in records:
        path = (
            artifacts / "cache" / record["business_id"] / record["revision"][:20] / "catalog.json"
        )
        contexts.append(BusinessContext.model_validate_json(path.read_text(encoding="utf-8")))
    # Baseline code was executed on all 339 tables. The only subsequent ingestion
    # change classifies tail summaries/notes, whose affected set was audited fully.
    expected_removed = len(audit["affected_rows"])
    removed = base["rows"] - sum(record["rows"] for record in records)
    if removed != expected_removed:
        raise ValueError("Unexpected row changes beyond the audited export tails")
    original_eda = read(WORKSPACE.parent / "analysis" / "train_profile.json")
    expected = {}
    for item in original_eda["scanned_files"]:
        business = item["business_id"]
        prefix = f"train/data/{business}/"
        for table in item["tables"]:
            expected[(business, table["file"].removeprefix(prefix), table["table"])] = table
    comparisons, mismatches = [], []
    for context in contexts:
        for table in context.tables:
            source = table.sources[0]
            key = (context.business_id, source.path, source.table)
            other = expected.get(key)
            if other is None:
                mismatches.append({"source": key, "issue": "missing_original_eda_table"})
                continue
            row_total = other["body_nonblank_rows"] + other["blank_rows_after_header"]
            header = other["header_row"] or 0
            result = {
                "source": key,
                "header_matches": table.header_row == header,
                "physical_body_rows_match": table.raw_row_count == row_total,
                "raw_rows": table.raw_row_count,
                "original_eda_body_rows": row_total,
            }
            comparisons.append(result)
            if not result["header_matches"] or not result["physical_body_rows_match"]:
                mismatches.append(result)
    diagnostics = [diagnostic for context in contexts for diagnostic in context.diagnostics]
    result = {
        "businesses": records,
        "business_count": len(records),
        "tables": sum(record["tables"] for record in records),
        "rows": sum(record["rows"] for record in records),
        "source_hashes_checked": base["source_hashes_checked"],
        "source_hashes_rechecked_after_tail_fix": rechecked,
        "source_hash_mismatches": [],
        "all_row_accounting_valid": all(record["row_accounting_valid"] for record in records),
        "parse_error_diagnostics": sum(item.code == "PARSE_ERROR" for item in diagnostics),
        "error_diagnostics": sum(item.severity == "error" for item in diagnostics),
        "excluded_summary_rows": sum(item.code == "EXCLUDED_SUMMARY_ROW" for item in diagnostics),
        "excluded_note_rows": sum(item.code == "EXCLUDED_NOTE_ROW" for item in diagnostics),
        "original_eda_comparisons": comparisons,
        "original_eda_mismatches": mismatches,
        "verification_stages": [
            "Full 30-business pass",
            "Raw-tail audit across every table",
            "Re-ingestion of all affected businesses",
            "Independent original EDA header/physical-row comparison",
        ],
        "cache_revisions": "Per-business revision is recorded. Unaffected businesses retain their fully audited baseline cache revision.",
        "note": "Ingestion verification is not an answer-accuracy or leaderboard benchmark.",
    }
    atomic_json(artifacts / "ingestion_validation_complete.json", result)
    print(
        {
            key: result[key]
            for key in [
                "business_count",
                "tables",
                "rows",
                "parse_error_diagnostics",
                "error_diagnostics",
                "excluded_summary_rows",
                "excluded_note_rows",
                "source_hashes_checked",
            ]
        }
    )
    print("Original EDA mismatches:", len(mismatches))


if __name__ == "__main__":
    main()
