"""Turn a typed intent into an answer on canonical tables. Definitions follow the rulebook."""

from __future__ import annotations

import operator
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from bizreason.contracts import TimeWindow
from bizreason.semantic.vocabulary import (
    DEBT_METHOD_TOKENS, NON_DEBT_METHOD_TOKENS, SHIFT_BASELINE, SHIFT_WINDOWS,
)
from bizreason.utils import normalize_lookup

VALID = "status IN ('completed','shipping')"
MONEY = {
    "revenue", "avg_per_order", "purchase_amount", "gross_profit", "debt_outstanding", "debt_repaid", "debt_new",
    "unit_purchase_price", "unit_sale_price_on_date", "order_total_due",
}
DEBT_MEASURES = {"debt_outstanding", "debtor_count", "debt_repaid", "debt_new"}
# Debt measures that can be limited to orders older than a number of days (rulebook §4 debt age).
AGE_MEASURES = {"debt_outstanding", "debtor_count"}
# Measures that read order status. Without any status the SQL would return 0 or nothing, which is a wrong answer.
STATUS_MEASURES = {
    "revenue", "order_count", "cancelled_count", "cancel_rate", "avg_per_order", "qty_base_sold",
    "distinct_customers", "gross_profit", "orders_containing_product", "no_sale_count", "order_total_due",
}
# Measures where a zero is only trustworthy when cancellation is recorded at all.
CANCEL_MEASURES = {"cancelled_count", "cancel_rate"}
ORDER_DIMS = {
    "customer": "customer_key", "customer_group": "customer_group", "employee": "employee",
    "channel": "channel", "payment_method": "payment_method",
}
LINE_DIMS = {"product": "product_key", "product_group": "category", "size": "product_name", "unit": "unit"}
LABEL = {"product": "MIN(product_name)", "customer": "MIN(customer_name)", "employee": "employee"}
# Threshold subjects: (table, key column, metric expressions). A subject is one key; its metric is per key.
THRESHOLD_SUBJECTS = {
    "customer": ("orders", "customer_key", {"order_count": "COUNT(DISTINCT order_id)", "revenue": "SUM(revenue)"}),
    "order": ("orders", "order_id", {"revenue": "SUM(revenue)"}),
    "day": ("orders", "date", {"order_count": "COUNT(DISTINCT order_id)", "revenue": "SUM(revenue)"}),
    "product": (
        "lines", "product_key",
        {"order_count": "COUNT(DISTINCT order_id)", "qty": "SUM(COALESCE(qty_base, qty))", "revenue": "SUM(amount)"},
    ),
}
THRESHOLD_OPS = {"gte": operator.ge, "gt": operator.gt, "lte": operator.le, "lt": operator.lt}
THRESHOLD_SHAPES = {"threshold_count", "ratio_of_counts"}
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")


class Unsupported(Exception):
    """The system cannot establish an answer; the caller abstains."""


@dataclass
class Intent:
    measure: str
    shape: str = "scalar"
    group_by: str | None = None
    filters: list[dict[str, Any]] = field(default_factory=list)
    top_k: int | None = None
    compare_as: str = "percent"
    # {"subject": "customer|order|product|day", "metric": "order_count|revenue|qty", "op": "gte|gt|lte|lt", "value": number}
    threshold: dict[str, Any] | None = None
    # Debt measures only: keep orders strictly older than this many days at the cutoff.
    age_days: int | None = None


@dataclass
class Answer:
    type: str
    value: Any


