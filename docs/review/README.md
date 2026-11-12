# Tài liệu review và công cụ đo của DataMate V2

Nhánh `docs/review-notes` dựa trên `arch/rulebook-ir`. Nhánh gom các tài liệu và công cụ nằm ngoài gói `bizreason`.

## Tài liệu (`docs/review/`)

| File | Nội dung |
|---|---|
| `FABLE_REVIEW.md` | Đánh giá kiến trúc, rủi ro cho bộ private, kế hoạch đột phá (hướng A, B, C), lộ trình |
| `FABLE_RESEARCH.md` | Cuộc thi và benchmark tương tự, mã nguồn công khai, phục vụ model trên T4, model trong giới hạn |
| `REVIEW_PLAN.md`, `REVIEW_PLANNING_PROMPT.md` | Kế hoạch review ban đầu |
| `SUBAGENT_RULES.md`, `AGENTS_source_test.md` | Quy tắc cho agent phụ |
| `GOAL_LOOP.md`, `LOOP_LOG.md` | Vòng lặp sửa theo cụm lỗi (đêm 10/11), 8 vòng |
| `GOAL_LOOP_A.md`, `LOOP_LOG_A.md` | Vòng lặp thực hiện hướng A: bộ đo tổng quát, lệnh chạy đầu-cuối, thông lượng |

## Công cụ (`tools/`)

- `tools/eval/`:
  - `gen_score.py`: chấm điểm, kèm khoảng tin cậy bootstrap theo shop.
  - `gen_perturb_questions.py`, `gen_perturb_data.py`: phép đo 2 và 3.
  - `gen_industry_baseline.py`: phép đo 4.
  - `gen_bench_server.py`: đo thông lượng máy chủ.
  - `gen_kaggle_auth.py`, `gen_push_server.py`, `gen_cancel_session.py`, `gen_wait_new_endpoint.py`: quản lý kernel Kaggle.
  - Các script chấm cũ (`eval_e2e*.py`, `alias_off*.py`).
- `tools/server/server_batch/`: máy chủ có gom lô và cache prefix KV (phiên bản 2). `server_base/` là cùng máy chủ đó nhưng phục vụ `google/gemma-4-12B-it`.
  - Đây là bản đã che giá trị bí mật: token và kênh relay là chuỗi giữ chỗ `__SECRET_n__`, tên người dùng Kaggle là `<kaggle-username>`.
  - `gen_push_server.py` điền lại các giá trị này lúc push, từ file cục bộ không nằm trong repo.

## Không có trong repo

Dữ liệu đề, đáp án, bản ghi kết quả có câu hỏi và đáp án, bảng SQLite dựng sẵn, `.env`, `connection_config.json`, địa chỉ máy chủ.

## Lệnh chính

    python -m bizreason.run_v2 --data <thư mục bộ test> --out <thư mục ra> --connection <json> --endpoint-file <json> --workers 16 --build-workers 2 --no-alias
