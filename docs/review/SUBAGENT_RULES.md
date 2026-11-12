# Quy định sử dụng sub agent bằng `agy` (gemini-3.8-flash-high)

Tài liệu này dành cho **người điều phối** (Claude Code hoặc người vận hành). Luật dành cho chính sub agent nằm ở `source_test/AGENTS.md`.

Kiểm chứng lần cuối: 2026-11-10, `agy` 1.5.3, commit gốc `bf565b3`. Khi nâng cấp `agy` hoặc đổi máy, phải kiểm chứng lại mục 2.

## 1. Nguyên tắc

1. **Worker là người thực thi, không phải người quyết định.** Điều phối viên đọc code, tìm nguyên nhân, chốt cách sửa rồi mới giao việc.
2. **Không tin báo cáo của worker.** Chỉ tin `git diff` và kết quả test do điều phối viên tự chạy.
3. **Luật viết bằng chữ không phải hàng rào.** Mỗi rủi ro quan trọng phải có một lớp chặn kỹ thuật hoặc một bước kiểm tra sau khi chạy.
4. **Việc nhỏ, rõ, kiểm tra được.** Nếu không viết được tiêu chí đạt bằng một lệnh hoặc một hành vi cụ thể thì chưa được giao.
5. **Chỉ dùng `gemini-3.8-flash-high`.** Không tự đổi sang model khác khi lỗi hoặc hết quota.

## 2. Hành vi đã kiểm chứng

| Hành vi đo được | Hệ quả bắt buộc |
|---|---|
| Khi công cụ bị từ chối, `agy` vẫn trả exit 0, `status: SUCCESS`, `response` rỗng | Không dùng exit code hay `status` để kết luận thành công. Phải đọc `denied_actions` |
| `denied_actions` xuất hiện trong sự kiện `result` của `--output-format stream-json` | Luôn chạy bằng `stream-json` |
| Một lệnh shell bị từ chối làm **kết thúc cả lượt** | Allowlist phải đủ dùng, và brief phải cấm các lệnh ngoài allowlist |
| Model có phản xạ chạy `pwd && ls -la`, và tự dùng shell để sửa file | Cho phép `pwd`, `ls`. Bắt buộc dùng công cụ sửa file. Khi có chỉ dẫn này, 2/2 lần sửa thành công |
| Không tìm thấy file được giao, model tự lục ra thư mục cha bằng `find ..`, đọc cả file log ở thư mục cha | Worker phải dừng và báo `BI_CHAN`. Thư mục log phải bị chặn đọc |
| Ở `accept-edits`, worker **ghi được file ra ngoài workspace** | Bắt buộc có luật `deny`, và kiểm tra repo chính sau khi chạy |
| `permissions.deny` với `write_file(<đường dẫn>)` và `read_file(<đường dẫn>)` có hiệu lực, kể cả theo thư mục | Dùng làm hàng rào chính cho đường dẫn nhạy cảm |
| `--mode auto` và terminal sandbox không dùng được trên máy này, `auto` lùi về chế độ mặc định | Không dùng `auto`, `--sandbox`. Dùng `accept-edits` với allowlist |
| `command(<tiền tố>)` khớp theo tiền tố. Lệnh ghép `pwd && touch x` bị chặn, `python3 -c` bị chặn khi chỉ cho `python3 -m pytest` | Chỉ cho phép đúng lệnh cần, không cho `python`, `bash`, `sed` |
| `AGENTS.md` và `GEMINI.md` ở thư mục chạy được nạp tự động. Trong một bài thử, `AGENTS.md` ở thư mục cha không được nạp | Worker phải chạy với thư mục hiện tại là `source_test/` |
| Bộ test chạy được trong worktree với `data` là symlink chỉ đọc: 450 passed trong 11 giây, không cần `.env` | Mỗi worker một worktree, `data` dùng symlink |

**Chưa được kiểm chứng:**

- Luật `deny` theo đường dẫn có áp dụng khi đi qua symlink không, nên `data` được chặn thêm bằng `chmod`.
- Quota của flash-high với tác vụ dài. Mới chỉ thử các lượt ngắn, tối đa 8 tiến trình cùng lúc.
- Hành vi với lượt chạy nhiều bước trên code thật.

**Giới hạn còn lại:** `pytest` chạy code Python với quyền của user, nên luật `deny` không áp dụng cho code mà test thực thi. Mọi test mới do worker viết phải được đọc trước khi điều phối viên tự chạy.

