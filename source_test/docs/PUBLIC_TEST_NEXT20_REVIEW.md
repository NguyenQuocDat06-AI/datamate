# Đối chiếu 20 đáp án tiếp theo — câu 11 đến 30

**Kết quả: 14 khớp phép tính độc lập, 4 sai giá trị, 2 abstain.** Không có gold public; đây là kiểm tra tại máy, chưa phải kết quả từ bộ chấm chính thức.

Mẫu gồm 20 checkpoint hoàn thành tiếp theo sau nhóm 10 câu đầu, không phải câu 11–30 theo thứ tự questions.jsonl. Các prediction thuộc bản code/cache cũ; tiến trình cũ được dừng ở 31 checkpoint để thực hiện sửa chuẩn hoá.

- [20 prediction gốc JSONL](../result/public_test_next20.jsonl).
- [Kết quả, SQL, số liệu đối chiếu và hash nguồn](../artifacts/public_next20/review.json).
- [Script audit đọc nguồn gốc](../scripts/review_public_next20.py).

| # | ID | Câu hỏi nguyên văn | Model | Đối chiếu | Kết luận |
|---|---|---|---|---|---|
| 11 | q_03bbdfa6e0d5 | Hom ni quan duoc may don roi? | 68 | 68 | Khớp |
| 12 | q_037178b58fcc | Từ đầu năm tới hnay tiệm có bnhiu đơn hàng rồi? | 6134 | 6134 | Khớp |
| 13 | q_0389b0c41da6 | Dịp Tết âm lịch 2026 có doanh thu hơn hay kém 17 ngày trước Tết bao nhiêu %? | -16.02 | -16.02 | Khớp |
| 14 | q_049f0a34efdc | 02/2026 có mấy khách mua từ 4 đơn trở lên z? | 197 | 197 | Khớp |
| 15 | q_03bee605bf7f | Tổng tiền bán tháng 10/25? | 281304400 | 281304400 | Khớp |
| 16 | q_040d437fdf09 | trung binh moi don shop mik th1/2025 gia tri dc bnhiu z nhi | 195008 | 70324 | Sai |
| 17 | q_04e55a8f8c0c | 6-2025 trung binh 1 don dem ve bao nhieu doanh thu? | 49878 | 69630 | Sai |
| 18 | q_052834325528 | 10/25 doanh thu ca chieu hon hay kem ca sang may %? | 54.36 | 54.36 | Khớp |
| 19 | q_0537f6e4f5ab | So sánh 3/25, 4/25 và 5/25 xem tháng nào cửa hàng bán đc nhiều tiền nhất | "2025-05" | "2025-05" | Khớp |
| 20 | q_05ab1c71dcaa | Ai là khách có số đơn mua nhiều nhất trong tháng 4 năm 2025? | "kl" | "La Minh Dương" | Sai |
| 21 | q_05421814d0e3 | Thuốc kê đơn th này bán được bnhiu tiền gồi? | 2713000 | 2826100 | Sai |
| 22 | q_06b7f437fcc4 | Tho Dam Lan An thang nay lam duoc tong bao nhieu tien roi? | abstain | 25180500 | Bỏ trả lời |
| 23 | q_0641a621b596 | Kể giúp dthu 3 tháng liền từ T4/2025 tới T6/2025, theo thứ tự thời gian. | [233818700, 248568700, 237185200] | [233818700, 248568700, 237185200] | Khớp |
| 24 | q_06de4545fe25 | T1/2026 có bao nhiêu mặt hàng có mặt trong ít nhất 187 đơn vậy? | 14 | 14 | Khớp |
| 25 | q_07b7be37628b | Tháng này tới hôm nay, doanh thu so với cùng kỳ tháng trước thay đổi bao nhiêu phần trăm? | -8.16 | -8.16 | Khớp |
| 26 | q_06dfe652d857 | Lãi ròng của quán trong 10/2025 là bao nhiêu? | abstain | -38326476 | Bỏ trả lời |
| 27 | q_0878814b6a76 | Thang nay nhap cua ncc nao nhieu tien nhat z? | "Cty Trương Hoa" | "Cty Trương Hoa" | Khớp |
| 28 | q_08b923f3c5c5 | Xem giúp mình tháng 12/2025, nhà cung cấp chính của cửa hàng là ai? | "Cty Mai Hải" | "Cty Mai Hải" | Khớp |
| 29 | q_08f075b54698 | Từ đầu năm tới hết hnay tiệm có tổng cộng bnhiu đh rồi? | 14783 | 14783 | Khớp |
| 30 | q_08c521dda3d9 | Cả quý 4/2025 cửa hàng bán được bao nhiêu tiền? | 1974947000 | 1974947000 | Khớp |

