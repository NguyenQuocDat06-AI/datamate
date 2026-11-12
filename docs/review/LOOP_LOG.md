# Nhật ký vòng lặp tự động

Quy trình và quy tắc: `GOAL_LOOP.md`.

## Trạng thái lúc bắt đầu

- Code đợt 8 đã xong trên `arch/data-operators`, chưa commit, 899 test pass, chưa chấm.
- Đợt 7 (commit `af0b49c`, nhánh `arch/structural-ingestion`) đang được chấm lại sau sự cố đầy ổ đĩa.
- Bản thử các công cụ chuẩn hóa tiếng Việt đang chạy (agent Haiku), kết quả sẽ ở `norm_test/`.

## Vòng 1 (bắt đầu 19:05)

- Môi trường: ổ đĩa trống 16 GB, máy chủ model online, worktree `arch8` có thay đổi đợt 8 chưa commit (đúng dự kiến).
- Việc: tích hợp đợt 8. Cho model gom giá trị phân loại cho 44 doanh nghiệp (`prep_wave8.py`), rồi chấm đợt 8 ở hai chế độ (`eval_e2e8.py`, `alias_off8.py`), chạy sau khi lần chấm đợt 7 kết thúc để có mốc so sánh trực tiếp.
- Quyết định giữ hay bỏ đợt 8: chờ kết quả.

### Kết quả thử các công cụ chuẩn hóa tiếng Việt (bước "tìm kiếm", chạy trước vòng 1)

Chạy trên 150 câu có nhãn và 12 câu thử, kết quả ở `norm_test/`.

| Công cụ | Đổi số, ngày | Làm mất mã như `T6/2025` | Nhận xét từ ví dụ |
|---|---|---|---|
| Tra từ điển teencode | 0 | 0 | Mở rộng đúng `hqua`, `hnay`, `bnhiu`, `k`, `dc`, nhưng đổi `bn` thành "bạn" (sai nghĩa trong câu hỏi bán hàng), không biết `dthu`, `mô`. Repo không ghi giấy phép |
| `bartpho-teencode-vilexnorm` | 1 | 8 | **Có hại**: `T6/2025` thành "thứ 6", `th5/2025` thành "thứ 5", `bnhiu` thành "bình thường". Giấy phép phi thương mại. **Loại** |
| `vietnamese-diacritic-restoration-v2` (MIT) | 0 | 0 | Thêm dấu đúng cho câu viết đủ chữ, ví dụ "Đến hết ngày 13/12/2025 tiệm còn bao nhiêu khách nợ chưa trả?". Không đụng từ viết tắt. Có lỗi đổi "ca chiều" thành "cả chiều", tức làm đổi nghĩa |
| `vietnamese-accent-marker-xlm-roberta` (Apache-2.0) | 0 | 0 | Nhiều lỗi nghĩa: "sợ bill", "mùa", "chờ lời", "cá sáng". **Loại** |

Kết luận: chỉ model thêm dấu MIT là ứng viên, và chỉ dùng kèm câu gốc (đưa cả hai cho model hiểu câu hỏi), cộng một bảng viết tắt nhỏ cho lĩnh vực bán hàng (`bn` là "bao nhiêu", `dthu` là "doanh thu", `ck` là "chuyển khoản", `tm` là "tiền mặt", `mô` là "nào"). Cần đo trên 150 câu có nhãn trước khi đưa vào luồng. Model thêm dấu có khoảng 0,4 tỷ tham số, vẫn trong giới hạn 15 tỷ. Đây là ứng viên cho một vòng sau.

### Đợt 7 tắt alias (đã chấm xong)

