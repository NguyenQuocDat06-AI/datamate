"""Business ontology taken from the rulebook. The only module allowed to hold domain vocabulary."""

from __future__ import annotations

MEASURES: dict[str, str] = {
    "revenue": "money sold: line amounts of orders neither cancelled nor returned, by order date",
    "order_count": "number of orders neither cancelled nor returned",
    "cancelled_count": "number of cancelled orders",
    "cancel_rate": "percent of cancelled orders among all orders placed",
    "avg_per_order": "revenue divided by number of orders",
    "qty_base_sold": "quantity sold in base units; also what best-selling means",
    "orders_containing_product": "number of orders containing a product; what most favoured means",
    "order_total_due": "total customers must pay for orders, including shipping, after order discount",
    "order_discount_total": "money discounted by voucher, points, or shipping fee collected",
    "distinct_customers": "number of customers with at least one valid order",
    "active_days": "number of distinct days with activity by a customer or employee",
    "purchase_amount": "money of import receipts by receipt date, excluding cancelled receipts",
    "purchase_receipt_count": "number of import receipts; what main supplier means",
    "unit_purchase_price": "average import price of one unit of a product",
    "unit_sale_price_on_date": "selling price of a product on a given date",
    "gross_profit": "revenue minus cost of goods sold",
    "net_profit": "gross profit minus all expenses of the month",
    "expense_amount": "money in the expense ledger",
    "debt_outstanding": "money still owed at the end of a day, credit sales and instalment plans alike, instalments not yet due included",
    "debtor_count": "number of customers still owing at the end of a day",
    "installment_outstanding": "money still owed on instalment plans only at the end of a day, instalments not yet due included",
    "installments_due_count": "number of instalments whose due day has arrived by a day, one due on that day included, whether paid or not",
    "overdue_amount": "money of instalments due before a day and not yet paid (only instalments already past due)",
    "overdue_debtor_count": "number of customers with an instalment due before a day that is still unpaid",
    "debt_new": "credit added in a period: unpaid part on the purchase day",
    "debt_repaid": "money repaid in a period, after the purchase day",
    "debt_age_bucket": "outstanding debt split by age in days since the order date",
    "inventory_qty": "stock quantity at the end of a day",
    "inventory_value": "stock value at cost at the end of a day",
    "expiry_lot": "lots in stock by expiry date",
    "expiring_lot_value": "cost value of remaining stock in lots expiring in a range",
    "out_of_stock_count": "number of products with zero stock at end of day",
    "no_sale_count": "number of products sold before a period but not in it",
    "price_change_effect": "change in quantity sold after versus before a price increase",
    "service_ticket_count": "number of repair visits or tickets",
    "warranty_count": "number of warranty claims",
    "warranty_rate": "warranty claims divided by paid tickets or units sold",
    "delivery_trip_count": "number of delivery trips",
    "new_member_count": "customers who registered in a period",
    "avg_price_per_unit": "money sold divided by the base units sold: the average selling price of one unit",
    "avg_order_total_due": "average, over orders, of the total the customer must pay for the whole order",
    "outside": "none of the above, or the concept is not defined",
}

SHAPES: dict[str, str] = {
    "scalar": "one number or value",
    "compare_periods": "difference or percent change between two periods",
    "share_of_total": "percent that one subset represents of the whole",
    "ratio_of_counts": "percent of entities meeting a condition",
    "threshold_count": "count of entities or days or orders meeting a threshold",
    "top_k": "the k best entities, as a list",
    "argmax_entity": "which single entity is highest or lowest",
    "argmax_date": "which single day is highest or lowest",
    "argmax_period": "which of several listed periods is highest",
    "breakdown": "several values, one per group or per period, in the order asked",
    "trend_check": "yes or no about how values change across periods",
    "list_filter": "list entities satisfying a condition",
}

DIMENSIONS: dict[str, str] = {
    "product": "a named product",
    "product_group": "a product group or category",
    "customer": "a named customer",
    "customer_group": "a customer segment",
    "employee": "a named or coded staff member",
    "supplier": "a supplier",
    "channel": "a sales channel",
    "payment_method": "how the order was paid",
    "shift": "morning or afternoon shift",
    "size": "product size",
    "unit": "a sales or base unit",
    "brand": "a brand",
    "active_ingredient": "a drug active ingredient",
    "prescription": "whether a drug needs a prescription; value yes or no",
    "product_kind": "kind of product such as new or used device",
    "weekday": "a day of the week",
    "hour": "an hour of the day",
    "service_item": "a kind of repair work",
    "vehicle": "a vehicle or plate",
    "device_line": "a device model line: the product name before its storage capacity",
}