## Chi tiết và bằng chứng

### 11. q_03bbdfa6e0d5

Hom ni quan duoc may don roi?

Doanh nghiệp `dev_002`, as_of_date `2025-09-09`.

**Khớp:** Khớp phép tính độc lập trên dữ liệu gốc và rulebook; không có gold chính thức.

Prediction gốc:

```json
{"answer": 68, "id": "q_03bbdfa6e0d5", "type": "number"}
```

[Trace](../artifacts/public_50_trammay/traces/0c8228118f2522d3c1cd7415.json).

```sql
SELECT COUNT(DISTINCT ma_don_hang) AS order_count FROM banhang_t9_2025_donhang_chitiet_90560cdb WHERE ngay_dat_hang >= ? AND ngay_dat_hang < ? AND trang_thai_don_hang NOT IN ('cancelled', 'returned')
```

Parameters: `["2025-09-09", "2025-09-10"]`.

### 12. q_037178b58fcc

Từ đầu năm tới hnay tiệm có bnhiu đơn hàng rồi?

Doanh nghiệp `dev_005`, as_of_date `2026-03-16`.

**Khớp:** Khớp phép tính độc lập trên dữ liệu gốc và rulebook; không có gold chính thức.

Prediction gốc:

```json
{"answer": 6134, "id": "q_037178b58fcc", "type": "number"}
```

[Trace](../artifacts/public_50_trammay/traces/78ca52da2f4978ff35e132cc.json).

```sql
WITH all_orders AS (
  SELECT ma_don_hang, ngay_dat_hang, trang_thai_don_hang
  FROM banhang_thang01_2026_donhang_chitiet_19c342a4
  UNION ALL
  SELECT ma_don_hang, ngay_dat_hang, trang_thai_don_hang
  FROM banhang_thang02_2026_donhang_chitiet_f93ef92c
  UNION ALL
  SELECT ma_don_hang, ngay_dat_hang, trang_thai_don_hang
  FROM banhang_thang03_2026_donhang_chitiet_ff405111
)
SELECT COUNT(DISTINCT ma_don_hang) AS order_count
FROM all_orders
WHERE ngay_dat_hang >= ?
  AND ngay_dat_hang < ?
  AND trang_thai_don_hang NOT IN ('cancelled', 'returned')
```

Parameters: `["2026-01-01", "2026-03-17"]`.

### 13. q_0389b0c41da6

Dịp Tết âm lịch 2026 có doanh thu hơn hay kém 17 ngày trước Tết bao nhiêu %?

Doanh nghiệp `dev_006`, as_of_date `2026-08-08`.

**Khớp:** Đã đối chiếu calendar.csv: 23 tháng Chạp là 10/02/2026; mùng 10 tháng Giêng là 26/02/2026. Tết gồm 17 ngày; kỳ trước là 24/01–09/02. Tính tiền từ POS gốc, không lấy lịch bên ngoài.

Prediction gốc:

```json
{"answer": -16.02, "id": "q_0389b0c41da6", "type": "percent"}
```

[Trace](../artifacts/public_50_trammay/traces/d402efacd1420f7ee7dea39a.json).

