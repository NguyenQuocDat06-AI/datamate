"""Domain vocabulary of the data layer.

This is the only module in bizreason/data/ that may hold Vietnamese or English domain words: generic header
synonyms, note and summary labels, status words, currency/date/phone words. Entries are generic. A particular
dataset's file, sheet, table or shop column names do not belong here; tests/test_no_dataset_literals.py checks
every other module of the data layer for them.
"""

from __future__ import annotations

# Canonical role -> "|"-separated header labels (normalized by the consumer).
ALIASES = {
    "order_id": "mã đơn hàng|mã đơn|madon|madonhang|order_id|order id|số đơn|mahd",
    "ordered_at": "ngày đặt hàng|ngày đặt|ngày bán|ngaydat|ngayban|ngày giờ|ngaygio|order_date|ordered_at",
    "status": "trạng thái đơn hàng|trạng thái đơn|trạng thái|trangthai|trangthaidon|status",
    "customer_id": "mã khách hàng|mã khách|makh|makhach|makhachhang|customer_id",
    "customer_name": "tên khách hàng|tên khách|khách hàng|tenkh|tenkhach|tenkhachhang|customer_name",
    "product_id": "mã sản phẩm|mã hàng|mã sku|masp|mahang|masku|sku|product_id",
    "product_name": "tên sản phẩm|tên hàng|tên thuốc|tensp|tenhang|tenthuoc|product_name",
    "qty_sale": "số lượng sản phẩm|số lượng|soluong|sl|qty_sale|quantity",
    "qty_base": "số lượng cơ bản|số lượng quy đổi|slquydoi|soluongquydoi|qty_base",
    "unit": "đơn vị tính|đơn vị|dvt|donvi|donvitinh|unit",
    "line_amount": "thành tiền|thanhtien|trigia|trị giá|line_amount|line_net_vnd",
    "unit_price": "giá sản phẩm|đơn giá|giá bán|dongia|giaban|giabanle|unit_price",
    "cost_sale": "giá vốn lúc bán|giá vốn|giavonlucban|giavon|cost_per_sale_unit_vnd|cost_sale",
    "total_due": "tổng thanh toán|tongthanhtoan|tổng phải trả|total_due",
    "paid_amount": "số tiền thu|sotienthu|số tiền trả|số tiền thanh toán|paid_amount|amount_paid",
    "paid_at": "ngày thu|ngày trả|ngày thanh toán|ngaythu|ngaytra|ngaythanhtoan|paid_at",
    "payment_method": "hình thức thanh toán|phương thức thanh toán|hinhthucthanhtoan|phuongthuctt|payment_method",
    "conversion_factor": "tỉ lệ quy đổi|tỷ lệ quy đổi|hệ số quy đổi|heso|tylequydoi|tilequydoi|conversion_factor",
    "conversion_unit": "đơn vị quy đổi|donviquydoi|conversion_unit",
    "base_unit": "đơn vị cơ bản|donvicoban|base_unit",
    "product_category": "loại sản phẩm|nhóm hàng|loaisp|nhomhang|product_category",
    "purchase_id": "mã đơn nhập|mã phiếu nhập|maphieunhap|madonnhap|purchase_id",
    "purchased_at": "ngày nhập|ngaynhap|purchased_at",
    "supplier_id": "mã nhà cung cấp|mancc|manhacungcap|supplier_id",
    "batch_id": "mã lô|số lô|malo|solo|batch_id",
    "expires_at": "hạn sử dụng|hạn dùng|hansudung|handung|expires_at",
    "inventory_qty": "tồn cuối|toncuoi|tồn kho|tonkho|inventory_qty",
    "event_at": "ngày phát sinh|ngayphatsinh|ngày chứng từ|ngaychungtu|event_at",
    "expense_amount": "tiền chi|expense_amount",
    "order_revenue": "order_revenue|merchandise_total",
    "due_at": "ngày phải trả|hạn thanh toán|due_at|due_date",
    "qty_returned": "số lượng trả|qty_returned",
}

# Further generic labels seen in common export layouts. Applied after ALIASES, in this order.
HEADER_SYNONYMS = {
    "scope": "store_id|scope",
    "order_id": "Mã chứng từ|Số HĐ",
    "product_id": "ID SP",
    "qty_base": "SL quy đổi",
    "unit_price": "Giá đơn vị",
    "paid_at": "Thời gian thu",
    "payment_method": "Phương thức TT",
    "sales_channel": "Bán qua|sales_channel",
}
HEADER_SYNONYMS_EXTRA = {
    "order_id": "Đơn số|HĐ|Bill",
    "ordered_at": "Giờ tạo đơn|Giờ bán|Ngày HĐ|Ngày xuất",
    "status": "Trạng thái giao|Trạng thái xử lý|Tiến độ|TT đơn",
    "product_id": "Barcode|Mã H.hóa|Mã H.hoá|Mã H.hoa|Mã h.hóa|Ma hang|Mã hàng hóa",
    "product_name": "Hàng hóa|Hàng hoá|Hàng bán|Mặt hàng|Tên hàng hóa",
    "qty_sale": "S.L|SL bán|SLg|Sluong",
    "qty_base": "SL (ĐV cơ bản)|SL lẻ|Số lượng lẻ",
    "unit": "ĐV|Đơn vị (tính)",
    "customer_name": "Khách hàng (tên)|Khách|Họ tên khách",
    "line_amount": "Thành tiền (đ)|Amount",
    "unit_price": "Đơn giá (đ)|Giá 1 đv",
    "qty_returned": "Số lượng trả",
}