# Dimensions whose filter value is copied from the question text, so the value must be found in the question.
# Other dimensions carry canonical values (for example "yes", "afternoon", "phone") that need not appear in it.
QUOTED_DIMENSIONS = frozenset({
    "product", "product_group", "customer", "customer_group", "employee", "supplier", "size", "unit", "brand",
    "active_ingredient", "service_item", "vehicle",
})

# Rulebook §3: an order without a customer identity is a walk-in and is not a customer.
# Spellings of a walk-in customer seen in shop files ("lẻ", "kl" for khách lẻ) are the same walk-in.
WALK_IN_NAMES = ("khach le", "khach vang lai", "le", "kl", "khachle", "vang lai")
# Rulebook §1 and §5: wording of a cancelled document, compared after lookup normalization.
CANCEL_TOKENS = ("huy", "cancel")
# Rulebook §4: payment-method wording that marks a debt order, and wording of orders that are not customer debt
# (cash on delivery, marketplace sales). Compared after lookup normalization, as whole words.
DEBT_METHOD_TOKENS = ("ghi no", "debt")
NON_DEBT_METHOD_TOKENS = ("cod", "giao hang thu tien", "san", "marketplace", "tmdt")
# Free-text debt notes on an order (accent-free lower case, whole words). PAID: the debt is settled (read as nothing
# owed); OWES_ALL: the whole order is owed; OWES: an amount still owed follows the word, or, with no amount, the rest
# of the order is owed; PRE_PAID: an amount already handed over follows.
DEBT_NOTE_PAID = ("da tra du", "tra du", "ck du", "chuyen khoan du", "da thanh toan", "da tat toan", "het no",
                  "tra het", "da tra het", "tra xong", "da tra xong", "tra day du")
