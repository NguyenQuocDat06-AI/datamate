# Nhật ký vòng lặp hướng A

Quy trình: `GOAL_LOOP_A.md`. Kế hoạch: `FABLE_REVIEW.md`.

## Trạng thái lúc bắt đầu (11/11, 05:20)

- `arch/data-operators` ở commit `e46113c`. Vòng 8 (ánh xạ tên thợ) chưa commit, 932 test pass.
- Điểm (bộ đo cũ, chỉ để tham chiếu): train-300 0,920; public có alias 0,686 (0,706 với vòng 8); public tắt alias 0,706.
- Máy chủ model online, khởi chạy lại lúc 04:50, hết hạn khoảng 16:50.

## Danh sách việc

- [x] 1. Tính tất định và chốt vòng 8, tạo worktree `arch9`
- [x] 1b. Lỗi model không được âm thầm thay bằng mặc định (thử lại, cờ thiếu, đọc lại địa chỉ máy chủ)
- [x] 2. Lệnh chạy đầu-cuối
- [x] 3. Phép đo 1: train-full chạy mới
- [x] 3b. Tăng thông lượng (FABLE_RESEARCH §3.4, việc 1):
  - (i) gom lô tĩnh trong máy chủ HF hiện tại, đo 1/4/8/16 luồng;
  - (ii) đo `llama-server` với GGUF, trong cùng phiên Kaggle;
  - (iii) vLLM có vá, chỉ khi (i) và (ii) chưa đạt 3 lần;
  - `run_v2` dựng shop song song với trả lời câu hỏi.
  - Điều kiện: ngoại suy dưới 4,5 giờ, phép đo 1 không giảm, câu đổi giữa hai lần chạy dưới 1%.
- [x] 4. Phép đo 2: nhiễu câu hỏi
- [x] 5. Phép đo 3: nhiễu dữ liệu, dùng bộ toán tử Auto-Tables làm bộ sinh nhiễu (FABLE_RESEARCH việc 11)
- [x] 6. Phép đo 4: bỏ một ngành (số nền theo ngành; phép ẩn thật làm sau A3)
- [x] 7. So model: bản fine-tune SQL hiện tại với `gemma-4-12B-it` bản QAT chính thức (GGUF Q4_0 / W4A16, Apache-2.0); kết quả quyết định luôn cách phục vụ (FABLE_RESEARCH việc 2)
- [x] 8. Chốt số nền (điểm kiểm soát 1)

## Lần thức 1 (05:35): mục 1, tính tất định

- Nhiệt độ sinh đã là 0 (`bizreason/llm.py:564`).
- Thử (`gen_linekind_probe.py`): gọi phân loại dòng cho `dev_013` hai lần trên cùng máy chủ, kết quả giống hệt. Có 6 tên được phân loại: voucher, dùng điểm, phí ship.
- **Nguyên nhân thật của việc `dev_013` tụt ở vòng 8 không phải model không tất định:**
  - `alias_off8.py` đọc `active_endpoint.json` một lần lúc khởi động. Lần chấm vòng 8 bắt đầu khi đường hầm cũ đã chết, nên mọi lần gọi model đều thất bại.
  - `classify_line_names` (`annotator.py` khoảng dòng 395) bắt mọi lỗi và trả về `{}`. Vì vậy voucher và phí ship (11.344 dòng) bị tính như hàng bán: 20.643 dòng thành 31.987 dòng.
- **Lỗi tổng quát mới phát hiện:** các bước gọi model (phân loại dòng, gom giá trị `group_values`, có thể cả chú thích) im lặng thay bằng giá trị mặc định khi model lỗi. Ở bài thi, một lỗi mạng tạm thời sẽ làm hỏng dữ liệu cả shop mà không có dấu hiệu nào.
  - Thêm mục 1b: khi model lỗi thì thử lại có giới hạn, rồi ghi cờ "thiếu" để các câu phụ thuộc abstain. Không được giả định mặc định. Client đọc lại địa chỉ máy chủ khi kết nối lỗi.
- Đang chấm lại vòng 8 tắt alias với máy chủ đang sống. Bảng hỏng chuyển sang `alias_off_run8/sem_r8bad`.
- Chấm lại vòng 8 tắt alias với máy chủ sống: **0,725** (80 / 6 / 16). `dev_013` về lại 20.643 dòng, `q_b342fe1e1211` thành đúng, không câu nào bị mất.
- Vòng 8 đạt cả ba phép đo cũ, nên commit `0cf8635` trên `arch/data-operators`.
- Tạo worktree `/home/ubuntu/agy-worktrees/arch9` trên nhánh `arch/rulebook-ir`, từ `0cf8635`. Đã có symlink dữ liệu, 932 test pass.
- Số tham chiếu theo bộ đo cũ: train-300 0,920; public có alias 0,706; public tắt alias 0,725.
- Đã xóa các bảng dự phòng, ổ đĩa trống 12 GB.
- **Mục 1 xong.** Lần thức sau: mục 1b.

## Lần thức 2 (05:30): mục 1b, lỗi model không được âm thầm thay bằng mặc định

- Khảo sát:
  - `annotator.py` có 7 chỗ bắt lỗi rồi trả về giá trị mặc định, ở các dòng 367, 395, 459, 566, 575, 601, 622.
  - Client đã thử lại 4 lần với thời gian chờ tối đa 10 giây, nhưng không đọc lại địa chỉ máy chủ khi đường hầm đổi.
- Giao 2 agent Haiku song song:
  - `llm.py`: thêm `endpoint_resolver` để đọc lại địa chỉ khi lỗi kết nối, và `outage_wait_seconds` để chờ máy chủ quay lại. Thêm `endpoint_file_resolver`.
  - `annotator.py`, `layer.py`, `solver.py`: ghi lại bước model thất bại (`take_failures`), không ghi cache khi thất bại, thêm bảng `model_failures` vào DB, và cho shop có bước thất bại abstain.
- Kết quả agent:
  - `annotator.py` ghi tên bước thất bại ở cả 7 chỗ bắt lỗi, và không ghi cache khi có lỗi.
  - `layer.build(model_failures=...)` ghi bảng `model_failures`. `solver` abstain khi bảng có dòng, trước nhánh "không trả lời được".
  - `llm.py` có `endpoint_resolver`, `outage_wait_seconds`, `endpoint_file_resolver`. Thêm 15 test.
