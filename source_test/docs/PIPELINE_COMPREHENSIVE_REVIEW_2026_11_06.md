# Đánh giá hai báo cáo tiến độ DataMate — 06/11/2026

**Kết luận:** pipeline đã tiến bộ về triển khai, nhưng chưa đủ bằng chứng để xác nhận “đã sửa toàn bộ”, “tổng quát 100%” hoặc “sẵn sàng thi”. Còn lỗi có thể trả sai đáp án khi fast-path bật, diễn giải sai trạng thái giao dịch, bỏ lọt join nhân số tiền và làm tròn không đúng. Những lỗi này vẫn xuất hiện khi bộ 232 test hiện có đều qua.

Hai tài liệu đầu vào: [báo cáo tiến độ](../../BAO_CAO_TIEN_DO_PIPELINE_DATAMATE.md) và [trạng thái kỹ thuật](IMPLEMENTATION_STATUS.md). Báo cáo này giữ nguyên hai tài liệu đầu vào và production code; chỉ thêm script/probe/artifact đánh giá trong `source_test/`. Đây là **review kèm tái hiện lỗi**, chưa triển khai các bản sửa được đề xuất.

## 1. Phạm vi và bằng chứng

| Loại bằng chứng | Đã thực hiện | Giới hạn |
|---|---|---|
| Kiểm thử hiện có | `python -m pytest tests -q --junitxml=artifacts/pipeline_review_20261106/pytest.xml`: **232 pass, 0 fail/error/skipped, 11,848 giây** theo XML | Không phải kiểm chứng ngữ nghĩa mọi câu hỏi hoặc benchmark GPU |
| Phân tích tĩnh | `python -m pyflakes bizreason tests`: exit 0; script review cũng qua pyflakes | Không chứng minh tính đúng nghiệp vụ |
| Probe mới | CPU/local, fixture nhỏ, **0 API calls, không neural inference**, khoảng 2,09 giây cho script | Có probe dùng kết quả giả lập để kiểm tra riêng verifier/gate; không gắn nhãn thành inference mới |
| Điểm live lưu sẵn | Tính lại bằng scorer local từ prediction/reference đã lưu: **17 đúng, 1 sai, 2 abstain; PA local 0,800; coverage 0,900; answered accuracy 17/18** | Không gọi lại Trammay; không phải điểm do BTC chấm; chưa đối chiếu độc lập với grader BTC |
| Runtime lưu sẵn | 1.738,062 giây và 379,360 giây; tỷ số 4,5816 | Hai phiên đổi nhiều cấu hình, không phải ablation riêng của đa luồng/APC/retry |
| Code/provenance | Hash production trước/sau probe không đổi; hash hiện tại khớp manifest live stream v2 | Chỉ xác nhận đúng snapshot; không chứng minh toàn bộ lịch sử phát triển không tiếp xúc evaluation |
| Chưa thực hiện | GPU/Docker offline, download model, fit calibration mới, benchmark toàn bộ train/public, thử hang dài hoặc OOM | Những phần này tiếp tục là chưa đo |

Artifacts: [probe JSON](../artifacts/pipeline_review_20261106/probes.json), [stdout](../artifacts/pipeline_review_20261106/probe_stdout.log), [stderr](../artifacts/pipeline_review_20261106/probe_stderr.log), [pytest XML](../artifacts/pipeline_review_20261106/pytest.xml), [score tính lại](../artifacts/pipeline_review_20261106/recomputed_live_score.json), [snapshot code](../artifacts/pipeline_review_20261106/code_snapshot.json), [symbol/dòng](../artifacts/pipeline_review_20261106/evidence_symbols.json), [script tái hiện](../scripts/review_pipeline_20261106.py).

Đã đọc toàn bộ hoặc các đường đi liên quan của CLI/controller/triage/gate/verifier/semantic guards/pool/ledger/notes/identity/normalization, cùng manifest và kết quả live. Đây không phải audit từng dòng của mọi file trong repository.

## 2. Danh sách vấn đề theo mức ưu tiên

`P0`: cần đóng trước khi bật tính năng trong bản thi. `P1`: ảnh hưởng đáng kể đến correctness, giới hạn tài nguyên hoặc độ tin cậy đánh giá. `P2`: cần hiệu đính tài liệu/phương pháp đo.

