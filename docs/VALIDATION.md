# Kiểm chứng bản triển khai — 03/11/2026

**Batch tiếp theo 51–100, 16 luồng:** đủ 50 prediction mới, [đối chiếu độc lập](PUBLIC_TEST_NEXT50_REVIEW.md) **45 khớp, 2 sai khác, 3 abstain**. [Tổng kết](PUBLIC_TEST_NEXT50_SUMMARY.md) nêu hai lỗi: trùng dòng header hóa đơn 47.000 đồng và chuẩn hóa tên không đồng nhất làm 151 ly thành 0. Một nhãn trả hàng bị audit bỏ sót đã được đính chính trước khi chốt; giữ snapshot audit cũ. [Completion checks](../artifacts/public_next50_16w/completion_checks.json) xác nhận 50 ID mới không trùng 50 cũ, đủ checkpoint/trace, runtime không đổi, 97 hash nguồn/metadata không đổi. [Prediction 50 câu](../result/public_test_next50_16w.jsonl), [ghép 100 ID](../result/public_test_100_collected.jsonl); file ghép có nhiều phiên bản và giữ nguyên lỗi lịch sử. Chưa báo PA chính thức hoặc sửa solver theo batch mới này.

**Sửa cơ chế dùng chung:** [báo cáo debug](GENERALIZED_REASONING_FIXES.md), **149 tests qua**, 17 kết quả đúng trước đó không đổi khi replay, ba câu lỗi gọi model mới đều khớp audit: 27 xe, 400.700.000 đồng, `[8000, 4000]`. [Prediction mới](../result/public_generalized_fixes_live3_v2.jsonl), [kiểm tra cuối](../artifacts/generalized_fixes/final_verification.json). Không gắn ID/tên/đáp án vào runtime; chưa phải đánh giá business holdout hoặc PA chính thức.

**Audit 20 câu cuối:** [báo cáo từ dữ liệu gốc](PUBLIC_TEST_LAST20_REVIEW.md) ghi **17 khớp, 1 sai khác, 2 abstain**. Sai khác là đếm 30 chuỗi tên xe thay vì 27 biển số SH Mode. Hai abstain có giá trị đối chiếu 400.700.000 đồng dư nợ trả góp và `[8000, 4000]` đồng nợ thêm/trả nợ của Lê Sơn Trung. [Kiểm tra lần hai](../artifacts/public_last20_review/crosschecks.json) khớp cả ba trường hợp. [Verification](../artifacts/public_last20_review/verification.json) xác nhận 97 hash nguồn/metadata không đổi, code solver và hai JSONL 20/50 câu nguyên trạng. Không gọi API khi audit; chưa phải gold hoặc PA chính thức.

**20 câu public còn lại đã chạy inference mới:** hoàn tất 20/20 prediction, gồm 18 giá trị và 2 abstain. [Bảng prediction](PUBLIC_TEST_LAST20_PREDICTIONS.md), [JSONL 20 câu](../result/public_test_last20_trammay.jsonl), [JSONL ghép 50 ID](../result/public_test_50_collected.jsonl). Kiểm tra đủ ID/không trùng, đối chiếu input batch/trace, solver không đổi và 94 hash nguồn public không thay đổi: [completion checks](../artifacts/public_last20_trammay_extended/completion_checks.json). File 50 giữ 30 prediction baseline và 20 prediction mới, chưa phải benchmark đồng nhất phiên bản hoặc PA. Chi tiết hai abstain trong [báo cáo lượt chạy](PUBLIC_TEST_LAST20_RUN.md).

**Debug tiếp 20 câu:** [báo cáo mới](PUBLIC_TEST_NEXT20_RERUN.md), [đính chính audit A.An](../artifacts/public_next20/audit_correction.json). Sau sửa: 20/20 replay khớp, [127 tests](../artifacts/public_next20/rerun_after_debug/pytest_final.xml) không failure/error. Replay chạy toàn bộ Solver với plan lưu sẵn; không gắn nhãn thành inference model mới.

