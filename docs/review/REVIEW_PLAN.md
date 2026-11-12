# Kế hoạch đánh giá source code DataMate và thiết kế kiến trúc mới

Lập ngày 2026-11-10 tại commit `bf565b3`, theo `REVIEW_PLANNING_PROMPT.md`. Đây là kế hoạch, chưa phải bản đánh giá. Mọi nhận định về lỗi hay overfit dưới đây là **giả thuyết cần kiểm chứng**, trừ các con số đã đo được ghi rõ nguồn.

## 1. Tóm tắt

**Mục tiêu:** trả lời ba câu theo thứ tự. Code `bizreason` có đúng không (giai đoạn 1). Cách tổ chức pipeline có đúng không (giai đoạn 2). Kiến trúc nào tốt hơn, hoặc có cần kiến trúc mới không (giai đoạn 3).

**Cách tiếp cận:** đo trước, đọc sau. Khảo sát cho thấy dự án đã có sẵn 1.000 trace với plan LLM đã ghi lại và một script replay offline chạy khoảng 220 giây. Vì vậy phần lớn câu hỏi quan trọng trả lời được bằng thí nghiệm replay không tốn lời gọi LLM, trước khi phải đọc sâu 18.000 dòng code.

**Ba rủi ro lớn nhất:**

1. **Chưa có con số nào đo được năng lực trên dữ liệu chưa thấy.** Con số 0,708 được đo trên chính 1.000 câu đã dùng để viết healer. Cả 30 doanh nghiệp train, kể cả 6 doanh nghiệp thuộc split "evaluation", đều nằm trong 1.000 câu đó.
2. **Lớp sửa SQL sau khi sinh có dấu hiệu khớp theo câu chữ.** 23 trên 37 hàm trong `sql_healers.py` đọc nội dung câu hỏi, với 57 phép so khớp cụm từ. Có cụm gần như nguyên văn một câu hỏi, ví dụ `"cac don tra bang cod voi doanh thu cac don tra bang tien mat"` ở dòng 3456.
3. **Cổng "verified" không phân biệt được đáp án sai.** Trong lần chạy trực tiếp, cả 161 câu sai đều đã qua kiểm tra. Với luật +1/−1/0, đây là nguồn mất điểm trực tiếp.

**Ước tính:** giai đoạn 1 khoảng 4–6 ngày làm việc, giai đoạn 2 khoảng 2–3 ngày, giai đoạn 3 khoảng 3–4 ngày chưa kể bản thử. Đây là ước lượng thô, sẽ chỉnh sau thí nghiệm E1. Chi phí LLM bằng 0 cho mọi thí nghiệm mặc định. Hai thí nghiệm tùy chọn cần chạy model, mỗi lần 1.000 câu mất khoảng 4,3 giờ GPU theo lần chạy đã lưu (15.649 giây).

## 2. Luồng thực tế của hệ thống

Lần theo code, không theo tài liệu.

| Bước | Vị trí | Dùng LLM | Ghi chú |
|---|---|---|---|
| Điều phối lệnh `run` | `cli.py:_run` (dòng 88), `_builder` (55), `compute_question_budget` (69) | Không | |
| Dựng dữ liệu doanh nghiệp | `data/ingestion.py:BusinessBuilder.prepare` (180) | Không | Kết quả lưu ở `artifacts/cache/<biz>/<rev>/business.sqlite` |
| Fast path | `triage.py:route` (344), `parse_fast_path_slots` (242) | Không | 30/1.000 câu, cả 30 đều đúng |
| Sinh plan | `planning.py:Planner.make_plan` (570) | Có, tối đa 3 vị trí gọi | 952/987 plan có `metric = generic_sql`, tức LLM tự viết SQL |
| Sửa plan và SQL | `planning.py:auto_heal_plan` (1066) → `plan_input.py:_auto_heal_sql_syntax_and_semantics` (154) → `sql_healers.py` | Không | Chạy trước khi validate, và chạy lại khi validate lỗi |
| Kiểm tra plan | `planning.py:validate_plan` (1269), `semantic_checks.py` | Không | |
| Unsupported và coverage | `decision.py:verify_unsupported` (220), `assess_metric_coverage` (436) | Không | |
| Compile | `compilation.py:TemplateCompiler.compile` (43) | Không | 5 template, còn lại dùng SQL của LLM |
| Thực thi | `execution.py:execute_sql` (552) | Không | |
| Phục hồi kết quả | `pipeline.py:_recover_list_execution` (40), `_recover_scalar_execution` (144), `_execute_zero_row_fallback` (171) | Không | Fallback nới lỏng điều kiện lọc khi kết quả là 0 hoặc rỗng |
| Kiểm tra candidate | `verification.py:verify_candidate` (156) | Không | |
| Critic | `triage.py:SemanticCritic` | Có | Tắt trong lần chạy đã lưu |
| Cổng quyết định | `decision.py:DecisionGate.choose` (519) | Không | Chế độ `verified` |
| Ghi kết quả | `formatting.py`, trace `QuestionTrace` | Không | |