```sql
WITH pre_tet AS (
  SELECT COALESCE(SUM(c.trigia), 0) AS rev
  FROM pos_don_hang_9fa5d9db d
  JOIN pos_chi_tiet_don_78baf42d c
    ON d.idcuahang = c.idcuahang AND d.mahoadon = c.mahoadon
  WHERE d.idcuahang = ?
    AND d.tinhtrang NOT IN ('cancelled', 'returned')
    AND d.ngaylap >= ? AND d.ngaylap < ?
),
tet AS (
  SELECT COALESCE(SUM(c.trigia), 0) AS rev
  FROM pos_don_hang_9fa5d9db d
  JOIN pos_chi_tiet_don_78baf42d c
    ON d.idcuahang = c.idcuahang AND d.mahoadon = c.mahoadon
  WHERE d.idcuahang = ?
    AND d.tinhtrang NOT IN ('cancelled', 'returned')
    AND d.ngaylap >= ? AND d.ngaylap < ?
)
SELECT
  ROUND((tet.rev - pre_tet.rev) * 100.0 / pre_tet.rev, 2) AS pct_change
FROM tet, pre_tet
```

Parameters: `["dev_006", "2026-01-24", "2026-02-10", "dev_006", "2026-02-10", "2026-02-27"]`.

### 14. q_049f0a34efdc

02/2026 có mấy khách mua từ 4 đơn trở lên z?

Doanh nghiệp `dev_006`, as_of_date `2026-08-08`.

**Khớp:** Khớp phép tính độc lập trên dữ liệu gốc và rulebook; không có gold chính thức.

Prediction gốc:

```json
{"answer": 197, "id": "q_049f0a34efdc", "type": "number"}
```

[Trace](../artifacts/public_50_trammay/traces/c9f4b72731055ea631ca76b0.json).

```sql
SELECT COUNT(*) FROM (SELECT makhachhang FROM pos_don_hang_9fa5d9db WHERE ngaylap >= ? AND ngaylap < ? AND tinhtrang NOT IN ('cancelled', 'returned') AND makhachhang IS NOT NULL AND makhachhang != '' GROUP BY makhachhang HAVING COUNT(DISTINCT mahoadon) >= ?)
```

Parameters: `["2026-02-01", "2026-03-01", 4]`.

### 15. q_03bee605bf7f

Tổng tiền bán tháng 10/25?

Doanh nghiệp `dev_005`, as_of_date `2026-05-10`.

**Khớp:** Khớp phép tính độc lập trên dữ liệu gốc và rulebook; không có gold chính thức.

Prediction gốc:

```json
{"answer": 281304400, "id": "q_03bee605bf7f", "type": "number"}
```

[Trace](../artifacts/public_50_trammay/traces/d4da24f459f02410afbf63b2.json).

```sql
SELECT CAST(ROUND(COALESCE(SUM(tong_tien), 0)) AS INTEGER) AS sales_revenue FROM (SELECT DISTINCT ma_don_hang, ngay_dat_hang, trang_thai_don_hang, ten_khach_hang, ten_san_pham, so_luong_san_pham, gia_san_pham, tong_tien, don_vi_tinh, derived_qty_base FROM banhang_thang10_2025_donhang_chitiet_a2ca19b0 WHERE trang_thai_don_hang NOT IN ('cancelled', 'returned') AND ngay_dat_hang >= ? AND ngay_dat_hang < ?) AS sub
```

Parameters: `["2025-10-01", "2025-11-01"]`.

### 16. q_040d437fdf09

trung binh moi don shop mik th1/2025 gia tri dc bnhiu z nhi

Doanh nghiệp `dev_002`, as_of_date `2026-05-24`.

**Sai:** Sai do dữ liệu tiền/dòng tổng trong cache cũ. Đọc lại dòng hàng gốc, loại huỷ/trả rồi chia số đơn: 70.324 đồng/đơn.

Prediction gốc:

```json
{"answer": 195008, "id": "q_040d437fdf09", "type": "number"}
```

[Trace](../artifacts/public_50_trammay/traces/fdb85eaa95129bdd58bb0b0e.json).

```sql
WITH deduped_lines AS (
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
    FROM banhang_t1_2025_donhang_chitiet_c6c0db60
    WHERE ngay_dat_hang >= ? AND ngay_dat_hang < ?
      AND ma_don_hang IS NOT NULL
),
valid_orders AS (
    SELECT
        ma_don_hang,
        SUM(COALESCE(tong_tien, 0)) AS order_total
    FROM deduped_lines
    WHERE NOT EXISTS (
        SELECT 1
        FROM deduped_lines d2
        WHERE d2.ma_don_hang = deduped_lines.ma_don_hang
          AND LOWER(TRIM(d2.trang_thai_don_hang)) IN ('cancelled', 'returned')
    )
    GROUP BY ma_don_hang
)
SELECT CAST(ROUND(AVG(order_total)) AS INTEGER) AS trung_binh_don
FROM valid_orders;
```