DEBT_NOTE_OWES_ALL = ("no ca don", "no het don", "no toan bo", "chua tra gi", "chua thanh toan")
DEBT_NOTE_OWES = ("ghi no", "ghi so", "con no", "con thieu", "con lai", "thieu lai", "thieu", "chua tra", "no")
DEBT_NOTE_PRE_PAID = ("dua truoc", "tra truoc", "khach dua", "da dua", "dat coc", "da tra", "ck", "chuyen khoan", "tra")
# Words after a number that give its size, accent-free: a thousand, a million.
MONEY_THOUSAND_WORDS = ("k", "nghin", "ngan")
MONEY_MILLION_WORDS = ("tr", "trieu")
# A phrase that makes a wording an example, not a rule of the shop (README conversions and notes).
EXAMPLE_CUES = ("vd", "v.d", "vi du", "chang han", "e.g", "eg", "example", "for example")
# Rulebook §1 and §4: the payment methods a shop's wording is mapped onto. The only fixed category list; the
# keys are English and the definitions say what each method means for a sale.
PAYMENT_CANONICAL: dict[str, str] = {
    "cash": "paid in cash when the order is placed",
    "transfer": "paid by bank transfer",
    "cod": "cash on delivery, collected by the carrier; not cash in the shop",
    "debt": "recorded as debt (ghi no), the customer pays later",
    "ewallet": "paid through an e-wallet app",
    "card": "paid by payment card",
}
# Rulebook §2: shifts by order time, both ends inclusive. Matched when the key appears in the normalized filter text.
SHIFT_WINDOWS = {"sang": ("06:00:00", "13:59:59"), "chieu": ("14:00:00", "21:59:59"),
    "morning": ("06:00:00", "13:59:59"),
    "afternoon": ("14:00:00", "21:59:59"),
}
# Rulebook §2: when two shifts are compared, the morning shift is the baseline.
SHIFT_BASELINE = "sang"
# Rulebook §2: the afternoon shift, the one compared against the baseline. A key of SHIFT_WINDOWS.
SHIFT_AFTERNOON = "chieu"
# Rulebook §2: the Tết window. Start is (lunar month, lunar day, year offset from the Tết lunar year); end is
# (lunar month, lunar day) in the Tết lunar year, inclusive. Default: 23 of month 12 of the previous year to 10 of month 1.
TET_START = (12, 23, -1)
TET_END = (1, 10)
# Meanings established at ingestion, expressed in the annotation vocabulary.
INGESTION_ROLES = {
    "order_id": "order_id", "ordered_at": "ordered_at", "ordered_date": "ordered_at", "status": "order_status",
    "product_id": "product_id", "product_name": "product_name", "customer_id": "customer_id",
    "customer_name": "customer_name", "qty_sale": "qty", "qty_base": "qty_base", "unit": "unit",
    "unit_price": "unit_price", "line_amount": "line_amount", "cost_sale": "line_cost",
    "total_due": "order_total_due", "payment_method": "payment_method", "product_category": "product_category",
    "sales_channel": "channel", "purchase_id": "purchase_id", "purchased_at": "purchased_at", "supplier_id": "supplier_id",
}
# Rulebook §3: a service charge line is revenue but not a product. Compared after lookup normalization.
SERVICE_FEE_NAMES = ("phi phuc vu",)
# Rulebook §1: the canonical order statuses whose orders are sales (revenue, order counts, sold quantities), and the
# canonical status of a cancelled order (cancelled count, cancel rate, debt). Definition sets: a rulebook that states
# them replaces these, it does not add to them. Canonical names are the layer's CANONICAL_STATUS.
VALID_SALE_STATUSES = ("completed", "shipping")
CANCELLED_STATUSES = ("cancelled",)
# English names a question may use for the two shifts, mapped to the shift role they stand for. Always present in the
# shift windows of the active rulebook parameters, so a question in English still finds its shift.
SHIFT_CANONICAL_NAMES = {"morning": "baseline", "afternoon": "afternoon"}
# Rulebook cue words. A rulebook row or line carrying one of them is shown to the parameter model
# (rulebook_params.relevant_rulebook_text). Whole words, compared after lookup normalization.
RULEBOOK_CUES: dict[str, tuple[str, ...]] = {
    "shift": ("ca sáng", "ca chiều", "shift", "morning", "afternoon"),
    "walk_in": ("khách vãng lai", "khách lẻ", "walk-in", "walk in"),
    "service_fee": ("phí phục vụ", "service charge", "service fee"),
    "payment_method": ("ghi nợ", "cod", "tmđt", "giao hàng thu tiền", "marketplace", "debt"),
    "tet": ("tết", "âm lịch", "chạp", "giêng", "mùng"),
    # Rulebook §1 definitions of a sale, a cancelled order and a return, with the canonical status names.
    "order_status": ("đơn có trạng thái huỷ", "đơn trả hàng", "đang giao", "cancelled", "returned", "shipping"),
    # Rulebook §4 instalments: whether an instalment order is a debt order and when an instalment is overdue.
    "installment": ("trả góp", "kỳ góp", "quá hạn", "installment", "installments", "overdue"),
}
# A convention is read from the rulebook only when it writes the topic at all (a deterministic check against a model
# that reports a rule from general knowledge). Each field needs every group to be present (one cue of each group).
INSTALLMENT_TOPIC_CUES = ("trả góp", "kỳ góp", "installment", "installments")
OVERDUE_TOPIC_CUES = ("quá hạn", "overdue")
# Rulebook §5 stock in large units: a question that asks for whole large units and loose base units gets the list
# [whole large units, loose base units]. Whole-word cues after normalize_lookup: one for "whole" and one for "loose".
LARGE_UNIT_WHOLE_CUES = ("chan", "nguyen")
LARGE_UNIT_LOOSE_CUE = "le"
# Rulebook §2 and §5: stock measures read the position at the end of a day from the stock report, and lot measures
# read the lots that carry expiry dates.
STOCK_MEASURES = frozenset({"inventory_qty", "inventory_value", "out_of_stock_count"})
LOT_MEASURES = frozenset({"expiry_lot", "expiring_lot_value"})
# Currency spellings that may follow a grouped amount such as 75,000 or 75.000. Compared case-insensitively.
CURRENCY_SUFFIXES = ("vnd", "vnđ", "đ", "₫")

# Generic nouns, unit words and time words a question may attach to a name; removed before an exact match.
GENERIC_FILTER_WORDS = (
    "hang", "mat hang", "san pham", "sp", "nhom", "nhom hang", "khach", "khach hang", "nhan vien", "thung", "hop",
    "chai", "lon", "goi", "ly", "kg", "cai", "thang", "tuan", "ngay", "nam",
)
# Rulebook §7: a question about a customer's address needs a column carrying one.
ADDRESS_HEADER_TOKENS = ("dia chi", "addr", "address", "duong pho")
# Question wording that asks for a value beside each entity of a ranked list ("with the amount", "each person's
# amount", "and the number"). Compared after lookup normalization, as whole words. Used only with a top-k intent
# that groups by an entity, so a plain list of values is never matched by these words.
TABLE_ASK_CUES = ("kem", "moi nguoi", "tung nguoi", "tung ngui", "tug nguoi", "tug ngui", "va so", "tung tho", "moi tho")