**Cấu hình của lần chạy đã lưu** (`artifacts/run/run_manifest.json`): model `Shaleen123/gemma4-12b-sql` qua API từ xa, `max_model_calls = 1`, `max_repairs = 0`, critic tắt, gate `verified`. Trung vị 55,6 giây mỗi câu, p95 120 giây.

**Nhận xét cấu trúc, ở dạng giả thuyết:** trên giấy là "QueryPlan có kiểu + template compiler", nhưng trên thực tế 96% câu đi qua đường "LLM viết SQL tự do, rồi chương trình sửa SQL bằng regex". Đây là câu hỏi trung tâm của giai đoạn 2.

## 3. Hiện trạng số điểm

| Con số | Nguồn | Đo trên | So sánh được với |
|---|---|---|---|
| PA 0,408 (569 đúng, 161 sai, 270 abstain) | `results/score_report.json` | 1.000 câu đầu theo thứ tự id của 3.000 câu train, cả 30 `biz_*`, chạy trực tiếp | 0,708, cùng bộ câu |
| PA 0,708 (801, 93, 106) | `results/replayed_score_report.json` | Cùng 1.000 câu, replay plan đã ghi qua code hiện tại | 0,408 |
| PA 0,498 → 0,749 | Tính lại từ hai file trên | 243 câu thuộc 6 doanh nghiệp split "evaluation" | Không phải hold-out, vì các câu này nằm trong bộ replay |
| 1.00000 | Báo cáo 1.000 câu | 1.000 câu `public_test` (`dev_*`), bằng solver riêng không có trong repo | Không so sánh được với các số trên |
| 17 đúng, 1 sai, 2 abstain | `docs/IMPLEMENTATION_STATUS.md` | 20 câu, model `gemini-3.8-flash-high`, không phải model thi | Không |

Dịch chuyển giữa hai lần đo trên 1.000 câu: 161 câu abstain → đúng, 71 câu sai → đúng, 87 câu vẫn sai, 6 câu abstain → sai, 3 câu sai → abstain, 0 câu đúng → khác.

**Điểm cần kiểm chứng về tính hợp lệ của phép đo replay:**

- `replay_1000.py` lấy `plans[0]` trong trace. `Solver.solve` lưu plan **sau khi** đã qua `auto_heal_plan` của code cũ. Vậy đầu vào replay có thể không phải đầu ra thô của LLM, trừ 2 trace có `invalid_plans`.
- Replay dùng `Settings()` mặc định (`max_model_calls = 3`, `max_repairs = 1`), còn lần chạy thật dùng 1 và 0. Model giả trả lại cùng một plan ở mọi lần gọi.
- Replay không lưu trace mới, nên chưa biết healer nào đã đổi câu nào.
- 0 câu tụt điểm trên 1.000 câu vừa là tin tốt, vừa là dấu hiệu code được chỉnh cho đến khi khớp bộ này.
- `subset_predicted_only: true` trong báo cáo trực tiếp: cần xác nhận không làm lệch mẫu số (báo cáo có `n = 1000`, nên giả thuyết là không ảnh hưởng).

## 4. Bản đồ rủi ro

**Theo loại câu hỏi** (đúng, sai, abstain trên tổng):

| Loại | Trực tiếp | Replay | Ghi chú |
|---|---|---|---|
| number (629) | 416, 100, 113 | 530, 57, 42 | Nguồn câu sai lớn nhất |
| percent (182) | 86, 38, 58 | 129, 27, 26 | Tỷ lệ sai cao nhất trong nhóm lớn: 15% |
| list (76) | 18, 12, 46 | 54, 6, 16 | |
| date (50) | 33, 6, 11 | 45, 2, 3 | |
| string (37) | 16, 2, 19 | 34, 1, 2 | |
| unanswerable (16) | 0, 0, 16 | 0, 0, 16 | **Không bao giờ trả lời đúng.** Train có 70 câu loại này trên 3.000 |
| table (8), boolean (2) | 0/3/5 và 0/0/2 | 8/0/0 và 1/0/1 | Mẫu nhỏ |

