"""Run the current Solver on recorded plans, then independently compare outputs.

This is end-to-end local plan replay, not fresh remote model inference.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from bizreason.config import Settings
from bizreason.contracts import BusinessContext, Question
from bizreason.formatting import SubmissionWriter
from bizreason.llm import Usage
from bizreason.pipeline import Solver
from bizreason.rules import RuleRegistry
from bizreason.temporal import TemporalResolver
from bizreason.utils import atomic_json, digest, file_hash

ROOT = Path(__file__).resolve().parents[1]


class RecordedPlan:
    def __init__(self, plan):
        self.plan = plan
        self.usage = Usage()

    def generate_json(self, *args, **kwargs):
        self.usage.calls += 1
        return copy.deepcopy(self.plan)


def main():
    root = ROOT / "artifacts/public_next20/rerun_after_debug"
    root.mkdir(parents=True, exist_ok=True)
    selection = json.loads((ROOT / "artifacts/public_next20/selection.json").read_text(encoding="utf-8"))
    prepared = json.loads((ROOT / "artifacts/normalization_v2/public_ingestion_final.json").read_text(encoding="utf-8"))
    contexts = {}
    for record in prepared["businesses"]:
        path = ROOT / "artifacts/cache" / record["business_id"] / record["revision"][:20] / "catalog.json"
        contexts[record["business_id"]] = BusinessContext.model_validate_json(path.read_text(encoding="utf-8"))
    rules = RuleRegistry.load(ROOT / "data/public_test/rulebook.md")
    outputs = []
    for item in selection:
        question = Question.model_validate(item["question"])
        prediction, trace = Solver(RecordedPlan(item["plan"]), rules, TemporalResolver(),
                                   Settings(sql_timeout=30, question_timeout=60)).solve(question, contexts[question.business_id])
        atomic_json(root / "traces" / f"{digest(question.id)[:24]}.json", trace)
        outputs.append((item, prediction))
    with SubmissionWriter(ROOT / "result/public_test_next20_replay_fixed.jsonl") as writer:
        for _, prediction in outputs:
            writer.write(prediction)
    # Reference-like audit values are read only after all solver executions finish.
    references = json.loads((ROOT / "artifacts/public_next20/raw_audit.json").read_text(encoding="utf-8"))
    rows = []
    for item, prediction in outputs:
        identity = prediction.id
        expected = references[identity]["expected"]
        expected_type = item["prediction"]["type"] if item["prediction"]["type"] != "abstain" else "number"
        matched = prediction.type.value == expected_type and prediction.answer == expected
        rows.append({"index": item["index"], "id": identity, "question": item["question"],
                     "old_prediction": item["prediction"], "new_local_prediction": prediction.model_dump(mode="json", exclude_none=True),
                     "independent_expected": expected, "expected_type": expected_type, "matches": matched})
        print(identity, matched, prediction.answer, flush=True)
    report = {"mode": "current_solver_with_recorded_plans", "new_llm_inference": False,
              "api_calls": 0, "questions": len(rows), "matched": sum(r["matches"] for r in rows),
              "unmatched": sum(not r["matches"] for r in rows), "official_pa": None,
              "audit_correction": "A.An contributes 9,402,500; correct worker total is 25,180,500.",
              "runtime_sha256": {p.relative_to(ROOT).as_posix(): file_hash(p) for p in (ROOT / "bizreason").rglob("*.py")},
              "details": rows}
    atomic_json(root / "comparison.json", report)
    labels = {True: "Khớp", False: "Chưa khớp"}
    lines = ["# Chạy lại 20 câu sau debug", "",
             "**Đây là chạy Solver hiện tại với plan đã lưu, không phải 20 lượt LLM mới.** Có trace và prediction riêng; dữ liệu/đáp án baseline được giữ nguyên.", "",
             f"Kết quả đối chiếu độc lập: **{report['matched']}/20 khớp** sau sửa. Public không có gold chính thức; không báo PA.", "",
             "## Đính chính kết quả trước", "",
             "Báo cáo replay ban đầu ghi 18/20 vì hai dòng không khớp. Rà soát nguồn cho thấy bộ audit bỏ sót alias A.An, làm thiếu 9.402.500 đồng. Giá trị đúng là 25.180.500, trùng SQL cũ; sau sửa audit còn một sai lệch thực sự là lãi ròng.", "",
             "SQL lãi ròng cũ dùng Đã hủy đơn/Khách trả trên cột canonical. Nó tính doanh thu 264.310.000 và giá vốn 78.056.007, thay vì 258.613.500 và 76.347.976; chi phí 220.592.000 đã đúng. Vì vậy lãi ròng lệch 3.988.469 đồng.", "",
             "Bản sửa ràng buộc câu lãi ròng đơn giản với các cột đã xác minh, trạng thái canonical, số lượng bán × giá vốn tại bán và chi phí cùng kỳ; dùng arithmetic Decimal. Không sửa chuỗi SQL bằng thay thế toàn cục, không hardcode ID/tên doanh nghiệp/đáp án. Bộ validator cũng từ chối filter status còn dùng nhãn ngoài enum.", "",
             "Khi chạy toàn bộ Solver, còn phát hiện prompt vượt 65.000 ký tự do hàng trăm diagnostic loại dòng tổng lặp lại. Đã gộp theo điều kiện/file/table thành count và tối đa ba vị trí mẫu; diagnostic đầy đủ trong catalog được giữ. Sau sửa, cả 20 câu đi qua bước tạo context và thực thi.", "",
             "| # | ID | Prediction baseline | Chạy lại tại máy | Đối chiếu | Kết quả |",
             "|---|---|---|---|---|---|"]
    for row in rows:
        def cell(value):
            return json.dumps(value, ensure_ascii=False).replace("|", "\\|")
        lines.append(f"| {row['index']} | {row['id']} | {cell(row['old_prediction'].get('answer', row['old_prediction']['type']))} | {cell(row['new_local_prediction'].get('answer'))} | {cell(row['independent_expected'])} | {labels[row['matches']]} |")
    lines += ["", "- [Prediction chạy lại](../result/public_test_next20_replay_fixed.jsonl).",
              "- [So sánh và hash code](../artifacts/public_next20/rerun_after_debug/comparison.json).",
              "- [Trace thực thi](../artifacts/public_next20/rerun_after_debug/traces/).",
              "- [Đính chính bộ audit](../artifacts/public_next20/audit_correction.json).", "",
              "Các câu đã được xem/debug nên kết quả này chỉ là kiểm chứng cơ chế trên mẫu đã biết. Muốn đánh giá model/pipeline tổng quát cần lượt lập plan mới và tập chưa dùng để debug.", ""]
    (ROOT / "docs/PUBLIC_TEST_NEXT20_RERUN.md").write_text("\n".join(lines), encoding="utf-8")
    assert report["matched"] == 20, "Inspect comparison.json and traces before claiming success"


if __name__ == "__main__":
    main()