Parameters: `["2025-01-01", "2025-02-01"]`.

### 17. q_04e55a8f8c0c

6-2025 trung binh 1 don dem ve bao nhieu doanh thu?

Doanh nghiệp `dev_002`, as_of_date `2026-05-24`.

**Sai:** SQL đã tránh dòng tên sản phẩm trống, nhưng tiền trong cache cũ vẫn bị đọc sai. Tính trên nguồn gốc: 69.630 đồng/đơn.

Prediction gốc:

```json
{"answer": 49878, "id": "q_04e55a8f8c0c", "type": "number"}
```

[Trace](../artifacts/public_50_trammay/traces/67745826fe8b6776b7f1f20f.json).

```sql
WITH de_duped AS (
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
    FROM banhang_t6_2025_donhang_chitiet_02a692e8
    WHERE ma_don_hang IS NOT NULL
      AND ngay_dat_hang >= ?
      AND ngay_dat_hang < ?
),
eligible_orders AS (
    SELECT DISTINCT ma_don_hang
    FROM de_duped
    WHERE ma_don_hang NOT IN (
        SELECT DISTINCT ma_don_hang
        FROM de_duped
        WHERE LOWER(TRIM(trang_thai_don_hang)) IN ('cancelled', 'returned')
    )
),
order_revenue AS (
    SELECT
        eo.ma_don_hang,
        COALESCE(SUM(CASE WHEN d.ten_san_pham IS NOT NULL THEN d.tong_tien ELSE 0 END), 0) AS don_doanh_thu
    FROM eligible_orders eo
    JOIN de_duped d ON eo.ma_don_hang = d.ma_don_hang
    GROUP BY eo.ma_don_hang
)
SELECT CAST(ROUND(AVG(don_doanh_thu)) AS INTEGER)
FROM order_revenue;
```

Parameters: `["2025-06-01", "2025-07-01"]`.

### 18. q_052834325528

10/25 doanh thu ca chieu hon hay kem ca sang may %?

Doanh nghiệp `dev_006`, as_of_date `2026-08-08`.

**Khớp:** Khớp phép tính độc lập trên dữ liệu gốc và rulebook; không có gold chính thức.

Prediction gốc:

```json
{"answer": 54.36, "id": "q_052834325528", "type": "percent"}
```

[Trace](../artifacts/public_50_trammay/traces/640e859bdbf5f61f521566e3.json).

```sql
WITH shift_rev AS (
  SELECT
    SUM(CASE WHEN time(dh.thoigianlap) >= '06:00:00' AND time(dh.thoigianlap) < '14:00:00' THEN ctd.trigia ELSE 0 END) AS rev_sang,
    SUM(CASE WHEN time(dh.thoigianlap) >= '14:00:00' AND time(dh.thoigianlap) < '22:00:00' THEN ctd.trigia ELSE 0 END) AS rev_chieu
  FROM pos_don_hang_9fa5d9db dh
  JOIN pos_chi_tiet_don_78baf42d ctd
    ON dh.idcuahang = ctd.idcuahang AND dh.mahoadon = ctd.mahoadon
  WHERE dh.ngaylap >= ? AND dh.ngaylap < ?
    AND dh.tinhtrang NOT IN ('cancelled', 'returned')
)
SELECT
  ROUND((rev_chieu - rev_sang) * 100.0 / rev_sang, 2) AS pct_diff
FROM shift_rev
```

Parameters: `["2025-10-01", "2025-11-01"]`.

### 19. q_0537f6e4f5ab

So sánh 3/25, 4/25 và 5/25 xem tháng nào cửa hàng bán đc nhiều tiền nhất

Doanh nghiệp `dev_001`, as_of_date `2026-07-08`.

**Khớp:** Khớp phép tính độc lập trên dữ liệu gốc và rulebook; không có gold chính thức.

Prediction gốc:

```json
{"answer": "2025-05", "id": "q_0537f6e4f5ab", "type": "date"}
```

[Trace](../artifacts/public_50_trammay/traces/49da5762dcb3eadd1eabceae.json).

```sql
WITH deduplicated AS (
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
SELECT substr(ngay, 1, 7) AS thang
FROM deduplicated
WHERE ma_don_hang NOT IN (
    SELECT DISTINCT ma_don_hang
    FROM deduplicated
    WHERE trang_thai IN ('cancelled', 'returned')
      AND ma_don_hang IS NOT NULL
)
  AND ngay >= ?
  AND ngay < ?
GROUP BY substr(ngay, 1, 7)
ORDER BY SUM(thanh_tien) DESC, thang ASC
LIMIT 1
```

Parameters: `["2025-03-01", "2025-06-01"]`.

### 20. q_05ab1c71dcaa

Ai là khách có số đơn mua nhiều nhất trong tháng 4 năm 2025?

Doanh nghiệp `dev_001`, as_of_date `2026-07-08`.

**Sai:** kl là cách ghi khách lẻ, bị sót trong danh sách loại trừ của SQL. Khách lẻ không được xếp hạng. Đối chiếu tên/cách viết cùng đơn: La Minh Dương đứng đầu với 9 đơn hợp lệ.

Prediction gốc:

```json
{"answer": "kl", "id": "q_05ab1c71dcaa", "type": "string"}
```

[Trace](../artifacts/public_50_trammay/traces/64e8d56d38d26a5d8ef24aef.json).

```sql
SELECT TRIM(khach_hang) AS khach_hang FROM so_chi_tiet_ban_hang_sheet1_0db82db0 WHERE ngay >= ? AND ngay < ? AND trang_thai NOT IN ('cancelled', 'returned') AND khach_hang IS NOT NULL AND TRIM(khach_hang) != '' AND lookup(khach_hang) NOT IN ('khach le', 'le', 'khach vang lai', 'vang lai') GROUP BY TRIM(khach_hang) ORDER BY COUNT(DISTINCT ma_don_hang) DESC, TRIM(khach_hang) ASC LIMIT 1
```

Parameters: `["2025-04-01", "2025-05-01"]`.

### 21. q_05421814d0e3

Thuốc kê đơn th này bán được bnhiu tiền gồi?

Doanh nghiệp `dev_005`, as_of_date `2026-05-10`.

**Sai:** Nối tên bằng TRIM còn bỏ sót cách viết tên. Giữ nguyên SQL khác, chỉ đổi điều kiện nối sang lookup cho kết quả 2.826.100. Thay riêng mốc cuối kỳ không làm thay đổi kết quả; không xác nhận lỗi ngày ở câu này.

Prediction gốc:

```json
{"answer": 2713000, "id": "q_05421814d0e3", "type": "number"}
```

[Trace](../artifacts/public_50_trammay/traces/996fa7e79c33362e3ab20a76.json).

```sql
WITH deduplicated_banhang AS (
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
    FROM banhang_thang05_2026_donhang_chitiet_14397a8a
),
prescription_drugs AS (
    SELECT DISTINCT sp.ten_san_pham
    FROM danhsachsanpham_sheet1_3c4df1a4 sp
    JOIN danhsachthuoc_sheet1_faee7ae0 t ON sp.ma_sku = t.ma_sku
    WHERE t.thuoc_ke_don = 1 OR UPPER(CAST(t.thuoc_ke_don AS TEXT)) IN ('TRUE', '1')
)
SELECT COALESCE(SUM(b.tong_tien), 0) AS revenue
FROM deduplicated_banhang b
JOIN prescription_drugs pd ON TRIM(b.ten_san_pham) = TRIM(pd.ten_san_pham)
WHERE b.ngay_dat_hang >= ?
  AND b.ngay_dat_hang <= ?
  AND LOWER(TRIM(b.trang_thai_don_hang)) NOT IN ('cancelled', 'returned')
  AND NOT EXISTS (
      SELECT 1
      FROM donhang_lo_sheet1_0e28e3b5 l
      WHERE l.ma_don_hang = b.ma_don_hang
        AND l.so_luong_tra > 0
  )
```