**Theo doanh nghiệp, replay:** yếu nhất là `biz_109` (17 đúng, 10 sai, 7 abstain), `biz_127` (8 sai), `biz_124` (8 sai), `biz_123` (7 sai), `biz_122` (14 abstain), `biz_106` (11 abstain).

**Theo tầng, từ trace của lần chạy trực tiếp:**

| Trạng thái trong trace gốc | Số câu | Kết quả trực tiếp | Kết quả replay |
|---|---:|---|---|
| Fast path template | 30 | 30 đúng | 30 đúng |
| SQL của LLM, qua kiểm tra | 700 | 539 đúng, 161 sai | 610 đúng, 87 sai, 3 abstain |
| SQL của LLM, không qua kiểm tra | 188 | abstain | 118 đúng, 2 sai, 68 abstain |
| Plan bị validator từ chối | 69 | abstain | 43 đúng, 4 sai, 22 abstain |
| LLM trả đầu ra không hợp lệ | 13 | abstain | 13 abstain |

Mã lỗi nhiều nhất trong trace: `MISSING_PARTITION` 138, `PERCENT_MISSING_100_SCALE` 20, `CUSTOMER_IDENTITY_GROUPING_REQUIRED` 14, `ModelError` 13.

**Nối với module nghi ngờ:**

| Hiện tượng | Module cần xem | Giai đoạn |
|---|---|---|
| Câu sai đã qua kiểm tra | `verification.py`, `decision.py`, `semantic_checks.py` | 1 (P6), 2 |
| 161 câu abstain thành đúng nhờ sửa SQL | `sql_healers.py`, `plan_input.py` | 1 (P5, P3), E2 |
| `MISSING_PARTITION` là lỗi phổ biến nhất | `planning.py:validate_plan`, `schema_context.py`, `grounding.py` | 1 (P3) |
| Unanswerable luôn abstain | `decision.py:verify_unsupported`, prompt của planner | 1 (P6), E5 |
| Fallback khi kết quả bằng 0 tự nới điều kiện lọc | `pipeline.py:_execute_zero_row_fallback` | 1 (P7). Giả thuyết: biến đáp án đúng "0" thành đáp án sai khác 0 |
| 96% plan là `generic_sql` | `compilation.py`, `planning.py`, `contracts.py` | 2 |

## 5. Các gói đánh giá

Người thực hiện: **C** là người đánh giá chính (model mạnh), **W** là worker `gemini-3.8-flash-high` chỉ đọc. Số dòng đã đếm lại.

| Gói | Phạm vi | Dòng | Câu hỏi GĐ1 | Công sức |
|---|---|---:|---|---|
| P0 | Hạ tầng đo: replay có trace, cờ tắt healer, kiểm tra tính hợp lệ của replay | script mới | 1, 2, 8 | 1 ngày |
| P5 | `sql_healers.py` | 3.813 | 2, 3, 5 | 1 ngày |
| P6 | `verification`, `semantic_checks`, `decision`, `triage` | 2.478 | 4, 7 | 0,75 ngày |
| P3 | `grounding`, `schema_context`, `prompt_budget`, `planning`, `plan_input` | 3.861 | 2, 5, 6 | 0,75 ngày |
| P2 | `rules`, `temporal`, `profit`, `metadata_fields` | 1.299 | 5 | 0,5 ngày |
| P1 | `bizreason/data/*` (13 file) | 2.132 | 5, 7 | 0,5 ngày |
| P4 | `compilation`, `execution`, `sql_parameters` | 1.048 | 5, 10 | 0,5 ngày |
| P7 | `pipeline`, `llm`, `streaming`, `model_ledger`, `config`, `contracts`, `cli`, `formatting`, `utils`, `__init__`, `__main__` | 3.339 | 7, 8, 10 | 0,5 ngày |
| P8 | `evaluation/*` | 310 | 1, 3, 8 | 0,25 ngày |
| P9 | `tests/` (31 file, 450 test) | ~10.900 | 9 | 0,5 ngày |
| P10 | `scripts/` (71 file), `replay_1000.py`, `run_overnight.sh`, `results/`, `docs/` | ~11.000 | 3, 8, 11 | 0,5 ngày |

Chi tiết từng gói:

**P0. Hạ tầng đo (C).** Phụ thuộc: người dùng duyệt việc viết script mới ngoài repo chính. Đầu ra: script replay ghi trace mới vào thư mục tạm, không ghi vào `results/` hay `artifacts/`; cơ chế tắt từng healer từ bên ngoài, không sửa `bizreason`. Hoàn thành khi replay mặc định tái tạo đúng 801/93/106.