- 102 câu public, alias tắt: **PA 0,618** (73 đúng, 10 sai, 19 abstain), so với 0,461 ở đợt 6. Cả 9 câu từng bị mất khi tắt alias đều đã đúng trở lại (7 sai thành đúng, 2 abstain thành đúng).
- Khi tắt alias, điểm giờ bằng đúng mức của đợt 6 có alias, tức tầng dữ liệu không còn phụ thuộc vào danh sách alias trên 14 doanh nghiệp này.
- 10 câu sai còn lại: so sánh ca của `dev_003` (đã biết), "đã chở đi" (`dev_004`), hai câu của `dev_007` (hiểu sai câu hỏi và tên khách viết thường), đơn vị cơ bản "lon" (`dev_008`), chia theo hai kênh (`dev_008`), tên thợ viết tắt (`dev_012`), sữa bột số 4 (`dev_013`), trung bình mỗi đơn (`dev_001`), xếp hạng món (`dev_002`).
- Lần chấm đợt 7 có alias đang chạy lại sau sự cố đầy ổ đĩa.

### Vòng 1, tiếp (20:40)

- Lỗi: bước gom giá trị dừng sau 5 doanh nghiệp vì hàm chọn cột theo bằng chứng (đợt 5) cần cơ sở dữ liệu nguồn được gắn tên `source` cùng bảng mã danh mục tạm, mà bước gom giá trị không chuẩn bị. Test không bắt được vì dữ liệu test chỉ có một cột cho mỗi vai trò.
- Sửa trong `annotator.py`: gắn nguồn dưới tên `source` và nạp bảng mã danh mục trước, đúng cách `layer.build` làm. Đã thử trên `biz_106`, `dev_008`, `dev_003` với dữ liệu thật.
- Sửa trong `compiler.py`: khi giá trị đã được gom về dạng chuẩn, bộ lọc vẫn khớp được mọi cách ghi gốc (cột `*_raw`). Không có điều này, phần hiểu câu hỏi lưu sẵn bằng tiếng Việt sẽ không khớp giá trị chuẩn và tạo abstain giả.
- 899 test pass. Chạy lại gom giá trị, rồi chấm đợt 8 ở hai chế độ.

### Vòng 1, tiếp (21:40): lỗi hiệu năng

- Lần chấm đợt 7 có alias chạy hơn 2,5 giờ. Nguyên nhân: phép kiểm tra phiếu thu của `quality.py` dùng truy vấn con tương quan trên bảng không có chỉ mục, mất 255 giây mỗi câu ở `biz_106`. Ở môi trường thi, lỗi này sẽ vượt giới hạn 6 giờ.
- Sửa bằng phép tra tập hợp: còn 0,1 giây, kết quả giống hệt. Commit `ee313e1` trên `arch/structural-ingestion`, áp dụng cả trong `arch8`.
- Chạy lại lần chấm đợt 7 có alias, dùng lại bảng đã dựng trong `sem7`.

### Vòng 1, tiếp (22:00): đợt 7 có alias tệ hơn đợt 6

- Đợt 7 có alias: 102 câu public PA 0,520 (62 / 9 / 31), 300 câu train PA 0,830 (254 / 5 / 41). Không có câu nào thành sai, nhưng 23 câu đúng thành abstain.
- 13 câu `dev_003`: lỗi do mình dừng lần chấm chậm đúng lúc đang dựng bảng `dev_003`, để lại file hỏng có journal dở mà lần chạy lại dùng tiếp. Không phải lỗi pipeline. Đã xóa file và sửa `eval_e2e*.py` để dựng ra file tạm rồi mới đổi tên.
- 10 câu `biz_123`: lỗi thật của đợt 7. `annotation_keys_unresolved` đếm cả khóa gõ sai mà vai trò vẫn được lấp bằng nhận diện của tầng đọc file, nên phép kiểm tra chặn nhầm. Sửa trong `arch8/layer.py`: chỉ đếm vai trò vẫn thiếu sau mọi bước lấp. 899 test pass.
- Đợt 7 không đạt tiêu chí giữ khi tính trên bản có alias, nhưng các sửa của nó cần cho bản tắt alias. Hướng xử lý: sửa tiếp trong đợt 8 và so đợt 8 với mốc đợt 6.
- Sự cố vận hành: một lệnh `pkill` khớp nhầm với chính lệnh đang chạy nên làm dừng nó. Đã áp dụng lại bản sửa và chạy lại. Từ nay dừng tiến trình bằng PID cụ thể.

