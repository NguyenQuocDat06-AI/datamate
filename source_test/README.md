# BizReason

Agent trả lời câu hỏi tiếng Việt trên CSV, Excel và SQLite theo rulebook của DataMate. LLM lập kế hoạch và sinh SQL; chương trình đọc dữ liệu, thực thi, kiểm tra kiểu kết quả và ghi JSONL. Bộ chấm chạy riêng và không được import vào solver.

Đây là bản triển khai để phát triển và đo baseline. Các kiểm tra runtime không chứng minh một SQL bất kỳ đúng ngữ nghĩa. Chưa có kết quả đánh giá đầy đủ trên holdout hoặc leaderboard.

## Bắt đầu

Chạy các lệnh sau tại **`source_test/`**, với Python 3.11 trở lên:

```powershell
python -m pip install -e ".[dev]"
python -m bizreason --help
python -m pytest
```

`.env` hiện có được giữ nguyên. Backend development đọc:

```dotenv
TRAMMAY_KEY=your_key
BIZREASON_BASE_URL=https://trammay.xyz/v1
BIZREASON_MODEL=ag/gemini-3.8-flash-high
BIZREASON_TIMEOUT=150
```

Tên model đã được đối chiếu bằng `GET /v1/models`: model ID của provider là `ag/gemini-3.8-flash-high`. Không đưa `.env` vào Git hoặc Docker image.

```powershell
python -m bizreason models
python -m scripts.smoke_api
```

`smoke_api` chỉ gửi một yêu cầu JSON tự tạo, không đọc dữ liệu thi. Client hỗ trợ cả `/v1/chat/completions` và `/v1/responses`; chọn Responses bằng `run --api-style responses`.

## Chuẩn bị dữ liệu

Đợt sửa 03/11/2026 đã chuẩn hoá đủ train/public và kiểm tra 252 hash nguồn. Dùng [chỉ mục cache hiện hành](docs/NORMALIZED_DATA.md), xem [thay đổi và kiểm chứng](docs/NORMALIZATION_FIXES.md). Các cache/report cũ giữ để truy nguyên; không trộn revision khi chạy lại.

```powershell
python -m bizreason unpack --archive train.zip --destination data
python -m bizreason inspect --data data/train --business biz_104
python -m scripts.validate_ingestion --data data/train --archive train.zip --workers 3
```

Lệnh giải nén kiểm tra toàn bộ tên member, symlink, kích thước và đường dẫn trước khi ghi; từ chối ghi đè file đã có. Nếu đã giải nén thì dùng trực tiếp `data/train/`.

Cache SQLite nằm tại `artifacts/cache/<business>/<revision>/`. Revision chứa hash nguồn, rulebook và implementation ingestion. Không ghi ngược vào file doanh nghiệp. Có thể kiểm tra `catalog.json`, `business_manifest.json` và các diagnostic để biết parser đã quyết định gì.

## Chạy solver bằng API development

```powershell
python -m bizreason run --data data/train --business biz_104 --limit 3 --output result/predictions.jsonl --artifacts artifacts/experiment_01
```

Mỗi câu có giới hạn model calls, repairs, probes và thời gian. Có thể điều chỉnh:

```powershell
python -m bizreason run --data data/train --business biz_104 --limit 1 --max-model-calls 1 --max-repairs 0 --request-timeout 180 --question-timeout 200 --max-output-tokens 4000 --output result/one_question.jsonl --artifacts artifacts/one_question
```

API nhận câu hỏi, README, rulebook, metadata và một số giá trị mẫu; không nhận file `answers.jsonl`. Trace lưu kế hoạch, SQL, params, kết quả thực thi, phiên bản nguồn, lỗi, quyết định và chi phí model nếu provider trả usage. Khóa không nằm trong prompt, trace hoặc manifest.

Mặc định `--gate verified` nhận candidate đã qua các kiểm tra cấu trúc. Đây là chính sách **chưa calibration**, không gán xác suất giả cho candidate. Dùng `--gate templates` để chỉ nhận các template đủ tiền điều kiện; `--gate abstain` để đo baseline bỏ trả lời.

Để chạy hoàn toàn không gọi model và kiểm tra đường xuất file:

```powershell
python -m bizreason run --data data/train --business biz_104 --limit 3 --backend none --output result/abstain.jsonl --artifacts artifacts/abstain
```

## Chấm độc lập

```powershell
python -m bizreason score --predictions result/predictions.jsonl --references data/train/answers.jsonl --questions data/train/questions.jsonl --output artifacts/score_full.json
```

Mặc định chấm trên **toàn bộ reference**; các ID chưa dự đoán là abstain. Khi chỉ kiểm tra một mẫu nhỏ, phải chỉ định rõ:

```powershell
python -m bizreason score --predictions result/predictions.jsonl --references data/train/answers.jsonl --questions data/train/questions.jsonl --subset-predicted --output artifacts/score_subset.json
```

Report có C/W/A, PA, coverage, accuracy trên câu trả lời, phân tích theo business/type và false-positive unanswerable. Subset score không đại diện cho toàn train/holdout. Bộ chấm kiểm tra type trước value, giữ dấu tiếng Việt và thứ tự list, hỗ trợ alias theo vị trí và hàng table không có thứ tự. Rounding `.5` dùng HALF_UP; các góc không được đặc tả đủ bởi BTC vẫn cần đối chiếu grader chính thức.

CLI từ chối JSON hỏng hoặc ID trùng để nhóm sửa file trước khi chấm; không giả định cách BTC xử lý mọi file hỏng. Thông tin `reason` sai kiểu được scorer xem là abstain theo rulebook.

