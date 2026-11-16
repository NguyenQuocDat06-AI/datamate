# Kiểm tra shop và rulebook mới

Đợt này thêm công cụ đo và sửa lỗi được tái hiện; chưa có phép đo model mới hoặc bằng chứng PA private 0,9.
Gói Docker XNeural đã upload được giữ nguyên. Semantic contracts và plan fallback vẫn mặc định tắt.

## Thay đổi inference

- Một dòng trùng tổng khối nhưng chỉ có một cột số, không có nhãn tổng rõ ràng, được giữ làm
  `ambiguous_total`. Một đẳng thức không phân biệt được giao dịch thật với tổng phụ.
- Bảng `__row_audit` giữ dòng nguồn, vị trí và quyết định của những dòng bị loại hoặc mơ hồ.
  Với ứng viên tổng phụ, bằng chứng gồm tổng khối/tổng chạy, giá trị quan sát và số dòng khối.
  Dòng thường vẫn nằm ở bảng nguồn cùng `__source_row` và `__raw_json` để tránh nhân đôi dữ liệu.
- Mỗi lượt `run_v2` khởi tạo lại tham số và thuật ngữ rulebook. Lượt sau không kế thừa cấu hình
  nghiệp vụ từ lượt trước khi chạy cùng process.

Đây là sửa hẹp. Các ứng viên nhiều cột vẫn dùng cơ chế cũ; nhãn tổng và số học không chứng minh
đầy đủ ý nghĩa của một dòng. Chưa có bộ chứng thực vai trò cột hoặc bộ đọc công thức mới hoàn chỉnh.

## Bộ thử nghiệm có cặp đối chiếu

Từ `source_test`, dùng Python môi trường hiện có:

```bash
/home/ubuntu/datamate/venv/bin/python -m scripts.generalization_probe generate \
  --out /absolute/path/new_suite --shops 12 --seed 97
```

Mỗi shop có hai dòng hàng mỗi giao dịch, hai cột ngày, trạng thái và mức thưởng khác nhau.
Bốn rulebook thay đúng một yếu tố so với bản cơ sở: nhận thêm trạng thái đang giao, cộng thuế,
hoặc chuyển từ ngày đặt sang ngày giao. Năm cách trình bày gồm bản gốc, đảo cột/chèn tiêu đề,
dòng tiếp để trống thông tin giao dịch, chia file theo tháng và tài liệu viết thành đoạn văn.
12 shop × 4 rulebook × 5 cách trình bày × 5 câu = 1200 câu.

`inputs/<scenario>/` là đầu vào inference. `references.jsonl`, bản ghi gốc, schema bindings và fold
nằm bên ngoài thư mục đó. Không đưa chúng vào prompt hoặc vào đường dẫn `--data` của inference.

Oracle là phép tính Python trên bản ghi gốc, độc lập với SQL/compiler sản xuất. Tác giả generator
đã đọc pipeline: đây là bộ chẩn đoán tổng hợp, chưa phải bộ kiểm độc lập hoặc đại diện private.
Các biến thể cùng shop nguồn nằm trong cùng fold; cần dành seed/họ bài toán khác cho lần kiểm cuối.

Kiểm ingestion CPU, không gọi model:

```bash
/home/ubuntu/datamate/venv/bin/python -m scripts.generalization_probe probe-ingestion \
  --suite /absolute/path/new_suite --out /absolute/path/new_ingestion_probe
```

Probe dùng schema bindings từ generator để đối chiếu số dòng, số giao dịch, tổng lượng, tổng tiền
và khoảng ngày. Nó không kiểm khả năng model đọc schema, đọc rulebook hoặc phân tích câu hỏi.

Để đo model thật, chạy `bizreason.run_v2` trên từng `inputs/<scenario>` với snapshot/model/cấu hình
cố định và thư mục output riêng. Chỉ dùng cơ chế local model hiện có; công cụ này không tự mở GPU,
đọc endpoint hoặc gọi API. Sau đó chấm các tệp dự đoán được chỉ định rõ:

```bash
/home/ubuntu/datamate/venv/bin/python -m scripts.generalization_probe score \
  --suite /absolute/path/new_suite \
  --predictions /absolute/path/run_a/submission.jsonl /absolute/path/run_b/submission.jsonl \
  --out /absolute/path/score.json
```

Chấm báo đúng/sai/abstain, `unanswerable`, thiếu dự đoán, PA theo shop/ngành/fold/quy tắc/bố trí,
CI bootstrap theo shop và độ đúng của từng cặp. Câu chưa được chạy tính abstain và báo riêng số thiếu.
ID lạ hoặc trùng bị từ chối. Hai câu cùng abstain không được tính là qua kiểm tra bất biến.
Đáp án thay đổi khi đổi rulebook chỉ được coi là đúng khi cả hai khớp reference.

Khi thời gian ít, chỉ chạy `base__plain` và `shipping__plain` trước, rồi thêm
`--scenarios base__plain shipping__plain` vào lệnh chấm. Với 12 shop, pilot này có 120 câu.
Chọn một cách trình bày khác phải kèm bản plain của cùng policy và bản base của cùng cách trình bày;
công cụ từ chối thiếu các cặp đối chiếu đó. Câu thuộc kịch bản không được chọn không tính là thiếu.

## Loại shop/ngành khỏi few-shot

`run_v2` nhận các cờ mới, mặc định không áp dụng loại trừ thêm:

```text
--fewshot-groups /absolute/path/groups.json
--fewshot-exclude-groups industry:held source:held-family
--fewshot-exclude-businesses held-shop-a held-shop-b
```

Metadata là JSON `{business_id: [group_tag, ...]}`. Nhãn nhóm do phép đo cung cấp, không được suy
đoán từ câu hỏi hay đáp án. Khi loại nhóm, mọi shop trong bank phải có metadata hợp lệ; tên nhóm
không tồn tại gây lỗi. Phải bổ sung metadata cho TRAIN bank trước khi dùng map của bộ tổng hợp.
Bank được lọc trước khi dựng lại chỉ mục và IDF, nên câu của fold bị giữ riêng cũng không ảnh hưởng
trọng số truy hồi. Manifest ghi hash metadata và số ví dụ trước/sau loại trừ.

## Kiểm tra hồi quy

```bash
/home/ubuntu/datamate/venv/bin/python -m pytest -o addopts="" -q
```

Báo cáo đợt triển khai: `../reports/GENERALIZATION_EXECUTION_20261114.md`.