- Mình sửa một chỗ: lần gọi gán vai trò đơn thất bại nhưng lần thử theo lô thành công thì không ghi là lỗi. Nếu ghi, cả shop sẽ abstain oan.
- Script mới:
  - `gen_eval_e2e9.py`: worktree `arch9`, dùng lại bảng `sem8`, ghi vào `e2e9/`.
  - `gen_alias_off9.py`: dựng lại vào `alias_off_run9/`, truyền `take_failures()` vào `build`.
  - Cả hai dùng bộ đọc lại địa chỉ máy chủ và chờ tối đa 30 phút khi mất kết nối.
- 956 test pass. Đang chấm để xác nhận không câu nào đổi so với vòng 8.
- Giới hạn còn lại: bộ đọc lại địa chỉ đọc `active_endpoint.json`. File này chỉ được cập nhật khi chạy `kaggle_12h_client.py --status` hoặc `--wait`. Nếu muốn tự động hoàn toàn, cần một tiến trình nền gọi `--status` định kỳ. Việc này để cho lệnh chạy đầu-cuối ở mục 2.
- Chấm kiểm tra: train 0,920, public 0,706, public tắt alias 0,725. Không câu nào đổi so với vòng 8, cả 14 shop có `model_failures` bằng 0.
- **Mục 1b xong.** Commit trên `arch/rulebook-ir` (xem `git log`). Lần thức sau: mục 2, lệnh chạy đầu-cuối.

## Lần thức 3 (06:08): mục 2, lệnh chạy đầu-cuối

- Thiết kế: `python -m bizreason.run_v2 --data <thư mục bộ test> --out <thư mục ra>`, gồm:
  - các bước ingestion, chú thích, trạng thái, phân loại dòng, gom giá trị, dựng bảng, hiểu câu hỏi, trả lời;
  - mọi cache nằm trong thư mục ra, không ghi cache khi có lỗi model;
  - checkpoint `submission.jsonl` và bản đủ `submission.final.jsonl`;
  - ngân sách thời gian, sắp hết thì phần còn lại abstain;
  - `timing.jsonl` theo bước, theo câu, kèm ngoại suy cho 30 shop và 3000 câu;
  - `manifest.json` (commit, sha256 rulebook, không có token);
  - `--no-alias` để đo với tên cột lạ.
- Giao 1 agent Haiku: chỉ được tạo `bizreason/run_v2.py` và `tests/test_run_v2.py`.
- Agent xong `bizreason/run_v2.py` và 9 test, 965 test pass. Mình sửa `--business` để viết nhiều lần được (`action="extend"`).
- Chạy thử thật trên `dev_002`, tắt alias, hiểu câu hỏi mới hoàn toàn (không dùng intent lưu sẵn), 4 luồng: 41 câu, PA **0,610** (30 / 5 / 6). Chấm bằng `gen_score.py` mới.
- **Thời gian (rủi ro thật):**
  - Dựng một shop mất khoảng 106 giây, phần lớn là ingestion (60 giây).
  - Mỗi câu mất trung vị khoảng 29 giây với 4 luồng song song, tức máy chủ xử lý gần như tuần tự, khoảng 7 giây mỗi câu.
  - Ngoại suy cho 30 shop và 3000 câu: **7,04 giờ, vượt giới hạn 6 giờ.** Cần giảm số lần gọi và độ dài prompt hiểu câu hỏi, hoặc gom lô trên máy chủ. Ghi thành việc sau mục 3, sau khi có số đo đầy đủ.
- **Mục 2 xong.** Commit `7979eaf` trên `arch/rulebook-ir`. Lần thức sau: mục 3, phép đo 1.

## Lần thức 4 (06:25): mục 3, phép đo 1 (train-full, chạy mới)

- Tập nghiệm thu là 200 trong 1000 câu public, không trùng câu train nào. Chỉ đếm số id trùng, không in id.
- Thư mục bộ test `train_full_root/` gồm 3000 câu train của 30 shop, symlink tới dữ liệu và rulebook train.
- Đang chạy `run_v2` tắt alias, 4 luồng, ngân sách 10 giờ, vào thư mục `train_full_1/`, dự kiến khoảng 7 giờ. Chấm bằng `gen_score.py` theo `reference_answers.jsonl`, kèm khoảng tin cậy theo shop.
- Sau khi xong còn phải: chạy lại 3 shop để xác nhận kết quả giống nhau từng câu, và tách điểm theo câu đã xem lúc phát triển và câu chưa xem.
- Chuẩn bị mục 4 trong lúc chờ (agent Haiku, không gọi model):
  - Viết `gen_perturb_questions.py` và 14 test.
  - Mẫu 500 câu train, chia đều theo shop, nằm ở `perturb_q500/`: 379 biến thể bỏ dấu (121 câu vốn không dấu), 500 biến thể viết tắt, 171 biến thể đổi chỗ cụm thời gian (329 câu bỏ qua vì cụm thời gian nằm giữa câu, không có, hoặc có nhiều cụm).
  - Điểm chưa đẹp: câu đổi chỗ có thể viết hoa giữa câu ("30 ngày gần nhất Cho tôi biết dthu"), nghĩa không đổi.
- 06:24: phép đo 1 đang chạy, đã xong ingestion của shop đầu tiên.
- 06:30, câu hỏi của người dùng về 8 hoặc 16 luồng:
  - Máy chủ có 2 tiến trình, mỗi GPU một tiến trình. Mỗi tiến trình sinh một yêu cầu một lúc sau `gpu_lock` (`run_server_12h.py:212`), không gom lô.
  - 4 luồng đã giữ cả hai GPU bận. Thêm luồng chỉ làm hàng đợi dài hơn và có nguy cơ vượt thời gian chờ 200 giây.
  - Thêm mục 3b: gom lô trên máy chủ, và dựng shop song song với trả lời câu hỏi.
  - `run_v2` hiện dựng hết 30 shop rồi mới trả lời câu hỏi, mất khoảng 50 phút.

## Việc dự trữ sau điểm kiểm soát 1 (từ `FABLE_RESEARCH.md` §5.2, chưa làm)

- Song song với A1:
  - hồ sơ cột (kiểu, độ phủ, giá trị mẫu) làm đầu vào chú thích (việc 3);
  - dự đoán kiểu đáp án và kiểm tra khớp kiểu làm cổng abstain (việc 4);
  - nối giá trị hai bước: khớp ký tự hoặc nhúng ra ứng viên, rồi model chọn hoặc trả "không có" (việc 5).
- Cách làm A1 và A3:
  - khung MetricFlow (đơn, tỉ lệ, lũy kế, dẫn xuất) cho IR (việc 6);
  - thư viện định nghĩa dựng sẵn ngoài giờ, lúc chạy chỉ khớp và điền tham số (việc 7).