## 3. Được giao và không được giao

Các con số dưới đây là giới hạn do quy định này đặt ra.

**Một worker, một nhiệm vụ:**

- Một mục tiêu duy nhất, sửa tối đa 3 file, diff tối đa khoảng 150 dòng.
- File đích và hàm đích đã được điều phối viên chỉ đích danh.
- Có một lệnh test cụ thể chứng minh kết quả.

**Phù hợp để giao:**

- Sửa lỗi đã xác định nguyên nhân, kèm vị trí chính xác.
- Thêm test mới theo đặc tả cho trước.
- Thay đổi cơ học lặp lại, ví dụ đổi tên một tham số trong phạm vi đã liệt kê.
- Khảo sát chỉ đọc để liệt kê vị trí, sau đó điều phối viên phải xác minh.

**Không được giao, điều phối viên tự làm:**

- Tìm nguyên nhân gốc của lỗi chưa rõ, thiết kế, chọn kiến trúc.
- Refactor nhiều module, đổi `bizreason/contracts.py` hoặc giao diện dùng chung giữa các module.
- Sửa logic chấm điểm trong `bizreason/evaluation/`.
- Viết lại nguyên cả file. Với `sql_healers.py` (3.813 dòng), `plan_input.py` (2.212 dòng), `planning.py` (1.380 dòng), chỉ được sửa trong đúng hàm đã nêu.
- Thay đổi prompt hoặc logic quyết định mà không có test tái hiện.
- Đổi dependency, cấu hình dự án, chạy pipeline, gọi API hoặc chạy Kaggle.

**Vùng cấm tuyệt đối:** `.env`, `source_test/data/`, `source_test/artifacts/`, `results/`, `logs/`, `venv/`, `source_test_backup_task10/`, `BAO_CAO_GIAI_1000_CAU_DATAMATE_1.00000.md`, `run_overnight.sh`, `replay_1000.py`, `pyproject.toml`.

## 4. Quy tắc song song

1. Tối đa 4 worker cùng lúc. Chỉ tăng khi người dùng đồng ý.
2. **Tập file được sửa của các worker không được giao nhau**, kể cả file test.
3. Mỗi worker có worktree và nhánh riêng. Không worker nào làm trực tiếp trong `/home/ubuntu/datamate`.
4. Việc phụ thuộc nhau phải chạy tuần tự. Worker sau chỉ bắt đầu khi kết quả worker trước đã được duyệt và gộp.
5. Không chạy worker khi pipeline, `run_overnight.sh` hoặc `replay_1000.py` đang chạy.
6. Không chia nhỏ một thay đổi có tính nguyên khối thành nhiều worker. Việc đó giao cho một worker hoặc điều phối viên tự làm.

## 5. Quy trình

### Bước 1. Kiểm tra trước khi chạy

```bash
ROOT=/home/ubuntu/datamate
git -C "$ROOT" status --porcelain                 # phải rỗng
git -C "$ROOT" ls-files --error-unmatch source_test/AGENTS.md   # phải đã commit
grep -qx source_test/data "$ROOT/.git/info/exclude" || echo source_test/data >> "$ROOT/.git/info/exclude"
pgrep -af 'bizreason|run_overnight|replay_1000'   # phải rỗng
cd "$ROOT/source_test" && "$ROOT/venv/bin/python" -m pytest -q --no-header -p no:cacheprovider
```

Ghi lại số test pass làm mốc so sánh. Nếu test đang đỏ thì không chạy worker.

Khóa dữ liệu. Worker chạy bằng cùng user nên chỉ cần bỏ quyền ghi của chủ sở hữu:

```bash
find "$ROOT/source_test/data" ! -perm -u+w | wc -l   # phải bằng 0, nếu không thì dừng lại xem
chmod -R u-w "$ROOT/source_test/data"
```

Bắt buộc mở khóa ở bước 6, kể cả khi worker lỗi.

### Bước 2. Tạo chỗ làm việc cho từng worker

Giữ nguyên các đường dẫn này. `agy-worktrees` không được bắt đầu bằng `/home/ubuntu/datamate`, vì luật `deny` có thể so khớp theo tiền tố chuỗi.

```bash
ID=<ma-nhiem-vu>
WT=/home/ubuntu/agy-worktrees/$ID
RUN=/home/ubuntu/agy-runs/$ID
mkdir -p "$RUN"
git -C "$ROOT" worktree add -q -b "agy/$ID" "$WT" main
ln -s "$ROOT/source_test/data" "$WT/source_test/data"
```

