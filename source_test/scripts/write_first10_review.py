"""Render the frozen raw-source audit and original model predictions for review."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/public_first10"

NOTES = {
    "q_00bf8df3ea0b": (
        "Cộng thành tiền dòng hàng ngày 04/03/2025, loại cả đơn huỷ/trả, không cộng dòng tổng. "
        "Đọc Excel gốc: 112 dòng hợp lệ, 71 đơn, tổng 4.533.000 đồng. "
        "Trong cache có 33 ô tiền bị đọc nhỏ đi 1.000 lần, tổng thiếu 1.389.609 đồng; "
        "đồng thời dòng 813 có nhãn TỔNG NGÀY 04/03 ở cột Tên khách hàng bị kế thừa mã đơn/ngày/trạng thái, "
        "rồi cộng thêm 4.750.000 đồng. Vì vậy 4.533.000 − 1.389.609 + 4.750.000 = 7.893.391. "
        "Ví dụ dòng 696: Tổng tiền = 7.000đ, đúng là 7.000 đồng nhưng cache ghi 7. "
        "Đây là lỗi ingestion; SQL chạy thành công vẫn cho đáp án sai."
    ),
    "q_00c4e24fa228": (
        "Đọc BanHang_2026.xlsx, sheet T06-2026 và QuanLy.xlsx, sheet DanhMuc. "
        "Nối tên/cách viết tên vào đúng nhóm đồ dùng, kế thừa ghi chú trong cùng hoá đơn, "
        "loại đơn huỷ. Cộng TT của 104 dòng hàng hợp lệ, không cộng Voucher, Dùng điểm hoặc Phí ship: 16.226.000 đồng."
    ),
    "q_008fdebb6ec9": (
        "Đã mở header toàn bộ 17 file bán hàng và DanhSachQuyDoi.xlsx: không có cột hoặc bảng nhóm hàng; "
        "bảng quy đổi chỉ có croissant và tiramisu. Theo rulebook §3 và §7, không được suy nhóm trà từ tên món. "
        "Kết luận kiểm tra độc lập là unanswerable. Model cũng đề xuất điều này trong plan, nhưng validator "
        "báo MISSING_PARTITION rồi UNSUPPORTED_NOT_ESTABLISHED, nên đầu ra cuối là abstain. "
        "Theo thể lệ, abstain nhận 0 điểm; cần phân biệt với một đáp án unanswerable được chấm đúng."
    ),
    "q_014cdcc1da6e": (
        "8/26 là tháng 08/2026; ca chiều theo giờ tạo đơn từ 14:00 đến trước 22:00. "
        "Đọc chi_tiet_hoa_don.csv gốc, loại toàn bộ đơn huỷ/trả, cộng Thành tiền của 3.258 dòng thuộc "
        "878 đơn hợp lệ: 459.334.000 đồng. Tiền bán là doanh thu, không lấy tiền mặt khách đã trả."
    ),
    "q_013b4ff35811": (
        "as_of_date là Chủ nhật 16/11/2025: tuần này từ 10/11 đến hết 16/11. "
        "Đọc tất cả sheet SoBanHang.xlsx để phát hiện cả dòng trả hàng về sau; đơn có SL bán âm bị loại cả đơn. "
        "Không giao được là trạng thái huỷ. Sau loại huỷ/trả: 389 dòng, 171 đơn, cộng Amount = 230.034.300 đồng. "
        "Không dùng Tổng phiếu lặp ở từng dòng."
    ),
    "q_01ac0fc9f2a2": (
        "as_of_date 18/08/2026: tuần rồi từ 10/08 đến hết 16/08/2026. "
        "Đọc hai file bán hàng 2025/2026, cộng thành tiền dòng của đơn không huỷ/trả, rồi chia số mã đơn khác nhau: "
        "66.272.600 / 924 = 71.723,593… đồng; làm tròn nửa lên thành 71.724 đồng. "
        "Dữ liệu đủ để tính. Trace cho thấy SELECT DISTINCT * bị chặn vì đọc cả cột nội bộ __source_row; "
        "đầu ra cuối là abstain. Đây là trường hợp solver chưa trả được một câu có thể trả lời."
    ),
    "q_01ba91ff94c6": (
        "Tuần rồi là 03–09/11/2025. Đã đọc cả hai file ChiTietHoaDon_1.xlsx và ChiTietHoaDon_2.xlsx, "
        "loại đơn huỷ/trả, bỏ Phí phục vụ, cộng SL lẻ theo Ma hang. Top theo số lượng cơ bản là "
        "SP0004: 130; SP0016: 119; SP0014: 118; SP0006: 114; SP0003: 112. "
        "SP0003 có ba cách viết hoa thường của Cánh gà chiên nước mắm. SQL của model GROUP BY ma_hang, ten_hang "
        "làm tách cùng một mặt hàng thành nhiều nhóm; vì vậy lấy Bia Tiger lon (SP0002, 108) vào vị trí thứ 5. "
        "Bốn vị trí đầu khớp; danh sách hoàn chỉnh sai vì chấm cả tập và thứ tự."
    ),
    "q_0329c9711c55": (
        "Đọc danh_sach_hoa_don.csv gốc; lọc ngày 25/02/2026; bỏ trạng thái huỷ/trả, đếm Mã hóa đơn khác nhau: "
        "108 đơn. README xác nhận mỗi dòng là một hoá đơn, không phải chi tiết mặt hàng."
    ),
    "q_033e37dde4d5": (
        "Tuần này từ 22/06 đến hết 25/06/2026. Đọc pos.sqlite gốc ở chế độ read-only; nối don_hang, "
        "chi_tiet_don, san_pham bằng cả IDCuaHang và mã tương ứng. Loại đơn huỷ và mọi đơn có phiếu tra_hang; "
        "chỉ lấy sản phẩm có DonViCoSo = gói, cộng SLQuyDoi: 959 gói."
    ),
    "q_01d1fc600b26": (
        "Đọc So_chi_tiet_ban_hang.xlsx gốc, lọc cả tháng 07/2025 và tên Bia Heineken 330ml "
        "sau chuẩn hoá khoảng trắng/hoa thường. Loại cả đơn huỷ/trả, gồm cả cách ghi Đã hoàn hàng và hàng trả về; "
        "cộng Thành tiền, không lấy số lượng quy đổi nhân giá hiện tại: 3.142.000 đồng."
    ),
}


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def main():
    selection = json.loads((OUT / "selection.json").read_text(encoding="utf-8"))
    raw = json.loads((OUT / "raw_audit.json").read_text(encoding="utf-8"))
    predictions = {
        p["id"]: p
        for p in map(json.loads, (ROOT / "result/public_test_first10.jsonl").read_text(encoding="utf-8").splitlines())
    }
    assert len(predictions) == 10 and set(predictions) == set(selection["ids"])
    assert set(raw["results"]) == set(predictions), "All ten raw audits must succeed"
    traces = {}
    for path in (ROOT / "artifacts/public_50_trammay/answers").glob("*.json"):
        prediction = json.loads(path.read_text(encoding="utf-8"))
        if prediction["id"] in predictions:
            traces[prediction["id"]] = path.parent.parent / "traces" / path.name
    rows = []
    for question in selection["questions"]:
        identity = question["id"]
        prediction = predictions[identity]
        audit = raw["results"][identity]
        expected_type = audit.get("independent_type", "list" if isinstance(audit["independent_answer"], list) else "number")
        if prediction["type"] == "abstain":
            verdict = "abstain"
        else:
            verdict = "match" if (prediction["type"] == expected_type and prediction["answer"] == audit["independent_answer"]) else "mismatch"
        trace = json.loads(traces[identity].read_text(encoding="utf-8"))
        rows.append({"question": question, "original_prediction": prediction,
            "independent_type": expected_type, "independent_answer": audit["independent_answer"],
            "verdict": verdict, "explanation": NOTES[identity],
            "trace": traces[identity].relative_to(ROOT).as_posix(),
            "programs": [candidate["program"] for candidate in trace.get("candidates", [])],
            "pipeline_errors": trace.get("errors", []),
            "audit_evidence": f"artifacts/public_first10/raw_audit.json#results/{identity}"})
    counts = dict(Counter(row["verdict"] for row in rows))
    assert counts == {"mismatch": 2, "match": 6, "abstain": 2}
    payload = {"created_at": datetime.now(timezone.utc).isoformat(),
        "selection": selection["selection"], "counts": counts,
        "official_gold_available": False, "official_pa": None,
        "solver_outputs_changed": False, "details": rows}
    (OUT / "review.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Đối chiếu 10 đáp án public test đầu tiên đã hoàn thành", "",
        "Ngày kiểm tra: 03/11/2026. Lấy 10 checkpoint hoàn thành sớm nhất, có một checkpoint tái sử dụng từ lần chạy trước. "
        "Đây không nhất thiết là 10 câu đứng đầu questions.jsonl; danh sách ID đã được cố định trong [selection.json](../artifacts/public_first10/selection.json).", "",
        "**Kết quả đối chiếu độc lập: 6 câu khớp, 2 câu sai giá trị, 2 câu abstain.** "
        "Public test không phát hành đáp án chuẩn: các kết luận này dựa trên đọc file gốc và rulebook, chưa phải xác nhận từ bộ chấm chính thức; không báo PA chính thức.", "",
        "- [10 prediction gốc, giữ nguyên giá trị](../result/public_test_first10.jsonl).",
        "- [Câu hỏi, prediction, kết luận và SQL](../artifacts/public_first10/review.json).",
        "- [Phép tính trên nguồn gốc, dòng đóng góp, schema và SHA-256](../artifacts/public_first10/raw_audit.json).",
        "- [Code kiểm tra độc lập](../scripts/review_public_first10.py), [code dựng báo cáo](../scripts/write_first10_review.py).", "",
        "## Bảng kết quả", "",
        "Tiền trong bảng tính bằng VND. Khớp là khớp phép tính kiểm tra độc lập; abstain là bỏ trả lời, không tự đổi thành một đáp án sai.", "",
        "| # | ID | Câu hỏi | Model trả | Kiểm tra độc lập | Kết luận |",
        "|---|---|---|---|---|---|"]
    labels = {"match": "Khớp", "mismatch": "Sai", "abstain": "Bỏ trả lời"}
    for index, row in enumerate(rows, 1):
        q = row["question"]
        prediction = row["original_prediction"]
        answer = dumps(prediction.get("answer")) if prediction["type"] != "abstain" else "abstain"
        expected = "unanswerable" if row["independent_type"] == "unanswerable" else dumps(row["independent_answer"])
        lines.append("| " + " | ".join(map(cell, [index, q["id"], q["question"], answer, expected, labels[row["verdict"]]])) + " |")
    lines += ["", "## Chi tiết từng câu", ""]
    for index, row in enumerate(rows, 1):
        q = row["question"]
        lines += [f"### {index}. {q['id']}", "", f"**Câu hỏi nguyên văn:** {q['question']}", "",
            f"Doanh nghiệp: `{q['business_id']}`. `as_of_date`: `{q['as_of_date']}`.", "",
            "**Prediction gốc:**", "", "```json", dumps(row["original_prediction"]), "```", "",
            f"**Kết luận: {labels[row['verdict']]}.** {row['explanation']}", "",
            f"[Trace model](../{row['trace']})."]
        if row["programs"]:
            # The first candidate is the executed candidate; repaired plans might never execute.
            program = row["programs"][0]
            lines += ["", "SQL của candidate đã thực thi (giữ nguyên):", "", "```sql", program["sql"], "```", "",
                f"Parameters: `{dumps(program['parameters'])}`."]
        lines.append("")
    ranking = raw["results"]["q_01ba91ff94c6"]["ranking_by_product_code"]
    lines += ["## Bằng chứng hai lỗi giá trị", "",
        "**Doanh thu ngày:** [chênh lệch từng dòng với cache](../artifacts/public_first10/dev002_normalization_differences.json). "
        "Cột Tổng tiền trộn cách ghi `45k`, `101.000đ`, `50,500` và số trực tiếp. "
        "Một locale áp cho toàn cột đã đọc sai các giá trị có dấu chấm nghìn và ký hiệu đồng. "
        "Dòng tổng ngày nằm trong cột tên khách còn bị nhận là dòng giao dịch.", "",
        "**Top 5:** bảng dưới cộng theo mã hàng, bao gồm các cách viết tên khác nhau của cùng mã.", "",
        "| Hạng | Mã | Tên đại diện có trong dữ liệu | Số lượng cơ bản |",
        "|---|---|---|---:|"]
    for index, rank in enumerate(ranking[:6], 1):
        lines.append(f"| {index} | {rank['code']} | {rank['representative']} | {rank['quantity_base']} |")
    lines += ["", "## Phương pháp và giới hạn", "",
        "Script audit đọc trực tiếp Excel bằng openpyxl, CSV bằng csv, SQLite gốc bằng kết nối read-only; "
        "không dùng parser, compiler, executor hoặc SQL của solver để tạo các giá trị đối chiếu. "
        "Việc so cache chỉ phục vụ tìm nguyên nhân sau khi đã tính trên nguồn gốc. "
        "Đã xử lý kế thừa trong nhóm đơn, dòng trùng, quy ước tiền/ngày, huỷ/trả toàn đơn, đơn vị cơ bản và thời gian theo as_of_date. "
        "Code audit dùng riêng cho 10 câu này, không phải bộ chấm tổng quát đã được xác thực.", "",
        "Ở bước thăm dò, audit từng chưa nhận đủ nhãn huỷ/trả; kết quả trong báo cáo này đã được tính lại với "
        "các nhãn thực tế như Không giao được, Đã hoàn hàng và hàng trả về. Phần raw_status_counts cho phép rà soát lại cách diễn giải.", "",
        "Không gọi API thêm khi kiểm tra. Prediction gốc và code solver của đợt 50 câu đang chạy được giữ nguyên; "
        "các giá trị đối chiếu chưa được ghi đè vào file prediction. Mẫu là các câu hoàn thành sớm, không ngẫu nhiên, "
        "nên không dùng 6/10 hoặc 6/8 để dự đoán điểm của toàn bộ 1.000 câu.", "",
        "Muốn tái lập trong source_test:", "", "```powershell",
        "python -X utf8 -u -m scripts.review_public_first10",
        "python -X utf8 -m scripts.write_first10_review", "```", ""]
    (ROOT / "docs/PUBLIC_TEST_FIRST10_REVIEW.md").write_text("\n".join(lines), encoding="utf-8")
    print(dumps({"questions": len(rows), "counts": counts, "report": "docs/PUBLIC_TEST_FIRST10_REVIEW.md"}))


if __name__ == "__main__":
    main()
