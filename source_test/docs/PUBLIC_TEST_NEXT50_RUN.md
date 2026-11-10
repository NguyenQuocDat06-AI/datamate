# Chạy 50 câu tiếp theo với 16 luồng

**Đã hoàn tất 50/50 prediction lúc 20:08:38 ngày 03/11/2026, giờ Việt Nam.** Đối chiếu độc lập sau kiểm tra chéo: **45 khớp, 2 sai khác, 3 abstain**; chưa phải gold/PA chính thức. Inference mất khoảng 14,7 phút kể cả khoảng chạy bù một câu lỗi kết nối. [Tổng kết và nguyên nhân](PUBLIC_TEST_NEXT50_SUMMARY.md), [bảng đủ 50 câu](PUBLIC_TEST_NEXT50_PREDICTIONS.md), [review](PUBLIC_TEST_NEXT50_REVIEW.md).

[JSONL 50 câu mới](../result/public_test_next50_16w.jsonl), [JSONL ghép 100 ID](../result/public_test_100_collected.jsonl), [kiểm tra hoàn tất](../artifacts/public_next50_16w/completion_checks.json). File 100 giữ nguyên kết quả cũ và mới; không phải 100 câu đều suy luận bằng cùng phiên bản solver.

Phạm vi: đúng 50 ID ở vị trí 51–100 của `questions.jsonl`, không trùng 50 ID ban đầu. [Selection](../artifacts/public_next50_16w/selection.json) ghi câu hỏi và thứ tự. Không nhập prediction hoặc plan từ các lượt trước.

Toàn bộ 14 doanh nghiệp public được chuẩn bị bằng normalizer hiện tại trước inference: [ingestion](../artifacts/public_next50_16w/public_ingestion.json). Không ghi nhận diagnostic severity error. File nguồn và cache cũ được giữ nguyên.

- Model `ag/gemini-3.8-flash-high`, Trammay API, SSE streaming, **16 workers**.
- Cấu hình: 420 giây/request, 900 giây/lần giải, tối đa 3 model calls và 1 repair/lần giải. Runner thử lại một lần khi lỗi vận chuyển. Đây là các budget cấu hình; không phải cam kết độ trễ.
- [Status](../artifacts/public_next50_16w/status.json), [log](../artifacts/public_next50_16w/run.log), [stderr](../artifacts/public_next50_16w/stderr.log).
- [Manifest và hash runtime](../artifacts/public_next50_16w/run_manifest.json). Giữ nguyên code solver trong lượt chạy.
- [Prediction từng phần](../result/public_test_next50_16w.partial.jsonl); file hoàn chỉnh khi đủ 50: `result/public_test_next50_16w.jsonl`.
- [Phép tính độc lập](../artifacts/public_next50_16w_review/): chạy riêng trên CSV/Excel/SQLite gốc, không đưa giá trị đối chiếu vào prompt. Report đúng/sai chỉ xuất khi đủ prediction và đủ phép tính có căn cứ.

Không có gold chính thức trong public. Tốc độ 16 luồng cần đánh giá bằng tiến độ/thời gian thực tế; chưa đo được giới hạn concurrency của key. Không gửi bài lên portal.
