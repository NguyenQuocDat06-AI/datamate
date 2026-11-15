# Nghiên cứu tổng quát hoá — 15/11/2026

Bối cảnh: lượt full public m1 cho dev PA 0,907 nhưng held 0,649 (sai 19→23). Train replay solver không hồi quy
(2895→2896 đúng, 16 sai giữ nguyên). Các bản sửa đến giờ chủ yếu "vá họ lỗi đã thấy trên dev"; held cho thấy chúng không
tổng quát. Tài liệu này liệt kê các **lớp trường hợp** có thể gặp ở shop/ngành chưa thấy, đối chiếu mã nguồn mở / bài báo
đã có, và đề xuất cách dùng. Không lặp lại các nguồn đã có trong `reports/PA_09_SEMANTIC_RESEARCH_20261114.md`
(MetricFlow, CHESS, OmniSQL, NatSQL, test-suite eval, XGrammar/PICARD, Instructor, Pint, CheckList, QLoRA, conformal risk control).

## 1. Phân loại trường hợp (taxonomy)

| Lớp | Trường hợp có thể gặp | Đã thấy ở đâu | Trạng thái |
|---|---|---|---|
| A. Bố trí file | header lệch, nhiều bảng một sheet, tách theo tháng, dòng tổng phụ, ô gộp, bảng ngang (wide) cần unpivot, bảng phụ nối bằng mã, sổ có số dư chạy | DRS, dev_004/006 | một phần (ingestion, ledger, side-table) |
| B. Đơn vị/số lượng | quy đổi nhiều cấp, quy đổi bằng lời trong README/rulebook, tên hàng chứa quy cách, đơn vị đồng nghĩa (cái/chiếc) | dev_005, probe | đã sửa phần chính (W1, W6) |
| C. Thực thể/khoá | mã vs tên (nhân viên, khách), tên gọi vs họ tên, bảng phụ nối theo mã, lô ↔ đơn | dev_006, dev_012 | từng trường hợp riêng (W4, W1) |
| D. Ngôn ngữ câu hỏi | không dấu, teencode ("bnhiu", "zô", "kó"), phương ngữ ("rứa", "ni"), đồng nghĩa ("tới"/"đến", "lạc"/"đậu phộng") | toàn bộ | luật + few-shot; chưa có lớp chuẩn hoá chung |
| E. Thời gian | khoảng/so sánh/liệt kê, tương đối, lịch âm, cửa sổ tăng giá | dev_001/006 | luật cấu trúc + time reader (chưa được kích hoạt ở m1) |
| F. Định nghĩa nghiệp vụ | luật rulebook đổi giữa các bộ, luật ngành mới, quy ước README | public §6 | rulebook params/terms; nhiều luật ngành chưa có |
| G. Quyết định trả lời | câu mơ hồ, đại lượng không biểu diễn được, dữ liệu thiếu, *trả lời sai tự tin* | held sai 23 | chặn theo từng họ lỗi; **chưa có cổng hiệu chỉnh chung** |

Nhận xét chính: lỗi held không quan sát được từng câu, nên cách duy nhất hợp lệ để cải thiện là (1) tạo tập đánh giá
*giống held* từ dữ liệu được phép (train + biến đổi), và (2) cơ chế tổng quát giảm "sai tự tin" (lớp G), vì một câu sai
−1 tương đương mất 2 câu so với abstain.

## 2. Nguồn mở và bài báo, theo lớp

### Lớp G — abstain có hiệu chỉnh (ưu tiên 1)
- **TrustSQL** (Lee et al., 2024, arXiv:2403.15879; code/data github.com/glee4810/TrustSQL, CC BY-SA 4.0): chấm điểm phạt
  giống hệt quy tắc +1/−1/0 của cuộc thi. Kết quả quan trọng: không phương pháp nào vượt baseline "abstain tất cả" dưới phạt
  nặng → cổng quyết định phải được *hiệu chỉnh trên dữ liệu*, không đặt tay.
- **Reliable Text-to-SQL with Adaptive Abstention** (arXiv:2501.10858): conformal abstention ở bước schema linking.
- **Confidence Estimation for Text-to-SQL** (arXiv:2508.14056); **Richardson 2026** (arXiv:2607.06799): tín hiệu kiểm chứng
  (verification) tốt hơn log-prob cho abstention.
