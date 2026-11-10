# Prediction cho 20 câu public còn lại

**Ba lỗi đã được sửa và kiểm chứng bằng model mới:** xem [báo cáo debug](GENERALIZED_REASONING_FIXES.md) và [JSONL mới](../result/public_generalized_fixes_live3_v2.jsonl). Bảng này giữ nguyên prediction của lượt chạy ban đầu.

Cập nhật theo status lúc **18:13:43 ngày 03/11/2026** (giờ Việt Nam).

Trạng thái: **complete**, **20/20** prediction.

Đây là kết quả inference mới trong đợt chạy 20 câu còn lại; một số checkpoint được chuyển từ giai đoạn timeout ngắn sang giai đoạn timeout dài, giữ nguyên solver. **Đã đối chiếu độc lập với dữ liệu gốc: 17 câu khớp, 1 câu sai khác, 2 abstain.** Xem [báo cáo kiểm chứng](PUBLIC_TEST_LAST20_REVIEW.md). Bảng dưới giữ nguyên prediction; chưa có đáp án chuẩn chính thức hoặc PA.

STT 31–50 nối tiếp thứ tự review 30 câu trước; vị trí gốc ghi riêng vì các lượt review trước chọn theo thứ tự hoàn thành.

| STT | Vị trí gốc | ID | Doanh nghiệp | Câu hỏi | Type | Prediction |
|---|---|---|---|---|---|---|
| 31 | 2 | q_009187cd2960 | dev_004 | Tháng 5/2026 bán ra bao nhiêu cây Ống PVC Bình Minh D50 cây 4m? | number | 77 |
| 32 | 9 | q_01d1099489a0 | dev_005 | Từ đầu tháng này tới hết hnay, dthu cao hay thấp hơn cùng kỳ tháng trước mấy %? | percent | -2.94 |
| 33 | 11 | q_027f5441831d | dev_012 | Tháng 8/2025 thợ Đoàn Bảo Phúc lãnh bao nhiêu tiền huê hồng? | number | 8421700 |
| 34 | 12 | q_02feb595d6b7 | dev_004 | Tháng 3/2026 bán ra bao nhiêu cây Ống PVC Bình Minh D32 cây 4m? | number | 123 |
| 35 | 15 | q_03622bae312b | dev_004 | Th6/2025 khách có từ 4 đơn trở lên chiếm mấy % tổng số khách mua? | percent | 25.71 |
| 36 | 26 | q_0594583b3d46 | dev_014 | Đến hết hôm nay, các hợp đồng trả góp còn lại bao nhiêu tiền chưa góp, tính cả kỳ chưa tới hạn? | abstain | "No eligible candidate or unsupported evidence" |
| 37 | 28 | q_05cde771f4c3 | dev_012 | Th2/2026 không tính xe quay lại bảo hành, bình quân 1 phiếu sửa hết bnhiu tiền? | number | 226846 |
| 38 | 34 | q_07ceefc30333 | dev_003 | T4/2026 tính cả tháng thì sp nào lãi gộp nhiều nhất? | string | "Cà phê sữa (M)" |
| 39 | 35 | q_07e2508ad671 | dev_012 | check jum, shop mik thang nay bn chiec SH Mode khac nhau zo sua hen | number | 30 |
| 40 | 40 | q_09542f67d04a | dev_003 | Dthu bình quân mỗi đơn tuần trước là bnhiu z? | number | 71465 |
| 41 | 41 | q_099a18d1ed60 | dev_013 | Nho xem 8/2025 doanh thu tiem minh duoc bao nhieu? | number | 466616500 |
| 42 | 42 | q_09e3efc66c68 | dev_001 | T5/2025 trung bình mỗi đơn giá trị bnhiu? | number | 169866 |
| 43 | 43 | q_0a3308eb76bb | dev_011 | Hom qua tiem ban dc may don? | number | 10 |
| 44 | 44 | q_0a9819711554 | dev_006 | Le Son Trung T7/2026: no them bnhiu tien, tra no bnhiu? | abstain | "No eligible candidate or unsupported evidence" |
| 45 | 45 | q_0ac818a0f5f2 | dev_002 | T2/2026 don huy chiem bao nhieu phan tram? | percent | 2.15 |
| 46 | 46 | q_0acdf488056e | dev_007 | Thang 4 nam 2026 so thang truoc tien ban len hay xuong bao nhieu % z? | percent | -6.28 |
| 47 | 47 | q_0ae9b7cf7003 | dev_010 | Cho tôi biết doanh thu các đơn của khách nhóm sỉ tháng 4/2026. | number | 41959500 |
| 48 | 48 | q_0b0081426a17 | dev_009 | Riêng hôm nay cửa hàng có doanh thu bao nhiêu? | number | 12823000 |
| 49 | 49 | q_0b78b53675c1 | dev_006 | Tết âm lịch 2026 sp nào bán chạy nhất? | string | "Berberin 100mg Mekophar chai 100 viên" |
| 50 | 50 | q_0b854610073b | dev_014 | 9/2025 cộng các khoản tiền thu máy cũ trừ vào đơn, hết nhiêu tiền z? | number | 36300000 |

[Cấu hình và nguồn kết quả](PUBLIC_TEST_LAST20_RUN.md). Số prediction không phải số câu đúng. Các nhãn abstain/unanswerable, nếu có, được giữ nguyên từ solver.