- Sau khi có gom lô:
  - một lượt sửa theo lỗi khi IR không biên dịch được hoặc ra rỗng (việc 8);
  - ví dụ few-shot truy hồi, tách theo shop (việc 9);
  - nhiều góc nhìn chỉ làm tín hiệu abstain (việc 10).
- Cuối cùng: LoRA trên nhãn đã kiểm bằng thực thi (việc 12). Trước đó cần hỏi ban tổ chức về luật model.
- Điều kiện đưa hướng B lên sớm: phép đo bỏ một ngành ở A3 khôi phục dưới 60%, hoặc hơn 15% câu abstain vì "ngoài danh mục" sau A2.

## Lần thức 5 (07:25): chờ phép đo 1, chuẩn bị mục 3b(i)

- Phép đo 1: đã dựng 14/30 shop (khoảng 275 giây mỗi shop), chưa có lỗi model.
- Chuẩn bị gom lô cho máy chủ:
  - `run_server_12h.py` gốc có token và tên kênh relay nằm sẵn trong mã nguồn.
  - Mình tạo bản sao sạch ở `server_batch/run_server_12h.py`, thay 3 giá trị đó bằng `__SECRET_n__`.
  - Bảng ánh xạ để điền lại lúc push nằm cạnh file gốc, ở `.secret_map.json`, quyền 600, không bao giờ in ra.
- Giao 1 agent Haiku làm hai việc:
  - Gom lô vi mô trong mỗi worker: hàng đợi và một luồng GPU, chờ tối đa 40 ms hoặc 8 yêu cầu, đệm bên trái, một lần `generate`; hết bộ nhớ thì chia đôi lô. Đặt `BATCH_MAX=1` thì giống hệt cũ.
  - Script đo `gen_bench_server.py`, đo ở 1/4/8/16 luồng, gồm thông lượng, độ trễ và tỉ lệ đầu ra giống hệt mức 1 luồng.
- Chỉ push sau khi phép đo 1 xong và kernel hiện tại hết hạn, khoảng 16:50, theo quy tắc mỗi 12 giờ một lần.
- Agent xong phần gom lô ở `server_batch/run_server_12h.py`, 8 test pass, script và mã worker sinh ra đều biên dịch được.
  - `MicroBatcher`: một hàng đợi và một luồng GPU giữ `gpu_lock`.
  - Chỉ yêu cầu không streaming và nhiệt độ 0 mới đi đường gom lô. Đặt `BATCH_MAX=1` thì giữ đúng đường cũ.
  - Hết bộ nhớ thì chia đôi lô.
  - Phần `generate_batch` chạy torch chưa có test, chỉ mới biên dịch, phải kiểm trên Kaggle.
- Script đo `gen_bench_server.py` đã viết, chưa chạy.
- Agent để lại 2 file tạm trong `/tmp`, chỉ là bản sao sạch, không chứa khóa. Đã xóa.
- Viết `gen_push_server.py`: điền lại các chuỗi giữ chỗ vào một thư mục tạm quyền 700, push lên kernel cũ, rồi xóa thư mục tạm. Không in giá trị nào. Chạy thử `--dry-run` đạt.
- 07:40: người dùng thêm tài khoản Kaggle thứ hai (`KAGGLE_USERNAME1`/`KAGGLE_KEY1`) vì hạn mức cũ sắp hết.
  - Đã kiểm tra: cả hai biến có mặt, khác tài khoản cũ, đăng nhập được, và tài khoản 1 đã có kernel cùng tên. Không in giá trị nào.
  - Thêm `gen_kaggle_auth.py` và `--account` cho `gen_push_server.py`. Chạy thử `--dry-run` đạt cho cả hai tài khoản.
  - Lần khởi chạy tới, bản gom lô, dùng tài khoản 1.
- Dự phòng: nếu kernel tài khoản 0 dừng vì hết hạn mức trong lúc phép đo 1 đang chạy, khởi chạy lại bằng `gen_push_server.py --account 1 --original`, tức máy chủ gốc không gom lô, để không đổi cấu hình giữa phép đo. Sau đó chạy `kaggle_12h_client.py --wait` để cập nhật địa chỉ. Client chờ tối đa 30 phút nên lần chạy không mất câu nào.
- 08:12: phép đo 1 đã dựng 27/30 shop.

## Lần thức 6 (08:43): phép đo 1 bắt đầu trả lời câu hỏi

- Đã dựng xong cả 30 shop. Dựng bảng mất khoảng 2 giờ 20 phút.
- Đã trả lời 200/3000 câu. Thông lượng 7,5 giây mỗi câu với 4 luồng, dự kiến xong khoảng 14:35.
- Điểm tạm trên 200 câu đầu: PA **0,810** (166 đúng / 4 sai / 30 abstain). Thấp hơn 0,920 của train-300, vốn dùng intent lưu sẵn, chú thích đã sửa tay và có alias. Điều này khớp với dự đoán của `FABLE_REVIEW.md`. Chưa kết luận khi chưa đủ mẫu.
- Máy chủ tài khoản 0 vẫn online.

## Lần thức 7 (09:43)

- Đã trả lời 644/3000 câu. Điểm tạm: PA **0,798** (534 / 20 / 90). Máy chủ tài khoản 0 vẫn online.
- **Phát hiện:** cả shop `biz_120` abstain với lý do "model stage failed".
  - Bước chú thích bảng bán hàng tháng `danhsachhoadon_01_2025` thất bại ở cả bước chọn loại bảng (`kind`) lẫn bước gán vai trò (`annotation`).
  - Bảng này cũng từng lỗi trong `ann_rest` trước đây, và chỉ chạy được nhờ bản sửa tay `ann_fix`. Nghĩa là đây là lỗi lặp lại được trên một dạng bảng, không phải lỗi mạng.
  - Cổng mới ở mục 1b đã làm đúng việc: biến câu sai thành abstain. Nhưng chặn cả shop là quá thô, vì shop này có khoảng 100 câu, tức khoảng 3% PA.
- **Việc sau phép đo:**
  - Tái hiện lỗi chú thích bảng này, khi máy chủ rảnh.
  - Thu hẹp cổng: chỉ chặn câu phụ thuộc bảng lỗi. Nếu các sheet tháng khác cùng cấu trúc đã chú thích được, dùng chú thích của chúng.
  - Tổng quát hơn: các sheet cùng chữ ký cột dùng chung một chú thích. Đây là ứng viên của việc 3 (hồ sơ cột) ở danh sách dự trữ.

## Lần thức 8 (10:43)

