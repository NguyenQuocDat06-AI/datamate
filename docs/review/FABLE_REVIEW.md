# Đánh giá kiến trúc và chiến lược: DataMate V2 semantic pipeline

Ngày viết: 2026-11-11. Phạm vi: chỉ đọc, không sửa code, không chạy chấm điểm, không mở tập nghiệm thu.

Cách đánh dấu trong báo cáo:

- **[ĐÃ KIỂM]**: tôi đã tự xác nhận trong code hoặc dữ liệu (kèm `file:dòng` hoặc tên file kết quả).
- **[NHẬN ĐỊNH]**: suy luận của tôi, chưa có phép đo trực tiếp.
- **[TỪ TRÍ NHỚ]**: thông tin về model, bài báo tôi nhớ nhưng hôm nay chưa tra lại được; cần kiểm trước khi dùng.

Đường dẫn code viết tắt: `semantic/…` là `/home/ubuntu/agy-worktrees/arch8/source_test/bizreason/semantic/…`.

---

## 0. Tóm tắt một trang

Kết luận chính: hệ thống hiện tại trả lời rất chính xác những gì nó đã được lập trình sẵn (độ chính xác trên câu có trả lời là 0,92 ở public và 0,98 ở train), nhưng trần điểm trên shop lạ bị chặn bởi bốn điểm cấu trúc, không phải bởi các lỗi lẻ còn lại.

1. **Rulebook bị biên dịch cứng vào Python, trong khi mỗi bộ test đi kèm rulebook riêng.** Rulebook public có thêm 3 ngành, 8 định nghĩa xếp hạng và nhiều luật không có trong rulebook train. Bộ private gần như chắc chắn cũng có rulebook riêng. Pipeline V2 không đọc rulebook lúc chạy. [ĐÃ KIỂM]
2. **Tập câu hỏi biểu diễn được là một danh sách đóng.** 37 đại lượng, trong đó 16 chưa có cách tính; 8 loại bảng được model gán nhãn nhưng không được nạp vào bảng chuẩn; không có đáp án dạng bảng, không có xếp hạng "thấp nhất". [ĐÃ KIỂM]
3. **Model đang dùng là bản fine-tune sinh SQL bằng tiếng Anh, nhưng pipeline không còn sinh SQL.** Nó đang làm việc phân loại câu hỏi tiếng Việt, việc mà nó không được huấn luyện. Câu không dấu có PA thấp hơn câu có dấu 13 điểm trên train. [ĐÃ KIỂM]
4. **Chưa có lần chạy đầu-cuối nào của V2, và chưa có phép đo nào thật sự "chưa từng thấy".** Pipeline V2 chưa được nối vào `cli.py`; mọi con số đều dùng intent lưu sẵn và chú thích bảng làm sẵn. 185 trên 300 câu train dùng để chấm có cùng 4 từ đầu với câu đã được xem lúc phát triển. [ĐÃ KIỂM]

Điểm theo shop trên public cho thấy rõ vấn đề: các shop thuộc ngành mới hoặc có dạng dữ liệu mới (`dev_011` 0,25; `dev_007` 0,33; `dev_002`, `dev_013`, `dev_014` 0,50) kéo điểm xuống, trong khi các shop giống train đạt khoảng 0,9. [ĐÃ KIỂM từ `e2e8/e9_records.jsonl`]

Khuyến nghị: dừng kiểu sửa từng cụm lỗi. Cam kết vào **hướng A: tầng ngữ nghĩa điều khiển bằng rulebook** (định nghĩa đại lượng là dữ liệu, sinh ra từ rulebook của chính bộ test, trên một ngôn ngữ truy vấn trung gian có tính tổ hợp), và làm trước tiên **bộ đo tính tổng quát** để mọi tiến bộ sau này không thể đến từ việc khớp 14 shop public. Lộ trình 1 tuần và 1 tháng ở mục 1.6.

Tôi không cam kết con số 0,8 trên shop lạ. Đó là mục tiêu hợp lý của hướng A nếu các mốc go/no-go đạt, không phải dự báo.

---

## 1. Kế hoạch đột phá

### 1.1 Vì sao thiết kế hiện tại chạm trần

Vòng lặp đêm qua tăng public từ 0,618 lên 0,706 bằng 8 bản sửa, mỗi bản gỡ 1 đến 10 câu. Đó là cách làm đúng cho một hệ thống đã gần hoàn chỉnh, nhưng không đổi được trần, vì ba lý do:

- Mỗi đại lượng mới, mỗi loại bảng mới, mỗi luật ngành mới là một nhánh Python mới (`semantic/compiler.py:846-858` là từ điển 8 biểu thức; `semantic/layer.py:1026-1068` là chuỗi `elif` theo loại bảng). Chi phí tăng tuyến tính theo số luật, và luật của bộ private thì chưa biết.
- Kiến thức nghiệp vụ nằm ở ba nơi cứng: `semantic/vocabulary.py` (khung giờ ca 06:00–13:59 ở dòng 107, từ khóa huỷ, tên khách lẻ, "phi phuc vu"), prompt của `semantic/intent.py:35-113`, và SQL trong compiler. Nếu rulebook private đổi khung giờ ca hay thêm "khách sộp nhất", không có đường nào để hệ thống biết.
- Bộ đo hiện tại thưởng cho việc khớp 44 shop đã xem. Tiêu chí "giữ" của vòng lặp là điểm không giảm trên chính các tập đó.

### 1.2 Hướng A (khuyến nghị): tầng ngữ nghĩa điều khiển bằng rulebook

**Ý tưởng cốt lõi.** Tách "đại lượng là gì" khỏi code. Có ba phần:

1. **Bảng sự kiện tổng quát thay cho chuỗi `elif`.** Mọi bảng được nạp thành một trong vài dạng chung: sự kiện có ngày (đơn, dòng hàng, phiếu nhập, phiếu thu, phiếu sửa, đợt giao, bảo hành, chi phí, biến động kho), danh mục (hàng, khách, nhân viên, lô), và bảng quy đổi. Mỗi cột được gán kiểu chung (khóa thực thể, ngày, tiền, số lượng, thuộc tính phân loại, ghi chú). Thuộc tính phân loại nào cũng dùng làm bộ lọc và chiều nhóm được, không cần khai báo trước trong `DIMENSIONS`.
2. **Ngôn ngữ truy vấn trung gian (IR) có tính tổ hợp thay cho danh sách `measure`.** Một truy vấn gồm: nguồn sự kiện, phép gộp (tổng, đếm, đếm khác nhau, trung bình, nhỏ nhất, lớn nhất), cột, bộ lọc, chiều nhóm (thực thể hoặc hạt thời gian như ngày, giờ, thứ), điều kiện trên nhóm, hướng sắp xếp, giới hạn, và phép hậu xử lý (tỉ lệ, hiệu, phần trăm thay đổi, tỉ trọng, số dư đến một ngày). Compiler từ IR sang SQL vẫn tất định như hiện nay, nên giữ được ưu điểm "model không viết SQL tự do".
3. **Rulebook là dữ liệu.** Mỗi dòng rulebook ("Khách sộp nhất = khách có giá trị trung bình một đơn cao nhất, chỉ xét khách có từ 3 đơn trở lên") là một định nghĩa IR có tên. Đầu mỗi lần chạy, model đọc rulebook của chính bộ test và, cho từng dòng: (a) khớp với định nghĩa đã có trong thư viện và điền tham số (khung giờ ca, trạng thái nào được tính), hoặc (b) viết định nghĩa IR mới, được kiểm bằng ngữ pháp và chạy thử trên dữ liệu shop. Khi hiểu câu hỏi, model chọn trong danh mục định nghĩa này, kèm các dòng rulebook liên quan được truy hồi vào prompt.