# Status words: normalized alias -> canonical status (base), then labelled phrases with Vietnamese marks.
STATUS_BASE = {
    "hoan thanh": "completed",
    "da hoan thanh": "completed",
    "completed": "completed",
    "da giao": "completed",
    "xong": "completed",
    "hoan tat": "completed",
    "huy": "cancelled",
    "da huy": "cancelled",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "tra hang": "returned",
    "da tra": "returned",
    "returned": "returned",
    "da hoan hang": "returned",
    "dang giao": "shipping",
    "dang giao hang": "shipping",
    "shipping": "shipping",
    "dang giao dich": "shipping",
}
STATUS_SYNONYMS = {
    "completed": "Thành công|đã lấy|đã xong|ok|received|success|done|đã giao hàng",
    "cancelled": "Huỷ đơn|Đã hủy đơn|khách hủy|Không giao được|cancel|void",
    "returned": "Đổi trả|Đã trả hàng|Khách trả|Hoàn trả|hàng trả về|trả lại|return|refunded",
    "shipping": "Đang vận chuyển|đang ship|in transit|đang chuyển|Đang xử lý|đang gửi|Chờ giao",
}

# Note rows and the header that marks a note column.
NOTE_PREFIX_RE = r"^\s*(?:ghi chú|ghi chu|note|notes)\s*:"
NOTE_HEADER = "ghi chu"

# Currency and unit words, as regular expressions over lower-cased text.
CURRENCY_SUFFIX_RE = r"(?:vnd|vnđ|đ|₫)$"
CURRENCY_STRIP_RE = r"\s*(?:vnd|vnđ|đ|₫)$"
CURRENCY_OR_PERCENT_STRIP_RE = r"\s*(?:vnd|vnđ|đ|₫|%)$"
CURRENCY_ANCHOR_RE = r"(?:k|tr|triệu|trieu|vnd|vnđ|đ|₫)$"
CURRENCY_PARENTHESIS_RE = r"\s*\((?:đ|₫|vnd|vnđ)\)\s*$"
MILLION_DECIMAL_RE = r"([+-]?\d+(?:[.,]\d+)?)\s*(?:tr|triệu|trieu)"
MILLION_RE = r"([+-]?\d+)\s*(?:tr|triệu|trieu)\s*(\d{3})?k?"

# Vietnamese day/month phrases.
VIETNAMESE_DATE_RE = (
    r"^\s*(?:ngày\s+|ngay\s+)?(\d{1,2})\s*(?:thg|tháng|thang)\.?\s*(\d{1,2})"
    r"(?:\s*,?\s*(?:năm|nam)?\s*(\d{4}))(?:\s+(\d{1,2}:\d{2}(?::\d{2})?))?\s*$"
)

# Declared coverage of an export, stated in its readme.
DECLARED_END_RE = r"(?:đến hết|tới hết|đến ngày)\s+(\d{1,2}/\d{1,2}/\d{4})"
DECLARED_START_RE = r"dữ liệu có từ\s+(\d{1,2}/\d{1,2}/\d{4})"

# Readme hint that a sheet holds one independent ledger event per row.
INDEPENDENT_ROW_HINT = "mỗi dòng"

# Cancellation is marked by a tone mark; without one a word is usually a name.
CANCEL_TONE_MARKS = ("hủy", "huỷ")

# Phone-number labels and walk-in (retail) customer names.
PHONE_LABEL_RE = r"(?:sdt|sđt|tel|phone)"
RETAIL_CUSTOMER_NAMES = {"lẻ", "khách lẻ", "khách vãng lai", "vãng lai"}

# Words written right after a number in a question that scale it ("8tr", "7,5 triệu", "500k", "2 tỷ").
QUESTION_NUMBER_SCALES = {
    "tr": 1_000_000, "triệu": 1_000_000, "trieu": 1_000_000, "củ": 1_000_000,
    "k": 1_000, "nghìn": 1_000, "nghin": 1_000, "ngàn": 1_000, "ngan": 1_000,
    "tỷ": 1_000_000_000, "tỉ": 1_000_000_000, "ty": 1_000_000_000,
}

