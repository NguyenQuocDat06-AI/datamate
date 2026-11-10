# Đối chiếu 10 đáp án public test đầu tiên đã hoàn thành

Ngày kiểm tra: 03/11/2026. Lấy 10 checkpoint hoàn thành sớm nhất, có một checkpoint tái sử dụng từ lần chạy trước. Đây không nhất thiết là 10 câu đứng đầu questions.jsonl; danh sách ID đã được cố định trong [selection.json](../artifacts/public_first10/selection.json).

**Kết quả đối chiếu độc lập: 6 câu khớp, 2 câu sai giá trị, 2 câu abstain.** Public test không phát hành đáp án chuẩn: các kết luận này dựa trên đọc file gốc và rulebook, chưa phải xác nhận từ bộ chấm chính thức; không báo PA chính thức.

- [10 prediction gốc, giữ nguyên giá trị](../result/public_test_first10.jsonl).
- [Câu hỏi, prediction, kết luận và SQL](../artifacts/public_first10/review.json).
- [Phép tính trên nguồn gốc, dòng đóng góp, schema và SHA-256](../artifacts/public_first10/raw_audit.json).
- [Code kiểm tra độc lập](../scripts/review_public_first10.py), [code dựng báo cáo](../scripts/write_first10_review.py).

## Bảng kết quả

Tiền trong bảng tính bằng VND. Khớp là khớp phép tính kiểm tra độc lập; abstain là bỏ trả lời, không tự đổi thành một đáp án sai.

| # | ID | Câu hỏi | Model trả | Kiểm tra độc lập | Kết luận |
|---|---|---|---|---|---|
| 1 | q_00bf8df3ea0b | Rieng ngay hom nay doanh thu quan la bao nhieu? | 7893391 | 4533000 | Sai |
| 2 | q_00c4e24fa228 | Nhóm đồ dùng tháng 6/2026 bán được bao nhiêu tiền? | 16226000 | 16226000 | Khớp |
| 3 | q_008fdebb6ec9 | Tháng 11 năm 2025, nhóm trà có mặt hàng nào bán được nhiều ly nhất? | abstain | unanswerable | Bỏ trả lời |
| 4 | q_014cdcc1da6e | 8/26 tiền bán ca chiều là bnhiu? | 459334000 | 459334000 | Khớp |
| 5 | q_013b4ff35811 | Cho tôi biết doanh thu tuần này. | 230034300 | 230034300 | Khớp |
| 6 | q_01ac0fc9f2a2 | Tuần rồi trung bình mổi đơn quán bán được nhiêu tiền vậy? | abstain | 71724 | Bỏ trả lời |
| 7 | q_01ba91ff94c6 | Tuần rồi 5 món bán chạy nhất là những món nàoo? | ["Lẩu Thái tôm cá (nhỏ)", "Canh chua cá lóc", "Bia Tiger Bạc lon", "Bia Sài Gòn Special lon", "Bia Tiger lon"] | ["Lẩu Thái tôm cá (nhỏ)", "Canh chua cá lóc", "Bia Tiger Bạc lon", "Bia Sài Gòn Special lon", "Cánh gà chiên nước mắm"] | Sai |
| 8 | q_0329c9711c55 | Bữa nay tiệm có bao nhiêu đơn rồi nè? | 108 | 108 | Khớp |
| 9 | q_033e37dde4d5 | Tuần này bán được tổng cộng bao nhiêu gói rồi ha? | 959 | 959 | Khớp |
| 10 | q_01d1fc600b26 | Doanh thu của Bia Heineken 330ml trong tháng 7 năm 2025 là bao nhiêu? | 3142000 | 3142000 | Khớp |

## Chi tiết từng câu

### 1. q_00bf8df3ea0b

**Câu hỏi nguyên văn:** Rieng ngay hom nay doanh thu quan la bao nhieu?

Doanh nghiệp: `dev_002`. `as_of_date`: `2025-03-04`.

**Prediction gốc:**

```json
{"answer": 7893391, "id": "q_00bf8df3ea0b", "type": "number"}
```