- Đã trả lời 1117/3000 câu. Điểm tạm: PA **0,795** (924 / 36 / 157). Máy chủ online. Tốc độ khoảng 470 câu mỗi giờ, dự kiến xong khoảng 14:45.
- Ổ đĩa còn 8,2 GB nên đã xóa bảng và cache cũ của `alias_off_run8` và `run_v2_smoke`. Các file kết quả `records*.jsonl` vẫn giữ.
- Theo yêu cầu người dùng: đã xóa `norm_env` (1,1 GB) và `~/.cache/pip` (3,3 GB).

## Lần thức 9 (11:42)

- Đã trả lời 1572/3000 câu. Điểm tạm: PA **0,790** (1293 / 51 / 228). Máy chủ online, dự kiến xong khoảng 14:50.
- Lý do abstain trên phần đã chạy, chỉ là số sơ bộ:

  | Lý do | Số câu |
  |---|---|
  | Bước model thất bại (`biz_120`, cả shop) | 60 |
  | Cổng chất lượng dữ liệu | 52 |
  | Tồn kho (`inventory_qty`, `inventory_value`), chưa có đại lượng | 26 |
  | Không khớp được sản phẩm | 19 |
  | Không có khung thời gian | 13 |
  | Câu hai phần ở dạng `breakdown` / `share_of_total` | 15 |

  - Có 14 câu có đáp án là "không trả lời được" đang bị abstain.
- Câu sai theo kiểu đáp án: number 30, percent 9, **table 7**, unanswerable 2, date 2.
  - Hệ thống chưa trả lời được dạng bảng, nên câu dạng bảng mà vẫn bị trả lời là sai kiểu. Đây là ứng viên cho việc "dự đoán kiểu đáp án và kiểm tra khớp kiểu" (`FABLE_RESEARCH` việc 4).
- Phân tích đầy đủ sẽ làm khi có đủ 3000 câu.

## Lần thức 10 (12:41)

- Đã trả lời 2027/3000 câu. Điểm tạm: PA **0,781** (1653 / 70 / 304). Máy chủ online, dự kiến xong khoảng 14:50.

## Lần thức 11 (13:41)

- Đã trả lời 2493/3000 câu. Điểm tạm: PA **0,781** (2031 / 83 / 379). Máy chủ online, dự kiến xong khoảng 14:50.

## Lần thức 12 (14:47): **số nền phép đo 1**

- 3000 câu train, 30 shop, tắt alias, mọi bước chạy mới, không dùng cache: **PA 0,785**, khoảng tin cậy 95% theo shop là [0,689; 0,863], gồm 2457 đúng, 101 sai, 442 abstain.
  - So với bộ đo cũ (train-300, intent lưu sẵn, chú thích sửa tay, có alias): 0,920. Khoảng cách 13,5 điểm là mức bộ đo cũ đã thổi phồng.
  - Câu đã xem lúc phát triển (1060 câu): 0,817. Câu chưa xem (1940 câu): **0,768**.
- **Thời gian:**
  - Ingestion 2188 giây, chú thích 1808 giây, gom giá trị 722 giây, dựng bảng 2272 giây. Trung bình 237 giây mỗi shop.
  - Mỗi câu 31 giây với 4 luồng, tức khoảng 7,8 giây thông lượng.
  - Ngoại suy cho 30 shop và 3000 câu: **8,45 giờ, vượt giới hạn 6 giờ khoảng 41%.** Mục 3b là bắt buộc.
- **Shop tệ nhất:** `biz_120` 0,00, `biz_123` 0,09, `biz_124` 0,68, `biz_130` 0,71, `biz_125` 0,72.
- **Lý do abstain (442):**

  | Lý do | Số câu |
  |---|---|
  | Bước model thất bại, cả shop `biz_120` (chú thích bảng tháng 01) | 118 |
  | Cổng chất lượng `order_structure_broken`, cả shop `biz_123`. Chú thích mới có khóa gõ sai, đúng bộ phát hiện từng sửa ở vòng 1 | 91 |
  | Tồn kho, chưa có đại lượng | 43 |
  | Không khớp được sản phẩm | 33 |
  | Bộ lọc không có trong câu hỏi | 23 |
  | Không có khung thời gian | 22 |
  | Câu hai phần ở dạng `breakdown` | 17 |
  | Không có khung so sánh | 11 |

  - **209/442 câu abstain (khoảng 7 điểm PA) là do chú thích mới hoàn toàn trên 2 shop.** Đây là lỗi tổng quát về độ vững của chú thích, không phải lỗi của câu hỏi.
- Câu sai theo kiểu đáp án: number 60, percent 20, **table 15**, unanswerable 3, date 2, string 1.
  - Có 48 câu đáp án là "không trả lời được" bị abstain. Hệ thống trả "không trả lời được" 27 lần, đúng 19.
- **Việc ưu tiên rút ra, xếp theo điểm:**
  1. Độ vững của chú thích: thử lại theo lô nhỏ hơn; các sheet cùng chữ ký cột dùng chung chú thích; nối khóa gõ sai; hồ sơ cột làm đầu vào. Dự kiến lấy lại phần lớn của 209 câu.
  2. Thông lượng (3b).
  3. Kiểu đáp án và đáp án dạng bảng: 15 câu sai, đều trừ điểm.
  4. Tồn kho qua bộ nạp tổng quát ở A2: 43 câu.
- Đang chạy lại 3 shop (`biz_101`, `biz_108`, `biz_123`) để kiểm tra tính tất định.

## Lần thức 13 (15:05): người dùng yêu cầu tăng tốc, làm song song vì sắp tới hạn

- 4 lõi CPU, 23 GB RAM.
- **Tài khoản Kaggle 1 cũng đã hết hạn mức GPU tuần này** ("Maximum weekly GPU quota of 30.00 hours reached"), khi push kernel riêng tư mới `datamate-batch-server`. Hiện chỉ còn kernel tài khoản 0 đang chạy, hết hạn khoảng 16:50.
- Kernel cũ `gemma4-12b-sql-12h-persistent-api-server` trên tài khoản 1 đang để công khai. Đã báo người dùng nguy cơ lộ token và tên kênh relay. Mình không tự sửa kernel đó.
- `gen_push_server.py` thêm `--slug` và luôn đặt `is_private=True`.
- Hai agent Haiku chạy song song, chỉ dùng CPU, không gọi model:
  - `run_v2.py`: dựng shop song song (`--build-workers`), trả lời câu hỏi của shop nào dựng xong trước, giới hạn số lệnh gọi model đồng thời, mỗi shop một `Annotator`.
  - `annotator.py`, `layer.py`, `quality.py`: chẩn đoán `biz_120` và `biz_123`; dùng lại chú thích của sheet anh em khi một sheet lỗi; thử lại bước chọn loại bảng với prompt ngắn hơn; nối khóa vai trò gõ sai; bộ phát hiện chỉ chặn khi vai trò quan trọng vẫn thiếu sau mọi bước lấp.