**P5. Healer (C, W hỗ trợ).** Với từng hàm trong 37 hàm: điều kiện kích hoạt, căn cứ trong rulebook hoặc đặc tả, số câu bị ảnh hưởng theo E2, phân loại "tổng quát / theo lĩnh vực / theo câu chữ". W liệt kê mọi chuỗi và regex so khớp nội dung câu hỏi theo từng hàm. C xác minh bằng cách đối chiếu ngẫu nhiên 20% danh sách với code. Ưu tiên theo kích thước và mức phụ thuộc câu hỏi: `heal_customer_debt_rules` (404 dòng), `heal_group_c_and_e`, `heal_cde_query_and_output`, `heal_percent_formulas`, `heal_store_revenue_and_average_order`, `heal_status_vocabulary`. Hoàn thành khi mỗi hàm có nhãn phân loại và số liệu ablation.

**P6. Cổng trả lời (C).** `verify_candidate` kiểm tra những gì, bỏ sót gì ở 87 câu sai còn lại; cổng `verified` so với ngưỡng tối ưu theo +1/−1/0; đường đi của câu unanswerable; `parse_fast_path_slots`. Đầu ra gồm phân loại 93 câu sai của replay theo nguyên nhân.

**P3. Planning (C, W hỗ trợ).** Prompt gửi cho LLM có chứa ví dụ lấy từ train không; ngữ cảnh bị cắt thế nào ở giới hạn 65.000 ký tự; `validate_plan` từ chối đúng hay sai (43 trong 69 plan bị từ chối hóa ra cho đáp án đúng sau khi sửa); 6 hàm trong `plan_input.py` (2.212 dòng).

**P2. Luật và thời gian (C).** Đối chiếu từng mục rulebook §1–§9 với code; lịch âm, Tết, tuần, "tháng này"; tính độc lập của test thời gian.

**P1. Ingestion (C, W hỗ trợ).** Chọn header, kế thừa nhóm, dedup, quy đổi đơn vị, cache theo revision. Kiểm tra mẫu bằng cách so `business.sqlite` với file nguồn trên 3 doanh nghiệp.

**P4. Thực thi (C).** Authorizer chỉ đọc, timeout, worker pool, ràng buộc tham số.

**P7. Điều phối (C, W hỗ trợ).** Ngân sách thời gian, xử lý lỗi, các hàm phục hồi trong `pipeline.py`, định dạng nộp bài. W liệt kê `except` rộng và hàm không được gọi.

**P8. Bộ chấm (C).** So `scorer.py` với thể lệ (làm tròn 2 chữ số, alias, bảng, unanswerable). `splits.py` chia theo doanh nghiệp, nhưng `split.json` không có trong repo.

**P9. Test (C, W hỗ trợ).** Phân loại test theo "đặc tả độc lập" hay "khóa hành vi"; test nào có nguồn gốc từ câu hỏi train. Tên file gợi ý nhiều test sinh ra từ các vòng sửa lỗi (`test_eval1000_*`, `test_next50_*`, `test_review_20261106_fixes.py` với 65 test).

**P10. Tái lập và dọn dẹp (C, W hỗ trợ).** Script nào còn chạy được; tài liệu nào sai so với code (ví dụ `IMPLEMENTATION_STATUS.md` ghi 261 test, hiện là 450; README trỏ `WORK_PLAN.md` không tồn tại); bí mật trong `artifacts/kaggle_12h_server/`, chỉ kiểm tra tên trường, không in giá trị.

## 6. Thí nghiệm định lượng

Mặc định không gọi LLM, chạy trên bản sao trong thư mục tạm, không ghi vào repo.