Parameters: `["2026-05-01", "2026-05-10"]`.

### 22. q_06b7f437fcc4

Tho Dam Lan An thang nay lam duoc tong bao nhieu tien roi?

Doanh nghiệp `dev_012`, as_of_date `2026-08-31`.

**Bỏ trả lời:** Đối chiếu đã sửa cách nhận alias: An, a An và A.An đều thuộc cùng thợ. Tổng đúng là 25.180.500, gồm 9.402.500 từ A.An trước đây bị bộ audit bỏ sót. Đây là tiền phiếu, không phải hoa hồng. Prediction baseline vẫn là abstain.

Prediction gốc:

```json
{"id": "q_06b7f437fcc4", "reason": "No eligible candidate or unsupported evidence", "type": "abstain"}
```

[Trace](../artifacts/public_50_trammay/traces/7320808d501e14f3abe89995.json).

### 23. q_0641a621b596

Kể giúp dthu 3 tháng liền từ T4/2025 tới T6/2025, theo thứ tự thời gian.

Doanh nghiệp `dev_006`, as_of_date `2026-08-08`.

**Khớp:** Khớp phép tính độc lập trên dữ liệu gốc và rulebook; không có gold chính thức.

Prediction gốc:

```json
{"answer": [233818700, 248568700, 237185200], "id": "q_0641a621b596", "type": "list"}
```

[Trace](../artifacts/public_50_trammay/traces/ae8c65afb43ffa127e269c7b.json).

```sql
SELECT SUM(ct.trigia) AS revenue FROM pos_don_hang_9fa5d9db dh JOIN pos_chi_tiet_don_78baf42d ct ON dh.idcuahang = ct.idcuahang AND dh.mahoadon = ct.mahoadon WHERE dh.ngaylap >= ? AND dh.ngaylap < ? AND dh.tinhtrang NOT IN ('cancelled', 'returned') GROUP BY strftime('%Y-%m', dh.ngaylap) ORDER BY strftime('%Y-%m', dh.ngaylap) ASC
```

Parameters: `["2025-04-01", "2025-07-01"]`.

### 24. q_06de4545fe25

T1/2026 có bao nhiêu mặt hàng có mặt trong ít nhất 187 đơn vậy?

Doanh nghiệp `dev_008`, as_of_date `2026-07-22`.

**Khớp:** Đã đọc cả hai file chi tiết hoá đơn; đếm số đơn hợp lệ khác nhau theo mã hàng, bỏ Phí phục vụ.

Prediction gốc:

```json
{"answer": 14, "id": "q_06de4545fe25", "type": "number"}
```

[Trace](../artifacts/public_50_trammay/traces/7d762bf0c8c59141c1119e4d.json).

```sql
WITH combined_orders AS (
    SELECT bill, ma_hang, ten_hang, tt_don, ngay_xuat
    FROM chitiethoadon_1_sheet1_e161119e
    UNION ALL
    SELECT bill, ma_hang, ten_hang, tt_don, ngay_xuat
    FROM chitiethoadon_2_sheet1_0abc7c95
)
SELECT COUNT(*) AS count
FROM (
    SELECT ma_hang
    FROM combined_orders
    WHERE ngay_xuat >= ?
      AND ngay_xuat < ?
      AND tt_don NOT IN ('cancelled', 'returned')
      AND ma_hang IS NOT NULL
      AND lookup(coalesce(ten_hang, '')) NOT LIKE '%phi phuc vu%'
    GROUP BY ma_hang
    HAVING COUNT(DISTINCT bill) >= ?
) sub
```

Parameters: `["2026-01-01", "2026-02-01", 187]`.

### 25. q_07b7be37628b

Tháng này tới hôm nay, doanh thu so với cùng kỳ tháng trước thay đổi bao nhiêu phần trăm?

Doanh nghiệp `dev_013`, as_of_date `2026-06-30`.

**Khớp:** Khớp phép tính độc lập trên dữ liệu gốc và rulebook; không có gold chính thức.

Prediction gốc:

```json
{"answer": -8.16, "id": "q_07b7be37628b", "type": "percent"}
```

