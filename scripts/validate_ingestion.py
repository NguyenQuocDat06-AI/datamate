"""Read every selected business and compare source hashes against the supplied ZIP."""

from __future__ import annotations

import argparse
import hashlib
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from bizreason.data.ingestion import BusinessBuilder
from bizreason.rules import RuleRegistry
from bizreason.utils import WORKSPACE, atomic_json, file_hash


def inspect_business(arguments: tuple[str, str, str]) -> dict:
    directory, cache, rulebook = map(Path, arguments)
    rules = RuleRegistry.load(rulebook)
    started = time.monotonic()
    context = BusinessBuilder(
        cache, rule_hash=rules.revision, rules=rules, inheritance_allowed=rules.inheritance_allowed
    ).prepare(directory)
    return {
        "business_id": context.business_id,
        "revision": context.revision,
        "tables": len(context.tables),
        "rows": sum(table.row_count for table in context.tables),
        "raw_rows": sum(table.raw_row_count for table in context.tables),
        "excluded_rows": sum(table.excluded_rows for table in context.tables),
        "duplicate_rows": sum(table.duplicate_rows for table in context.tables),
        "joins": len(context.joins),
        "elapsed_seconds": time.monotonic() - started,
        "row_accounting_valid": all(
            table.raw_row_count == table.row_count + table.excluded_rows + table.duplicate_rows
            for table in context.tables
        ),
        "diagnostics": [diagnostic.model_dump(mode="json") for diagnostic in context.diagnostics],
        "source_manifest": context.manifest,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data/train")
    parser.add_argument("--archive", default="train.zip")
    parser.add_argument("--business", action="append")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--output", default="artifacts/ingestion_validation.json")
    args = parser.parse_args()
    data = Path(args.data).resolve()
    businesses = sorted(
        directory
        for directory in (data / "data").iterdir()
        if directory.is_dir() and (not args.business or directory.name in args.business)
    )
    jobs = [
        (str(directory), str(WORKSPACE / "artifacts" / "cache"), str(data / "rulebook.md"))
        for directory in businesses
    ]
    results = []
    started = time.monotonic()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(inspect_business, job) for job in jobs]
        for future in as_completed(futures):
            record = future.result()
            print(
                f"{record['business_id']}: {record['tables']} tables, {record['rows']} rows, {len(record['diagnostics'])} diagnostics",
                flush=True,
            )
            results.append(record)
    mismatches = []
    checked = 0
    with zipfile.ZipFile(args.archive) as archive:
        for record in results:
            for relative, expected_hash in record["source_manifest"].items():
                source = data / "data" / record["business_id"] / relative
                archived_path = f"{data.name}/data/{record['business_id']}/{relative}"
                actual_hash = file_hash(source)
                with archive.open(archived_path) as handle:
                    original_hash = hashlib.file_digest(handle, "sha256").hexdigest()
                checked += 1
                if actual_hash != expected_hash or actual_hash != original_hash:
                    mismatches.append(str(source.relative_to(data)))
    report = {
        "businesses": sorted(results, key=lambda record: record["business_id"]),
        "business_count": len(results),
        "tables": sum(record["tables"] for record in results),
        "rows": sum(record["rows"] for record in results),
        "source_hashes_checked": checked,
        "source_hash_mismatches": mismatches,
        "elapsed_seconds": time.monotonic() - started,
        "all_row_accounting_valid": all(record["row_accounting_valid"] for record in results),
        "note": "Ingestion diagnostics only; this does not measure answer accuracy.",
    }
    atomic_json(Path(args.output), report)


if __name__ == "__main__":
    main()