**Nó thay thế gì.** `MEASURES`/`SHAPES`/`DIMENSIONS` trong `vocabulary.py`, từ điển biểu thức và các nhánh theo đại lượng trong `compiler.py`, chuỗi `elif` theo loại bảng trong `layer.py`. Giữ nguyên: tầng đọc file L0, cách model chỉ gom giá trị do code liệt kê, bộ giải thời gian, các cổng abstain, test chặn hardcode.

**Vì sao tin là có tác dụng.**

- Bằng chứng nội bộ [ĐÃ KIỂM]: so với rulebook train, rulebook public thêm các ngành "Mẹ và bé", "Sửa xe", "Cửa hàng điện thoại", các định nghĩa "khách trung thành nhất", "khách sộp nhất", "khách hay đổi ý nhất", "mặt hàng được ưa chuộng nhất", "nhân viên chăm chỉ nhất", "ngày đắt khách nhất", "giờ cao điểm", "nhà cung cấp chính", luật "báo cáo tiền mặt theo ca", "topping ghi chung ô với món", "sổ không có cột trạng thái". Hầu hết các định nghĩa này chỉ là tổ hợp của phép gộp, nhóm, điều kiện và sắp xếp, tức là viết được bằng IR mà không cần code mới.
- Tài liệu [TỪ TRÍ NHỚ]: biểu diễn trung gian thay cho SQL thô (SemQL của IRNet 2019, NatSQL 2021); tầng ngữ nghĩa kiểu MetricFlow của dbt, nơi câu hỏi được ánh xạ vào đại lượng đã định nghĩa thay vì sinh SQL; giải mã ràng buộc theo ngữ pháp (PICARD 2021). Điểm chung: thu hẹp không gian đầu ra về một ngôn ngữ nhỏ, có kiểm tra, làm tăng mạnh độ chính xác của model nhỏ.

**Các mốc, mỗi mốc có điều kiện đi tiếp đo được.**

| Mốc | Nội dung | Điều kiện đi tiếp |
|---|---|---|
| A0 | Bộ đo tính tổng quát (mục 1.5) và lần chạy đầu-cuối có bấm giờ | Có số nền cho cả 5 phép đo; lần chạy 44 shop có ước lượng thời gian cho quy mô private |
| A1 | IR phiên bản 1, viết lại toàn bộ đại lượng đang có thành định nghĩa dữ liệu, không đổi hành vi | Điểm train và public lệch không quá 1 câu so với hiện tại; không còn nhánh `if m == …` theo đại lượng |
| A2 | Bộ nạp bảng sự kiện tổng quát cho 8 loại bảng đang bị bỏ | Các họ câu hỏi tồn kho, hạn dùng, bảo hành, chi phí, trả góp được trả lời chỉ bằng định nghĩa IR; trên các câu đang abstain vì thiếu đại lượng, trả lời được ít nhất 70% với độ chính xác ít nhất 0,9; số dòng Python thêm cho mỗi họ bằng 0 |
| A3 | Bộ biên dịch rulebook | Phép thử bỏ-một-ngành: xóa khỏi thư viện mọi định nghĩa của một ngành, để hệ thống tự dựng lại từ văn bản rulebook; khôi phục ít nhất 60% số câu đúng của ngành đó, tỉ lệ sai không quá 8% |
| A4 | Nâng bộ hiểu câu hỏi cho IR: giải mã ràng buộc, ví dụ truy hồi, bỏ phiếu nhiều mẫu, sau đó LoRA | Chênh lệch PA giữa câu không dấu và có dấu dưới 5 điểm; độ bất biến dưới nhiễu ít nhất 90% |
| A5 | Đo tập nghiệm thu đúng một lần | Người dùng quyết định |

**Ngân sách.** Biên dịch rulebook: khoảng 60–100 dòng luật, mỗi dòng một lần gọi ngắn, dưới 15 phút, làm một lần cho cả bộ test. Phần còn lại như hiện nay (chú thích bảng theo shop, hiểu câu hỏi theo câu). Không thêm tham số. [NHẬN ĐỊNH]

**Rủi ro chính.** (1) Định nghĩa do model viết sai mà vẫn chạy được, tạo ra cả họ câu sai. Giảm bằng: chỉ dùng định nghĩa mới khi hai lần sinh độc lập cho cùng IR, và chạy thử phải cho kết quả không rỗng; nếu không thì họ câu đó abstain. (2) A1 là viết lại lớn; nếu không giữ được điểm, dừng ở A1 và chỉ làm A2 cho các loại bảng thiếu. (3) Một số luật không phải tổ hợp đơn giản (giá vốn bình quân theo lô, hiệu ứng tăng giá, Tết âm lịch); các luật này giữ làm hàm dựng sẵn có tên trong IR.

### 1.3 Hướng B: sinh chương trình có ràng buộc trên bảng chuẩn, chọn theo đồng thuận

**Ý tưởng.** Giữ L0–L2, nhưng cho model viết SQL trên một lược đồ chuẩn sạch và nhỏ (các view đã áp sẵn luật mặc định: đơn hợp lệ, doanh thu theo dòng, số lượng quy đổi), kèm các dòng rulebook truy hồi được. Sinh N ứng viên, chạy, nhóm theo kết quả. Bộ compiler typed-intent hiện tại là một ứng viên nữa. Chỉ trả lời khi các đường độc lập đồng thuận, nếu không thì abstain.

**Vì sao thiết kế hiện tại chạm trần và hướng này gỡ được.** SQL biểu diễn được mọi họ câu hỏi, kể cả câu chưa ai nghĩ tới. Model đang dùng lại chính là model được huấn luyện để viết SQL. Pipeline SQL tự do cũ đạt 0,029 vì phải viết SQL trên bảng thô lộn xộn; trên bảng chuẩn thì bài toán gần với các benchmark mà model đã học.

**Bằng chứng.** [TỪ TRÍ NHỚ] CHASE-SQL (2024, arXiv 2410.01943) và CHESS (2024, arXiv 2405.16755) đạt kết quả hàng đầu trên BIRD nhờ sinh nhiều ứng viên rồi chọn; chọn theo kết quả thực thi (MBR-exec, arXiv 2204.11454); self-consistency (arXiv 2203.11171).

**Mốc.** B1: 20 view chuẩn và prompt, đo trên 150 câu có nhãn; đi tiếp nếu độ chính xác của riêng đường SQL ít nhất 0,6. B2: đồng thuận hai đường; đi tiếp nếu trên train-full, câu được cả hai đường đồng thuận có độ chính xác ít nhất 0,97 và tổng PA tăng ít nhất 3 điểm ở bộ đo tổng quát. B3: mở rộng cho họ câu compiler không biểu diễn được, chỉ trả lời khi 3 trên 3 mẫu SQL đồng thuận.

**Ngân sách.** Tốn nhất trong ba hướng: thêm 3–5 lần sinh cho mỗi câu, mỗi lần 100–200 token. Chỉ khả thi sau khi có phục vụ theo lô (mục 2d).

**Rủi ro chính.** Các mẫu SQL sai giống nhau vì cùng một model và cùng một hiểu lầm, nên đồng thuận không bảo đảm đúng. Lịch sử dự án có file vá SQL 150 KB (`sql_healers_bf565b3.py`), là dấu hiệu hướng này dễ trượt về kiểu vá từng trường hợp. Tôi coi B là lớp kiểm chứng bổ sung cho A, không phải nền chính.

### 1.4 Hướng C: máy sinh dữ liệu và huấn luyện bộ hiểu câu hỏi

**Ý tưởng.** Xây một máy sinh shop giả (ngành, bố cục sheet, tên cột đồng nghĩa, lỗi gõ, tiêu đề nhiều tầng, dòng tổng phụ) và câu hỏi giả từ rulebook, với đáp án biết trước do tự dựng. Dùng nó cho hai việc: (1) bộ đo không thể khớp tay; (2) dữ liệu huấn luyện LoRA cho bộ hiểu câu hỏi, cộng với nhãn yếu từ 3000 câu train: intent nào biên dịch ra đúng đáp án chuẩn thì coi là nhãn đúng (trên mẫu 300 câu, 281 câu, tức khoảng 94%, có nhãn theo cách này).

