"""Causal checks after raw references are frozen; no solver outputs are modified."""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from bizreason.contracts import ExecutionStatus
from bizreason.execution import execute_sql
from bizreason.utils import digest, normalize_lookup
from scripts.audit_next50_ledgers import workbook_sales
from scripts.next50_common import DATA, clean, csv_rows, day, deduplicate, excel, invalid_status, number, rounded
from scripts.review_public_first10 import invalid_status as legacy_invalid_status

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/public_next50_16w_review"
RUN = ROOT / "artifacts/public_next50_16w"


def trace(identity):
    return json.loads((RUN / "traces" / (digest(identity)[:24] + ".json")).read_text(encoding="utf-8"))


def duplicate_invoice_check():
    path = DATA / "dev_009/danh_sach_hoa_don.csv"
    rows = [(s, n, r) for s, n, r in csv_rows(path) if clean(r.get("Mã hóa đơn"))
        and not invalid_status(r["Trạng thái đơn"]) and day(r["Ngày tháng"]).isoformat() == "2026-04-19"]
    unique = list(deduplicate(rows))
    all_total = sum((number(r["Giá trị hàng"]) for _, _, r in rows), Decimal(0))
    unique_total = sum((number(r["Giá trị hàng"]) for _, _, r in unique), Decimal(0))
    kept_lines = {n for _, n, _ in unique}
    duplicates = [{"line": n, "order": r["Mã hóa đơn"], "amount": str(number(r["Giá trị hàng"]))}
                  for _, n, r in rows if n not in kept_lines]
    return {"raw_sum_without_deduplication": int(all_total), "sum_after_exact_order_row_deduplication": int(unique_total),
            "duplicate_rows": duplicates, "duplicate_amount": int(all_total - unique_total)}


def nonempty_notes_check():
    rows = workbook_sales("dev_014", "SoBanHang.xlsx", skip=("Ghi tắt",))
    invalid_orders = {clean(r["STT"]) for _, _, r in rows if invalid_status(r.get("Ghi chú"))}
    any_note = {clean(r["STT"]) for _, _, r in rows if clean(r.get("Ghi chú"))}
    correct, wrong, dropped = defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal)
    dropped_notes = defaultdict(int)
    for _, _, r in rows:
        month = r["Ngày"][:7]
        if month not in {"2025-10", "2025-11"} or day(r["Ngày"]).day > 8:
            continue
        oid = clean(r["STT"])
        if oid not in invalid_orders:
            correct[month] += number(r["Thành tiền"])
            if oid in any_note:
                dropped[month] += number(r["Thành tiền"])
                dropped_notes[clean(r["Ghi chú"])] += 1
        if oid not in any_note:
            wrong[month] += number(r["Thành tiền"])
    dates = {clean(r["STT"]): r["Ngày"] for _, _, r in rows}
    legacy_bad = {clean(r["STT"]) for _, _, r in rows if legacy_invalid_status(r.get("Ghi chú"))}
    historical_before, historical_after = Decimal(0), Decimal(0)
    for _, _, record in excel(DATA / "dev_014/ThuMayCu.xlsx", "Sheet1"):
        oid = clean(record["Trừ vào (STT sổ bán)"])
        if "2025-09-01" <= dates[oid] < "2025-10-01":
            if oid not in legacy_bad:
                historical_before += number(record["Giá thu"])
            if oid not in invalid_orders:
                historical_after += number(record["Giá thu"])
    return {"correct_revenue": {k: str(v) for k, v in correct.items()},
        "revenue_after_excluding_every_nonempty_note": {k: str(v) for k, v in wrong.items()},
        "valid_revenue_wrongly_dropped": {k: str(v) for k, v in dropped.items()},
        "valid_notes_wrongly_excluded": dict(dropped_notes),
        "correct_growth": rounded((correct["2025-11"] - correct["2025-10"]) * 100 / correct["2025-10"], 2),
        "growth_after_wrong_note_filter": rounded((wrong["2025-11"] - wrong["2025-10"]) * 100 / wrong["2025-10"], 2),
        "historical_september2025_trade_in_reference_before": int(historical_before),
        "historical_september2025_trade_in_reference_after": int(historical_after),
        "note": "All nonempty status notes in this source denote returns. The initial audit missed 'khách mang trả'; the solver growth is correct for this data."}


def lookup_check():
    t = trace("q_0ef28ee17f12")
    candidate = next(c for c in t["candidates"] if c["verified"])
    program = candidate["program"]
    manifest = json.loads((RUN / "run_manifest.json").read_text(encoding="utf-8"))
    database = ROOT / "artifacts/cache/dev_002" / manifest["business_revisions"]["dev_002"][:20] / "business.sqlite"
    original = execute_sql(database, program["sql"], program["parameters"], t["plans"][-1]["tables"])
    assert program["sql"].count(" = ? AND") == 1
    diagnostic_sql = program["sql"].replace(" = ? AND", " = lookup(?) AND", 1)
    diagnostic = execute_sql(database, diagnostic_sql, program["parameters"], t["plans"][-1]["tables"])
    assert original.status == diagnostic.status == ExecutionStatus.SUCCESS
    return {"recorded_parameter": program["parameters"][0],
        "normalized_parameter": normalize_lookup(program["parameters"][0]),
        "original_sql_value": original.rows, "diagnostic_both_sides_normalized": diagnostic.rows,
        "note": "Counterfactual read-only query only; no SQL or prediction changed in inference artifacts."}


def main():
    assert invalid_status("khách mang trả")
    assert invalid_status("khách đem máy trả lại")
    assert not invalid_status("khách mang tiền trả nợ")
    audits = json.loads((OUT / "raw_audit.json").read_text(encoding="utf-8"))
    invoices, notes, lookup = duplicate_invoice_check(), nonempty_notes_check(), lookup_check()
    assert invoices["sum_after_exact_order_row_deduplication"] == audits["q_0bac8c88238d"]["expected"]
    assert notes["correct_growth"] == audits["q_0d23cc65cc7a"]["expected"]
    assert lookup["diagnostic_both_sides_normalized"] == [[audits["q_0ef28ee17f12"]["expected"]]]
    result = {"duplicate_invoice": invoices, "nonempty_notes": notes, "lookup_normalization": lookup,
              "all_three_crosschecks_match_raw_references": True}
    (OUT / "crosschecks.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