# Shop talk for small shops, written with and without diacritics because questions are often typed without them.
# A shop word names the shop itself, the subject of the question. It is never a location, channel or customer.
SHOP_SELF_WORDS = ("cửa hàng", "cua hang", "quán", "quan", "tiệm", "tiem", "shop", "sốp", "sop")
# Count unit words. After a how-many word they ask for a quantity sold, so the measure is qty_base_sold.
QUANTITY_UNIT_WORDS = (
    "đĩa", "dia", "ly", "cốc", "coc", "chai", "hộp", "hop", "cái", "cai", "gói", "goi", "lon", "thùng", "thung",
    "kg", "bao", "cây", "cay", "viên", "vien", "vỉ", "vi",
)
# How-many words that come before a count unit or a money word, such as "how many" or "how much".
HOW_MANY_WORDS = ("bao nhiêu", "bao nhieu", "bnhiu", "mấy", "may")
# Money words: after how-many they ask for money (revenue), never for a quantity.
MONEY_ASK_WORDS = ("tiền", "tien", "doanh thu", "dthu")
# Cancel wording for prompts. Each form matches CANCEL_TOKENS after lookup normalization, which drops diacritics.
CANCEL_WORDS = ("huỷ", "hủy", "huy", "cancel")
# Words for a sale and for an order, used in the prompt examples.
SALE_WORDS = ("bán", "ban")
ORDER_WORDS = ("đơn", "don", "đơn hàng", "don hang")
# Group words. They are the only reason to add a product_group or customer_group filter.
PRODUCT_GROUP_WORDS = ("nhóm hàng", "nhom hang", "danh mục", "danh muc", "loại hàng", "loai hang")
CUSTOMER_GROUP_WORDS = ("nhóm khách", "nhom khach", "loại khách", "loai khach", "phân loại khách", "phan loai khach")
# Average words. With a money word and a per-order phrase they ask for the average revenue of one order.
AVERAGE_WORDS = ("bình quân", "binh quan", "trung bình", "trung binh")
# Per-order phrases. They name one order, so an average over them is revenue per order.
PER_ORDER_WORDS = ("mỗi đơn", "moi don", "một đơn", "mot don")
# Import price words. With an average word they ask for the average import price of one unit.
IMPORT_PRICE_WORDS = ("giá nhập", "gia nhap")


# Judgements a question may ask about ("thợ nhiệt tình nhất", "nhà cung cấp uy tín") that no shop file records. When
# the rulebook does not define one, the question is unanswerable (rulebook §7). Accent-free, after normalize_lookup.
# Words that also name goods or a sales ranking ("ngon", "tốt nhất") are left out.
UNDEFINED_JUDGEMENT_WORDS = (
    "trung thuc", "nhiet tinh", "chat luong", "uy tin", "lanh nghe", "tay nghe", "khen", "quy men", "tan tam",
    "chu dao", "than thien", "de thuong", "hai long", "vui ve", "lich su", "tin cay", "dang tin", "chuyen nghiep",
    "ngon nhat",
)

# Words of a rulebook term that only say what kind of entity it is about; a definition applies when the other words
# of its term appear in the question. Accent-free, after normalize_lookup.
DEFINITION_GENERIC_WORDS = frozenset({
    "khach", "khach hang", "nhan", "vien", "tho", "mat", "hang", "san", "pham", "cua", "cac", "mot", "trong", "ky",
    "nguoi", "nao", "la", "va", "hoac",
})
# A rulebook term that names a ranking carries one of these words.
DEFINITION_RANKING_WORDS = frozenset({"nhat", "chinh", "diem"})

# "vì" introduces the cause of an event ("huỷ vì giao trễ"); and the words that ask for a percent.
CAUSE_WORD = "vi"
PERCENT_CUES = ("phan tram", "ti le", "ty le")
# Delivery wording and the words that count deliveries rather than orders.
DELIVERY_WORDS = ("giao", "cho di", "cho hang")
TRIP_WORDS = ("dot", "chuyen", "luot")
# A ranking "nhiều đơn nhất" ranks by the number of orders.
MOST_ORDERS_CUE = "nhieu don nhat"
# "số nhập trừ số bán", "chênh lệch nhập trừ bán", "nhập trừ bán": received minus sold (rulebook §5).
PURCHASE_MINUS_SOLD_RE = r"\bnhap(?: vao| ve| zia)? (?:\S+ ){0,6}?tru (?:\S+ ){0,3}?ban\b"
# Rulebook phrases that must be present for an industry rule to apply (rule gating): repair visits counted as
# tickets, device lines across capacities, multi-trip deliveries.
RULE_TICKET_VISITS = ("luot xe",)
RULE_DEVICE_LINE = ("dong may",)
RULE_DELIVERY_TRIPS = ("dot", "giao")