**Vì sao.** Câu hỏi của ban tổ chức rõ ràng được sinh theo khuôn: 185 trên 300 câu train lấy mẫu có cùng 4 từ đầu (sau khi bỏ dấu và che số) với một câu trong danh sách 900 câu đã xem [ĐÃ KIỂM]. Một bộ hiểu câu hỏi được huấn luyện trên phân phối khuôn này cộng nhiễu tiếng lóng sẽ vững hơn nhiều so với một prompt cố định. Lỗi tiếng lóng hiện là nguồn câu sai lớn nhất ở train: 3 trên 5 câu sai là câu không dấu, trong đó hai câu rất ngắn ("so bill hnay la bnhiu", "so don tu dau nam toi het hnay la bn") bị hiểu thành doanh thu.

**Mốc.** C1: máy sinh câu hỏi nhiễu (bỏ dấu, teencode, đảo cụm) từ câu train có nhãn; đo độ bất biến. C2: LoRA trên model gốc, tách theo shop; đi tiếp nếu trên các shop để riêng, độ chính xác intent tăng ít nhất 5 điểm và trên public không giảm. C3: máy sinh shop giả.

**Ngân sách.** Huấn luyện ngoài giờ thi, không tính vào 6 giờ. LoRA gộp vào trọng số, không thêm tham số.

**Rủi ro chính.** Học thuộc khuôn của train, trong khi public đã cho thấy private có họ câu mới. Vì vậy C phải đi sau A (đầu ra của LoRA là IR, không phải danh sách đại lượng cũ), và phải tách theo shop lẫn theo họ câu khi đánh giá.

### 1.5 Bộ đo thưởng cho tính tổng quát (làm trước mọi thứ)

Năm phép đo, báo cáo cùng nhau. Một thay đổi chỉ được giữ khi phép 1–4 không giảm; public chỉ để theo dõi.

| # | Phép đo | Cách làm | Vì sao không khớp tay được |
|---|---|---|---|
| 1 | Train-full, chạy mới hoàn toàn | Toàn bộ câu train trừ các id nghiệm thu (script tự loại, không ai mở file), alias tắt, đọc file bằng code worktree, hiểu câu hỏi bằng prompt hiện tại (không cache). Báo PA kèm khoảng tin cậy bootstrap theo shop | Khoảng 2500 câu thay cho 300; sai số chuẩn giảm khoảng 3 lần |
| 2 | Bất biến dưới nhiễu câu hỏi | Mỗi câu thêm 3 biến thể: bỏ dấu, chèn viết tắt, đổi thứ tự cụm thời gian. Đo tỉ lệ đáp án giữ nguyên | Không cần đáp án chuẩn; làm được cả trên shop chưa có nhãn |
| 3 | Bất biến dưới nhiễu dữ liệu | Đổi tên cột bằng từ đồng nghĩa do model sinh, đảo thứ tự cột, tách tháng thành sheet, thêm dòng tiêu đề và tổng phụ. Đáp án chuẩn không đổi | Mô phỏng trực tiếp "shop lạ, tiêu đề lạ" |
| 4 | Bỏ-một-ngành | Ẩn định nghĩa và ví dụ của một ngành, đo trên các shop ngành đó, lần lượt từng ngành | Đo đúng khả năng xử lý rulebook lạ |
| 5 | Public 102 câu | Như hiện nay | Chỉ theo dõi, không dùng để quyết định |

Quy tắc đi kèm: mỗi thay đổi phải nêu trước họ câu hỏi hoặc dạng dữ liệu mà nó nhắm tới và số câu dự kiến chuyển trạng thái trên phép đo 1. Thay đổi chỉ gỡ được dưới 5 câu trên 2500 thì không làm. Tập nghiệm thu chỉ đo ở các mốc A2, A4 và trước khi nộp.

### 1.6 Chọn một hướng và lộ trình

**Cam kết hướng A**, với A0 (bộ đo) làm ngay. Lý do: A là hướng duy nhất xử lý rủi ro số 1 (rulebook lạ) và số 2 (họ câu ngoài danh sách); B và C chỉ cải thiện những gì đã biểu diễn được. B dùng làm lớp kiểm chứng ở tuần 3–4, C dùng cho bộ hiểu câu hỏi ở tuần 3–4.

**Tuần 1.**

- Ngày 1: nối V2 thành một lệnh chạy đầu-cuối (đọc file, chú thích, dựng bảng, hiểu câu hỏi, trả lời, ghi `submission.jsonl`), bấm giờ từng bước trên 44 shop. Dựng phép đo 1.
- Ngày 2: so model gốc `gemma-4-12B-it` với bản fine-tune SQL trên việc hiểu câu hỏi và chú thích bảng (cùng prompt). Bật giải mã ràng buộc JSON. Dựng phép đo 2.
- Ngày 3: dựng phép đo 3 và 4. Chốt số nền. Thiết kế IR trên giấy: viết tay định nghĩa cho mọi dòng của cả hai rulebook, đếm bao nhiêu dòng viết được bằng tổ hợp, bao nhiêu cần hàm dựng sẵn. Nếu dưới 70% dòng viết được thì xem lại IR trước khi code.
- Ngày 4–6: A1, chuyển các đại lượng hiện có sang định nghĩa dữ liệu, giữ nguyên điểm.
- Ngày 7: bộ nạp tổng quát cho hai loại bảng đầu tiên (tồn kho và bảo hành), đo theo điều kiện A2.

**Tháng 1.**

- Tuần 2: hoàn tất A2 cho 8 loại bảng; chính sách "không trả lời được" tổng quát (mục 2c).
- Tuần 3: A3, bộ biên dịch rulebook và phép thử bỏ-một-ngành; phục vụ theo lô trên T4×2 để có ngân sách cho nhiều mẫu.
- Tuần 4: A4 (ví dụ truy hồi, bỏ phiếu 3 mẫu, LoRA nếu C2 đạt), lớp đồng thuận B cho họ câu khó; chạy thử trọn 6 giờ trên Kaggle hai lần liên tiếp cho cùng kết quả; đo nghiệm thu một lần.

---

## 2. Đánh giá hệ thống

### Điểm mạnh [ĐÃ KIỂM trừ khi ghi khác]

- Độ chính xác trên câu có trả lời cao: 79/86 = 0,919 ở public, 281/286 = 0,983 ở train.
- Model không viết SQL; mọi con số do code tất định tính. Lỗi model chỉ còn ở việc gán nhãn.
- Nhiều cổng "thà abstain còn hơn sai": thiếu trạng thái đơn (`compiler.py:342-348`), bộ lọc không khớp dữ liệu (`compiler.py:420-421`), ngưỡng bị bỏ rơi (`compiler.py:1027-1029`), bộ lọc không có trong câu hỏi (`solver.py:119-129`).
- Đã bỏ được phụ thuộc vào danh sách alias tiêu đề trên 14 shop public (alias tắt 0,461 lên 0,706).
- Kỷ luật không hardcode trong `semantic/`, có test chặn.

### Rủi ro cấu trúc cho bộ private, xếp theo điểm mất dự kiến

Ước lượng mức mất là nhận định của tôi, tính trên thang PA của một bộ test có tỉ lệ ngành mới giống public.