**Kết luận: Sai.** Cộng thành tiền dòng hàng ngày 04/03/2025, loại cả đơn huỷ/trả, không cộng dòng tổng. Đọc Excel gốc: 112 dòng hợp lệ, 71 đơn, tổng 4.533.000 đồng. Trong cache có 33 ô tiền bị đọc nhỏ đi 1.000 lần, tổng thiếu 1.389.609 đồng; đồng thời dòng 813 có nhãn TỔNG NGÀY 04/03 ở cột Tên khách hàng bị kế thừa mã đơn/ngày/trạng thái, rồi cộng thêm 4.750.000 đồng. Vì vậy 4.533.000 − 1.389.609 + 4.750.000 = 7.893.391. Ví dụ dòng 696: Tổng tiền = 7.000đ, đúng là 7.000 đồng nhưng cache ghi 7. Đây là lỗi ingestion; SQL chạy thành công vẫn cho đáp án sai.

[Trace model](../artifacts/public_50_trammay/traces/c2b686613bb91220441f35d7.json).

SQL của candidate đã thực thi (giữ nguyên):

```sql
SELECT CAST(ROUND(COALESCE(SUM(tong_tien), 0)) AS INTEGER) AS doanh_thu
FROM (
  SELECT DISTINCT
    ma_don_hang,
    ngay_dat_hang,
    trang_thai_don_hang,
    ten_khach_hang,
    ten_san_pham,
    so_luong_san_pham,
    gia_san_pham,
    tong_tien,
    don_vi_tinh
  FROM banhang_t3_2025_donhang_chitiet_fab7cfd9
  WHERE ngay_dat_hang >= ? AND ngay_dat_hang < ?
)
WHERE trang_thai_don_hang NOT IN ('cancelled', 'returned')
  AND ma_don_hang NOT IN (
    SELECT ma_don_hang
    FROM banhang_t3_2025_donhang_chitiet_fab7cfd9
    WHERE trang_thai_don_hang IN ('cancelled', 'returned')
  )
```

Parameters: `["2025-03-04", "2025-03-05"]`.

### 2. q_00c4e24fa228

**Câu hỏi nguyên văn:** Nhóm đồ dùng tháng 6/2026 bán được bao nhiêu tiền?

Doanh nghiệp: `dev_013`. `as_of_date`: `2026-06-30`.

**Prediction gốc:**

```json
{"answer": 16226000, "id": "q_00c4e24fa228", "type": "number"}
```

**Kết luận: Khớp.** Đọc BanHang_2026.xlsx, sheet T06-2026 và QuanLy.xlsx, sheet DanhMuc. Nối tên/cách viết tên vào đúng nhóm đồ dùng, kế thừa ghi chú trong cùng hoá đơn, loại đơn huỷ. Cộng TT của 104 dòng hàng hợp lệ, không cộng Voucher, Dùng điểm hoặc Phí ship: 16.226.000 đồng.

[Trace model](../artifacts/public_50_trammay/traces/7c62f6c7c56328ed0205a4e9.json).

SQL của candidate đã thực thi (giữ nguyên):

```sql
WITH dedup_sales AS (
    SELECT DISTINCT hd, ngay, ten_hang, dvt, sl, dg, tt, khach, kenh, ghi_chu
    FROM banhang_2026_t06_2026_3deef732
),
cancelled_orders AS (
    SELECT DISTINCT hd
    FROM dedup_sales
    WHERE ghi_chu IS NOT NULL
      AND (
          lookup(ghi_chu) LIKE '%huy%'
          OR lookup(ghi_chu) LIKE '%cancel%'
          OR lookup(ghi_chu) LIKE '%tra%'
      )
)
SELECT COALESCE(SUM(s.tt), 0) AS doanh_thu
FROM dedup_sales s
JOIN quanly_danhmuc_86907684 d ON lookup(s.ten_hang) = lookup(d.ten_hang)
WHERE lookup(d.nhom) = lookup(?)
  AND s.ngay >= ?
  AND s.ngay < ?
  AND s.hd NOT IN (SELECT hd FROM cancelled_orders)
```

Parameters: `["đồ dùng", "2026-06-01", "2026-07-01"]`.

### 3. q_008fdebb6ec9

**Câu hỏi nguyên văn:** Tháng 11 năm 2025, nhóm trà có mặt hàng nào bán được nhiều ly nhất?

Doanh nghiệp: `dev_002`. `as_of_date`: `2026-05-24`.

**Prediction gốc:**