### Vòng 1, tiếp (22:56): đợt 8 có alias
- 102 câu public PA 0,618 (73 / 10 / 19), 300 câu train PA 0,860 (264 / 6 / 30), bằng đúng mốc đợt 6. Hai lỗi khiến đợt 7 tụt điểm đã hết.
- Đang chấm đợt 8 tắt alias (mốc đợt 7 tắt alias: 0,618).

### Vòng 1, kết thúc (23:40): giữ đợt 8
- Đợt 8 tắt alias: 102 câu public PA 0,618 (73 / 10 / 19).
- Ba phép đo: train 0,860, public có alias 0,618, public tắt alias 0,618. Không phép đo nào giảm, số câu sai không tăng. 899 test pass, kể cả test chặn hardcode.
- **Giữ.** Commit `53951bc` trên `arch/data-operators`.

## Vòng 2 (bắt đầu 23:42)
- Môi trường: ổ đĩa trống 14 GB, máy chủ model online, worktree sạch sau commit.
- Debug, câu chưa đúng ở đợt 8 theo nhóm:
  - Câu hỏi hai phần (hai đại lượng, hoặc một đại lượng cho từng bên): public 3 abstain + 1 sai, train 4 abstain. Bộ hiểu câu hỏi gắn sai dạng (`breakdown`, `compare_periods`) cho câu có `measure2`, nên phần biên dịch từ chối.
  - Thiếu giờ đơn cho câu hỏi theo ca: train 7 abstain (`biz_108`, `biz_120`, `biz_128`).
  - Tồn kho: train 6 abstain, chưa có đại lượng này.
  - Lọc theo tên đường của khách: public 3 abstain, nhưng cả 3 đáp án là `unanswerable`, nên abstain thay vì trả lời đúng.
  - Câu sai còn lại: mỗi nhóm 1 câu, đã ghi ở vòng 1.
- Chọn nhóm câu hỏi hai phần (có câu sai, xuất hiện ở cả hai tập). Sửa trong `compiler.py`, không cần gọi lại model:
  - Có `measure2` khác `measure`, không chia nhóm, một khung thời gian: quy về dạng `scalar`, vốn đã trả hai số.
  - `measure2` trùng `measure` và có hai bộ lọc cùng chiều: trả giá trị cho từng bên theo thứ tự hỏi.
- Agent Haiku sửa `compiler.py` và thêm 4 test (903 test pass). Mình rà lại và thu hẹp quy tắc thứ nhất: chỉ áp cho `breakdown`. Lý do: câu "số nhập trừ số bán" bị parse thành `compare_periods` với hai đại lượng khác nhau, và đáp án của nó là hiệu chứ không phải hai số.
- Nghiên cứu sẵn cho vòng sau (nhóm câu hỏi theo ca): giờ nằm ở cột "Thời gian tạo" của file gốc. `layer.py:182` (`effective_roles`) ghi đè cột có giờ mà `_choose_column` đã chọn bằng cột chỉ có ngày lấy từ vai trò của tầng đọc file. Dựng lại tay 3 câu của `biz_108` bằng giờ gốc đều khớp đáp án (31.848.600, 25.921.800, −5,36%). Cách sửa đề xuất: không ghi đè `ordered_at` khi model đã chỉ ra cột.
- 23:58: đang chấm vòng 2 ở hai chế độ.
- Kết quả vòng 2: train 0,860 → **0,867** (266 / 6 / 28), public có alias 0,618 → **0,647** (76 / 10 / 16), public tắt alias 0,618 → **0,647** (76 / 10 / 16). Có 5 câu chuyển từ abstain thành đúng (3 public, 2 train) và không câu nào thành sai.
- **Giữ.** Commit trên `arch/data-operators` (xem `git log`).