| Mã | Mục đích | Dữ liệu | Cách làm | LLM | Thời gian | Cách đọc |
|---|---|---|---|---|---|---|
| E1 | Replay có đáng tin không | 1.000 trace | Chạy lại replay với trace; đo lại với `max_model_calls = 1`, `max_repairs = 0`; kiểm tra plan trong trace đã qua heal chưa | Không | ~10 phút | Nếu số liệu đổi đáng kể thì 0,708 phải ghi lại |
| E2 | Giá trị và độ tổng quát của từng healer | 1.000 trace | Tắt từng hàm trong 37 hàm, replay, ghi câu đổi kết quả và số doanh nghiệp bị ảnh hưởng | Không | ~2,5 giờ máy (37 × 220 giây), chạy song song được | Healer chỉ tác động 1–2 câu hoặc 1 doanh nghiệp là ứng viên overfit. Healer tắt đi mà điểm không đổi là code chết |
| E3 | Ước lượng tổng quát hóa theo doanh nghiệp | Kết quả E2 | Bỏ-một-doanh-nghiệp ở mức suy luận: với mỗi `biz_X`, tắt các healer mà mọi câu được lợi đều thuộc `biz_X`, rồi tính PA của `biz_X` | Không | ~1 giờ | Cho **cận trên** của PA trên doanh nghiệp chưa thấy, kèm khoảng tin cậy bootstrap theo doanh nghiệp |
| E4 | Kiểm tra độc lập | 60 câu chọn ngẫu nhiên phân tầng theo loại | C viết SQL tay trên `business.sqlite` và file nguồn, so với gold và với dự đoán | Không | ~1 ngày | Đo tỷ lệ gold đáng ngờ và lỗi ingestion |
| E5 | Cổng abstain | 1.000 trace + E1 | Dựng đặc trưng từ trace, vẽ đường PA theo ngưỡng; tách riêng câu unanswerable | Không | ~0,5 ngày | Điểm tối ưu so với cổng hiện tại; mức PA mất do không bao giờ trả lời unanswerable |
| E6 | Độ bền với câu chữ | 100 câu đang đúng | Diễn đạt lại câu hỏi bằng quy tắc cơ học (bỏ dấu, đổi từ đồng nghĩa trong danh sách cố định), replay cùng plan | Không | ~0,5 ngày | Câu đổi kết quả khi chỉ đổi câu chữ cho thấy healer phụ thuộc chuỗi |
| E7 | Chất lượng test | `bizreason` + test | Mutation có chọn lọc trên P2, P4, P6 | Không | ~0,5 ngày | Tỷ lệ đột biến sống sót theo module |
| E8 (tùy chọn) | Năng lực thực trên câu chưa dùng | 2.000 câu train còn lại, hoặc mẫu 500 | Chạy model thật một lần, cấu hình như lần đã lưu | **Có** | ~4,3 giờ GPU mỗi 1.000 câu | Cùng 30 doanh nghiệp nên chỉ là hold-out theo câu, không phải theo doanh nghiệp |
| E9 (tùy chọn) | Hold-out theo doanh nghiệp | 1.000 câu `public_test` với `submission.jsonl` làm gold | Chạy model thật trên 14 `dev_*` | **Có** | ~4,3 giờ GPU | Phép đo tốt nhất có thể có. Lưu ý khoảng 100 câu `dev_*` đã được dùng khi phát triển (id xuất hiện trong `docs/` và `scripts/`) và phải loại ra |

**Về dữ liệu sạch:** không còn doanh nghiệp train nào chưa bị dùng. Hai nguồn thay thế là E8 và E9, đều cần chạy model. Nếu không được duyệt, câu hỏi "năng lực thực" chỉ trả lời được bằng cận trên từ E3, và báo cáo phải ghi rõ giới hạn đó.

**Về đột biến dữ liệu** (đổi tên cột, định dạng ngày): hoãn sang giai đoạn 2, vì plan đã ghi gắn với tên cột cũ nên không replay được. Chỉ đo được ở tầng ingestion.

## 7. Thứ tự thực hiện và phần chạy song song

1. **Đợt 1, tuần tự:** P0 → E1. Mọi thứ khác phụ thuộc vào việc replay có đáng tin không.
2. **Đợt 2, song song:** E2 (máy chạy) cùng lúc với P6, P8 và các worker liệt kê cơ học cho P5, P3, P7, P9, P10.
3. **Đợt 3:** P5 dùng kết quả E2 → E3, E5, E6.
4. **Đợt 4, song song:** P3, P2, P1, P4, P7, và E4.
5. **Đợt 5:** P9 + E7, P10.
6. **Viết `REVIEW_REPORT.md`, dừng chờ duyệt.** E8, E9 chạy khi được duyệt, tốt nhất ngay từ đợt 2 vì mất nhiều giờ.

**Worker `gemini-3.8-flash-high`:** tối đa 4 worker song song, mỗi worker một danh sách file riêng, đầu ra là văn bản trả về, không ghi file. `SUBAGENT_RULES.md` hiện viết cho worker sửa file, nên cần bổ sung chế độ chỉ đọc: `--mode plan`, allowlist chỉ `pwd` và `ls`, giữ nguyên luật `deny`, và nghiệm thu bằng `git status` rỗng cộng đối chiếu mẫu. Chế độ này phải được thử trên một file trước khi dùng. Hiện mới biết `--mode plan` chạy được, chưa kiểm chứng nó chặn ghi.

## 8. Kế hoạch giai đoạn 2: đánh giá pipeline

Bắt đầu sau khi `REVIEW_REPORT.md` được duyệt.

