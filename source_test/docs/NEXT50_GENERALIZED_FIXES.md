# Debug và sửa cơ chế chung cho batch 51–100 — 03/11/2026

Batch ban đầu có **45 kết quả khớp audit, 2 sai giá trị, 3 abstain**. Bản sửa xử lý lỗi ở ingestion, compiler và metadata của plan; không rẽ nhánh theo ID câu hỏi, business, tên sản phẩm hay đáp án. Toàn bộ thay đổi nằm trong `source_test/`.

**Replay 50 plan đã lưu: 49 khớp audit, 1 abstain; giữ nguyên cả 45 kết quả đúng trước đó.** Đây là chạy lại Solver với plan cũ và cache mới, không phải 50 lượt sinh plan mới. [Kết quả replay](../artifacts/next50_fixes/replay50/summary.json), [167 kiểm thử không lỗi](../artifacts/next50_fixes/pytest_final.xml).

Kết quả gọi model mới được lưu riêng trong [JSONL](../result/public_next50_fixes_live4.jsonl), [trạng thái](../artifacts/next50_fixes/live4/status.json) và [kiểm chứng cuối](../artifacts/next50_fixes/final_verification.json). Không ghi đè các prediction lịch sử hoặc file ghép 100 ID. Các giá trị đối chiếu dưới đây do audit tính từ dữ liệu nguồn, **không phải gold hoặc PA chính thức**.

## 1. Lỗi cốt lõi và đường sửa

| Trường hợp phát hiện | Kết quả cũ | Replay sau sửa | Nguyên nhân |
|---|---:|---:|---|
| `q_0bac8c88238d`: doanh thu hôm qua | 15.582.000 | 15.535.000 | Ingestion chỉ loại trùng dòng chi tiết; header hóa đơn bị xuất lặp vẫn được cộng hai lần. |
| `q_0ef28ee17f12`: số ly theo tên sản phẩm | 0 | 151 | SQL dùng `lookup(column) = ?`, nhưng tham số còn dấu ngoặc trong khi `lookup()` bỏ dấu ngoặc. |
| `q_0dff1966b7c3`: trung bình đơn tuần trước | abstain | 106.712 | Metadata ngày không có tên bảng vật lý, kéo theo yêu cầu sai partition. Một plan ban đầu còn thiếu tham số SQL. |
| `q_10831a6f3a2b`: đếm đơn bán chịu có hạn sau ngày hỏi | abstain | 152 | Join ID tự đặt bị từ chối; plan sau dùng tên file làm prefix field, dẫn đến lỗi partition. Cột ngày phải trả cũng chưa được chuẩn hóa về ISO. |
| `q_15711c61bb0d`: nhân viên thân thiện nhất | abstain | abstain | Nguồn không có đánh giá mức thân thiện. Gate chưa chứng minh được tuyên bố thiếu trường theo contract nên giữ abstain. |

Ở lỗi doanh thu, hóa đơn `ORD019871` lặp tại dòng CSV 20616, làm dư **47.000 đồng**. Ở lỗi tên hàng, phía SQL chuẩn hóa `Nước ép cam (M)` thành `nuoc ep cam m`, nhưng tham số cũ là `nuoc ep cam (m)`. Các ID, tên và con số này chỉ xuất hiện trong tài liệu/audit, không được đưa vào logic runtime.

## 2. Loại trùng dựa trên cấp dữ liệu

Trong [ingestion.py](../bizreason/data/ingestion.py), cơ chế fingerprint được áp dụng cho cả `order_line` và `order_header`, với điều kiện có mã đơn thực. Fingerprint giữ mã đơn, scope và toàn bộ ô nguồn sau bước kế thừa/chuẩn hóa mã đơn. Hai header giống hệt nhau chỉ được tính một lần; diagnostic lưu file, sheet, số dòng và hash nguồn.

Các hàng cùng số tiền nhưng khác mã đơn hoặc scope vẫn được giữ. Hai header cùng mã nhưng khác giá trị cũng được giữ để tránh tự chọn một phiên bản không có căn cứ. **Payment event/ledger không bị loại chỉ vì hai dòng giống nhau**: đó có thể là hai lần trả tiền độc lập. Chưa triển khai hợp nhất trùng giữa nhiều file hoặc tự giải quyết các header mâu thuẫn.