# Rulebook sentences a measure's meaning rests on. The measure answers only when the active rulebook writes one of
# the alternatives (each a tuple of accent-free phrases that must all appear); a rulebook that does not define it
# makes the question unanswerable by this measure (abstain), never a meaning carried over from another rulebook.
# No rulebook text at all (unit tests) leaves a measure open, as rulebook_params.mentions does.
MEASURE_RULE_CUES: dict[str, tuple[tuple[str, ...], ...]] = {
    # a new member is a customer whose registration date falls in the period
    "new_member_count": (("thanh vien moi", "dang ky"),),
    # the total of a whole order: what the customer must pay for it
    "avg_order_total_due": (("tong don",), ("khach phai tra",)),
}
# Storage capacity inside a product name ("128GB", "1 TB"), case-insensitive. A device line is the name before it.
DEVICE_CAPACITY_RE = r"\s*\b\d+(?:[.,]\d+)?\s*(?:gb|tb)\b"
# Share of the distinct names of a product kind that carry a capacity for the kind to count as devices.
DEVICE_KIND_MIN_SHARE = 0.5
# Gender wording a question uses for customers, accent-free after normalize_lookup, and the header wording of a
# column that records it (as it does for an address with ADDRESS_HEADER_TOKENS).
GENDER_VALUE_WORDS = ("nam", "nu", "male", "female")
GENDER_HEADER_TOKENS = ("gioi tinh", "gender", "sex")
# Header wording of a column that records when a customer registered.
REGISTRATION_HEADER_TOKENS = ("dang ky", "tham gia", "registered", "registration", "join")

# Explicit definitional prose connectors, independent of document heading depth.
DEFINITION_PROSE_CONNECTORS = ("is defined as", "means", "is", "là", "được định nghĩa là")

# Narrow, explicit population clauses. Status values themselves come from the runtime
# schema/data; these are grammar words, not a mapping from metric names to answers.
POPULATION_ENTITY_RE = r"(?:orders?|records?|rows?|transactions?|đơn(?: hàng)?|bản ghi|giao dịch)"
POPULATION_POSITIVE_RE = (
    r"\bof (?P<values>{values}) {entities}\b",
    r"\bonly (?P<values>{values}) {entities}\b",
    r"\b(?P<values>{values}) {entities} only\b",
    r"\b{entities} (?:whose )?status (?:is|in) (?P<values>{values})\b",
    r"\bchỉ (?:tính |gồm )?{entities} (?P<values>{values})\b",
)
POPULATION_ALL_RE = r"\b(?:all|every|tất cả|mọi) {entities}\b"
POPULATION_INCLUDE_RE = r"\b(?:including|include|kể cả|bao gồm cả) (?P<values>{values}) {entities}\b"
POPULATION_CONNECTOR_RE = r"(?: or | hoặc | hay )"


# Measure families: a question that carries a cue of a family must get a measure of that family, otherwise the model
# read the wrong concept and the number would belong to another question. Cues are accent-free, whole words after
# normalize_lookup. `veto` cues move the question out of the family (an overdue lot is not an overdue instalment).
# `allowed` are the measures that can answer such a question; `money` and `count` are the measures a plain
# "how much money" or "how many customers" question maps to when the model chose a measure outside the family.
MEASURE_FAMILIES: dict[str, dict] = {
    "overdue": {
        "cues": ("qua han", "tre han", "cham gop", "cham tra gop", "tre gop", "no xau", "overdue", "past due"),
        "veto": ("han su dung", "hsd", "expiry", "expire", "expired", "lo hang"),
        "allowed": frozenset({"overdue_amount", "overdue_debtor_count", "debt_age_bucket", "outside"}),
        "money": "overdue_amount",
        "count": "overdue_debtor_count",
    },
}
# Wording that asks for a count of customers, wording that asks for money, and counts of other things; used only to
# repair the measure when exactly one reading remains.
FAMILY_COUNT_ENTITY_WORDS = ("khach", "nguoi", "ho", "customer", "customers", "debtor", "debtors")
FAMILY_MONEY_WORDS = ("tien", "money", "amount")
FAMILY_AMBIGUOUS_COUNT_WORDS = ("don", "hoa don", "order", "orders")
# Words that ask for a count, accent-free; a customer count is one of them followed within two words by an entity word.
FAMILY_COUNT_ASK_WORDS = ("bao nhieu", "bnhiu", "may", "so", "so luong", "tong so", "how many", "number of")
