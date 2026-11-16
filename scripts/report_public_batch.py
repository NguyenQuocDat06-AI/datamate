"""Render committed public predictions without reading audit/reference answers."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bizreason.utils import digest, writable_path


def cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>").replace("\r", "")


def render(artifacts: Path, output: Path) -> int:
    status = json.loads((artifacts / "status.json").read_text(encoding="utf-8"))
    selection = json.loads((artifacts / "selection.json").read_text(encoding="utf-8"))
    updated = datetime.fromisoformat(status["updated_at"]).astimezone(
        timezone(timedelta(hours=7))
    )
    lines = [
        "# Prediction cho 20 câu public còn lại",
        "",
        f"Cập nhật theo status lúc **{updated:%H:%M:%S ngày %d/%m/%Y}** (giờ Việt Nam).",
        "",
        f"Trạng thái: **{status['state']}**, **{status['completed']}/{status['total']}** prediction.",
        "",
        "Đây là kết quả inference mới trong đợt chạy 20 câu còn lại; một số checkpoint "
        "được chuyển từ giai đoạn timeout ngắn sang giai đoạn timeout dài, giữ nguyên solver. "
        "Chưa đối chiếu độc lập, chưa có đáp án chuẩn chính thức hoặc PA.",
        "",
        "STT 31–50 nối tiếp thứ tự review 30 câu trước; vị trí gốc ghi riêng vì các lượt "
        "review trước chọn theo thứ tự hoàn thành.",
        "",
        "| STT | Vị trí gốc | ID | Doanh nghiệp | Câu hỏi | Type | Prediction |",
        "|---|---|---|---|---|---|---|",
    ]
    committed = 0
    for question in selection["questions"]:
        path = artifacts / "answers" / (digest(question["id"])[:24] + ".json")
        prediction = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if prediction is not None:
            committed += 1
            kind = prediction["type"]
            answer = json.dumps(
                prediction.get("answer", prediction.get("reason", "")), ensure_ascii=False
            )
        else:
            kind, answer = "—", "Chưa có prediction"
        columns = [
            question["review_sequence"], question["original_input_position"],
            question["id"], question["business_id"], question["question"], kind, answer,
        ]
        lines.append("| " + " | ".join(cell(value) for value in columns) + " |")
    lines.extend([
        "",
        "[Cấu hình và nguồn kết quả](PUBLIC_TEST_LAST20_RUN.md). "
        "Số prediction không phải số câu đúng. Các nhãn abstain/unanswerable, nếu có, "
        "được giữ nguyên từ solver.",
        "",
    ])
    target = writable_path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    return committed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(f"Rendered {render(args.artifacts, args.output)} committed predictions")


if __name__ == "__main__":
    main()