| Hạng | Rủi ro | Bằng chứng | Mất dự kiến |
|---|---|---|---|
| 1 | Rulebook và ngành lạ không được đọc lúc chạy | `grep` "rulebook" trong `semantic/`: chỉ có chú thích. Chỉ pipeline cũ đọc (`cli.py:49`). Các shop ngành mới ở public đạt 0,25–0,50 | 0,10–0,20 |
| 2 | Chú thích bảng và dựng bảng trên bố cục lạ | Nghiệm thu 0,49 khi public 0,618 (cùng thời điểm); mọi sửa ingestion từ đó đều dựa trên 14 shop đã xem; 8 loại bảng không được nạp (`layer.py:1026-1068`) | 0,08–0,15 |
| 3 | Họ câu hỏi ngoài danh sách đóng | 16 trên 37 đại lượng không có cách tính; không có dạng bảng; chi tiết ở 2b | 0,05–0,10 |
| 4 | Hiểu sai tiếng lóng thành câu trả lời sai (mỗi câu mất 2 điểm so với đúng) | Train: không dấu 0,818, có dấu 0,949. `vi_probe.jsonl`: "dthu hqua bn z" được dịch thành "How was your day yesterday?" | 0,03–0,08 |
| 5 | Vượt 6 giờ hoặc lỗi vận hành | Chưa có lần chạy đầu-cuối; chi tiết ở 2d | Nhỏ về kỳ vọng, nhưng nếu xảy ra thì mất toàn bộ |
| 6 | "Không trả lời được" bị bỏ thành abstain | 0 trên 3 ở public, 3 trên 5 ở train | 0,02–0,04 |

### (a) Khoảng cách 0,92 / 0,70 so với 0,49

Những gì đã kiểm:

- Con số 0,49 đo ở commit `6c7059e` (đợt 6). Cùng thời điểm, public có alias là 0,618 và tắt alias là 0,461. Tức là nghiệm thu gần bằng mức tắt alias. Phần lớn khoảng cách lúc đó là do phụ thuộc alias tiêu đề.
- Từ đó public tắt alias đã lên 0,706. Nhưng mức tăng này có được bằng cách xem lỗi của chính 14 shop đó, nên không chứng minh được nghiệm thu cũng tăng tương ứng.
- Train-300 không phải tập "để riêng" thật: lấy từ cùng 30 shop đã được chú thích và sửa lỗi; script chỉ loại câu trùng nguyên văn (`eval_e2e8.py:44-46`), còn 37 câu trùng hoàn toàn sau khi che số và 185 câu trùng 4 từ đầu với danh sách đã xem.
- Chú thích bảng dùng cho chấm điểm là bản đã được sửa qua 4 đợt (`spike_a1b`, `ann_rest`, `ann_fix`, `ann_fix2`, nạp ở `eval_e2e8.py:31-35`), không phải kết quả của một lần chạy mới.

Nhận định: 0,92 là điểm "đã thấy khuôn câu, đã thấy shop"; 0,706 là "đã thấy shop, khuôn câu mới một phần"; 0,49 là ước lượng gần nhất với private nhưng đã cũ. Con số thật hiện nay nhiều khả năng nằm giữa 0,5 và 0,65. Cách đo trung thực là bộ đo ở mục 1.5 và một lần đo nghiệm thu tại mốc.

### (b) Typed intent + compiler có phải trần đúng không

Thiết kế đúng hướng (model gán nhãn, code tính), nhưng danh sách phẳng `measure × shape` là trần sai. Những họ câu không biểu diễn được hoặc biểu diễn sai, đã kiểm trong code:

| Họ câu hỏi | Tình trạng | Chỗ xác nhận |
|---|---|---|
| Tồn kho, giá trị tồn, hết hàng, lô và hạn dùng, hiệu ứng tăng giá, lãi ròng, chi phí, tuổi nợ theo nhóm, số ngày hoạt động, giảm giá đơn, bảo hành, phiếu sửa, thành viên mới | Có tên trong `MEASURES` nhưng `Compiler.value` ném `Unsupported("measure …")` | `compiler.py:846-860`; `vocabulary.py:5-42` |
| Đáp án dạng bảng (top 5 khách nợ kèm số nợ) | Compiler không bao giờ trả kiểu `table`; trả `list` tên, bị chấm sai | `compiler.py:1110-1112`; câu `q_8659862171ab` ở train |
| "Thấp nhất", "ít nhất", "ế nhất" | `SHAPES` ghi "highest or lowest" nhưng intent không có trường hướng; SQL luôn `ORDER BY s DESC` | `vocabulary.py:51`; `compiler.py:920` |
| Tỉ trọng trong một tập con (nhóm X trong doanh thu kênh Y) | Mẫu số bỏ mọi bộ lọc | `compiler.py:1083` |
| "Hơn bao nhiêu" mà model không ghi `compare_as` | Mặc định là phần trăm, có thể ra câu sai | `intent.py:16`, `intent.py:313-315` |
| Nhóm theo giờ, thứ, tháng (giờ cao điểm, ngày đắt khách) | `hour`, `weekday` có trong `DIMENSIONS` nhưng không nhóm được; chỉ có `argmax_date` | `compiler.py:40-48`, `892-897` |
| Chia theo mọi thành viên của một chiều (doanh thu từng kênh) | `breakdown` đòi nêu tên ít nhất 2 thành viên | `compiler.py:1124-1131` |
| Liệt kê theo điều kiện, câu hai bước (trong số khách mua tháng 3, ai không mua tháng 4), điều kiện trên dòng hàng của đơn | `list_filter` không có nhánh; ngưỡng chỉ có 4 chủ thể và 3 chỉ số | `compiler.py:52-60`, `1134` |
| Lọc theo thuộc tính bất kỳ của danh mục (số sữa, size bỉm, dòng máy, biển số, địa chỉ) | Chỉ các cột khai báo sẵn trong `LINE_DIMS`/`ORDER_DIMS` | `compiler.py:40-48` |
| Bộ lọc nhiều giá trị cùng chiều với nghĩa "hoặc" | Hai bộ lọc cùng chiều bị nối bằng AND, hoặc bị hiểu là so sánh | `compiler.py:524-535`, `934-960` |

Ngoài ra, cửa sổ thời gian do bộ giải bằng biểu thức chính quy quyết định (`solver.py:184-188`); trường `time` mà prompt yêu cầu model trả (`intent.py:42`) không được dùng. Cách nói thời gian lạ sẽ thành "no time window" (`solver.py:199-200`).

### (c) Chính sách abstain so với trả lời

Với thang +1 / −1 / 0, nên trả lời khi xác suất đúng trên 0,5. Hệ thống hiện trả lời ở mức chính xác 0,92–0,98, tức là các cổng đang chặt hơn mức tối ưu khá xa. Nhưng tôi không khuyên nới cổng một cách chung chung: 16 câu abstain ở public hầu hết là "không tính được", không phải "tính được nhưng không dám". Nới cổng chỉ có lợi khi có tín hiệu tin cậy theo từng câu. Tín hiệu tổng quát nên xây là **mức đồng thuận giữa nhiều lần hiểu câu hỏi** (và sau này giữa đường compiler và đường SQL): đồng thuận thì trả lời, không thì abstain. Cần hiệu chỉnh trên phép đo 1: vẽ độ chính xác theo mức đồng thuận, chọn ngưỡng ở điểm độ chính xác bằng 0,6 để có biên an toàn.

Về "không trả lời được":

- Hiện chỉ phát ra ở ba trường hợp hẹp: có `location` mà shop không có cột địa chỉ, hỏi nhóm khách mà không có cột nhóm khách, kỳ nằm ngoài khoảng dữ liệu (`solver.py:170-220`).
- Ba câu public về tên đường thất bại vì model đặt tên đường vào bộ lọc `customer` chứ không vào `location`, nên rơi vào "unresolved customer" và abstain. Ngược lại, ở train có một câu model bịa `location = "binh quan bn tien"` cho câu hỏi giá nhập bình quân, và hệ thống trả "không trả lời được" sai (`q_4f4919db1974`). Tức là đường này hiện có 0 câu đúng và 1 câu sai.
- Việc nhận biết cột địa chỉ dựa vào 4 từ khóa tiêu đề cứng (`vocabulary.py:133`, `layer.py:1125-1127`), sẽ hỏng với tiêu đề lạ.
- `measure == "outside"` luôn abstain (`solver.py:166-167`).

