# Debug nguyên nhân gốc của 10 câu public test

Ngày kiểm tra: 03/11/2026. Phạm vi: hai câu sai giá trị và hai câu abstain trong [báo cáo 10 câu](PUBLIC_TEST_FIRST10_REVIEW.md). Đã tái hiện cả bốn trường hợp tại máy bằng plan đã lưu, không gọi lại model/API. Các giá trị đối chiếu từ dữ liệu phát hành; chưa có gold chính thức.

**Các lỗi tập trung ở hợp đồng dữ liệu và kiểm chứng nghiệp vụ:** giá trị bị biến đổi sai lúc ingestion; danh tính mặt hàng chưa được bảo toàn khi tổng hợp; metadata thiếu làm điều phối sai; bộ kiểm tra chỉ xác nhận thực thi/định dạng nên vẫn cho hai kết quả sai đi ra.

- [Script tái hiện](../scripts/debug_first10_root_causes.py).
- [Toàn bộ kết quả debug, ablation và replay](../artifacts/public_first10/debug/root_causes.json).
- [Đối chiếu từng dòng tiền](../artifacts/public_first10/dev002_normalization_differences.json).

## 1. Câu doanh thu ngày: hai lỗi ingestion độc lập

ID: `q_00bf8df3ea0b`, `dev_002`, ngày 04/03/2025. Prediction: **7.893.391**; tính trên các dòng hàng gốc: **4.533.000**.

### 1.1. Locale của cột đơn giá bị truyền sang tổng tiền

Vị trí:

- [ingestion.py:395](../artifacts/public_50_trammay/runtime_snapshot/bizreason/data/ingestion.py#L395): suy locale trên 2.000 dòng đầu cho từng cột.
- [ingestion.py:410](../artifacts/public_50_trammay/runtime_snapshot/bizreason/data/ingestion.py#L410): nếu chỉ có một locale tài chính đã xác định, truyền nó sang các cột tiền còn `unknown`.
- [parsing.py:83](../artifacts/public_50_trammay/runtime_snapshot/bizreason/data/parsing.py#L83): dấu hiệu tiền trong từng ô chỉ làm chuyển sang `vi` khi locale đầu vào là `unknown`.

Đã đọc lại đúng 2.000 dòng nguồn để tái hiện:

```text
Giá sản phẩm: en
Tổng tiền:   unknown
          ↓ shared_locale
Tổng tiền:   en
```

Các ô trong cùng một cột tổng tiền có nhiều cách ghi. Giả định chúng có cùng locale, rồi mượn locale từ cột khác, không còn đúng với dữ liệu này. Ví dụ:

| Ô gốc | Parser với locale `en` đang dùng | Giá trị đúng của ô này |
|---|---:|---:|
| `7.000đ` | 7 | 7.000 |
| `105.000đ` | 105 | 105.000 |
| `45k` | 45.000 | 45.000 |
| `50,500` | 50.500 | 50.500 |

`parse_number("7.000đ", locale="en", unit="VND")` trả `ParseStatus.OK`, giá trị Decimal `7.000`, tức **7**, không phải 7.000 đồng. Vì là số hợp lệ về cú pháp, diagnostic không ghi parse error. Trong ngày hỏi, 33 ô bị đọc nhỏ đi 1.000 lần, tổng thiếu **1.389.609** đồng.

Nguyên nhân cần sửa là cơ chế ưu tiên/kiểm chứng bằng chứng định dạng, gồm cả truyền locale giữa cột. Không thể sửa bằng cách ép toàn bộ số sang locale Việt: cách ghi `50,500` hoặc `8,000.0000` ở các nguồn khác vẫn phải được đọc đúng. Các ô còn mơ hồ cần giữ trạng thái mơ hồ và provenance, thay vì gán một số chắc chắn.

### 1.2. Dòng tổng giữa bảng bị nhận thành dòng hàng

Vị trí:

- [layout.py:43](../artifacts/public_50_trammay/runtime_snapshot/bizreason/data/layout.py#L43): `is_summary()` chỉ xem xét dòng nằm **sau bản ghi cuối**, đồng thời đòi nhãn khớp hoàn toàn một mục trong `SUMMARY_LABELS`.
- [ingestion.py:432](../artifacts/public_50_trammay/runtime_snapshot/bizreason/data/ingestion.py#L432): tính vị trí bản ghi cuối cho cả vùng bảng.
- [ingestion.py:483](../artifacts/public_50_trammay/runtime_snapshot/bizreason/data/ingestion.py#L483): sau phân loại, kế thừa mã đơn/ngày/trạng thái từ nhóm trước.

Dòng Excel **813** có dữ liệu gốc:

```text
Mã đơn hàng = trống
Ngày đặt hàng = trống
Trạng thái = trống
Tên khách hàng = TỔNG NGÀY 04/03
Tên sản phẩm = trống
Số lượng = trống
Tổng tiền = 4750k
```

Dòng này nằm giữa các ngày bán; nhãn `TỔNG NGÀY 04/03` cũng không bằng chính xác `tong`, `tong cong`, `total`… Vì vậy có **hai điều kiện cùng làm bộ phân loại bỏ sót**. Gọi `is_summary` với `after_last_record=False` hay `True` đều trả `False`.

Sau đó inheritance biến dòng tổng thành dữ liệu của đơn `ORD006456`, ngày `2025-03-04`, trạng thái `completed`. SQL SUM cộng thêm **4.750.000** đồng. Inheritance được áp dụng khi chưa chứng minh dòng hiện tại là một dòng hàng hợp lệ.

Fixture nhỏ trong script gồm: đơn A = 100, tổng ngày = 100, đơn B = 200. Ingestion hiện tại giữ **3 dòng, tổng 400**, trong khi phải giữ **2 dòng, tổng 300**. Fixture chỉ dùng số nguyên để tách lỗi phân loại dòng khỏi lỗi parse tiền.

### 1.3. Tách ảnh hưởng từng lỗi

Các số dưới đây được tính lại từ các dòng của ngày hỏi trong cache và ô tiền gốc; chỉ thay một yếu tố mỗi lần, không ghi đè cache:

| Điều kiện | Doanh thu |
|---|---:|
| Giữ parser và dòng tổng hiện tại | 7.893.391 |
| Chỉ sửa cách đọc tiền | 9.283.000 |
| Chỉ loại dòng tổng 813 | 3.143.391 |
| Sửa cả hai | **4.533.000** |

Đẳng thức giải thích trọn vẹn sai lệch: `4.533.000 − 1.389.609 + 4.750.000 = 7.893.391`.

## 2. Câu top 5: một mã hàng bị tách theo cách viết tên

ID: `q_01ba91ff94c6`, `dev_008`, tuần 03–09/11/2025.

SQL model sinh:

```sql
GROUP BY ma_hang, ten_hang
ORDER BY SUM(sl_le) DESC, ma_hang ASC
LIMIT 5
```

Mã `SP0003` có ba cách viết tên:

| Tên trong dữ liệu | Số lượng cơ bản |
|---|---:|
| Cánh gà chiên nước mắm | 81 |
| Cánh Gà Chiên Nước Mắm | 18 |
| CÁNH GÀ CHIÊN NƯỚC MẮM | 13 |
| Cộng theo mã SP0003 | **112** |

Do nhóm theo cả tên, SQL chỉ thấy các nhóm 81, 18, 13. `SP0002` — Bia Tiger lon — có 108 nên được đưa vào hạng 5. Theo mã hàng, SP0003 có 112 và phải đứng trước SP0002.

**Tái hiện nhân quả:** giữ nguyên bảng, kỳ hỏi, bộ lọc trạng thái, số lượng, LIMIT và ORDER BY; chỉ thay `GROUP BY ma_hang, ten_hang` thành `GROUP BY ma_hang`. Kết quả đổi đúng món thứ 5 thành Cánh gà chiên nước mắm, bốn món đầu giữ nguyên.

Chỗ thiếu kiểm soát:

1. [planning.py:242](../artifacts/public_50_trammay/runtime_snapshot/bizreason/planning.py#L242) kiểm tra tên cột/grouping tồn tại, chưa kiểm tra grouping có bảo toàn một đối tượng theo mã hay không. Plan sai này trả **không có validation error**.
2. [compilation.py:51](../artifacts/public_50_trammay/runtime_snapshot/bizreason/compilation.py#L51) chuyển sang generated SQL khi `group_by` không rỗng, dù đây là grouping thông thường của metric top sản phẩm. `template_eligible=true` trong plan không đảm bảo dùng template.
3. Template có GROUP BY theo mã ở [compilation.py:143](../artifacts/public_50_trammay/runtime_snapshot/bizreason/compilation.py#L143), nhưng ép dùng riêng nhánh template hiện cũng bị chặn bởi null trong `sl_le`. Vì vậy sửa riêng điều kiện fallback chưa đủ; cần xử lý dòng không phải giao dịch và phân biệt null ở dòng bị loại với null ở dòng cần tính.
4. Verifier/gate vẫn cho kết quả sai đi ra, chi tiết ở mục 5.

Hướng sửa là xác lập identity bằng mã trước khi tổng hợp; tên là thuộc tính để hiển thị. Nhánh SQL tự do cũng cần invariant hoặc kiểm tra cấu trúc tương ứng. Chỉ thêm một câu nhắc trong prompt chưa tạo được bảo đảm này.

## 3. Câu nhóm trà: plan nhận ra thiếu dữ liệu, controller không công nhận

ID: `q_008fdebb6ec9`, `dev_002`.

Audit đã kiểm tra header của 17 file bán hàng và bảng quy đổi: không có nhóm hàng để xác định nhóm trà theo rulebook. Hai plan được lưu đều đề xuất `unsupported/missing_field`, required = `product_category`. Đầu ra cuối lại là `abstain`.

Hai điểm chặn:

1. **Validation của truy vấn tính số được áp sang bằng chứng thiếu dữ liệu.** [planning.py:321](../artifacts/public_50_trammay/runtime_snapshot/bizreason/planning.py#L321) chạy kiểm tra partition cho mọi metric. `partition_is_needed()` không có nhánh `unsupported`, nên không xác định role thời gian, không có bounds, rồi trả `True`. Plan đầu chỉ chọn tháng 11/2025 bị báo thiếu **16 partition khác** trước khi được xét bằng chứng thiếu nhóm hàng.
2. **Chỉ công nhận thiếu trường nếu README nói rõ bằng một câu phủ định.** [decision.py:92](../artifacts/public_50_trammay/runtime_snapshot/bizreason/decision.py#L92) tìm các dòng có `không ghi`, `không có`, `không lưu`, rồi đòi alias của nhóm hàng xuất hiện trong đó. README không có một câu phủ định về nhóm hàng, nên dù catalog không có role này, `verify_unsupported()` vẫn trả `False` với lý do `No explicit evidence that the data cannot support this question`.

Thử nghiệm chẩn đoán chỉ trong bộ nhớ: thêm câu “Không có nhóm hàng.” vào bản sao README làm cùng claim được chấp nhận. Không thêm câu này vào README thật. Thí nghiệm chứng minh bộ kiểm tra đang phụ thuộc vào cách diễn đạt phủ định trong README; nó chưa sử dụng đầy đủ bằng chứng kiểm kê schema đã hoàn tất.

Không nên đổi thành quy tắc “không thấy role thì chắc chắn unanswerable”: mục 4 cho thấy role có thể bị mapper bỏ sót dù cột thật vẫn tồn tại. Cần trạng thái capability `có / xác nhận thiếu / chưa xác định`, gắn với bằng chứng đã đọc đủ nguồn và kiểm tra mapping/derivation/join. Bằng chứng xác nhận thiếu không cần chạy như một SQL plan.

## 4. Câu trung bình đơn: metadata thiếu làm tốn lượt sửa, rồi SELECT * đụng provenance

ID: `q_01ac0fc9f2a2`, `dev_003`, tuần 10–16/08/2026. Tính độc lập: `66.272.600 / 924`, làm tròn thành **71.724 đồng/đơn**.

### 4.1. Mất các role nghiệp vụ ngay từ catalog

Trong cả hai bảng bán hàng 2025/2026:

| Cột | Role cần xác minh theo nguồn | Role hiện tại |
|---|---|---|
| `don_so` — Đơn số | mã đơn | `null` |
| `gio_tao_don` — Giờ tạo đơn | thời gian đặt | `null` |
| `trang_thai_giao` — Trạng thái giao | trạng thái đơn | `null` |
| `thanh_tien_d` — Thành tiền (đ) | thành tiền dòng | `null` |
| `barcode` | mã hàng | `null` |
| `hang_hoa` — Hàng hóa | tên hàng | `null` |

[schema.py:9](../artifacts/public_50_trammay/runtime_snapshot/bizreason/data/schema.py#L9) chủ yếu tra alias chính xác. [schema.py:92](../artifacts/public_50_trammay/runtime_snapshot/bizreason/data/schema.py#L92) chỉ lấy mô tả cột từ các đề mục/danh sách có cấu trúc cụ thể. README business này mô tả luồng và grain trong văn bản, không có đủ danh sách cột theo cấu trúc mapper trông đợi.

Hệ quả thực tế:

- Bảng bị gán `independent_ledger_row`, trong khi README nói mỗi dòng là dòng hàng của hoá đơn.
- Coverage của hai bảng đều là `{}` dù dữ liệu có thời gian tạo đơn.
- Cột có role `null` không đi qua parser tương ứng; `parse_errors=0` không chứng minh các cột đó đã được chuẩn hoá đúng.
- Validator không loại được partition năm 2025 khỏi yêu cầu tháng 8/2026. Không có bounds thì giữ nguồn là lựa chọn thận trọng, nhưng ở đây bounds bị mất vì schema mapping chưa hoàn tất.

Thử nghiệm chỉ trong bộ nhớ: gán role thời gian và min/max đã quan sát cho `gio_tao_don` của hai bảng. Plan đầu chỉ dùng 2026 chuyển từ `MISSING_PARTITION:...2025...` sang **không còn validation error**. Thử nghiệm này chỉ cô lập lỗi coverage, không coi toàn bộ mapping đã được sửa.

### 4.2. Lượt sửa tiếp theo bị chặn bởi SELECT *

Luồng đúng theo trace và replay:

```text
Plan 1: chọn năm 2026
→ bị yêu cầu thêm năm 2025; repairs = 1
Plan 2: UNION hai năm, SELECT * ở các nguồn
→ * mở rộng cả __source_row và __raw_json
→ executor chặn đọc cột nội bộ; repairs = 2
→ vượt max_repairs = 1; abstain
```

- [ingestion.py:390](../artifacts/public_50_trammay/runtime_snapshot/bizreason/data/ingestion.py#L390) đặt provenance và cột nghiệp vụ trong cùng bảng vật lý.
- [execution.py:149](../artifacts/public_50_trammay/runtime_snapshot/bizreason/execution.py#L149) chặn các cột bắt đầu `__`, nên SELECT * trên nguồn bị từ chối. Đây là cơ chế giới hạn quyền đang hoạt động theo cấu hình; lỗi nằm ở cách đưa schema công khai cho generator và cách biên dịch/khôi phục query.
- [pipeline.py:110](../artifacts/public_50_trammay/runtime_snapshot/bizreason/pipeline.py#L110), [pipeline.py:165](../artifacts/public_50_trammay/runtime_snapshot/bizreason/pipeline.py#L165) dùng chung số lượt sửa cho lỗi validation lẫn execution. Lượt sửa đã bị dùng vào yêu cầu partition do metadata thiếu.

**Tái hiện nhân quả:** thay hai `SELECT * FROM <bảng nguồn>` bằng danh sách 26 cột công khai, giữ nguyên mọi phép tính/bộ lọc của SQL plan 2. Query chạy thành công và trả **71.724**, khớp tính độc lập. `SELECT DISTINCT *` trên CTE lúc này hợp lệ vì CTE không còn cột nội bộ.

Hướng sửa: public views/catalog projection thực sự tách provenance, hoặc mở rộng `table.*` theo SQL AST và catalog trước execution. Không thay mọi ký tự `*` bằng regex; phải phân biệt `COUNT(*)`, phép nhân, alias và CTE. Đồng thời hoàn thiện mapping/coverage để không tốn repair vào lỗi metadata có thể phát hiện trước inference.

## 5. Điểm yếu chung: verified hiện chỉ xác nhận kỹ thuật

Vị trí: [verification.py:23](../artifacts/public_50_trammay/runtime_snapshot/bizreason/verification.py#L23), [verification.py:89](../artifacts/public_50_trammay/runtime_snapshot/bizreason/verification.py#L89), [decision.py:158](../artifacts/public_50_trammay/runtime_snapshot/bizreason/decision.py#L158), [pipeline.py:159](../artifacts/public_50_trammay/runtime_snapshot/bizreason/pipeline.py#L159).

Hai đáp án sai đều có:

```text
execution_success
result_shape
catalog_scope
typed_output
rule_refs_validated
semantic_correctness_not_proven_by_execution
verified = true
DecisionGate.action = value
```

`rule_refs_validated` kiểm tra plan dẫn ID luật hợp lệ ở bước trước; nó không chứng minh SQL đã thực hiện đúng từng luật. Cờ `semantic_correctness_not_proven_by_execution` là thông tin ghi log, chưa ngăn gate chọn đáp án. Solver cũng dừng ngay ở candidate đầu tiên được đánh dấu verified.

Vì vậy một số tiền đọc sai vẫn đúng type number; một top 5 nhóm sai vẫn đúng shape list. Cả hai qua gate. Trong 10 câu này, cả 8 câu trả giá trị đều dùng generated SQL, không có candidate template được chọn.

Cần tách rõ `execution_valid`, các invariant theo metric và căn cứ chọn đáp án. Ví dụ: tổng chỉ lấy dòng giao dịch hợp lệ; một mã hàng phải tạo đúng một nhóm tổng hợp; coverage/role cần được xác minh; SQL phải phù hợp điều kiện và grain trong plan. Calibration sau đó cần prediction holdout thực, không biến nhãn “SQL chạy được” thành nhãn đúng nghiệp vụ.

## 6. Vì sao các kiểm thử trước chưa phát hiện

| Kiểm thử hiện có | Trường hợp còn thiếu trong dữ liệu thật |
|---|---|
| Parser đọc `79.000đ` với locale unknown | Ô có đuôi đồng nhưng nhận locale `en` từ cột khác |
| Dòng “Cộng” ở cuối bảng, trước note | “TỔNG NGÀY dd/mm” giữa bảng, nhãn đặt ở cột tên khách |
| Top sản phẩm với tên ổn định | Cùng một mã có nhiều cách viết tên |
| Missing column không đủ để kết luận unanswerable | Có bằng chứng kiểm kê đầy đủ, nhưng README không nói phủ định |
| Policy chặn cột ẩn và xử lý empty result | Query nghiệp vụ bình thường dùng SELECT *, sau một lỗi partition |
| Fixture có cột chuẩn hoá tên | Header riêng của tiệm và README mô tả bằng văn xuôi |

100 tests trước đó kiểm tra những trường hợp đã viết; số đó không phải chứng cứ rằng các tổ hợp định dạng/nghiệp vụ ở public đã được bao phủ.

## 7. Thứ tự khắc phục và tiêu chí kiểm chứng

| Ưu tiên | Việc cần làm | Tiêu chí trên các trường hợp đã tái hiện |
|---|---|---|
| P0 | Sửa bằng chứng định dạng tiền, truyền locale và phát hiện xung đột | `7.000đ` thành 7.000; vẫn giữ đúng `50,500`, `8,000.0000`, k/tr và số thập phân thật |
| P0 | Phân loại dòng trước inheritance; hỗ trợ tổng nhóm/ngày giữa bảng bằng nhãn + cấu trúc | Dòng 813 không thành giao dịch; fixture 3 dòng giữ 2, tổng 300; sản phẩm thật có tên giống nhãn tổng vẫn được giữ |
| P1 | Xác minh role/grain/coverage từ schema và README, ghi rõ phần chưa map | Các cột cốt lõi dev_003 không còn im lặng mang null; coverage đủ để xét partition năm 2025 |
| P1 | Bảo toàn identity theo mã; kiểm tra grouping của generated SQL và điều kiện fallback template | SP0003 tổng 112, đứng thứ 5; đổi cách viết hoa thường không đổi top |
| P1 | Tách query công khai khỏi provenance và xử lý star đúng cấu trúc | Plan trung bình đơn chạy được, vẫn chặn truy vấn chủ động đọc cột nội bộ |
| P1 | Xác minh thiếu capability bằng kiểm kê nguồn; tách nhánh unsupported khỏi closure của query tính số | Câu nhóm trà được xét trực tiếp trên bằng chứng thiếu nhóm; mapping chưa biết vẫn không bị coi là xác nhận thiếu |
| P2 | Invariant nghiệp vụ, fault injection và holdout/calibration | Hai SQL sai bị phát hiện hoặc không đủ điều kiện đi ra; đo lại trên business holdout |

Sau sửa ingestion phải tạo cache revision mới, tính lại từ file nguồn và chạy lại inference/đánh giá ở thư mục artifact riêng. Không lấy checkpoint dùng cache cũ rồi coi là kết quả sau sửa.

## 8. Trạng thái thay đổi và cách tái hiện

Lần debug này chỉ thêm script, fixture và báo cáo bên trong `source_test/`. Code solver, README/dữ liệu gốc, cache của đợt chạy 50 câu và prediction gốc không được sửa. SHA-256 các database được đọc trước/sau debug: không đổi. Các query đối chiếu chạy read-only; biến thể role/coverage/README chỉ tồn tại trong bộ nhớ.

```powershell
cd source_test
python -X utf8 -u -m scripts.debug_first10_root_causes
```

Script dùng plan đã lưu để tái hiện, không tạo prediction mới bằng LLM. Các thử nghiệm thay từng yếu tố chứng minh nguyên nhân trên những câu được xét; chưa chứng minh một bản sửa tổng quát đã đúng trên cả public/private.