**Quy lỗi theo tầng.** Với mỗi câu sai hoặc abstain trong replay (199 câu), xác định tầng sớm nhất gây lỗi theo trình tự kiểm tra sau:

1. Dữ liệu trong `business.sqlite` có đủ và đúng để tính gold không? Nếu không: ingestion.
2. Cửa sổ thời gian và luật áp dụng có đúng không? Nếu không: luật và thời gian.
3. Ngữ cảnh gửi LLM có chứa bảng và cột cần thiết không? Nếu không: grounding.
4. Plan thô của LLM có đúng ý không? Nếu không: planning (lỗi của model).
5. Plan đúng nhưng bị sửa sai, bị từ chối, hoặc compile sai: healing, validator, compile.
6. Kết quả đúng nhưng bị loại, hoặc sai nhưng được chấp nhận: verification và cổng.
7. Giá trị đúng nhưng sai định dạng: đầu ra.

Làm đủ 199 câu nếu E4 cho thấy mỗi câu mất dưới 10 phút, nếu không thì lấy mẫu phân tầng 80 câu và báo khoảng tin cậy.

**Giá trị từng tầng.** Dùng E2, E5 và thêm: tắt fast path, tắt các hàm phục hồi trong `pipeline.py`, tắt từng nhóm kiểm tra trong `validate_plan` và `verify_candidate`. Ghi PA, số câu sai, thời gian và số dòng code của mỗi tầng vào một bảng.

**Ranh giới LLM và chương trình.** Từ bước 4 và 5 ở trên: tỷ lệ lỗi do plan thô so với lỗi do xử lý plan. Phân loại các lỗi SQL mà healer đang sửa thành "loại bỏ được bằng cấu trúc" (ví dụ sai tên bảng, thiếu partition, sai thang phần trăm, quy đổi đơn vị) và "cần hiểu nghiệp vụ".

**So với cách làm đạt 1.00000.** Nếu có `output_solvers/`: liệt kê tri thức trong solver mà `bizreason` thiếu, phân loại tổng quát hay riêng từng doanh nghiệp. Nếu không: chỉ dùng mục III–V của báo cáo, và ghi rõ giới hạn.

**Trace cần bổ sung:** plan thô trước khi heal, danh sách healer đã kích hoạt và diff SQL, kết quả từng kiểm tra trong verification. P0 đã tạo phần lớn. Plan thô chỉ có thể lấy lại bằng E8 hoặc E9.

**Tiêu chí kết luận từng tầng:**

| Kết luận | Điều kiện |
|---|---|
| Giữ | Đóng góp PA dương có ý nghĩa, không phụ thuộc câu chữ, ít lỗi mức Cao |
| Sửa | Thiết kế hợp lý, lỗi khu trú |
| Thay thế | Phần lớn giá trị đến từ xử lý theo câu chữ, hoặc tồn tại chỉ để bù lỗi của tầng trước |
| Bỏ | Đóng góp PA bằng 0 hoặc âm |

Tầng chưa đo được thì ghi "chưa kết luận".

## 9. Kế hoạch giai đoạn 3: thiết kế kiến trúc

Bắt đầu sau khi `PIPELINE_ASSESSMENT.md` được duyệt. Chưa chốt hướng nào.

**Quy trình:**

1. Lập danh sách yêu cầu, mỗi yêu cầu dẫn tới mã phát hiện hoặc số liệu của giai đoạn 1 và 2.
2. Đặt mục tiêu đo được: PA trên dữ liệu chưa thấy, tỷ lệ sai, độ phủ, thời gian mỗi câu, số lời gọi LLM, số dòng code.
3. Xây ít nhất ba phương án. Phương án A luôn là "giữ kiến trúc, sửa có mục tiêu". Các hướng khác ở dạng giả thuyết: (B) plan có kiểu với metric compiler mở rộng, LLM chỉ điền slot thay vì viết SQL; (C) semantic layer chuẩn hóa theo nghiệp vụ để SQL của LLM đơn giản đi và healer không còn cần; (D) nhiều candidate kèm kiểm tra nhất quán để làm tín hiệu abstain.
4. So sánh bằng cùng một bảng tiêu chí.
5. Kiểm chứng giả định bằng bản thử, sau khi được duyệt viết code.
6. Viết `ARCHITECTURE_PROPOSAL.md` và lộ trình chuyển đổi từng bước, mỗi bước có cổng chống tụt điểm dựa trên replay.

**Tiêu chí so sánh:** PA dự kiến và độ tin cậy của dự kiến đó; rủi ro overfit; công xây dựng; khả năng chạy trong ràng buộc thi; phần code hiện tại dùng lại được; khả năng quan sát và gỡ lỗi.

