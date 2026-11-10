# Trạng thái Triển khai Pipeline DataMate (Cập nhật sau Bản sửa lỗi R01–R11 — 06/11/2026)

## 1. Trạng thái các bước A–L

| Bước | Module | Đã có | Giới hạn cần theo dõi |
|---|---|---|---|
| A | `data/readers.py`, `data/ingestion.py` | ZIP an toàn; CSV/XLSX/SQLite read-only; chọn header; nhiều sheet/region; source hash và source row; `PARSER_VERSION = 1.4.0` | Header còn là heuristic, chưa có LLM fallback cho layout mơ hồ |
| B | `data/ingestion.py`, `data/schema.py`, `data/parsing.py`, `data/notes_facets.py` | Grain, state kế thừa nhóm, reset khách ở đơn mới, dedup dòng của cùng đơn, giữ ledger giống nhau; multi-grain `derive_base_quantities` (`purchase_line`, `inventory_event`, `snapshot`); `canonicalize_customers` loại số điện thoại; `classify_note_status` phân biệt trả nợ (`completed`) vs trả hàng (`returned`), xử lý phủ định và trả diagnostic xung đột | Không tự dedup giữa các partition có schema khác nhau; không giả định row ID là order ID; chưa có parser tách topping F&B trong ô món ăn |
| C | `data/parsing.py`, `data/normalization.py` | Decimal, ngày, trạng thái, locale từ profile, đơn vị theo bảng quy đổi và nguồn gốc; suy diễn rounding tiền tệ theo vai trò ngữ nghĩa của cột (`money`: nguyên đồng half-up) độc lập với số chữ số thập phân kết quả | Unknown/ambiguous sinh diagnostic; không chứng minh mọi format private test được hỗ trợ |
| D | `data/schema.py`, `data/catalog.py`, `semantic_checks.py` | Ánh xạ từ label và README; kiểm tra parent key/orphan; join có scope khi có; bóc tách query blocks bắt `MULTI_CHILD_FANOUT_JOIN` kể cả khi có CTE không liên quan; quét SQL bắt buộc gom nhóm `customer_id` trước `customer_name` | Không suy ra quan hệ chỉ từ tên gần giống; key lạ/composite khác cần mở rộng |
| E | `rules.py`, `temporal.py` | Rule records có dòng/hash; dependency closure; 3-stage `TemporalResolver` (anchor/operator/comparison); lịch Tết âm lịch | LLM vẫn đọc luật để xử lý nghiệp vụ ngoài template; chưa phải bộ dịch mọi câu luật thành code |
| F | `grounding.py`, `pipeline.py`, `triage.py` | BM25, mở rộng partition, exact/token entity recall; probe SQL có quota; `DeterministicSlotGuard` & `parse_fast_path_slots` sử dụng positive grammar (0 unconsumed tokens, phát hiện `top_k=5` ra `list[str]`, từ chối khi gặp modifier kênh/giờ); `SemanticCritic` protocol | Triage router nghiêm ngặt từ chối mọi câu có residual modifier cues/subgroups để fallback về Planner |
| G | `schema_context.py`, `planning.py`, `contracts.py` | Context chia sẻ schema giữa partition; `prompt_dumps` APC prefix-stable ordering; QueryPlan có kiểu, rule refs, filters/grouping/windows, schema/policy/partition validator, recursive date spine & derived_qty_base prompt guide | Kiểm tra cấu trúc không phải chứng minh SQL tương đương ngữ nghĩa câu hỏi |
| H | `compilation.py`, `execution.py` | 5 template: revenue/cancellation/top/debt/profit; SQL fallback; authorizer; deadline parent/worker; `_SqliteWorkerPool` kiểm soát cứng tiến trình active+idle (`max_workers=1` lease 2 lần vẫn chỉ có 1 tiến trình sống đồng thời) | Không thực thi Python sandbox; template không bao phủ mọi metric/ngành |
| I | `verification.py`, `pipeline.py` | Typed result, candidate hash/parent, budget; rounding tiền tệ theo schema/plan; kiểm tra biểu thức từng cột trong bảng: giữ nguyên `COUNT` là số nguyên và `SUM(qty)` là số thực thập phân | Mặc định dừng ở candidate đầu qua kiểm tra; chưa có dual-path verification ensemble |
| J | `decision.py` | Tách unsupported evidence với lỗi giải; domain-aware `outside_coverage` cho `generic_sql`; kiểm chứng coverage/README thiếu dữ liệu; `DecisionGate.choose` tích hợp critic veto (`semantic_score < 0.5` $\rightarrow$ `abstain`) | Nhiều trường hợp chưa chứng minh được sẽ abstain; không ép quota unanswerable |
| K | `decision.py`, `evaluation/calibration.py`, `pipeline.py` | Gate không tự gán p; `Solver` kiểm soát hạn mức gọi model tập trung (planner + critic $\le$ `max_model_calls`), ghi nhận `CRITIC_SKIPPED_BUDGET_EXHAUSTED`; calibration JSON tùy chọn; optional `ClassifierBackend` protocol | Chưa có artifact calibration fit trên prediction holdout đủ lớn bằng model offline cuối |
| L | `formatting.py`, `evaluation/`, `cli.py`, `model_ledger.py` | JSONL checkpoint tức thời từng ID; grader độc lập; grouped split; `PathPolicy` (dev/competition split); dynamic time pacing (`compute_question_budget`); `verify_model_ledger` bất biến thứ tự base/adapter, kiểm tra tính nhất quán phân rã; `count_safetensors_parameters` & `inspect_safetensors_parameters` khôi phục tham số logic cho packed tensors | Benchmark full inference offline model trên GPU chưa đo thực tế |