def _round_money(value: float | None) -> int:
    if value is None:
        raise Unsupported("no value")
    return int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _round2(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _whole(value: float | None) -> float | int:
    if value is None:
        raise Unsupported("no value")
    return int(value) if float(value).is_integer() else _round2(value)


def _mentions(method: Any, tokens: tuple[str, ...]) -> bool:
    """True when the normalized payment method equals a token or contains it as whole words."""
    if method is None:
        return False
    padded = f" {normalize_lookup(method)} "
    return any(f" {normalize_lookup(token)} " in padded for token in tokens)


def _filter_column(dim: str) -> str:
    """The stored column that a filter's predicate reads; rows where it is null are kept by an exclusion."""
    if dim in {"product", "size"}:
        return "product_name"
    if dim == "shift":
        return "ts"
    return ORDER_DIMS.get(dim) or LINE_DIMS[dim]


def _clock_of(value: Any) -> str | None:
    """The time part of a stored order timestamp as HH:MM:SS, or None when the text has no valid time."""
    if value is None:
        return None
    found = _CLOCK.search(str(value))
    if found is None:
        return None
    hour, minute, second = int(found[1]), int(found[2]), int(found[3] or 0)
    if hour > 23 or minute > 59 or second > 59:
        return None
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def _name_head(value: Any) -> str | None:
    """The normalized product name before its first add-on, so a size written before the add-on still reads as the end."""
    if value is None:
        return None
    return normalize_lookup(str(value).split(" + ", 1)[0])


def _check_age(intent: Intent) -> None:
    """Rulebook §4: an age only means something for the debt measures that take it; anywhere else it would be dropped."""
    if intent.age_days is None:
        return
    if intent.measure not in AGE_MEASURES:
        raise Unsupported(f"age on measure {intent.measure}")
    age = intent.age_days
    if isinstance(age, bool) or not isinstance(age, int) or age < 0:
        raise Unsupported("age_days must be a whole number of days")


def _shift_key(text: Any) -> str:
    """The shift named by a filter value. Unknown or ambiguous wording is not an answer."""
    needle = normalize_lookup(text)
    keys = [key for key in SHIFT_WINDOWS if key in needle]
    if len(keys) != 1:
        raise Unsupported(f"unknown shift: {text}")
    return keys[0]


class Compiler:
    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.db.create_function("clock_of", 1, _clock_of)
        self.db.create_function("name_head", 1, _name_head)
        self.has_lines = self.db.execute("SELECT 1 FROM lines LIMIT 1").fetchone() is not None
        line_columns = {row[1] for row in self.db.execute("PRAGMA table_info(lines)")}
        # Rulebook §3: a unit filter means base units. Only used when the base unit is recorded on the lines.
        self.base_unit = "base_unit" in line_columns and self.db.execute(
            "SELECT 1 FROM lines WHERE base_unit IS NOT NULL LIMIT 1"
        ).fetchone() is not None
        # Rulebook §3: a service charge is revenue but not a product that can be ranked or counted.
        self.items_only = "is_item=1" if "is_item" in line_columns else "1=1"
        self.price_expr = "COALESCE(unit_price, amount*1.0/qty)" if "unit_price" in line_columns else "amount*1.0/qty"
        self._has_status: bool | None = None
        self._has_cancelled: bool | None = None
        self._has_time: bool | None = None
        self._supplier_col: str | None = None

    def _require_order_status(self) -> None:
        if self._has_status is None:
            self._has_status = self.db.execute(
                "SELECT 1 FROM orders WHERE status IS NOT NULL LIMIT 1"
            ).fetchone() is not None
        if not self._has_status:
            raise Unsupported("order status unavailable")

    def _require_cancellations(self) -> None:
        if self._has_cancelled is None:
            self._has_cancelled = self.db.execute(
                "SELECT 1 FROM orders WHERE status='cancelled' LIMIT 1"
            ).fetchone() is not None
        if not self._has_cancelled:
            raise Unsupported("cancellation not recorded")

    def _require_order_time(self) -> None:
        """Shift filters need the time of day on the order timestamp; a shop that records dates only cannot use them."""
        if self._has_time is None:
            try:
                self._has_time = self.db.execute(
                    "SELECT 1 FROM orders WHERE clock_of(ts) IS NOT NULL LIMIT 1"
                ).fetchone() is not None
            except sqlite3.OperationalError as exc:
                raise Unsupported("order time unavailable") from exc
        if not self._has_time:
            raise Unsupported("order time unavailable")

    def _supplier_column(self) -> str:
        """Supplier matching reads the supplier name; the id is used only when no receipt carries a name."""
        if self._supplier_col is None:
            try:
                named = self.db.execute(
                    "SELECT 1 FROM purchases WHERE supplier_name IS NOT NULL LIMIT 1"
                ).fetchone() is not None
            except sqlite3.OperationalError as exc:
                raise Unsupported("no purchase supplier column") from exc
            self._supplier_col = "supplier_name" if named else "supplier_id"
        return self._supplier_col

    def _shift_filter(self, text: str) -> tuple[str, list[Any], bool]:
        """Rulebook §2: a shift is a window on the time of day of the order. Works on orders and lines alike."""
        key = _shift_key(text)
        self._require_order_time()
        start, end = SHIFT_WINDOWS[key]
        return "(clock_of(ts) >= ? AND clock_of(ts) <= ?)", [start, end], False

    def resolve_filter(self, dim: str, text: str, on: str | None = None) -> tuple[str, list[Any], bool]:
        """Return a SQL predicate for a filter, or raise when the value is not in the data.

        `on="purchases"` resolves a product or supplier name against the purchase rows instead of the sales lines.
        """
        if dim == "shift":
            if on is not None:
                raise Unsupported(f"shift filter on {on}")
            return self._shift_filter(text)
        if dim == "supplier" and on != "purchases":
            raise Unsupported("supplier filter outside purchases")
        column = ORDER_DIMS.get(dim) or LINE_DIMS.get(dim)
        if column is None and dim != "supplier":
            raise Unsupported(f"dimension {dim}")
        if on == "purchases" and dim not in {"product", "supplier"}:
            raise Unsupported(f"purchase filter on {dim}")
        needle = normalize_lookup(text)
        if dim == "size":
            token = needle.split()[-1] if needle else ""
            if not token:
                raise Unsupported("size value")
            return "(name_head(product_name) LIKE ?)", [f"% {token}"], True
        if on == "purchases":
            table, source, line = "purchases", (self._supplier_column() if dim == "supplier" else "product_name"), False
        elif dim == "unit" and self.base_unit:
            # Exact match only: a partial unit name could be a different unit.
            values = [r[0] for r in self.db.execute("SELECT DISTINCT base_unit FROM lines WHERE base_unit IS NOT NULL")]
            matched = [v for v in values if needle and normalize_lookup(v) == needle]
            if not matched:
                raise Unsupported(f"unresolved {dim}: {text}")
            return f"base_unit IN ({','.join('?' for _ in matched)})", list(matched), True
        else:
            table = "lines" if column in LINE_DIMS.values() else "orders"
            if not self.has_lines and table == "lines":
                raise Unsupported("line detail unavailable")
            source = "product_name" if dim == "product" else column
            line = table == "lines"
        values = [r[0] for r in self.db.execute(f"SELECT DISTINCT {source} FROM {table} WHERE {source} IS NOT NULL")]
        exact = [v for v in values if normalize_lookup(v) == needle]
        partial = [v for v in values if needle and needle in normalize_lookup(v)]
        matched = exact or partial
        if not matched or (len(matched) > 1 and not exact and dim in {"product", "customer"}):
            raise Unsupported(f"unresolved {dim}: {text}")
        slots = ",".join("?" for _ in matched)
        return f"{source} IN ({slots})", list(matched), line

    def _predicates(self, intent: Intent, skip: bool = False) -> tuple[list[str], list[Any], bool]:
        sql, args, needs_lines = [], [], False
        if not skip:
            for item in intent.filters:
                piece, values, line = self.resolve_filter(item["dim"], item["value"])
                if item.get("exclude") is True:
                    column = "base_unit" if item["dim"] == "unit" and self.base_unit else _filter_column(item["dim"])
                    piece = f"({column} IS NULL OR NOT ({piece}))"
                sql.append(piece)
                args.extend(values)
                needs_lines = needs_lines or line
        return sql, args, needs_lines

    def _purchase_predicates(self, intent: Intent, skip: bool) -> tuple[list[str], list[Any]]:
        """Receipts carry only supplier as a filter. Any other filter raises rather than giving a store-wide number."""
        sql, args = [], []
        if skip:
            return sql, args
        for item in intent.filters:
            if item.get("dim") != "supplier":
                raise Unsupported(f"purchase amount with filter {item.get('dim')}")
            piece, values, _ = self.resolve_filter("supplier", item["value"], on="purchases")
            if item.get("exclude") is True:
                piece = f"({self._supplier_column()} IS NULL OR NOT ({piece}))"
            sql.append(piece)
            args.extend(values)
        return sql, args

    def _threshold(self, intent: Intent, window: TimeWindow) -> tuple[int, int]:
        """Subjects whose metric meets the threshold, and all subjects with a valid row in the window.

        The second number is the base of a ratio. A subject with a null metric is in the base but never meets.
        """
        spec = intent.threshold or {}
        subject, metric, op_name = spec.get("subject"), spec.get("metric"), spec.get("op")
        if subject not in THRESHOLD_SUBJECTS:
            raise Unsupported(f"threshold subject {subject}")
        table, key, metrics = THRESHOLD_SUBJECTS[subject]
        if metric not in metrics:
            raise Unsupported(f"threshold metric {metric} on {subject}")
        if op_name not in THRESHOLD_OPS:
            raise Unsupported(f"threshold op {op_name}")
        try:
            bound = float(spec.get("value"))
        except (TypeError, ValueError) as exc:
            raise Unsupported("threshold value") from exc
        if table == "lines" and not self.has_lines:
            raise Unsupported("line detail unavailable")
        where, args, needs_lines = self._predicates(intent)
        if table == "orders" and needs_lines:
            raise Unsupported(f"{subject} threshold with a line filter")
        if subject == "product":
            # Rulebook §3: a service charge is not a product, so it is neither ranked nor counted.
            where = where + [self.items_only]
        span = [window.start.isoformat(), window.end_exclusive.isoformat()]
        clause = " AND ".join(where + [VALID, "date>=? AND date<?", f"{key} IS NOT NULL"])
        rows = self.db.execute(
            f"SELECT {metrics[metric]} FROM {table} WHERE {clause} GROUP BY {key}", args + span
        ).fetchall()
        compare = THRESHOLD_OPS[op_name]
        met = sum(1 for (value,) in rows if value is not None and compare(value, bound))
        return met, len(rows)

    @staticmethod
    def _members(intent: Intent, dim: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Filters that name members of `dim` (exclusions are not members), and all other filters, in filter order."""
        members, others = [], []
        for item in intent.filters:
            if item.get("dim") == dim and item.get("exclude") is not True:
                members.append(item)
            else:
                others.append(item)
        return members, others

    @staticmethod
    def _sole_product(intent: Intent, skip_filters: bool) -> str:
        """The one product named by the intent. The measures that take it accept no other filter."""
        if skip_filters or len(intent.filters) != 1:
            raise Unsupported("needs exactly one product filter")
        item = intent.filters[0]
        if item.get("dim") != "product" or item.get("exclude") is True:
            raise Unsupported("needs exactly one product filter")
        return str(item["value"])

    def _debt_orders(self, intent: Intent, skip_filters: bool) -> list[tuple[Any, Any, Any, Any, bool]]:
        """Eligible orders (not cancelled, no non-debt method) as (order_id, customer_key, date, total_due, is_debt)."""
        if not skip_filters:
            for item in intent.filters:
                if item["dim"] != "customer":
                    raise Unsupported(f"debt measure with filter {item['dim']}")
        where, args, _ = self._predicates(intent, skip_filters)
        clause = " AND ".join(["COALESCE(status, '') <> 'cancelled'", *where])
        rows = self.db.execute(
            f"SELECT order_id, customer_key, date, total_due, payment_method FROM orders WHERE {clause}", args
        ).fetchall()
        return [
            (order_id, customer, day, due, _mentions(method, DEBT_METHOD_TOKENS))
            for order_id, customer, day, due, method in rows
            if not _mentions(method, NON_DEBT_METHOD_TOKENS)
        ]

    def _debt(self, intent: Intent, window: TimeWindow, skip_filters: bool) -> float | None:
        """Rulebook §4. The unpaid part of an order is its total_due less payments dated up to the cutoff, never below 0."""
        try:
            payment_rows = self.db.execute("SELECT order_id, date, amount FROM payments").fetchall()
        except sqlite3.OperationalError as exc:
            raise Unsupported("no payments table") from exc
        if not payment_rows:
            raise Unsupported("no payments")
        paid: dict[Any, list[tuple[Any, Any]]] = {}
        for order_id, paid_on, amount in payment_rows:
            paid.setdefault(order_id, []).append((paid_on, amount))
        orders = self._debt_orders(intent, skip_filters)
        if orders and all(due is None for _, _, _, due, _ in orders):
            raise Unsupported("no total_due")
        start, end = window.start.isoformat(), window.end_exclusive.isoformat()
        # Rulebook §4: age counts days from the order date to the last day of the window. An order exactly
        # age_days old is not older than age_days, so it is left out.
        age_limit = None
        if intent.age_days is not None:
            age_limit = (window.end_exclusive - timedelta(days=1 + intent.age_days)).isoformat()

        def unpaid(order_id: Any, due: Any, paid_by: Any) -> float | None:
            if due is None:
                return None
            settled = sum(amount for paid_on, amount in paid.get(order_id, []) if amount is not None and paid_by(paid_on))
            return max(float(due) - settled, 0.0)

        m = intent.measure
        if m in {"debt_outstanding", "debtor_count"}:
            owed = [
                (customer, unpaid(order_id, due, lambda d: d is not None and d < end))
                for order_id, customer, day, due, _ in orders
                if day is not None and day < end and (age_limit is None or day < age_limit)
            ]
            if m == "debt_outstanding":
                return sum(r for _, r in owed if r is not None)
            per_customer: dict[Any, float] = {}
            for customer, remaining in owed:
                if customer is not None and remaining is not None:
                    per_customer[customer] = per_customer.get(customer, 0.0) + remaining
            return sum(1 for total in per_customer.values() if total > 0)
        if m == "debt_repaid":
            total = 0.0
            for order_id, _, day, _, is_debt in orders:
                if not is_debt or day is None:
                    continue
                for paid_on, amount in paid.get(order_id, []):
                    if amount is not None and paid_on is not None and start <= paid_on < end and paid_on > day:
                        total += amount
            return total
        if m == "debt_new":
            total = 0.0
            for order_id, _, day, due, is_debt in orders:
                if not is_debt or day is None or due is None or not start <= day < end:
                    continue
                total += unpaid(order_id, due, lambda d: d is not None and d == day) or 0.0
            return total
        raise Unsupported(f"measure {m}")

    def _unit_purchase_price(self, intent: Intent, window: TimeWindow, skip_filters: bool) -> float | None:
        """Rulebook §5 average import price: SUM(qty*unit_cost)/SUM(qty) over non-cancelled purchase rows in the window.

        Rows missing qty or unit_cost are left out of both sums, so the average is not skewed by them.
        """
        piece, values, _ = self.resolve_filter("product", self._sole_product(intent, skip_filters), on="purchases")
        row = self.db.execute(
            "SELECT SUM(qty*unit_cost)*1.0/NULLIF(SUM(qty),0) FROM purchases "
            f"WHERE cancelled=0 AND qty IS NOT NULL AND unit_cost IS NOT NULL AND {piece} AND date>=? AND date<?",
            values + [window.start.isoformat(), window.end_exclusive.isoformat()],
        ).fetchone()
        return row[0]

    def _unit_sale_price(self, intent: Intent, window: TimeWindow, skip_filters: bool) -> float | None:
        """Unit price charged for one product on the window's single day, from sale lines of every status.

        Uses the price written on the line (before line discount); falls back to amount over quantity.
        """
        if window.end_exclusive - window.start != timedelta(days=1):
            raise Unsupported("unit price needs a one-day window")
        if not self.has_lines:
            raise Unsupported("line detail unavailable")
        piece, values, _ = self.resolve_filter("product", self._sole_product(intent, skip_filters))
        rows = self.db.execute(
            f"SELECT DISTINCT {self.price_expr} FROM lines WHERE qty>0 AND {piece} AND date>=? AND date<?",
            values + [window.start.isoformat(), window.end_exclusive.isoformat()],
        ).fetchall()
        prices = {r[0] for r in rows if r[0] is not None}
        if len(prices) != 1:
            raise Unsupported("no single unit price that day")
        return prices.pop()

    def _no_sale_count(self, intent: Intent, window: TimeWindow, skip_filters: bool) -> int:
        """Rulebook §5 unsold products: sold in a valid order before the window, with no valid order inside it."""
        if not self.has_lines:
            raise Unsupported("line detail unavailable")
        where, args, _ = self._predicates(intent, skip_filters)
        start, end = window.start.isoformat(), window.end_exclusive.isoformat()
        before = " AND ".join(where + [VALID, "date<?", "product_key IS NOT NULL"])
        inside = " AND ".join(where + [VALID, "date>=? AND date<?"])
        row = self.db.execute(
            f"SELECT COUNT(*) FROM (SELECT product_key FROM lines WHERE {before} GROUP BY product_key) p "
            f"WHERE NOT EXISTS (SELECT 1 FROM lines WHERE product_key = p.product_key AND {inside})",
            args + [start] + args + [start, end],
        ).fetchone()
        return row[0]

    def value(self, intent: Intent, window: TimeWindow, skip_filters: bool = False) -> float | None:
        _check_age(intent)
        if intent.measure in DEBT_MEASURES:
            return self._debt(intent, window, skip_filters)
        m = intent.measure
        if m in STATUS_MEASURES:
            self._require_order_status()
        if m in CANCEL_MEASURES:
            self._require_cancellations()
        if m == "unit_purchase_price":
            return self._unit_purchase_price(intent, window, skip_filters)
        if m == "unit_sale_price_on_date":
            return self._unit_sale_price(intent, window, skip_filters)
        span = [window.start.isoformat(), window.end_exclusive.isoformat()]
        if m == "purchase_amount":
            # Checked before the sales predicates: a supplier name is not a sales column.
            pieces, piece_args = self._purchase_predicates(intent, skip_filters)
            where_sql = " AND ".join(["cancelled=0", "date>=?", "date<?", *pieces])
            row = self.db.execute(
                "SELECT SUM(a) FROM (SELECT COALESCE(MAX(receipt_total), SUM(COALESCE(amount, qty*unit_cost))) a "
                f"FROM purchases WHERE {where_sql} GROUP BY COALESCE(purchase_id, date))",
                span + piece_args).fetchone()
            return row[0]
        where, args, needs_lines = self._predicates(intent, skip_filters)
        period = "date>=? AND date<?"
        clause = " AND ".join(where + [period])
        sql_args = args + span
        if m == "order_total_due":
            # Rulebook §4: the total the customer owes for the order, after order-level discount, including shipping.
            if needs_lines:
                raise Unsupported("order total with a line filter")
            dues = [r[0] for r in self.db.execute(f"SELECT total_due FROM orders WHERE {VALID} AND {clause}", sql_args)]
            if dues and all(due is None for due in dues):
                raise Unsupported("no total_due")
            present = [due for due in dues if due is not None]
            return sum(present) if present else None
        if m == "no_sale_count":
            return self._no_sale_count(intent, window, skip_filters)
        source = "lines" if needs_lines or m in {"qty_base_sold", "gross_profit"} else "orders"
        if source == "lines" and not self.has_lines:
            raise Unsupported("line detail unavailable")
        amount = "amount" if source == "lines" else "revenue"
        expressions = {
            "revenue": f"SELECT SUM({amount}) FROM {source} WHERE {VALID} AND {clause}",
            "order_count": f"SELECT COUNT(DISTINCT order_id) FROM {source} WHERE {VALID} AND {clause}",
            "cancelled_count": f"SELECT COUNT(DISTINCT order_id) FROM {source} WHERE status='cancelled' AND {clause}",
            "avg_per_order": f"SELECT SUM({amount})*1.0/NULLIF(COUNT(DISTINCT order_id),0) FROM {source} WHERE {VALID} AND {clause}",
            "qty_base_sold": f"SELECT SUM(COALESCE(qty_base, qty)) FROM lines WHERE {VALID} AND {self.items_only} AND {clause}",
            "distinct_customers": f"SELECT COUNT(DISTINCT customer_key) FROM {source} WHERE {VALID} AND {clause}",
            "gross_profit": f"SELECT SUM(amount)-SUM(qty*line_cost) FROM lines WHERE {VALID} AND {clause}",
            "cancel_rate": (
                f"SELECT 100.0*COUNT(DISTINCT CASE WHEN status='cancelled' THEN order_id END)"
                f"/NULLIF(COUNT(DISTINCT order_id),0) FROM {source} WHERE {clause}"
            ),
        }
        if m not in expressions:
            raise Unsupported(f"measure {m}")
        if m == "gross_profit" and self.db.execute("SELECT 1 FROM lines WHERE line_cost IS NOT NULL LIMIT 1").fetchone() is None:
            raise Unsupported("no cost on sale lines")
        return self.db.execute(expressions[m], sql_args).fetchone()[0]

    def _format(self, measure: str, value: float | None) -> Answer:
        if value is None:
            raise Unsupported("empty result")
        if measure == "cancel_rate":
            return Answer("percent", _round2(value))
        if measure in MONEY:
            return Answer("number", _round_money(value))
        return Answer("number", _whole(value))

    def _ranked(self, intent: Intent, window: TimeWindow) -> list[tuple[Any, float]]:
        where, args, needs_lines = self._predicates(intent)
        dim = intent.group_by
        m = intent.measure
        if m in STATUS_MEASURES:
            self._require_order_status()
        if m == "cancelled_count":
            self._require_cancellations()
        if intent.shape == "argmax_date":
            key, label, line = "date", "date", False
        else:
            key = ORDER_DIMS.get(dim or "") or LINE_DIMS.get(dim or "")
            if key is None:
                raise Unsupported(f"group by {dim}")
            label = LABEL.get(dim or "", key)
            line = dim in LINE_DIMS
        if m == "orders_containing_product" and not line:
            raise Unsupported("orders containing a product need a product grouping")
        source = "lines" if line or needs_lines or m == "qty_base_sold" else "orders"
        if source == "lines" and not self.has_lines:
            raise Unsupported("line detail unavailable")
        amount = "amount" if source == "lines" else "revenue"
        score = {
            "revenue": f"SUM({amount})", "qty_base_sold": "SUM(COALESCE(qty_base, qty))",
            "order_count": "COUNT(DISTINCT order_id)", "gross_profit": "SUM(amount)-SUM(qty*line_cost)",
            "cancelled_count": "COUNT(DISTINCT order_id)", "orders_containing_product": "COUNT(DISTINCT order_id)",
        }.get(m)
        if score is None:
            raise Unsupported(f"ranking of {m}")
        status = "status='cancelled'" if m == "cancelled_count" else VALID
        if source == "lines" and dim in LINE_DIMS:
            where = where + [self.items_only]
        clause = " AND ".join(where + [status, "date>=? AND date<?", f"{key} IS NOT NULL"])
        rows = self.db.execute(
            f"SELECT {label}, {score} s, {key} k FROM {source} WHERE {clause} GROUP BY {key} ORDER BY s DESC, k ASC",
            args + [window.start.isoformat(), window.end_exclusive.isoformat()],
        ).fetchall()
        return [(r[0], r[1]) for r in rows if r[1] is not None]

    def _compare(self, intent: Intent, a: float | None, b: float | None) -> Answer:
        if a is None or b is None:
            raise Unsupported("empty period")
        if intent.compare_as == "percent":
            if not b:
                raise Unsupported("zero baseline")
            return Answer("percent", _round2(100.0 * (a - b) / b))
        return self._format(intent.measure, a - b)

    def _compare_members(self, intent: Intent, target: list[TimeWindow]) -> Answer:
        """Compare the two filters of one dimension when no comparison window is given.

        The filters keep the model's order: the first is compared against the second, which is the baseline of a
        percent. Rulebook §2 makes the morning shift the baseline when two shifts are compared, so the model must
        order that shift second. Filters on other dimensions apply to both members.
        """
        counts: dict[Any, int] = {}
        for item in intent.filters:
            if item.get("exclude") is not True:
                counts[item.get("dim")] = counts.get(item.get("dim"), 0) + 1
        dims = [dim for dim, n in counts.items() if n == 2]
        if len(target) != 1 or len(dims) != 1:
            raise Unsupported("no comparison window")
        members, others = self._members(intent, dims[0])
        if len(members) != 2:
            raise Unsupported("no comparison window")
        if dims[0] == "shift":
            # Rulebook §2: the morning shift is the baseline, whichever order the question names the shifts in.
            keys = [_shift_key(item["value"]) for item in members]
            if keys[0] == keys[1]:
                raise Unsupported("two shifts needed")
            if keys[1] != SHIFT_BASELINE:
                members = members[::-1]
        first =self.value(Intent(measure=intent.measure, filters=[*others, members[0]]), target[0])
        second = self.value(Intent(measure=intent.measure, filters=[*others, members[1]]), target[0])
        return self._compare(intent, first, second)

    def answer(self, intent: Intent, windows: list[TimeWindow]) -> Answer:
        shape = intent.shape
        if intent.threshold is not None and shape not in THRESHOLD_SHAPES:
            # A condition the shape does not apply to would be dropped silently; that is a wrong answer.
            raise Unsupported(f"threshold on shape {shape}")
        _check_age(intent)
        if intent.age_days is not None and shape in THRESHOLD_SHAPES:
            # The threshold path reads its own metric, so an age would be dropped silently.
            raise Unsupported(f"age on shape {shape}")
        target = [w for w in windows if w.role != "comparison"]
        other = [w for w in windows if w.role == "comparison"]
        if not target:
            raise Unsupported("no time window")
        window = target[0]
        if shape in THRESHOLD_SHAPES:
            if intent.threshold is None:
                raise Unsupported(f"{shape} needs a threshold")
            if intent.group_by or intent.top_k is not None:
                raise Unsupported(f"{shape} with grouping or top_k")
            if len(target) != 1 or other:
                raise Unsupported(f"{shape} needs one window")
            met, base = self._threshold(intent, window)
            if shape == "threshold_count":
                return Answer("number", met)
            if not base:
                raise Unsupported("no subject with a valid order in the window")
            return Answer("percent", _round2(100.0 * met / base))
        if shape == "scalar":
            if len(target) != 1 or other:
                raise Unsupported("scalar with several windows")
            return self._format(intent.measure, self.value(intent, window))
        if shape == "compare_periods":
            if not other:
                return self._compare_members(intent, target)
            return self._compare(intent, self.value(intent, window), self.value(intent, other[0]))
        if shape == "share_of_total":
            part, whole = self.value(intent, window), self.value(intent, window, skip_filters=True)
            if part is None or not whole:
                raise Unsupported("empty share")
            return Answer("percent", _round2(100.0 * part / whole))
        if shape in {"top_k", "argmax_entity", "argmax_date"}:
            rows = self._ranked(intent, window)
            if not rows:
                raise Unsupported("empty ranking")
            k = 1 if intent.top_k is None else intent.top_k
            if shape == "top_k" and k < 1:
                raise Unsupported("top_k below one")
            picked = rows[:k] if shape == "top_k" else rows[:1]
            if any(label is None for label, _ in picked):
                # A null name cannot be shown as an answer; a list of nulls would be a wrong answer.
                raise Unsupported("ranked entity has no name")
            if shape == "argmax_date":
                return Answer("date", picked[0][0])
            if shape == "top_k" and k >= 2:
                return Answer("list", [label for label, _ in picked])
            return Answer("string", picked[0][0])
        if shape == "argmax_period" and len(target) > 1:
            scored = [(self.value(intent, w), w) for w in target]
            if any(v is None for v, _ in scored):
                raise Unsupported("empty period")
            best = max(scored, key=lambda item: item[0])
            return Answer("date", best[1].start.isoformat()[:7])
        if shape == "breakdown" and len(target) > 1 and not intent.group_by:
            values = [self.value(intent, w) for w in target]
            if any(v is None for v in values):
                raise Unsupported("empty period")
            return Answer("list", [self._format(intent.measure, v).value for v in values])
        if shape == "breakdown" and intent.group_by and len(target) == 1:
            # One value per member the question names, in the order the filters give; each member keeps the other filters.
            members, others = self._members(intent, intent.group_by)
            if not members:
                raise Unsupported("breakdown names no member")
            values = [self.value(Intent(measure=intent.measure, filters=[*others, m]), window) for m in members]
            return Answer("list", [self._format(intent.measure, v).value for v in values])
        raise Unsupported(f"shape {shape}")


def as_of(value: Any) -> date:
    return date.fromisoformat(str(value)[:10])