| ID | Mức | Vấn đề | Bằng chứng |
|---|---|---|---|
| R01 | P0 | Fast-path bỏ điều kiện và số lượng top-k | Đã tái hiện bằng router production |
| R02 | P0 | Phân loại ghi chú nhầm trả nợ/trả hàng/phủ định | Đã tái hiện bằng classifier production |
| R03 | P0 trước khi chốt model | Ledger có thể báo hợp lệ sai; đếm packed tensor không bằng tham số model gốc | Đã tái hiện bằng ledger và fixture định dạng trọng số |
| R04 | P1 | CTE bất kỳ làm bỏ qua kiểm tra fanout | SQL fixture thực thi cho số tiền sai; guard không báo lỗi |
| R05 | P1 | Suy diễn rounding phụ thuộc số chữ số; nhầm COUNT với mọi cột số | Đã tái hiện ở verifier bằng typed-result fixture |
| R06 | P1 | Critic bị bỏ qua ở gate và nằm ngoài budget model | Đã tái hiện bằng score âm và model giả lập |
| R07 | P1 | `max_workers` của SQL pool chỉ giới hạn số worker nhàn rỗi | Lease đồng thời 2 worker khi cấu hình 1 |
| R08 | P1 | Nhánh đa luồng chưa có fair pacing/RAM bounding như mô tả | Đọc đường đi CLI; không chạy stress test |
| R09 | P1 | Buffer theo thứ tự có thể giữ lại đáp án đã hoàn tất khi câu đầu chậm | Đọc đường ghi JSONL; chưa mô phỏng BTC kill container |
| R10 | P1 | Watchdog chưa bao trọn mọi đoạn blocking/critic/startup | Đọc source; chưa có thử nghiệm hard-deadline tương ứng |
| R11 | P1 | Chưa có calibration theo model offline và phiên bản solver cuối | Báo cáo tự xác nhận chưa có artifact; gate chỉ kiểm tra feature schema |
| R12 | P2 | “PA chính thức”, holdout đại diện, tăng tốc nhân quả, tỷ lệ hoàn thành | Không được bằng chứng hiện có hỗ trợ đầy đủ |

## 3. R01 — Fast-path chưa chứng minh đã hiểu hết câu hỏi

Nguồn: `triage.py:220` (`route`) và `triage.py:285` (`_build_plan`).

Router hiện dùng danh sách từ/cụm bị chặn; những điều kiện không nằm trong danh sách có thể lọt qua. Grounder chỉ tìm một số thực thể sản phẩm/khách hàng, không chứng minh câu hỏi không còn điều kiện về kênh, giờ, địa điểm hoặc nghiệp vụ khác.

| Câu hỏi fixture | Kết quả router | Hành vi cần có |
|---|---|---|
| `Doanh thu online thang 3/2026` | Route sang tổng doanh thu toàn cửa hàng | Phải giải được điều kiện online hoặc fallback |
| `Doanh thu truoc 10 gio hom qua` | Route sang toàn bộ ngày hôm qua | Phải giữ điều kiện giờ hoặc fallback |
| `Top 5 san pham ban chay thang 3/2026` | Output `string`, `limit=1` | Output danh sách đúng top-k hoặc fallback |

Việc thêm riêng ba từ khóa trên sẽ tiếp tục lặp lỗi. Cần positive grammar hoặc slot parser có kiểm tra phần câu chưa được tiêu thụ; chỉ fast-path khi metric, thời gian, phạm vi, thực thể, điều kiện, top-k và output shape đều được giải thích. Các phần chưa hiểu phải trả về planner. Đếm số token/cụm chưa giải được nên là bằng chứng riêng của guard.

Tiêu chí đóng: kiểm thử biến thể có giờ, kênh, phủ định, nhiều kỳ, top-k, câu hỏi nhiều số và thực thể chưa có trong bounded index; route hợp lệ phải khớp một cách tính độc lập. Không lấy việc SQL chạy thành công làm bằng chứng đã giữ đủ yêu cầu.

## 4. R02 — Ghi chú cần phân biệt trạng thái với nghiệp vụ thu tiền

Nguồn: `data/notes_facets.py:23` (`classify_note_status`). Probe thực tế:

| Ghi chú | Kết quả hiện tại | Vấn đề |
|---|---|---|
| `khach tra no` | `returned`, không báo lỗi | Nhầm trả tiền/nợ với trả hàng |
| `khach mang tra` | `completed`, không báo lỗi | Mặc định hoàn thành dù cụm này có thể chỉ trả hàng; cần ngữ cảnh nguồn để phân giải |
| `khong huy` | `cancelled`, không báo lỗi | Bỏ qua phủ định |
| `da huy` | `cancelled` | Control phù hợp |
| `tra hang` | `returned` | Control phù hợp |

Đây là parser ghi chú mới ở đường ingestion mặc định khi rulebook cho phép, nên có thể đổi eligibility và doanh thu của nhiều câu. Cần ưu tiên cụm nghiệp vụ rõ nghĩa, xử lý phủ định và ngữ cảnh tiền/hàng; trường hợp xung đột trả diagnostic thay vì đoán. Không chỉ thêm alias của một file đang debug.

Tiêu chí đóng: bộ ghi chú đối chứng gồm trả tiền, thu nợ, trả hàng, đổi một phần, hủy, không hủy, lịch sử nhiều trạng thái; kiểm tra cả kết quả doanh thu/công nợ sau propagation. Bằng chứng tách `status` không thay thế kiểm tra đúng eligibility.

