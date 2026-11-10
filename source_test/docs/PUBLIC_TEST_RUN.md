# Public test: checkpoint cũ và kiểm chứng sau sửa

**50 câu tiếp theo, 16 luồng đã hoàn tất:** vị trí 51–100, không trùng nhóm 50 trước; đối chiếu độc lập **45 khớp, 2 sai khác, 3 abstain**. [Tổng kết](PUBLIC_TEST_NEXT50_SUMMARY.md), [bảng 50 câu](PUBLIC_TEST_NEXT50_PREDICTIONS.md), [review](PUBLIC_TEST_NEXT50_REVIEW.md), [JSONL](../result/public_test_next50_16w.jsonl). Toàn bộ 14 business đã được chuẩn bị bằng normalizer hiện tại, solver giữ nguyên trong lúc chạy. File [ghép 100 ID](../result/public_test_100_collected.jsonl) có các phiên bản lịch sử và chưa phải benchmark đồng nhất/PA chính thức.

**Bản sửa dùng chung mới nhất:** xem [GENERALIZED_REASONING_FIXES.md](GENERALIZED_REASONING_FIXES.md). Ba câu lỗi đã gọi model mới và khớp audit; các lệnh bên dưới mô tả lượt lịch sử. Khi chạy experiment mới dùng `--ingestion-report artifacts/generalized_fixes/public_ingestion.json` và thư mục artifact mới. Không nhập checkpoint trước sửa vào lượt inference mới.

**Lượt 20 câu còn lại đã hoàn tất:** 18 prediction có giá trị và 2 abstain. [Đối chiếu độc lập từ dữ liệu gốc](PUBLIC_TEST_LAST20_REVIEW.md): **17 khớp, 1 sai khác, 2 abstain**; chưa phải gold/PA chính thức. Xem [bảng prediction nguyên trạng](PUBLIC_TEST_LAST20_PREDICTIONS.md), [cấu hình và kiểm tra hoàn tất](PUBLIC_TEST_LAST20_RUN.md), [JSONL 20 câu mới](../result/public_test_last20_trammay.jsonl). Đã [ghép đủ 50 ID](../result/public_test_50_collected.jsonl), gồm 30 prediction baseline nguyên trạng + 20 prediction mới; đây là tập hợp nhiều phiên bản solver. [Nguồn từng ID](../artifacts/public_last20_trammay_extended/collection_manifest.json).

**Cập nhật debug 20 câu:** xem [kết quả chạy lại](PUBLIC_TEST_NEXT20_RERUN.md). Câu thợ An có tổng đúng 25.180.500; audit trước bỏ sót A.An. Code Solver đã đổi sau lượt live_3, nên artifact live_3 chỉ dùng xem lịch sử; cần thư mục experiment mới khi gọi model lại.

Lượt `public_50_trammay` đã dừng ở **31/50 checkpoint** khi sửa normalization. Dữ liệu, code và đáp án của lượt cũ được giữ lại; không resume thư mục đó bằng code mới.

- [Đối chiếu 10 câu đầu](PUBLIC_TEST_FIRST10_REVIEW.md): 6 khớp, 2 sai giá trị, 2 abstain.
- [Đối chiếu 20 câu tiếp theo](PUBLIC_TEST_NEXT20_REVIEW.md): 14 khớp, 4 sai giá trị, 2 abstain.
- [Chỉ mục dữ liệu chuẩn hoá mới](NORMALIZED_DATA.md).
- [Replay 30 plan trên cache mới](../artifacts/normalization_v2/frozen_plan_replay.json): 28 khớp sau đính chính audit, hai sai lệch thật trước bản sửa SQL mới; replay không phải inference mới hoặc PA.

Không có answers.jsonl trong public test. Hai báo cáo là đối chiếu độc lập với file gốc/rulebook, chưa phải kết quả bộ chấm chính thức. API là môi trường phát triển, không phải inference offline hợp lệ để nộp thi.

## Kiểm chứng mới trên ba câu cần lập plan lại

Ba ID: `q_01ac0fc9f2a2` (trung bình đơn), `q_06b7f437fcc4` (tiền phiếu của thợ), `q_06dfe652d857` (lãi ròng).

- [Trạng thái](../artifacts/normalization_v2/live_3/status.json).
- [Log](../artifacts/normalization_v2/live_3/run.log), [stderr](../artifacts/normalization_v2/live_3/stderr.log).
- [Trace](../artifacts/normalization_v2/live_3/traces/).
- [Prediction từng phần](../result/public_normalization_live3.partial.jsonl).
- File đủ ba câu: `result/public_normalization_live3.jsonl`, chỉ xuất hiện khi đủ checkpoint.

Đợt này lập plan mới qua Trammay bằng schema/cache sau sửa. Giá trị đối chiếu không được đưa vào prompt. Hai luồng, SSE streaming, tối đa ba model calls cho mỗi câu và một lượt repair.

[Kết quả kiểm chứng hiện có](NORMALIZATION_LIVE_REVIEW.md): một câu khớp, hai câu chưa có prediction do lỗi kết nối/timeout. Trạng thái cuối của lượt kiểm chứng là service_blocked; chưa khởi chạy lại 50 câu bằng bản mới.

```powershell
Get-Content artifacts/normalization_v2/live_3/status.json -Encoding UTF8
Get-Content artifacts/normalization_v2/live_3/run.log -Tail 10 -Encoding UTF8
```

Code đã đổi nên lệnh dưới tạo experiment mới, không resume thư mục live_3 cũ:

```powershell
python -X utf8 -u -m scripts.run_public_test --ingestion-report artifacts/normalization_v2/public_ingestion_final.json --question-id q_01ac0fc9f2a2 --question-id q_06b7f437fcc4 --question-id q_06dfe652d857 --workers 2 --stream --question-timeout 360 --request-timeout 180 --total-seconds 1800 --artifacts artifacts/normalization_v2/live3_after_debug --output result/public_normalization_live3_after_debug.jsonl
```

## Chạy lại 50 câu bằng dữ liệu mới

Dùng experiment riêng, có thể nhập ba checkpoint mới nếu revision/model/câu hỏi khớp; không nhập checkpoint trước sửa:

```powershell
python -X utf8 -u -m scripts.run_public_test --ingestion-report artifacts/normalization_v2/public_ingestion_final.json --limit 50 --workers 4 --stream --question-timeout 360 --request-timeout 180 --total-seconds 10800 --reuse-from artifacts/normalization_v2/live_3 --artifacts artifacts/public_50_normalized_trammay --output result/public_test_50_normalized_trammay.jsonl
```

Theo dõi `artifacts/public_50_normalized_trammay/status.json` nếu experiment này đã khởi chạy. Signature và file lock ngăn trộn phiên bản hoặc chạy trùng. Tái thực thi khớp vẫn không chứng minh SQL đã hiểu đúng nghiệp vụ.
