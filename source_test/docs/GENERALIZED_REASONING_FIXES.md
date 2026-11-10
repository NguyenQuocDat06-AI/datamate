# Debug và sửa cơ chế dùng chung cho ba lỗi public

Đã sửa trong `source_test/`. Không thêm ID câu hỏi, business ID, tên khách, tên mẫu xe hoặc đáp án của ba câu vào `bizreason/`. Bản sửa tác động vào chuẩn hóa schema/identity, hợp đồng JSON, controller và ngân sách context; không chèn đáp án audit vào prompt.

## Nguyên nhân và cơ chế sửa

| Lớp lỗi | Nguyên nhân gốc | Sửa dùng chung |
|---|---|---|
| Đếm xe | Dùng mô tả hiển thị làm identity; `COUNT(DISTINCT xe)` đếm cả cách viết tên xe | Tách khóa biển số và khóa dòng xe từ cột được README/schema xác định là mô tả xe kèm biển số. Đếm entity theo biển số; giữ nguyên mô tả gốc. |
| Sổ trả góp | Ngày/tiền chưa có kiểu rõ ràng; model tự viết chuỗi `REPLACE/CAST` dài và suy nhóm từ thứ tự dòng | Nhận dạng cấu trúc sổ bằng tổ hợp header Việt/Anh, gán `contract_id`, `contract_at`, `contract_amount`, `upfront_amount`, `paid_at`, `paid_amount`; parse tiền/ngày bằng parser chung. Khóa hợp đồng là khóa được ghi trong dữ liệu. |
| Metadata thực thể | Guide để `entity_constraints=[]`, trong khi validator đòi `table/column`; model dùng `field` và boolean `lookup_normalized`; `column_types=null` làm lỗi dù output là list | Guide nêu rõ nested fields. Canonicalizer chỉ phục hồi metadata dư thừa khi không mâu thuẫn: qualified field → table/column đã có trong catalog; cờ normalize → chuỗi chuẩn hóa từ chính giá trị được cung cấp; metadata table không dùng trong list được chuẩn hóa về mặc định. |
| Repair bị bỏ qua | Một plan có SQL đã sửa nhưng kèm probe khiến controller `continue` kể cả ở lượt cuối/hết quota | Truyền budget còn lại cho planner. Chỉ probe khi còn lượt model để dùng quan sát. Ở lượt cuối, SQL hoàn chỉnh qua validation vẫn được thực thi; `unresolved` vẫn chặn thực thi. |
| Vượt context sau probe | Rulebook, schema, bằng chứng khóa nối và preview cùng dồn vào prompt; chỉ bỏ samples chưa đủ | Tham chiếu join bằng ID, khôi phục chính xác edge đã xác minh ở controller. Khi cần, giới hạn preview và ghi rõ số dòng bị lược, tổng row_count và full-result hash; không bỏ rulebook, schema hoặc SQL. Trace giữ preview ban đầu. |

Các file chính:

- [Nhận dạng schema sổ](../bizreason/data/ledger_schema.py), [chuẩn hóa identity xe](../bizreason/data/vehicles.py), [tích hợp ingestion](../bizreason/data/ingestion.py).
- [Chuẩn hóa đầu vào plan](../bizreason/plan_input.py), [đóng gói context](../bizreason/prompt_budget.py), [planner](../bizreason/planning.py).
- [Kiểm tra identity/nhóm sổ](../bizreason/semantic_checks.py), [controller](../bizreason/pipeline.py), [SQL helpers](../bizreason/execution.py).

SQL helpers `money_vnd(value[, locale])` và `iso_date(value)` xử lý raw value khi cần; tiền mơ hồ bị từ chối thay vì tự đoán. Với cột đã chuẩn hóa, planner phải dùng trực tiếp, không parse lại. Phép tính tiền tiếp tục dùng Decimal/helpers có sẵn.

## Điều kiện để không gán nghĩa quá rộng

- Cột `STT` không tự động trở thành mã đơn. Cần đủ cấu trúc bảng và README xác nhận STT là số thứ tự đơn. Bảng tồn kho có `STT/Ngày/Hàng/SL` nhưng không có bằng chứng đó không bị biến thành order_line.
- Cột xe cần mô tả schema/README phù hợp; parser chỉ nhận một biển số rõ ràng, không đoán khi có nhiều biển hoặc cú pháp không hợp lệ.
- Nhận dạng sổ tài chính dựa vào đồng thời các trường khóa, giá trị, trả trước, ngày trả và số tiền trả; không chọn theo tên file hoặc tên doanh nghiệp.
- Join reference chỉ trỏ tới edge trong context hiện tại. ID lạ hoặc trường mâu thuẫn bị từ chối; không tạo khóa nối mới.
- Không flatten danh sách giá trị thực thể thành một chuỗi để vượt validation. Không chỉnh SQL/parameters bằng thay thế toàn cục.