## Vòng 3 (bắt đầu 00:00)
- Nhóm: câu hỏi theo ca bị thiếu giờ đơn (train 7 abstain). Nguyên nhân đã truy ở vòng 2.
- Sửa (agent Haiku, `layer.py`): cột ngày của tầng đọc file không còn ghi đè `ordered_at`, mà cạnh tranh với các cột model chỉ ra. Cột có giờ chỉ thắng khi ít nhất một nửa giá trị của nó cũng đọc được ngày, để cột chỉ có giờ không thành ngày đơn. Thêm 3 test, 906 test pass.
- 00:05: bảng cũ chuyển sang `sem8_r2` và `alias_off_run8/sem_r2` để khôi phục được. Đang dựng lại bảng và chấm hai chế độ.
- Nghiên cứu sẵn cho vòng 4 (tên sản phẩm, nhóm, hoạt chất không khớp; 10 câu):
  - Nguyên nhân A, 4 câu train (`biz_111`, `biz_123`): danh mục có hai cột được gán `product_id` (mã vạch, phần lớn trống, và SKU). `layer.py:937` gọi `effective_roles` không có `dst`, nên cột đầu, tức mã vạch, thắng. Kết quả là tên và nhóm của dòng bán bị trống. Đã dựng lại tay cả 4 đáp án.
  - Nguyên nhân B, 2 câu public (`dev_005`): sheet thuộc tính (hoạt chất, kê đơn) chỉ có SKU, không có tên. `_load_product_attrs` đòi tên ngay trên sheet đó nên bỏ thuộc tính. Đi qua SKU → tên của danh mục cho đúng cả hai đáp án.
  - Ba câu nên giữ abstain: đáp án không dựng lại được, hoặc đáp án có vẻ sai.
- Kết quả vòng 3: train 0,867 → **0,890** (273 / 6 / 21). Public có alias và tắt alias giữ nguyên 0,647. Cả 7 câu hỏi theo ca chuyển từ abstain thành đúng, không câu nào khác đổi kết quả.
- **Giữ.** Commit trên `arch/data-operators`. Đã xóa bảng dự phòng `sem8_r2` và `alias_off_run8/sem_r2`.

## Vòng 4 (bắt đầu 01:07)
- Nhóm: tên và thuộc tính sản phẩm bị mất khi dựng bảng (nguyên nhân A và B đã truy ở vòng 3). Sửa trong `layer.py`.
- Sửa (agent Haiku, `layer.py`):
  - Khi có hai cột cùng là mã, chưa có bằng chứng từ danh mục hoặc điểm nối bằng nhau, cột có nhiều giá trị khác nhau, không trống, nhất sẽ thắng. Vai trò của mọi bảng được chọn với dữ liệu nguồn trước khi nạp mã danh mục.
  - Dòng thuộc tính có mã mà không có tên thì lấy tên từ danh mục, nếu mã đó ứng với đúng một tên.
- Mình rà lại và sửa một chỗ: agent đã bỏ cả dòng thuộc tính có mã nhưng không tra được tên, trong khi trước đây dòng đó vẫn nối theo mã. Mình khôi phục điều kiện cũ và sửa test tương ứng. 910 test pass.
- 01:15: bảng cũ chuyển sang `sem8_r3` và `alias_off_run8/sem_r3`. Đang dựng lại bảng và chấm hai chế độ.
- Kết quả vòng 4, có alias: train 0,890 → **0,913** (281 / 7 / 12), public 0,647 → **0,667** (78 / 10 / 14). Có 10 câu chuyển từ abstain thành đúng: 2 public (`dev_005` thuốc kê đơn, hoạt chất), 8 train (`biz_111`, `biz_121`, `biz_123`).
- Một câu train chuyển từ abstain thành sai: `q_bc9c524d61dc` ("bnhiu ly size m ... cong moi mon z"). Model parse thành doanh thu và tự thêm bộ lọc nhóm khách "sỉ" không có trong câu. Câu cùng nội dung viết có dấu thì đúng. Lỗi nằm ở bước hiểu câu hỏi, chỉ lộ ra vì dữ liệu size nay đã có. Ứng viên vòng sau: loại bộ lọc có giá trị không xuất hiện trong câu hỏi.
- Đang chấm tắt alias.
- Kết quả vòng 4, tắt alias: public 0,647 → **0,667** (78 / 10 / 14).
- **Giữ**: ba phép đo đều tăng, số câu sai ở public có alias vẫn là 10. Commit trên `arch/data-operators`. Đã xóa bảng dự phòng.