Đề xuất tổng quát, không phải vá ba câu: mỗi truy vấn IR khai báo các loại thông tin nó cần (loại bảng, vai trò cột, thuộc tính). Hệ thống trả "không trả lời được" khi đồng thời (1) không bảng nào của shop có loại thông tin đó theo chú thích, (2) README không nhắc tới nó, và (3) một lần hỏi model riêng "shop này có ghi X không" trả lời là không. Thiếu một trong ba thì abstain. Điều kiện (2) và (3) cần thiết vì hai câu "no payments" ở public có đáp án thật: dữ liệu có, nhưng tầng dựng bảng không nạp loại `debt_ledger`. Nhầm "mình nạp hỏng" thành "shop không ghi" sẽ mất 2 điểm mỗi câu.

### (d) Ngân sách 6 giờ, 15 tỷ tham số, T4×2

Số đã đo [ĐÃ KIỂM]:

- Chú thích bảng: 156 bảng thành công, tổng 6253 giây thời gian model, trung bình 40 giây mỗi bảng (`annotate_all.log`), cho 44 shop. Chưa tính các lần gọi phân loại trạng thái, loại dòng hàng, gom giá trị.
- Hiểu câu hỏi: trung bình 5,3 giây mỗi lần gọi (`vi_probe.jsonl`, 28 lần). Prompt hệ thống dài khoảng 80 dòng code (`intent.py:35-113`) và được gửi lại ở mỗi câu.
- Train có 3000 câu cho 30 shop, tức 100 câu mỗi shop.
- Đã từng có một truy vấn kiểm tra chất lượng mất 255 giây mỗi câu (đã sửa, LOOP_LOG vòng 1). Lỗi kiểu này chỉ lộ khi chạy đầu-cuối.

Nhận định: nếu private có 30 shop và 3000 câu, chạy tuần tự sẽ cần khoảng 1,2 giờ chú thích cộng 4,4 giờ hiểu câu hỏi, sát hoặc vượt 6 giờ, chưa có chỗ cho thử lại hay bỏ phiếu nhiều mẫu. Các số trên đo qua đường hầm tới máy chủ, có 4 luồng song song, nên thời gian thực trên Kaggle cần đo lại. Việc phải làm:

- Phục vụ theo lô: hai tiến trình, mỗi GPU một bản 4-bit của model 12B (khoảng 7–8 GB mỗi bản), có gom lô liên tục. Ứng viên: llama.cpp với GGUF Q4_K_M, hoặc vLLM với AWQ.
- Lưu sẵn tiền tố prompt hệ thống để không tính lại ở mỗi câu.
- Giải mã ràng buộc để đầu ra ngắn và không phải thử lại.
- Đặt ngân sách cứng theo bước và đồng hồ tổng: khi còn dưới 20 phút, mọi câu chưa xong được ghi abstain và file kết quả được ghi ra. Ghi kết quả tăng dần sau mỗi shop.

Về tham số: 12B cho model chính, cộng bge-m3 (khoảng 0,57B) và model thêm dấu (khoảng 0,4B) là khoảng 13B. Nếu đổi sang Qwen3-14B (14,8B) thì không còn chỗ cho model phụ nào.

### (e) Vệ sinh đánh giá

| Vấn đề | Bằng chứng | Hậu quả |
|---|---|---|
| Intent lưu sẵn từ lần chạy model trước | `eval_e2e8.py:79-107`; cache được chép từ `e2e5/` (GOAL_LOOP). `intent.py` không đổi từ commit `6c7059e` (10/11 13:48), nên prompt nhiều khả năng khớp, nhưng danh sách giá trị lưu sẵn đưa vào prompt đã đổi sau khi gom giá trị và dựng lại bảng | Cả 8 vòng đêm qua không đo lại bước hiểu câu hỏi lần nào; biến động giữa các lần gọi model chưa từng được đo. Ba câu tên đường có `location` rỗng trong cache |
| Bảng dựng từ cache ingestion của nhánh main | `eval_e2e8.py:55-57` | Thay đổi ở `bizreason/data/` chỉ đo được ở chế độ tắt alias |
| Chú thích bảng làm sẵn, đã sửa tay nhiều đợt | `eval_e2e8.py:31-35` | Không đo được lỗi chú thích của một lần chạy mới |
| Tập nhỏ | 102 câu: 1 câu bằng 0,01 PA; sai số chuẩn khoảng 0,06 | Các bước "giữ" ở vòng 6, 7, 8 dựa trên 1–2 câu, nằm trong nhiễu |
| Tính tất định | Đã có một lỗi phụ thuộc `PYTHONHASHSEED` (LOOP_LOG vòng 6). Model chạy `temperature=0` (`llm.py:564`) nhưng 4 luồng song song và lượng tử hóa 4-bit có thể vẫn cho kết quả khác nhau giữa các lần chạy | Cần chạy đầu-cuối hai lần và so từng câu |
| Chưa có đường chạy thật | `SemanticSolver` chỉ được gọi trong test và script chấm; không có trong `cli.py` hay `pipeline.py` | Chưa biết hành vi khi model lỗi, hết thời gian, shop hỏng |
| Ngưỡng chất lượng hiệu chỉnh trên 14 shop | `quality.py:13-18` | Có thể chặn nhầm cả shop ở private (đã xảy ra với `biz_123`, LOOP_LOG vòng 1) |

---

## 3. Lỗ hổng theo tầng

**Đọc file (L0).**

- Từ vựng nghiệp vụ vẫn nằm ngoài `vocabulary.py`: `data/identities.py:13` (`RETAIL_ALIASES`), `data/parsing.py:208` (`STATUS_ALIASES`). Test chặn hardcode không phủ các file này. [ĐÃ KIỂM]
- `data/ingestion.py:182-183, 578`: vẫn còn điểm số tách vùng dựa trên alias ("kept for region splits where known names are present"). Cần kiểm ở phép đo 3 xem tắt đi có đổi kết quả không. [ĐÃ KIỂM là có trong code; tác động là NHẬN ĐỊNH]
- Tôi không đọc toàn bộ `ingestion.py` (967 dòng). Các nhận xét khác về tầng này phải chờ phép đo 3.

**Chú thích bảng (L1).**

- Danh sách đóng 20 loại bảng và vai trò cột theo loại (`annotator.py:27-48` và các từ điển sau đó). Bảng ngành lạ rơi vào `other` và bị bỏ. [ĐÃ KIỂM]
- Lỗi JSON làm mất cả bảng: `annotate_all.log` dòng 137, bảng sản phẩm của `biz_127` trả "ModelError: Model returned invalid JSON" sau 5,5 giây. Chỉ thử lại một lần (`annotator.py:348-354`). [ĐÃ KIỂM]
- Không có điểm tin cậy cho nhãn; một cột gán sai vai trò tiền sẽ làm sai mọi câu của shop mà không cổng nào bắt. [NHẬN ĐỊNH]
- Gom giá trị hầu như không tạo ra gì ở public: 12 trên 14 shop có kết quả rỗng (`prep_wave8.log`). Chi phí thời gian có, lợi ích chưa thấy. [ĐÃ KIỂM]

**Bảng chuẩn (L2).**

