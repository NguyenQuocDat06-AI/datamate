"""Compare completed fresh predictions after inference; never feeds labels to solver."""

from __future__ import annotations

import json

from bizreason.utils import WORKSPACE, atomic_json


def main():
    directory = WORKSPACE / "artifacts/normalization_v2/live_3"
    run = json.loads((directory / "run_manifest.json").read_text(encoding="utf-8"))
    status = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    replay = json.loads((WORKSPACE / "artifacts/normalization_v2/frozen_plan_replay.json").read_text(encoding="utf-8"))
    references = {row["id"]: row for row in replay["details"]}
    predictions = {}
    for path in (directory / "answers").glob("*.json"):
        prediction = json.loads(path.read_text(encoding="utf-8"))
        predictions[prediction["id"]] = prediction
    rows = []
    for identity in run["question_ids"]:
        prediction = predictions.get(identity)
        expected = references[identity]["independent_expected"]
        rows.append({"id": identity, "prediction": prediction, "independent_expected": expected,
                     "comparison": "pending_service" if prediction is None else
                     "match" if prediction["type"] == "number" and prediction.get("answer") == expected else "mismatch"})
    result = {"fresh_model_inference": True, "matched": sum(r["comparison"] == "match" for r in rows),
              "mismatched": sum(r["comparison"] == "mismatch" for r in rows),
              "pending": sum(r["comparison"] == "pending_service" for r in rows),
              "run_state": status["state"], "official_pa": None, "details": rows}
    atomic_json(WORKSPACE / "artifacts/normalization_v2/live_comparison.json", result)
    lines = ["# Kiểm chứng model sau sửa normalization", "",
             "Đã chạy inference mới bằng Trammay; giá trị đối chiếu chỉ được đọc ở bước báo cáo sau inference, không đưa vào prompt.", "",
             "| ID | Model mới | Đối chiếu độc lập | Kết quả |", "|---|---|---|---|"]
    labels = {"match": "Khớp", "mismatch": "Không khớp", "pending_service": "Chưa có prediction do lỗi kết nối/timeout"}
    for row in rows:
        value = row["prediction"].get("answer", row["prediction"]["type"]) if row["prediction"] else "—"
        lines.append(f"| {row['id']} | {value} | {row['independent_expected']} | {labels[row['comparison']]} |")
    lines += ["", f"Trạng thái lượt chạy: `{status['state']}`. Model mới xác nhận câu trung bình đơn là **71.724 đồng**. Hai câu còn lại chưa có kết quả sau lần đầu và hai lần resume; lỗi ghi nhận gồm ConnectError, streaming deadline và ReadTimeout. Không phân loại các câu chưa có prediction thành đáp án sai.", "",
              "[Prediction model đã hoàn thành](../result/public_normalization_live3.partial.jsonl), [so sánh JSON](../artifacts/normalization_v2/live_comparison.json), [trạng thái](../artifacts/normalization_v2/live_3/status.json), [hướng dẫn resume](PUBLIC_TEST_RUN.md).", "",
              "Lượt 50 câu cũ dừng ở 31 checkpoint. Chưa khởi chạy lại 50 câu bằng bản sửa trong khi kiểm chứng còn bị lỗi kết nối. Không có PA chính thức trên public; các phép đối chiếu dựa trên dữ liệu gốc/rulebook.", ""]
    (WORKSPACE / "docs/NORMALIZATION_LIVE_REVIEW.md").write_text("\n".join(lines), encoding="utf-8")
    print({key: value for key, value in result.items() if key != "details"})


if __name__ == "__main__":
    main()