## Vòng 5 (bắt đầu 02:17)
- Nhóm: bộ lọc do model tự thêm, có giá trị không xuất hiện trong câu hỏi (vừa gây 1 câu sai ở train). Câu sai bị trừ điểm, nên đây là nhóm ưu tiên.
- Sửa (agent Haiku, `solver.py`, `vocabulary.py`): thêm `QUOTED_DIMENSIONS`, gồm các chiều có giá trị chép nguyên văn từ câu hỏi (sản phẩm, nhóm, khách, nhóm khách, nhân viên, nhà cung cấp, size, đơn vị, hãng, hoạt chất, dịch vụ, xe). Bộ lọc thuộc các chiều này mà không có từ nào xuất hiện trong câu hỏi thì abstain. Các chiều có giá trị chuẩn (`phone`, `yes`, `afternoon`) không bị kiểm tra. 917 test pass.
- Kết quả vòng 5: train 0,913 → **0,920** (281 / 5 / 14). Public có alias và tắt alias giữ 0,667. Hai câu sai chuyển thành abstain (`q_bc9c524d61dc`, `q_64006dcec25e`), không câu đúng nào bị mất.
- **Giữ.** Commit trên `arch/data-operators`.

## Vòng 6 (bắt đầu 02:36)
- Nhóm: câu sai do chọn sai cách ghi tên khi trả lời, 3 câu sai ở public: `q_54bbf080f0cd` ("Ca phe sua (M)" thay vì "Cà phê sữa (M)"), `q_575157e63e1c` ("Ngo Hong Duy" thay vì "Ngô Hồng Duy"), `q_b342fe1e1211` ("Đạt" thay vì "Hà Loan Đạt").
- Nghiên cứu (agent Haiku, chỉ đọc):
  - Tên in ra là `MIN(product_name)` hoặc `MIN(customer_name)` (`compiler.py:47`), tức cách ghi nhỏ nhất theo nhị phân. Cách ghi không dấu luôn đứng trước nên luôn được chọn.
  - **Lỗi tính tất định:** `canonicalize_customers` (`bizreason/data/identities.py`) chọn cách ghi tên khách theo thứ tự duyệt `set` khi hai cách ghi dài bằng nhau. Kết quả phụ thuộc `PYTHONHASHSEED`: thử 6 seed, 3 lần ra "Ngô Hồng Duy", 3 lần ra "Ngo Hong Duy". Điểm có thể đổi giữa hai lần chạy giống hệt nhau.
  - `q_54bbf080f0cd` còn sai thứ tự: hai món hòa nhau về số lượng, đáp án xếp theo doanh thu. Chưa sửa, vì chưa rõ quy tắc phân xử khi hòa.
  - `q_b342fe1e1211` cần tra tên đầy đủ trong danh sách thợ. Để vòng sau.
- Sửa (2 agent Haiku song song): `identities.py` sắp thứ tự tường minh (không viết hoa toàn bộ, nhiều chữ có dấu, xuất hiện nhiều, dài, rồi theo chữ cái). `compiler.py` in cách ghi có dấu, rồi cách ghi xuất hiện nhiều nhất trong nhóm.
- Hai bản sửa xong, 926 test pass:
  - `identities.py`: dùng `Counter` thay `set`, sắp thứ tự tường minh. Thêm `tests/test_identities.py`, trong đó có test chạy với hai `PYTHONHASHSEED` khác nhau.
  - `compiler.py`: thêm hàm gộp SQLite `best_spelling`, dùng cho `LABEL` và cho xếp hạng con nợ.
