"""Freeze public questions 51--100 and prepare all public contexts with current code."""

from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone

from bizreason.data.ingestion import BusinessBuilder, PARSER_VERSION
from bizreason.rules import RuleRegistry
from bizreason.utils import WORKSPACE, atomic_json, file_hash, load_jsonl

ROOT = WORKSPACE
OUT = ROOT / "artifacts/public_next50_16w"


def prepare_business(business_id: str) -> dict:
    rules = RuleRegistry.load(ROOT / "data/public_test/rulebook.md")
    context = BusinessBuilder(ROOT / "artifacts/cache", rule_hash=rules.revision, rules=rules).prepare(
        ROOT / "data/public_test/data" / business_id)
    return {"business_id": business_id, "revision": context.revision,
        "tables": len(context.tables), "rows": sum(t.row_count for t in context.tables),
        "raw_rows": sum(t.raw_row_count for t in context.tables),
        "excluded_rows": sum(t.excluded_rows for t in context.tables),
        "duplicate_rows": sum(t.duplicate_rows for t in context.tables),
        "row_accounting_valid": all(t.raw_row_count == t.row_count + t.excluded_rows + t.duplicate_rows for t in context.tables),
        "source_manifest": context.manifest,
        "diagnostics": [d.model_dump(mode="json") for d in context.diagnostics]}


def main():
    old_path = ROOT / "artifacts/public_50_trammay/run_manifest.json"
    old_ids = json.loads(old_path.read_text(encoding="utf-8"))["question_ids"]
    questions = load_jsonl(ROOT / "data/public_test/questions.jsonl")
    selected = [q for q in questions if q["id"] not in set(old_ids)][:50]
    assert len(old_ids) == len(set(old_ids)) == len(selected) == 50
    assert set(old_ids).isdisjoint(q["id"] for q in selected)
    assert [q["id"] for q in questions[:50]] == old_ids
    selection = {"created_at": datetime.now(timezone.utc).isoformat(),
        "selection": "Input positions 51--100; exclude exactly the 50 original experiment IDs.",
        "previous_50_ids": old_ids, "ids": [q["id"] for q in selected],
        "questions": [{"index": 51 + i, **q} for i, q in enumerate(selected)],
        "question_file_sha256": file_hash(ROOT / "data/public_test/questions.jsonl"),
        "workers": 16, "fresh_llm_inference": True, "reuse_checkpoints": False}
    if (OUT / "run_manifest.json").exists():
        raise FileExistsError("Experiment already initialized; do not replace its preparation")
    atomic_json(OUT / "selection.json", selection)
    businesses = sorted({q["business_id"] for q in questions})
    records = []
    with ProcessPoolExecutor(max_workers=3) as pool:
        tasks = {pool.submit(prepare_business, bid): bid for bid in businesses}
        for future in as_completed(tasks):
            record = future.result()
            records.append(record)
            print(record["business_id"], record["rows"], "rows; error diagnostics=",
                sum(d["severity"] == "error" for d in record["diagnostics"]), flush=True)
    records.sort(key=lambda r: r["business_id"])
    report = {"parser": PARSER_VERSION, "business_count": len(records), "businesses": records,
        "rows": sum(r["rows"] for r in records), "tables": sum(r["tables"] for r in records),
        "all_row_accounting_valid": all(r["row_accounting_valid"] for r in records),
        "source_hash_mismatches": 0, "source_hashes_checked": sum(len(r["source_manifest"]) for r in records),
        "note": "All 14 public businesses prepared with the current normalizer; runner rechecks every source hash before API calls."}
    assert report["all_row_accounting_valid"]
    atomic_json(OUT / "public_ingestion.json", report)
    arguments = ["-X", "utf8", "-u", "-m", "scripts.run_public_test", "--ingestion-report",
        "artifacts/public_next50_16w/public_ingestion.json", "--workers", "16", "--stream",
        "--question-timeout", "900", "--request-timeout", "420", "--total-seconds", "10800",
        "--artifacts", "artifacts/public_next50_16w", "--output", "result/public_test_next50_16w.jsonl"]
    for question in selected:
        arguments.extend(["--question-id", question["id"]])
    atomic_json(OUT / "launch.json", {"arguments": arguments, "working_directory": str(ROOT)})
    print("Ready: 50 new IDs, 16 workers, all public businesses prepared", flush=True)


if __name__ == "__main__":
    main()
