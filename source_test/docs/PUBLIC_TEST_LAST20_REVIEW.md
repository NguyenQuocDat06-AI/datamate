# Đối chiếu độc lập 20 câu public cuối

**Đã debug và sửa sau audit:** [báo cáo cơ chế dùng chung](GENERALIZED_REASONING_FIXES.md), [prediction từ model mới của ba câu lỗi](../result/public_generalized_fixes_live3_v2.jsonl). Bảng dưới vẫn đối chiếu prediction lịch sử nguyên trạng.

Cập nhật 18:34 ngày 03/11/2026, giờ Việt Nam.

**17 khớp, 1 sai khác, 2 abstain.**

Tính lại từ CSV/Excel/SQLite gốc và rulebook; không dùng cache chuẩn hóa hoặc SQL của solver để tạo giá trị đối chiếu. Giá trị đối chiếu được tính và lưu trước khi script đọc prediction. Đây là audit theo từng câu, chưa phải gold/bộ chấm chính thức hoặc PA.

| STT | ID | Prediction | Đối chiếu độc lập | Kết luận |
|---|---|---|---|---|
| 31 | q_009187cd2960 | 77 | 77 | Khớp |
| 32 | q_01d1099489a0 | -2.94 | -2.94 | Khớp |
| 33 | q_027f5441831d | 8421700 | 8421700 | Khớp |
| 34 | q_02feb595d6b7 | 123 | 123 | Khớp |
| 35 | q_03622bae312b | 25.71 | 25.71 | Khớp |
| 36 | q_0594583b3d46 | abstain | 400700000 | Abstain; dữ liệu có thể tính được |
| 37 | q_05cde771f4c3 | 226846 | 226846 | Khớp |
| 38 | q_07ceefc30333 | "Cà phê sữa (M)" | "Cà phê sữa (M)" | Khớp |
| 39 | q_07e2508ad671 | 30 | 27 | Sai khác |
| 40 | q_09542f67d04a | 71465 | 71465 | Khớp |
| 41 | q_099a18d1ed60 | 466616500 | 466616500 | Khớp |
| 42 | q_09e3efc66c68 | 169866 | 169866 | Khớp |
| 43 | q_0a3308eb76bb | 10 | 10 | Khớp |
| 44 | q_0a9819711554 | abstain | [8000, 4000] | Abstain; dữ liệu có thể tính được |
| 45 | q_0ac818a0f5f2 | 2.15 | 2.15 | Khớp |
| 46 | q_0acdf488056e | -6.28 | -6.28 | Khớp |
| 47 | q_0ae9b7cf7003 | 41959500 | 41959500 | Khớp |
| 48 | q_0b0081426a17 | 12823000 | 12823000 | Khớp |
| 49 | q_0b78b53675c1 | "Berberin 100mg Mekophar chai 100 viên" | "Berberin 100mg Mekophar chai 100 viên" | Khớp |
| 50 | q_0b854610073b | 36300000 | 36300000 | Khớp |

- [Bằng chứng tính toán từng câu](../artifacts/public_last20_review/raw_audit.json).
- [Kết quả đối chiếu có cấu trúc](../artifacts/public_last20_review/review.json).
- [Trạng thái tính toán và hash source audit](../artifacts/public_last20_review/calculation_status.json).

Các file prediction và code solver được giữ nguyên. Không gửi dữ liệu tới API trong bước audit này.

## Câu sai: đếm xe SH Mode

`q_07e2508ad671`: prediction **30**, đối chiếu **27 biển số** trong tháng 8/2026. Theo rulebook §6 Sửa xe, số chiếc xe là số biển số khác nhau, kể cả xe chỉ tới bảo hành.

SQL đã lưu dùng `COUNT(DISTINCT xe)` trên cả ô tên dòng xe + biển số. Sổ có 30 chuỗi khác nhau nhưng chỉ 27 biển số. Ba biển dưới đây xuất hiện dưới hai cách viết tên dòng xe:

| Biển số | Dòng trong sheet T8-2026 | Cách ghi |
|---|---|---|
| 59H521594 | 63, 392 | SH Mode 59H5-215.94 / shmode 59H5-215.94 |
| 54F767334 | 105, 134 | SH Mode 54F7-673.34 / shmode 54F7-673.34 |
| 61C158343 | 143, 289 | shmode 61C1-583.43 / SH mode 61C1-583.43 |

## Hai câu abstain có đủ dữ liệu để tính

**Dư nợ trả góp — `q_0594583b3d46`: 400.700.000 đồng.** Có 51 hợp đồng mua đến hết 18/10/2025. Tổng gốc sau trả trước là 476.700.000 đồng, đã góp đến cutoff 76.000.000 đồng; cộng phần còn nợ dương từng hợp đồng được 400.700.000 đồng, bao gồm kỳ chưa tới hạn. Đối chiếu hợp đồng với STT/ngày đơn trong sổ bán, loại đơn huỷ và giữ quy tắc công nợ §4. Cách kiểm tra thứ hai nhóm trực tiếp theo STT, không suy nhóm bằng thứ tự dòng. Trace solver có candidate lỗi cú pháp SQL `near AS`; sau repair/probe chưa có candidate đủ điều kiện.

**Lê Sơn Trung — `q_0a9819711554`: `[8000, 4000]`, type `list`.** Theo rulebook §4 và §9, hai số theo thứ tự nợ thêm rồi trả nợ. Khách KH0045 có đơn ORD036302 ngày 20/07/2026, tổng phải trả 21.000 đồng; trả ngay 13.000 đồng nên nợ thêm 8.000 đồng. Khoản trả 4.000 đồng ngày 31/07 tính vào trả nợ tháng 7; khoản 4.000 đồng ngày 05/08 nằm ngoài kỳ. Kiểm tra SQL độc lập trên SQLite gốc cũng cho `[8000, 4000]`. Trace solver lỗi kiểu QueryPlan rồi `INVALID_ENTITY_CONSTRAINT`.

[Bằng chứng kiểm tra lần hai cho cả ba trường hợp](../artifacts/public_last20_review/crosschecks.json). Các giá trị đối chiếu không được ghi ngược vào prediction. Chưa sửa solver trong lượt audit này.