- Lưu ý về đo: `eval_e2e8.py` dựng bảng từ cache ingestion của main, nên bản sửa ở tầng đọc file (`identities.py`) chỉ đo được ở chế độ tắt alias, vì chế độ này đọc lại file bằng code worktree. Đã chuyển `alias_off_run8/cache` và `sem` sang `*_r5` để đọc lại từ đầu.
- **Việc nên làm sau vòng lặp:** cho `eval_e2e8.py` đọc file bằng code worktree, không dùng cache của main. Nếu không, các thay đổi ingestion từ đợt 6 chỉ được đo ở chế độ tắt alias.
- 02:55: đang chấm vòng 6.
- Kết quả vòng 6: public tắt alias 0,667 → **0,686** (79 / 9 / 14), vì `q_575157e63e1c` ("Ngô Hồng Duy") chuyển từ sai thành đúng. Public có alias 0,667 và train 0,920 không đổi. `q_54bbf080f0cd` đã in đúng tên có dấu nhưng vẫn sai thứ tự khi hòa.
- **Giữ.** Commit trên `arch/data-operators`.

## Vòng 7 (bắt đầu 03:40)
- Môi trường: máy chủ model online, ổ đĩa trống 11 GB.
- Debug, 10 câu sai ở public có alias:
  - Compiler âm thầm bỏ một phần câu hỏi (2 câu):
    - `average_per_weekday` không có bộ lọc thứ trong tuần nên trả về tổng (`q_6dd1a1921d60`).
    - `breakdown` chỉ có một thành viên, model bỏ mất vế "tại quầy" (`q_7e7b3dc1de86`).
  - Model bỏ ý của câu hỏi, mỗi lỗi 1 câu: "hủy" (`q_4bb10819b3a3`), "số 4" (`q_1fc8004b6900`).
  - Còn lại mỗi câu một nguyên nhân: hòa thứ hạng, ca của `dev_003`, "đã chở đi", đơn vị "lon", tên thợ đầy đủ, và "Ngô Hồng Duy" (chỉ còn sai ở chế độ dùng cache của main).
- Chọn nhóm compiler bỏ ý: chuyển 2 câu sai thành abstain. Đã kiểm tra trên cả hai tập, cả hai phép kiểm tra chỉ chạm vào câu đang sai hoặc đang abstain.
- Sửa (agent Haiku, `compiler.py`): hai phép kiểm tra. Agent thêm một phép ngoài yêu cầu: trung bình theo thứ trong tuần trên các dạng khác `scalar` cũng abstain, vì các dạng đó bỏ qua `compare_as`. Không câu đúng nào bị ảnh hưởng. 928 test pass.
- Kết quả vòng 7: public có alias 0,667 → **0,686** (78 / 8 / 16), public tắt alias 0,686 → **0,706** (79 / 7 / 16), train giữ 0,920. Hai câu sai chuyển thành abstain ở cả hai chế độ.
- **Giữ.** Commit trên `arch/data-operators`.

## Vòng 8 (bắt đầu 03:56, vòng cuối theo giới hạn 8 vòng)
- Nhóm: tên nhân viên viết tắt (`q_b342fe1e1211`, in "Đạt" thay vì "Hà Loan Đạt"). Danh sách thợ có tên đầy đủ.
- Nghiên cứu và sửa (agent Haiku, `layer.py`):
  - Danh sách thợ được gán đúng loại `employee_catalog`, nhưng `layer.build` không có nhánh cho loại này nên bỏ qua.
  - Đã thêm phần nạp tên đầy đủ, và `match_employee` đổi cách ghi ngắn ("Đạt", "a Đạt", "A.Đạt") thành tên đầy đủ khi khớp đúng một người. Cách ghi gốc giữ ở `employee_raw`. 932 test pass.
