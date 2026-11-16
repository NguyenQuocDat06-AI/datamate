"""Validate immutable batch artifacts and publish the next-50 run summary."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bizreason.contracts import AnswerType, OutputSpec, Prediction
from bizreason.formatting import validate_answer
from bizreason.utils import atomic_json, digest, file_hash

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "artifacts/public_next50_16w"
AUDIT = ROOT / "artifacts/public_next50_16w_review"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def main():
    status, manifest, selection = [read(RUN / name) for name in ("status.json", "run_manifest.json", "selection.json")]
    predictions = jsonl(ROOT / "result/public_test_next50_16w.jsonl")
    assert status["state"] == "complete" and status["completed"] == status["total"] == 50
    assert manifest["workers"] == status["workers"] == 16
    assert len(predictions) == len({r["id"] for r in predictions}) == 50
    assert {r["id"] for r in predictions} == set(selection["ids"])
    assert set(selection["previous_50_ids"]).isdisjoint(selection["ids"])
    current = {str(p.relative_to(ROOT)): file_hash(p) for p in (ROOT / "bizreason").rglob("*.py")}
    assert current == manifest["implementation"]
    assert file_hash(ROOT / "scripts/run_public_test.py") == manifest["runner_sha256"]
    for row in predictions:
        p = Prediction.model_validate(row)
        if p.type not in {AnswerType.ABSTAIN, AnswerType.UNANSWERABLE}:
            validate_answer(p.answer, OutputSpec(type=p.type))
        filename = digest(p.id)[:24] + ".json"
        assert read(RUN / "answers" / filename) == row
        trace = read(RUN / "traces" / filename)
        assert trace["question"]["id"] == p.id
        assert trace["source_revision"] == manifest["business_revisions"][trace["question"]["business_id"]]
        if p.type not in {AnswerType.ABSTAIN, AnswerType.UNANSWERABLE}:
            assert any(c["verified"] and c["value"] == p.answer for c in trace["candidates"])
    sources = read(AUDIT / "source_hashes_before.json")
    assert all(file_hash(ROOT / name) == expected for name, expected in sources.items())
    references, calculation, review = [read(AUDIT / name) for name in ("raw_audit.json", "calculation_status.json", "review.json")]
    assert calculation["computed"] == 50 and not calculation["failures"]
    assert set(references) == set(selection["ids"])
    assert review["prediction_sha256"] == file_hash(ROOT / "result/public_test_next50_16w.jsonl")
    assert review["counts"] == {"match": 45, "mismatch": 2, "abstain": 3}
    assert read(AUDIT / "crosschecks.json")["all_three_crosschecks_match_raw_references"]
    old_refs = read(AUDIT / "before_status_alias_correction/raw_audit.json")
    corrections = [{"id": identity, "before": old_refs[identity]["expected"], "after": value["expected"]}
                   for identity, value in references.items() if old_refs[identity]["expected"] != value["expected"]]
    atomic_json(AUDIT / "audit_status_alias_correction.json", {"changes": corrections,
        "reason": "Initial audit missed returned-status phrase 'khách mang trả'. All raw status notes in this phone source are blank or return descriptions. Original API predictions unchanged.",
        "previous_audit_archived": "before_status_alias_correction", "solver_changed": False})
    first50 = jsonl(ROOT / "result/public_test_50_collected.jsonl")
    collected = jsonl(ROOT / "result/public_test_100_collected.jsonl")
    combined = {r["id"]: r for r in [*first50, *predictions]}
    assert len(collected) == len({r["id"] for r in collected}) == 100
    assert collected == [combined[i] for i in selection["previous_50_ids"] + selection["ids"]]
    started = datetime.fromisoformat(read(RUN / "launcher.json")["started_at"])
    finished = datetime.fromisoformat(status["updated_at"])
    seconds = (finished - started).total_seconds()
    first_log = (RUN / "run.log").read_text(encoding="utf-8")
    first_status = json.loads(first_log[first_log.rfind("\n{") + 1:])
    active_seconds = first_status["elapsed_this_session_seconds"] + status["elapsed_this_session_seconds"]
    verification = {"workers": 16, "fresh_prediction_count": 50, "combined_unique_count": 100,
        "counts": review["counts"], "exact_ids_no_overlap": True, "checkpoint_trace_checks_passed": True,
        "runtime_unchanged": True, "source_files_checked": len(sources), "source_hash_mismatches": 0,
        "prediction_sha256": file_hash(ROOT / "result/public_test_next50_16w.jsonl"),
        "combined100_sha256": file_hash(ROOT / "result/public_test_100_collected.jsonl"),
        "inference_wall_seconds_including_resume_gap": round(seconds, 3),
        "active_runner_seconds": round(active_seconds, 3),
        "initial_completed": first_status["completed"], "initial_service_issues": first_status["service_issues"],
        "model_calls_reported": status["model_calls"], "model_tokens_reported": status["model_tokens"],
        "audit_corrections": corrections, "official_gold_available": False, "pa": None}
    atomic_json(RUN / "completion_checks.json", verification)
    question_by_id = {r["id"]: r for r in selection["questions"]}
    lines = ["# Prediction 50 câu tiếp theo — vị trí 51–100", "",
        "Inference mới bằng 16 luồng; bảng giữ nguyên prediction từ API. "
        "[Đối chiếu độc lập](PUBLIC_TEST_NEXT50_REVIEW.md): **45 khớp, 2 sai khác, 3 abstain**; chưa phải chấm chính thức.", "",
        "| STT | ID | Business | Câu hỏi | Type | Prediction |", "|---|---|---|---|---|---|"]
    for p in predictions:
        q = question_by_id[p["id"]]
        value = p["type"] if p["type"] in {"abstain", "unanswerable"} else json.dumps(p["answer"], ensure_ascii=False)
        lines.append("| " + " | ".join(cell(v) for v in [q["index"], q["id"], q["business_id"], q["question"], p["type"], value]) + " |")
    lines += ["", "[JSONL 50 câu](../result/public_test_next50_16w.jsonl). "
        "[File ghép 100 ID](../result/public_test_100_collected.jsonl) gồm 50 prediction cũ nguyên trạng và 50 mới, "
        "không phải 100 lần inference bằng cùng phiên bản.", ""]
    (ROOT / "docs/PUBLIC_TEST_NEXT50_PREDICTIONS.md").write_text("\n".join(lines), encoding="utf-8")
    timezone_vn = timezone(timedelta(hours=7))
    summary = ["# Tổng kết lượt 50 câu tiếp theo, 16 luồng", "",
        f"Inference: **{started.astimezone(timezone_vn):%H:%M:%S}–{finished.astimezone(timezone_vn):%H:%M:%S ngày %d/%m/%Y}**, giờ Việt Nam. "
        f"Khoảng **{seconds / 60:.1f} phút** từ khởi chạy đến đủ 50, gồm thời gian giữa hai phiên. "
        f"Tổng thời gian hai runner hoạt động **{active_seconds / 60:.1f} phút**. Chuẩn bị cache/audit không nằm trong thời gian này.", "",
        "Đợt đầu nhận 49/50 prediction sau khoảng 11 phút; một câu lỗi RemoteProtocolError đã được chạy bù từ checkpoint. "
        "Giữ cấu hình 16 workers và cùng hash runtime. Các câu hỏi/code khác lượt cũ nên chưa phải benchmark so tốc độ có kiểm soát giữa 4 và 16 luồng.", "",
        "**Đối chiếu dữ liệu gốc: 45 khớp, 2 sai khác, 3 abstain.** Không có gold chính thức hoặc PA. "
        "Bộ audit đã bổ sung nhãn trả hàng và kiểm tra chéo các sai khác trước khi chốt.", "",
        "| Sai khác | Prediction | Tính lại | Bằng chứng |", "|---|---|---|---|",
        "| Doanh thu hôm qua dev_009 | 15.582.000 | 15.535.000 | Hoá đơn ORD019871 bị xuất trùng 47.000 đồng, dòng CSV 20616. Bảng header/cache và SQL chưa loại trùng dòng này. |",
        "| Ly Nước ép cam (M), 3/2025 | 0 | 151 | lookup(column) bỏ dấu ngoặc nhưng tham số còn `(m)`. Chuẩn hóa cả hai phía trong truy vấn kiểm tra chỉ đọc cho 151, khớp tổng từ nguồn. |", "",
        "| Abstain | Đối chiếu | Quan sát từ trace |", "|---|---|---|",
        "| Trung bình đơn tuần trước dev_005 | 106.712 đồng | Metadata thời gian dùng alias không phải field catalog; kéo theo yêu cầu sai nhiều partition. Kỳ đúng 28/07–03/08/2025. |",
        "| Số đơn bán chịu có hạn sau 10/12/2025 dev_004 | 152 đơn | Join ID tự đặt không có trong context; lần sau vẫn sai tên field/partition. Hiểu 'mấy đơn' là số lượng, không thêm điều kiện còn dư nợ mà câu không nêu. |",
        "| Nhân viên thân thiện nhất dev_008 | unanswerable | Nguồn không ghi đánh giá mức độ thân thiện. Solver nêu đúng lý do nhưng không thiết lập được bằng chứng theo contract nên chọn abstain. |", "",
        "**Đính chính audit trước khi chốt:** câu tăng trưởng doanh thu điện thoại có prediction **−12,82% là khớp**. "
        "Audit ban đầu bỏ sót nhãn `khách mang trả`, cho −16,52%. Nguồn chỉ có ghi chú trống hoặc ba cách ghi trả hàng; "
        "sau sửa audit, doanh thu đối chiếu tháng 10 là 442.188.500, tháng 11 là 385.480.000 đồng. "
        "Lọc mọi ghi chú không trống tình cờ đúng ở nguồn này, không nên mặc định đúng cho nguồn khác. "
        "Giữ [snapshot audit ban đầu](../artifacts/public_next50_16w_review/before_status_alias_correction/) và "
        "[ghi nhận đính chính](../artifacts/public_next50_16w_review/audit_status_alias_correction.json). "
        "Giá trị đối chiếu thu máy cũ 9/2025 của nhóm trước vẫn là 36.300.000, không thay đổi.", "",
        "- [Bảng đủ 50 câu hỏi/prediction](PUBLIC_TEST_NEXT50_PREDICTIONS.md).",
        "- [Review đủ 50 câu](PUBLIC_TEST_NEXT50_REVIEW.md), [phép tính gốc](../artifacts/public_next50_16w_review/raw_audit.json).",
        "- [Kiểm tra chéo](../artifacts/public_next50_16w_review/crosschecks.json), [kiểm tra hoàn tất](../artifacts/public_next50_16w/completion_checks.json).",
        "- [JSONL 50 câu mới](../result/public_test_next50_16w.jsonl), [ghép 100 ID](../result/public_test_100_collected.jsonl), "
        "[nguồn từng ID của file ghép](../artifacts/public_next50_16w/collection100_manifest.json).", "",
        "Toàn bộ 14 business được chuẩn bị trước inference với normalizer hiện tại. Solver, prediction, raw sources giữ nguyên "
        "trong lúc audit; không sửa code theo các câu mới này. File ghép 100 có nhiều phiên bản solver và vẫn giữ lỗi của các lượt lịch sử.", ""]
    (ROOT / "docs/PUBLIC_TEST_NEXT50_SUMMARY.md").write_text("\n".join(summary), encoding="utf-8")
    print(json.dumps({k: v for k, v in verification.items() if k not in {"audit_corrections", "initial_service_issues"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
