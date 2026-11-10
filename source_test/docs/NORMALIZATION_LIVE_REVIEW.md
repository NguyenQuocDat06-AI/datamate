# Kiểm chứng model sau sửa normalization

Đã chạy inference mới bằng Trammay; giá trị đối chiếu chỉ được đọc ở bước báo cáo sau inference, không đưa vào prompt.

| ID | Model mới | Đối chiếu độc lập | Kết quả |
|---|---|---|---|
| q_01ac0fc9f2a2 | 71724 | 71724 | Khớp |
| q_06b7f437fcc4 | — | 25180500 | Chưa có prediction do lỗi kết nối/timeout |
| q_06dfe652d857 | — | -38326476 | Chưa có prediction do lỗi kết nối/timeout |

Trạng thái lượt chạy: `service_blocked`. Model mới xác nhận câu trung bình đơn là **71.724 đồng**. Hai câu còn lại chưa có kết quả sau lần đầu và hai lần resume; lỗi ghi nhận gồm ConnectError, streaming deadline và ReadTimeout. Không phân loại các câu chưa có prediction thành đáp án sai.

[Prediction model đã hoàn thành](../result/public_normalization_live3.partial.jsonl), [so sánh JSON](../artifacts/normalization_v2/live_comparison.json), [trạng thái](../artifacts/normalization_v2/live_3/status.json), [hướng dẫn resume](PUBLIC_TEST_RUN.md).

Lượt 50 câu cũ dừng ở 31 checkpoint. Chưa khởi chạy lại 50 câu bằng bản sửa trong khi kiểm chứng còn bị lỗi kết nối. Không có PA chính thức trên public; các phép đối chiếu dựa trên dữ liệu gốc/rulebook.