- Lần chạy kiểm tra tất định 3 shop vẫn đang chạy trên tài khoản 0.
- **Đính chính tài khoản Kaggle (15:30), do người dùng chỉ ra:**
  - `kagglesdk` luôn đọc `~/.kaggle/access_token` trước biến môi trường (`kagglesdk/kaggle_env.py:106`). Các khóa trong `.env` là token kiểu mới `KGAT_`, cần đặt vào `KAGGLE_API_TOKEN`.
  - Vì vậy mọi lệnh trước đó đều chạy bằng tài khoản trong `~/.kaggle`. Đó cũng là tài khoản sở hữu kernel máy chủ đang chạy, và là tài khoản đã hết 30 giờ GPU. Tài khoản 1 không hề hết hạn mức.
  - Đã sửa `gen_kaggle_auth.py`: dùng `KAGGLE_API_TOKEN` và thư mục cấu hình riêng cho từng tài khoản. Đã xác nhận cả hai tài khoản đăng nhập đúng người dùng trong `.env`.
  - Cảnh báo lúc nãy về kernel công khai có thể sai: API danh sách không trả trường riêng tư. Kernel nào mình push cũng luôn đặt `is_private=True`.
- 15:31: push bản máy chủ gom lô lên tài khoản 1 (phiên bản 1, `RUNNING`). Kernel cũ trên tài khoản `~/.kaggle` vẫn chạy cho lần kiểm tra tất định.
- Hai agent Haiku xong, 992 test pass, chưa commit vì cần đo:
  - `run_v2`: dựng shop song song bằng tiến trình con (`--build-workers 2`), trả lời câu hỏi của shop nào xong trước, cổng `--max-model-concurrency`.
  - Chú thích:
    - Dùng lại chú thích của sheet anh em khi một sheet lỗi.
    - Thử lại bước chọn loại bảng với prompt ngắn.
    - Chỉ ghi lỗi của lần thử được giữ. Đây là nguyên nhân thật của `biz_120`: lỗi của lần thử 1 bị ghi dù lần thử lại đã thành công.
    - Nối khóa theo nhiều tầng: dạng gọn, tiền tố hoặc hậu tố, difflib, Levenshtein ≤ 2. Ở `biz_123`, khóa thiếu giảm từ 1 xuống 0.
- Đã tạo `bench_prompts.jsonl`: 32 prompt hiểu câu hỏi thật, trung bình 9481 ký tự.
- 15:38: **kiểm tra tính tất định đạt.** Chạy lại 3 shop (`biz_101`, `biz_108`, `biz_123`), 277 câu, cùng code và cùng máy chủ: không câu nào khác lần đầu.
- **Mục 3 xong.** Số nền phép đo 1: PA 0,785 [0,689; 0,863].
- Đang chờ máy chủ gom lô trên tài khoản 1 công bố địa chỉ. `gen_wait_new_endpoint.py` ghi địa chỉ ra `endpoint_batch.json` riêng, để không đổi địa chỉ đang dùng.

## Lần thức 14 (16:02): đo thông lượng máy chủ gom lô (mục 3b-i)

- Máy chủ gom lô trên tài khoản 1, 32 prompt hiểu câu hỏi thật, `gen_bench_server.py`:

  | Số luồng | Yêu cầu/phút | p50 | p90 | Giống hệt 1 luồng |
  |---|---|---|---|---|
  | 1 | 3,98 | 14,4 s | 17,4 s | 100% |
  | 4 | 9,57 | 21,6 s | 32,4 s | 100% |
  | 8 | 11,12 | 43,7 s | 46,5 s | 96,9% (1 câu khác) |
  | 16 | 11,80 | 75,9 s | 86,8 s | 100% |

- Từ 4 lên 16 luồng chỉ tăng 1,23 lần. So với máy chủ cũ ở 4 luồng (khoảng 8 yêu cầu/phút), bản gom lô ở 8 luồng nhanh khoảng 1,4 lần. **Chưa đạt mục tiêu 3 lần.**
- Nguyên nhân:
  - Prompt hệ thống (`intent.py` `SYSTEM`) dài 9067 ký tự, khoảng 3000 token, giống hệt nhau ở mọi câu. Phần riêng của câu hỏi chỉ khoảng 550 ký tự. Đầu ra khoảng 230 ký tự, 70–80 token.
  - Ước lượng: đọc prompt chiếm khoảng 70% thời gian mỗi yêu cầu. Gom lô chủ yếu giúp giai đoạn sinh token, nên lợi ích bị giới hạn.
- **Việc tiếp theo cho thông lượng (3b-ii), ưu tiên cao:**
  - Cache KV của phần đầu chung trong máy chủ: tính một lần KV của prompt hệ thống cho mỗi worker, rồi dùng lại cho mọi yêu cầu và mọi lô. Ước lượng nhanh thêm 3–4 lần.
  - Hoặc `llama-server` với GGUF, vốn có sẵn cache prompt và nhiều slot song song.
  - Song song đó, có thể rút gọn prompt hệ thống, nhưng việc này đổi hành vi nên phải đo bằng phép đo 1.
- Thêm một lợi thế khi đo: có hai máy chủ trên hai tài khoản thì chạy được hai phép đo song song.
- 16:20: agent Haiku xong cache prefix KV trong `server_batch/run_server_12h.py`.
  - Gồm LRU 2 slot, ranh giới prefix lấy theo chat template, lô bố trí theo dạng prefix | đệm | phần riêng.
  - Lúc khởi động, máy chủ tự kiểm tra kết quả có cache và không cache phải giống từng token. Nếu khác thì tự tắt cache.
  - 26 test pass. Phần torch chưa chạy thử được vì máy này không có torch, chỉ kiểm được trên Kaggle.
- Nới quy tắc khởi chạy lại: được khởi chạy lại để thử thay đổi máy chủ khi máy chủ đó không phục vụ phép đo nào.
- Push phiên bản 2 lên tài khoản 1. Đang chờ địa chỉ mới rồi đo lại thông lượng (`bench_batch2`).
- 16:26: **chạy lại 2 shop có chú thích hỏng**, dùng code mới, máy chủ cũ, `--build-workers 2`:
  - `biz_120`: 0 → **100 đúng** / 1 sai / 17 abstain (13 câu là tồn kho).
  - `biz_123`: 12 → **91 đúng** / 6 sai / 21 abstain.
  - Thêm khoảng 173 điểm ròng trên 3000 câu. Nếu các shop khác không đổi, phép đo 1 sẽ lên khoảng 0,843. **Phải chạy lại toàn bộ để xác nhận**, vì cách nối khóa mới có thể đổi kết quả ở shop khác.
  - Thông lượng với 2 tiến trình dựng song song là 7,9 giây mỗi câu ở trạng thái ổn định. Ngoại suy 6,78 giờ, giảm từ 8,45 giờ, vẫn vượt 6 giờ.