Parser tăng từ **1.3.0 lên 1.3.1**. Đã tạo revision cache mới cho **14 business, 163 bảng, 1.315.179 hàng**, không có diagnostic mức error và mọi bảng cân bằng số hàng đầu vào/giữ lại/loại trừ/trùng. Riêng bộ hóa đơn liên quan giảm từ 22.798 xuống **22.490 hàng**, loại **308 header lặp**. Đây là dữ liệu dẫn xuất; file CSV/XLSX gốc và cache trước sửa được giữ nguyên. [Ingestion report](../artifacts/next50_fixes/public_ingestion.json).

## 3. Chuẩn hóa tham số ở compiler

[sql_parameters.py](../bizreason/sql_parameters.py) nhận diện token SQL và vị trí binding, dùng chính `normalize_lookup()` đã đăng ký cho SQLite. [Compiler](../bizreason/compilation.py) tạo danh sách tham số mới cho các phép so sánh trực tiếp `lookup(expression) = ?`, `==`, `!=`, `<>`, dạng đảo hai vế và tham số nguyên giá trị trong `IN/NOT IN`.

Ví dụ với dữ liệu biến thể:

```sql
WHERE lookup(product_name) = ? AND ordered_at >= ? AND line_amount > ?
```

Tham số tên `Trà đào [L]` được bind thành `tra dao l`; ngày và số tiền giữ nguyên. SQL và tham số gốc của plan không bị sửa. Tokenizer bỏ qua dấu `?` trong literal, identifier và comment; kiểm tra số binding trước khi execute để feedback chỉ rõ lỗi.

Các ràng buộc tránh sửa nhầm:

- Không chuẩn hóa tham số `LIKE`, so sánh cột thô hoặc khoảng ngày/tiền.
- Không tự sửa tham số nằm trong hàm lồng hoặc subquery của `IN`.
- Binding đánh số dùng chung cho ngữ cảnh chuẩn hóa và ngữ cảnh thô bị từ chối nếu chuyển đổi sẽ đổi nghĩa; model phải dùng binding riêng.
- Phép so sánh có biểu thức phức tạp ngoài mẫu hỗ trợ yêu cầu model viết rõ `lookup(?)`.
- Contract dùng positional binding; named binding không được chấp nhận.

Đây là bộ nhận diện token có phạm vi giới hạn, không phải parser/kiểm chứng ngữ nghĩa SQL đầy đủ. Nó không bảo đảm nhận diện mọi cách viết tương đương như các wrapper tùy ý quanh kết quả `lookup()`.

## 4. Metadata thời gian và partition

[metadata_fields.py](../bizreason/metadata_fields.py) chỉ sửa **chú thích field của plan**, không thay SQL, bảng đã chọn hoặc nội dung câu hỏi. Một field chưa có tên bảng được ánh xạ khi các bảng ứng viên đã chọn có schema và grain tương đương. Prefix khác tên vật lý chỉ được chấp nhận nếu là alias `FROM/JOIN` thật trong SQL hoặc stem của file nguồn đã có trong catalog.

Khi field của window bị thiếu, chỉ phục hồi từ `required_fields` nếu có duy nhất một vai trò ngày trên toàn bộ bảng đã chọn và schema tương thích. Có cả ngày bán và ngày thanh toán/hạn trả thì không đoán. Prefix tự đặt và join ID không tồn tại tiếp tục bị từ chối; không suy diễn lineage của CTE tùy ý. Mỗi lần ánh xạ được ghi vào `derivation_notes`.

`TimeWindow.mode` phân biệt hai ý nghĩa:

| Mode | Partition phải giữ |
|---|---|
| `period` — mặc định, ví dụ doanh thu tuần trước | Các partition có coverage giao với `[start, end_exclusive)`. |
| `cumulative` — lũy kế đến thời điểm chốt | Mọi lịch sử trước `end_exclusive`, không cắt ở `start`. |

Vì vậy tuần 28/07–03/08 cần cả tháng 7 và tháng 8, nhưng không cần mọi tháng khác. Truy vấn lũy kế vẫn giữ lịch sử cũ. Coverage không gán nhãn ngoài dữ liệu chỉ vì ngày chốt lũy kế nằm sau ngày cuối trong nguồn. Template không hỗ trợ lũy kế chuyển sang SQL fallback thay vì âm thầm tính một kỳ.