- Các loại `inventory`, `lot`, `expense`, `price_history`, `shift_report`, `debt_ledger`, `warranty`, `promotion` được gán nhãn nhưng không có nhánh nạp (`layer.py:1026-1068`). `dev_014` có 2 bảng `debt_ledger` và `payments: 0`, dẫn tới hai câu abstain "no payments". [ĐÃ KIỂM]
- Phiếu sửa xe bị ép vào khuôn đơn hàng (`layer.py:1031-1046`): phiếu là đơn, việc làm là sản phẩm. Luật "phiếu bảo hành không tính là lượt làm việc đó" hay "số chiếc xe = số biển số" không có chỗ biểu diễn. [ĐÃ KIỂM code; hệ quả là NHẬN ĐỊNH]
- Cờ `has_address` dựa trên từ khóa tiêu đề (`layer.py:1115-1127`). [ĐÃ KIỂM]

**Hiểu câu hỏi (L3).**

- Model fine-tune SQL tiếng Anh; ví dụ trong prompt cũng bằng tiếng Anh (`intent.py:93-104`) trong khi câu hỏi là tiếng Việt lóng. [ĐÃ KIỂM]
- Không ràng buộc đầu ra; kiểm tra sau rồi thử lại tối đa 2 lần (`intent.py:348`, `363-381`). Một lần gọi, không có ước lượng tin cậy.
- Nhóm khách, kênh, phương thức thanh toán, nhóm hàng được đưa vào prompt, tối đa 40 giá trị (`solver.py:36-42`). Các chiều khác (sản phẩm, khách, nhân viên, hãng) model phải chép từ câu hỏi. Shop có trên 40 nhóm hàng sẽ bị cắt theo thứ tự chữ cái.
- Lỗi đã thấy: "so bill hnay la bnhiu" và "so don tu dau nam toi het hnay la bn" thành doanh thu (2 câu sai ở train); "huy bnhiu don" thành mọi đơn; bịa bộ lọc nhóm khách (3 câu abstain). [ĐÃ KIỂM từ records]

**Khớp thực thể (L4).**

- Khớp bằng chuỗi chuẩn hóa: bằng nhau, bằng nhau sau khi bỏ từ chung, rồi chứa trong và duy nhất (`compiler.py:393-413`). Không xử lý lỗi gõ, viết tắt, thiếu từ ("Kệ điện thoại để bàn" không khớp; "sữa bột số 4" khớp nhầm cả nhóm). [ĐÃ KIỂM]
- Danh sách từ chung cứng bằng tiếng Việt (`vocabulary.py:128-131`).
- Cổng "bộ lọc có trong câu hỏi" chỉ cần trùng một từ (`solver.py:126-128`), nên không bắt được trường hợp mất một phần giá trị như "số 4".

**Compiler (L5).** Xem bảng ở 2b. Thêm: thứ tự khi hòa là theo khóa tăng dần (`compiler.py:920`), đúng với rulebook mục 9; câu `dev_002` đáp án chuẩn xếp khác, tôi không xác định được vì sao và không khuyên sửa theo một câu.

**Cổng (L7).**

- Cờ chất lượng tính một lần cho cả shop (`solver.py:144-148`) và chặn mọi câu thuộc nhóm đại lượng liên quan. Ngưỡng 0,5% ô không đọc được (`quality.py:15-16`) rất chặt; một cột ghi chú bị gán nhầm là số lượng sẽ làm cả shop abstain. [ĐÃ KIỂM code; rủi ro là NHẬN ĐỊNH]
- `except Exception` ở `solver.py:154` và `232` biến mọi lỗi lập trình thành abstain im lặng. An toàn cho điểm, nhưng che lỗi; cần đếm và báo số abstain theo tên ngoại lệ trong bộ đo.

---

## 4. Kỹ thuật và model nên thêm

Mỗi mục được chấm theo tính tổng quát: nó cải thiện cả họ câu hoặc dạng dữ liệu lạ, hay chỉ vài câu.

### 4.1 Kỹ thuật

| Kỹ thuật | Giải quyết gì ở đây | Lợi ích dự kiến | Chi phí | Rủi ro | Tổng quát? |
|---|---|---|---|---|---|
| Giải mã ràng buộc theo JSON schema hoặc ngữ pháp | Hết lỗi JSON hỏng, hết giá trị ngoài danh sách, hết thử lại; áp dụng cho cả chú thích bảng | Bỏ được các bảng bị mất vì JSON; giảm 10–30% thời gian | Không thêm tham số; cần máy chủ hỗ trợ (llama.cpp GBNF, vLLM guided decoding, Outlines, XGrammar) | Ràng buộc quá chặt làm model chọn bừa thay vì báo lỗi; luôn để một lựa chọn "không rõ" | Có |
| Nhiều mẫu hiểu câu hỏi và bỏ phiếu (3–5 mẫu, hoặc 1 mẫu trên câu gốc cộng 1 trên câu đã thêm dấu cộng 1 trên bản viết lại) | Tín hiệu tin cậy theo câu; chặn lỗi tiếng lóng kiểu "so bill" thành doanh thu | Giảm khoảng một nửa số câu sai do hiểu nhầm, đổi lại thêm vài abstain [NHẬN ĐỊNH] | Gấp 3 thời gian hiểu câu hỏi; cần phục vụ theo lô | Các mẫu sai giống nhau | Có |
| Truy hồi ví dụ tương tự (few-shot động) từ câu train có intent đã kiểm bằng đáp án | Thay 3 ví dụ tiếng Anh cố định bằng 5–8 ví dụ tiếng Việt gần nhất, có cả câu lóng | Lớn với câu theo khuôn; đây là cách rẻ nhất để đưa 3000 câu có đáp án vào hệ thống | Model nhúng khoảng 0,57B; vài mili giây mỗi câu | Học thuộc khuôn train; phải đo bằng tách theo shop và theo họ câu | Có, với điều kiện đo đúng |
| Viết lại câu hỏi về tiếng Việt chuẩn trước khi phân tích | Tiếng lóng, không dấu | Chưa rõ. Thử nghiệm dịch cho thấy model hiện tại viết lại sai nặng. Chỉ thử lại sau khi đổi model, và luôn đưa cả câu gốc | 1 lần gọi thêm mỗi câu | Viết lại làm mất mã, số, tên | Có, nếu qua được phép đo 2 |
| Thêm dấu bằng model nhỏ cộng bảng viết tắt ngành bán hàng | Như trên, rẻ hơn | Đã thử: chỉ model MIT an toàn với số và mã, nhưng đổi "ca chiều" thành "cả chiều". Dùng làm một "góc nhìn" thứ hai cho bỏ phiếu, không thay câu gốc | 0,4B tham số | Đổi nghĩa | Có |
| Lượt kiểm tra sau khi trả lời | So đáp án với câu hỏi: kiểu đáp án có khớp từ hỏi không (bao nhiêu tiền thì là tiền, mấy % thì là phần trăm, "ai" thì là tên), mọi con số và tên riêng trong câu hỏi có được dùng trong intent không | Bắt được cả họ lỗi "bỏ rơi một phần câu hỏi" (mất "số 4", mất "huỷ") mà không cần model | Không đáng kể nếu làm bằng code; 1 lần gọi nếu dùng model | Bản dùng model có thể bác bỏ câu đúng | Có |
| Khớp thực thể bằng nhúng cộng khớp mờ ký tự, có biên an toàn | Tên sản phẩm, khách, nhân viên gõ sai, thiếu từ, viết tắt | Giảm abstain "unresolved …" (4 trên 16 câu abstain ở public, không tính 3 câu tên đường) | Model nhúng dùng chung với truy hồi ví dụ | Khớp nhầm tên gần giống; chỉ nhận khi ứng viên đầu cách ứng viên thứ hai một khoảng rõ, nếu không thì abstain | Có |
| Chú thích cột có bằng chứng: cho model xem thống kê giá trị (kiểu, độ phủ, số giá trị khác nhau, quan hệ khóa giữa các bảng) và kiểm chéo bằng phép tính (thành tiền ≈ số lượng × đơn giá) | Tiêu đề lạ, cột gán nhầm vai trò | Lớn cho rủi ro hạng 2; cho điểm tin cậy của từng vai trò | Vài truy vấn SQL mỗi bảng | Ít | Có |
| LoRA cho bộ hiểu câu hỏi trên nhãn đã kiểm bằng thực thi, cộng nhiễu tiếng lóng | Tiếng lóng, độ ổn định, prompt ngắn hơn (nhanh hơn) | Có thể lớn; xem hướng C | Vài giờ GPU ngoài giờ thi; không thêm tham số | Học thuộc; cần kiểm ngày phát hành có bị tính theo bản fine-tune hay không (hỏi ban tổ chức) | Có, nếu đầu ra là IR |
| Model chuyên hiểu bảng (ví dụ TableGPT2 7B) | Chú thích bảng | Không khuyên: 7B cộng 12B vượt 15B; thay model chính bằng nó thì mất năng lực tiếng Việt | 7B | Cao | Không phù hợp ngân sách |