- Commit `0227155` trên `arch/rulebook-ir` (992 test pass): `run_v2` song song và chú thích vững hơn.

## Lần thức 15 (16:30): đo máy chủ v2 (gom lô và cache prefix)

| Số luồng | Yêu cầu/phút | p50 | Giống hệt 1 luồng |
|---|---|---|---|
| 1 | 4,75 | 11,8 s | 100% |
| 4 | 9,44 | 24,9 s | 100% |
| 8 | 14,99 | 30,9 s | 90,6% (3 câu khác) |
| 16 | **18,07** | 45,4 s | 96,9% |

- So với máy chủ cũ ở 4 luồng (khoảng 8 yêu cầu/phút): **nhanh khoảng 2,26 lần**, tức khoảng 3,3 giây mỗi câu ở 16 luồng.
- Chạy 1 luồng chỉ giảm từ 14,4 xuống 11,8 giây. Có thể cache prefix chỉ có tác dụng một phần. Chưa có log máy chủ để xác nhận.
- Tỉ lệ giống hệt giảm còn 90–97% ở 8–16 luồng. Có lẽ do sai số fp16 khi gom lô. Cần đo xem đáp án cuối có đổi không.
- 16:31: chạy lại phép đo 1 trên v2 (tài khoản 1), 16 luồng, 2 tiến trình dựng song song, vào `train_full_2/`. Lần chạy đo cùng lúc điểm sau bản sửa chú thích và thời gian thật.
- 16:45: agent Haiku xong phần kiểm tra kiểu đáp án và trả lời dạng bảng, chưa commit, 1009 test pass.
  - Hàm chấm: số cột phải khớp, so cột theo vị trí (tên cột chỉ dùng khi cùng một tập tên), không quan tâm thứ tự dòng.
  - Cả 19 đáp án dạng bảng của train đều là xếp hạng khách nợ (`khach_hang`, `so_tien_no`).
  - `TABLE_ASK_CUES` gồm "kem", "moi nguoi", "tung nguoi", "va so". Agent đã loại "lan luot", "tung" và "bao nhieu" vì chúng làm đổi các câu đang đúng.
  - Offline: dấu hiệu khớp 78 câu (15 câu sai dạng bảng, 16 abstain, 47 câu đúng dạng số hoặc phần trăm mà code không đụng tới). Không câu đúng nào bị đổi.
  - Số tiền trong bảng chưa kiểm được. Cần chạy lại 78 câu đó, dùng lại bảng của `train_full_2`.
- 16:41: agent xong `gen_perturb_data.py`, 10 test pass. Đã chạy `--all-ops` trên `biz_101` và `biz_108`, ra 5 thư mục bộ test trong `perturb_data/<op>/`, mỗi thư mục 159 câu, tổng 58 MB. Số dòng dữ liệu giữ nguyên.
  - Hạn chế: dòng "Tổng cộng" cộng cả cột mã số. Danh sách khách bị tách theo ngày sinh. `manifest.json` nằm trong thư mục shop, cần kiểm tra ingestion bỏ qua file này.
- `train_full_2`: đã dựng 2 shop, trả lời 116 câu sau khoảng 10 phút.
- 17:12: `train_full_2` sau 43 phút: dựng 7/30 shop, trả lời 590 câu, điểm tạm PA 0,880, không có lỗi model.
  - Giờ dựng bảng là chỗ nghẽn: khoảng 12 phút mỗi shop cho mỗi tiến trình, vì 4 lõi CPU phải chia cho cả các luồng trả lời.
  - Dự kiến tổng khoảng 3,1 giờ, xong khoảng 19:35. **Lần đầu tiên vừa trong 6 giờ.**
- Đã kiểm tra: ingestion chỉ đọc `.csv`, `.xlsx`, `.sqlite`, `.db` (`ingestion.py:51`), nên `manifest.json` trong thư mục nhiễu dữ liệu bị bỏ qua.
- Lưu ý: ingestion không đọc `.xls`. Nếu bộ private có file `.xls` thì sẽ mất dữ liệu. Cần kiểm tra bộ public và train có file `.xls` không.
  - Kiểm tra: train và public không có file `.xls` nào (176 xlsx, 32 csv, 8 sqlite). Rủi ro thấp nhưng vẫn còn với bộ private.
- 17:58: `train_full_2` sau 89 phút: dựng 15/30 shop, trả lời 1267 câu, điểm tạm PA 0,848 (1117 / 42 / 108), không có lỗi model. Dự kiến xong khoảng 19:35.
- 18:49: `train_full_2`: dựng 24/30 shop, trả lời 2021 câu, điểm tạm PA 0,830 (1757 / 79 / 185), không có lỗi model. Dự kiến xong khoảng 19:40.

## Lần thức 16 (19:40): phép đo 1 lần 2, trả lời dạng bảng, bắt đầu phép đo 2

- **Phép đo 1, lần 2** (máy chủ v2 có gom lô và cache prefix, commit `0227155`, 16 luồng, 2 tiến trình dựng):
  - **PA 0,842**, khoảng tin cậy [0,813; 0,871], gồm 2634 đúng, 109 sai, 257 abstain. Lần 1 là 0,785.
  - **Thời gian thật 3,16 giờ** cho 30 shop và 3000 câu, vừa trong 6 giờ. Mục 3b xong.
  - Gom giá trị tốn 4823 giây tổng, vì gọi model. Đây là chỗ nên tối ưu tiếp.
  - Thay đổi so với lần 1:
    - 2 shop được sửa: 179 câu từ abstain thành đúng, 6 câu từ abstain thành sai.
    - 28 shop còn lại: chỉ 6 câu đổi (2 thành đúng, 2 thành abstain, 2 thành sai). Đây là nhiễu do đổi máy chủ và gom lô.
