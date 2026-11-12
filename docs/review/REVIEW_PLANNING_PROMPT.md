# Prompt: Lập kế hoạch đánh giá source code DataMate và thiết kế kiến trúc mới

Dán toàn bộ nội dung dưới đây cho agent lập kế hoạch. Nên dùng model mạnh nhất có sẵn, chạy tại `/home/ubuntu/datamate`. Không giao vai trò này cho `gemini-3.8-flash-high`.

---

## Vai trò và nhiệm vụ

Bạn là kỹ sư trưởng độc lập, được giao **lập kế hoạch** cho một chương trình gồm ba giai đoạn nối tiếp trên dự án DataMate. Bạn không bảo vệ hệ thống và không có giả định rằng hệ thống tốt hay xấu.

| Giai đoạn | Nội dung | Sản phẩm khi thực hiện |
|---|---|---|
| 1 | Đánh giá toàn bộ source code | `REVIEW_REPORT.md` |
| 2 | Đánh giá lại pipeline ở cấp hệ thống, dùng kết quả giai đoạn 1 | `PIPELINE_ASSESSMENT.md` |
| 3 | Thiết kế kiến trúc mới tốt hơn, dùng kết quả giai đoạn 1 và 2 | `ARCHITECTURE_PROPOSAL.md` |

Sản phẩm duy nhất của bạn lúc này là file `/home/ubuntu/datamate/REVIEW_PLAN.md` theo định dạng ở mục "Đầu ra". **Bản kế hoạch chưa phải là bản đánh giá hay bản thiết kế:** chỉ khảo sát đủ để phân chia và ưu tiên công việc, không đi sâu kết luận từng lỗi, không chốt kiến trúc. Mọi nhận định ở bước này phải ghi là "giả thuyết cần kiểm chứng".

## Ràng buộc

**Được phép:** đọc code và tài liệu, chạy lệnh chỉ đọc, chạy `pytest` offline.

**Không được phép khi chưa có người dùng phê duyệt:**

- Sửa, xóa, format bất kỳ file nào ngoài `REVIEW_PLAN.md`.
- Commit, tạo nhánh, tạo worktree.
- Gọi LLM thật, gọi API, chạy Kaggle, chạy `bizreason run`, chạy `run_overnight.sh`.
- Nộp bài lên cổng thi.
- Viết code cho kiến trúc mới, kể cả bản thử nghiệm.
- Chạy `replay_1000.py` hoặc các script ghi vào `artifacts/`, `results/`, `logs/`.

**Bí mật:** không mở `source_test/.env` và `source_test/artifacts/kaggle_12h_server/connection_config.json`. Không in giá trị khóa, token, mật khẩu vào đầu ra.

## Bối cảnh đã xác minh

Các dữ kiện sau đúng tại commit `bf565b3`, ngày 2026-11-10. Hãy kiểm tra lại nếu kế hoạch phụ thuộc vào chúng.

**Bài toán:** Zalo AI Challenge 2026, DataMate. Trả lời câu hỏi tiếng Việt trên dữ liệu kinh doanh dạng CSV, Excel, SQLite. Điểm mỗi câu: đúng +1, sai −1, abstain 0. Vì vậy một câu trả lời sai tệ hơn một lần abstain.

**Quy mô:**

| Phần | Quy mô |
|---|---|
| `source_test/bizreason/` | 18.280 dòng Python |
| `source_test/tests/` | 450 test, khoảng 10.900 dòng, đang pass trong khoảng 11 giây |
| `source_test/scripts/` | 71 file, khoảng 11.000 dòng, nhiều script dùng một lần |
| `source_test/docs/` | 20 tài liệu |
| `DATAMATE_PIPELINE_ARCHITECTURE.md` | Đặc tả kiến trúc, 93 KB |
| `source_test/data/` | `train` (`biz_*`, có `answers.jsonl`, `reference_answers.jsonl`) và `public_test` (`dev_001` đến `dev_014`, không thấy file đáp án) |

**Ba con số điểm khác nhau, không được gộp làm một:**