### 4.2 Model

| Model | Vai trò | Tham số | Giấy phép | Phát hành | Ghi chú | Liên kết |
|---|---|---|---|---|---|---|
| `Shaleen123/gemma4-12b-sql` (đang dùng) | Mọi việc | khoảng 12B | Thẻ model ghi "TODO", theo giấy phép model gốc | Thẻ không ghi ngày; trích dẫn ghi 2026 | Fine-tune từ `google/gemma-4-12B-it` cho việc sinh SQL từ câu hỏi **tiếng Anh**; dữ liệu huấn luyện chưa công bố. Cần xác nhận ngày phát hành trước 01/10/2026 vì thẻ không ghi [ĐÃ KIỂM hôm nay] | https://huggingface.co/Shaleen123/gemma4-12b-sql |
| `google/gemma-4-12B-it` | Thay thế trực tiếp cho hiểu câu hỏi và chú thích | khoảng 12B | Apache 2.0 | Họ Gemma 4 công bố 02/04/2026; bản 12B được một nguồn phụ ghi 23/05/2026 (chưa có nguồn chính thức xác nhận) | Cùng kiến trúc, cùng cách phục vụ; so trực tiếp chỉ mất nửa ngày | https://ai.google.dev/gemma/docs/core/model_card_4 và https://www.llm-stats.com/models/gemma-4-12b-it |
| `Qwen/Qwen3-14B` | Ứng viên thay thế | 14,8B | Apache 2.0 | 28–29/04/2025 | Huấn luyện trên 119 ngôn ngữ; nguồn tra hôm nay không nêu đích danh tiếng Việt. Dùng nó thì hết ngân sách cho model phụ | https://qwenlm.github.io/blog/qwen3/ và https://arxiv.org/abs/2505.09388 |
| `sail/Sailor2-8B-Chat` | Ứng viên chuyên Đông Nam Á, có tiếng Việt | 8B | Apache 2.0 | Báo cáo kỹ thuật 02/2025; model 12/2024 [TỪ TRÍ NHỚ cho tháng phát hành] | Nhỏ hơn nên nhanh hơn, còn dư 7B; cần đo năng lực JSON và suy luận | https://huggingface.co/sail/Sailor2-8B-Chat và https://arxiv.org/abs/2502.12982 |
| `BAAI/bge-m3` | Nhúng cho truy hồi ví dụ, khớp thực thể, truy hồi dòng rulebook | khoảng 0,57B [TỪ TRÍ NHỚ] | MIT [TỪ TRÍ NHỚ] | 01/2024 [TỪ TRÍ NHỚ] | Có cả điểm từ vựng lẫn điểm ngữ nghĩa, hợp với tên sản phẩm | https://huggingface.co/BAAI/bge-m3 |
| `AITeamVN/Vietnamese_Embedding` | Như trên, đã tinh chỉnh cho tiếng Việt | Bằng bge-m3 | Apache 2.0 | Chưa tra được ngày | Tinh chỉnh trên khoảng 300 nghìn bộ ba; Accuracy@1 trên Legal Zalo 2021 là 0,727 so với 0,568 của bge-m3. Miền pháp lý khác miền tên hàng, cần đo | https://huggingface.co/AITeamVN/Vietnamese_Embedding |
| `Qwen/Qwen3-Embedding-0.6B` | Như trên | 0,6B | Apache 2.0 | 06/2025 | Hỗ trợ trên 100 ngôn ngữ | https://qwenlm.github.io/blog/qwen3-embedding và https://arxiv.org/pdf/2506.05176 |
| `vietnamese-diacritic-restoration-v2` (yammdd) | Góc nhìn thứ hai của câu hỏi | khoảng 0,4B | MIT | Xem `norm_test/` | Kết quả thử có sẵn trong LOOP_LOG | (đã có trong `norm_test/models`) |

Các model tiếng Việt khác (SeaLLMs-v3-7B, Vistral-7B-Chat): hôm nay tôi không tra được giấy phép; theo trí nhớ, SeaLLMs dùng giấy phép riêng và Vistral nền Mistral 7B đã cũ. Không ưu tiên.

Tài liệu tham khảo [TỪ TRÍ NHỚ, cần kiểm lại số arXiv trước khi trích]: self-consistency arXiv 2203.11171; PICARD arXiv 2109.05093; NatSQL arXiv 2109.05153; MBR-exec arXiv 2204.11454; DAIL-SQL (chọn ví dụ few-shot) arXiv 2308.15363; CHESS arXiv 2405.16755; CHASE-SQL arXiv 2410.01943; XGrammar arXiv 2411.15100; TrustSQL (text-to-SQL có abstain) arXiv 2403.15879; MetricFlow https://github.com/dbt-labs/metricflow; Outlines https://github.com/dottxt-ai/outlines; VMLU (đã tra hôm nay) https://aclanthology.org/2025.acl-long.563.

---

## 5. Kế hoạch các hướng nên thử, có xếp hạng

Mọi thí nghiệm đo trên phép đo 1–4 của mục 1.5. "Train-full" là phép đo 1.

### Việc nhanh (mỗi việc không quá 1 ngày), đều mang tính nền tảng, không phải vá

| # | Hướng | Giả thuyết | Thí nghiệm và ngưỡng thành công | Nếu thất bại |
|---|---|---|---|---|
| 1 | Lệnh chạy đầu-cuối có bấm giờ và đồng hồ tổng | Thời gian thật trên T4×2 vượt ngân sách nếu chạy tuần tự | Chạy 44 shop và 500 câu lấy mẫu trên Kaggle; ngoại suy cho 30 shop và 3000 câu phải dưới 4,5 giờ; hai lần chạy cho kết quả giống nhau từng câu | Giảm prompt, bỏ bước gom giá trị (đang gần như vô ích), tăng gom lô |
| 2 | Bộ đo tổng quát (phép đo 1–4) | Điểm thật thấp hơn 0,92 đáng kể khi không cache và tách đúng | Có 4 số nền kèm khoảng tin cậy; phép đo 1 khác train-300 hơn 3 điểm sẽ xác nhận giả thuyết | Không có nhánh thất bại; đây là điều kiện cho mọi việc sau |
| 3 | So `gemma-4-12B-it` gốc với bản fine-tune SQL | Model gốc hiểu tiếng Việt lóng tốt hơn | Cùng prompt, 150 câu có nhãn (`spike_a2_labels.jsonl`) cộng phép đo 2; đổi nếu độ chính xác intent tăng ít nhất 3 điểm và độ bất biến tăng. Chạy lại cả chú thích bảng trên 44 shop, số bảng gán đúng loại không giảm | Giữ model cũ; thử Sailor2-8B và Qwen3-14B với cùng quy trình |
| 4 | Giải mã ràng buộc cho intent và chú thích | Hết lỗi JSON, nhanh hơn | Số lần `ModelError` và số lần thử lại về 0 trên 44 shop; phép đo 1 không giảm | Giữ kiểm tra sau như hiện nay |
| 5 | Lượt kiểm tra bằng code sau khi trả lời (kiểu đáp án khớp từ hỏi; mọi số và tên riêng trong câu hỏi phải được intent dùng) | Bắt được họ lỗi "bỏ rơi một phần câu hỏi" | Trên train-full: trong các câu bị lượt này chặn, ít nhất 60% vốn là câu sai; số câu đúng bị chặn dưới 0,5% | Thu hẹp về phần kiểu đáp án |
| 6 | Sửa ba chỗ compiler trả lời sai có hệ thống: hướng xếp hạng (thấp nhất), đáp án dạng bảng khi hỏi kèm số đo, mẫu số của tỉ trọng | Đây là lỗi của cả họ câu, mỗi câu mất 2 điểm | Đếm trên train-full số câu có từ "ít nhất/thấp nhất" với dạng xếp hạng, câu "mỗi … bao nhiêu" với top k; sau sửa, các nhóm này đạt độ chính xác ít nhất 0,9 | Chuyển các nhóm đó sang abstain |

