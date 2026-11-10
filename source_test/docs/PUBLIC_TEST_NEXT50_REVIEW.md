# Đối chiếu 50 câu tiếp theo — vị trí 51–100

Cập nhật 20:23 ngày 03/11/2026, giờ Việt Nam.

Đã chạy inference mới qua Trammay bằng **16 luồng**, không nhập checkpoint từ 50 câu trước. Giữ nguyên solver trong lượt chạy. Giá trị đối chiếu tính độc lập từ dữ liệu gốc/rulebook, không dùng SQL hoặc cache chuẩn hóa của solver để tạo reference.

**45 khớp, 2 sai khác, 3 abstain.** Chưa phải gold/PA chính thức. Các sai khác cần kiểm tra thêm bằng nguồn và trace trước khi sửa solver.

| STT | ID | Business | Prediction | Đối chiếu độc lập | Kết luận |
|---|---|---|---|---|---|
| 51 | q_0bac8c88238d | dev_009 | 15582000 | 15535000 | Sai khác |
| 52 | q_0bee4ad8f965 | dev_008 | 496767 | 496767 | Khớp |
| 53 | q_0c0a1a780164 | dev_008 | 101066500 | 101066500 | Khớp |
| 54 | q_0c62d2078959 | dev_002 | 76 | 76 | Khớp |
| 55 | q_0ca19cd317cb | dev_006 | ["Nhiệt kế điện tử Microlife MT500", "Tiffy Dey hộp 25 vỉ x 4 viên", "Cao Sao Vàng TW3 hộp 3g", "Nước muối sinh lý Natri clorid 0,9% Vĩnh Phúc chai 500ml", "Siro ăn ngon Ích Nhi Plus chai 100ml", "Prednisolon 5mg Boston chai 500 viên", "Panadol Extra hộp 15 vỉ x 12 viên", "Fugacar 500mg hộp 1 viên", "Băng cá nhân Urgo Transparent hộp 100 miếng", "Kudos Vitamin C 1000mg viên sủi tuýp 20 viên"] | ["Nhiệt kế điện tử Microlife MT500", "Tiffy Dey hộp 25 vỉ x 4 viên", "Cao Sao Vàng TW3 hộp 3g", "Nước muối sinh lý Natri clorid 0,9% Vĩnh Phúc chai 500ml", "Siro ăn ngon Ích Nhi Plus chai 100ml", "Prednisolon 5mg Boston chai 500 viên", "Panadol Extra hộp 15 vỉ x 12 viên", "Fugacar 500mg hộp 1 viên", "Băng cá nhân Urgo Transparent hộp 100 miếng", "Kudos Vitamin C 1000mg viên sủi tuýp 20 viên"] | Khớp |
| 56 | q_0cb6a1a6f1ef | dev_003 | "Hứa Bảo Trung" | "Hứa Bảo Trung" | Khớp |
| 57 | q_0d23cc65cc7a | dev_014 | -12.82 | -12.82 | Khớp |
| 58 | q_0da15db21978 | dev_005 | 3.28 | 3.28 | Khớp |
| 59 | q_0dff1966b7c3 | dev_005 | abstain | 106712 | Abstain |
| 60 | q_0e1eff36bbc5 | dev_010 | 61.54 | 61.54 | Khớp |
| 61 | q_0e72ff8aa98e | dev_008 | 216 | 216 | Khớp |
| 62 | q_0ea6fdb19b86 | dev_005 | -27.94 | -27.94 | Khớp |
| 63 | q_0ed1ebc4fe4d | dev_001 | 153 | 153 | Khớp |
| 64 | q_0ef1608808dd | dev_008 | 530899800 | 530899800 | Khớp |
| 65 | q_0ef28ee17f12 | dev_002 | 0 | 151 | Sai khác |
| 66 | q_0f59f61affd1 | dev_001 | 4947 | 4947 | Khớp |
| 67 | q_0f7d15097a8c | dev_007 | 4917 | 4917 | Khớp |
| 68 | q_1019063a351e | dev_014 | 54100000 | 54100000 | Khớp |
| 69 | q_101a2b744e56 | dev_014 | 4 | 4 | Khớp |
| 70 | q_101e84f9b702 | dev_003 | 186355417 | 186355417 | Khớp |
| 71 | q_102720daf6a9 | dev_013 | 685034500 | 685034500 | Khớp |
| 72 | q_1040545087b6 | dev_011 | 55500000 | 55500000 | Khớp |
| 73 | q_105bc863feea | dev_005 | 267653200 | 267653200 | Khớp |
| 74 | q_107e86e608dc | dev_005 | 264831100 | 264831100 | Khớp |
| 75 | q_10831a6f3a2b | dev_004 | abstain | 152 | Abstain |
| 76 | q_10a0c4fec3d2 | dev_003 | false | false | Khớp |
| 77 | q_113d86c3eb6a | dev_006 | -2.27 | -2.27 | Khớp |
| 78 | q_11c915f78c7e | dev_001 | 312 | 312 | Khớp |
| 79 | q_1205eaa6091a | dev_012 | 16.03 | 16.03 | Khớp |
| 80 | q_121c6959016d | dev_008 | 15 | 15 | Khớp |
| 81 | q_12712b3cc328 | dev_011 | 6 | 6 | Khớp |
| 82 | q_128fd07d838e | dev_009 | 44 | 44 | Khớp |
| 83 | q_129b29f14e89 | dev_008 | 414492100 | 414492100 | Khớp |
| 84 | q_12d78074f059 | dev_003 | 73226 | 73226 | Khớp |
| 85 | q_1341cdede99a | dev_003 | 12 | 12 | Khớp |
| 86 | q_14565ad8b459 | dev_007 | 26744500 | 26744500 | Khớp |
| 87 | q_1473ee8c46fd | dev_005 | -14.34 | -14.34 | Khớp |
| 88 | q_14a10f251fd0 | dev_013 | "Vinamilk" | "Vinamilk" | Khớp |
| 89 | q_15711c61bb0d | dev_008 | abstain | unanswerable | Abstain |
| 90 | q_15a8893ff6f9 | dev_008 | 1708 | 1708 | Khớp |
| 91 | q_15dd37187b0d | dev_005 | 62.8 | 62.8 | Khớp |
| 92 | q_1602cfcc7d42 | dev_003 | 4233 | 4233 | Khớp |
| 93 | q_1618b7c50ae7 | dev_001 | 69.31 | 69.31 | Khớp |
| 94 | q_162c076a350d | dev_013 | 1529866500 | 1529866500 | Khớp |
| 95 | q_16852a1ac5f7 | dev_014 | 16 | 16 | Khớp |
| 96 | q_169a123623e6 | dev_004 | 49.71 | 49.71 | Khớp |
| 97 | q_169bfdcce0ae | dev_006 | 28.25 | 28.25 | Khớp |
| 98 | q_16e72d67a5f4 | dev_010 | 32859087 | 32859087 | Khớp |
| 99 | q_16ec75d6c8a4 | dev_008 | 10864 | 10864 | Khớp |
| 100 | q_16fc554b8db0 | dev_003 | 2130 | 2130 | Khớp |

- [Prediction JSONL](../result/public_test_next50_16w.jsonl).
- [Phép tính/bằng chứng gốc](../artifacts/public_next50_16w_review/raw_audit.json).
- [Review JSON](../artifacts/public_next50_16w_review/review.json).
- [Manifest phiên bản runtime](../artifacts/public_next50_16w/run_manifest.json).

Audit không sửa file prediction hoặc code solver và không gọi API.