- **Trả lời dạng bảng** (commit `7f02100`), đo trên 78 câu có dấu hiệu: 17 câu từ sai thành đúng, 2 câu từ abstain thành đúng, 52 câu đúng giữ nguyên, không câu nào mất. Ước tính phép đo 1 lên khoảng **0,854**.
- Đã xóa bảng và cache của `train_full_1`, `train_det_2`, `train_fix_ann` (khoảng 2,5 GB). Kết quả vẫn giữ.
- 19:45: bắt đầu **phép đo 2** trên `perturb_q500`: 500 câu gốc và 1050 biến thể, dùng lại bảng của `train_full_2`, vào thư mục `q500_run/`.
- 20:35: tài khoản 0 cũng đã hết hạn mức GPU tuần này (push trả "Maximum weekly GPU quota of 30.00 hours reached"). Hiện chỉ còn máy chủ v2 trên tài khoản 1, hết hạn khoảng 04:20. Các phép đo phải chạy lần lượt.
- Chuẩn bị phép đo 3: thư mục `perturb_all/` gộp 5 phép biến đổi × 2 shop thành 10 shop (`<biz>__<op>`), 795 câu, kèm `mapping.json` trỏ về id gốc. Sẽ chạy ngay khi phép đo 2 xong.

## Lần thức 17 (21:01): **số nền phép đo 2** (nhiễu câu hỏi)

- 500 câu train gốc (chia đều theo shop) và 1050 biến thể, commit `7f02100`, máy chủ v2. **Tỉ lệ đáp án giữ nguyên chung: 0,917.**

  | Biến thể | Số câu | Giữ nguyên | PA |
  |---|---|---|---|
  | Câu gốc | 500 | | 0,858 |
  | Bỏ dấu | 379 | 0,900 | 0,773 |
  | Chèn viết tắt | 500 | 0,912 | 0,792 |
  | Đổi chỗ cụm thời gian | 171 | 0,971 | 0,901 |

- Bỏ dấu và viết tắt làm PA giảm 7–8 điểm. Đây là điểm yếu tiếng lóng mà `FABLE_REVIEW` đã nêu. Ứng viên sửa: so model gốc QAT (mục 7), ví dụ few-shot truy hồi, cho model xem cả câu đã thêm dấu.
- 21:02: bắt đầu phép đo 3 trên `perturb_all/` (10 shop, 795 câu), vào `perturb_all_run/`.

## Lần thức 18 (21:20): **số nền phép đo 4** (theo ngành)

- `gen_industry_baseline.py` tính trên `train_full_2`, kết quả ở `industry_baseline.json`. Shop được xếp ngành theo từ khóa trong dữ liệu, vì README không ghi tên ngành.

  | Ngành | Shop | Câu | PA | Khoảng tin cậy 95% |
  |---|---|---|---|---|
  | Cà phê | 5 | 593 | 0,836 | [0,787; 0,891] |
  | Nhà thuốc | 6 | 708 | **0,798** | [0,737; 0,855] |
  | Vật liệu xây dựng | 6 | 552 | 0,882 | [0,835; 0,929] |
  | Không có ngành trong rulebook | 12 | 1107 | 0,848 | [0,795; 0,900] |
  | Chưa xác định (`biz_110`) | 1 | 40 | 0,950 | |

  - Nhà thuốc có tỉ lệ abstain cao nhất, khoảng 14%.
- **Phát hiện:** 3 ngành chỉ có trong rulebook public (Mẹ và bé, Sửa xe, Cửa hàng điện thoại) không có shop train nào. Đây chính là các shop public yếu nhất trong `FABLE_REVIEW` (`dev_011` 0,25; `dev_012` đến `dev_014` khoảng 0,5). Đây là bằng chứng trực tiếp cho rủi ro "rulebook lạ" và cho hướng A3.
- Phép đo 4 thật (ẩn định nghĩa của một ngành rồi đo) chỉ có nghĩa sau A3, khi hệ thống đọc rulebook lúc chạy. Hiện chưa có gì để ẩn.
- 21:40: chuẩn bị mục 7.
  - `google/gemma-4-12B-it` và các bản QAT không yêu cầu chấp nhận giấy phép, giấy phép Apache-2.0, tạo ngày 23/05 và 04/06/2026, trước mốc 01/10/2026. Bản fine-tune đang dùng tạo ngày 29/09/2026, cũng trước mốc.
  - `server_base/`: bản sao của máy chủ v2, chỉ đổi kho tải trọng số sang `google/gemma-4-12B-it` (`MODEL_REPO`), giữ tên API nên client không phải đổi. Cùng cách lượng tử NF4 nên so sánh công bằng.
  - `gen_push_server.py` thêm `--src`. Chạy thử `--dry-run` đạt.
  - Sẽ push sau khi phép đo 3 xong, rồi chạy `perturb_q500` (1550 câu) để so PA và độ bất biến với bản fine-tune (0,858 và 0,917).

## Lần thức 19 (21:53): **số nền phép đo 3** (nhiễu dữ liệu)

- 5 phép biến đổi × 2 shop (`biz_101`, `biz_108`), 795 câu, commit `7f02100`. **Tỉ lệ đáp án giữ nguyên chung: 0,980.**

  | Phép biến đổi | Giữ nguyên | PA | PA gốc |
  |---|---|---|---|
  | Chèn dòng tổng phụ | 0,994 | 0,925 | 0,931 |
  | Thêm dòng tiêu đề | 0,994 | 0,925 | 0,931 |
  | Đổi tên cột | 0,987 | 0,918 | 0,931 |
  | Đảo thứ tự cột | 0,994 | 0,925 | 0,931 |
  | **Tách sheet theo tháng** | **0,931** | **0,818** | 0,931 |

- Tách sheet theo tháng: cả 11 câu đổi kết quả thuộc `biz_108`. Có 10 câu từ đúng thành **sai**, đều là câu theo nhóm khách (sỉ, thân thiết).
  - Nguyên nhân: danh sách khách (`DanhSachKhachHang`) bị tách theo cột ngày sinh thành nhiều sheet.
  - Phép biến đổi này không thực tế, nhưng lộ ra lỗi thật: khi danh mục khách nằm rải nhiều sheet, nhóm khách của một phần khách bị mất mà **không abstain**, nên trả lời sai.
  - Ứng viên sửa: gộp các sheet danh mục cùng chữ ký cột; nếu dữ liệu nhóm khách thiếu nhiều thì cổng chất lượng phải abstain.
- 22:00: **sự cố hạn mức GPU.**
  - Mỗi lần push phiên bản mới, Kaggle mở phiên mới mà không dừng phiên cũ. Phiên bản 1 (từ 15:31) và phiên bản 2 (từ 16:20) đều đang chạy trên tài khoản 1. Cả hai địa chỉ đều trả 200.
  - Push máy chủ model gốc bị từ chối: "Maximum batch GPU session count of 2 reached".
  - Thống kê hạn mức tài khoản 1: đã dùng khoảng 44.369 giây (12,3 giờ), đang giữ chỗ khoảng 22.510 giây. Hạn mức làm mới lúc 2026-11-14 00:00 UTC.
  - API `CancelKernelSession` cần mã phiên, nhưng script push trước đây không lưu mã phiên và API không có cách liệt kê. **Cần người dùng dừng phiên bản 1 trên giao diện Kaggle.**
  - Đã sửa `gen_push_server.py`: lưu mã phiên vào `kaggle_sessions.jsonl`, không in khóa. Thêm `gen_cancel_session.py <account> <session_id>`.
  - Bài học cho quy trình: trước khi push phiên bản mới, phải dừng phiên cũ bằng mã phiên đã lưu.
