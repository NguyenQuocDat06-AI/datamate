# API model DataMate

Gateway OpenAI-compatible trên máy này: `http://127.0.0.1:8010/v1`.
User service `datamate-model-api.service` giữ địa chỉ cố định khi URL GPU thay đổi.
Chat đầu tiên tự khởi động GPU; kiểm tra health không khởi động GPU.

Model duy nhất: `google/gemma-4-12B-it`, revision
`707f0a3b8a3c7ad586ed01e27eafbad8a27dd0f7`, 11.959.730.224 tham số,
NF4/fp16. Mỗi lần khởi động kiểm SHA-256 trọng số, số tham số, revision và
instance trước khi chuyển câu hỏi. Hai GPU nạp hai bản sao cùng checkpoint.

Chạy từ `the repository root`, dùng Python
`/home/ubuntu/datamate/venv/bin/python`:

```bash
python -m serve.model_api status
python -m serve.model_api start --wait --timeout 1200
python -m serve.model_api chat --prompt 'Chỉ trả lời: Xin chào' --max-tokens 32
python -m serve.model_api stop
```

Đã cài shortcut `datamate-api` trong `~/.local/bin`; có thể chạy từ mọi thư mục:
`datamate-api status`, `datamate-api start --wait`, `datamate-api chat --prompt 'Xin chào'`,
`datamate-api stop`. User service đã bật linger để tiếp tục chạy sau khi logout.

`stop` tắt phiên GPU thuộc controller; gateway vẫn chạy để nhận yêu cầu tiếp theo.
Mặc định phiên tối đa 6 giờ, tự tắt sau 30 phút không có chat. Khởi động lại cần
quota Kaggle còn đủ và có thể mất vài phút để tải/nạp model. Không bảo đảm dịch vụ
GPU luôn sẵn sàng. Controller tránh khởi động thêm khi tài khoản có job đang chạy
hoặc trạng thái chưa xác định. Dữ liệu service và khóa được lưu ngoài Git, quyền 600.

Client Python dùng cùng cơ chế tự tìm URL và bật lại GPU:

```python
from serve.model_api import ModelApiClient

api = ModelApiClient()
try:
    response = api.chat(
        [{"role": "user", "content": "Chỉ trả lời: Xin chào"}],
        max_tokens=32,
        timeout=1200,
    )
    print(response["choices"][0]["message"]["content"])
finally:
    api.close()
```

Đã tạo cặp cấu hình private cho pipeline:

- `/home/ubuntu/datamate_review_data/model_api_client/connection.json`
- `/home/ubuntu/datamate_review_data/model_api_client/endpoint.json`

Để dùng trong `bizreason.run_v2`, prewarm bằng
`datamate-api start --wait --timeout 1200`, rồi truyền hai tham số sau cho lệnh chạy
pipeline hiện có (tránh để thời gian cold start vượt timeout suy luận của pipeline):

```bash
--connection /home/ubuntu/datamate_review_data/model_api_client/connection.json \
--endpoint-file /home/ubuntu/datamate_review_data/model_api_client/endpoint.json
```

Client HTTP đọc khóa vào bộ nhớ; không in khóa hay đưa khóa trực tiếp vào argv:

```python
import json
from pathlib import Path
import httpx

folder = Path('/home/ubuntu/datamate_review_data/model_api_client')
connection = json.loads((folder / 'connection.json').read_text())
endpoint = json.loads((folder / 'endpoint.json').read_text())
with httpx.Client(timeout=1800, trust_env=False) as client:
    response = client.post(
        endpoint['base_url'] + '/chat/completions',
        headers={'Authorization': 'Bearer ' + connection['bearer_token']},
        json={'model': connection['model'],
              'messages': [{'role': 'user', 'content': 'Chỉ trả lời: Xin chào'}],
              'max_tokens': 32, 'temperature': 0},
    )
    response.raise_for_status()
    print(response.json()['choices'][0]['message']['content'])
```

Routes yêu cầu bearer: `GET /health`, `GET /v1/metadata`, `GET /v1/models`,
`POST /v1/chat/completions`, `POST /admin/start`, `POST /admin/shutdown`.
`/admin/start` trả 202; health cho biết đã ready hay chưa. Streaming hiện gom toàn
bộ kết quả trước rồi trả SSE; chưa hỗ trợ token streaming.

Gateway chỉ bind localhost. Muốn truy cập từ máy khác, dùng SSH port forwarding
`ssh -L 8010:127.0.0.1:8010 <máy-chạy-gateway>`, rồi dùng cùng client private.
Không đưa khóa, file state, endpoint GPU hay source job đã điền cấu hình lên Git.
Registry bên ngoài chỉ phục vụ tìm URL; thông báo có HMAC và URL được kiểm chứng
lại bằng metadata của đúng instance. Cách đọc cache theo
[API chính thức của ntfy](https://docs.ntfy.sh/subscribe/api/#fetch-cached-messages),
với hạn tuổi kiểm tra trên timestamp đã ký. Tải model/khởi động GPU cần Internet;
đây là dịch vụ nghiên cứu, tách biệt gói Docker nộp thi offline.

Vận hành gateway:

```bash
systemctl --user status datamate-model-api.service
systemctl --user restart datamate-model-api.service
```