**Ràng buộc thi đã thấy trong đặc tả kiến trúc (mục 17), cần người dùng xác nhận:** chạy offline, `/data` chỉ đọc, tổng model không quá 15 tỷ tham số, 2 × RTX 5090 32 GB, tối đa 6 giờ gồm cả nạp model. Với 55,6 giây trung vị mỗi câu, số câu của private test và mức song song quyết định ngân sách lời gọi LLM. Đây là ràng buộc cứng cho mọi phương án.

**Giả định cần bản thử, dự kiến:**

| Giả định | Bản thử | Ngưỡng đạt định trước |
|---|---|---|
| Model 12B điền được plan có kiểu chính xác hơn viết SQL | 100 câu, hai kiểu prompt | Tỷ lệ plan đúng cao hơn có ý nghĩa thống kê |
| Semantic layer loại được phần lớn lớp lỗi healer đang sửa | Dựng cho 3 doanh nghiệp, đo số healer còn kích hoạt | Đặt sau khi có số liệu giai đoạn 2 |
| Tín hiệu nhất quán giữa các candidate tách được câu sai | Dùng trace của E8 hoặc E9 | AUC cao hơn cổng hiện tại |

**Dữ liệu dành riêng cho bản thử:** 2.000 câu train chưa replay được chia đôi ngay từ bây giờ theo id: một nửa cho thiết kế, một nửa chỉ dùng để nghiệm thu cuối. `public_test` kèm `submission.jsonl`, nếu có, dành cho nghiệm thu theo doanh nghiệp. Bản thử cần gọi LLM nên cần duyệt riêng.

## 10. Ma trận truy vết

**Giai đoạn 1:**

| Câu hỏi | Gói | Thí nghiệm | Dẫn tới quyết định kiến trúc |
|---|---|---|---|
| 1. Năng lực thực | P0, P8 | E1, E3, E8, E9 | Mốc so sánh cho mọi phương án |
| 2. Tổng quát hóa | P5, P3 | E2, E3, E6 | Giữ hay thay lớp healer |
| 3. Rò rỉ dữ liệu | P3, P5, P8, P9, P10 | E2, E6 | Quy trình phát triển và dữ liệu nghiệm thu |
| 4. Tối ưu theo luật điểm | P6 | E5 | Thiết kế cổng abstain |
| 5. Đúng đắn nghiệp vụ | P1, P2, P4, P5 | E4 | Nơi đặt tri thức rulebook |
| 6. Đối chiếu đặc tả | P3 và mọi gói | Không | Phần đặc tả nào còn giá trị |
| 7. Bền vững | P1, P6, P7 | E6 | Xử lý lỗi và quan sát |
| 8. Tái lập | P0, P7, P10 | E1 | Hạ tầng đánh giá |
| 9. Chất lượng test | P9 | E7 | Chiến lược test |
| 10. An toàn | P4, P7, P10 | Không | Ranh giới thực thi |
| 11. Bảo trì | Mọi gói, P10 | E2 (code chết) | Danh sách module giữ, sửa, bỏ |

**Giai đoạn 2:**

| Câu hỏi | Phương pháp | Dẫn tới |
|---|---|---|
| 1. Quy lỗi theo tầng | Trình tự 7 bước trên 199 câu | Tầng nào cần đầu tư |
| 2. Giá trị từng tầng | E2, E5, ablation bổ sung | Giữ, sửa, thay thế, bỏ |
| 3. Ranh giới LLM | Tỷ lệ lỗi plan thô và lỗi xử lý | Phương án B so với C |
| 4. Sửa sau hay đúng từ đầu | Phân loại lỗi healer đang sửa | Phương án B, C |
| 5. So với 1.00000 | `output_solvers/` hoặc báo cáo | Tri thức cần đưa vào |
| 6. Ràng buộc thực tế | Thể lệ, người dùng xác nhận | Ngân sách LLM |
| 7. Kết luận từng tầng | Bảng tiêu chí mục 8 | Yêu cầu của giai đoạn 3 |

## 11. Cổng phê duyệt

Người duyệt là người dùng ở mọi cổng.

| Cổng | Dựa trên | Đổi hướng khi |
|---|---|---|
| G0, trước khi bắt đầu | Kế hoạch này và câu trả lời ở mục 14 | |
| G1, sau E1 | Kết quả kiểm tra replay | Replay không đáng tin: phải có E8 trước khi làm tiếp |
| G2, sau giai đoạn 1 | `REVIEW_REPORT.md` | Có phát hiện Nghiêm trọng về rò rỉ hoặc bộ chấm: sửa trước, đo lại |
| G3, sau giai đoạn 2 | `PIPELINE_ASSESSMENT.md` | Số liệu cho thấy phương án A là đủ: giai đoạn 3 rút gọn thành kế hoạch sửa |
| G4, trước khi viết code kiến trúc mới | `ARCHITECTURE_PROPOSAL.md`, kể cả bản thử | Bản thử trượt ngưỡng |
| Riêng | Mọi thí nghiệm gọi LLM, mọi lần ghi vào repo | |