| Nguồn | Kết quả | Ý nghĩa cần lưu ý |
|---|---|---|
| `BAO_CAO_GIAI_1000_CAU_DATAMATE_1.00000.md` | 1.00000 trên `dev_*` | Báo cáo mô tả 14 solver riêng cho từng doanh nghiệp (`output_solvers/solve_dev_*.py`) và `submission.jsonl`. **Các file này không có trong repo.** Đáp án được tinh chỉnh bằng phản hồi điểm từ cổng thi (binary probe) |
| `results/score_report.json` | PA 0,408: 569 đúng, 161 sai, 270 abstain, trên 1.000 câu | Lần chạy trực tiếp đã lưu của `bizreason` |
| `results/replayed_score_report.json` | PA 0,708: 801 đúng, 93 sai, 106 abstain | Replay với **plan LLM đã ghi lại**, trên cùng bộ câu hỏi đã dùng để sửa lỗi |

Hệ quả:

- Không được dùng 1.00000 làm bằng chứng về năng lực của `bizreason`.
- Không được coi 0,708 là năng lực tổng quát khi chưa có kiểm chứng trên dữ liệu chưa thấy.
- Binary probe có hợp lệ theo thể lệ hay không là câu hỏi mở. Không suy đoán. Nếu thể lệ không có trong repo, ghi vào mục câu hỏi cho người dùng.

**Điểm đáng ngờ để đưa vào kế hoạch:**

- `sql_healers.py` có 3.813 dòng và không tồn tại trong bản sao lưu `source_test_backup_task10`. Mức tăng từ 0,408 lên 0,708 có thể phụ thuộc nhiều vào file này. Cần đánh giá nó sửa lỗi tổng quát hay học thuộc bộ train.
- Code runtime không chứa chuỗi `biz_NNN` hay `dev_NNN`, và không đọc file gold ngoài lệnh `score`. Đây chỉ là tín hiệu ban đầu: overfit vẫn có thể nằm ở mẫu câu tiếng Việt, tên cột, từ khóa hoặc hằng số.
- `results/score_report.json` có `subset_predicted_only: true`. Cần xác định điều này có làm các con số không so sánh được với nhau không.
- README trỏ đến `WORK_PLAN.md`, file này không có trong `source_test/`. Tài liệu có thể đã lỗi thời so với code.
- README ghi rõ cơ chế SQL chỉ đọc không phải sandbox đầy đủ ở cấp hệ điều hành.

## Giai đoạn 1: các câu hỏi đánh giá code phải trả lời được

Kế hoạch phải chỉ rõ từng câu được trả lời bằng cách nào và bằng bằng chứng gì.

1. **Năng lực thực:** điểm kỳ vọng trên doanh nghiệp chưa từng thấy là bao nhiêu, với khoảng tin cậy?
2. **Tổng quát hóa:** mỗi healer, rule, heuristic có căn cứ trong đặc tả và rulebook, hay được thêm để khớp câu cụ thể? Khi tắt từng nhóm thì điểm thay đổi ra sao?
3. **Rò rỉ dữ liệu:** đáp án, kết quả của train hoặc `dev_*` có đi vào code, prompt, cache, calibration, test không? Có chia split theo doanh nghiệp không?
4. **Tối ưu theo luật điểm:** câu sai đang đến từ đâu? Cổng quyết định abstain có tối ưu cho +1/−1/0 không?
5. **Đúng đắn nghiệp vụ:** code có tuân theo rulebook §1–§9 về doanh thu, thời gian tương đối, định danh khách hàng, công nợ, quy đổi đơn vị không?
6. **Đối chiếu đặc tả:** phần nào trong `DATAMATE_PIPELINE_ARCHITECTURE.md` đã làm, làm khác, hoặc chưa làm?
7. **Bền vững:** dữ liệu có cấu trúc lạ, thiếu cột, ngày Việt, bảng nhiều sheet, timeout, lỗi LLM có được xử lý mà không âm thầm ra đáp án sai không?
8. **Tái lập:** có thể tái tạo từng con số điểm từ code, dữ liệu và commit hiện tại không? Phụ thuộc vào cache, trace nào?
9. **Chất lượng test:** 450 test có bắt lỗi thật không, hay chỉ khóa hành vi hiện tại? Test nào có gold được tính độc lập?
10. **An toàn:** SQL thực thi, xử lý input, bí mật trong repo và script Kaggle có rủi ro gì?
11. **Khả năng bảo trì:** độ phức tạp, trùng lặp, code chết, script dùng một lần, tài liệu sai lệch.

## Giai đoạn 2: đánh giá lại pipeline ở cấp hệ thống

Giai đoạn 1 hỏi "code có đúng không". Giai đoạn này hỏi "**cách tổ chức pipeline này có đúng không**". Kế hoạch phải chỉ rõ cách trả lời các câu sau.

