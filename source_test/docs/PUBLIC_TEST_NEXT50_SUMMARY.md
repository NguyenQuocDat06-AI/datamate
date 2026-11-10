# Tổng kết lượt 50 câu tiếp theo, 16 luồng

Inference: **19:53:56–20:08:38 ngày 03/11/2026**, giờ Việt Nam. Khoảng **14.7 phút** từ khởi chạy đến đủ 50, gồm thời gian giữa hai phiên. Tổng thời gian hai runner hoạt động **12.6 phút**. Chuẩn bị cache/audit không nằm trong thời gian này.

Đợt đầu nhận 49/50 prediction sau khoảng 11 phút; một câu lỗi RemoteProtocolError đã được chạy bù từ checkpoint. Giữ cấu hình 16 workers và cùng hash runtime. Các câu hỏi/code khác lượt cũ nên chưa phải benchmark so tốc độ có kiểm soát giữa 4 và 16 luồng.

**Đối chiếu dữ liệu gốc: 45 khớp, 2 sai khác, 3 abstain.** Không có gold chính thức hoặc PA. Bộ audit đã bổ sung nhãn trả hàng và kiểm tra chéo các sai khác trước khi chốt.

| Sai khác | Prediction | Tính lại | Bằng chứng |
|---|---|---|---|
| Doanh thu hôm qua dev_009 | 15.582.000 | 15.535.000 | Hoá đơn ORD019871 bị xuất trùng 47.000 đồng, dòng CSV 20616. Bảng header/cache và SQL chưa loại trùng dòng này. |
| Ly Nước ép cam (M), 3/2025 | 0 | 151 | lookup(column) bỏ dấu ngoặc nhưng tham số còn `(m)`. Chuẩn hóa cả hai phía trong truy vấn kiểm tra chỉ đọc cho 151, khớp tổng từ nguồn. |

| Abstain | Đối chiếu | Quan sát từ trace |
|---|---|---|
| Trung bình đơn tuần trước dev_005 | 106.712 đồng | Metadata thời gian dùng alias không phải field catalog; kéo theo yêu cầu sai nhiều partition. Kỳ đúng 28/07–03/08/2025. |
| Số đơn bán chịu có hạn sau 10/12/2025 dev_004 | 152 đơn | Join ID tự đặt không có trong context; lần sau vẫn sai tên field/partition. Hiểu 'mấy đơn' là số lượng, không thêm điều kiện còn dư nợ mà câu không nêu. |
| Nhân viên thân thiện nhất dev_008 | unanswerable | Nguồn không ghi đánh giá mức độ thân thiện. Solver nêu đúng lý do nhưng không thiết lập được bằng chứng theo contract nên chọn abstain. |

**Đính chính audit trước khi chốt:** câu tăng trưởng doanh thu điện thoại có prediction **−12,82% là khớp**. Audit ban đầu bỏ sót nhãn `khách mang trả`, cho −16,52%. Nguồn chỉ có ghi chú trống hoặc ba cách ghi trả hàng; sau sửa audit, doanh thu đối chiếu tháng 10 là 442.188.500, tháng 11 là 385.480.000 đồng. Lọc mọi ghi chú không trống tình cờ đúng ở nguồn này, không nên mặc định đúng cho nguồn khác. Giữ [snapshot audit ban đầu](../artifacts/public_next50_16w_review/before_status_alias_correction/) và [ghi nhận đính chính](../artifacts/public_next50_16w_review/audit_status_alias_correction.json). Giá trị đối chiếu thu máy cũ 9/2025 của nhóm trước vẫn là 36.300.000, không thay đổi.

- [Bảng đủ 50 câu hỏi/prediction](PUBLIC_TEST_NEXT50_PREDICTIONS.md).
- [Review đủ 50 câu](PUBLIC_TEST_NEXT50_REVIEW.md), [phép tính gốc](../artifacts/public_next50_16w_review/raw_audit.json).
- [Kiểm tra chéo](../artifacts/public_next50_16w_review/crosschecks.json), [kiểm tra hoàn tất](../artifacts/public_next50_16w/completion_checks.json).
- [JSONL 50 câu mới](../result/public_test_next50_16w.jsonl), [ghép 100 ID](../result/public_test_100_collected.jsonl), [nguồn từng ID của file ghép](../artifacts/public_next50_16w/collection100_manifest.json).

Toàn bộ 14 business được chuẩn bị trước inference với normalizer hiện tại. Solver, prediction, raw sources giữ nguyên trong lúc audit; không sửa code theo các câu mới này. File ghép 100 có nhiều phiên bản solver và vẫn giữ lỗi của các lượt lịch sử.