# Debt age buckets written in a question ("0–7 ngày, 8–15 ngày, trên 15 ngày"), after normalize_lookup.
AGE_BUCKET_WORD = r"tuoi no"
AGE_OPEN_BUCKET_WORDS = r"(?:tren|hon|qua|sau)"

# A price rise named by its new price ("lên giá 97.500đ", "tăng giá lên 3.200đ"), after normalize_lookup keeps the
# digits; and the words around the counted days of a price-rise comparison.
PRICE_RISE_RE = r"(?:len gia|gia len|tang gia len|tang gia|len)\s+(\d[\d ]*?)\s*(d|dong|vnd|k)\b"
# The unit word of a price that means thousands ("152k"), after normalize_lookup.
PRICE_THOUSAND_UNITS = {"k"}
# Words that make a question one about a price rise even when it does not name the new price, after normalize_lookup.
PRICE_RISE_CUE_RE = r"\b(?:len gia|tang gia|gia len|doi gia|gia moi|ngay tang)\b"
# "N days ... before" with at most two words between, after normalize_lookup: the comparison window of a price rise.
DAYS_BEFORE_RE = r"\b(\d{1,3}) ngay (?:\w+ ){0,2}?(?:truoc|trc)\b"
# A day written with its year, day first ("28/5/2025", "01-09-25"); the separator is never a dot (prices use it).
DMY_DATE_RE = r"(?<![\d/\-])(\d{1,2})\s*[/\-]\s*(\d{1,2})\s*[/\-]\s*(\d{4}|\d{2})(?![\d/\-])"

# Phrases where "thứ <n>" or "thứ tự" is a rank or an order, not a day of the week, after normalize_lookup.
NON_WEEKDAY_PHRASE_RE = r"\b(?:theo|co|xep|hang|dung|vi tri|top) thu (?:tu|\d)\b"

# Regional word variants of the same thing (north / south); a name a question writes with one may be stored with the
# other, after normalize_lookup. Used only when the written name matches nothing and exactly one variant does.
REGIONAL_WORD_VARIANTS = (("lac", "dau phong"), ("heo", "lon"))

# A million written with its decimals after the word: "3tr7" = 3.7 million, "1tr250" = 1.25 million.
MILLION_SHORT_RE = r"(?<![\d.,])(\d+)\s*(?:tr|trieu|triệu|củ)\s*(\d{1,3})(?![\d.,])"

# Words that make a question ask an amount of money ("bao nhiêu tiền vốn", "giá trị"), after normalize_lookup.
MONEY_QUESTION_WORDS = ("tien", "von", "gia tri", "dong", "vnd")

# Words that tie a larger unit to a count of a smaller one in a written conversion ("1 X = N Y", "1 X có N Y",
# "mỗi X gồm N Y", "quy đổi: X N Y"), lower-case with diacritics kept; used as regex alternations by the layer.
UNIT_STATEMENT_LINKERS = ("=", "bằng", "có", "gồm", "là", "tương đương", "quy đổi được")
UNIT_EACH_WORDS = ("mỗi",)
UNIT_PACKING_CUES = ("quy đổi", "đóng gói", "quy cách")
# Words that are money or time, never a countable unit: a statement naming one is a price or a duration, after
# normalize_lookup.
UNIT_NON_QUANTITY_WORDS = frozenset({
    "d", "dong", "vnd", "k", "nghin", "ngan", "trieu", "tr", "ty", "ti", "usd", "dollar", "xu",
    "ngay", "tuan", "thang", "nam", "gio", "phut", "giay", "quy", "tiet",
})


# A question that asks for one value per member it names ("A và B lần lượt bao nhiêu"), after normalize_lookup.
MEMBER_LIST_CUES = ("lan luot", "moi ben", "moi loai", "tung ben", "tung loai")
# A question that asks its amount once per member ("A bao nhiêu, B bao nhiêu"), after normalize_lookup.
MEMBER_ASK_RE = r"\b(?:bao nhieu|bnhieu|bn|nhieu)\b"
# Words that make a second member an exclusion or a contrast rather than a member to list, after normalize_lookup.
MEMBER_LIST_NEGATION_CUES = ("khong phai", "ngoai tru", "tru ", "khong tinh", "khong bao gom", "loai tru", "ngoai ",
    "so sanh", "chenh", " hon ", " gap ", "phan tram", "ti le", " % ")

# Words a question uses for the canonical values of a closed dimension (after normalize_lookup), longest first per code.
CLOSED_VALUE_WORDS = {
    "payment_method": {
        "cash": ("tien mat",), "ewallet": ("vi dien tu",), "transfer": ("chuyen khoan",),
        "cod": ("cod", "thu ho"), "debt": ("ghi no", "cong no"),
    },
    "channel": {
        "store": ("tai quay", "ban le tai cua hang"), "phone": ("dien thoai",), "zalo_oa": ("zalo oa",),
    },
}