## 2. Bảng Trạng thái 6 Cổng Nghiệm thu (Acceptance Gates G0–G5)

| Cổng | Tên cổng | Điều kiện đóng | Trạng thái kỹ thuật thực tế (06/11/2026) |
| :---: | :--- | :--- | :--- |
| **G0** | Correctness | Đóng R01/R02/R04/R05; có fixture biến thể và kiểm thử hồi quy baseline. | **Đã đóng các counterexample R01/R02/R04/R05 trên bộ test & probe post-fix** (còn cần mở rộng fixture topping F&B và chứng từ trả hàng âm). |
| **G1** | Bounded runtime | Hard pool cap; deadline chung; failure isolation; output đã tính được phải được checkpoint; pacing đa luồng. | **Đã triển khai hard pool cap, fair pacing đa luồng, checkpoint JSONL tức thời & khôi phục thứ tự gốc** (cần chạy thêm stress test kill container/hang dài). |
| **G2** | Model compliance | Weights thật, logical params, all-shard hash, revision/date/licence; ledger bất biến thứ tự. | **Đã sửa ledger bất biến thứ tự, kiểm tra tổng thành phần & đếm logical parameters cho packed safetensors** (chưa tải weights model offline thật). |
| **G3** | Offline end-to-end | Image tự boot, không mạng, không tải trọng số, `/data` read-only, output đúng, log/debug không lộ khóa. | **Chưa chạy** (cần đóng gói GPU Docker + local vLLM `--network none`). |
| **G4** | Calibration/evaluation | Model solver cuối, split/exposure minh bạch, đủ mọi type, business-macro + query-macro, coverage/risk và ablation. | **Chưa đạt** (cần chạy trên model offline cuối cho toàn bộ 631 câu calibration và 709 câu evaluation). |
| **G5** | Rerun reproducibility | Hai lần chạy trên GPU thực tế; đo thời gian toàn container, model load, RSS/VRAM, output hash và PA ($|\Delta\text{PA}| \le 0.03$). | **Chưa chạy** (cần đo 2 lần trên GPU thực tế). |

## 3. Ranh giới môi trường & PathPolicy

Tất cả source, test, cache và report do lần triển khai này tạo nằm trong `source_test/`. Tuyệt đối không sửa solver hoặc code tham khảo bên ngoài `source_test/`.
- Tệp `.env` được đọc tại chỗ khi cần gọi API phát triển; tuyệt đối không hiển thị/log khóa hoặc đưa khóa vào source/repo/image.
- Chế độ `dev`: Cho phép ghi trong phạm vi workspace `source_test/`.
- Chế độ `competition`: Được thực thi qua `PathPolicy`, chỉ cho phép ghi vào các thư mục kết quả/artifact/cache được chỉ định (`allowed_roots`), đồng thời khóa cứng thư mục dữ liệu đầu vào `--data` và `/data` ở chế độ chỉ đọc.

## 4. Kiểm chứng quy định mô hình (Model Ledger)

