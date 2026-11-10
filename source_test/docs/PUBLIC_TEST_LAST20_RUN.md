# Chạy 20 câu còn lại để đủ nhóm 50

**Đã hoàn tất lúc 18:13:43 ngày 03/11/2026, giờ Việt Nam: 20/20 prediction, gồm 18 câu có giá trị và 2 abstain.** Đây là inference mới qua API. [Đối chiếu độc lập sau đó](PUBLIC_TEST_LAST20_REVIEW.md): **17 khớp, 1 sai khác, 2 abstain dù dữ liệu đủ để tính**. Câu SH Mode phải là 27 biển số thay vì 30 chuỗi tên xe; hai abstain có giá trị đối chiếu 400.700.000 và `[8000, 4000]`.

- [Bảng đầy đủ 20 câu và prediction](PUBLIC_TEST_LAST20_PREDICTIONS.md).
- [JSONL của 20 câu mới](../result/public_test_last20_trammay.jsonl).
- [JSONL ghép đủ 50 ID](../result/public_test_50_collected.jsonl): 30 prediction baseline được giữ nguyên + 20 prediction mới; có 6 abstain tổng cộng. File này tập hợp nhiều phiên bản solver, không phải 50 câu đều chạy lại bằng bản mới.
- [Nguồn từng ID và hash file](../artifacts/public_last20_trammay_extended/collection_manifest.json).
- [Kiểm tra hoàn tất](../artifacts/public_last20_trammay_extended/completion_checks.json): đủ 50 ID chính xác, không trùng; kết quả khớp các batch đầu vào; đủ trace/checkpoint của 20 câu; solver/runner không đổi; 94 hash nguồn public được kiểm tra lại và không thay đổi. Pyflakes qua đối với hai script tổng hợp/báo cáo mới.

Hai câu abstain cần kiểm tra tiếp:

| ID | Câu hỏi | Quan sát từ trace, chưa phải chấm đúng/sai |
|---|---|---|
| q_0594583b3d46 | Dư nợ trả góp đến hết 18/10/2025, kể cả kỳ chưa tới hạn | Candidate gặp SQL `execution_error`, diagnostic `near "AS": syntax error`; sau lượt repair/probe vẫn không có candidate đủ điều kiện để trả giá trị. |
| q_0a9819711554 | Lê Sơn Trung: nợ thêm và trả nợ tháng 7/2026 | Lần đầu sai kiểu `lookup_normalized` và `output.column_types` trong QueryPlan; lần tiếp theo bị `INVALID_ENTITY_CONSTRAINT`. |

Không đổi các kết quả abstain thành số hoặc kết luận dữ liệu không có đáp án. Không sửa solver theo các câu mới trong đợt chạy này.

Ngày 03/11/2026. Đợt này gọi Trammay để lập plan mới cho 20 ID còn lại, dùng dữ liệu đã chuẩn hóa và solver sau debug. Không nhập checkpoint hoặc plan từ các lượt trước khi sửa solver.

Lượt đầu nhận được 3 prediction nhưng một số request vượt 180 giây. Đã dừng đúng tiến trình cũ sau khi kiểm tra PID/command/signature, giữ nguyên checkpoint và chuyển sang timeout dài hơn. Lượt tiếp tục chỉ nhập 3 prediction vừa nhận trong cùng yêu cầu này; hash toàn bộ solver đã được kiểm tra không đổi. 17 câu còn thiếu được gọi model tiếp.

Lượt timeout dài bị ngắt sau khi lưu 10/20 prediction; file status cũ dừng cập nhật lúc 15:34:03 ngày 03/11/2026 (giờ Việt Nam). Khi tiếp tục vào tối cùng ngày, đã xác nhận PID cũ không còn tồn tại, kiểm tra lại hash solver/runner và khởi động lại đúng experiment. 10 checkpoint được giữ, chỉ giải 10 ID còn thiếu. [Status trước khi tiếp tục](../artifacts/public_last20_trammay_extended/interruption_before_resume.json) được lưu riêng; không suy tiến trình còn chạy chỉ từ nhãn `running` cũ.

## Phạm vi

30 câu trước được chọn theo thứ tự hoàn thành. Vì vậy, 20 câu còn lại là phần bù của 30 ID đã review trong danh sách 50 ID ban đầu; chúng không tương ứng với việc cắt dòng 31–50 của `questions.jsonl`. Phần bù gồm cả câu từng lỗi dịch vụ.

- [Danh sách câu và kiểm tra phạm vi](../artifacts/public_last20_trammay_extended/selection.json): 20 ID còn lại + 30 ID đã review = đúng 50 ID, không trùng.
- Model: `ag/gemini-3.8-flash-high`, endpoint `https://trammay.xyz/v1`.
- Bốn luồng; SSE streaming; tối đa ba model calls mỗi lần giải, một lần repair; runner thử lại một lần khi lỗi vận chuyển.
- Lượt tiếp tục: timeout mỗi request 420 giây, mỗi lần giải 900 giây; toàn phiên tối đa 7.200 giây và thời gian chờ các request đang chạy kết thúc. Lượt đầu dùng 180/360 giây.
- Solver nhận câu hỏi, README, rulebook, schema và giá trị mẫu. Không đưa các đáp án từ script audit vào prompt.
- Giữ nguyên code solver trong suốt lượt chạy; [run manifest hiện tại](../artifacts/public_last20_trammay_extended/run_manifest.json) ghi hash từng file và revision dữ liệu.

## Theo dõi

- [Bảng câu hỏi và prediction](PUBLIC_TEST_LAST20_PREDICTIONS.md): ảnh chụp tiến độ tại thời điểm ghi trong báo cáo, chưa chấm đúng/sai.
- [Trạng thái hiện tại](../artifacts/public_last20_trammay_extended/status.json).
- [Log tiến độ](../artifacts/public_last20_trammay_extended/run.log).
- [Log sau khi tiếp tục](../artifacts/public_last20_trammay_extended/resume.log).
- [Lỗi tiến trình](../artifacts/public_last20_trammay_extended/stderr.log).
- [Lỗi sau khi tiếp tục](../artifacts/public_last20_trammay_extended/resume.stderr.log).
- [Trace từng câu](../artifacts/public_last20_trammay_extended/traces/).
- [Trạng thái lượt đầu](../artifacts/public_last20_trammay/status.json).
- Prediction từng phần: `result/public_test_last20_trammay.partial.jsonl`.
- File đủ 20 câu chỉ xuất hiện khi hoàn tất: `result/public_test_last20_trammay.jsonl`.

Public không có đáp án chuẩn chính thức. Số câu có prediction chỉ phản ánh tiến độ, chưa phải số câu đúng. Đây là lượt chạy API phục vụ phát triển tại máy; chưa gửi bài lên portal.

Khi đủ 20 câu, có thể ghép nguyên trạng 30 prediction baseline trước đó với 20 prediction mới bằng:

```powershell
python -X utf8 -m scripts.collect_public_batches --manifest artifacts/public_50_trammay/run_manifest.json --batch result/public_test_first10.jsonl --batch result/public_test_next20.jsonl --batch result/public_test_last20_trammay.jsonl --output result/public_test_50_collected.jsonl --report artifacts/public_last20_trammay_extended/collection_manifest.json
```

File ghép là tập hợp kết quả nhiều đợt, có nguồn của từng ID; không có nghĩa toàn bộ 50 câu đã chạy lại bằng solver mới. 30 prediction baseline vẫn bao gồm các lỗi đã ghi trong báo cáo review. Script kiểm tra đủ ID/không trùng/type/format và không sửa giá trị theo kết quả audit.