- Áp dụng: với điểm PA, trả lời chỉ khi P(đúng | đặc trưng đường giải) > 0,5. Đặc trưng có sẵn trong trace: nhánh solver,
  measure/shape, nguồn cửa sổ thời gian (luật/model), kiểu khớp giá trị (chính xác/chứa/biến thể), có fallback, cảnh báo
  annotation, độ phủ khoá nối. Ước lượng precision từng nhóm trên **train 3000 + dev 701** (được phép), chia theo shop
  (cluster), chọn ngưỡng bằng conformal risk control; nhóm nào precision < 0,5 thì abstain. Không dùng held để chọn ngưỡng.

### Lớp A/C — hiểu bố trí bảng và quan hệ bảng
- **Auto-Tables** (Li, He, Yan, Wang, Chaudhuri — VLDB 2023 best paper, arXiv:2307.14565): phân loại các phép biến đổi cần
  để "quan hệ hoá" bảng (transpose, stack/unpivot, wide-to-long, pivot, ffill, explode, bỏ subtitle…) + benchmark 244 ca thật.
  Dùng làm **danh mục kiểm tra** cho ingestion và làm **toán tử sinh dữ liệu biến đổi** (metamorphic) — mở rộng DRS hiện có.
- **Auto-BI** (Lin, He, Chaudhuri — VLDB 2023, arXiv:2306.12515): ứng viên khoá nối bằng inclusion dependency (độ phủ giá trị)
  rồi tối ưu toàn cục đồ thị schema. Các bản sửa W1 (lô ↔ đơn), W2 (`_order_link`), W4 (mã ↔ tên) đều là trường hợp riêng
  của việc này → hợp nhất thành **một bộ dựng đồ thị khoá** (IND + ràng buộc cardinality), thay vì logic rải rác.
- **TableSense** (AAAI 2019; code gốc không phát hành) / **TableSense2** (Apache-2.0, cộng đồng, cần huấn luyện);
  **SpreadsheetLLM** (arXiv:2407.09025; chưa có code chính thức của SheetCompressor). Không đề xuất thêm phụ thuộc mạng nơ-ron;
  ý tưởng "structural anchors" (hàng/cột không đồng nhất ở biên bảng) có thể dùng làm đặc trưng xác định header/khối.
- **Sherlock / Sato / DoDuo** (github mitmedialab/sherlock-project, megagonlabs/sato, megagonlabs/doduo): gán kiểu cột ngữ
  nghĩa từ đặc trưng giá trị. Mô hình huấn luyện tiếng Anh, không dùng trực tiếp; dùng **ý tưởng**: mỗi vai trò cột (ngày, mã,
  số lượng, tiền, trạng thái, lô) có "chữ ký giá trị" kiểm được → **bộ kiểm annotation theo giá trị** trước khi dựng layer
  (annotation sai ở dev_006 là loại lỗi này).

### Lớp D — chuẩn hoá ngôn ngữ tiếng Việt
- **ViLexNorm** (EACL 2024, arXiv:2401.16403; github ngxtnhi/ViLexNorm; HF SEACrowd/vilexnorm; 10.467 cặp câu Facebook/TikTok;
  **CC BY-NC-SA 4.0, chỉ cho nghiên cứu**): chuẩn hoá teencode. Dùng làm **tập đo độ phủ** của lớp chuẩn hoá hiện có (bao
  nhiêu dạng viết tắt trong câu hỏi đã được xử lý); không trích từ điển vào code nộp bài nếu chưa rõ điều khoản cuộc thi.

### Lớp E — thời gian
- **HeidelTime** (github HeidelTime/heideltime; có tài nguyên tiếng Việt viết tay; GPL-3.0, Java): dùng mẫu biểu thức thời
  gian tiếng Việt của nó làm **ca kiểm thử** cho luật cấu trúc + time reader; không nhúng code (giấy phép, runtime Java).
- **Duckling** (facebook/duckling, Haskell; có module VI; bản Rust/pyduckling): tương tự — tham khảo/kiểm thử, không thêm
  runtime. **dateparser** (hỗ trợ `vi`) chỉ đọc ngày đơn, không đọc khoảng/so sánh.
- Time reader (W7) đã có; m1 không có câu nào dùng nó → cần trace "được gọi / bị loại vì sao" để biết nguyên nhân.

### Lớp B — đơn vị
- Pint (đã dùng làm tham chiếu); **quantulum3** (trích số + đơn vị từ văn bản, tiếng Anh); Microsoft **Recognizers-Text**
  không có tiếng Việt. → giữ bộ đọc riêng (W6), bổ sung ca kiểm thử từ danh mục quy cách đóng gói phổ biến.