Đã hoàn thiện module `model_ledger.py` và CLI subcommand `verify-ledger`:
- Giới hạn tham số của thể lệ ($\le 15\text{B}$) áp dụng cho mọi model dùng trong quá trình suy luận (kể cả các model nạp nối tiếp). Trọng số base dùng chung không bị cộng lặp khi thực sự chia sẻ cùng base checkpoint.
- Yêu cầu mô hình pretrained/third-party phát hành trước ngày 01/10/2026; cho phép LoRA self-finetuned phát hành sau ngày 01/10/2026 với điều kiện base model trước ngày 01/10/2026 và dữ liệu huấn luyện tuân thủ.
- `verify_model_ledger` bảo đảm tính bất biến thứ tự entries giữa base model và adapter (`[base, adapter]` và `[adapter, base]` đều cho kết quả vi phạm đồng nhất), đồng thời bắt buộc kiểm tra tính nhất quán giữa tổng và các thành phần phân rã (`base_parameters + adapter_parameters == total`).
- `count_safetensors_parameters` và `inspect_safetensors_parameters` khôi phục chính xác số tham số ma trận logic trước khi đóng gói đối với trọng số packed (AWQ/INT4/INT8), phân biệt rõ giữa số phần tử tensor lưu trữ và số tham số logic.

## 5. Phân bổ Dữ liệu Chính thức (`split.json`)

Toàn bộ 3.000 câu hỏi tập `train` được phân bổ thành 3 tập dữ liệu độc lập theo doanh nghiệp:
- **709 câu evaluation (6 businesses: `biz_104`, `biz_111`, `biz_117`, `biz_121`, `biz_128`, `biz_130`)**
- **631 câu calibration (6 businesses)**
- **1.660 câu fit (18 businesses)**

## 6. Kiểm chứng Kỹ thuật & Bản sửa lỗi R01–R11 (06/11/2026)

- **Kiểm tra tĩnh (`pyflakes`):** `python -m pyflakes bizreason tests` $\rightarrow$ **0 lỗi, exit code 0**.
- **Bộ kiểm thử đơn vị & tích hợp (`pytest`):** `python -m pytest tests -q` $\rightarrow$ **261 / 261 passed (100%)** trong `~14s` (232 test nền tảng + 29 regression tests trong `tests/test_review_20261106_fixes.py`).
- **Snapshot probe kiểm chứng post-fix:** Đã tạo tại [`artifacts/pipeline_review_20261106_post_fix/probes.json`](file:///c:/Users/Mr.Dat09/Documents/python/datamate/source_test/artifacts/pipeline_review_20261106_post_fix/probes.json), bảo toàn snapshot lịch sử pre-fix tại `artifacts/pipeline_review_20261106/probes.json`.
- **Kiểm chứng Live qua cổng Trammay (`ag/gemini-3.8-flash-high`):**
  - Thực hiện trên: **Lát cắt 20 câu/5 businesses (`biz_104`, `biz_111`, `biz_117`, `biz_121`, `biz_128`; cơ cấu `16 number`, `3 percent`, `1 unanswerable`; chưa phủ `list`/`table`/`date`/`string`/`boolean` hoặc toàn bộ 6 businesses evaluation)**.
  - Kết quả: **17 Đúng / 1 Sai / 2 Abstain** (`Answered Accuracy = 94.44%`, `Coverage = 0.900`).
  - Điểm số: **PA local 0.800 trên 20 câu train thuộc nhóm evaluation, do scorer của dự án tính**.
  - Thời gian: **Hai phiên quan sát có tỷ số thời gian 4.58x (1.738,06s $\rightarrow$ 379,36s); đồng thời đổi `--stream`, code, `question_timeout` 150 $\rightarrow$ 210s và `--workers 5`, chưa phải ablation cô lập từng nhân tố**.
  - Ghi nhận: Toàn bộ 20 trace live đều có `model_calls > 0`, do đó lợi ích tiết kiệm lượt gọi LLM của `DeterministicSlotGuard` chưa được thể hiện trên lát cắt này.
- **Xử lý chứng từ hoàn trả âm:** Khi phân tích câu sai `q_008f69128180`, xác định nguyên nhân do không phân biệt loại chứng từ phiếu hoàn trả. Tuyệt đối không sửa ngây thơ bằng cách lọc bỏ `amount <= 0` (vì sẽ loại nhầm đơn 0 đồng hợp lệ hoặc khuyến mại 100%), mà phải nhận diện theo nguồn và rulebook.