## Kiểm chứng

**149 kiểm thử qua, không failure/error**, gồm 22 test instances mới: [JUnit](../artifacts/generalized_fixes/pytest_final.xml), [source kiểm thử](../tests/test_generalized_reasoning_fixes.py).

Các biến thể gồm ba dòng xe khác nhau, nhiều kiểu dấu cách/dấu câu của biển số, phiếu không thu tiền, biển số thiếu/mơ hồ; header sổ Việt/Anh và đảo thứ tự các dòng; tiền theo k/tr/đ, giao dịch ngoài cutoff và các payment events giống nhau vẫn được giữ; ba tên khách khác với public, kỳ năm 2028, đơn huỷ/đơn trả/cash, trả cùng ngày và trả sau ngày mua; plan metadata mâu thuẫn, join ID giả, probe quá lớn và repair ở lượt cuối. Pyflakes qua đối với code mới/sửa và các script kiểm chứng.

**17 câu trước đó đúng giữ nguyên kết quả khi replay plan đã lưu:** [summary](../artifacts/generalized_fixes/regression20/summary.json). Replay này không gọi API và không phải 17 lần lập plan mới.

**Ba câu lỗi được gọi model mới, không dùng plan cũ hoặc đáp án audit trong prompt:**

| Câu | Trước sửa | Model mới | Đối chiếu độc lập |
|---|---|---|---|
| Số xe SH Mode khác nhau | 30 | 27 | 27 |
| Dư nợ trả góp | abstain | 400700000 | 400700000 |
| Nợ thêm/trả nợ Lê Sơn Trung | abstain | [8000, 4000] | [8000, 4000] |

- [Prediction API mới](../result/public_generalized_fixes_live3_v2.jsonl).
- [Manifest và hash code lúc gọi API](../artifacts/generalized_fixes/live3_v2/run_manifest.json), [trạng thái hoàn tất](../artifacts/generalized_fixes/live3_v2/status.json).
- [Kiểm tra cuối](../artifacts/generalized_fixes/final_verification.json): ba prediction khớp audit và chạy lại SQL trên cache cuối; 97 hash nguồn/metadata không đổi; JSONL 20/50 câu lịch sử được giữ nguyên.

Lần API đầu của bản sửa đã khắc phục hai câu nhưng lộ lỗi vượt context ở câu công nợ: [live3](../artifacts/generalized_fixes/live3/status.json). Lần `live3_v2` sau sửa context hoàn tất cả ba. Không ghi đè lượt đầu thành kết quả thành công.

Sau lượt API thành công, bổ sung điều kiện README cho nhận dạng STT. Đã dựng lại hai cache và chứng minh SQLite giống hệt theo byte, catalog giống hệt trừ revision/path: [equivalence](../artifacts/generalized_fixes/schema_guard_equivalence.json). SQL thực sự do model sinh đã được thực thi lại trên cache cuối; không giả định tương đương chỉ từ tên file.

## Dữ liệu và lần chạy tiếp

Parser hiện là `1.3.0`; revision cache bao gồm hash code normalizer. Chỉ dựng lại hai business cần các trường mới, các cache khác giữ revision trước. [Ingestion report hiện dùng](../artifacts/generalized_fixes/public_ingestion.json) ghi rõ việc này. File gốc, cache cũ, prediction và audit cũ được giữ nguyên.

Ví dụ chạy một experiment mới với các cache này, từ `source_test/`:

```powershell
python -X utf8 -u -m scripts.run_public_test --ingestion-report artifacts/generalized_fixes/public_ingestion.json --limit 50 --workers 4 --stream --question-timeout 900 --request-timeout 420 --total-seconds 10800 --artifacts artifacts/public_50_generalized_trammay --output result/public_test_50_generalized_trammay.jsonl
```

Chưa khởi chạy lại toàn bộ 50 câu qua API. Không resume experiment cũ bằng code mới hoặc nhập checkpoint trước sửa như prediction mới.

## Phạm vi bằng chứng

Ba câu public đã được debug và kiểm thử biến thể không thay thế business holdout chưa xem. Nhận dạng header/ý định vẫn có phạm vi; guard SQL chỉ phát hiện các dạng nguy hiểm đã định nghĩa, chưa theo được toàn bộ alias/CTE như một semantic SQL analyzer hoàn chỉnh. Nguồn thiếu identity, tiền mơ hồ hoặc schema ngoài phạm vi có thể tiếp tục cần probe/abstain. Không báo PA chính thức hay bảo đảm thứ hạng từ các kết quả này.