[Trace](../artifacts/public_50_trammay/traces/4cf848e0d47072af6af19a1c.json).

```sql
WITH deduped AS (
    SELECT DISTINCT
        't05' AS part,
        hd,
        ngay,
        ten_hang,
        dvt,
        sl,
        dg,
        tt,
        ghi_chu,
        khach,
        kenh
    FROM banhang_2026_t05_2026_e71f6818
    UNION ALL
    SELECT DISTINCT
        't06' AS part,
        hd,
        ngay,
        ten_hang,
        dvt,
        sl,
        dg,
        tt,
        ghi_chu,
        khach,
        kenh
    FROM banhang_2026_t06_2026_3deef732
),
order_meta AS (
    SELECT
        part,
        hd,
        MAX(ngay) AS order_date,
        MAX(
            CASE
                WHEN ghi_chu IS NOT NULL AND (
                    lookup(ghi_chu) LIKE '%huy%'
                    OR lookup(ghi_chu) LIKE '%cancel%'
                    OR lookup(ghi_chu) LIKE '%tra%'
                ) THEN 1
                ELSE 0
            END
        ) AS is_cancelled
    FROM deduped
    GROUP BY part, hd
),
eligible_lines AS (
    SELECT
        d.part,
        d.hd,
        m.order_date,
        d.tt
    FROM deduped d
    JOIN order_meta m ON d.part = m.part AND d.hd = m.hd
    WHERE m.is_cancelled = 0
      AND d.sl IS NOT NULL
      AND d.ten_hang NOT IN ('Voucher', 'Phí ship')
      AND d.ten_hang NOT LIKE 'Dùng % điểm'
),
period_rev AS (
    SELECT
        COALESCE(SUM(CASE WHEN order_date >= ? AND order_date <= ? THEN tt ELSE 0 END), 0) AS rev_target,
        COALESCE(SUM(CASE WHEN order_date >= ? AND order_date <= ? THEN tt ELSE 0 END), 0) AS rev_comp
    FROM eligible_lines
)
SELECT
    ROUND((CAST(rev_target AS REAL) - rev_comp) * 100.0 / rev_comp, 2)
FROM period_rev
```

Parameters: `["2026-06-01", "2026-06-30", "2026-05-01", "2026-05-30"]`.

### 26. q_06dfe652d857

Lãi ròng của quán trong 10/2025 là bao nhiêu?

Doanh nghiệp `dev_003`, as_of_date `2026-08-21`.

**Bỏ trả lời:** Tính lãi ròng tháng 10/2025: doanh thu 258.613.500 trừ giá vốn 76.347.976 trừ mọi chi phí 220.592.000 = −38.326.476 đồng. Solver bỏ trả lời sau lỗi schema probe và partition.

Prediction gốc:

```json
{"id": "q_06dfe652d857", "reason": "No eligible candidate or unsupported evidence", "type": "abstain"}
```

[Trace](../artifacts/public_50_trammay/traces/46fb8d534029d1ceea2ce4ac.json).

### 27. q_0878814b6a76

Thang nay nhap cua ncc nao nhieu tien nhat z?

Doanh nghiệp `dev_006`, as_of_date `2026-08-08`.

**Khớp:** Khớp phép tính độc lập trên dữ liệu gốc và rulebook; không có gold chính thức.

Prediction gốc:

```json
{"answer": "Cty Trương Hoa", "id": "q_0878814b6a76", "type": "string"}
```

[Trace](../artifacts/public_50_trammay/traces/49bcd8948574563e17479de2.json).

```sql
SELECT ncc.tengoi AS tengoi FROM pos_phieu_nhap_0a64aa2b pn JOIN pos_nha_cung_cap_d3bd8926 ncc ON pn.idcuahang = ncc.idcuahang AND pn.manhacungcap = ncc.manhacungcap WHERE pn.ngaynhapkho >= ? AND pn.ngaynhapkho < ? AND pn.tinhtrang != ? GROUP BY ncc.manhacungcap, ncc.tengoi ORDER BY SUM(pn.tonggiatrinhap) DESC, ncc.manhacungcap ASC LIMIT 1
```

Parameters: `["2026-08-01", "2026-08-09", "cancelled"]`.