```json
{"id": "q_008fdebb6ec9", "reason": "No eligible candidate or unsupported evidence", "type": "abstain"}
```

**Kết luận: Bỏ trả lời.** Đã mở header toàn bộ 17 file bán hàng và DanhSachQuyDoi.xlsx: không có cột hoặc bảng nhóm hàng; bảng quy đổi chỉ có croissant và tiramisu. Theo rulebook §3 và §7, không được suy nhóm trà từ tên món. Kết luận kiểm tra độc lập là unanswerable. Model cũng đề xuất điều này trong plan, nhưng validator báo MISSING_PARTITION rồi UNSUPPORTED_NOT_ESTABLISHED, nên đầu ra cuối là abstain. Theo thể lệ, abstain nhận 0 điểm; cần phân biệt với một đáp án unanswerable được chấm đúng.

[Trace model](../artifacts/public_50_trammay/traces/9087b0e7a4cdf4664d479339.json).

### 4. q_014cdcc1da6e

**Câu hỏi nguyên văn:** 8/26 tiền bán ca chiều là bnhiu?

Doanh nghiệp: `dev_007`. `as_of_date`: `2026-09-19`.

**Prediction gốc:**

```json
{"answer": 459334000, "id": "q_014cdcc1da6e", "type": "number"}
```

**Kết luận: Khớp.** 8/26 là tháng 08/2026; ca chiều theo giờ tạo đơn từ 14:00 đến trước 22:00. Đọc chi_tiet_hoa_don.csv gốc, loại toàn bộ đơn huỷ/trả, cộng Thành tiền của 3.258 dòng thuộc 878 đơn hợp lệ: 459.334.000 đồng. Tiền bán là doanh thu, không lấy tiền mặt khách đã trả.

[Trace model](../artifacts/public_50_trammay/traces/9a5746b3c6518ecb88aa911f.json).

SQL của candidate đã thực thi (giữ nguyên):

```sql
SELECT COALESCE(SUM(thanh_tien), 0) AS revenue
FROM chi_tiet_hoa_don_chi_tiet_hoa_don_289db483
WHERE ngay_giao_dich >= ?
  AND ngay_giao_dich < ?
  AND strftime('%H:%M', gio_ban) >= ?
  AND strftime('%H:%M', gio_ban) <= ?
  AND tien_do NOT IN ('cancelled', 'returned')
  AND ma_bill NOT IN (
      SELECT ma_bill
      FROM chi_tiet_hoa_don_chi_tiet_hoa_don_289db483
      WHERE tien_do IN ('cancelled', 'returned')
        AND ma_bill IS NOT NULL
  )
```

Parameters: `["2026-08-01", "2026-09-01", "14:00", "21:59"]`.

### 5. q_013b4ff35811

**Câu hỏi nguyên văn:** Cho tôi biết doanh thu tuần này.

Doanh nghiệp: `dev_004`. `as_of_date`: `2025-11-16`.

**Prediction gốc:**

```json
{"answer": 230034300, "id": "q_013b4ff35811", "type": "number"}
```

**Kết luận: Khớp.** as_of_date là Chủ nhật 16/11/2025: tuần này từ 10/11 đến hết 16/11. Đọc tất cả sheet SoBanHang.xlsx để phát hiện cả dòng trả hàng về sau; đơn có SL bán âm bị loại cả đơn. Không giao được là trạng thái huỷ. Sau loại huỷ/trả: 389 dòng, 171 đơn, cộng Amount = 230.034.300 đồng. Không dùng Tổng phiếu lặp ở từng dòng.

[Trace model](../artifacts/public_50_trammay/traces/cde5a26dbcc75ab5c064a279.json).

SQL của candidate đã thực thi (giữ nguyên):

```sql
WITH deduplicated AS (
  SELECT DISTINCT *
  FROM sobanhang_t11_2025_f56b3d0b
),
excluded_orders AS (
  SELECT DISTINCT ma_don
  FROM deduplicated
  WHERE LOWER(TRIM(trang_thai_xu_ly)) IN ('cancelled', 'returned')
     OR sl_ban < 0
)
SELECT
  CAST(COALESCE(SUM(amount), 0) AS INTEGER) AS total_revenue
FROM deduplicated
WHERE thoi_gian >= ?
  AND thoi_gian < ?
  AND ma_don NOT IN (SELECT ma_don FROM excluded_orders WHERE ma_don IS NOT NULL)
```

