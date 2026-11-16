"""Independent references and review for frozen public questions 51--100."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import audit_next50_sales as sales
from scripts import audit_next50_ledgers as ledgers
from scripts.review_public_last20 import normalized_label, matches, sha, source_hashes, write_json

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "artifacts/public_next50_16w"
OUT = ROOT / "artifacts/public_next50_16w_review"


def calculate():
    OUT.mkdir(parents=True, exist_ok=True)
    before = source_hashes()
    write_json(OUT / "source_hashes_before.json", before)
    results, failures = {}, {}
    functions = [getattr(ledgers if i in (4, 6, 11, 14) else sales, f"audit_dev{i:03d}") for i in range(1, 15)]
    with ProcessPoolExecutor(max_workers=3) as pool:
        tasks = {pool.submit(fn): fn.__name__ for fn in functions}
        for future in as_completed(tasks):
            name = tasks[future]
            try:
                result = future.result()
                assert results.keys().isdisjoint(result)
                results.update(result)
                write_json(OUT / (name + ".json"), result)
                print(name, json.dumps({k: v["expected"] for k, v in result.items()}, ensure_ascii=False), flush=True)
            except Exception as error:
                failures[name] = f"{type(error).__name__}: {error}"
                print(name, failures[name], flush=True)
    assert before == source_hashes(), "Raw sources changed during audit"
    write_json(OUT / "raw_audit.partial.json", results)
    write_json(OUT / "calculation_status.json", {"computed": len(results), "failures": failures,
        "source_hashes_unchanged": True, "source_files_checked": len(before),
        "audit_script_sha256": {str(p.relative_to(ROOT)): sha(p) for p in
            [Path(sales.__file__), Path(ledgers.__file__), ROOT / "scripts/next50_common.py", Path(__file__)]}})
    if failures:
        raise RuntimeError("Independent calculations incomplete; no correctness totals published")
    selection = json.loads((RUN / "selection.json").read_text(encoding="utf-8"))
    assert set(results) == set(selection["ids"]) and len(results) == 50
    write_json(OUT / "raw_audit.json", results)


def value_matches(prediction, audit):
    if prediction["type"] == "list" and audit["expected_type"] == "list" and "list_aliases" in audit:
        actual = prediction.get("answer")
        return isinstance(actual, list) and len(actual) == len(audit["list_aliases"]) and all(
            normalized_label(value) in {normalized_label(alias) for alias in allowed}
            for value, allowed in zip(actual, audit["list_aliases"]))
    return matches(prediction, audit)


def report():
    status = json.loads((RUN / "status.json").read_text(encoding="utf-8"))
    if status["state"] != "complete" or status["completed"] != 50:
        raise ValueError("API batch is incomplete; do not report 50 scored predictions")
    selection = json.loads((RUN / "selection.json").read_text(encoding="utf-8"))
    audits = json.loads((OUT / "raw_audit.json").read_text(encoding="utf-8"))
    path = ROOT / "result/public_test_next50_16w.jsonl"
    predictions = {r["id"]: r for r in map(json.loads, path.read_text(encoding="utf-8").splitlines())}
    assert set(predictions) == set(audits) == set(selection["ids"])
    rows = []
    for question in selection["questions"]:
        identity = question["id"]
        p, a = predictions[identity], audits[identity]
        outcome = "abstain" if p["type"] == "abstain" else ("match" if value_matches(p, a) else "mismatch")
        rows.append({"question": question, "prediction": p, "expected_type": a["expected_type"],
                     "expected": a["expected"], "status": outcome})
    counts = dict(Counter(r["status"] for r in rows))
    write_json(OUT / "review.json", {"counts": counts, "details": rows,
        "prediction_sha256": sha(path), "raw_audit_sha256": sha(OUT / "raw_audit.json"),
        "official_gold_available": False, "pa": None,
        "note": "Independent question-specific source audit; not official scoring or unseen-business accuracy."})
    now = datetime.now(timezone(timedelta(hours=7)))
    lines = ["# Đối chiếu 50 câu tiếp theo — vị trí 51–100", "",
        f"Cập nhật {now:%H:%M ngày %d/%m/%Y}, giờ Việt Nam.", "",
        "Đã chạy inference mới qua Trammay bằng **16 luồng**, không nhập checkpoint từ 50 câu trước. "
        "Giữ nguyên solver trong lượt chạy. Giá trị đối chiếu tính độc lập từ dữ liệu gốc/rulebook, "
        "không dùng SQL hoặc cache chuẩn hóa của solver để tạo reference.", "",
        f"**{counts.get('match', 0)} khớp, {counts.get('mismatch', 0)} sai khác, {counts.get('abstain', 0)} abstain.** "
        "Chưa phải gold/PA chính thức. Các sai khác cần kiểm tra thêm bằng nguồn và trace trước khi sửa solver.", "",
        "| STT | ID | Business | Prediction | Đối chiếu độc lập | Kết luận |", "|---|---|---|---|---|---|"]
    for r in rows:
        q, p = r["question"], r["prediction"]
        predicted = p["type"] if p["type"] in {"abstain", "unanswerable"} else json.dumps(p.get("answer"), ensure_ascii=False)
        expected = "unanswerable" if r["expected_type"] == "unanswerable" else json.dumps(r["expected"], ensure_ascii=False)
        verdict = {"match": "Khớp", "mismatch": "Sai khác", "abstain": "Abstain"}[r["status"]]
        lines.append(f"| {q['index']} | {q['id']} | {q['business_id']} | {predicted} | {expected} | {verdict} |")
    lines += ["", "- [Prediction JSONL](../result/public_test_next50_16w.jsonl).",
        "- [Phép tính/bằng chứng gốc](../artifacts/public_next50_16w_review/raw_audit.json).",
        "- [Review JSON](../artifacts/public_next50_16w_review/review.json).",
        "- [Manifest phiên bản runtime](../artifacts/public_next50_16w/run_manifest.json).",
        "", "Audit không sửa file prediction hoặc code solver và không gọi API.", ""]
    (ROOT / "docs/PUBLIC_TEST_NEXT50_REVIEW.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(counts), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calculate-only", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    if not args.report_only:
        calculate()
    if not args.calculate_only:
        report()


if __name__ == "__main__":
    main()