### 28. q_08b923f3c5c5

Xem giúp mình tháng 12/2025, nhà cung cấp chính của cửa hàng là ai?

Doanh nghiệp `dev_003`, as_of_date `2026-08-21`.

**Khớp:** Nhà cung cấp chính xếp theo số phiếu nhập khác nhau, không theo tổng tiền; đã đọc sổ nhập gốc và gộp các cách viết tên.

Prediction gốc:

```json
{"answer": "Cty Mai Hải", "id": "q_08b923f3c5c5", "type": "string"}
```

[Trace](../artifacts/public_50_trammay/traces/2b2265cbb0a1c6c1f1591236.json).

```sql
SELECT nha_cung_cap FROM sotay_nhaphang_sheet1_bdfff7c3 WHERE ngay_nhap >= ? AND ngay_nhap < ? AND (luu_y IS NULL OR LOWER(luu_y) NOT LIKE '%hủy%') AND nha_cung_cap IS NOT NULL GROUP BY nha_cung_cap ORDER BY COUNT(DISTINCT so_phieu) DESC, nha_cung_cap ASC LIMIT 1
```

Parameters: `["2025-12-01", "2026-01-01"]`.

### 29. q_08f075b54698

Từ đầu năm tới hết hnay tiệm có tổng cộng bnhiu đh rồi?

Doanh nghiệp `dev_001`, as_of_date `2025-09-12`.

**Khớp:** Khớp phép tính độc lập trên dữ liệu gốc và rulebook; không có gold chính thức.

Prediction gốc:

```json
{"answer": 14783, "id": "q_08f075b54698", "type": "number"}
```

[Trace](../artifacts/public_50_trammay/traces/6f6008e8bc0af6f52a68baf5.json).

```sql
SELECT COUNT(DISTINCT ma_don_hang) AS so_don FROM so_chi_tiet_ban_hang_sheet1_0db82db0 WHERE ngay >= ? AND ngay < ? AND trang_thai NOT IN ('cancelled', 'returned')
```

Parameters: `["2025-01-01", "2025-09-13"]`.

### 30. q_08c521dda3d9

Cả quý 4/2025 cửa hàng bán được bao nhiêu tiền?

Doanh nghiệp `dev_010`, as_of_date `2026-09-06`.

**Khớp:** Khớp phép tính độc lập trên dữ liệu gốc và rulebook; không có gold chính thức.

Prediction gốc:

```json
{"answer": 1974947000, "id": "q_08c521dda3d9", "type": "number"}
```

[Trace](../artifacts/public_50_trammay/traces/1155b1c5d8a7591159f73924.json).

```sql
SELECT CAST(ROUND(SUM(c.cong)) AS INTEGER)
FROM don_hang_don_hang_f6d8fd1f d
JOIN chi_tiet_don_chi_tiet_don_bf1de06c c ON d.so_ct = c.ma_bill
WHERE d.date >= ? AND d.date < ?
  AND d.xu_ly NOT IN ('cancelled', 'returned')
```

Parameters: `["2025-10-01", "2026-01-01"]`.

## Phạm vi kiểm tra

Đọc trực tiếp Excel/CSV/SQLite nguồn, dùng logic audit riêng cho câu hỏi, không dùng SQL model để tạo giá trị đối chiếu. Phép thử chỉ thay điều kiện nối tên của câu thuốc là bước tìm nguyên nhân sau khi đã tính trên nguồn. Tiền và phần trăm được làm tròn theo rulebook; đơn huỷ/trả bị loại cả đơn; danh sách giữ thứ tự; thợ/sổ không có mã phiếu giữ các lượt ghi như dữ liệu.

Đối chiếu tên khách dùng các cách viết cùng xuất hiện trong một đơn để gộp identity; khách lẻ/kl bị loại. Việc này là audit có phạm vi, không phải bộ chấm tổng quát đã được xác thực. Các câu hoàn thành sớm không phải mẫu ngẫu nhiên.

Gộp với 10 câu đầu: 20 câu khớp, 6 câu sai giá trị, 4 câu abstain trên 30 câu đã đối chiếu. Không suy ra PA chính thức hoặc độ chính xác toàn bộ public từ mẫu này.