### Đặt cược lớn

| # | Hướng | Giả thuyết | Thí nghiệm và ngưỡng thành công | Công sức | Nếu thất bại |
|---|---|---|---|---|---|
| 7 | Hướng A, mốc A1–A2: IR tổ hợp và bộ nạp bảng tổng quát | Phần lớn đại lượng còn thiếu là tổ hợp đơn giản | Điều kiện A1 và A2 ở mục 1.2 | 2 tuần | Giữ compiler cũ, chỉ thêm bộ nạp tổng quát và 3–4 hàm dựng sẵn cho tồn kho và lô |
| 8 | Hướng A, mốc A3: biên dịch rulebook lúc chạy | Model đọc được dòng luật và viết định nghĩa IR đúng | Phép đo 4: khôi phục ít nhất 60% câu của ngành bị ẩn, sai không quá 8% | 1 tuần | Hạ xuống mức thấp hơn: rulebook chỉ dùng để điền tham số (khung giờ, trạng thái) và truy hồi dòng luật vào prompt |
| 9 | Chú thích cột có bằng chứng và điểm tin cậy | Lỗi chú thích là nguồn mất điểm lớn trên shop lạ | Phép đo 3: độ bất biến dưới nhiễu dữ liệu tăng ít nhất 10 điểm; chạy chú thích mới hoàn toàn trên 44 shop cho PA không thấp hơn chú thích đã sửa tay quá 3 điểm | 1 tuần | Dùng điểm tin cậy chỉ để abstain theo vai trò thay vì theo cả shop |
| 10 | Truy hồi ví dụ cộng bỏ phiếu nhiều mẫu | Giảm một nửa số câu sai do hiểu nhầm | Tách theo shop (ví dụ chỉ lấy từ shop khác): phép đo 1 tăng ít nhất 2 điểm, số câu sai giảm ít nhất 30%; phép đo 2 tăng | 4 ngày, sau việc 1 | Chỉ giữ bỏ phiếu, bỏ truy hồi |
| 11 | "Không trả lời được" theo yêu cầu thông tin (mục 2c) | Thu được 2–4% số câu thành +1 | Trên train-full cộng public: độ chính xác của đáp án "không trả lời được" ít nhất 0,8 và tìm lại ít nhất một nửa số câu chuẩn là không trả lời được | 3 ngày, sau A2 | Giữ ba trường hợp hẹp hiện có, sửa lỗi bịa `location` bằng lượt kiểm tra ở việc 5 |
| 12 | Hướng C: LoRA trên nhãn đã kiểm bằng thực thi; sau đó lớp đồng thuận SQL của hướng B | Bộ hiểu câu hỏi vững với tiếng lóng; kiểm chứng độc lập | Điều kiện C2 và B2 | 1–2 tuần | Dừng; ví dụ truy hồi đã cho phần lớn lợi ích |

### Chống khớp quá mức vào 14 shop public

- Public chỉ để theo dõi; không mở từng câu public để tìm lỗi nữa. Lỗi được tìm trên train-full và trên dữ liệu nhiễu.
- Mỗi thay đổi phải nêu trước họ câu và số câu dự kiến; không nhận thay đổi dưới 5 câu trên 2500.
- Khi dùng câu train làm ví dụ hoặc dữ liệu huấn luyện, luôn tách theo shop, và báo thêm kết quả tách theo họ câu.
- Giữ nguyên quy tắc không hardcode và mở rộng test chặn sang `bizreason/data/`.
- Tập nghiệm thu đo tối đa ba lần trong cả tháng.

---

## 6. Những việc không nên làm

| Việc | Lý do |
|---|---|
| Tiếp tục vòng lặp "chọn một cụm lỗi public, sửa, giữ nếu không giảm" | Mỗi vòng gỡ 1–10 câu trên tập đã xem; các bước giữ gần đây nằm trong nhiễu của tập 102 câu; không chạm vào bốn điểm trần |
| Thêm từng đại lượng còn thiếu bằng một nhánh Python riêng (tồn kho, rồi bảo hành, rồi trả góp…) | Đây chính là vá theo trường hợp ở quy mô lớn hơn; luật của private chưa biết nên không bao giờ đủ |
| Sửa thứ tự khi hòa, phép so ca của `dev_003`, "đã chở đi" của `dev_004`, đơn vị "lon" của `dev_008` như các việc riêng | Mỗi việc một câu, cơ sở đáp án chưa rõ; sửa theo một đáp án dễ làm sai các câu khác |
| Quay lại sinh SQL tự do trên bảng thô, hoặc thêm bộ vá SQL | Đã đo: 0,029. File vá 150 KB là minh chứng cho chi phí |
| Thay câu hỏi bằng bản đã chuẩn hóa (teencode, bartpho) | Đã đo: làm hỏng mã thời gian và đổi nghĩa. Chỉ dùng làm góc nhìn bổ sung |
| Nới ngưỡng `quality.py` hoặc nới cổng abstain để tăng điểm public | Không có tín hiệu tin cậy theo câu thì nới cổng là đổi abstain lấy câu sai |
| Đổi sang model lớn hơn mà chưa có bộ đo | Không biết được lợi hay hại; Qwen3-14B còn lấy hết ngân sách tham số |
| Thêm model hiểu bảng 7B hoặc hệ nhiều agent | Vượt 15B hoặc vượt 6 giờ |
| Đo tập nghiệm thu sau mỗi thay đổi | Biến nó thành tập phát triển thứ hai, mất phép đo trung thực duy nhất |
| Tin vào con số từ intent lưu sẵn sau khi đã sửa prompt | Điểm báo cáo không còn là điểm của hệ thống sẽ nộp |

---

## 7. Giới hạn của bản đánh giá này

- Tôi không chạy pipeline, không gọi model, không mở tập nghiệm thu. Mọi con số điểm là từ các file kết quả có sẵn.
- Tôi đọc toàn bộ `intent.py`, `vocabulary.py`, `solver.py`, `quality.py`, `compiler.py`; đọc một phần `layer.py` và `annotator.py`; không đọc `ingestion.py`, `temporal.py` ngoài vài dòng. Nhận xét về tầng đọc file và bộ giải thời gian vì thế còn mỏng.
- Các ước lượng "mất dự kiến" và "lợi ích dự kiến" là nhận định, chưa có phép đo. Bộ đo ở mục 1.5 là thứ biến chúng thành số.
- Số shop và số câu của private tôi không biết; ước lượng thời gian dùng tỉ lệ của train (100 câu mỗi shop).
- Thông tin về ngày phát hành bản Gemma 4 12B và của bản fine-tune đang dùng chưa có nguồn chính thức; cần xác nhận với thẻ model gốc và với ban tổ chức trước hạn 01/10/2026 của luật thi.
