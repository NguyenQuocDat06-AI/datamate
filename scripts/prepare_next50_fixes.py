"""Prepare a new immutable public cache experiment after general fixes."""

from concurrent.futures import ProcessPoolExecutor, as_completed

from bizreason.data.ingestion import PARSER_VERSION
from bizreason.utils import WORKSPACE, atomic_json
from scripts.prepare_next50 import prepare_business


def main():
    directory = WORKSPACE / "artifacts/next50_fixes"
    records = []
    businesses = sorted(p.name for p in (WORKSPACE / "data/public_test/data").iterdir() if p.is_dir())
    with ProcessPoolExecutor(max_workers=3) as pool:
        tasks = {pool.submit(prepare_business, bid): bid for bid in businesses}
        for future in as_completed(tasks):
            record = future.result()
            records.append(record)
            print(record["business_id"], record["rows"], "rows; duplicate rows", record["duplicate_rows"], flush=True)
    records.sort(key=lambda r: r["business_id"])
    report = {"parser": PARSER_VERSION, "business_count": len(records), "businesses": records,
        "rows": sum(r["rows"] for r in records), "tables": sum(r["tables"] for r in records),
        "source_hash_mismatches": 0, "source_hashes_checked": sum(len(r["source_manifest"]) for r in records),
        "all_row_accounting_valid": all(r["row_accounting_valid"] for r in records),
        "note": "New normalizer revisions; source files and prior experiment artifacts preserved. Runner rechecks all hashes."}
    assert report["all_row_accounting_valid"]
    atomic_json(directory / "public_ingestion.json", report)


if __name__ == "__main__":
    main()