Mode là contract của plan; chưa có bộ chứng minh đầy đủ rằng SQL tự sinh thực sự tuân thủ mọi annotation thời gian. SQL chạy thành công vẫn có thể sai nghiệp vụ.

## 5. Chuẩn hóa kiểu dữ liệu theo tài liệu nguồn

[schema.py](../bizreason/data/schema.py) bổ sung `order_revenue` và `due_at`. Mô tả README xác nhận tiền hàng là tổng thành tiền các mặt hàng được ánh xạ sang `order_revenue`, tách khỏi tổng phải trả/phí/tiền đã thu. Các trường hạn thanh toán như `Ngày phải trả` hoặc `due_date` đi qua parser ngày, lưu ISO; không để SQL `date()` phụ thuộc chuỗi ngày hỗn hợp.

Kiểm thử dùng tên cột khác dữ liệu public (`Merchandise`), tiền `2,7tr`/`2800k` và ngày năm 2030. Đây là nhận diện theo vai trò/mô tả, không phải một quy tắc cho riêng file hóa đơn đã phát hiện lỗi. Những mô tả chưa nhận diện được không tự động được suy thành cùng metric.

## 6. Kiểm chứng và giới hạn tổng quát hóa

[test_next50_general_fixes.py](../tests/test_next50_general_fixes.py) thêm **18 trường hợp kiểm thử** với mã đơn, sản phẩm, scope và ngày khác public. Có kiểm thử âm cho payment lặp, header mâu thuẫn, alias không tồn tại, binding dùng lẫn chuẩn hóa/thô, LIKE và nhiều vai trò ngày. Cộng với bộ hiện có: **167 test pass, 0 failure/error/skipped**. Pyflakes đã kiểm tra các module/test/script thay đổi.

[replay_next50_fixes.py](../scripts/replay_next50_fixes.py) dùng full Solver nhưng trả về plan đã lưu thay cho gọi API: **49/50 khớp audit**, giữ nguyên **45/45** kết quả khớp trước đó. Reference chỉ được đọc sau khi toàn bộ output replay đã lưu. Replay kiểm tra tác động của compiler/cache/validator; không đo khả năng model sinh plan mới.

Lượt Trammay riêng chọn bốn câu có đủ dữ liệu nhưng trước đó sai/abstain, sinh plan mới bằng `ag/gemini-3.8-flash-high`, cấu hình `workers=16` với tối đa bốn câu đồng thời. Prompt không chứa đáp án audit hoặc plan cũ. [Manifest](../artifacts/next50_fixes/live4/run_manifest.json) lưu hash runtime, revision dữ liệu, model và budget; [finalize script](../scripts/finalize_next50_fixes.py) đối chiếu sau inference và kiểm tra nguồn/prediction lịch sử không đổi.

Câu về mức độ thân thiện vẫn abstain. Prompt đã yêu cầu model không thay một thuộc tính thiếu dữ liệu bằng proxy doanh số; gate thiếu bằng chứng không được nới chỉ để trả một tên nhân viên hoặc ép `unanswerable`. Cần cơ chế khai báo capability/không có dữ liệu đủ tổng quát và được kiểm định riêng để cải thiện nhánh này.

Các câu dùng để debug không phải holdout; 49/50 replay không phải độ chính xác kỳ vọng trên private test. Chưa chạy lại toàn bộ 50 câu bằng model mới, chưa đánh giá trên business mới và chưa có PA chính thức.

## 7. Chạy lại tại máy

Từ `source_test/`, các lệnh sau chỉ chạy local:

```powershell
python -m pytest -q
python -X utf8 -u -m scripts.prepare_next50_fixes
python -X utf8 -u -m scripts.replay_next50_fixes
python -X utf8 -m scripts.finalize_next50_fixes
```

Lệnh cuối yêu cầu lượt API đã hoàn tất và runtime vẫn khớp manifest. Khi thay runtime hoặc cache, tạo experiment mới; không resume checkpoint của phiên bản trước sửa. [Kết quả baseline trước sửa](PUBLIC_TEST_NEXT50_SUMMARY.md) vẫn được giữ để truy nguyên.