### Bước 3. Tạo cấu hình quyền `"$RUN/settings.json"`

Thay `<WT>` bằng đường dẫn thật. Thêm một cặp `write_file` và `read_file` cho worktree của mỗi worker khác đang chạy.

```json
{
  "permissions": {
    "allow": [
      "command(pwd)",
      "command(ls)",
      "command(git status)",
      "command(git diff)",
      "command(/home/ubuntu/datamate/venv/bin/python -m pytest)"
    ],
    "deny": [
      "write_file(/home/ubuntu/datamate)",
      "write_file(/home/ubuntu/agy-runs)",
      "write_file(/home/ubuntu/.gemini)",
      "write_file(/home/ubuntu/.ssh)",
      "write_file(/home/ubuntu/.claude)",
      "write_file(<WT>/source_test/data)",
      "write_file(<WT>/source_test/artifacts)",
      "write_file(<WT>/source_test/pyproject.toml)",
      "write_file(<WT>/source_test/AGENTS.md)",
      "write_file(<WT>/results)",
      "write_file(<WT>/source_test_backup_task10)",
      "read_file(/home/ubuntu/datamate/source_test/.env)",
      "read_file(/home/ubuntu/datamate/source_test/artifacts)",
      "read_file(/home/ubuntu/agy-runs)",
      "read_file(/home/ubuntu/.gemini)",
      "read_file(/home/ubuntu/.ssh)",
      "read_file(/home/ubuntu/.claude)"
    ]
  }
}
```

Không thêm vào allowlist: `cat`, `grep`, `find`, `sed`, `python`, `bash`, `sh`, `rm`, `mv`, `cp`, `curl`, `git commit`, `git checkout`, `git reset`, `pip`, `chmod`, hoặc `command(*)`. Các lệnh đọc như `cat` và `grep` có thể đọc file bí mật mà không qua luật `read_file`. Worker đã có công cụ đọc và tìm kiếm riêng.

Không bao giờ dùng `--dangerously-skip-permissions`.

### Bước 4. Viết brief `"$RUN/brief.md"` theo mẫu ở mục 6

### Bước 5. Chạy song song

Mỗi worker là một lệnh chạy nền riêng:

```bash
cd "$WT/source_test" && timeout 960 agy -p "$(cat "$RUN/brief.md")" \
  --model gemini-3.8-flash-high --mode accept-edits \
  --settings "$(cat "$RUN/settings.json")" \
  --print-timeout 900s --output-format stream-json \
  < /dev/null > "$RUN/out.jsonl" 2> "$RUN/err.txt"
```

Trong lúc chờ không sửa gì trong repo chính.

### Bước 6. Mở khóa và nghiệm thu

Sau khi **tất cả** worker kết thúc, mở khóa trước:

```bash
chmod -R u+w "$ROOT/source_test/data"
```

Sau đó kiểm tra lần lượt. Một mục không đạt thì worker đó không đạt.

1. **Kết thúc thật:** có sự kiện `result`, `status` là `SUCCESS`, `denied_actions` rỗng, `response` không rỗng, `err.txt` rỗng, `TRANG_THAI: HOAN_THANH`.
2. **Không thoát ra ngoài:** `git -C "$ROOT" status --porcelain` rỗng.
3. **Công cụ đã dùng:** rà mọi `tool_name` và `CommandLine` trong `out.jsonl`. Từ chối nếu thấy web, trình duyệt, `invoke_subagent`, `define_subagent`, `schedule`, `send_message` hoặc lệnh ngoài allowlist.
4. **Đúng phạm vi:** `git -C "$WT" status --porcelain` và `git -C "$WT" diff --stat` chỉ gồm file được phép, trong giới hạn kích thước.
5. **Đọc từng dòng diff**, đối chiếu danh sách dấu hiệu ở mục 7.
6. **Tự chạy test** trong worktree, sau khi đã đọc mọi test mới:
   ```bash
   cd "$WT/source_test" && /home/ubuntu/datamate/venv/bin/python -m pytest -q --no-header -p no:cacheprovider
   ```
   Tất cả phải pass. Số test không được giảm so với mốc.
7. **Đối chiếu báo cáo:** mỗi tuyên bố của worker phải khớp với diff và kết quả test.

### Bước 7. Gộp và dọn dẹp

