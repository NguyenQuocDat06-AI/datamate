# Vòng lặp thực hiện FABLE_REVIEW.md (hướng A)

Mỗi lần được đánh thức, làm **đúng một đơn vị việc** trong danh sách việc dưới đây, theo thứ tự. Ghi kết quả vào `LOOP_LOG_A.md`, rồi hẹn lần thức tiếp theo. Mọi đường dẫn tuyệt đối. Kế hoạch gốc nằm ở `/home/ubuntu/datamate_review_data/FABLE_REVIEW.md`, gồm mục 1.2 về các mốc A0–A5, mục 1.5 về bộ đo, mục 1.6 về lộ trình, mục 5 và mục 6.

Đây **không** phải vòng lặp sửa từng cụm lỗi. Mỗi đơn vị việc xây một khả năng tổng quát: bộ đo, lệnh chạy đầu-cuối, IR, bộ nạp bảng hoặc bộ biên dịch rulebook. Không đơn vị nào được nhắm vào câu hỏi cụ thể.

## Bối cảnh

- Code xuất phát: worktree `/home/ubuntu/agy-worktrees/arch8/source_test`, nhánh `arch/data-operators`, commit `e46113c`.
  - Vòng 8 (ánh xạ tên thợ) còn chưa commit. Xem `LOOP_LOG.md`.
- Worktree cho hướng A: `/home/ubuntu/agy-worktrees/arch9/source_test`, nhánh `arch/rulebook-ir`. Đơn vị việc 1 tạo nó. Từ đơn vị việc 2 trở đi, chỉ được sửa code ở đây.
- Tạo worktree mới cũng phải làm như các worktree trước:
  - symlink `source_test/data` trỏ vào dữ liệu của main;
  - thêm `source_test/data` vào `.git/info/exclude`.