**Cập nhật sau sửa normalization:** [115 tests](../artifacts/normalization_v2/pytest_final_1_2_1.xml) không failure/error; đủ 30 business train và 14 public, 502 bảng, 5.300.777 bản ghi; 252 hash nguồn khớp ZIP. [Manifest cuối](../artifacts/normalization_v2/manifest.json) không còn column parse error/diagnostic mức error. [Bản sửa](NORMALIZATION_FIXES.md), [chỉ mục dữ liệu](NORMALIZED_DATA.md), [20 câu tiếp theo](PUBLIC_TEST_NEXT20_REVIEW.md). Phần dưới giữ kết quả baseline trước sửa để truy nguyên.

Các kết quả dưới đây là kiểm thử code, ingestion và một lần tích hợp API nhỏ. Chưa có benchmark PA trên toàn train, business holdout hoặc leaderboard.

## Kiểm thử và chất lượng code

- **100 tests passed**, không có failure/error: [JUnit XML](../artifacts/pytest.xml).
- Black kiểm tra định dạng; Pyflakes kiểm tra import/tên biến.
- Môi trường đã chạy: Python 3.12.0, httpx 0.28.1, openpyxl 3.1.5, pydantic 2.13.0, python-dotenv 1.2.2, pytest 9.1.1.

Regression bao gồm: header sau tiêu đề, nhiều sheet, kế thừa nhóm không làm lây khách sang đơn mới, giữ mã có số 0 đầu, dedup theo đơn, giữ ledger trùng, tiền dạng `k`/`tr`, locale từ profile, đơn vị có chữ và số, ngày serial Excel, ngày tiếng Việt, biên tháng nhuận, ordered list/alias/table, SQL read-only, timeout, result limit, repair mã mới nhất, probe có quota, calibration tie, coverage và làm tròn tiền.

Các fixture nghiệp vụ kiểm tra doanh thu 380.000, tỷ lệ hủy 20%, top hàng theo số lượng cơ bản, công nợ 125.000 tại cutoff đầu và 75.000 ở cutoff sau, lãi gộp 111.000. Những số này thuộc dữ liệu tự tạo, không phải benchmark độ chính xác train.

## Dữ liệu train

[Báo cáo hoàn chỉnh](../artifacts/ingestion_validation_complete.json) ghi:

| Kiểm tra | Kết quả |
|---|---:|
| Doanh nghiệp | 30 |
| Bảng/sheet vật lý | 339 |
| Bản ghi giữ lại sau phân loại và dedup theo grain | 3.987.126 |
| File nguồn đối chiếu hash | 158 |
| Hash nguồn thay đổi | 0 |
| Diagnostic `PARSE_ERROR` | 0 |
| Diagnostic severity `error` | 0 |
| Dòng tổng loại có source row/reason | 17 |
| Dòng ghi chú loại có source row/reason | 46 |
| Header hoặc số dòng vật lý lệch EDA gốc | 0 / 339 |

158 file gồm dữ liệu doanh nghiệp và README; được đối chiếu với ZIP và kiểm tra lại sau sửa classifier cuối bảng. Toàn bộ table đều đạt phương trình hạch toán `raw = kept + excluded + duplicates`.

Phương pháp kiểm chứng gồm: full pass 30 business; [audit raw tail của cả 339 bảng](../artifacts/export_tail_audit.json); đọc lại hai business có ảnh hưởng; sau đó đối chiếu header và số dòng vật lý với báo cáo EDA cũ dùng reader riêng. Cache revision của từng business được ghi rõ; không mô tả các bước này như một lần chạy đơn nhất cùng revision.

Sửa cuối cùng ngăn dòng “Cộng” đứng trước “Ghi chú: …” bị kế thừa mã/trạng thái của đơn cuối. Regression đồng thời giữ sản phẩm có tên giống nhãn tổng khi vẫn có bằng chứng giao dịch. Không dùng business ID để quyết định cách tính đáp án.

Không có parse error chỉ chứng minh parser xử lý được các cột đã nhận diện trên dữ liệu này; chưa chứng minh mọi semantic mapping, join hoặc SQL của model đều đúng.

## Context cho planner

[Kiểm tra budget](../artifacts/context_budget_validation.json) dựng context cho một câu thực của mỗi business, hoàn toàn tại máy:

