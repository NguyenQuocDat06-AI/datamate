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
    "debt_outstanding": "money still owed at the end of a day",
    "debtor_count": "number of customers still owing at the end of a day",
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
}

# Rulebook §3: an order without a customer identity is a walk-in and is not a customer.
WALK_IN_NAMES = ("khach le", "khach vang lai")
# Rulebook §1 and §5: wording of a cancelled document, compared after lookup normalization.
CANCEL_TOKENS = ("huy", "cancel")
# Rulebook §4: payment-method wording that marks a debt order, and wording of orders that are not customer debt
# (cash on delivery, marketplace sales). Compared after lookup normalization, as whole words.
DEBT_METHOD_TOKENS = ("ghi no", "debt")
NON_DEBT_METHOD_TOKENS = ("cod", "giao hang thu tien", "san", "marketplace", "tmdt")
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
# Currency spellings that may follow a grouped amount such as 75,000 or 75.000. Compared case-insensitively.
CURRENCY_SUFFIXES = ("vnd", "vnđ", "đ", "₫")

# Generic nouns, unit words and time words a question may attach to a name; removed before an exact match.
GENERIC_FILTER_WORDS = (
    "hang", "mat hang", "san pham", "sp", "nhom", "nhom hang", "khach", "khach hang", "nhan vien", "thung", "hop",
    "chai", "lon", "goi", "ly", "kg", "cai", "thang", "tuan", "ngay", "nam",
)
# Rulebook §7: a question about a customer's address needs a column carrying one.
ADDRESS_HEADER_TOKENS = ("dia chi", "addr", "address", "duong pho")