- Commit dùng `git -c user.name=datamate-local -c user.email=datamate-local@localhost commit`. Thông điệp commit kết thúc bằng dòng `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Máy chủ model:
  - Kaggle T4×2, ping theo `active_endpoint.json`.
  - Client nằm ở `scripts/kaggle_12h_client.py`, có lệnh `--wait` để chờ máy chủ sẵn sàng.
  - Lần khởi chạy lại gần nhất: 11/11 lúc 04:50, hết hạn khoảng 16:50.
  - Mỗi tài khoản được khởi chạy lại tối đa một lần mỗi 12 giờ. Ngoại lệ: được khởi chạy lại để thử một thay đổi của máy chủ khi máy chủ đó không phục vụ phép đo nào. Ghi số giờ GPU đã dùng vào log.
  - Có hai tài khoản Kaggle trong `.env`. Tài khoản 0 dùng `KAGGLE_USERNAME`/`KAGGLE_KEY` và sắp hết hạn mức. Tài khoản 1 dùng `KAGGLE_USERNAME1`/`KAGGLE_KEY1`, đã có kernel cùng tên.
  - Khởi chạy bằng `gen_push_server.py --account N`. Script điền chuỗi giữ chỗ trong `server_batch/` mà không in. Kiểm tra trạng thái và đăng nhập bằng `gen_kaggle_auth.py`.
  - Lần khởi chạy tới dùng tài khoản 1. Tài khoản 0 chỉ dùng khi tài khoản 1 hết hạn mức.
  - Hai tài khoản dùng cùng token và kênh relay, nên `kaggle_12h_client.py --wait` vẫn tìm ra địa chỉ.
  - Gặp lỗi hết hạn mức GPU ở cả hai tài khoản thì không thử lại.
  - Không bao giờ mở hay in `.env`. Chỉ đọc qua `gen_kaggle_auth.py`.
- Script chấm cũ nằm trong `/home/ubuntu/datamate_review_data/`: `eval_e2e8.py`, `alias_off8.py`. Script mới đặt cạnh chúng, có tiền tố `gen_`.

## Danh sách việc (làm theo thứ tự, mỗi lần thức một mục)

Đánh dấu `[x]` trong `LOOP_LOG_A.md` khi xong. Một mục quá lớn thì tách thành mục con, ghi vào log, và làm mục con đầu tiên.

### Mốc A0: bộ đo và lệnh chạy đầu-cuối

1. **Tính tất định và chốt vòng 8.**
   - Lưu kết quả phân loại dòng (`annotate_line_kinds`) vào cache trong `alias_off8.py`, như `groups/`.
   - Mọi lần gọi model trong pipeline dùng nhiệt độ 0. Kiểm tra trong `bizreason/llm`.
   - Dựng `dev_013` hai lần, số dòng phải bằng nhau. Sau đó chấm lại tắt alias.
   - Nếu không giảm so với 0,706 thì commit vòng 8 lên `arch/data-operators`. Nếu giảm thì `git restore`.
   - Tạo worktree `arch9` từ HEAD kết quả.
2. **Lệnh chạy đầu-cuối.**
   - Nối `SemanticSolver` vào một lệnh (ví dụ `bizreason/run_v2.py` hoặc một lệnh con của `cli.py`).
   - Lệnh đọc thư mục dữ liệu và rulebook của bộ test, rồi lần lượt: ingestion, chú thích, dựng bảng, hiểu câu hỏi, trả lời, ghi `submission.jsonl` theo định dạng của bài thi.
   - Bấm giờ từng bước. Ghi log thời gian theo từng shop và từng câu.
   - Không dùng cache nào ngoài cache do chính lệnh tạo ra trong thư mục đầu ra.
3. **Phép đo 1: train-full, chạy mới hoàn toàn.**
   - Script `gen_train_full.py` chạy toàn bộ câu train, tắt alias, đọc file bằng code worktree, hiểu câu hỏi không qua cache.
   - Script tự loại các id trong `acceptance_ids.json` mà không in hay mở nội dung.
   - Báo PA kèm khoảng tin cậy bootstrap theo shop.
   - Bấm giờ để ngoại suy cho private, giả định 30 shop và 3000 câu. Ngưỡng là 4,5 giờ.
   - Chạy hai lần trên 3 shop để xác nhận kết quả giống nhau từng câu.
4. **Phép đo 2: bất biến dưới nhiễu câu hỏi.**
   - Mỗi câu thêm 3 biến thể, sinh bằng code, không dùng model: bỏ dấu, chèn viết tắt từ một bảng trong `vocabulary.py`, đổi chỗ cụm thời gian.
   - Đo tỉ lệ đáp án giữ nguyên.
   - Dùng mẫu khoảng 500 câu train để vừa thời gian.
5. **Phép đo 3: bất biến dưới nhiễu dữ liệu.**
   - Biến đổi file của shop bằng code: đổi tên cột theo bảng đồng nghĩa, đảo thứ tự cột, tách tháng thành sheet, thêm dòng tiêu đề và dòng tổng phụ.
   - Đáp án chuẩn giữ nguyên. Đo trên 6–8 shop train.
6. **Phép đo 4: bỏ một ngành.**
   - Liệt kê các ngành theo rulebook.
   - Với mỗi ngành, ẩn định nghĩa và ví dụ của ngành đó, rồi đo trên các shop của ngành đó.
   - Trước A3 thì phép đo này chỉ là số nền. Ghi rõ cách ẩn.
7. **So model.**
   - So `google/gemma-4-12B-it` gốc với bản fine-tune SQL đang dùng, trên 150 câu có nhãn (`spike_a2_labels.jsonl`) và phép đo 2.
   - Bật giải mã ràng buộc JSON nếu máy chủ hỗ trợ.
   - Đổi model chỉ khi độ chính xác intent tăng ít nhất 3 điểm và độ bất biến tăng.
   - Ghi giấy phép và ngày phát hành của cả hai model, đối chiếu luật "phát hành trước 01/10/2026".
   - Đổi model nghĩa là sửa `run_server_12h.py` và khởi chạy lại kernel. Tính vào giới hạn mỗi 12 giờ.
8. **Chốt số nền.**
   - Ghi bảng 5 phép đo vào `LOOP_LOG_A.md`.
   - **Dừng vòng lặp để người dùng xem.** Đây là điểm kiểm soát 1.

### Mốc A1: IR tổ hợp (chỉ làm sau khi người dùng xác nhận điểm kiểm soát 1)

9. **Thiết kế IR trên giấy.**
   - Viết `IR_DESIGN.md`: nguồn sự kiện, phép gộp, cột, bộ lọc, chiều nhóm, điều kiện trên nhóm, sắp xếp, giới hạn, hậu xử lý, hàm dựng sẵn có tên.
   - Viết tay định nghĩa IR cho mọi dòng của `rulebook.md` và `rulebook_public.md`, rồi đếm tỉ lệ viết được bằng tổ hợp.
   - Nếu dưới 70% thì dừng vòng lặp và báo người dùng.
10. **A1.**
    - Chuyển các đại lượng hiện có thành định nghĩa dữ liệu, chạy qua một compiler IR tất định.
    - Bỏ các nhánh `if m == …` theo đại lượng.
    - Điều kiện qua: phép đo 1 lệch không quá 1% số câu so với số nền, và các phép đo 2–4 không giảm.
    - Mục này lớn, tách thành mục con theo từng nhóm đại lượng. Giữ song song đường cũ cho đến khi đường mới đạt.
11. **Điểm kiểm soát 2.** Dừng vòng lặp để người dùng quyết định A2 (bộ nạp bảng sự kiện tổng quát).

## Các bước của một lần thức

1. **Kiểm tra môi trường.**
   - Ổ đĩa còn trên 8 GB. Nếu không, xóa thư mục `sem*` hoặc `*_r*` cũ do vòng lặp tạo. Không xóa file kết quả `*.jsonl`.
   - Kiểm tra máy chủ model.
   - Kiểm tra `git status` của worktree.
2. **Đọc `LOOP_LOG_A.md`, chọn mục chưa xong đầu tiên.** Nếu một tác vụ nền của mục đang chạy thì kiểm tra tác vụ đó.
   - Trong lúc phép đo dài đang chạy, được chuẩn bị code cho mục kế tiếp nếu việc đó không cần gọi model và không sửa file mà phép đo đang dùng.
   - Ví dụ: bộ sinh nhiễu, script đo. Code của phép đo đã được nạp lúc khởi động, nhưng vẫn chỉ tạo file mới, không sửa file cũ.
   - Không chạy phép đo thứ hai cùng lúc, vì máy chủ model chỉ có một.
3. **Thiết kế ngắn ngay trong session chính:** đầu vào, đầu ra, file nào thay đổi, test nào, phép đo nào chứng minh xong.
4. **Thực hiện.**
   - Giao tối đa 3 agent Haiku 5.5, mỗi agent một nhóm file riêng, kèm mô tả rất chi tiết.
   - Agent không commit, không gọi model thật, không mở `.env` hay `connection_config.json`, không tạo file ngoài worktree.
   - Session chính rà lại diff trước khi chấp nhận.
   - Việc chạy đo do session chính chạy nền.
5. **Kiểm.**
   - Toàn bộ test pass.
   - Test chặn hardcode pass, chạy với `DATASET_CACHE_ROOT=/home/ubuntu/datamate/source_test`.
   - Nếu mục thay đổi hành vi trả lời: chạy các phép đo đã có. Phép đo 1–4 không được giảm. Public 102 câu chỉ để theo dõi.
6. **Commit** lên nhánh của worktree khi mục đạt.
7. **Ghi log:** mục, giờ, việc đã làm, số đo trước và sau, quyết định, commit, điều học được.
8. **Hẹn lần thức sau** bằng ScheduleWakeup. Nếu có tác vụ nền thì đặt chờ dài làm dự phòng.

## Quy tắc bắt buộc

- **Không vá theo câu.** Không mở từng câu public để tìm lỗi. Lỗi tìm trên train-full và dữ liệu nhiễu.
  - Mỗi thay đổi hành vi phải nêu trước họ câu hoặc dạng dữ liệu nó nhắm tới, và số câu dự kiến đổi trạng thái trên phép đo 1.
  - Không làm thay đổi nào dự kiến gỡ dưới 5 câu.
- **Không hardcode** tên bảng, tên cột, giá trị dữ liệu, mã shop, ngày cụ thể hay cụm từ câu hỏi. Từ vựng chỉ đặt trong `vocabulary.py` hoặc trong định nghĩa dữ liệu sinh từ rulebook.
  - Mở rộng test chặn hardcode sang `bizreason/data/` khi có dịp.
- **Không dùng tập nghiệm thu** (`acceptance_ids.json`, `accept*/`). Script chỉ được loại các id đó ra mà không in. Người dùng quyết định khi nào đo.
- **Không tin số từ intent lưu sẵn** sau khi sửa prompt hay model. Phép đo 1 luôn chạy mới.
- **Không sửa `main`**, không sửa `/home/ubuntu/datamate/source_test`, không push, không merge.
- Không in khóa hay token. Không gửi dữ liệu dự án ra ngoài, trừ máy chủ model của dự án. Được tìm kiếm web về model và kỹ thuật.
- Không nới ngưỡng `quality.py` hay cổng abstain để tăng điểm.
- Dừng tiến trình bằng PID cụ thể, không dùng `pkill -f`.
- Dựng bảng ra file tạm rồi mới đổi tên.

## Điều kiện dừng

Dừng vòng lặp (ScheduleWakeup với `stop: true`) và viết tổng kết ở cuối `LOOP_LOG_A.md` khi gặp một trong các trường hợp:

- Đến điểm kiểm soát 1 (mục 8), điểm kiểm soát 2 (mục 11), hoặc mục 9 cho dưới 70%.
- Một mục thất bại hai lần liên tiếp, hoặc test hỏng không rõ nguyên nhân.
- Hết hạn mức GPU mà việc còn lại đều cần model, hoặc ổ đĩa không giải phóng được.
- Đã chạy 16 lần thức kể từ khi bắt đầu.

Tổng kết gồm: các mục đã xong, số đo nền và số đo hiện tại, các commit, những gì học được, và việc nên làm tiếp.