1. **Quy lỗi theo tầng:** mỗi câu sai và câu abstain bắt nguồn từ tầng sớm nhất nào: ingestion, luật và thời gian, grounding, planning, compile, thực thi, healing, verification, cổng quyết định, hay định dạng đầu ra?
2. **Giá trị từng tầng:** mỗi tầng đóng góp bao nhiêu PA, tốn bao nhiêu thời gian, lời gọi LLM và độ phức tạp code? Tầng nào có thể bỏ mà không giảm điểm?
3. **Ranh giới LLM và chương trình:** phần nào LLM đang làm mà chương trình làm tốt hơn, và ngược lại? Lỗi nào do bản thân plan của LLM, lỗi nào do hệ thống xử lý plan?
4. **Sửa sau hay đúng ngay từ đầu:** vì sao cần đến 3.813 dòng healer để sửa SQL sau khi sinh? Những lớp lỗi đó có thể bị loại từ trước bằng biểu diễn plan chặt hơn, semantic layer hoặc compiler đúng theo kiểu không?
5. **So với cách làm đạt điểm cao:** các solver trong báo cáo 1.00000 biết điều gì mà `bizreason` không biết? Phần tri thức nào tổng quát được, phần nào chỉ đúng cho từng doanh nghiệp? Nếu `output_solvers/` không lấy được, kế hoạch phải chỉ dùng phần mô tả trong báo cáo và ghi rõ giới hạn đó.
6. **Ràng buộc thực tế:** thể lệ, thời gian chạy, model được phép, GPU, khả năng truy cập mạng khi chấm private test. Thông tin nào không có trong repo thì đưa vào câu hỏi cho người dùng.
7. **Kết luận cho từng tầng:** giữ, sửa, thay thế, hay bỏ, kèm số liệu.

Tầng nào chưa đo được đóng góp thì không được kết luận. Kế hoạch phải nêu cách bổ sung trace hoặc replay để đo được, không chỉ dựa vào đọc code.

## Giai đoạn 3: thiết kế kiến trúc mới

Kế hoạch phải quy định quy trình thiết kế như sau, và **không chốt kiến trúc trước khi có số liệu của giai đoạn 1 và 2**.

1. **Yêu cầu từ bằng chứng:** mỗi yêu cầu của kiến trúc mới phải nối với phát hiện hoặc số liệu cụ thể. Không có căn cứ thì không đưa vào.
2. **Mục tiêu đo được:** PA kỳ vọng trên dữ liệu chưa thấy, tỷ lệ câu sai, độ phủ, thời gian mỗi câu, số lời gọi LLM, quy mô code.
3. **Phương án:** tối thiểu ba phương án, trong đó phải có "giữ kiến trúc hiện tại, sửa có mục tiêu" để làm mốc so sánh. Được phép kết luận rằng không cần kiến trúc mới.
4. **So sánh:** cùng bộ tiêu chí, gồm lợi ích điểm dự kiến, rủi ro overfit, chi phí và thời gian xây dựng, khả năng chạy trong ràng buộc thi, khả năng tái sử dụng code hiện có.
5. **Bản thử để kiểm chứng:** mỗi giả định quan trọng của phương án được chọn cần một bản thử nhỏ, có ngưỡng đạt và trượt định trước, chạy trên dữ liệu không dùng để thiết kế.
6. **Nội dung `ARCHITECTURE_PROPOSAL.md`:** sơ đồ luồng, trách nhiệm và hợp đồng dữ liệu giữa các tầng, ranh giới LLM và chương trình, thiết kế cho abstain, xử lý lỗi, khả năng quan sát, chiến lược test và đánh giá, danh sách module giữ, sửa, bỏ.
7. **Lộ trình chuyển đổi:** từng bước nhỏ, mỗi bước chạy được và đo được, có cổng chống tụt điểm và cách quay lại. Không viết lại toàn bộ một lần nếu không có lý do được số liệu ủng hộ.

Hướng thiết kế có thể xem xét, ở dạng giả thuyết: semantic layer chuẩn hóa theo nghiệp vụ, plan có kiểu và metric compiler thay cho sửa SQL sau khi sinh, cổng abstain hiệu chỉnh theo luật +1/−1/0, tách tri thức theo rulebook khỏi code điều phối.

**Cổng phê duyệt:** phải dừng để người dùng duyệt sau giai đoạn 1, sau giai đoạn 2, và trước khi viết bất kỳ code nào của kiến trúc mới.

## Cách lập kế hoạch

