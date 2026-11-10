"""Check the new tail classifier against raw retained rows in every prepared table."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bizreason.contracts import BusinessContext
from bizreason.data.layout import has_record_evidence, is_note, is_summary
from bizreason.data.readers import sqlite_readonly
from bizreason.utils import WORKSPACE, atomic_json, quote_identifier


def audit(report_path: Path) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    affected = []
    checked = 0
    for business in report["businesses"]:
        catalog = (
            WORKSPACE
            / "artifacts"
            / "cache"
            / business["business_id"]
            / business["revision"][:20]
            / "catalog.json"
        )
        context = BusinessContext.model_validate_json(catalog.read_text(encoding="utf-8"))
        connection = sqlite_readonly(context.database)
        try:
            for table in context.tables:
                checked += 1
                roles = [column.semantic_role for column in table.columns]
                tail = []
                has_record = False
                for source_row, raw in connection.execute(
                    f"SELECT __source_row,__raw_json FROM {quote_identifier(table.name)} ORDER BY __source_row DESC"
                ):
                    row = json.loads(raw)
                    row += [None] * max(0, len(roles) - len(row))
                    if has_record_evidence(row, roles):
                        has_record = True
                        break
                    tail.append((source_row, row))
                if has_record:
                    for source_row, row in tail:
                        kind = (
                            "summary"
                            if is_summary(row, roles, after_last_record=True)
                            else "note" if is_note(row, roles) else None
                        )
                        if kind:
                            affected.append(
                                {
                                    "business_id": business["business_id"],
                                    "table": table.name,
                                    "source_row": source_row,
                                    "kind": kind,
                                    "source": table.sources[0].model_dump(mode="json"),
                                }
                            )
        finally:
            connection.close()
    return {
        "base_report": str(report_path),
        "tables_checked": checked,
        "affected_rows": affected,
        "affected_businesses": sorted({row["business_id"] for row in affected}),
        "note": "Reads original raw cells in the cache. Old deduplicated transactions retain record evidence; already excluded rows remain excluded.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default="artifacts/ingestion_validation_release.json")
    parser.add_argument("--output", default="artifacts/export_tail_audit.json")
    args = parser.parse_args()
    result = audit(Path(args.report))
    atomic_json(Path(args.output), result)
    print(
        {
            "tables_checked": result["tables_checked"],
            "affected_businesses": result["affected_businesses"],
            "affected_rows": len(result["affected_rows"]),
        }
    )


if __name__ == "__main__":
    main()