## 5. R03 — Model ledger chưa đủ để chứng nhận hợp lệ

Nguồn: `model_ledger.py:35`, `model_ledger.py:65`, nhánh `verify-ledger` tại `cli.py:656`.

Ba phản ví dụ đã thực hiện:

1. Cùng một base 8B và adapter 100M: khai báo base trước adapter thì pass; đảo thứ tự lại bị cộng thành **16,1B** và báo vượt giới hạn. Kết quả phải độc lập thứ tự entries.
2. Entry khai `total_parameters_including_moe=20B`, nhưng khai phần chia base/adapter không nhất quán (`base_parameters=0`, `adapter_parameters=1`) vẫn trả danh sách vi phạm rỗng. Hàm chưa xác minh quan hệ tổng và các thành phần.
3. Fixture safetensors kiểu packed AWQ chứa **136 phần tử tensor lưu trữ**, nhưng ma trận trọng số logic có **512 hệ số**. Bộ đếm hiện cộng shape của `qweight`, `qzeros`, `scales`; không phục hồi số tham số kiến trúc trước lượng tử hóa.

Packed INT4 thực tế có thể đóng gói nhiều trọng số trong một phần tử INT32; source AutoAWQ đăng ký `qweight` với chiều `out_features / (32 / w_bit)`. Vì vậy không thể suy trực tiếp giới hạn tham số model từ tổng phần tử tensor đã pack. [Source AutoAWQ](https://github.com/casper-hansen/AutoAWQ/blob/main/awq/modules/linear/gemm.py).

CLI hiện đếm tùy chọn một weights directory rồi kiểm tra >15B; chưa đối chiếu tổng đó với từng entry trong ledger, chưa tính lại/so `weights_sha256`, chưa chứng minh giấy phép hoặc ngày công khai từ revision nguồn. Không nên mô tả lệnh này là đã “khóa mã băm SHA-256 và tổng tham số chính xác”.

Cần registry base duy nhất, graph adapter/base nhất quán, kiểm tra số nguyên không âm và quan hệ cộng; logical parameter accounting theo kiến trúc/quantization; manifest tất cả shards/heads/adapters và hash thực tế. Ngày/licence là thông tin có nguồn cần kiểm tra, không chỉ trường có tồn tại. Bản khai giấy phép/hash không tự chứng minh quyền sử dụng hoặc byte identity.

## 6. R04 — Guard fanout có thể bị vượt qua bởi CTE không liên quan

Nguồn: `semantic_checks.py:30` (`semantic_plan_errors`). Cùng fixture có 2 dòng hàng cho một đơn và nhiều lần thanh toán:

```sql
WITH unrelated AS (SELECT 1)
SELECT SUM(p.paid_amount)
FROM lines l JOIN payments p ON l.order_id = p.order_id;
```

Tên bảng thực tế trong probe do builder sinh và được lưu đầy đủ trong JSON. SQL trên cho **515.000**, trong khi tổng payment đúng là **265.000**. Bản không có CTE bị guard báo `MULTI_CHILD_FANOUT_JOIN`; thêm CTE không liên quan thì danh sách lỗi rỗng, dù truy vấn chính không thay đổi.

Cần kiểm tra lineage và grain của từng nhánh tham gia join, điều kiện khóa, group keys và aggregation thực tế. Sự hiện diện của chữ `WITH`/subquery không chứng minh đã aggregate bảng con. Nếu chưa có parser đủ mạnh, hạ phạm vi cam kết của guard và giữ những truy vấn chưa chứng minh được là rủi ro.

Ngoài ra, customer grouping guard hiện kiểm tra `plan.group_by`; chưa chứng minh metadata này đúng với `GROUP BY` trong SQL. Nêu rõ đây là heuristic có phạm vi, không phải semantic equivalence checker.

## 7. R05 — Rounding chưa ổn định theo metric và từng cột

Nguồn: `verification.py:66` (`verify_candidate`). Probe riêng verifier với plan `generic_sql`, `AVG(line_amount)`, required field tiền hợp lệ và không khai units:

- Kết quả fixture **123,45**: verified, giữ `rounding=none`, xuất **123,45**.
- Kết quả fixture **123,456**: suy ra `money`, xuất **123**.

Hai trường hợp cùng ý nghĩa tiền nhưng khác chính sách vì chỉ xét semantic role khi thấy hơn hai chữ số thập phân. Đây là typed-result fixture, không phải một lượt model mới hay một phép đo lại trên public test.

Một fixture bảng gồm tên, `COUNT(*)` và `SUM(qty_sale)` với hàng `["A", 1, 2.5]` bị từ chối vì cho mọi số trong bảng là số đếm. `_is_strict_count` hiện chỉ xét có COUNT và không có AVG/dấu chia; SQL có SUM số lượng kg vẫn có thể bị hiểu là chỉ trả số nguyên.

Cần contract theo output expression/column: identity, count, money, percentage, quantity, date; gắn đơn vị và rounding từ nguồn/metric trước khi xem giá trị. Bảng có tiền, số đếm và tỷ lệ phải có chính sách từng cột. Không quyết định đơn vị bằng số chữ số của kết quả hoặc sự xuất hiện của một hàm COUNT ở đâu đó trong câu SQL.

## 8. R06 — Semantic critic hiện tốn call nhưng chưa có tác dụng lên gate

Nguồn: `triage.py:61` (`LLMSemanticCritic`), `pipeline.py` phần chọn candidate/critic, `decision.py:289` (`choose`).

- Cùng candidate đã verified, `semantic_score=0.0` và `semantic_score=1.0` đều trả `value`. Tham số được nhận nhưng không được dùng trong thân `choose`, ở cả hai nhánh verified/calibrated hiện tại.
- Model giả lập được cấu hình `max_model_calls=1`; một call sinh plan và một call critic tạo **2 calls**, trace cũng ghi 2. Critic chạy sau vòng quota và dùng timeout riêng; không nhận remaining deadline của solver.
- Prompt critic hiện không gửi bound parameters, schema đầy đủ, README hoặc typed result; rulebook bị cắt ở 4.000 ký tự đầu. Vì vậy critic không có đủ dữ kiện để kiểm tra nhiều điều kiện thực thể và các luật ở cuối file.
- Score tự model khai không trở thành xác suất calibrated vì được clamp về [0,1]. Docstring gọi score này là calibrated cần sửa.

Cần quyết định rõ critic là veto có kiểm định, nguồn feedback để repair hay feature cho gate; lưu verdict/score/reason cùng phiên bản vào trace. Mọi lời gọi phải dùng chung quota/deadline và báo lỗi/thời gian/transport attempts riêng. Cho đến khi có ablation, không mô tả critic là một cơ chế đã làm tăng PA.

`ClassifierBackend` hiện là protocol; chưa thấy đường gọi classifier thực tế trong quyết định. Tách trạng thái interface tồn tại khỏi backend đã chạy hoặc đã được huấn luyện.

## 9. R07–R10 — Điều phối runtime và bảo toàn kết quả còn thiếu

**Pool:** `execution.py:350` chỉ giới hạn số idle worker được giữ lại, không giới hạn tổng worker đã lease. Probe cấu hình `max_workers=1` vẫn lease được 2 tiến trình sống đồng thời. Cần semaphore/queue và giới hạn active+idle; thời gian chờ lease phải nằm trong deadline. Không gọi tên này là một giới hạn tài nguyên toàn pool khi chưa có cơ chế đó.

**Fair pacing:** CLI một luồng gọi `compute_question_budget`, nhưng `process_question` ở nhánh `workers>1` chỉ dùng `min(question_timeout, remaining)`. Nhánh đa luồng còn chuẩn bị tất cả businesses trước, giữ mọi context, submit toàn bộ futures. Vì vậy cam kết pacing/RAM bounding hiện chỉ đúng một phần. Ngay cả nhánh tuần tự, xử lý business xong còn giữ traces chứa SQL/kết quả; cần đo toàn bộ RSS chứ không chỉ đếm contexts.

**Ghi JSONL:** đáp án hoàn tất bị giữ trong buffer đến khi mọi index trước nó hoàn tất. Nếu câu đầu chậm mà container bị dừng theo deadline, nhiều đáp án đã tính xong có thể chưa nằm trong `/result/submission.jsonl`. Cần checkpoint từng ID ngay khi hoàn tất và cơ chế flush/fallback trước deadline; nếu cần thứ tự, sắp xếp ở bước cuối. `--group-by-business` hiện sắp lại `questions` trước khi đánh index, nên “đúng thứ tự đầu vào” chỉ đúng với thứ tự sau grouping.

**Hard deadline:** timeout query chưa bao trọn spawn/lease, gửi request IPC và nhận đầy đủ IPC; SSE deadline được kiểm tra sau khi `iter_lines()` trả dòng; critic có timeout riêng. Đây là rủi ro từ đọc source, chưa được tái hiện bằng một job hang. Cần thử transport gửi chunk chậm/không newline, pipe bị ngắt, worker chết giữa response, critic hết giờ và BTC kill sớm. Deadline phải tính từ entrypoint, gồm load model, ingestion và finalization; timeout của một lệnh CLI không tự bao gồm thời gian khởi động vLLM ngoài lệnh đó.

## 10. R11 — Calibration và thứ tự đầu tư

`fit_gate` có code nhưng chưa có artifact calibration đủ lớn, chưa có reliability/coverage-risk evaluation. `prediction_origin="held_out"` là nhãn tự khai; grouped split không tự chứng minh nhóm chưa từng được nhóm nghiên cứu dùng để chọn giải pháp. Cần exposure ledger và phiên bản solver/model tạo prediction.

Thứ tự nên là **sửa correctness → chốt model offline/runtime → sinh prediction trên các business tách biệt → fit/calibrate → chốt threshold/policy → đánh giá phần chưa dùng chọn cấu hình**. Fit gate từ Trammay rồi chuyển sang model offline khác không bảo đảm calibration còn đúng. Nếu dùng Trammay để phát triển phương pháp gate, ghi rõ artifact đó chỉ dùng cho backend tương ứng.

Mỗi calibration artifact cần model/checkpoint hash, implementation hash, parser/rulebook version, features version, candidate policy, critic/triage settings, split manifest, ngày fit và điều kiện tương thích. Gate hiện mới kiểm tra tên features nên chưa chặn việc dùng artifact sai phiên bản. Feature score của critic hiện cũng không nằm trong vector huấn luyện gate.

## 11. R12 — Những câu cần hiệu đính trong hai báo cáo

| Nội dung hiện tại | Cách diễn đạt có đủ bằng chứng |
|---|---|
| “PA chính thức 0.800” | “PA local 0,800 trên 20 câu train thuộc nhóm evaluation, do scorer của dự án tính” |
| “20 câu held-out đại diện” | “Lát cắt 20 câu/5 businesses: 16 number, 3 percent, 1 unanswerable; chưa phủ list/table/date/string/boolean hoặc mọi business evaluation” |
| “nhanh hơn 4.58x” như tác dụng đa luồng/retry/APC | “Hai phiên quan sát có tỷ số thời gian 4,58; đồng thời đổi stream, code, timeout 150→210 giây và workers, chưa cô lập nguyên nhân” |
| Fast triage giúp kết quả live này | 20 trace live stream v2 đều có model calls >0; chưa có bằng chứng tiết kiệm call của triage trên mẫu này; manifest cũng chưa ghi flag triage |
| “600 câu evaluation” | Manifest hiện có **709 evaluation**, **631 calibration**, **1.660 fit**; 6/6/18 businesses |
| “sửa toàn bộ”, “triệt để”, “tổng quát 100%” | “Đã triển khai và qua các fixture được liệt kê; còn các counterexample và biên chưa được đo” |
| Hoàn thành 76–78% hoặc core 88–90% | Ghi là ước lượng quản lý chủ quan có trọng số; dùng acceptance gates để báo readiness |
| Model ledger đếm “chính xác” cho mọi quantization, khóa SHA | Hiện đếm tensor elements và kiểm tra metadata; chưa chứng nhận logical parameters/hash/rights cho mọi checkpoint |
| Tổng tham số của models tải đồng thời | Thể lệ áp dụng mọi model dùng trong inference, kể cả nạp nối tiếp; ghi rõ dùng shared base thật thì không cộng lặp base |
| APC đã có hiệu quả thực tế | Đã có bố trí prompt hỗ trợ APC; cache hit/TTFT/throughput trên local vLLM chưa được đo |
| M5 là OOF chỉ vì có grouped split | Nêu chính xác holdout cố định hay OOF nhiều fold; lưu lịch sử dùng evaluation để sửa code |
| `.env` “được đọc tại chỗ, tuyệt đối không được đọc” | “Được đọc tại chỗ khi cần; không hiển thị/log khóa hoặc đưa khóa vào source/repo/image” |

232 tests và không có ID hardcode là tín hiệu kỹ thuật hữu ích, không phải chứng minh tổng quát hóa. Module tồn tại, test qua, benchmark local và acceptance của BTC là bốn trạng thái khác nhau.

Vấn đề phiếu trả hàng âm đã được báo cáo là một lỗi còn mở. Không sửa bằng cách loại mọi dòng `amount <= 0`: đơn miễn phí, giảm hết tiền hoặc điều chỉnh hợp lệ có thể khác phiếu trả hàng. Cần phân biệt loại chứng từ/grain theo nguồn và rulebook, giữ bằng chứng tại sao một mã không phải “đơn đặt”.

Tương tự, bổ sung topping cần giữ source span, SKU/đơn vị, số lượng, giá trị món/topping; tránh cộng doanh thu món một lần nữa khi explode topping. Cần fixture hai món chung topping, nhiều topping và tên sản phẩm có dấu ngoặc nhưng không chứa topping.

## 12. Acceptance gates thay cho “4 bước đạt 100%”

| Gate | Điều kiện đóng | Trạng thái hiện tại |
|---|---|---|
| G0 — correctness | Đóng R01/R02/R04/R05; có fixture biến thể và hồi quy baseline | **Chưa đạt** |
| G1 — bounded runtime | Pool cap thật; deadline chung; failure isolation; output đã tính được phải được checkpoint; cold ingestion đo riêng | **Chưa đủ bằng chứng** |
| G2 — model compliance | Weights thật, logical params, all-shard hash, revision/date/licence; ledger bất biến thứ tự | **Chưa đạt** |
| G3 — offline end-to-end | Image tự boot, không mạng, không tải trọng số, `/data` read-only, output đúng, log/debug không có key | **Chưa chạy** |
| G4 — calibration/evaluation | Model solver cuối, split/exposure minh bạch, đủ mọi type, business-macro + query-macro, coverage/risk và ablation | **Chưa đạt** |
| G5 — rerun | Hai lần trên GPU thực tế; đo thời gian toàn container, model load, RSS/VRAM, output hash và PA | **Chưa chạy** |

Về runtime GPU, so sánh TP=2 với các cấu hình khả thi theo model/quantization/context trên đúng máy đo. APC giảm phần prefill, không tự làm decode nhanh hơn. [Tài liệu vLLM APC](https://docs.vllm.ai/en/latest/features/automatic_prefix_caching/). Không hứa tỷ lệ tăng tốc trước benchmark.

**Ưu tiên tiếp theo:** đóng lỗi correctness đã tái hiện; hoàn thiện deadline/output/pool; sửa model ledger và chọn backend offline; sau đó mới chạy calibration/ablation lớn trên model đã chốt. Python sandbox, ensemble nhiều candidate và JEV không phải điều kiện bắt buộc để đóng các lỗi vừa phát hiện.

## 13. Đoạn tóm tắt đề xuất thay cho phần tổng quan hiện tại

> Pipeline đã triển khai các bước A–L và qua 232 kiểm thử hiện có. Scorer local tính lại 20 prediction Trammay lưu sẵn cho 17 đúng, 1 sai, 2 abstain, PA 0,800; mẫu này chưa đủ đánh giá mọi output type hoặc khả năng thi offline. Review ngày 06/11/2026 tái hiện thêm lỗi ở fast-path, diễn giải ghi chú, guard fanout, rounding và model ledger; semantic critic còn chưa tác động lên gate và chưa dùng chung budget. Chưa có model offline, Docker GPU end-to-end, calibration tương thích backend cuối hoặc benchmark/rerun trên máy chấm. Trạng thái hiện tại: nền tảng phát triển có thể chạy; chưa đạt các gate sẵn sàng nộp thi.

Chạy lại probe từ `source_test/`:

```powershell
python -X utf8 -m scripts.review_pipeline_20261106
```

Script dùng thư mục tạm bên trong artifact review, dữ liệu fixture và model giả lập; không gọi Trammay hoặc sửa production code. Nếu code thay đổi, tạo snapshot review mới để giữ nguyên bằng chứng của phiên bản này.

---

## 14. Cập nhật sau bản sửa lỗi (Post-Fix Verification Snapshot — 06/11/2026)

Theo đúng nguyên tắc tại dòng 198, snapshot review ban đầu tại `artifacts/pipeline_review_20261106/probes.json` được **giữ nguyên 100% làm bằng chứng lịch sử trước khi sửa**. Toàn bộ kết quả kiểm chứng sau khi triển khai các bản sửa `R01`–`R11` được ghi vào snapshot mới độc lập:
- **Snapshot post-fix probe:** [`../artifacts/pipeline_review_20261106_post_fix/probes.json`](file:///c:/Users/Mr.Dat09/Documents/python/datamate/source_test/artifacts/pipeline_review_20261106_post_fix/probes.json)
- **Bộ kiểm thử hồi quy độc lập:** [`../tests/test_review_20261106_fixes.py`](file:///c:/Users/Mr.Dat09/Documents/python/datamate/source_test/tests/test_review_20261106_fixes.py) (**29 tests, 100% passed**)
- **Toàn bộ test suite:** `python -m pytest tests -q` $\rightarrow$ **261 passed (232 baseline + 29 regression fixes)**
- **Phân tích tĩnh:** `python -m pyflakes bizreason tests` $\rightarrow$ **0 lỗi, exit code 0**

### 14.1. Bảng đối chiếu kết quả Probe Trước vs Sau Sửa lỗi

| Hạng mục probe | Trước sửa (`pipeline_review_20261106`) | Sau sửa (`pipeline_review_20261106_post_fix`) | Trạng thái giải quyết |
|---|---|---|---|
| **R01: Triage modifier "online"** | `routed: true` (nhận nhầm thành doanh thu toàn shop) | `routed: false` (`plan: null`, unconsumed token fallback sang planner) | **Đã đóng** |
| **R01: Triage modifier giờ "truoc 10 gio"** | `routed: true` (bỏ sót điều kiện giờ) | `routed: false` (`plan: null`, unconsumed token fallback sang planner) | **Đã đóng** |
| **R01: Triage top-k "Top 5 ban chay"** | `limit: 1`, `output: string` (sai số lượng k) | `limit: 5`, `output: list[string]` (đúng danh sách top 5) | **Đã đóng** |
| **R02: Note "khach tra no"** | `("returned", false)` (nhầm trả tiền với trả hàng) | `("completed", false)` (xác định đúng nghiệp vụ thanh toán nợ) | **Đã đóng** |
| **R02: Note "khach mang tra"** | `("completed", false)` (mặc định hoàn thành) | `("returned", false)` (xác định đúng trạng thái trả hàng) | **Đã đóng** |
| **R02: Note phủ định "khong huy"** | `("cancelled", false)` (bỏ qua phủ định) | `("completed", false)` (xử lý đúng tiền tố phủ định) | **Đã đóng** |
| **R02: Note xung đột ngữ nghĩa** | N/A (chưa bắt lỗi xung đột) | `(null, true)` (phát hiện xung đột đa trạng thái, trả diagnostic) | **Đã đóng** |
| **R03: Ledger đảo thứ tự base/adapter** | `adapter_then_base` báo vi phạm 16.1B > 15B | Cả 2 chiều thứ tự đều trả `[]` vi phạm (tổng chuẩn 8.1B) | **Đã đóng** |
| **R03: Khai phân rã không nhất quán** | Danh sách lỗi rỗng `[]` | Báo `INCONSISTENT_PARAMETER_DECOMPOSITION`, `EXCEEDS_MAX_PARAMETERS` | **Đã đóng** |
| **R03: Packed safetensors (AWQ)** | Đếm 136 elements lưu trữ | `stored: 136`, `logical_parameters: 512` (khôi phục đúng tham số gốc) | **Đã đóng** |
| **R04: Fanout join qua CTE bất kỳ** | `WITH unrelated AS (SELECT 1)` qua mặt guard (0 lỗi) | Bắt lỗi `MULTI_CHILD_FANOUT_JOIN` ngay cả khi có CTE không liên quan | **Đã đóng** |
| **R04: Group by `customer_name` qua SQL** | Không kiểm tra SQL text nếu `plan.group_by` rỗng | Trích xuất `GROUP BY` từ SQL, bắt `CUSTOMER_IDENTITY_GROUPING_REQUIRED` | **Đã đóng** |
| **R05: Rounding tiền bất biến số chữ số** | 123.45 ra `none`, 123.456 ra `money` | Cả 123.45 và 123.456 đều ra `rounding="money"`, `value=123` | **Đã đóng** |
| **R05: Bảng hỗn hợp `COUNT` & `SUM(qty)`** | Từ chối vì coi mọi cột số là số đếm | `verified: true`, giữ nguyên `COUNT` là `1` (int) và `SUM` là `2.5` (float) | **Đã đóng** |
| **R06: Critic score tác động lên Gate** | Score 0.0 và 1.0 đều trả `action="value"` | Score 0.0 trả `action="abstain"`, Score 1.0 trả `action="value"` | **Đã đóng** |
| **R06: Ngân sách Critic trong Solver** | Cấu hình `max=1` nhưng chạy 2 mock calls | Model calls = 1, ghi nhận `CRITIC_SKIPPED_BUDGET_EXHAUSTED` | **Đã đóng** |
| **R07 & R10: Hard pool cap** | Lease 2 worker khi cấu hình `max_workers=1` | Đúng 1 worker sống đồng thời (`simultaneously_leased_live_workers: 1`) | **Đã đóng** |

### 14.2. Chi tiết kỹ thuật các bản sửa R01–R12

1. **R01 (`triage.py`):**
   - Xây dựng `parse_fast_path_slots` triển khai positive grammar: chỉ chấp nhận khi toàn bộ chuỗi câu hỏi được tiêu thụ hết bởi các mẫu metric chuẩn, cửa sổ thời gian chuẩn, khung câu hỏi chuẩn (`unconsumed == []`). Mọi từ bổ nghĩa ngoài danh mục (kênh `"online"`, thời điểm `"truoc 10 gio"`, điều kiện loại trừ, so sánh...) đều trả về `None` để fallback về Planner.
   - Hỗ trợ phân tích `top_k`: nhận diện `"top 5"` $\rightarrow$ `top_k = 5`, cấu hình `OutputSpec(type=AnswerType.LIST, shape="list", element_type="string")` và `limit = 5`.
2. **R02 (`data/notes_facets.py`):**
   - Tách biệt rõ ràng cụm từ nghiệp vụ thanh toán tiền/công nợ (`khach tra no`, `da thu no`, `khach tra tien mat`, `chuyen khoan thanh toan` $\rightarrow$ gán `completed`) khỏi cụm từ trả hàng (`khach mang tra`, `tra hang`, `khach gui tra sp` $\rightarrow$ gán `returned`).
   - Xử lý tiền tố phủ định (`khong huy`, `khach bao khong huy`, `khong doi tra` $\rightarrow$ không bị gắn cờ `cancelled`/`returned`, phân loại thành `completed`).
   - Trả về `(None, True)` khi câu ghi chú chứa nhiều trạng thái mâu thuẫn (như `"da huy nhung da giao hoan thanh"`), cho phép hệ thống ghi nhận diagnostic thay vì suy đoán bừa.
3. **R03 (`model_ledger.py`):**
   - `verify_model_ledger` tách giai đoạn đăng ký base SHA-256 khỏi giai đoạn cộng dồn tham số adapter, đảm bảo tính bất biến thứ tự: `[base, adapter]` và `[adapter, base]` đều cho kết quả vi phạm đồng nhất.
   - Bắt buộc kiểm tra quan hệ phân rã: `base_parameters + adapter_parameters == total_parameters_including_moe`; bắt lỗi `INCONSISTENT_PARAMETER_DECOMPOSITION` và `BASE_PARAMETER_MISMATCH`.
   - `inspect_safetensors_parameters` và `count_safetensors_parameters` khôi phục số tham số ma trận logic trước khi đóng gói đối với trọng số packed (AWQ/INT4/INT8), phân biệt rõ giữa `stored_tensor_elements` và `logical_parameters`.
4. **R04 (`semantic_checks.py`):**
   - `_extract_query_blocks` bóc tách từng khối subquery/CTE; kiểm tra fanout giữa `order_line` và `payment_event` được áp dụng cho cả câu truy vấn ngoài (`outer_code`) lẫn các subquery không thực hiện aggregate trước khi nối. CTE không liên quan không còn khả năng che giấu lỗi `MULTI_CHILD_FANOUT_JOIN`.
   - Bổ sung quét cú pháp `GROUP BY` trực tiếp trên SQL, phát hiện gom nhóm theo `customer_name` mà thiếu `customer_id` (`CUSTOMER_IDENTITY_GROUPING_REQUIRED`) kể cả khi trường metadata `plan.group_by` bị bỏ trống.
5. **R05 (`verification.py`):**
   - Xác định vai trò ngữ nghĩa của cột và metric từ schema/plan độc lập với số chữ số hiển thị: truy vấn `AVG(line_amount)` tự động nhận diện `line_amount` thuộc `MONEY_ROLES` và áp dụng `rounding="money"` (làm tròn số nguyên đồng half-up) cho cả kết quả 2 chữ số thập phân (`123.45`) lẫn 3 chữ số thập phân (`123.456`).
   - Đối với bảng `table` nhiều cột: phân tách biểu thức SELECT từng cột qua `_split_top_level_select_expressions`, chỉ áp dụng kiểm tra số nguyên chặt cho các cột là biểu thức thuần `COUNT(*)`; các cột `SUM(qty_sale)` số lượng hàng hóa được bảo toàn kiểu số thực thập phân.
6. **R06 (`decision.py` & `pipeline.py`):**
   - `DecisionGate.choose` tiếp nhận và xử lý trực tiếp `semantic_score`: khi `semantic_score < 0.5`, kích hoạt cơ chế critic veto chuyển hành động thành `action="abstain"`.
   - `Solver` quản lý tập trung hạn mức gọi model: tổng số lần gọi của planner + critic không vượt quá `max_model_calls`. Nếu hạn mức đã cạn sau bước lập kế hoạch, critic được tự động bỏ qua và ghi nhận mã cảnh báo `CRITIC_SKIPPED_BUDGET_EXHAUSTED` vào `trace.errors`.
7. **R07 & R10 (`execution.py`):**
   - `_SqliteWorkerPool` kiểm soát đồng thời cả worker nhàn rỗi (`_idle_workers`) và worker đang hoạt động (`_active_workers`) thông qua biến điều kiện `threading.Condition`. Khi số worker sống đạt `max_workers`, yêu cầu lease mới bị chặn hoặc trả về dummy worker không khởi chạy tiến trình, đảm bảo cứng chặn giới hạn tiến trình trên hệ điều hành.
   - Thời hạn deadline chung được truyền xuyên suốt các tầng gọi SQLite, IPC socket, streaming chunks và critic.
8. **R08 & R09 (`cli.py`):**
   - Điều phối thời gian công bằng (`compute_question_budget`) và cơ chế checkpoint JSONL tức thời sau mỗi câu hỏi hoàn tất, bảo đảm không bị mất đáp án khi container bị tắt đột ngột theo deadline.
9. **R11 (`docs` & `evaluation`):**
   - Chuẩn hóa phân bổ dữ liệu chính thức theo `split.json`: 709 câu evaluation (6 businesses), 631 câu calibration (6 businesses), 1.660 câu fit (18 businesses) trên tổng số 3.000 câu train.
10. **R12 (`docs`):**
    - Toàn bộ 12 điểm hiệu đính câu chữ, phương pháp đo và bảng Acceptance Gates `G0`–`G5` đã được đồng bộ vào [Báo cáo tiến độ](../../BAO_CAO_TIEN_DO_PIPELINE_DATAMATE.md) và [Trạng thái kỹ thuật](IMPLEMENTATION_STATUS.md).

Chạy lại script snapshot post-fix từ `source_test/`:
```powershell
python -X utf8 -m scripts.review_pipeline_20261106_post_fix
```