1. Đọc `SUBAGENT_RULES.md`, `source_test/README.md`, `source_test/docs/IMPLEMENTATION_STATUS.md`, `source_test/docs/VALIDATION.md`, `source_test/docs/PIPELINE_COMPREHENSIVE_REVIEW_2026_11_06.md`. Đọc mục lục và các phần liên quan của đặc tả kiến trúc.
2. Vẽ luồng thực tế của một câu hỏi từ `cli.py` qua `pipeline.py`, dựa trên code chứ không dựa trên tài liệu. Xác định nơi dùng LLM và nơi dùng chương trình.
3. Xác định phần có rủi ro ảnh hưởng điểm cao nhất, từ dữ liệu trong `results/`: `by_type`, `by_business`, `details`, `replay_1000_audit.json`. Phân loại câu sai và câu abstain theo nhóm nguyên nhân.
4. Chia việc thành các gói. Bảng gợi ý dưới đây có thể đổi nếu có lý do.
5. Thiết kế thí nghiệm định lượng và viết rõ chi phí của từng thí nghiệm.
6. Liệt kê những gì cần người dùng quyết định.

**Gói gợi ý (dòng code đã đếm):**

| Gói | Phạm vi | Dòng | Trọng tâm |
|---|---|---:|---|
| P1 | `bizreason/data/*` | 2.131 | Đọc bảng, header, parse, join, cache |
| P2 | `rules`, `temporal`, `profit`, `metadata_fields` | 1.299 | Đúng rulebook, lịch, âm lịch, thời gian tương đối |
| P3 | `grounding`, `schema_context`, `prompt_budget`, `planning`, `plan_input` | 3.861 | Context, cắt ngữ cảnh, sửa plan của LLM |
| P4 | `compilation`, `execution`, `sql_parameters` | 1.048 | SQL đúng, an toàn, timeout |
| P5 | `sql_healers` | 3.813 | Tổng quát hay học thuộc. Chia nhỏ theo từng healer |
| P6 | `verification`, `semantic_checks`, `decision`, `triage` | 2.478 | Cổng trả lời, abstain, unanswerable |
| P7 | `pipeline`, `llm`, `streaming`, `model_ledger`, `config`, `contracts`, `cli`, `formatting`, `utils` | 3.332 | Điều phối, lỗi, ngân sách, định dạng nộp bài |
| P8 | `evaluation/*` | 309 | Bộ chấm khớp thể lệ, split, calibration |
| P9 | `tests/` | ~10.900 | Chất lượng và độ phủ |
| P10 | `scripts/`, `replay_1000.py`, `run_overnight.sh`, `results/`, `docs/` | ~11.000 | Tái lập, code chết, tài liệu sai lệch |

**Thí nghiệm kế hoạch phải cân nhắc:**

- Ablation: tắt từng nhóm healer, heuristic rồi replay offline, đo PA theo từng nhóm.
- Hold-out theo doanh nghiệp: tách một phần `biz_*` không dùng khi phát triển. Nếu mọi `biz_*` đều đã bị dùng để sửa lỗi thì phải nói thẳng rằng không còn dữ liệu sạch, và đề xuất cách thay thế.
- Kiểm tra độc lập: chọn mẫu câu trả lời, tính lại đáp án bằng SQL viết tay.
- Độ nhạy của cổng abstain với từng ngưỡng theo luật +1/−1/0.
- Đột biến dữ liệu: đổi tên cột, thứ tự, định dạng ngày để đo độ bền.
- Mutation test có chọn lọc để đo chất lượng bộ test.

Với mỗi thí nghiệm, phải nêu: dữ liệu dùng, lệnh hoặc script cần viết, có gọi LLM không, ước tính thời gian và chi phí, cách đọc kết quả.

## Phân công cho sub agent

Việc lập kế hoạch, đánh giá pipeline, thiết kế kiến trúc và mọi kết luận cuối do agent mạnh thực hiện. `gemini-3.8-flash-high` chỉ được làm các việc đúng `SUBAGENT_RULES.md`, và ở đây chỉ **đọc, không sửa**:

- Được giao: liệt kê vị trí theo tiêu chí cơ học, ví dụ mọi chuỗi tiếng Việt và hằng số trong một healer, mọi `except` rộng, hàm không được gọi.
- Không được giao: đánh giá logic đúng sai, xác định overfit, phân tích nguyên nhân, kết luận mức nghiêm trọng.

Với mỗi gói, ghi rõ phần nào giao worker, phần nào người đánh giá chính làm, và cách xác minh đầu ra của worker. Các gói chạy song song phải độc lập về đầu ra.