Parameters: `["2025-11-10", "2025-11-17"]`.

### 6. q_01ac0fc9f2a2

**Câu hỏi nguyên văn:** Tuần rồi trung bình mổi đơn quán bán được nhiêu tiền vậy?

Doanh nghiệp: `dev_003`. `as_of_date`: `2026-08-18`.

**Prediction gốc:**

```json
{"id": "q_01ac0fc9f2a2", "reason": "No eligible candidate or unsupported evidence", "type": "abstain"}
```

**Kết luận: Bỏ trả lời.** as_of_date 18/08/2026: tuần rồi từ 10/08 đến hết 16/08/2026. Đọc hai file bán hàng 2025/2026, cộng thành tiền dòng của đơn không huỷ/trả, rồi chia số mã đơn khác nhau: 66.272.600 / 924 = 71.723,593… đồng; làm tròn nửa lên thành 71.724 đồng. Dữ liệu đủ để tính. Trace cho thấy SELECT DISTINCT * bị chặn vì đọc cả cột nội bộ __source_row; đầu ra cuối là abstain. Đây là trường hợp solver chưa trả được một câu có thể trả lời.

[Trace model](../artifacts/public_50_trammay/traces/a6f6f07f7111f85411761683.json).

SQL của candidate đã thực thi (giữ nguyên):

```sql
WITH combined AS (
    SELECT *
    FROM banhang_tonghop_2025_banhang_66ac2fa2
    UNION ALL
    SELECT *
    FROM banhang_tonghop_2026_banhang_149b4f8f
),
deduped AS (
    SELECT DISTINCT *
    FROM combined
),
cancelled_or_returned AS (
    SELECT DISTINCT don_so
    FROM deduped
    WHERE LOWER(TRIM(trang_thai_giao)) LIKE '%hủy%'
       OR LOWER(TRIM(trang_thai_giao)) LIKE '%huỷ%'
       OR LOWER(TRIM(trang_thai_giao)) LIKE '%trả%'
       OR lookup(trang_thai_giao) LIKE '%huy%'
       OR lookup(trang_thai_giao) LIKE '%tra%'
),
order_totals AS (
    SELECT
        d.don_so,
        SUM(CAST(REPLACE(REPLACE(TRIM(d.thanh_tien_d), ',', ''), '.', '') AS INTEGER)) AS don_rev
    FROM deduped d
    LEFT JOIN cancelled_or_returned c ON d.don_so = c.don_so
    WHERE c.don_so IS NULL
      AND DATE(d.gio_tao_don) >= ?
      AND DATE(d.gio_tao_don) < ?
    GROUP BY d.don_so
)
SELECT CAST(ROUND(AVG(don_rev)) AS INTEGER) AS trung_binh_don
FROM order_totals
```

Parameters: `["2026-08-10", "2026-08-17"]`.

### 7. q_01ba91ff94c6

**Câu hỏi nguyên văn:** Tuần rồi 5 món bán chạy nhất là những món nàoo?

Doanh nghiệp: `dev_008`. `as_of_date`: `2025-11-10`.

**Prediction gốc:**

```json
{"answer": ["Lẩu Thái tôm cá (nhỏ)", "Canh chua cá lóc", "Bia Tiger Bạc lon", "Bia Sài Gòn Special lon", "Bia Tiger lon"], "id": "q_01ba91ff94c6", "type": "list"}
```

**Kết luận: Sai.** Tuần rồi là 03–09/11/2025. Đã đọc cả hai file ChiTietHoaDon_1.xlsx và ChiTietHoaDon_2.xlsx, loại đơn huỷ/trả, bỏ Phí phục vụ, cộng SL lẻ theo Ma hang. Top theo số lượng cơ bản là SP0004: 130; SP0016: 119; SP0014: 118; SP0006: 114; SP0003: 112. SP0003 có ba cách viết hoa thường của Cánh gà chiên nước mắm. SQL của model GROUP BY ma_hang, ten_hang làm tách cùng một mặt hàng thành nhiều nhóm; vì vậy lấy Bia Tiger lon (SP0002, 108) vào vị trí thứ 5. Bốn vị trí đầu khớp; danh sách hoàn chỉnh sai vì chấm cả tập và thứ tự.

