# Nghiên cứu các cuộc thi và lời giải tương tự DataMate: nên lấy gì, không nên lấy gì

Ngày viết: 2026-11-11. Phạm vi: chỉ đọc web và ba file dự án được phép (`FABLE_REVIEW.md`, `rulebook_public.md`, `LOOP_LOG_A.md`). Không sửa code, không chạy gì, không clone repo, không gửi dữ liệu dự án ra ngoài.

Cách đánh dấu:

- **[WEB]**: hôm nay tôi đã mở trang nguồn và đọc được con số hoặc nội dung đó (có liên kết).
- **[WEB-phụ]**: chỉ thấy qua kết quả tìm kiếm hoặc nguồn thứ cấp, chưa mở được trang gốc.
- **[NHỚ]**: từ trí nhớ, hôm nay chưa tra lại. Cần kiểm trước khi dựa vào.
- **[ƯỚC LƯỢNG]**: suy luận của tôi, chưa có phép đo.

Giấy phép và ngày cập nhật của repo lấy từ GitHub API; ngày tạo, giấy phép và số tham số của model lấy từ Hugging Face API, đều tra hôm nay.

---

## 0. Tóm tắt một trang

1. **Tài liệu ủng hộ hướng A, và không có bằng chứng nào đủ mạnh để chuyển sang hướng B làm nền chính.**
   - Thử nghiệm của dbt Labs (04/2026): tầng ngữ nghĩa đạt 98–100% trên câu nó phủ được, so với 84–90% của sinh SQL tự do. Khi không phủ được, tầng ngữ nghĩa báo "không trả lời được" thay vì trả số sai. Với thang +1 / −1 / 0, đây đúng là hành vi cần có. [WEB]
   - TrustSQL (chấm có phạt câu sai): không phương pháp sinh SQL nào thắng ổn định một hệ thống "luôn abstain" khi mức phạt cao. [WEB-phụ]
   - Model mở cỡ 7–8B sinh code tự do chỉ đạt khoảng 65–68% trên DataBench (bảng sạch, câu hỏi tiếng Anh) và 68–72% trên BIRD. Quy sang thang của ta, đó là PA khoảng 0,3–0,4 nếu trả lời hết. [WEB]
