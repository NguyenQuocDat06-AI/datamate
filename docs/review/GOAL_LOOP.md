# Vòng lặp tự động qua đêm: debug → nghiên cứu → tìm kiếm → sửa → chấm → debug

Mỗi lần được đánh thức, làm **đúng một vòng** theo các bước dưới đây, ghi kết quả vào `LOOP_LOG.md`, rồi hẹn lần thức tiếp theo. Mọi đường dẫn tuyệt đối.

## Bối cảnh

- Kiến trúc: V2 semantic pipeline, trong `bizreason/semantic/` và các thay đổi ở `bizreason/data/`.
- Worktree làm việc: `/home/ubuntu/agy-worktrees/arch8/source_test`, nhánh `arch/data-operators`. Đây là nơi duy nhất được sửa code. Mỗi vòng tạo commit mới trên nhánh này khi thay đổi được giữ.
- Mốc so sánh hiện có:
  - Đợt 6 có alias, 102 câu public: PA 0,618 (73 đúng, 10 sai, 19 abstain). 300 câu train: PA 0,860 (264 / 6 / 30).
  - Đợt 6 tắt alias, 102 câu public: PA 0,461 (64 / 17 / 21).
  - Nghiệm thu 200 câu ở commit `6c7059e`: PA 0,490.
- Công cụ chấm, đều nằm trong `/home/ubuntu/datamate_review_data/`:
  - `eval_e2e.py`: chấm 300 câu train và 102 câu public. Hằng số `WT`, `SEM`, `RUN` ở đầu file chọn worktree và thư mục đầu ra. Phần hiểu câu hỏi được cache trong `RUN/*_intents.jsonl`. Sao chép từ `e2e5/` để không tốn GPU, trừ khi vòng này sửa `intent.py`.
  - `alias_off.py`: chấm 102 câu public với alias đã tắt. Hằng số `WT` chọn worktree. Kết quả trong `alias_off_run/records.jsonl`. Sao chép bản cũ sang tên khác trước khi chạy.
  - Ghi chú tiếng Việt về từng đợt và các script phụ (`prep_wave*.py`, `refresh_status.py`) để tham khảo cách gọi model cho gán nghĩa, trạng thái, gom giá trị.
- Kết quả gom giá trị phân loại của đợt 8 chưa được tạo và chưa được nối vào `eval_e2e.py`. Đây là việc đầu tiên của vòng 1.

## Các bước của một vòng

1. **Kiểm tra môi trường.**
   - Ổ đĩa còn trên 8 GB. Nếu không, xóa các thư mục `semN` cũ nhất do vòng lặp tạo, không xóa `e2e*`, `alias_off_run/records*`, `accept*`, `*_records.jsonl`.
   - Máy chủ model online: ping `active_endpoint.json`. Nếu tắt, khởi chạy lại kernel Kaggle **tối đa một lần mỗi đêm**. Nếu gặp lỗi hết hạn mức GPU, không thử lại, chỉ làm các việc không cần model.
   - `git status` của worktree sạch, hoặc chỉ có thay đổi của vòng đang dở được ghi trong log.
2. **Debug.** Đọc kết quả chấm mới nhất. Liệt kê câu sai và abstain theo nhóm nguyên nhân, kèm số câu. Chọn **một** nhóm có số câu lớn nhất mà sửa được tổng quát. Ưu tiên câu sai hơn câu abstain.
3. **Nghiên cứu.** Giao 1–2 agent Haiku 5.5 truy nguyên các câu trong nhóm đó, chỉ đọc. Agent dùng SQLite `mode=ro`, tái tạo đáp án, và chỉ ra chính xác tầng gây lỗi.
4. **Tìm kiếm.** Chỉ khi cần năng lực mới: tìm mã nguồn mở hoặc model mở đã giải bài này. Ghi giấy phép, ngày phát hành, số tham số. Tổng tham số mọi model không quá 15 tỷ, model phát hành trước 01/10/2026. Không cài gói từ nguồn không rõ.
5. **Sửa.** Giao tối đa 3 agent Haiku 5.5 song song, mỗi agent một nhóm file riêng trong worktree, có test riêng. Agent không được commit, không gọi model thật, không mở `.env`.
6. **Chấm.**
   - Chạy toàn bộ test. Chạy test chặn hardcode với `DATASET_CACHE_ROOT=/home/ubuntu/datamate/source_test`.
   - Dựng lại bảng và chấm cả hai chế độ, có alias và tắt alias.
7. **Quyết định.**
   - **Giữ** khi: test pass, PA không giảm ở cả ba phép đo (train, public có alias, public tắt alias), và số câu sai trên public có alias không tăng. Commit lên nhánh `arch/data-operators`.
   - **Bỏ** khi không đạt: `git restore` các file đã sửa, ghi lý do vào log.
8. **Ghi log** vào `LOOP_LOG.md`: số vòng, giờ, nhóm lỗi, nguyên nhân, thay đổi, điểm trước và sau ở ba phép đo, quyết định, commit.
9. **Hẹn vòng sau** bằng ScheduleWakeup. Nếu có tác vụ nền đang chạy, đặt thời gian chờ dài làm dự phòng, vì thông báo hoàn thành sẽ tự đánh thức.

## Quy tắc bắt buộc

- **Không hardcode** tên bảng, tên cột, giá trị dữ liệu, mã doanh nghiệp, ngày cụ thể hay cụm từ câu hỏi. Từ vựng nghiệp vụ hoặc ngôn ngữ chỉ đặt trong `vocabulary.py`.
- **Không dùng tập nghiệm thu 200 câu** (`acceptance_ids.json`) trong vòng lặp, kể cả để xem từng câu. Chỉ người dùng quyết định khi nào đo lại.
- **Không sửa `main`**, không sửa `/home/ubuntu/datamate/source_test`, không push, không merge.
- Không in khóa, token. Không gửi dữ liệu dự án ra dịch vụ bên ngoài ngoài máy chủ model của dự án.
- Không chỉnh ngưỡng của `quality.py` để làm đẹp điểm.
- Một câu sai bị trừ điểm: khi không chắc thì abstain.

## Điều kiện dừng

Dừng vòng lặp (ScheduleWakeup với `stop: true`) và viết phần tổng kết ở cuối `LOOP_LOG.md` khi gặp một trong các điều kiện:

- 3 vòng liên tiếp không giữ được thay đổi nào.
- Đã chạy 8 vòng.
- Đã quá 07:30 giờ máy.
- Gặp lỗi không tự khắc phục được, ví dụ hết hạn mức GPU mà việc còn lại đều cần model, ổ đĩa không giải phóng được, hoặc test hỏng không rõ nguyên nhân.

Phần tổng kết gồm: điểm đầu và cuối ở ba phép đo, các commit đã giữ, các hướng đã thử và bỏ, và việc nên làm tiếp.
