# Kiến trúc V2: Semantic Layer và Metric Compiler

Trạng thái: bản nháp, các giả định kiến trúc chưa kiểm chứng xong (xem mục 5). Lập ngày 2026-11-10.

## 1. Bằng chứng dẫn tới thiết kế

| Số liệu | Nguồn |
|---|---|
| PA 0,687 trên 1.000 câu train đã dùng để phát triển, PA 0,029 trên 102 câu doanh nghiệp chưa thấy | Replay, chạy thật |
| 38/38 câu sai trên dữ liệu mới đều qua kiểm tra | Trace |
| 61 câu sai và abstain: luật 27, nghĩa cột 9, dữ liệu 6, thời gian 5, join và gộp 4, khác 10 | Truy nguyên, tái tạo đáp án 54/61 |
| 25/37 hàm sửa SQL gắn với tên cột, giá trị, câu chữ cụ thể | Rà soát `sql_healers.py` |
| Khoảng 85% câu hỏi biểu diễn được bằng 1–2 chỉ số, dạng câu, bộ lọc, thời gian | Khảo sát 900 câu, ước lượng |
| README mô tả nghĩa của phần lớn cột mà tầng nhận diện theo tên bỏ qua | Truy nguyên |

Kết luận: phần sai chủ yếu là **ý nghĩa nghiệp vụ**, không phải cú pháp SQL. Ý nghĩa nghiệp vụ đã được rulebook định nghĩa, nên phải do chương trình thực thi, không để model nhớ lại mỗi câu.

## 2. Nguyên tắc

1. **Không hardcode theo dữ liệu.** Không có tên bảng, tên cột, giá trị dữ liệu, mã doanh nghiệp, hay cụm từ câu hỏi cụ thể trong code. Từ vựng được phép duy nhất là bản thể nghiệp vụ lấy từ rulebook: danh sách vai trò cột, danh sách chỉ số.
2. **Model hiểu ngôn ngữ, chương trình tính toán.** Model chỉ ánh xạ văn bản sang cấu trúc đóng. Số liệu do compiler tạo ra.
3. **Mọi phán đoán của model được kiểm bằng dữ liệu** khi có thể.
4. **Không chắc thì không trả lời.** Thiếu vai trò, thực thể không khớp, ý định mơ hồ dẫn tới abstain, hoặc `unanswerable` khi có bằng chứng theo rulebook §7.
5. **Không sửa SQL bằng regex.** Lớp `sql_healers.py` không tham gia luồng mới.
6. **Hardcode bị chặn bằng test, không bằng lời hứa.** `tests/test_no_dataset_literals.py` quét mọi chuỗi trong `bizreason/semantic/`, trừ `vocabulary.py`, và làm hỏng build nếu thấy năm hoặc ngày cụ thể, mã câu hỏi hoặc doanh nghiệp, chữ tiếng Việt, hay tên bảng từng xuất hiện trong dữ liệu. `audit_rule6_clean.py` cũ chỉ tìm mã câu hỏi, mã doanh nghiệp và tên bộ dữ liệu nên không đủ.
7. **Thời gian chỉ tính ở một nơi.** Tết, tuần, kỳ so sánh do `TemporalResolver` tính từ `as_of_date` và `calendar.csv`.

## 3. Các tầng

```text
File nguồn
  └─ L0 Ingestion: suy kiểu từ giá trị (ngày trộn serial và chữ, tiền dạng chữ, khóa)
       └─ L1 Schema Understanding (model, một lần cho mỗi doanh nghiệp, có cache)
            └─ L2 Semantic Layer: bảng chuẩn, gộp partition, chuẩn hóa trạng thái
Câu hỏi
  └─ L3 Intent Parser (model) ─ L3b Temporal Resolver (chương trình)
       └─ L4 Entity Resolver (chương trình, dùng alias từ dữ liệu)
            └─ L5 Metric Compiler (registry chỉ số theo rulebook)
                 └─ L6 Thực thi và định dạng theo §9
                      └─ L7 Gate: giá trị, unanswerable, hoặc abstain
```

| Tầng | Vào | Ra | Cách kiểm |
|---|---|---|---|
| L0 | Cột thô | Kiểu đã chuẩn hóa, báo lỗi parse | Tỷ lệ parse theo cột |
| L1 | README, mẫu giá trị, thống kê cột, nhóm partition theo chữ ký | `kind` của bảng, `role` và `level` của cột, ánh xạ giá trị trạng thái và thanh toán | Từ vựng đóng, kiểm dữ liệu (cột cấp đơn phải lặp đều theo đơn, tiền phải dương...) |
| L2 | Chú giải L1 | Bảng chuẩn `orders`, `lines`, `payments`, `purchases`, `products`, `customers`, `inventory`... | Tổng dòng khớp nguồn, khóa không rỗng |
| L3 | Câu hỏi, danh sách chỉ số và chiều mà doanh nghiệp này có | JSON ý định: chỉ số, dạng câu, chiều, bộ lọc, cụm thời gian, top-k, thứ tự giá trị | Schema đóng, thử lại một lần |
| L4 | Giá trị trong câu | Mã hoặc tên chuẩn | Khớp duy nhất, nếu không thì abstain |
| L5 | Ý định, bảng chuẩn | SQL do chương trình sinh | Test đơn vị từng chỉ số theo rulebook |
| L7 | Mức đủ điều kiện của từng bước | Quyết định | Đường PA theo ngưỡng |

## 4. Xử lý phần ngoài registry

Khoảng 15% câu nằm ngoài. Thứ tự ưu tiên:

1. Câu đòi thuộc tính mà doanh nghiệp không có: `unanswerable` theo §7.
2. Sinh SQL trên **bảng chuẩn** (lược đồ nhỏ), kiểm bằng `EXPLAIN` của SQLite, không sửa bằng regex.
3. Chỉ trả lời khi vượt ngưỡng ở L7, còn lại abstain.

## 5. Giả định phải kiểm chứng trước khi xây tiếp

| # | Giả định | Bản thử | Ngưỡng đạt định trước |
|---|---|---|---|
| A1 | Model 12B gán đúng vai trò cột từ README | Chú giải 6 doanh nghiệp, so với nhãn từ truy nguyên | Đúng ít nhất 85% trên các cột trọng yếu |
| A2 | Model 12B điền ý định chính xác hơn viết SQL | 100 câu có đáp án, so ý định với đáp án tính được | Đúng ít nhất 90% trên chỉ số và dạng câu |
| A3 | Compiler tái tạo đáp án khi ý định đúng | Chạy registry trên câu thuộc phạm vi | Đúng ít nhất 95% |
| A4 | Hệ mới tốt hơn trên dữ liệu chưa thấy | 200 câu nghiệm thu đã khóa | PA cao hơn 0,029 một cách có ý nghĩa, tỷ lệ sai dưới 5% |

Nếu A1 hoặc A2 trượt, phải đổi thiết kế trước khi xây tiếp. Tập nghiệm thu chỉ được dùng cho A4 và chỉ khi hệ đã đóng băng.
