# Sửa normalization và kiểm chứng — 03/11/2026

**Đính chính sau debug 20 câu:** audit cũ bỏ sót alias `A.An`; tổng thợ An đúng là **25.180.500**, không phải 15.778.000. Replay trước sửa SQL là 28/30 khớp sau đính chính này. [Lượt chạy lại 20 câu qua Solver hiện tại](PUBLIC_TEST_NEXT20_RERUN.md) đã xử lý câu lãi ròng và lỗi context quá dài. Các kết quả này dùng plan lưu sẵn, không phải 20 lượt LLM mới.

Đã sửa code trong source_test và tạo lại dữ liệu chuẩn hoá từ nguồn. File gốc và ZIP được giữ nguyên. [Chỉ mục cache mới](NORMALIZED_DATA.md), [manifest kiểm chứng](../artifacts/normalization_v2/manifest.json).

| Bộ | Doanh nghiệp | Bảng | Bản ghi giữ lại | File nguồn khớp ZIP |
|---|---:|---:|---:|---:|
| Train | 30 | 339 | 3.985.115 | 158/158 |
| Public test | 14 | 163 | 1.315.662 | 94/94 |

Mọi bảng đạt raw = kept + excluded + duplicates. Không còn lỗi parse trong metadata cột, gồm qty_base suy từ quy đổi; không có diagnostic mức error. Các cột có dữ liệu nhưng chưa gán semantic role vẫn được giữ và [liệt kê riêng](../artifacts/normalization_v2/unmapped_columns.json); không coi chúng là đã được hiểu đầy đủ.

## Các thay đổi đã áp dụng

1. **Tiền:** bỏ truyền locale giữa các cột. Ưu tiên định dạng tiền rõ trong từng ô: `7.000đ` không còn thành 7 với locale en. Giữ đúng số thập phân/export như `8,000.0000`, k/tr. Profile nhận ra nhóm nghìn xen số nguyên nhỏ, tránh biến `4,400` thành NULL chỉ vì cùng cột có `500`.
2. **Bố cục:** nhận tổng ngày/tháng giữa bảng, nhãn ở cột tên khách hoặc mã đơn. Chỉ loại khi nhãn đi cùng cấu trúc tổng và thiếu bằng chứng giao dịch. Phân loại trước inheritance; giữ sản phẩm thật tên giống nhãn tổng khi có bằng chứng giao dịch.
3. **Schema:** bổ sung Đơn số, Giờ tạo đơn, Hàng hóa, Barcode, S.L, Thành tiền (đ), HĐ; tách ngày với thời điểm tạo đơn. Chuẩn hoá tiền/ngày chi; sổ sửa xe có ngày sự kiện và tiền phiếu.
4. **Identity:** trim/NFC/khoảng trắng cho mã/tên. Chuẩn tên sản phẩm theo mã/danh mục hoặc alias duy nhất; không ép ghép khi mơ hồ. Loại khách lẻ/kl khỏi tên khách dùng xếp hạng, giữ giao dịch và dữ liệu gốc; không loại khách thực mang tên Lê.
5. **Trả hàng:** truyền returned cho toàn đơn khi có bằng chứng từ trạng thái, số lượng trả, hoặc số lượng âm được README xác nhận, theo mã/scope. Trả sau kỳ vẫn ảnh hưởng đơn gốc.
6. **Đơn vị:** giữ đơn vị trung gian khi quy đổi thùng → hộp → vỉ → viên. Chuỗi thiếu/cyclic/conflicting không được biến thành hệ số đoán.
7. **SQL:** cấp public views theo catalog; SELECT * không kéo provenance, truy vấn trực tiếp cột nội bộ vẫn bị chặn.
8. **Compiler/controller:** template chấp nhận điều kiện ngày/trạng thái chuẩn và grouping sản phẩm thông thường, tiếp tục fallback với điều kiện bổ sung. Top theo mã, tên dùng hiển thị. Nhánh unsupported không bị buộc chọn mọi partition; chấp nhận kiểm kê đủ khi header đều đã được hiểu và capability thiếu được xác nhận từ nguồn.
9. **Prompt/runner:** tách nested-field guidance khỏi object cần xuất, nhắc giới hạn hai probe; hỗ trợ chọn question ID, kiểm tra signature/source revision để không nhập checkpoint cũ sau sửa.