1. Commit và gộp vào `main` chỉ khi người dùng đã yêu cầu. Không tự push.
2. Gộp từng nhánh một, chạy lại toàn bộ test sau mỗi lần gộp.
3. Dọn dẹp sau khi đã gộp hoặc đã quyết định bỏ:
   ```bash
   git -C "$ROOT" worktree remove --force "$WT"
   git -C "$ROOT" branch -D "agy/$ID"
   ```
4. Trước khi dọn, xem lại worktree để không mất phần việc còn giá trị. Giữ `$RUN` cho đến khi xong đợt.

### Khi có sự cố

| Tình huống | Cách xử lý |
|---|---|
| `denied_actions` không rỗng hoặc `response` rỗng | Không đạt. Xem lệnh nào bị chặn. Sửa brief, chỉ thêm quyền nếu lệnh đó thực sự an toàn |
| Lỗi 429, `RESOURCE_EXHAUSTED`, exit 3 | Dừng cả đợt, báo người dùng, không thử lại hàng loạt, không đổi model |
| Hết thời gian (exit 124) | Không đạt. Việc quá lớn, chia nhỏ lại |
| `BI_CHAN` | Đọc lý do. Điều phối viên tự tìm thông tin còn thiếu rồi viết lại brief |
| Repo chính có thay đổi | Dừng mọi worker, báo người dùng, xem diff trước khi khôi phục bất cứ thứ gì |
| Test đỏ hoặc diff sai | Tiếp tục bằng `--conversation <ID>` kèm lỗi cụ thể |
| Hỏng 2 lần cho cùng một việc | Không thử lần thứ ba. Bỏ nhánh, điều phối viên tự làm hoặc báo người dùng |

## 6. Mẫu brief

Worker không biết gì về cuộc trò chuyện. Brief phải đầy đủ đến mức không cần đoán.

```markdown
# Nhiệm vụ <ID>

## Mục tiêu (một câu)
<Hành vi sau khi sửa.>

## Bối cảnh và nguyên nhân đã xác định
<Lỗi gì, tái hiện thế nào, vì sao xảy ra.>

## File được đọc
- <đường dẫn tương đối từ source_test/>

## File được sửa (ngoài danh sách này là cấm)
- <đường dẫn>: hàm `<tên>`, khoảng dòng <a>-<b>

## Thay đổi cần làm, theo thứ tự
1. <Thay đoạn hiện tại:>
   <code hiện tại>
   <bằng logic:>
   <đặc tả hoặc code mới>

## Không được làm
- <Các hướng sửa sai dễ mắc phải trong đúng việc này.>
- Không sửa test hiện có. Không refactor, không đổi định dạng, không thêm dependency.

## Tiêu chí đạt
- <Hành vi cụ thể, input và output kỳ vọng.>

## Lệnh kiểm tra (chạy đúng nguyên văn)
/home/ubuntu/datamate/venv/bin/python -m pytest <đường dẫn test> -q --no-header -p no:cacheprovider

## Khi gặp bất ngờ
Dừng và báo `TRANG_THAI: BI_CHAN`. Không tự mở rộng phạm vi.
```

Brief không đạt: "Cải thiện xử lý ngày tháng trong temporal.py và sửa các lỗi liên quan."

Brief đạt: nêu đúng hàm, đúng dòng, đoạn code hiện tại, input gây lỗi, output kỳ vọng, và tên test sẽ chạy.

## 7. Dấu hiệu worker làm sai

Thấy một trong các dấu hiệu sau thì từ chối phần việc:

- Sửa, xóa, bỏ qua test hoặc nới lỏng assert để test xanh, thêm `skip`, `xfail`.
- Hard-code đáp án, ID câu hỏi, `biz_*`, `dev_*` hoặc giá trị riêng của bộ dữ liệu.
- `try/except` nuốt lỗi, thêm giá trị mặc định che lỗi.
- Gọi hàm, tham số, thuộc tính, thư viện không tồn tại.
- Xóa code hoặc comment không được yêu cầu, đổi định dạng hàng loạt.
- Refactor "nhân tiện", đổi chữ ký hàm, đổi kiểu dữ liệu trả về.
- Thêm dependency, file mới, script, tài liệu không được yêu cầu.
- Báo đã chạy test nhưng `out.jsonl` không có lệnh test tương ứng.
- Báo hoàn thành nhưng diff trống hoặc chỉ làm một phần.