## 12. Mẫu báo cáo

**Định dạng phát hiện, dùng chung:**

```text
[Mã] <P5-07>  Mức: Nghiêm trọng | Cao | Trung bình | Thấp   Trạng thái: Đã tái hiện | Giả thuyết
Vị trí: <file:dòng>
Mô tả: <một đoạn>
Tái hiện: <input hoặc id câu hỏi, lệnh>
Thực tế / Đúng: <kết quả> / <kết quả và căn cứ: rulebook §, đặc tả mục, SQL tay>
Tác động điểm: <số câu trong 1.000, hoặc ước lượng>
Tin cậy: Cao | Trung bình | Thấp
```

**`REVIEW_REPORT.md`:** tóm tắt và điểm thực; trả lời 11 câu hỏi kèm bằng chứng; kết quả E1–E7; phát hiện theo mức; bảng phân loại 37 healer; những gì chưa đánh giá được.

**`PIPELINE_ASSESSMENT.md`:** bảng quy lỗi theo tầng; bảng giá trị và chi phí từng tầng; phân tích ranh giới LLM; so sánh với cách đạt 1.00000; kết luận từng tầng; danh sách yêu cầu cho kiến trúc.

**`ARCHITECTURE_PROPOSAL.md`:** yêu cầu và mục tiêu; các phương án và bảng so sánh; phương án đề xuất với sơ đồ, hợp đồng dữ liệu, thiết kế abstain, quan sát, test; kết quả bản thử; module giữ, sửa, bỏ; lộ trình và cổng chống tụt điểm.

## 13. Ngoài phạm vi

- `venv/` và thư viện bên thứ ba.
- `source_test_backup_task10/`: chỉ dùng để so sánh diff, không đánh giá riêng.
- Chất lượng và quá trình fine-tune của model `gemma4-12b-sql`: không có dữ liệu huấn luyện trong repo. Chỉ ghi nhận rủi ro rò rỉ nếu model được huấn luyện trên câu hỏi train.
- 14 solver `solve_dev_*.py`: không có trong repo.
- Tính hợp lệ của binary probe theo thể lệ: không suy đoán, chờ người dùng.
- Đóng gói Docker, chạy offline trên GPU (cổng G3, G5 trong `IMPLEMENTATION_STATUS.md`): chỉ ghi nhận là chưa làm.
- Sửa lỗi: chương trình này chỉ đánh giá và thiết kế.

## 14. Câu hỏi cần người dùng quyết định

| # | Câu hỏi | Đề xuất mặc định |
|---|---|---|
| 1 | Có cho phép viết script đo (P0) và chạy replay trong thư mục tạm, không ghi vào repo không? | Có. Thiếu việc này thì hầu hết thí nghiệm không làm được |
| 2 | Có chạy model thật cho E8 hoặc E9 không? Endpoint Kaggle còn dùng được không? | Chạy E9 nếu có `submission.jsonl`, nếu không thì E8 với 500 câu |
| 3 | Có cung cấp được `submission.jsonl` và `output_solvers/` từ máy Windows không? | Có. Đây là nguồn gold duy nhất cho doanh nghiệp chưa thấy |
| 4 | Thể lệ cuộc thi: số câu private test, giới hạn 6 giờ và 15 tỷ tham số có đúng không, binary probe có được phép không? | Cần văn bản thể lệ |
| 5 | Model `gemma4-12b-sql` được fine-tune trên dữ liệu nào? Có chứa câu hỏi train không? | Cần biết để đánh giá rò rỉ |
| 6 | Model dự thi cuối là model nào? | Giả định là `gemma4-12b-sql` |
| 7 | Mục tiêu là điểm private test của cuộc thi, hay một hệ thống tổng quát dùng lâu dài? | Điểm private test. Câu trả lời đổi trọng số các tiêu chí ở giai đoạn 3 |
| 8 | Có dùng worker `gemini-3.8-flash-high` cho việc liệt kê cơ học không? | Có, tối đa 4, sau khi thử chế độ chỉ đọc |
| 9 | Hạn chót và thời gian còn lại đến ngày nộp? | Cần biết để quyết định làm đủ hay rút gọn giai đoạn 3 |