```powershell
python -m bizreason compare --before artifacts/score_a.json --after artifacts/score_b.json --output artifacts/comparison.json
```

So sánh yêu cầu cùng tập ID, báo các chuyển đổi đúng/sai/abstain thay vì chỉ nhìn chênh lệch PA.

## Split và calibration

```powershell
python -m bizreason split --questions data/train/questions.jsonl --output artifacts/split.json
```

Với 30 business, split mặc định tạo 18 fit, 6 calibration và 6 evaluation, không giao nhau. Solver hiện không có few-shot retrieval hoặc SFT; khi bổ sung, chỉ được fit trên nhóm fit. Calibration cần prediction từ business không tham gia fit/chọn prompt của solver.

```powershell
python -m bizreason calibration-records --traces artifacts/held_out_run/traces --references data/train/answers.jsonl --split-manifest artifacts/split.json --output artifacts/calibration_records.jsonl
python -m pip install -e ".[calibration]"
python -m bizreason fit-gate --records artifacts/calibration_records.jsonl --output artifacts/gate.json
python -m bizreason run --data data/train --gate calibrated --calibration artifacts/gate.json --limit 3 --output result/calibrated.jsonl --artifacts artifacts/calibrated
```

Gate lưu coefficients/scaler dưới JSON, không tải pickle. Cần cả nhãn dương và âm cho từng model xác suất. Manifest kiểm tra được business groups nhưng không chứng minh người phát triển chưa từng xem lỗi của holdout; nhóm phải tự giữ kỷ luật thí nghiệm. Gate chọn hành động theo `2*p_correct - 1`, với abstain có utility 0; `unanswerable` chỉ được xét khi có evidence được kiểm tra.

## Đổi sang backend local

```powershell
python -m bizreason run --data data/train --backend local --base-url http://127.0.0.1:8000/v1 --model YOUR_LOCAL_MODEL --limit 3 --output result/local.jsonl --artifacts artifacts/local
```

Local mode chỉ chấp nhận loopback và dùng credential giả `local`, không gửi `TRAMMAY_KEY`. Model server phải được khởi động và có weights sẵn; repo chưa đóng gói weights/vLLM hoặc xác nhận giới hạn VRAM. API development không đáp ứng yêu cầu inference offline của cuộc thi. Model ledger, tổng số tham số, ngày phát hành, licence và container GPU cần được hoàn thiện khi chọn model cuối.

## Cấu trúc

```text
bizreason/
  contracts.py          # Các object có version, enum và validation
  config.py, llm.py     # Backend API/local và giới hạn
  data/                 # Readers, header/group/parser, schema, joins, conversion
  rules.py, temporal.py # Runtime rules, lịch và cửa sổ thời gian
  grounding.py          # BM25, partition expansion, entity recall
  schema_context.py     # Gộp mô tả schema lặp, giữ đủ partition và diagnostic
  planning.py           # Context, QueryPlan và validator
  compilation.py        # Năm metric template + SQL fallback
  execution.py          # SQL chỉ đọc, authorizer, worker và watchdog
  verification.py       # Kết quả có kiểu, kiểm tra và chữ ký candidate
  decision.py           # Unsupported evidence và gate
  pipeline.py           # Budget, probes, repair, candidate selection
  formatting.py         # Serializer và JSONL checkpoint
  evaluation/           # Scorer, grouped splits, A/B, calibration
  cli.py                # Điểm vào dòng lệnh
tests/                  # Regression fixtures, gold, policy và integration
scripts/                # Kiểm tra ingestion và API
```

Xem [trạng thái triển khai](docs/IMPLEMENTATION_STATUS.md), [báo cáo kiểm chứng](docs/VALIDATION.md) và [kế hoạch](WORK_PLAN.md). Hai tài liệu nguồn là [kiến trúc](../DATAMATE_PIPELINE_ARCHITECTURE.md) và [source walkthroughs](../analysis/architecture_deep/SOURCE_WALKTHROUGHS.md).

## Giới hạn hiện tại

- Chỉ thực thi SQL. Chưa cho phép Python tùy ý, chưa triển khai SFT/ensemble hoặc mọi kỹ thuật tùy chọn trong paper.
- Template chỉ dùng khi metric, nguồn, policy, thời gian và shape phù hợp; câu có filter/group/comparison hoặc nhiều partition dùng SQL fallback.
- Mapping label/README và định dạng cột là heuristic có diagnostic; giá trị mơ hồ không âm thầm thành 0. Cần xem báo cáo ingestion của từng doanh nghiệp.
- Dòng tổng và ghi chú ở cuối vùng bảng được phân loại trước kế thừa nhóm; vị trí và lý do loại được ghi trong diagnostic. Tên sản phẩm giống nhãn tổng vẫn được giữ khi có bằng chứng giao dịch.
- Entity recall bị giới hạn, nên không tìm thấy candidate trong index không đủ kết luận unanswerable.
- Process riêng + SQL authorizer/read-only là cơ chế giới hạn cho backend SQL, không được mô tả như sandbox cho Python hoặc bảo vệ đầy đủ ở cấp hệ điều hành.
- SQL chạy được, được model đồng ý hoặc cho kết quả giống nhau vẫn có thể sai nghiệp vụ. Cần holdout, ablation và calibration trước khi dùng gate để tối ưu PA.
- Cache của một business/revision được thiết kế cho một tiến trình build tại một thời điểm. Các worker validation xử lý các business khác nhau.