- 30/30 context nằm trong giới hạn **65.000 ký tự**, tính cả system prompt.
- Context lớn nhất: **64.846 ký tự**.
- Không gọi API và không đọc reference answers trong kiểm tra này.
- Các partition cùng schema dùng chung mô tả cột, vẫn giữ tên bảng vật lý, coverage, đơn vị và diagnostic.
- Validator chỉ bỏ yêu cầu một partition khi ngày ghi nhận chứng minh nó không thể đóng góp; không cắt toàn bộ các nguồn theo `as_of_date`.

Đây là kiểm tra xây context của 30 câu, không phải bảo đảm mọi câu hỏi đều vừa mọi model/token window.

## Trammay và kiểm thử xuyên suốt

`GET /v1/models` trả về model ID **`ag/gemini-3.8-flash-high`**. [Health check](../artifacts/api_health.json) với yêu cầu JSON tự tạo đã thành công.

Lần thử ngữ cảnh dài đầu tiên gặp timeout và hệ thống ghi abstain. Sau khi chỉnh phần hướng dẫn định dạng, lần thử thành công trả **64 đơn hủy** cho `q_00eadfc5c599`, qua một lượt model:

- [Prediction JSONL](../result/live_verified.jsonl).
- [Báo cáo scorer độc lập](../artifacts/live_verified/score.json): **1 correct / 1 question**.
- [Runtime report](../artifacts/live_verified/runtime_report.json): khoảng 64,64 giây cho câu, provider báo 19.931 tokens.
- [Replay bằng code hiện tại](../artifacts/live_replay.json): thực thi lại plan đã nhận, vẫn đúng, không gọi mạng.

Replay không phải một dự đoán model mới. PA = 1 trên một câu tích hợp không đại diện cho PA holdout. Reference chỉ được đọc sau inference ở phía scorer, không được đưa vào prompt.

[Smoke test không có model](../artifacts/offline_smoke/runtime_report.json) ghi 3 abstain, **0 model calls**, kiểm tra được đường checkpoint khi backend vắng mặt. Nó chưa kiểm chứng một model offline thực.

## Các giới hạn còn lại

- [Debug nguyên nhân gốc của 10 câu public](PUBLIC_TEST_FIRST10_DEBUG.md) đã tái hiện bốn trường hợp có vấn đề bằng plan lưu sẵn, không gọi API. Đã cô lập lỗi locale/dòng tổng, grouping theo tên, nhánh unsupported và metadata/SELECT *. SQL đổi sang cột công khai trả lại 71.724 cho câu trung bình đơn; database gốc không đổi hash. Đây là chẩn đoán, chưa phải bản sửa solver.
- Chưa fit gate trên tập prediction holdout đủ lớn; gate mặc định không gán xác suất giả.
- SQL được thực thi và đúng shape vẫn có thể sai nghiệp vụ. Cần scorer/holdout để đo điều đó.
- Chưa triển khai arbitrary Python execution, SFT hoặc các ensemble tùy chọn.
- Chưa chọn/bundled weights offline, benchmark GPU/VRAM hoặc xác nhận runtime toàn bộ private test trong 6 giờ.
- Dockerfile hiện tại là môi trường development; không được coi là image dự thi cuối.
- Public test đã chạy thử qua API; đợt 50 câu đang có checkpoint. [Đối chiếu 10 câu hoàn thành đầu tiên](PUBLIC_TEST_FIRST10_REVIEW.md): 6 khớp phép tính độc lập từ file gốc, 2 sai giá trị, 2 abstain. Public test không có gold nên chưa có PA chính thức. Hai lỗi giá trị liên quan đến đọc tiền/dòng tổng ngày và tách cùng mã hàng theo cách viết tên; code solver của đợt chạy hiện tại chưa được sửa các lỗi này.

Tất cả thay đổi code của lần triển khai nằm trong `source_test/`. File kiến trúc gốc giữ SHA-256 `8d991aee63944a5a8215f9fd4afa958cf6f18dcc9c8cb017326c8eceff698c5a`. `.env` không được sửa; bản build không chứa `.env` hoặc dữ liệu doanh nghiệp.