[Trace model](../artifacts/public_50_trammay/traces/fdc38d2e441f86aaa659d46d.json).

SQL của candidate đã thực thi (giữ nguyên):

```sql
SELECT ten_hang FROM chitiethoadon_1_sheet1_e161119e WHERE ngay_xuat >= ? AND ngay_xuat < ? AND tt_don NOT IN ('cancelled', 'returned') AND ten_hang IS NOT NULL AND lookup(ten_hang) != lookup('Phí phục vụ') GROUP BY ma_hang, ten_hang ORDER BY SUM(sl_le) DESC, ma_hang ASC LIMIT 5
```

Parameters: `["2025-11-03", "2025-11-10"]`.

### 8. q_0329c9711c55

**Câu hỏi nguyên văn:** Bữa nay tiệm có bao nhiêu đơn rồi nè?

Doanh nghiệp: `dev_009`. `as_of_date`: `2026-02-25`.

**Prediction gốc:**

```json
{"answer": 108, "id": "q_0329c9711c55", "type": "number"}
```

**Kết luận: Khớp.** Đọc danh_sach_hoa_don.csv gốc; lọc ngày 25/02/2026; bỏ trạng thái huỷ/trả, đếm Mã hóa đơn khác nhau: 108 đơn. README xác nhận mỗi dòng là một hoá đơn, không phải chi tiết mặt hàng.

[Trace model](../artifacts/public_50_trammay/traces/71da95dd5dfc8f71a93ce59c.json).

SQL của candidate đã thực thi (giữ nguyên):

```sql
SELECT COUNT(DISTINCT ma_hoa_don) AS order_count FROM danh_sach_hoa_don_danh_sach_hoa_don_028dc5dc WHERE ngay_thang >= ? AND ngay_thang < ? AND trang_thai_don NOT IN ('cancelled', 'returned');
```

Parameters: `["2026-02-25", "2026-02-26"]`.

### 9. q_033e37dde4d5

**Câu hỏi nguyên văn:** Tuần này bán được tổng cộng bao nhiêu gói rồi ha?

Doanh nghiệp: `dev_006`. `as_of_date`: `2026-06-25`.

**Prediction gốc:**

```json
{"answer": 959, "id": "q_033e37dde4d5", "type": "number"}
```

**Kết luận: Khớp.** Tuần này từ 22/06 đến hết 25/06/2026. Đọc pos.sqlite gốc ở chế độ read-only; nối don_hang, chi_tiet_don, san_pham bằng cả IDCuaHang và mã tương ứng. Loại đơn huỷ và mọi đơn có phiếu tra_hang; chỉ lấy sản phẩm có DonViCoSo = gói, cộng SLQuyDoi: 959 gói.

[Trace model](../artifacts/public_50_trammay/traces/b37db2beecdd42e7627d48be.json).

SQL của candidate đã thực thi (giữ nguyên):

```sql
SELECT COALESCE(SUM(ctd.slquydoi), 0) AS total_qty FROM pos_chi_tiet_don_78baf42d ctd JOIN pos_don_hang_9fa5d9db dh ON ctd.idcuahang = dh.idcuahang AND ctd.mahoadon = dh.mahoadon JOIN pos_san_pham_de560f86 sp ON ctd.idcuahang = sp.idcuahang AND ctd.mahanghoa = sp.mahanghoa WHERE dh.ngaylap >= ? AND dh.ngaylap < ? AND dh.tinhtrang NOT IN ('cancelled', 'returned') AND LOWER(TRIM(sp.donvicoso)) = ?
```

Parameters: `["2026-06-22", "2026-06-26", "gói"]`.

### 10. q_01d1fc600b26

**Câu hỏi nguyên văn:** Doanh thu của Bia Heineken 330ml trong tháng 7 năm 2025 là bao nhiêu?

Doanh nghiệp: `dev_001`. `as_of_date`: `2026-07-08`.

**Prediction gốc:**

```json
{"answer": 3142000, "id": "q_01d1fc600b26", "type": "number"}
```

**Kết luận: Khớp.** Đọc So_chi_tiet_ban_hang.xlsx gốc, lọc cả tháng 07/2025 và tên Bia Heineken 330ml sau chuẩn hoá khoảng trắng/hoa thường. Loại cả đơn huỷ/trả, gồm cả cách ghi Đã hoàn hàng và hàng trả về; cộng Thành tiền, không lấy số lượng quy đổi nhân giá hiện tại: 3.142.000 đồng.