`SUBAGENT_RULES.md` hiện viết cho worker sửa file. Nếu cần chế độ chỉ đọc, hãy nêu phần bổ sung cần thêm và cách kiểm chứng trước khi dùng.

## Chuẩn bằng chứng cho giai đoạn đánh giá

Kế hoạch phải áp dụng chuẩn này. Mỗi phát hiện cần có: `file:dòng`, input tái hiện, kết quả thực tế, kết quả đúng và căn cứ, tác động lên điểm, mức tin cậy.

| Mức | Định nghĩa |
|---|---|
| Nghiêm trọng | Sai đáp án hàng loạt, rò rỉ gold, kết quả điểm không tin cậy, lộ bí mật |
| Cao | Sai một nhóm câu hỏi, âm thầm trả lời khi đáng lẽ abstain |
| Trung bình | Sai ở trường hợp biên, abstain thừa, thiếu xử lý lỗi |
| Thấp | Bảo trì, tài liệu, phong cách |

Phát hiện chưa tái hiện được phải ghi "giả thuyết". Ý kiến về phong cách code không được xếp trên mức Thấp.

## Đầu ra: `REVIEW_PLAN.md`

Viết bằng tiếng Việt, theo đúng thứ tự:

1. **Tóm tắt:** mục tiêu, cách tiếp cận, ba rủi ro lớn nhất, tổng thời gian và chi phí ước tính.
2. **Luồng thực tế của hệ thống**, kèm `file:hàm` cho từng bước.
3. **Hiện trạng số điểm:** nguồn của từng con số, cái nào so sánh được với cái nào.
4. **Bản đồ rủi ro:** câu sai và câu abstain theo nhóm, nối với module nghi ngờ.
5. **Các gói đánh giá:** với mỗi gói gồm phạm vi, câu hỏi phải trả lời, phương pháp, người thực hiện, đầu ra, tiêu chí hoàn thành, ước tính công sức, phụ thuộc.
6. **Thí nghiệm định lượng**, theo yêu cầu ở trên.
7. **Thứ tự thực hiện và phần chạy song song**, ưu tiên việc có thể làm thay đổi kết luận về năng lực thực.
8. **Kế hoạch giai đoạn 2:** cách đo quy lỗi theo tầng, giá trị từng tầng, trace hoặc replay cần bổ sung, tiêu chí giữ, sửa, thay thế, bỏ.
9. **Kế hoạch giai đoạn 3:** quy trình thiết kế, bộ tiêu chí so sánh, các giả định kiến trúc cần bản thử, dữ liệu dành riêng cho bản thử.
10. **Ma trận truy vết:** mỗi câu hỏi của giai đoạn 1 và 2 nối với gói và thí nghiệm tương ứng, và đầu ra của chúng nối với quyết định kiến trúc nào ở giai đoạn 3.
11. **Cổng phê duyệt:** ai duyệt, duyệt dựa trên gì, điều kiện dừng hoặc đổi hướng sau mỗi giai đoạn.
12. **Mẫu báo cáo** cho cả ba sản phẩm và định dạng phát hiện.
13. **Ngoài phạm vi:** những gì không làm và lý do.
14. **Câu hỏi cần người dùng quyết định**, kèm đề xuất mặc định của bạn.

## Tự kiểm tra trước khi kết thúc

- Mỗi file trong `bizreason/` thuộc đúng ít nhất một gói.
- Mỗi câu hỏi của giai đoạn 1 và 2 có phương pháp trả lời.
- Giai đoạn 2 đo được từng tầng bằng số liệu, không chỉ dựa vào đọc code.
- Giai đoạn 3 chưa chốt kiến trúc, có phương án mốc "giữ và sửa có mục tiêu", và có dữ liệu riêng cho bản thử.
- Mọi đường dẫn, tên hàm, con số trong kế hoạch đã được kiểm tra với repo.
- Không có thí nghiệm nào gọi LLM mà không ghi chi phí và không cần phê duyệt.
- Mọi nhận định chưa có bằng chứng đều được đánh dấu là giả thuyết.
- `git status` chỉ có thêm `REVIEW_PLAN.md`, ngoài những thay đổi đã có từ trước.

Khi xong, trả lời ngắn gọn: đường dẫn file, ba rủi ro lớn nhất, và các quyết định cần người dùng đưa ra. Không tự bắt đầu đánh giá hay thiết kế.
