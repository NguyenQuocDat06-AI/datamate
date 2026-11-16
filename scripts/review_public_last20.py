"""Calculate raw references first, then compare frozen last-20 predictions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from scripts import audit_last20_sources as source

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/public_last20_review"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_hashes():
    paths = [p for p in source.DATA.rglob("*") if p.is_file()]
    paths += [ROOT / "data/public_test" / name for name in ("questions.jsonl", "rulebook.md", "calendar.csv")]
    return {str(p.relative_to(ROOT)): sha(p) for p in sorted(paths)}


def calculate():
    OUT.mkdir(parents=True, exist_ok=True)
    before = source_hashes()
    write_json(OUT / "source_hashes_before.json", before)
    results, failures = {}, {}
    functions = [getattr(source, f"audit_dev{number:03d}") for number in (1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14)]
    with ProcessPoolExecutor(max_workers=3) as pool:
        tasks = {pool.submit(fn): fn.__name__ for fn in functions}
        for future in as_completed(tasks):
            name = tasks[future]
            try:
                result = future.result()
                assert results.keys().isdisjoint(result)
                results.update(result)
                write_json(OUT / (name + ".json"), result)
                print(name, json.dumps({key: v["expected"] for key, v in result.items()}, ensure_ascii=False), flush=True)
            except Exception as error:
                failures[name] = f"{type(error).__name__}: {error}"
                print(name, failures[name], flush=True)
    after = source_hashes()
    assert before == after, "Source changed during independent audit"
    write_json(OUT / "calculation_status.json", {"computed": len(results), "failures": failures,
        "source_hashes_unchanged": True, "source_files_checked": len(before),
        "calculation_script_sha256": sha(Path(source.__file__)),
        "raw_reader_sha256": sha(ROOT / "scripts/review_public_first10.py"),
        "alias_helper_sha256": sha(ROOT / "scripts/review_public_next20.py")})
    write_json(OUT / "raw_audit.partial.json", results)
    if failures:
        raise RuntimeError("Raw calculations incomplete; do not publish correctness totals")
    assert len(results) == 20
    write_json(OUT / "raw_audit.json", results)


def normalized_label(value):
    return re.sub(r"[^\w]", "", str(value).casefold())


def matches(prediction, audit):
    if prediction["type"] != audit["expected_type"]:
        return False
    actual, expected = prediction.get("answer"), audit["expected"]
    if prediction["type"] in {"number", "percent"}:
        return abs(Decimal(str(actual)) - Decimal(str(expected))) < Decimal("0.000000001")
    if prediction["type"] == "string":
        aliases = [expected, *audit.get("aliases", [])]
        if "winner_code" in audit:
            aliases.append(audit["winner_code"])
        return normalized_label(actual) in {normalized_label(alias) for alias in aliases}
    return actual == expected


def report():
    audits = json.loads((OUT / "raw_audit.json").read_text(encoding="utf-8"))
    selection = json.loads((ROOT / "artifacts/public_last20_trammay_extended/selection.json").read_text(encoding="utf-8"))
    prediction_path = ROOT / "result/public_test_last20_trammay.jsonl"
    predictions = {r["id"]: r for r in map(json.loads, prediction_path.read_text(encoding="utf-8").splitlines())}
    assert set(predictions) == set(audits) == set(selection["ids"])
    rows = []
    for question in selection["questions"]:
        identity = question["id"]
        p, a = predictions[identity], audits[identity]
        status = "abstain" if p["type"] == "abstain" else ("match" if matches(p, a) else "mismatch")
        rows.append({"index": question["review_sequence"], "question": question,
            "prediction": p, "expected_type": a["expected_type"], "expected": a["expected"],
            "status": status, "evidence": f"{question['business_id']}: audit_{question['business_id']}.json"})
    counts = dict(Counter(r["status"] for r in rows))
    write_json(OUT / "review.json", {"counts": counts, "details": rows,
        "prediction_sha256": sha(prediction_path), "raw_audit_sha256": sha(OUT / "raw_audit.json"),
        "official_gold_available": False, "pa": None,
        "note": "Question-specific independent raw-source audit; not official grading or a new solver run."})
    now = datetime.now(timezone(timedelta(hours=7)))
    lines = ["# Đối chiếu độc lập 20 câu public cuối", "",
        f"Cập nhật {now:%H:%M ngày %d/%m/%Y}, giờ Việt Nam.", "",
        f"**{counts.get('match', 0)} khớp, {counts.get('mismatch', 0)} sai khác, {counts.get('abstain', 0)} abstain.**", "",
        "Tính lại từ CSV/Excel/SQLite gốc và rulebook; không dùng cache chuẩn hóa hoặc SQL của solver để tạo giá trị đối chiếu. "
        "Giá trị đối chiếu được tính và lưu trước khi script đọc prediction. Đây là audit theo từng câu, chưa phải gold/bộ chấm chính thức hoặc PA.", "",
        "| STT | ID | Prediction | Đối chiếu độc lập | Kết luận |", "|---|---|---|---|---|"]
    labels = {"match": "Khớp", "mismatch": "Sai khác", "abstain": "Abstain; dữ liệu có thể tính được"}
    for r in rows:
        p = "abstain" if r["status"] == "abstain" else json.dumps(r["prediction"].get("answer"), ensure_ascii=False)
        expected = json.dumps(r["expected"], ensure_ascii=False)
        lines.append(f"| {r['index']} | {r['question']['id']} | {p} | {expected} | {labels[r['status']]} |")
    lines += ["", "- [Bằng chứng tính toán từng câu](../artifacts/public_last20_review/raw_audit.json).",
        "- [Kết quả đối chiếu có cấu trúc](../artifacts/public_last20_review/review.json).",
        "- [Trạng thái tính toán và hash source audit](../artifacts/public_last20_review/calculation_status.json).",
        "", "Các file prediction và code solver được giữ nguyên. Không gửi dữ liệu tới API trong bước audit này.", ""]
    crosscheck_path = OUT / "crosschecks.json"
    if crosscheck_path.exists():
        crosscheck = json.loads(crosscheck_path.read_text(encoding="utf-8"))
        assert crosscheck["all_three_checks_match_raw_audit"]
        assert crosscheck["vehicle_count"]["distinct_plates"] == audits["q_07e2508ad671"]["expected"]
        assert crosscheck["customer_debt"]["expected"] == audits["q_0a9819711554"]["expected"]
        assert crosscheck["installments"]["expected"] == audits["q_0594583b3d46"]["expected"]
        lines += [
            "## Câu sai: đếm xe SH Mode", "",
            "`q_07e2508ad671`: prediction **30**, đối chiếu **27 biển số** trong tháng 8/2026. "
            "Theo rulebook §6 Sửa xe, số chiếc xe là số biển số khác nhau, kể cả xe chỉ tới bảo hành.", "",
            "SQL đã lưu dùng `COUNT(DISTINCT xe)` trên cả ô tên dòng xe + biển số. "
            "Sổ có 30 chuỗi khác nhau nhưng chỉ 27 biển số. Ba biển dưới đây xuất hiện dưới hai cách viết tên dòng xe:", "",
            "| Biển số | Dòng trong sheet T8-2026 | Cách ghi |", "|---|---|---|",
        ]
        for plate, entries in crosscheck["vehicle_count"]["duplicate_plates"].items():
            lines.append(f"| {plate} | {', '.join(str(e['row']) for e in entries)} | {' / '.join(e['raw_vehicle'] for e in entries)} |")
        lines += [
            "", "## Hai câu abstain có đủ dữ liệu để tính", "",
            "**Dư nợ trả góp — `q_0594583b3d46`: 400.700.000 đồng.** "
            "Có 51 hợp đồng mua đến hết 18/10/2025. Tổng gốc sau trả trước là 476.700.000 đồng, "
            "đã góp đến cutoff 76.000.000 đồng; cộng phần còn nợ dương từng hợp đồng được 400.700.000 đồng, "
            "bao gồm kỳ chưa tới hạn. Đối chiếu hợp đồng với STT/ngày đơn trong sổ bán, loại đơn huỷ và giữ quy tắc công nợ §4. "
            "Cách kiểm tra thứ hai nhóm trực tiếp theo STT, không suy nhóm bằng thứ tự dòng. "
            "Trace solver có candidate lỗi cú pháp SQL `near AS`; sau repair/probe chưa có candidate đủ điều kiện.", "",
            "**Lê Sơn Trung — `q_0a9819711554`: `[8000, 4000]`, type `list`.** "
            "Theo rulebook §4 và §9, hai số theo thứ tự nợ thêm rồi trả nợ. Khách KH0045 có đơn "
            "ORD036302 ngày 20/07/2026, tổng phải trả 21.000 đồng; trả ngay 13.000 đồng nên nợ thêm "
            "8.000 đồng. Khoản trả 4.000 đồng ngày 31/07 tính vào trả nợ tháng 7; khoản 4.000 đồng "
            "ngày 05/08 nằm ngoài kỳ. Kiểm tra SQL độc lập trên SQLite gốc cũng cho `[8000, 4000]`. "
            "Trace solver lỗi kiểu QueryPlan rồi `INVALID_ENTITY_CONSTRAINT`.", "",
            "[Bằng chứng kiểm tra lần hai cho cả ba trường hợp](../artifacts/public_last20_review/crosschecks.json). "
            "Các giá trị đối chiếu không được ghi ngược vào prediction. Chưa sửa solver trong lượt audit này.", "",
        ]
    (ROOT / "docs/PUBLIC_TEST_LAST20_REVIEW.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(counts), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    if not args.report_only:
        calculate()
    report()


if __name__ == "__main__":
    main()