- 04:02: bảng cũ chuyển sang `sem8_r7` và `alias_off_run8/sem_r7`. Đang dựng lại và chấm.
- 04:50: máy chủ model không còn phản hồi, kernel Kaggle ở trạng thái `COMPLETE` (hết 12 giờ). Đã khởi chạy lại, là lần duy nhất được phép trong đêm (phiên bản 4, `RUNNING`). Lần push chỉ tải lên `run_server_12h.py` và metadata, kernel để riêng tư. Đang chờ đường hầm online. Bước chấm tắt alias cần model, nên có thể phải chạy lại sau khi máy chủ online.
- Kết quả vòng 8, có alias: public 0,686 → **0,706** (79 / 7 / 16), vì `q_b342fe1e1211` chuyển thành "Hà Loan Đạt". Train giữ 0,920. Không câu nào bị mất.
- Kết quả vòng 8, tắt alias: 0,706 → 0,647 (76 / 10 / 16). `q_b342fe1e1211` thành đúng, nhưng 4 câu của `dev_013` chuyển từ đúng thành sai, với giá trị lệch nhẹ (203 so với 211, 445.649.000 so với 448.559.000).
- Nguyên nhân 4 câu `dev_013` **không phải bản sửa vòng 8**:
  - Số dòng bán của `dev_013` là 20.643 ở mọi lần dựng từ vòng 2 đến vòng 7, nhưng lên 31.987 ở vòng 8. Vòng 8 là lần dựng đầu tiên sau khi khởi chạy lại kernel.
  - `alias_off8.py` gọi `annotate_line_kinds` mỗi lần dựng, không cache, nên phân loại dòng do model mới trả về đã khác.
  - `dev_013` không có bảng nhân viên, và bản sửa vòng 8 chỉ cập nhật cột `employee`.
- **Quyết định: không commit** theo đúng quy tắc giữ, vì điểm tắt alias giảm. Bản sửa để nguyên, chưa commit, trong worktree `arch8` (`layer.py`, `tests/test_semantic_layer.py`, 932 test pass). Người dùng quyết định.
- Phát hiện quan trọng: phân loại dòng không tất định giữa hai phiên máy chủ. Trên bộ private, cùng một code có thể cho điểm khác nhau. Cần cache phân loại dòng và cố định cấu hình sinh (nhiệt độ 0, cùng seed), rồi đo lại `dev_013` hai lần.

## Tổng kết vòng lặp (dừng 05:10, đủ 8 vòng)

| Phép đo | Đầu đêm | Cuối (vòng 7, đã commit) | Vòng 8 (chưa commit) |
|---|---|---|---|
| 300 câu train | 0,860 | **0,920** (281 / 5 / 14) | 0,920 |
| 102 câu public có alias | 0,618 | **0,686** (78 / 8 / 16) | 0,706 |
| 102 câu public tắt alias | 0,618 | **0,706** (79 / 7 / 16) | 0,647 (nhiễu do phân loại dòng) |

Commit đã giữ trên `arch/data-operators`:
- `53951bc` đợt 8: gom giá trị, sửa lỗi kiểm tra chặn nhầm.
- `4e2b5bb` câu hỏi hai phần.
- `4c00922` cột có giờ cho `ordered_at`.
- `066ea51` chọn cột mã danh mục, tên cho sheet thuộc tính.
- `5be7ed3` chặn bộ lọc không có trong câu hỏi.
- `c4f91a9` chọn cách ghi tên tất định, in tên có dấu.
- `e46113c` abstain khi compiler bỏ một phần câu hỏi.

Đã thử và bỏ: không có vòng nào bị bỏ vì chính thay đổi gây hại. Vòng 8 chưa commit vì nhiễu đo.

Vận hành:
- Kaggle hết 12 giờ lúc khoảng 04:00. Đã khởi chạy lại một lần (phiên bản 4) và đang online.
- Đã sửa `eval_e2e*.py` để dựng bảng an toàn, ghi ra file tạm rồi mới đổi tên.

Việc nên làm tiếp:
- Cache phân loại dòng, cố định cấu hình sinh, rồi đo lại để quyết định vòng 8.
- Cho `eval_e2e8.py` đọc file bằng code worktree thay vì cache của main.
- Theo `FABLE_REVIEW.md`: dừng kiểu sửa từng cụm lỗi. Dựng bộ đo tính tổng quát, nối V2 chạy đầu-cuối, rồi làm hướng A (tầng ngữ nghĩa điều khiển bằng rulebook).