2. **Phát hiện gấp nhất là về phục vụ model.** vLLM bản gốc không chạy được Gemma 4 trên T4: lỗi bộ nhớ chia sẻ của nhân attention, bản sửa (PR #39018) đến 28/10/2026 vẫn chưa được gộp. Có bản vá, và một bài đo công khai cho thấy Gemma 4 12B 4-bit trên một T4 đạt 28,5 token/giây một luồng và 216 token/giây khi 16 yêu cầu song song. [WEB]
3. **Google đã phát hành bản QAT 4-bit chính thức của Gemma 4 12B** (GGUF Q4_0 và compressed-tensors W4A16), Apache 2.0, tháng 6/2026. Đây là lý do thêm để làm sớm phép so "model gốc với bản fine-tune SQL" đã có trong kế hoạch. [WEB]
4. **Ba thứ các đội đứng đầu SemEval-2025 Task 8 làm mà ta chưa làm**, đều hợp với hướng A:
   - mô tả cột có thống kê và giá trị mẫu thay cho bảng thô (bỏ đi mất 5,4 điểm, là thành phần lớn nhất);
   - dự đoán kiểu đáp án bằng một bước riêng (thêm 3,3–6,9 điểm);
   - nối giá trị trong câu hỏi với ô dữ liệu bằng khớp chuỗi rồi để model chọn. [WEB]
5. **Bỏ phiếu nhiều mẫu không nên là nguồn tăng độ chính xác.** Một nghiên cứu 2026 trên BIRD với model mở: bỏ phiếu đổi tối đa 0,13 điểm với chi phí gấp 5 lần token; tự sửa theo lỗi thực thi là thành phần duy nhất có lợi ổn định (1,2–4,4 điểm). Nên dùng nhiều mẫu làm tín hiệu abstain, không phải để tăng số câu đúng. [WEB]

Danh sách 12 việc nên lấy, có xếp hạng, ở mục 5.2.

---

## 1. Các cuộc thi và benchmark tương tự

### 1.1 Bảng tổng hợp

| Benchmark | Bài toán, dữ liệu | Thang đo | Kết quả đứng đầu | Giống ta ở đâu |
|---|---|---|---|---|
| SemEval-2025 Task 8 (DataBench) | Hỏi đáp trên bảng thật, đáp án có kiểu (số, chuỗi, bool, danh sách); bản Lite cắt còn 20 dòng | Độ chính xác khớp đáp án | TeleAI 93,87 / 91,76; AILS-NTUA 85,63 (89,85 sau chấm tay); Samsung 86,21 [WEB, số do đội tự báo] | Gần nhất: đáp án có kiểu, chấm khớp tuyệt đối |
| BIRD | Text-to-SQL, 95 CSDL, giá trị bẩn, có "evidence" đi kèm câu hỏi | Độ chính xác thực thi (EX) | Hệ thống kín 80–82% [WEB-phụ]; model mở nhỏ: Arctic-Text2SQL-R1-7B 68,47, CSC-SQL 7B 71,72 [WEB] | "Evidence" đóng vai trò như rulebook |
| Spider 2.0 (Lite, Snow) | Text-to-SQL doanh nghiệp, lược đồ hàng nghìn cột, nhiều phương ngữ SQL | EX | Snow 96,70; Lite 76,23, đều là agent dùng model lớn [WEB] | Ít: ta không có lược đồ khổng lồ |
| MultiSpider 2.0 | Spider 2.0 dịch sang 8 ngôn ngữ, có tiếng Việt | EX | Khoảng 12–16% với agent, 4–5% nếu chỉ suy luận [WEB] | Cho thấy câu hỏi không phải tiếng Anh làm giảm điểm |
| TableBench | 886 mẫu, 18 loại câu hỏi trên bảng (số học, phân tích, biểu đồ) | Khớp đáp án | Model mạnh nhất vẫn cách xa người; sinh code bị chặn bởi tỉ lệ code chạy được (GPT-4-Turbo 78,67%) [WEB-phụ] | Suy luận số trên bảng |
| WikiTableQuestions, TAT-QA, FinQA | Hỏi đáp trên bảng nhỏ, bảng lẫn văn bản tài chính | Khớp đáp án | WikiTQ: Chain-of-Table khoảng 67%, Binder khoảng 65%, DATER khoảng 66% [NHỚ] | Phép tính số; bảng nhỏ nên ít giống |
| DABstep (Adyen) | Khoảng 450 câu phân tích dữ liệu thanh toán, phải đọc tài liệu luật nghiệp vụ đi kèm; 84% là câu khó | Khớp đáp án | Bài báo gốc: agent tốt nhất 14,55% trên câu khó [WEB-phụ]. NVIDIA đứng đầu 03/2026 [WEB-phụ] | Rất gần: có tài liệu luật riêng phải đọc mới tính đúng |
| InfiAgent-DABench | 257 câu trên 52 file CSV, đáp án dạng đóng | Khớp đáp án | GPT-4 khoảng 79% [WEB-phụ] | Phân tích CSV, đáp án đóng |
| Tianchi NL2SQL lần 1 (2019) | Câu hỏi tiếng Trung trên bảng nghiệp vụ, một bảng | Độ chính xác | 92,19%, 1457 đội [WEB-phụ] | Câu hỏi không phải tiếng Anh, giá trị viết khác trong bảng |
| ViText2SQL (2020) | Spider dịch tay sang tiếng Việt | Khớp chính xác | Khoảng 50–60% với model thời đó [NHỚ] | Tiếng Việt, nhưng câu hỏi chuẩn, không lóng |
| Thử nghiệm tầng ngữ nghĩa của dbt và Cube | 11 câu (dbt), 100 câu (Cube) trên dữ liệu bán lẻ, bảo hiểm | Độ chính xác | Tầng ngữ nghĩa 98–100% so với 84–90% [WEB]; Cube: thêm 17–23 điểm [WEB-phụ] | Chính là hướng A |
| TrustSQL | Text-to-SQL có câu không trả lời được, chấm có phạt | Điểm tin cậy có phạt | Không phương pháp nào thắng ổn định "luôn abstain" ở mức phạt cao [WEB-phụ] | Cùng kiểu thang +1 / −1 / 0 |

Những thứ tôi tìm mà **không thấy**:

- Không có cuộc thi Kaggle hay AIcrowd nào đúng dạng "hỏi đáp trên file Excel/CSV lộn xộn". Gần nhất về ràng buộc phần cứng là Amazon KDD Cup 2024 (mục 1.6).
- VLSP 2025 "Semantic Parsing" là bài AMR, không phải text-to-SQL. [WEB-phụ]
- Zalo AI Challenge 2025 có hai đề RoadBuddy (hỏi đáp video hành trình) và AeroEyes; năm 2022 có đề hỏi đáp đầu-cuối. Không tìm được bài viết lời giải nào dùng được cho ta. [WEB-phụ]

### 1.2 SemEval-2025 Task 8: bài học chi tiết

Tôi không đọc được file PDF bài tổng quan chính thức (công cụ chỉ trả về dữ liệu nhị phân), nên thứ hạng dưới đây là do các đội tự báo. Trang tóm tắt của bài tổng quan chỉ nêu: cách tiếp cận dùng LLM áp đảo, model lớn hơn tốt hơn rõ rệt. [WEB]

**TeleAI, "TableReasoner"** (đội tự báo hạng nhất cả hai nhánh) [WEB: https://arxiv.org/html/2507.08046v1]

- Dựng "lược đồ bảng": mỗi cột có kiểu, thống kê, vài dòng mẫu, rồi model viết thêm mô tả cột. Chi phí token tăng theo số cột, không theo số dòng.
- Nối lược đồ: model tách câu hỏi, chọn cột liên quan. Với thực thể, tìm ô ứng viên bằng dãy con chung dài nhất (ngưỡng trùng 0,6), rồi model chọn ô đúng.
- Viết lại câu hỏi thành các câu con gắn tên cột, sinh Python, chạy, tóm tắt đáp án theo kiểu yêu cầu.
- LoRA trên dữ liệu "lấy mẫu rồi loại": chỉ giữ lời giải cho đáp án đúng (1184 trên 1308 mẫu). Bỏ phiếu 5 mẫu.
- Mức đóng góp (Qwen2.5-32B): bỏ lược đồ bảng mất 5,37 điểm; bỏ nối lược đồ mất 2,30; bỏ viết lại câu hỏi mất 1,34; fine-tune thêm 1,92; bỏ phiếu thêm 1,34.

**AILS-NTUA** (hạng nhất nhánh model kín) [WEB: https://arxiv.org/html/2503.00435v2]

- Prompt bốn phần: mô tả nhiệm vụ, mô tả cột (chỉ số, tên, kiểu, số ô khác rỗng, tối đa 5 giá trị mẫu có dấu nháy để lộ khoảng trắng thừa), dòng mẫu, cột cần dùng và kiểu đáp án.
- Hai bước: đoán cột cần dùng trước, rồi mới sinh code.
- Vòng sửa lỗi: gửi lại code hỏng và thông báo lỗi, tối đa 3 lần. Thêm trung bình 2,4 điểm; model nhỏ chỉ sửa được tối đa khoảng 30% lỗi.
- Model nhỏ với cùng pipeline: Llama 3.1 8B đạt 65,13; Qwen2.5-Coder 7B đạt 65,33; so với 85,63 của Claude 3.5 Sonnet.

**Samsung** (86,21%, đội tự báo hạng 2 trên 100 đội) [WEB: https://research.samsung.com/blog/SemEval-2025-Task-8-LLM-Ensemble-Methods-for-QA-Over-Tabular-Data]

- Tám model chạy 4-bit bằng llama.cpp, bỏ phiếu đa số; không có đa số thì một model làm trọng tài, có điểm phạt cho truy vấn phức tạp.
- Bộ phân loại kiểu đáp án riêng (ALBERT fine-tune, 96%; kết hợp bỏ phiếu lên 98,28%). Thêm 3,3 điểm cho pandas và 6,9 điểm cho SQL.
- Thêm dòng mẫu vào prompt: 8,8 điểm (pandas), 5,9 điểm (SQL), là mức tăng lớn nhất.
- Năm bản diễn đạt lại câu hỏi: thêm 1–1,9 điểm. Vòng sửa lỗi: thêm 1–1,9 điểm.
- Bước chọn cột giúp nhẹ pandas nhưng **làm hại** SQL.

**Team Anotheroption** (chỉ model mở, 80,08%, hạng 13 trên 38) [WEB: https://arxiv.org/html/2506.09657v2]

- Chạy song song SQL và pandas, một model điều phối chọn đáp án. 63,4% trường hợp các đường cho cùng kết quả.
- Truy hồi 3 dòng gần nghĩa nhất để model thấy cách viết giá trị thật trong bảng.
- Viết lại câu hỏi **làm giảm** điểm (84% xuống 68% trên tập phát triển). Vòng sửa lỗi gần như vô ích.

Kết luận cho ta: thứ có lợi ổn định là mô tả cột giàu bằng chứng, kiểu đáp án và nối giá trị. Viết lại câu hỏi và chọn cột cho kết quả trái chiều giữa các đội, khớp với phép đo nội bộ của ta (viết lại làm hỏng mã thời gian).

### 1.3 BIRD và Spider 2.0

- **Model mở nhỏ trên BIRD.** Arctic-Text2SQL-R1 (Snowflake, 05/2025): 7B đạt 68,47; 14B đạt 70,04; 32B đạt 71,83. Huấn luyện bằng học tăng cường (GRPO) với phần thưởng duy nhất là "SQL chạy và ra đúng kết quả"; phần thưởng chi tiết hơn gây gian lận phần thưởng. [WEB: https://www.snowflake.com/en/blog/engineering/arctic-text2sql-r1-sql-generation-benchmark/]
- **CSC-SQL** (05/2025): sinh nhiều mẫu, nhóm theo kết quả thực thi, đưa hai nhóm lớn nhất cho một model "hợp nhất và sửa". 7B đạt 69,19 (dev) và 71,72 (test). Hơn bỏ phiếu thường 0,72–5,54 điểm. [WEB-phụ: https://arxiv.org/abs/2505.13271]
- **Đo độc lập trên model mở, không fine-tune** (06/2026): Qwen2.5-Coder 7B / 14B / 32B đạt 42,6 / 52,0 / 54,9. Tự sửa theo lỗi thực thi thêm 1,2–4,4 điểm; nối lược đồ không có lợi có ý nghĩa; bỏ phiếu đổi tối đa 0,13 điểm với gấp 5 lần token (chỉ thử trên một model). [WEB: https://arxiv.org/abs/2606.29733]
- **CHASE-SQL, XiYan-SQL, CHESS**: nhiều bộ sinh khác nhau rồi một model chọn; CHESS có truy hồi giá trị bằng LSH cộng khoảng cách sửa cộng nhúng. Đều dựa vào model cỡ GPT-4 hoặc Gemini và hàng chục lần sinh mỗi câu. [NHỚ]
- **Spider 2.0**: các hạng đầu đều là agent thương mại với model lớn (Gemini 3 Pro, Claude Sonnet 4.5, Claude Opus 4.6, GLM 5.2). [WEB: https://spider2-sql.github.io/]. ReFoRCE (Snowflake) có ba ý đáng nhớ: khám phá cột bằng truy vấn thử, ràng buộc định dạng đáp án, tự tinh chỉnh. [WEB-phụ]
- **MultiSpider 2.0**: cách giảm thiệt hại ngôn ngữ là bản địa hóa cả câu hỏi lẫn lược đồ và dùng từ điển lược đồ song ngữ; lỗi nối lược đồ chiếm 33% số lỗi được phân tích. [WEB: https://arxiv.org/html/2509.24405v1]

### 1.4 DABstep: mẫu hình gần hướng A nhất

Theo bản tóm tắt bài viết của NVIDIA (03/2026), lời giải đứng đầu có ba pha: pha học dùng model lớn viết một thư viện hàm trợ giúp tái sử dụng từ các bài mẫu; pha suy luận dùng model nhỏ gọi thư viện đó; pha kiểm tra. [WEB-phụ, tôi chỉ đọc được bản sao và bản tóm tắt]

Đây chính là ý "biên dịch rulebook thành thư viện định nghĩa một lần, rồi model nhỏ chỉ chọn trong danh mục" của mốc A3. Khác biệt: họ dựng thư viện ngoài giờ bằng model lớn, còn ta phải dựng lúc chạy bằng model 12B vì rulebook private chưa biết. Vì vậy phần thư viện dựng sẵn ngoài giờ (cho các luật đã thấy ở train và public) nên càng đầy càng tốt, và phần dựng lúc chạy chỉ nên là "khớp và điền tham số".

### 1.5 Tầng ngữ nghĩa và chấm có phạt

Thử nghiệm của dbt (07/04/2026), 11 câu, mỗi câu chạy 20 lần [WEB: https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026]:

| Model | Sinh SQL tự do | Qua tầng ngữ nghĩa |
|---|---|---|
| claude-sonnet-4-6 | 90,0% | 98,2% |
| gpt-5.3-codex | 84,1% | 100,0% |

- Câu cần quá nhiều bước nối bảng: tầng ngữ nghĩa 0% (báo lỗi, không trả số), sinh SQL tự do 70–100%.
- Tác giả mô tả: tầng ngữ nghĩa "không bao giờ trả dữ liệu sai"; sinh SQL tự do hỏng theo kiểu "đáp án nghe hợp lý nhưng sai".
- Quy trình họ khuyên: thử tầng ngữ nghĩa trước; nếu không phủ, xem có thể thêm định nghĩa nhỏ không; cuối cùng mới rơi về sinh SQL.
- Hạn chế: nhà cung cấp tự đo, mẫu nhỏ, model lớn.

Ý nghĩa cho ta: quy trình của dbt trùng với thứ tự "A là nền, B là đường dự phòng". Điểm khác là với thang có phạt, đường dự phòng chỉ nên trả lời khi xác suất đúng trên 0,5.

### 1.6 Cuộc thi có ràng buộc phần cứng giống ta

Amazon KDD Cup 2024 (giới hạn thời gian 20–140 phút, 4 GPU): đội NVIDIA thắng mọi nhánh bằng Qwen2-72B fine-tune, lượng tử hóa AWQ 4-bit, phục vụ bằng vLLM, và dùng bộ xử lý logit để ép đầu ra về các token hợp lệ của từng nhiệm vụ. Họ ghi nhận GPTQ-Int4 cho kết quả gần như AWQ. [WEB-phụ: https://arxiv.org/abs/2408.04658]. Tôi không xác nhận được loại GPU là T4.

Bài học: lượng tử hóa 4-bit cộng vLLM cộng ràng buộc đầu ra là công thức chuẩn của người thắng khi có giới hạn giờ.

---

## 2. Mã nguồn công khai

"Dùng lại" có ba mức: **dùng trực tiếp**, **mượn ý**, **không áp dụng**.

### 2.1 Text-to-SQL

| Repo | Làm gì | Giấy phép | Cập nhật cuối | Dùng lại | Phần nên lấy |
|---|---|---|---|---|---|
| [CHESS](https://github.com/ShayanTalaei/CHESS) | Truy hồi thực thể và ngữ cảnh, lọc lược đồ, sinh và sửa SQL | Apache-2.0 | 05/2025 | Mượn ý | Truy hồi giá trị: LSH, khoảng cách sửa, nhúng [NHỚ về chi tiết] |
| CHASE-SQL | Nhiều bộ sinh, model chọn theo cặp | Không có code chính thức [NHỚ] | — | Không áp dụng | Cần model lớn và nhiều mẫu |
| [DIN-SQL](https://github.com/MohammadrezaPourreza/Few-shot-NL2SQL-with-prompting) | Chia bài toán, tự sửa, dùng GPT-4 | MIT | 03/2024 | Không áp dụng | Phân loại độ khó câu hỏi (ý nhỏ) |
| [DAIL-SQL](https://github.com/BeachWang/DAIL-SQL) | Chọn ví dụ few-shot theo độ giống câu hỏi đã che thực thể | Apache-2.0 | 03/2025 | Mượn ý | Che số, tên rồi mới tính độ giống khi truy hồi ví dụ |
| [MAC-SQL](https://github.com/wbbeyourself/MAC-SQL) | Ba agent: chọn, phân rã, sửa | Không ghi | 02/2025 | Không áp dụng | Không ghi giấy phép; nhiều lượt gọi |
| [XiYan-SQL](https://github.com/XGenerationLab/XiYan-SQL) | Nhiều bộ sinh fine-tune, model chọn, định dạng lược đồ M-Schema | Apache-2.0 | 05/2026 | Mượn ý | M-Schema: cách trình bày lược đồ kèm kiểu, khóa, giá trị mẫu |
| [OmniSQL](https://github.com/RUCKBReasoning/OmniSQL) | Sinh 2,5 triệu mẫu tổng hợp, model 7B/14B/32B | Repo không ghi; model Apache-2.0 | 09/2025 | Mượn ý | Quy trình sinh dữ liệu tổng hợp có kiểm bằng thực thi (cho hướng C) |
| [CodeS](https://github.com/RUCKBReasoning/codes) | Model SQL 1–15B, bộ lọc lược đồ, truy hồi giá trị bằng BM25 | Apache-2.0 | 11/2024 | Mượn ý | Chỉ mục BM25 trên giá trị ô |
| [RESDSQL](https://github.com/RUCKBReasoning/RESDSQL) | Xếp hạng lược đồ rồi giải mã khung trước | MIT | 05/2024 | Mượn ý | "Khung trước, chi tiết sau", gần với IR |
| [PICARD](https://github.com/ServiceNow/picard) | Giải mã ràng buộc bằng phân tích cú pháp tăng dần | Apache-2.0 | 10/2023 | Mượn ý | Ý tưởng; hiện thực nên dùng XGrammar hoặc GBNF |
| [CSC-SQL](https://github.com/CycloneBoy/csc_sql) | Bỏ phiếu theo kết quả thực thi cộng model hợp nhất | Apache-2.0 | 08/2025 | Mượn ý | Nhóm ứng viên theo **kết quả**, không theo văn bản |
| [ReFoRCE](https://github.com/Snowflake-Labs/ReFoRCE) | Agent Spider 2.0 | Apache-2.0 | 08/2025 | Mượn ý | Ràng buộc định dạng đáp án; truy vấn thăm dò cột |
| [Spider2 / Spider-Agent](https://github.com/xlang-ai/Spider2) | Benchmark và agent nền | MIT | 08/2026 | Không áp dụng | Agent nhiều bước, vượt ngân sách |
| [ArcticTraining](https://github.com/snowflakedb/ArcticTraining) | Code huấn luyện Arctic-Text2SQL-R1 | Chưa tra | — | Mượn ý | Phần thưởng thực thi đơn giản, nếu sau này huấn luyện |
| [TrustSQL](https://github.com/glee4810/TrustSQL) | Benchmark abstain có phạt | CC-BY-4.0 | 05/2026 | Mượn ý | Cách báo cáo điểm theo nhiều mức phạt |

### 2.2 Hỏi đáp trên bảng

| Repo | Làm gì | Giấy phép | Cập nhật cuối | Dùng lại | Phần nên lấy |
|---|---|---|---|---|---|
| [AILS-NTUA tabularqa](https://github.com/andrewevag/tabularqa) | Lời giải SemEval | Không ghi | 05/2025 | Mượn ý | Mẫu mô tả cột (kiểu, số ô khác rỗng, 5 giá trị mẫu có nháy) |
| [Samsung semeval2025-task8](https://github.com/SamsungLabs/semeval2025-task8) | Lời giải SemEval, 8 model | Không ghi | 07/2025 | Mượn ý | Bộ phân loại kiểu đáp án; bản báo cáo tóm tắt bảng |
| [Team Anotheroption](https://github.com/Nickolas-option/QA_on_Tabular_Data_SemEval2025_Task8) | SQL cộng pandas, model mở | Không ghi | 04/2025 | Mượn ý | Truy hồi dòng gần nghĩa để lộ cách viết giá trị |
| TableReasoner (TeleAI) | Lời giải SemEval | Không tìm thấy repo | — | Mượn ý | Nối giá trị bằng dãy con chung dài nhất rồi model chọn |
| [databench_eval](https://github.com/jorses/databench_eval) | Bộ chấm DataBench | MIT | 05/2025 | Mượn ý | Cách so đáp án theo kiểu |
| [TableLLM](https://github.com/RUCKBReasoning/TableLLM) | Model 8B/13B thao tác bảng văn phòng | Không ghi | 08/2025 | Không áp dụng | Không ghi giấy phép; tiếng Anh |
| [tablegpt-agent](https://github.com/tablegpt/tablegpt-agent) | Agent cho TableGPT2-7B | Apache-2.0 | 10/2026 | Không áp dụng | 7B thêm vào 12B vượt 15B |
| [StructLM](https://github.com/TIGER-AI-Lab/StructLM) | Model tổng quát cho dữ liệu có cấu trúc | MIT | 10/2024 | Không áp dụng | Nền CodeLlama, cũ |
| [Chain-of-Table](https://github.com/google-research/chain-of-table) | Biến đổi bảng từng bước | Apache-2.0 | 06/2024 | Không áp dụng | Nhiều lượt gọi mỗi câu, cho bảng nhỏ |
| [Binder](https://github.com/xlang-ai/Binder) | SQL có lời gọi LLM nhúng bên trong | Apache-2.0 | 08/2023 | Mượn ý | Hàm LLM trong truy vấn cho cột văn bản tự do (ví dụ ô "Làm gì" của sổ sửa xe), chạy một lần lúc dựng bảng |
| DATER | Tách bảng và câu hỏi | Trong repo DAMO-ConvAI [NHỚ] | — | Không áp dụng | Cần Codex |
| [ReAcTable](https://github.com/yunjiazhang/ReAcTable) | ReAct trên bảng | Không ghi | 04/2024 | Không áp dụng | Nhiều lượt gọi |
| [InfiAgent](https://github.com/InfiAgent/InfiAgent) | Benchmark DABench | Apache-2.0 | 05/2025 | Mượn ý | Kỹ thuật "ép dạng đóng" cho đáp án |
| [TableBench](https://github.com/TableBench/TableBench) | Benchmark | Không ghi | 05/2025 | Không áp dụng | — |

### 2.3 Làm sạch bảng tính, tầng ngữ nghĩa, giải mã ràng buộc

| Repo | Làm gì | Giấy phép | Cập nhật cuối | Dùng lại | Phần nên lấy |
|---|---|---|---|---|---|
| [Auto-Tables-Benchmark](https://github.com/LiPengCS/Auto-Tables-Benchmark) | 244 ca bảng không quan hệ thật và phép biến đổi đúng (VLDB 2023) | MIT | 11/2026 | Mượn ý | Bộ toán tử: stack, wide-to-long, transpose, pivot. Dùng làm bộ sinh nhiễu cho phép đo 3 và làm danh sách kiểm cho ingestion |
| SpreadsheetLLM (Microsoft) | Mã hóa bảng tính bằng "mỏ neo cấu trúc"; dò vùng bảng F1 78,9% | Không có code chính thức [WEB-phụ] | — | Mượn ý | Giữ các dòng, cột ở ranh giới vùng đồng nhất để dò tiêu đề và vùng bảng |
| [Spreadsheet_LLM_Encoder](https://github.com/kingkillery/Spreadsheet_LLM_Encoder) | Bản cài đặt không chính thức | MIT | 08/2026 | Mượn ý | Tham khảo cách cài; chưa kiểm chất lượng |
| TableSense (Microsoft) | CNN dò vùng bảng | Không có code chính thức [NHỚ] | — | Không áp dụng | — |
| [CleverCSV](https://github.com/alan-turing-institute/CleverCSV) | Dò dấu phân cách, kiểu trích dẫn của CSV lộn xộn | MIT | 08/2026 | Dùng trực tiếp | Dò phương ngữ CSV nếu private có CSV lạ |
| [docling](https://github.com/docling-project/docling) | Chuyển tài liệu, có xlsx | MIT | 11/2026 | Không áp dụng | Hướng tới tài liệu, không phải sổ bán hàng |
| `messy-xlsx` (PyPI) | Dò tiêu đề "thông minh" cho Excel | Chưa tra | — | Chưa rõ | Cần đọc mã trước khi tin [WEB-phụ] |
| [MetricFlow](https://github.com/dbt-labs/metricflow) | Định nghĩa đại lượng bằng dữ liệu, biên dịch ra SQL | Apache-2.0 | 11/2026 | Mượn ý | Mô hình khái niệm: thực thể, chiều, số đo, và bốn loại đại lượng (đơn, tỉ lệ, lũy kế, dẫn xuất) [NHỚ về bốn loại] |
| [dbt-llm-sl-bench](https://github.com/dbt-labs/dbt-llm-sl-bench) | Bộ đo tầng ngữ nghĩa với LLM | Giấy phép riêng | 08/2026 | Mượn ý | Cách tách điểm "phủ được" và "không phủ được" |
| [XGrammar](https://github.com/mlc-ai/xgrammar) | Giải mã ràng buộc nhanh theo ngữ pháp, JSON schema | Apache-2.0 | 11/2026 | Dùng trực tiếp | Mặc định trong vLLM [NHỚ] |
| [Outlines](https://github.com/dottxt-ai/outlines) | Sinh có cấu trúc cho transformers | Apache-2.0 | 09/2026 | Dùng trực tiếp | Nếu ở lại máy chủ HF hiện tại |
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | Máy chủ GGUF, có khe song song, GBNF, JSON schema | MIT | 11/2026 | Dùng trực tiếp | Xem mục 3 |
| [vLLM](https://github.com/vllm-project/vllm) | Máy chủ gom lô liên tục | Apache-2.0 | 11/2026 | Dùng trực tiếp, cần vá | Xem mục 3 |

Lưu ý giấy phép: các repo "không ghi" giấy phép (MAC-SQL, TableLLM, ba repo SemEval, ReAcTable, TableBench) về pháp lý là giữ mọi quyền. Chỉ đọc để lấy ý, không chép mã.

---

## 3. Thông lượng phục vụ trên T4×2

### 3.1 Tình hình từng lựa chọn

| Lựa chọn | Chạy được Gemma 4 trên T4? | Gom lô | Ràng buộc JSON | Rủi ro chính |
|---|---|---|---|---|
| **vLLM + bản vá Turing + W4A16** | Có, **chỉ khi vá**. Bản gốc lỗi "shared memory Required 98304, limit 65536" ở yêu cầu đầu tiên. PR #39018 chưa gộp tính đến 28/10/2026; issue #38918 còn mở, vẫn lỗi ở 0.29.0 [WEB] | Liên tục, có dùng chung tiền tố | Có (XGrammar) | Phụ thuộc bản vá ngoài luồng; phải dùng `float16`; KV cache nhỏ |
| **llama.cpp `llama-server` + GGUF** | Có từ bản b8637 (PR #21309, gộp 02/04/2026), sau đó có các bản sửa tokenizer [WEB-phụ] | Khe song song (`-np`), gom lô liên tục mặc định [WEB-phụ] | Có (GBNF, JSON schema) [NHỚ] | Chưa có số đo song song trên T4; ngữ cảnh chia đều cho các khe; phải build bản CUDA trên Kaggle |
| **HF transformers + bitsandbytes, gom lô tĩnh** | Có (đang chạy) | Tự gom: đệm trái, sinh theo lô | Qua Outlines | Nhân 4-bit của bitsandbytes chậm; lô phải có độ dài gần nhau [NHỚ] |
| HF continuous batching | Có API trong transformers gần đây [NHỚ] | Có | Chưa rõ | Còn mới, ít người dùng |
| SGLang | Một báo cáo: wheel nhân của SGLang không chạy trên T4 [WEB-phụ] | — | — | Không khuyên |
| TGI | Đã vào chế độ bảo trì [NHỚ] | — | — | Không khuyên |
| ExLlamaV2 | Gom lô cần flash-attn, vốn đòi GPU Ampere trở lên [NHỚ, WEB-phụ] | Không trên T4 | — | Không khuyên |

### 3.2 Số đo công khai duy nhất tôi tìm được cho đúng cấu hình

Bài đo trên AWS (một T4, vLLM 0.30.0 có vá, Gemma 4 12B QAT 4-bit, `float16`, ngữ cảnh 8192) [WEB: https://builder.aws.com/content/3K3ZnLrNaDhh6xnnUAsP2LbJZVQ/gemma-4-on-amazon-sagemaker-the-nvidia-t4-decodes-at-08x-of-the-l4-with-the-same-answers]:

| Chỉ số | T4 | L4 |
|---|---|---|
| Giải mã một luồng | 28,5 token/giây | 35,0 |
| Tổng giải mã khi 16 yêu cầu song song | 216 token/giây | 412 |
| Trọng số | 7,36 GiB | 7,36 GiB |
| KV cache còn lại | khoảng 20.354 token | khoảng 52.541 token |

- Song song 16 cho tổng thông lượng gấp khoảng 7,6 lần một luồng trên T4.
- Đáp án trên T4 và L4 giống nhau ở cả 40 câu thử.
- Bài không nêu độ dài prompt và đầu ra, nên không quy thẳng ra "câu mỗi giây" của ta được.
- Các báo cáo khác trong PR #39018: Gemma 4 12B và 31B chạy trên hai T4 sau khi vá; cache KV dạng FP8 bị từ chối trên T4. [WEB]

Số cho llama.cpp trên T4 với 12B: chỉ có ước lượng khoảng 25–30 token/giây một luồng từ các trang không đáng tin; không có số đo song song. [WEB-phụ]

### 3.3 Cạm bẫy đã biết

- **T4 không có bf16.** Phải đặt `dtype=half`. Với Gemma 3, có báo cáo `float16` cho đầu ra rỗng hoặc không ổn định trong vLLM; với Gemma 4 QAT W4A16 thì bài đo trên cho đáp án đúng. Cần tự kiểm trên 100 câu. [WEB]
- **KV cache trên T4 chỉ khoảng 20 nghìn token cho 12B.** Prompt hiểu câu hỏi của ta dài (prompt hệ thống khoảng 80 dòng). Nếu mỗi yêu cầu 2.000 token thì chỉ chứa được khoảng 8–10 chuỗi cùng lúc, trừ khi tiền tố chung được dùng chung. Rút ngắn prompt có lợi kép. [ƯỚC LƯỢNG]
- **Model đang dùng là bản fine-tune**, không có sẵn GGUF hay W4A16. Phải tự chuyển (GGUF: một lệnh chuyển và một lệnh lượng tử hóa; W4A16: cần chạy hiệu chỉnh bằng llm-compressor). Model gốc `gemma-4-12B-it` thì có sẵn cả hai bản QAT chính thức. [WEB cho bản QAT; NHỚ cho quy trình chuyển]
- **Gom lô làm mất tính tất định từng bit.** Kết quả của một câu có thể đổi theo các câu cùng lô. Phép kiểm "hai lần chạy giống nhau từng câu" cần nới thành "tỉ lệ câu đổi dưới ngưỡng". [NHỚ]
- **llama.cpp chia ngữ cảnh cho các khe**: `-c 16384 -np 8` cho mỗi khe 2.048 token. Phải đọc log khởi động để biết ngữ cảnh thật của mỗi khe. [WEB-phụ]
- **AWQ trong vLLM** có báo cáo chỉ nhanh hơn ở lô nhỏ, prompt ngắn (đo trên A100). Một tài liệu của Red Hat ghi kiến trúc Turing không có nhân tối ưu cho INT4 trong vLLM; nhưng báo cáo trong issue cho thấy W4A16 nạp được trên T4 qua nhân Marlin. Hai nguồn này mâu thuẫn một phần, nên phải tự đo. [WEB-phụ]

### 3.4 Khuyến nghị: đường tin cậy nhất để đạt ít nhất 3 lần

Đích: hiện khoảng 7 giây mỗi câu trên cả hai GPU, tức 3000 câu mất 5,8 giờ. Cần dưới 2,3 giây mỗi câu.

Tôi khuyên làm theo thứ tự, vì mỗi bước là đường lui của bước sau:

1. **Ngay (vài giờ, không thêm phụ thuộc): gom lô tĩnh trong máy chủ HF hiện tại.** Thay khóa GPU bằng hàng đợi gom 8 yêu cầu, đệm trái, sinh một lần. Kèm theo: rút prompt hệ thống, giới hạn token đầu ra. Kỳ vọng 3–5 lần [ƯỚC LƯỢNG]. Đây là đường ít rủi ro nhất vì không đổi model, không đổi định dạng trọng số. Việc này đã có trong danh sách (mục 3b của nhật ký).
2. **Trong một buổi trên Kaggle: đo `llama-server` với GGUF Q4_0 QAT chính thức của `gemma-4-12B-it`**, mỗi GPU một tiến trình, `-np 8`, bật `cache_prompt`, ràng buộc JSON schema. Đây là ứng viên cho bản nộp cuối: không cần vá, giấy phép MIT, Google tự phát hành GGUF. Kỳ vọng 3–6 lần [ƯỚC LƯỢNG, chưa có số đo công khai].
3. **Cùng buổi đó: đo vLLM có vá với bản W4A16 chính thức.** Đây là đường có trần cao nhất và là đường duy nhất có số đo công khai (7,6 lần khi 16 luồng). Chỉ chọn làm đường chính nếu bản vá chạy ổn hai lần liên tiếp trên Kaggle, hoặc PR được gộp vào bản phát hành.

Bước 2 và 3 chỉ dùng được ngay nếu phép so model (mục 7 của nhật ký) cho thấy model gốc không kém bản fine-tune SQL. Nếu bản fine-tune thắng, phải tự chuyển nó sang GGUF trước.

---

## 4. Model trong giới hạn

Giới hạn: tổng dưới 15 tỷ tham số, phát hành trước 01/10/2026. Ngày và số tham số dưới đây đều [WEB] từ Hugging Face API trừ khi ghi khác.

### 4.1 Model chính (hiểu câu hỏi tiếng Việt, chú thích cột)

| Model | Tham số | Giấy phép | Ngày tạo trên HF | Hợp giới hạn? | Nhận xét |
|---|---|---|---|---|---|
| [google/gemma-4-12B-it](https://huggingface.co/google/gemma-4-12B-it) | 11,96B | Apache-2.0 | 23/05/2026 | Có | Ứng viên số một. Có bản QAT chính thức: [GGUF Q4_0](https://huggingface.co/google/gemma-4-12B-it-qat-q4_0-gguf) (05/06/2026) và [W4A16](https://huggingface.co/google/gemma-4-12B-it-qat-w4a16-ct) (05/06/2026) |
| [Qwen/Qwen3.5-9B](https://huggingface.co/Qwen/Qwen3.5-9B) | 9,65B | Apache-2.0 | 27/02/2026 | Có | Nhỏ hơn nên nhanh hơn, còn dư 5,3B cho model phụ. Chưa có số liệu tiếng Việt lóng; phải tự đo |
| [Qwen/Qwen3-14B](https://huggingface.co/Qwen/Qwen3-14B) | 14,77B | Apache-2.0 | 27/04/2025 | Có, nhưng hết chỗ | Không còn chỗ cho model nhúng (0,57B sẽ vượt 15B) |
| [sail/Sailor2-8B-Chat](https://huggingface.co/sail/Sailor2-8B-Chat) | 8,55B | Apache-2.0 | 03/12/2024 | Có | Chuyên Đông Nam Á, nền Qwen2.5; khả năng xuất JSON và suy luận cần đo |
| [SeaLLMs-v3-7B-Chat](https://huggingface.co/SeaLLMs/SeaLLMs-v3-7B-Chat) | 7,62B | Giấy phép riêng ("other") | 03/07/2024 | Rủi ro giấy phép | Không ưu tiên |
| SEA-LION v4 | Nền Gemma 3 27B [WEB-phụ] | — | cuối 2025 | Không (vượt 15B) | — |

Tôi không tìm được bảng xếp hạng tiếng Việt nào (SEA-HELM, VMLU) có cả Gemma 4 12B và Qwen3.5-9B. Chỉ có phép đo của chính ta (phép đo 1 và 2) trả lời được.

### 4.2 Model chuyên text-to-SQL (cho đường kiểm chứng kiểu hướng B)

| Model | Tham số | Giấy phép | Ngày tạo | Ghép được với model chính nào? |
|---|---|---|---|---|
| [Snowflake/Arctic-Text2SQL-R1-7B](https://huggingface.co/Snowflake/Arctic-Text2SQL-R1-7B) | 7,62B | Apache-2.0 | 22/05/2025 | Không ghép được với Gemma 12B (19,6B). Với Qwen3.5-9B cũng vượt (17,3B) |
| [seeklhy/OmniSQL-7B](https://huggingface.co/seeklhy/OmniSQL-7B) | 7,62B | Apache-2.0 | 06/03/2025 | Như trên |
| [XiYanSQL-QwenCoder-14B-2504](https://huggingface.co/XGenerationLab/XiYanSQL-QwenCoder-14B-2504) | 14,77B | Apache-2.0 | 28/04/2025 | Chỉ dùng một mình |
| XiYanSQL-QwenCoder-3B-2504 | khoảng 3,1B [NHỚ] | Apache-2.0 [WEB-phụ] | 04/2025 | Với Qwen3.5-9B và bge-m3: khoảng 13,3B, vừa. Với Gemma 12B: khoảng 15,05B, **vượt nhẹ** |
| [Qwen2.5-Coder-14B-Instruct](https://huggingface.co/Qwen/Qwen2.5-Coder-14B-Instruct) | 14,77B | Apache-2.0 | 06/11/2024 | Chỉ dùng một mình; 52,0% BIRD không fine-tune [WEB] |
| [tablegpt/TableGPT2-7B](https://huggingface.co/tablegpt/TableGPT2-7B) | 7,62B | Apache-2.0 | 01/11/2024 | Không ghép được với Gemma 12B |

Hệ quả về ngân sách tham số: muốn có một model SQL thứ hai độc lập (để các mẫu không sai giống nhau) thì model chính phải là cỡ 9B và model SQL cỡ 3B. Với Gemma 12B, đường SQL kiểm chứng phải dùng chính Gemma. Các model SQL này đều huấn luyện trên câu hỏi tiếng Anh; câu hỏi tiếng Việt lóng phải được chuyển thành mô tả chuẩn trước, mà bước đó chính là bước hiểu câu hỏi của ta.

### 4.3 Model nhúng (khớp giá trị, truy hồi ví dụ, truy hồi dòng rulebook)

| Model | Tham số | Giấy phép | Ngày tạo | Nhận xét |
|---|---|---|---|---|
| [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) | khoảng 0,57B | MIT | 27/01/2024 | Có điểm từ vựng lẫn ngữ nghĩa; lựa chọn mặc định |
| [AITeamVN/Vietnamese_Embedding](https://huggingface.co/AITeamVN/Vietnamese_Embedding) | 0,568B | Apache-2.0 | 17/03/2025 | bge-m3 tinh chỉnh cho tiếng Việt, miền pháp lý |
| [Qwen/Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | 0,596B | Apache-2.0 | 03/06/2025 | Hơn 100 ngôn ngữ |
| [google/embeddinggemma-300m](https://huggingface.co/google/embeddinggemma-300m) | 0,303B | Giấy phép Gemma (không phải Apache) | 17/07/2025 | Nhỏ nhất; cần đọc điều khoản giấy phép trước khi dùng cho cuộc thi |

Với tên hàng, tên khách ngắn và có lỗi gõ, khớp ký tự (n-gram, dãy con chung dài nhất) thường quan trọng hơn nhúng ngữ nghĩa; đội đứng đầu SemEval dùng đúng cách đó. Nên coi model nhúng là tín hiệu thứ hai, không phải tín hiệu chính.

---

## 5. Tổng hợp cho hệ thống của ta

### 5.1 Ánh xạ phát hiện vào từng tầng

| Tầng | Điều tài liệu nói | Nguồn |
|---|---|---|
| Đọc file (L0) | Bốn phép biến đổi phủ phần lớn bảng không quan hệ: stack, wide-to-long, transpose, pivot. Dò vùng bảng bằng ranh giới vùng đồng nhất | Auto-Tables, SpreadsheetLLM |
| Chú thích (L1) | Cho model xem hồ sơ cột (kiểu, độ phủ, số giá trị khác nhau, 5 giá trị mẫu có nháy, thống kê số) thay vì dòng thô. Đây là thành phần đáng giá nhất của đội hạng nhất | TableReasoner, AILS-NTUA, M-Schema |
| Bảng chuẩn (L2) | Mô hình thực thể, chiều, số đo; bốn loại đại lượng | MetricFlow |
| Hiểu câu hỏi (L3) | Dự đoán kiểu đáp án riêng; ví dụ few-shot chọn theo câu hỏi đã che thực thể; viết lại câu hỏi có kết quả trái chiều | Samsung, DAIL-SQL, Anotheroption |
| Khớp giá trị (L4) | Khớp ký tự lấy ứng viên, rồi model chọn; truy hồi dòng mẫu để lộ cách viết thật | TableReasoner, CHESS, CodeS |
| Compiler (L5) | Tầng ngữ nghĩa chính xác hơn sinh SQL trên phần phủ được, và từ chối thay vì trả sai | dbt, Cube |
| Rulebook (A3) | Dựng thư viện hàm một lần, model nhỏ chỉ chọn | NVIDIA trên DABstep |
| Kiểm chứng, abstain (L7) | Nhóm ứng viên theo kết quả thực thi; sửa theo lỗi thực thi có lợi ổn định; bỏ phiếu đơn thuần gần như không tăng độ chính xác | CSC-SQL, arXiv 2606.29733, TrustSQL |
| Phục vụ | 4-bit cộng gom lô cộng ràng buộc đầu ra | KDD Cup 2024, bài đo AWS |

### 5.2 Mười hai việc nên lấy, xếp theo giá trị trên rủi ro

Phép đo 1–4 là các phép đo ở mục 1.5 của `FABLE_REVIEW.md`: (1) train-full chạy mới, (2) bất biến dưới nhiễu câu hỏi, (3) bất biến dưới nhiễu dữ liệu, (4) bỏ một ngành. Mọi con số lợi ích là [ƯỚC LƯỢNG] trừ khi ghi nguồn.

| # | Lấy gì, từ đâu, cho tầng nào | Lợi ích dự kiến | Công | Rủi ro | Đo bằng |
|---|---|---|---|---|---|
| 1 | **Gom lô**: gom lô tĩnh trong máy chủ hiện tại, rồi `llama-server` hoặc vLLM có vá (mục 3.4). Tầng phục vụ | Thông lượng 3–7 lần; mở khóa mọi việc cần nhiều lượt gọi | 0,5–2 ngày | Mất tất định từng bit; bản vá vLLM chưa gộp | Thời gian ngoại suy dưới 4,5 giờ; phép đo 1 không giảm; tỉ lệ câu đổi giữa hai lần chạy dưới 1% |
| 2 | **Model gốc bản QAT chính thức** thay bản fine-tune SQL. Tầng L1, L3 | QAT thường giữ chất lượng tốt hơn NF4 sau huấn luyện [NHỚ]; có sẵn GGUF và W4A16 | 0,5 ngày (đã có trong kế hoạch) | Model gốc có thể kém hơn ở chú thích bảng | Phép đo 1 và 2 cùng prompt; đổi nếu không giảm |
| 3 | **Hồ sơ cột làm đầu vào chú thích**, theo mẫu AILS-NTUA và TableReasoner. Tầng L1 | SemEval: 5,4 điểm khi so với bảng thô [WEB]. Với ta: giảm lỗi gán vai trò trên tiêu đề lạ | 2–3 ngày | Prompt dài hơn; cần cắt gọn cho bảng rộng | Phép đo 3 tăng; PA của chú thích mới hoàn toàn không thấp hơn bản đã sửa tay quá 3 điểm |
| 4 | **Dự đoán kiểu đáp án và kiểm tra khớp kiểu**, theo Samsung. Tầng L3, L7 | SemEval: 3,3–6,9 điểm [WEB]. Với ta: sửa họ lỗi "trả list khi cần table", "phần trăm hay số", và là một cổng abstain rẻ | 1–2 ngày | Bộ phân loại sai sẽ chặn nhầm câu đúng | Trên phép đo 1: trong số câu bị chặn, ít nhất 60% vốn sai; câu đúng bị chặn dưới 0,5% |
| 5 | **Nối giá trị hai bước**: ứng viên bằng khớp ký tự (dãy con chung dài nhất trên 0,6, n-gram) cộng nhúng, rồi model chọn hoặc trả "không có". Từ TableReasoner, CHESS. Tầng L4 | Giảm abstain "unresolved" và giảm khớp nhầm kiểu "số 4" | 2–3 ngày | Khớp nhầm tên gần giống; cần lựa chọn "không có" và biên an toàn | Phép đo 1: số abstain "unresolved" giảm, số câu sai không tăng; phép đo 2 tăng |
| 6 | **Khung khái niệm MetricFlow cho IR**: thực thể, chiều, số đo, và bốn loại đại lượng (đơn, tỉ lệ, lũy kế, dẫn xuất). Tầng L2, L5, mốc A1 | Tránh tự nghĩ lại từ đầu; "lũy kế" phủ họ "còn nợ, còn tồn đến hết ngày X", "dẫn xuất" phủ "chênh lệch, phần trăm thay đổi" | Nằm trong A1 | Không dùng thư viện MetricFlow lúc chạy (nặng, không cần); chỉ mượn mô hình | Điều kiện A1; đếm số dòng rulebook viết được bằng bốn loại |
| 7 | **Thư viện định nghĩa dựng sẵn ngoài giờ, lúc chạy chỉ khớp và điền tham số**, theo mẫu NVIDIA trên DABstep. Mốc A3 | Giảm rủi ro "model 12B viết định nghĩa sai mà vẫn chạy" | Nằm trong A3 | Luật thật sự mới vẫn cần model viết; giữ quy tắc "hai lần sinh giống nhau mới dùng" | Phép đo 4: khôi phục ít nhất 60% câu của ngành bị ẩn, sai không quá 8% |
| 8 | **Một lượt sửa theo lỗi** khi IR không biên dịch được hoặc ra rỗng: gửi lại IR cùng thông báo lỗi. Từ AILS-NTUA và arXiv 2606.29733. Tầng L3, L5 | Tài liệu: 1,2–4,4 điểm, thành phần duy nhất có lợi ổn định [WEB]. Với ta: đổi một phần abstain thành câu đúng | 1 ngày | Model "sửa" bằng cách bỏ bộ lọc; cổng "mọi số và tên trong câu hỏi phải được dùng" phải chạy sau lượt sửa | Phép đo 1: độ chính xác của các câu được cứu ít nhất 0,8 |
| 9 | **Ví dụ few-shot truy hồi theo câu hỏi đã che số và tên**, từ DAIL-SQL. Tầng L3 | AILS-NTUA: few-shot là mức tăng lớn nhất của họ (58 lên 64–79) [WEB]. Với ta: đưa 3000 câu có đáp án vào hệ thống | 3–4 ngày | Học thuộc khuôn train | Phép đo 1 tách theo shop (ví dụ chỉ lấy từ shop khác) tăng ít nhất 2 điểm; phép đo 2 tăng |
| 10 | **Nhiều góc nhìn làm tín hiệu abstain, nhóm theo kết quả thực thi**, từ CSC-SQL và Samsung. Tầng L7 | Không kỳ vọng tăng số câu đúng (tài liệu: 0,13 điểm). Kỳ vọng giảm số câu sai | 2 ngày, sau việc 1 | Gấp 2–3 lần thời gian hiểu câu hỏi; các mẫu sai giống nhau | Phép đo 1: vẽ độ chính xác theo mức đồng thuận; chỉ giữ nếu nhóm bất đồng có độ chính xác dưới 0,5 |
| 11 | **Bộ toán tử Auto-Tables làm bộ sinh nhiễu dữ liệu** và danh sách kiểm cho ingestion. Tầng L0, phép đo 3 | Phép đo 3 sát thực tế hơn; lộ các dạng bảng ingestion chưa xử lý | 1–2 ngày | Ít | Chính là phép đo 3; báo điểm theo từng toán tử |
| 12 | **LoRA trên nhãn đã kiểm bằng thực thi** (lấy mẫu rồi loại, như TableReasoner: giữ 1184 trên 1308). Hướng C | SemEval: 1,9 điểm [WEB]; với tiếng lóng có thể hơn | 1–2 tuần | Cần hỏi ban tổ chức: bản fine-tune tự làm sau 01/10/2026 có hợp lệ không | Điều kiện C2 trong `FABLE_REVIEW.md` |

Thứ tự đề nghị: 1 và 2 ngay; 3, 4, 5 song song với A1; 6 và 7 là cách làm A1 và A3; 8, 9, 10 sau khi có gom lô; 11 khi dựng phép đo 3; 12 cuối.

### 5.3 Tài liệu ủng hộ hay phản bác hướng A?

**Ủng hộ.**

- Tầng ngữ nghĩa chính xác hơn sinh SQL tự do trên phần phủ được, và hỏng theo kiểu từ chối chứ không phải trả sai (dbt [WEB], Cube [WEB-phụ]).
- Dưới thang có phạt, sinh SQL tự do không thắng ổn định "luôn abstain" (TrustSQL [WEB-phụ]).
- Model trong giới hạn 15B sinh code tự do trên bảng **sạch**, câu hỏi **tiếng Anh**, chỉ đạt 65–72% (AILS-NTUA, Arctic, CSC-SQL [WEB]). Với câu hỏi tiếng Việt lóng và luật nghiệp vụ riêng, con số sẽ thấp hơn. Ở mức 65%, trả lời hết cho PA 0,30.
- Lời giải đứng đầu DABstep (benchmark duy nhất có tài liệu luật nghiệp vụ đi kèm) dùng thư viện hàm dựng trước thay vì để model viết lại logic mỗi câu [WEB-phụ].
- Tianchi NL2SQL 2019: đạt 92% bằng cách điền khung truy vấn cố định cộng hậu xử lý khớp giá trị, không sinh SQL tự do [NHỚ về cách làm; con số là WEB-phụ].

**Phản bác hoặc cảnh báo.**

- Mọi đội đứng đầu SemEval-2025 Task 8 và Spider 2.0 đều sinh code tự do. Nhưng họ dùng model 32B trở lên hoặc model kín, và thang đo không phạt câu sai. Đây là khác biệt về điều kiện, không phải bằng chứng chống A.
- Điểm yếu thật của tầng ngữ nghĩa là **độ phủ**: 0% trên câu ngoài danh mục (dbt). Đây đúng là rủi ro số 3 trong `FABLE_REVIEW.md`. Tài liệu không cho cách nào tốt hơn ngoài "mở rộng định nghĩa" và "rơi về sinh SQL".
- Bỏ phiếu nhiều mẫu, vốn là nền của hướng B và của mốc A4, có bằng chứng yếu hơn tôi đã viết trong bản đánh giá trước: tối đa 0,13 điểm trong một nghiên cứu độc lập [WEB]. CSC-SQL chỉ có lợi khi có thêm model hợp nhất được huấn luyện riêng. Tôi điều chỉnh: bỏ phiếu chỉ để abstain.

**Có nên chuyển sang hướng B sớm hơn không?** Không làm nền chính. Nhưng có hai trường hợp nên kéo phép thử B1 lên sớm, và cả hai đều rẻ sau khi có gom lô:

1. Nếu phép đo 4 (bỏ một ngành) ở mốc A3 khôi phục dưới 60%, thì việc biên dịch rulebook lạ bằng model 12B không đủ tin, và đường SQL trên bảng chuẩn (có dòng rulebook truy hồi trong prompt) là cách duy nhất còn lại để phủ định nghĩa lạ. Khi đó B thành đường dự phòng theo đúng quy trình của dbt.
2. Nếu phép đo 1 cho thấy trên 15% số câu abstain vì "ngoài danh mục" sau mốc A2.

Đề nghị cụ thể: ngay sau khi có gom lô, chạy B1 một lần trên 150 câu có nhãn (đã có trong kế hoạch), chỉ để có con số độ chính xác của đường SQL trên bảng chuẩn. Nếu dưới 0,6 thì gác B hẳn; nếu trên 0,75 thì B đáng làm lớp dự phòng sớm hơn tuần 4.

---

## 6. Những thứ không nên chép

| Thứ | Lý do |
|---|---|
| Pipeline nhiều bộ sinh cộng model chọn kiểu CHASE-SQL, XiYan-SQL đầy đủ | Dựa vào model cỡ Gemini hoặc GPT-4 và 10–20 lần sinh mỗi câu; với 3000 câu và 6 giờ, ta có khoảng 2–3 lần sinh mỗi câu |
| Agent nhiều bước kiểu Spider-Agent, ReFoRCE, COLA, ReAcTable, Chain-of-Table | 5–20 lượt gọi mỗi câu; kết quả tốt chỉ với model lớn (Spider 2.0: các hạng đầu đều là model kín) |
| Tập hợp 8 model kiểu Samsung | Tổng tham số vượt 15B nhiều lần |
| Sinh pandas hoặc SQL tự do trên bảng thô | Model 7–8B đạt 65% trên bảng sạch; ta đã đo 0,029 trên bảng thô. Dưới thang có phạt, đây là điểm âm |
| Thêm model chuyên SQL 7B bên cạnh Gemma 12B (Arctic-R1-7B, OmniSQL-7B, TableGPT2-7B) | 19,6B, vượt giới hạn. Chỉ ghép được model SQL 3B với model chính 9B |
| Viết lại câu hỏi rồi **thay** câu gốc | Team Anotheroption: 84% xuống 68% [WEB]; phép đo nội bộ của ta cũng cho kết quả xấu |
| Bước chọn cột riêng trước khi sinh | Samsung: làm hại đường SQL [WEB]; arXiv 2606.29733: nối lược đồ không có lợi có ý nghĩa [WEB]. Lược đồ chuẩn của ta nhỏ, không cần lọc |
| Bỏ phiếu 5 mẫu để tăng độ chính xác | Bằng chứng: 0,13 điểm với gấp 5 lần token [WEB] |
| vLLM bản gốc với Gemma 4 trên T4 | Lỗi ở yêu cầu đầu tiên; cần bản vá chưa gộp [WEB] |
| SGLang, TGI, ExLlamaV2 trên T4 | Không hỗ trợ Turing hoặc đã ngừng phát triển (mục 3.1) |
| Chép mã từ repo không ghi giấy phép (MAC-SQL, TableLLM, ba repo SemEval, ReAcTable) | Không có quyền sử dụng; chỉ đọc lấy ý |
| Dùng MetricFlow làm thư viện lúc chạy | Nặng, gắn với dbt; ta chỉ cần mô hình khái niệm |
| Fine-tune bằng học tăng cường kiểu Arctic-R1, CSC-SQL | Cần hạ tầng huấn luyện lớn; lợi ích đo trên SQL tiếng Anh; chưa rõ luật về bản fine-tune tự làm |
| Dịch câu hỏi sang tiếng Anh để dùng model SQL tiếng Anh | Chưa có bằng chứng (MultiSpider 2.0 không thử); model hiện tại dịch tiếng lóng sai nặng (đã đo nội bộ) |

---

## 7. Giới hạn của bản nghiên cứu này

- Tôi không đọc được PDF bài tổng quan SemEval-2025 Task 8, nên thứ hạng chính thức chưa được xác nhận. TeleAI và AILS-NTUA đều tự báo "hạng nhất" (nhiều khả năng ở hai nhánh khác nhau: model mở và model kín); một bài khác ghi điểm cao nhất là 95,02, không khớp với 93,87 của TeleAI.
- Bảng xếp hạng DABstep tải động, tôi không đọc được số. Cách làm của NVIDIA chỉ thấy qua bản tóm tắt.
- Bảng xếp hạng BIRD chính thức chưa được mở trực tiếp; số của hệ thống kín là từ nguồn thứ cấp.
- Mọi con số thông lượng trên T4 cho llama.cpp là chưa có; con số của vLLM đến từ một bài viết cá nhân duy nhất, không rõ độ dài prompt. Phải đo trên Kaggle trước khi quyết định.
- Chi tiết của CHASE-SQL, CHESS, XiYan-SQL, Chain-of-Table, Binder, DATER, ViText2SQL, cách làm của đội thắng Tianchi 2019, và bốn loại đại lượng của MetricFlow là từ trí nhớ.
- Tôi không tra lại thẻ của bản fine-tune SQL đang dùng (để không gửi tên gắn với dự án ra ngoài); thông tin về nó vẫn như trong `FABLE_REVIEW.md`.
- Không tìm được số liệu tiếng Việt lóng cho bất kỳ model nào trong mục 4. Lựa chọn model phải dựa vào phép đo 1 và 2 của ta.
- Ghi chú vận hành: công cụ tải web đã tự lưu một bản PDF bài tổng quan SemEval vào thư mục kết quả công cụ của phiên làm việc (ngoài thư mục dự án). Tôi không tạo file nào khác ngoài báo cáo này.

## 8. Nguồn chính

- SemEval-2025 Task 8: https://aclanthology.org/2025.semeval-1.324/ ; TeleAI https://arxiv.org/html/2507.08046v1 ; AILS-NTUA https://arxiv.org/html/2503.00435v2 ; Samsung https://research.samsung.com/blog/SemEval-2025-Task-8-LLM-Ensemble-Methods-for-QA-Over-Tabular-Data ; Anotheroption https://arxiv.org/html/2506.09657v2
- BIRD, model nhỏ: https://www.snowflake.com/en/blog/engineering/arctic-text2sql-r1-sql-generation-benchmark/ ; https://arxiv.org/abs/2505.13271 ; https://arxiv.org/abs/2606.29733
- Spider 2.0: https://spider2-sql.github.io/ ; MultiSpider 2.0: https://arxiv.org/html/2509.24405v1
- DABstep: https://adyen-dabstep.hf.space/ ; https://arxiv.org/html/2506.23719v1 ; InfiAgent-DABench: https://arxiv.org/abs/2401.05507
- TableBench: https://tablebench.github.io ; TrustSQL: https://arxiv.org/abs/2403.15879
- Tầng ngữ nghĩa: https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026 ; https://dbt-labs.github.io/dbt-llm-sl-bench
- Bảng tính: https://arxiv.org/abs/2307.14565 (Auto-Tables) ; https://arxiv.org/html/2407.09025v2 (SpreadsheetLLM)
- Phục vụ trên T4: https://github.com/vllm-project/vllm/issues/38918 ; https://github.com/vllm-project/vllm/pull/39018 ; https://discuss.vllm.ai/t/gemma3-on-a-t4-gpu/643 ; https://builder.aws.com/content/3K3ZnLrNaDhh6xnnUAsP2LbJZVQ/gemma-4-on-amazon-sagemaker-the-nvidia-t4-decodes-at-08x-of-the-l4-with-the-same-answers ; https://github.com/ggml-org/llama.cpp/pull/21309
- KDD Cup 2024: https://arxiv.org/abs/2408.04658
- Tiếng Việt: https://arxiv.org/abs/2010.01891 (ViText2SQL) ; https://aclanthology.org/2025.vlsp-1.30 (VLSP 2025, bài AMR)
- Tianchi NL2SQL 2019: https://www.qbitai.com/2019/10/7994.html