[Trace model](../artifacts/public_50_trammay/traces/39d95799f5538588bdd1ca5d.json).

SQL của candidate đã thực thi (giữ nguyên):

```sql
WITH deduplicated_sales AS (
    SELECT DISTINCT
        ngay,
        ma_don_hang,
        ten_hang_hoa,
        don_vi_tinh,
        so_luong,
        don_gia,
        thanh_tien,
        trang_thai,
        so_luong_quy_doi,
        khach_hang
    FROM so_chi_tiet_ban_hang_sheet1_0db82db0
)
SELECT
    CAST(COALESCE(SUM(thanh_tien), 0) AS INTEGER) AS sales_revenue
FROM deduplicated_sales
WHERE ngay >= ?
  AND ngay < ?
  AND (ten_hang_hoa IN (?, ?) OR REPLACE(ten_hang_hoa, '  ', ' ') = ?)
  AND ma_don_hang NOT IN (
      SELECT ma_don_hang
      FROM so_chi_tiet_ban_hang_sheet1_0db82db0
      WHERE trang_thai IN ('cancelled', 'returned')
        AND ma_don_hang IS NOT NULL
  )
  AND trang_thai NOT IN ('cancelled', 'returned')
```

Parameters: `["2025-07-01", "2025-08-01", "Bia Heineken 330ml", "Bia  Heineken 330ml", "Bia Heineken 330ml"]`.

## Bằng chứng hai lỗi giá trị

**Doanh thu ngày:** [chênh lệch từng dòng với cache](../artifacts/public_first10/dev002_normalization_differences.json). Cột Tổng tiền trộn cách ghi `45k`, `101.000đ`, `50,500` và số trực tiếp. Một locale áp cho toàn cột đã đọc sai các giá trị có dấu chấm nghìn và ký hiệu đồng. Dòng tổng ngày nằm trong cột tên khách còn bị nhận là dòng giao dịch.

**Top 5:** bảng dưới cộng theo mã hàng, bao gồm các cách viết tên khác nhau của cùng mã.

| Hạng | Mã | Tên đại diện có trong dữ liệu | Số lượng cơ bản |
|---|---|---|---:|
| 1 | SP0004 | Lẩu Thái tôm cá (nhỏ) | 130 |
| 2 | SP0016 | Canh chua cá lóc | 119 |
| 3 | SP0014 | Bia Tiger Bạc lon | 118 |
| 4 | SP0006 | Bia Sài Gòn Special lon | 114 |
| 5 | SP0003 | Cánh gà chiên nước mắm | 112 |
| 6 | SP0002 | Bia Tiger lon | 108 |

## Phương pháp và giới hạn

Script audit đọc trực tiếp Excel bằng openpyxl, CSV bằng csv, SQLite gốc bằng kết nối read-only; không dùng parser, compiler, executor hoặc SQL của solver để tạo các giá trị đối chiếu. Việc so cache chỉ phục vụ tìm nguyên nhân sau khi đã tính trên nguồn gốc. Đã xử lý kế thừa trong nhóm đơn, dòng trùng, quy ước tiền/ngày, huỷ/trả toàn đơn, đơn vị cơ bản và thời gian theo as_of_date. Code audit dùng riêng cho 10 câu này, không phải bộ chấm tổng quát đã được xác thực.

Ở bước thăm dò, audit từng chưa nhận đủ nhãn huỷ/trả; kết quả trong báo cáo này đã được tính lại với các nhãn thực tế như Không giao được, Đã hoàn hàng và hàng trả về. Phần raw_status_counts cho phép rà soát lại cách diễn giải.

Không gọi API thêm khi kiểm tra. Prediction gốc và code solver của đợt 50 câu đang chạy được giữ nguyên; các giá trị đối chiếu chưa được ghi đè vào file prediction. Mẫu là các câu hoàn thành sớm, không ngẫu nhiên, nên không dùng 6/10 hoặc 6/8 để dự đoán điểm của toàn bộ 1.000 câu.

Muốn tái lập trong source_test:

```powershell
python -X utf8 -u -m scripts.review_public_first10
python -X utf8 -m scripts.write_first10_review
```