## Lineage

Full pass đọc đủ 14 public và 30 train bằng 1.2.0. Sau khi phát hiện ba cột giá mơ hồ, biz_119, biz_120, biz_124 được đọc lại bằng 1.2.1; các cache không lỗi được giữ. Bản vá cuối chỉ thay suy locale chưa biết cho cột tiền có nhóm nghìn xen số nguyên nhỏ; các định dạng đã parse thành công giữ cách diễn giải cũ. Manifest ghi revision/snapshot từng business; đây không phải một full pass duy nhất bằng 1.2.1.

Tất cả source hash, column.parse_errors và diagnostic được kiểm tra lại khi tạo manifest cuối. Cache từng có lỗi giữ để truy nguyên, không nằm trong manifest hiện hành.

## Kiểm chứng

- [115 tests không failure/error](../artifacts/normalization_v2/pytest_final_1_2_1.xml): tiền trộn định dạng, tổng giữa bảng, tên theo mã, khách vãng lai, trả ở kỳ sau, public SELECT *, thiếu capability, quy đổi nhiều cấp/cycle.
- [Public full pass](../artifacts/normalization_v2/public_ingestion_final.json), [train full pass trước vá cuối](../artifacts/normalization_v2/train_ingestion_final.json), [ba business dựng lại](../artifacts/normalization_v2/train_ingestion_patch.json). Report train lịch sử còn ghi lỗi đã được vá; dùng manifest cuối để lấy revision hiện hành.
- [Replay 30 plan cũ trên cache mới](../artifacts/normalization_v2/frozen_plan_replay.json): **28 khớp** sau đính chính audit; còn hai sai lệch thật ở trung bình đơn và lãi ròng. Sáu prediction sai giá trị của baseline đều khớp sau sửa dữ liệu/compiler; câu thiếu nhóm hàng được xác nhận unanswerable.
- [Kiểm chứng bằng model mới](NORMALIZATION_LIVE_REVIEW.md) đã xác nhận trung bình đơn 71.724 đồng. Câu thợ không sai SQL trên dữ liệu này; lỗi nằm ở audit. Lãi ròng được sửa bằng binding typed SQL và kiểm tra tại máy; hai lượt API còn lại vẫn chưa có prediction do lỗi dịch vụ.

Replay là kiểm tra cơ chế, không phải 27 dự đoán model mới hoặc PA. Mẫu đã được xem và debug không đại diện holdout/private. Structural verifier/gate chưa bảo đảm mọi SQL đúng đủ nghiệp vụ; chưa fit calibration.

## Sử dụng

Tra SQLite/catalog theo [NORMALIZED_DATA.md](NORMALIZED_DATA.md). Không lấy đường dẫn từ report cũ hoặc trộn revision. Thử nghiệm API mới dùng `--ingestion-report artifacts/normalization_v2/public_ingestion_final.json`, artifact/output riêng theo [PUBLIC_TEST_RUN.md](PUBLIC_TEST_RUN.md).

Đọc lại toàn bộ bằng code hiện tại:

```powershell
python -m scripts.validate_ingestion --data data/public_test --archive public_test.zip --workers 3 --output artifacts/public_rebuild_new.json
python -m scripts.validate_ingestion --data data/train --archive train.zip --workers 3 --output artifacts/train_rebuild_new.json
```

Code tự tạo revision mới khi ingestion implementation đổi. Source không bị ghi đè; __raw_json và __source_row giữ thông tin gốc để audit.