### Đánh giá độ bền (dùng cho mọi lớp)
- **RobuT** (ACL 2023, arXiv:2306.14321; github yilunzhao/RobuT): 10 loại nhiễu ở 4 mức (header, nội dung, câu hỏi, kết hợp).
  Dùng taxonomy này để sinh **tập "giả held"** từ train: đổi tên header, đảo cột, thêm dòng tổng phụ, tách sheet theo tháng
  (đã có trong DRS), viết lại câu hỏi không dấu/teencode/đồng nghĩa (CheckList INV), đổi luật rulebook (DIR).
- **AmbiQT** (EMNLP 2023, arXiv:2310.13659; github testzer0/AmbiQT): câu có hai cách hiểu hợp lệ → với điểm PA nên abstain;
  dùng để thiết kế ca kiểm thử mơ hồ (cột đồng nghĩa, có/không join).

## 3. Kế hoạch đề xuất (theo thứ tự lợi/chi phí)

1. **Cổng abstain hiệu chỉnh theo đường giải** (lớp G). Dữ liệu: trace train 3000 (replay, được phép) + dev 701 m1.
   Đặc trưng có cấu trúc, không theo từ của dataset. Chia theo shop (leave-shop-out) để ước lượng precision từng nhóm;
   ngưỡng bằng conformal risk control; báo cáo khoảng tin cậy. Chỉ sau đó mới đo held một lần (tổng).
   Giả thuyết kiểm được: số câu sai giảm nhiều hơn số câu đúng mất.
2. **Tập giả-held** từ train theo taxonomy RobuT + Auto-Tables (mở rộng DRS-v2 đã có), cố định trước khi sửa tiếp.
   Mọi bản sửa sau này phải không làm giảm PA trên tập này.
3. **Bộ kiểm annotation theo chữ ký giá trị + đồ thị khoá IND** (Sherlock/Auto-BI): thay các sửa riêng lẻ (lô, mã nhân viên,
   bảng phụ) bằng một cơ chế; kiểm trên DRS + shop gốc (đối chiếu số dòng/tổng như mục 6 AGENTS).
4. **Đo độ phủ chuẩn hoá tiếng Việt** bằng ViLexNorm (chỉ đánh giá) và bộ mẫu thời gian HeidelTime (chỉ kiểm thử);
   sửa lớp chuẩn hoá chung trong vocabulary, không theo câu.
5. Time reader: thêm trace lý do (không gọi / bị loại / nhận) rồi đo trên dev; giữ off nếu precision thấp.

## 4. Giới hạn
- Chưa đọc mã nguồn các repo trên (chỉ paper/README qua tìm kiếm); trước khi dùng phải ghim commit và kiểm giấy phép
  như các lượt trước (GPL của HeidelTime, NC-SA của ViLexNorm không phù hợp để nhúng).
- Các con số benchmark trong paper không chuyển sang bài toán này; chỉ dùng phương pháp và taxonomy.
- Chưa có bằng chứng nào trong tài liệu này về PA private.

## Nguồn
- TrustSQL: https://arxiv.org/abs/2403.15879 · https://github.com/glee4810/TrustSQL
- Reliable Text-to-SQL with Adaptive Abstention: https://arxiv.org/html/2501.10858v1
- Confidence Estimation for Text-to-SQL: https://arxiv.org/pdf/2508.14056 · Richardson 2026: https://arxiv.org/abs/2607.06799
- Auto-Tables: https://arxiv.org/pdf/2307.14565 · Auto-BI: https://arxiv.org/pdf/2306.12515
- TableSense: https://arxiv.org/abs/2106.13500 · TableSense2: https://github.com/Docimoto/TableSense2
- SpreadsheetLLM: https://arxiv.org/html/2407.09025v1
- Sherlock: https://www.github.com/mitmedialab/sherlock-project · Sato: https://www.github.com/megagonlabs/sato ·
  DoDuo: https://arxiv.org/pdf/2104.01785v1
- ViLexNorm: https://arxiv.org/pdf/2401.16403 · https://github.com/ngxtnhi/ViLexNorm
- HeidelTime: https://github.com/HeidelTime/heideltime · Duckling: https://github.com/facebook/duckling/blob/main/README.md
- dateparser: https://dateparser.readthedocs.io/en/v0.6.0/ · quantulum3: https://pypi.org/project/quantulum3/0.9.0
- Recognizers-Text: https://github.com/shana/Recognizers-Text
- RobuT: https://arxiv.org/pdf/2306.14321 · AmbiQT: https://arxiv.org/pdf/2310.13659