- 12/11 04:34: câu hỏi cho người dùng chờ vài giờ mới có trả lời. Trong lúc đó cả phiên bản 1 và 2 đã tự tắt theo đồng hồ 11 giờ 50 phút, cả hai địa chỉ đều chết. `gen_after_v1_stop.py` đang push máy chủ model gốc (`server_base`) lên tài khoản 1, lần này chỉ có một phiên.
- **Hạn mức tài khoản 1:** ước tính đã dùng khoảng 23–24/30 giờ. Còn khoảng 6 giờ đến lần làm mới 14/11 00:00 UTC. Mục 7 tốn khoảng 1,5 giờ, sau đó chỉ còn khoảng 4,5 giờ cho phần còn lại của tuần. Cần ưu tiên: sau mục 7, chạy lại phép đo 1 một lần với cấu hình chốt rồi dừng.
- 04:41: máy chủ model gốc (phiên bản 3, tài khoản 1) online, chỉ một phiên. Bắt đầu mục 7: chạy `perturb_q500` (1550 câu) với model gốc, dùng lại bảng và chú thích của `train_full_2`, vào `q500_base/`. Như vậy phép so chỉ đo phần hiểu câu hỏi.
- 05:05: theo yêu cầu người dùng, đã tạo repo **riêng tư** https://github.com/NguyenQuocDat06-AI/datamate và push 6 nhánh: `main`, `arch/semantic-layer`, `arch/structural-ingestion`, `arch/data-operators`, `arch/rulebook-ir`, `docs/review-notes`.
  - `docs/review-notes` (commit `900e54a`, worktree `/home/ubuntu/agy-worktrees/docs`): tài liệu review, nhật ký 2 vòng lặp, script đo, máy chủ đã che giá trị bí mật, kèm README.
  - Kiểm tra trước khi push:
    - Quét toàn bộ lịch sử git với 11 giá trị bí mật: không có khóa thật. Hai kết quả khớp chỉ là tên model công khai.
    - Tên người dùng Kaggle trong `kernel-metadata.json` đã thay bằng `<kaggle-username>`.
  - Không đưa lên: dữ liệu đề, đáp án, bản ghi kết quả, bảng SQLite, `.env`, `connection_config.json`, địa chỉ máy chủ.
  - Từ nay commit trên nhánh làm việc có thể push lên `origin`, vì người dùng đã yêu cầu.

## Lần thức 20 (06:20): mục 7 (so model) và **mục 8: chốt số nền, điểm kiểm soát 1**

### Mục 7

Chạy `perturb_q500` (1550 câu), dùng lại bảng và chú thích do bản fine-tune tạo ra, nên chỉ so phần hiểu câu hỏi. Cả hai máy chủ đều gom lô, cache prefix, NF4.

| | Bản fine-tune SQL | `google/gemma-4-12B-it` |
|---|---|---|
| PA câu gốc (500) | 0,858 | **0,878** |
| Giữ nguyên chung | 0,917 | **0,930** |
| Viết tắt: giữ nguyên / PA | 0,912 / 0,792 | **0,942 / 0,830** |
| Bỏ dấu: giữ nguyên / PA | 0,900 / 0,773 | 0,897 / 0,776 |
| Đổi chỗ: giữ nguyên / PA | 0,971 / 0,901 | 0,971 / 0,912 |

- Trên 1494 câu đã so cặp: 43 câu tốt lên, 21 câu xấu đi. Số câu sai giảm từ 60 xuống 56.
- Mức tăng PA là +2,0 điểm, chưa đạt ngưỡng +3 của quy tắc. Độ bất biến có tăng. **Theo quy tắc, chưa đổi model.**
- Lợi thế khác của model gốc: giấy phép Apache-2.0 rõ ràng (bản fine-tune ghi "TODO"), và có sẵn bản QAT chính thức.
- Muốn quyết định đầy đủ phải chạy phép đo 1 với model gốc chú thích lại bảng: khoảng 3,2 giờ GPU.

### Bảng số nền (code `7f02100`, máy chủ v2, tắt alias)

| Phép đo | Kết quả |
|---|---|
| 1. Train-full chạy mới (3000 câu, 30 shop) | PA **0,842** [0,813; 0,871]. Ước tính khoảng 0,854 nếu tính trả lời dạng bảng. Thời gian thật 3,16 giờ |
| Tính tất định (277 câu chạy lại, cùng máy chủ) | 0 câu khác |
| 2. Nhiễu câu hỏi (500 câu gốc và 1050 biến thể) | Giữ nguyên 0,917. Viết tắt và bỏ dấu làm PA giảm 7–8 điểm |
| 3. Nhiễu dữ liệu (5 phép biến đổi × 2 shop) | Giữ nguyên 0,980. Riêng tách sheet theo tháng: 0,931, có 10 câu thành sai (danh mục khách rải nhiều sheet) |
| 4. Theo ngành | Cà phê 0,836; Nhà thuốc 0,798; Vật liệu xây dựng 0,882; không có ngành 0,848. Ba ngành chỉ có ở public thì không có shop train nào |
| Public 102 câu (bộ đo cũ, chỉ theo dõi) | 0,706 có alias, 0,725 tắt alias |

### Commit trên `arch/rulebook-ir` (đã push lên `origin`)

`cdd3a03` (lỗi model không âm thầm thay mặc định), `7979eaf` (`run_v2`), `0227155` (song song và chú thích vững), `7f02100` (trả lời dạng bảng).

### Hạn mức GPU

Tài khoản 1 còn khoảng 4–4,5 giờ đến lần làm mới 14/11 00:00 UTC. Tài khoản 0 và tài khoản trong `~/.kaggle` đã hết.

### Quyết định cần người dùng

1. **Model:** chạy phép đo 1 với model gốc (khoảng 3,2 giờ, gần hết hạn mức còn lại), hay giữ bản fine-tune?
2. **Thứ tự sau điểm kiểm soát:**
   - A1, IR tổ hợp, việc lớn;
   - hoặc trước hết các việc nhanh đã thấy lợi ích: nối giá trị hai bước, hồ sơ cột, tồn kho (43 câu), gom danh mục rải nhiều sheet, giảm thời gian gom giá trị (4823 giây).
