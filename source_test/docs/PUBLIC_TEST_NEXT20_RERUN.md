# Chạy lại 20 câu sau debug

**Đây là chạy Solver hiện tại với plan đã lưu, không phải 20 lượt LLM mới.** Có trace và prediction riêng; dữ liệu/đáp án baseline được giữ nguyên.

Kết quả đối chiếu độc lập: **20/20 khớp** sau sửa. Public không có gold chính thức; không báo PA.

## Đính chính kết quả trước

Đã chạy 127 kiểm thử hồi quy, không failure/error: [JUnit](../artifacts/public_next20/rerun_after_debug/pytest_final.xml).

Báo cáo replay ban đầu ghi 18/20 vì hai dòng không khớp. Rà soát nguồn cho thấy bộ audit bỏ sót alias A.An, làm thiếu 9.402.500 đồng. Giá trị đúng là 25.180.500, trùng SQL cũ; sau sửa audit còn một sai lệch thực sự là lãi ròng.

SQL lãi ròng cũ dùng Đã hủy đơn/Khách trả trên cột canonical. Nó tính doanh thu 264.310.000 và giá vốn 78.056.007, thay vì 258.613.500 và 76.347.976; chi phí 220.592.000 đã đúng. Vì vậy lãi ròng lệch 3.988.469 đồng.

Khi chạy toàn bộ Solver, còn phát hiện prompt vượt 65.000 ký tự do hàng trăm diagnostic loại dòng tổng lặp lại. Đã gộp theo điều kiện/file/table thành count và tối đa ba vị trí mẫu; diagnostic đầy đủ trong catalog được giữ. Sau sửa, cả 20 câu đi qua bước tạo context và thực thi.

Bản sửa ràng buộc câu lãi ròng đơn giản với các cột đã xác minh, trạng thái canonical, số lượng bán × giá vốn tại bán và chi phí cùng kỳ; dùng arithmetic Decimal. Không sửa chuỗi SQL bằng thay thế toàn cục, không hardcode ID/tên doanh nghiệp/đáp án. Bộ validator cũng từ chối filter status còn dùng nhãn ngoài enum.

| # | ID | Prediction baseline | Chạy lại tại máy | Đối chiếu | Kết quả |
|---|---|---|---|---|---|
| 11 | q_03bbdfa6e0d5 | 68 | 68 | 68 | Khớp |
| 12 | q_037178b58fcc | 6134 | 6134 | 6134 | Khớp |
| 13 | q_0389b0c41da6 | -16.02 | -16.02 | -16.02 | Khớp |
| 14 | q_049f0a34efdc | 197 | 197 | 197 | Khớp |
| 15 | q_03bee605bf7f | 281304400 | 281304400 | 281304400 | Khớp |
| 16 | q_040d437fdf09 | 195008 | 70324 | 70324 | Khớp |
| 17 | q_04e55a8f8c0c | 49878 | 69630 | 69630 | Khớp |
| 18 | q_052834325528 | 54.36 | 54.36 | 54.36 | Khớp |
| 19 | q_0537f6e4f5ab | "2025-05" | "2025-05" | "2025-05" | Khớp |
| 20 | q_05ab1c71dcaa | "kl" | "La Minh Dương" | "La Minh Dương" | Khớp |
| 21 | q_05421814d0e3 | 2713000 | 2826100 | 2826100 | Khớp |
| 22 | q_06b7f437fcc4 | "abstain" | 25180500 | 25180500 | Khớp |
| 23 | q_0641a621b596 | [233818700, 248568700, 237185200] | [233818700, 248568700, 237185200] | [233818700, 248568700, 237185200] | Khớp |
| 24 | q_06de4545fe25 | 14 | 14 | 14 | Khớp |
| 25 | q_07b7be37628b | -8.16 | -8.16 | -8.16 | Khớp |
| 26 | q_06dfe652d857 | "abstain" | -38326476 | -38326476 | Khớp |
| 27 | q_0878814b6a76 | "Cty Trương Hoa" | "Cty Trương Hoa" | "Cty Trương Hoa" | Khớp |
| 28 | q_08b923f3c5c5 | "Cty Mai Hải" | "Cty Mai Hải" | "Cty Mai Hải" | Khớp |
| 29 | q_08f075b54698 | 14783 | 14783 | 14783 | Khớp |
| 30 | q_08c521dda3d9 | 1974947000 | 1974947000 | 1974947000 | Khớp |

- [Prediction chạy lại](../result/public_test_next20_replay_fixed.jsonl).
- [So sánh và hash code](../artifacts/public_next20/rerun_after_debug/comparison.json).
- [Trace thực thi](../artifacts/public_next20/rerun_after_debug/traces/).
- [Đính chính bộ audit](../artifacts/public_next20/audit_correction.json).

Các câu đã được xem/debug nên kết quả này chỉ là kiểm chứng cơ chế trên mẫu đã biết. Muốn đánh giá model/pipeline tổng quát cần lượt lập plan mới và tập chưa dùng để debug.
