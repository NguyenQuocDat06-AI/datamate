"""Turn a typed intent into an answer on canonical tables. Definitions follow the rulebook."""

from __future__ import annotations

import operator
import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from bizreason.contracts import TimeWindow
from bizreason.semantic.vocabulary import (
    DEBT_METHOD_TOKENS, GENERIC_FILTER_WORDS, NON_DEBT_METHOD_TOKENS, SHIFT_BASELINE, SHIFT_WINDOWS,
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
# Measures ranked by supplier, read from the purchase receipts.
PURCHASE_RANK_MEASURES = {"purchase_amount", "purchase_receipt_count"}
# Measures that can be averaged per weekday (rulebook §2).
WEEKDAY_AVERAGE_MEASURES = {"order_count", "revenue"}
ORDER_DIMS = {
    "customer": "customer_key", "customer_group": "customer_group", "employee": "employee",
    "channel": "channel", "payment_method": "payment_method",
}
# Drug and device columns on the sale lines (rulebook section 6). A filter on one needs the column to exist.
LINE_DIMS = {
    "product": "product_key", "product_group": "category", "size": "product_name", "unit": "unit",
    "prescription": "prescription", "active_ingredient": "active_ingredient", "product_kind": "product_kind",
}
# The printed name of a ranked group: the best spelling among the group's rows (see _BestSpelling).
LABEL = {"product": "best_spelling(product_name)", "customer": "best_spelling(customer_name)", "employee": "employee"}
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
# Filter dimensions matched on the whole stored string only: no generic-word removal, no partial match.
EXACT_ONLY_DIMS = {"active_ingredient"}
# Filter dimensions where a partial match could name another customer or group.
NO_PARTIAL_DIMS = {"customer", "customer_group"}
# Prescription values: a token for "no" means 0, any other value means 1 (rulebook section 6). ASCII tokens.
NO_TOKENS = ("no", "khong")
# Weekday words as normalize_lookup writes them, with the SQL strftime('%w') number of the day (Sunday is 0).
WEEKDAY_TOKENS = {
    "thu 2": 1, "thu hai": 1, "monday": 1,
    "thu 3": 2, "thu ba": 2, "tuesday": 2,
    "thu 4": 3, "thu tu": 3, "wednesday": 3,
    "thu 5": 4, "thu nam": 4, "thursday": 4,
    "thu 6": 5, "thu sau": 5, "friday": 5,
    "thu 7": 6, "thu bay": 6, "saturday": 6,
    "chu nhat": 0, "cn": 0, "sunday": 0,
}
# The afternoon shift key (the other key of SHIFT_WINDOWS besides SHIFT_BASELINE, rulebook section 2).
SHIFT_AFTERNOON = "chieu"
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")
# Generic phrases, longest first, so that a phrase is removed whole.
_GENERIC_PHRASES = sorted(
    {tuple(normalize_lookup(word).split()) for word in GENERIC_FILTER_WORDS},
    key=lambda phrase: (-len(phrase), phrase),
)


class Unsupported(Exception):
    """The system cannot establish an answer; the caller abstains."""


def _diacritic_letters(text: str) -> int:
    """How many non-ASCII letters a spelling has, after composing marks into their letters."""
    return sum(1 for ch in unicodedata.normalize("NFC", text) if ch.isalpha() and not ch.isascii())


class _BestSpelling:
    """SQLite aggregate: the spelling to print for one name group.

    Among the distinct spellings of a group, the winner has the most letters with diacritics, then the most rows,
    then the lowest string. Null names are skipped, so a group with no name gives NULL and the caller abstains.
    """

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def step(self, value: Any) -> None:
        if value is not None:
            self.counts[str(value)] += 1

    def finalize(self) -> str | None:
        if not self.counts:
            return None
        return min(self.counts, key=lambda s: (-_diacritic_letters(s), -self.counts[s], s))


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
    # A second measure asked in the same question; answered as a list in the order asked.
    measure2: str | None = None
    # A place the question restricts to, such as a street.
    location: str | None = None


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


def _strip_generic(needle: str) -> str:
    """The normalized needle with generic words removed from its start and end. Never returns an empty string."""
    words = needle.split()
    changed = True
    while changed:
        changed = False
        for phrase in _GENERIC_PHRASES:
            size = len(phrase)
            if len(words) <= size:
                continue
            if tuple(words[:size]) == phrase:
                words = words[size:]
                changed = True
            elif tuple(words[-size:]) == phrase:
                words = words[:-size]
                changed = True
    return " ".join(words)


def _weekday_of(text: Any) -> int:
    """The SQL weekday number (Sunday is 0) named by a filter value: a day number 2 to 7, or a weekday name."""
    needle = normalize_lookup(text)
    if needle in {"2", "3", "4", "5", "6", "7"}:
        return int(needle) - 1
    padded = f" {needle} "
    days = {day for token, day in WEEKDAY_TOKENS.items() if f" {token} " in padded}
    if len(days) != 1:
        raise Unsupported(f"unknown weekday: {text}")
    return days.pop()


def _unpaid(paid: dict[Any, list[tuple[Any, Any]]], order_id: Any, due: Any, paid_by: Any) -> float | None:
    """Rulebook §4: the part of an order's total not yet paid by the payments that pass `paid_by`, never below 0."""
    if due is None:
        return None
    settled = sum(amount for paid_on, amount in paid.get(order_id, []) if amount is not None and paid_by(paid_on))
    return max(float(due) - settled, 0.0)


def _filter_column(dim: str) -> str:
    """The stored column that a filter's predicate reads; rows where it is null are kept by an exclusion."""
    if dim in {"product", "size"}:
        return "product_name"
    if dim == "shift":
        return "ts"
    if dim == "weekday":
        return "date"
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
    """Rulebook §4: an age only means something for the debt measures that take it.

    An age on a non-debt measure is an artefact of a window phrase, so it is ignored. An age on a debt measure
    that does not take one would change the answer, so it raises.
    """
    if intent.age_days is None or intent.measure not in DEBT_MEASURES:
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


# Shapes that a two-part question can be mislabelled as by the question parser.
TWO_PART_SHAPES = frozenset({"breakdown", "compare_periods"})


def _two_part_window(intent: Intent, windows: list[TimeWindow]) -> TimeWindow | None:
    """The single window of a two-part question, or None when grouping, a threshold or a comparison rules it out."""
    if intent.shape not in TWO_PART_SHAPES or intent.group_by is not None or intent.top_k is not None:
        return None
    if intent.threshold is not None or len(windows) != 1 or windows[0].role == "comparison":
        return None
    return windows[0]


def _repeated_dims(filters: list[dict[str, Any]]) -> dict[Any, list[int]]:
    """Positions of the non-excluded filters per dimension, keeping only the dimensions that appear more than once."""
    positions: dict[Any, list[int]] = {}
    for index, item in enumerate(filters):
        if item.get("exclude") is True:
            continue
        positions.setdefault(item.get("dim"), []).append(index)
    return {dim: where for dim, where in positions.items() if len(where) > 1}


def _as_scalar_pair(intent: Intent, windows: list[TimeWindow]) -> Intent:
    """The parser mislabels the shape of a two-measure question, so one window with two measures is a scalar pair."""
    if intent.measure2 is None or intent.measure2 == intent.measure:
        return intent
    # A compare of two different measures may ask for their difference, so only a breakdown becomes a pair.
    if intent.shape != "breakdown":
        return intent
    if _two_part_window(intent, windows) is None or _repeated_dims(intent.filters):
        return intent
    return replace(intent, shape="scalar")


def _same_measure_pair(intent: Intent, windows: list[TimeWindow]) -> tuple[TimeWindow, list[Intent]] | None:
    """The parser mislabels two members of one dimension as a compare; each member is answered as its own scalar."""
    if intent.measure2 != intent.measure:
        return None
    window = _two_part_window(intent, windows)
    if window is None:
        return None
    repeated = _repeated_dims(intent.filters)
    if len(repeated) != 1 or len(next(iter(repeated.values()))) != 2:
        return None
    first, second = next(iter(repeated.values()))
    # The member for the first filter drops the second filter, and the other way round; the order of the filters is kept.
    members = [
        replace(intent, measure2=None, shape="scalar", filters=[f for i, f in enumerate(intent.filters) if i != drop])
        for drop in (second, first)
    ]
    return window, members


def _asks_weekday_average(intent: Intent) -> bool:
    """True when the intent asks the average per weekday of a measure that has one."""
    return intent.compare_as == "average_per_weekday" and intent.measure in WEEKDAY_AVERAGE_MEASURES


class Compiler:
    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.db.create_function("clock_of", 1, _clock_of)
        self.db.create_function("name_head", 1, _name_head)
        self.db.create_aggregate("best_spelling", 1, _BestSpelling)
        self.has_lines = self.db.execute("SELECT 1 FROM lines LIMIT 1").fetchone() is not None
        self.line_columns = {row[1] for row in self.db.execute("PRAGMA table_info(lines)")}
        self.order_columns = {row[1] for row in self.db.execute("PRAGMA table_info(orders)")}
        # Rulebook §3: a unit filter means base units. Only used when the base unit is recorded on the lines.
        self.base_unit = "base_unit" in self.line_columns and self.db.execute(
            "SELECT 1 FROM lines WHERE base_unit IS NOT NULL LIMIT 1"
        ).fetchone() is not None
        # Rulebook §3: a service charge is revenue but not a product that can be ranked or counted.
        self.items_only = "is_item=1" if "is_item" in self.line_columns else "1=1"
        self.price_expr = (
            "COALESCE(unit_price, amount*1.0/qty)" if "unit_price" in self.line_columns else "amount*1.0/qty"
        )
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

    def _require_line_column(self, column: str) -> None:
        if not self.has_lines or column not in self.line_columns:
            raise Unsupported(f"{column} unavailable")

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

    @staticmethod
    def _match(dim: str, values: list[Any], needle: str, partial: bool = True) -> list[Any]:
        """Stored values a filter names: exact, then exact after generic words are removed, then one partial match.

        A partial match is used only when it yields exactly one stored value.
        """
        if not needle:
            return []
        exact = [v for v in values if normalize_lookup(v) == needle]
        if exact or dim in EXACT_ONLY_DIMS:
            return exact
        stripped = _strip_generic(needle)
        if stripped != needle:
            found = [v for v in values if normalize_lookup(v) == stripped]
            if found:
                return found
        if partial and dim not in NO_PARTIAL_DIMS:
            loose = [v for v in values if needle in normalize_lookup(v)]
            if len(loose) == 1:
                return loose
        return []

    def _stored_matches(
        self, table: str, source: str, dim: str, needle: str, text: str, partial: bool = True,
    ) -> list[Any]:
        values = [r[0] for r in self.db.execute(f"SELECT DISTINCT {source} FROM {table} WHERE {source} IS NOT NULL")]
        matched = self._match(dim, values, needle, partial)
        if not matched:
            raise Unsupported(f"unresolved {dim}: {text}")
        return matched

    def _customer_predicate(self, text: str) -> tuple[str, list[Any], bool]:
        """Rulebook §3. A customer is a code when the shop has codes, otherwise the name written on the order.

        Matches customer_key or customer_name. One name that belongs to two or more codes is ambiguous.
        """
        needle = normalize_lookup(text)
        name_expr = "customer_name" if "customer_name" in self.order_columns else "NULL"
        pairs = self.db.execute(f"SELECT DISTINCT customer_key, {name_expr} FROM orders").fetchall()
        keys = sorted({key for key, _ in pairs if key is not None})
        names = sorted({name for _, name in pairs if name is not None})
        if not keys:
            matched = self._match("customer", names, needle)
            if not matched:
                raise Unsupported(f"unresolved customer: {text}")
            return f"customer_name IN ({','.join('?' for _ in matched)})", list(matched), False
        found = set(self._match("customer", keys, needle))
        for name in self._match("customer", names, needle):
            owners = {key for key, owner in pairs if owner == name and key is not None}
            if len(owners) > 1:
                raise Unsupported(f"ambiguous customer name: {text}")
            found |= owners
        if not found:
            raise Unsupported(f"unresolved customer: {text}")
        ordered = sorted(found)
        return f"customer_key IN ({','.join('?' for _ in ordered)})", ordered, False

    def _columns(self, table: str) -> set[str]:
        return {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}

    def resolve_filter(self, dim: str, text: str, on: str | None = None) -> tuple[str, list[Any], bool]:
        """Return a SQL predicate for a filter, or raise when the value is not in the data.

        `on="purchases"` resolves a product or supplier name against the purchase rows instead of the sales lines.
        """
        if dim == "shift":
            if on is not None:
                raise Unsupported(f"shift filter on {on}")
            return self._shift_filter(text)
        if dim == "weekday":
            if on is not None:
                raise Unsupported(f"weekday filter on {on}")
            return "(strftime('%w', date) = ?)", [str(_weekday_of(text))], False
        if dim == "supplier" and on != "purchases":
            raise Unsupported("supplier filter outside purchases")
        column = ORDER_DIMS.get(dim) or LINE_DIMS.get(dim)
        if column is None and dim != "supplier":
            raise Unsupported(f"dimension {dim}")
        if on == "purchases" and dim not in {"product", "supplier"}:
            raise Unsupported(f"purchase filter on {dim}")
        if dim == "customer":
            return self._customer_predicate(text)
        needle = normalize_lookup(text)
        if dim == "size":
            token = needle.split()[-1] if needle else ""
            if not token:
                raise Unsupported("size value")
            return "(name_head(product_name) LIKE ?)", [f"% {token}"], True
        if dim == "prescription":
            self._require_line_column("prescription")
            if not needle:
                raise Unsupported("prescription value")
            padded = f" {needle} "
            return "(prescription = ?)", [0 if any(f" {token} " in padded for token in NO_TOKENS) else 1], True
        if on == "purchases":
            table = "purchases"
            source = self._supplier_column() if dim == "supplier" else "product_name"
            matched = self._stored_matches(table, source, dim, needle, text)
            return f"{source} IN ({','.join('?' for _ in matched)})", list(matched), False
        if dim == "unit" and self.base_unit:
            # Exact match only after generic words are removed: a partial unit name could be a different unit.
            matched = self._stored_matches("lines", "base_unit", dim, needle, text, partial=False)
            return f"base_unit IN ({','.join('?' for _ in matched)})", list(matched), True
        table = "lines" if column in LINE_DIMS.values() else "orders"
        line = table == "lines"
        if line:
            if not self.has_lines:
                raise Unsupported("line detail unavailable")
            source = "product_name" if dim == "product" else column
        else:
            source = column
        available = self.line_columns if line else self.order_columns
        if source not in available:
            raise Unsupported(f"{dim} unavailable")
        raw = f"{source}_raw"
        if raw in self._columns(table):
            # Values were regrouped; a question may still use any spelling found in the source.
            try:
                matched = self._stored_matches(table, source, dim, needle, text)
            except Unsupported:
                spellings = self._stored_matches(table, raw, dim, needle, text)
                slots = ",".join("?" for _ in spellings)
                matched = [row[0] for row in self.db.execute(
                    f"SELECT DISTINCT {source} FROM {table} WHERE {raw} IN ({slots}) AND {source} IS NOT NULL",
                    list(spellings))]
                if not matched:
                    raise
        else:
            matched = self._stored_matches(table, source, dim, needle, text)
        return f"{source} IN ({','.join('?' for _ in matched)})", list(matched), line

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

    def _payment_index(self) -> dict[Any, list[tuple[Any, Any]]]:
        """Payments grouped by order as (paid_on, amount). Raises when the shop has no payment record."""
        try:
            payment_rows = self.db.execute("SELECT order_id, date, amount FROM payments").fetchall()
        except sqlite3.OperationalError as exc:
            raise Unsupported("no payments table") from exc
        if not payment_rows:
            raise Unsupported("no payments")
        paid: dict[Any, list[tuple[Any, Any]]] = {}
        for order_id, paid_on, amount in payment_rows:
            paid.setdefault(order_id, []).append((paid_on, amount))
        return paid

    @staticmethod
    def _owed(
        orders: list[tuple[Any, Any, Any, Any, bool]], paid: dict[Any, list[tuple[Any, Any]]],
        window: TimeWindow, age_days: int | None,
    ) -> list[tuple[Any, float | None]]:
        """(customer, unpaid part at the end of the window) for each order placed up to the window's last day."""
        end = window.end_exclusive.isoformat()
        # Rulebook §4: age counts days from the order date to the last day of the window. An order exactly
        # age_days old is not older than age_days, so it is left out.
        age_limit = None
        if age_days is not None:
            age_limit = (window.end_exclusive - timedelta(days=1 + age_days)).isoformat()
        return [
            (customer, _unpaid(paid, order_id, due, lambda d: d is not None and d < end))
            for order_id, customer, day, due, _ in orders
            if day is not None and day < end and (age_limit is None or day < age_limit)
        ]

    def _debt(self, intent: Intent, window: TimeWindow, skip_filters: bool) -> float | None:
        """Rulebook §4. The unpaid part of an order is its total_due less payments dated up to the cutoff, never below 0."""
        paid = self._payment_index()
        orders = self._debt_orders(intent, skip_filters)
        if orders and all(due is None for _, _, _, due, _ in orders):
            raise Unsupported("no total_due")
        start, end = window.start.isoformat(), window.end_exclusive.isoformat()
        m = intent.measure
        if m in {"debt_outstanding", "debtor_count"}:
            owed = self._owed(orders, paid, window, intent.age_days)
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
                total += _unpaid(paid, order_id, due, lambda d: d is not None and d == day) or 0.0
            return total
        raise Unsupported(f"measure {m}")

    def _customer_labels(self) -> dict[Any, Any] | None:
        """A display name per customer code, or None when the orders carry no name column."""
        if "customer_name" not in self.order_columns:
            return None
        rows = self.db.execute(
            "SELECT customer_key, best_spelling(customer_name) FROM orders WHERE customer_key IS NOT NULL GROUP BY customer_key"
        ).fetchall()
        return dict(rows)

    def _ranked_debtors(self, intent: Intent, window: TimeWindow) -> list[tuple[Any, float]]:
        """Rulebook §4 and §3: customers by the debt still owed at the window's last day, largest first."""
        paid = self._payment_index()
        orders = self._debt_orders(intent, skip_filters=False)
        if orders and all(due is None for _, _, _, due, _ in orders):
            raise Unsupported("no total_due")
        totals: dict[Any, float] = {}
        for customer, remaining in self._owed(orders, paid, window, intent.age_days):
            # Walk-in orders (no customer code) are not a row of the debt table.
            if customer is not None and remaining is not None:
                totals[customer] = totals.get(customer, 0.0) + remaining
        owing = sorted(((c, t) for c, t in totals.items() if t > 0), key=lambda item: (-item[1], item[0]))
        labels = self._customer_labels()
        return [((labels.get(c) if labels is not None else c), t) for c, t in owing]

    def _ranked_suppliers(self, intent: Intent, window: TimeWindow) -> list[tuple[Any, float]]:
        """Rulebook §5: suppliers by import money (per receipt, as in value) or by number of receipts."""
        column = self._supplier_column()
        pieces, piece_args = self._purchase_predicates(intent, False)
        where = " AND ".join(["cancelled=0", "date>=?", "date<?", f"{column} IS NOT NULL", *pieces])
        span = [window.start.isoformat(), window.end_exclusive.isoformat()]
        if intent.measure == "purchase_amount":
            sql = (
                f"SELECT k, SUM(a) s FROM (SELECT {column} k, "
                "COALESCE(MAX(receipt_total), SUM(COALESCE(amount, qty*unit_cost))) a "
                f"FROM purchases WHERE {where} GROUP BY {column}, COALESCE(purchase_id, date)) "
                "GROUP BY k ORDER BY s DESC, k ASC"
            )
        else:
            sql = (
                f"SELECT {column} k, COUNT(DISTINCT purchase_id) s FROM purchases WHERE {where} "
                f"GROUP BY {column} ORDER BY s DESC, k ASC"
            )
        rows = self.db.execute(sql, span + piece_args).fetchall()
        return [(r[0], r[1]) for r in rows if r[1] is not None]

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

    def _delivery_trips(self, intent: Intent, window: TimeWindow, skip_filters: bool) -> int:
        """Rulebook section 6 (construction materials): distinct (order, trip) pairs dated in the window.

        The order status does not matter. No filter can be applied to the delivery rows, so a filter raises.
        """
        if not skip_filters and intent.filters:
            raise Unsupported("delivery trips with a filter")
        try:
            total = self.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        except sqlite3.OperationalError as exc:
            raise Unsupported("no deliveries table") from exc
        if not total:
            raise Unsupported("no deliveries")
        row = self.db.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT order_id, trip FROM deliveries WHERE date>=? AND date<?)",
            [window.start.isoformat(), window.end_exclusive.isoformat()],
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
        if m == "delivery_trip_count":
            return self._delivery_trips(intent, window, skip_filters)
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
        dim = intent.group_by
        m = intent.measure
        if m in PURCHASE_RANK_MEASURES:
            if dim != "supplier":
                raise Unsupported("purchase ranking needs supplier grouping")
            return self._ranked_suppliers(intent, window)
        if dim == "supplier":
            raise Unsupported("supplier grouping needs a purchase measure")
        if m == "debt_outstanding":
            if dim != "customer":
                raise Unsupported("debt ranking needs customer grouping")
            return self._ranked_debtors(intent, window)
        where, args, needs_lines = self._predicates(intent)
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
            if line and key not in self.line_columns:
                raise Unsupported(f"{dim} unavailable")
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
        first = self.value(Intent(measure=intent.measure, filters=[*others, members[0]]), target[0])
        second = self.value(Intent(measure=intent.measure, filters=[*others, members[1]]), target[0])
        return self._compare(intent, first, second)

    def _compare_shift(self, intent: Intent, window: TimeWindow, item: dict[str, Any]) -> Answer:
        """Rulebook §2: one named shift against the other shift. The morning shift is always the baseline.

        The named shift is validated; the comparison is afternoon against morning, so the percent is the change
        from morning to afternoon, and the value mode is afternoon minus morning.
        """
        if item.get("exclude") is True:
            raise Unsupported("shift exclusion has no comparison")
        _shift_key(item["value"])
        others = [f for f in intent.filters if f.get("dim") != "shift"]
        dims = [f.get("dim") for f in others if f.get("exclude") is not True]
        if len(dims) != len(set(dims)):
            raise Unsupported("comparison with several members of one dimension")
        morning = self.value(
            Intent(measure=intent.measure, filters=[*others, {"dim": "shift", "value": SHIFT_BASELINE}],
                   age_days=intent.age_days),
            window,
        )
        afternoon = self.value(
            Intent(measure=intent.measure, filters=[*others, {"dim": "shift", "value": SHIFT_AFTERNOON}],
                   age_days=intent.age_days),
            window,
        )
        return self._compare(intent, afternoon, morning)

    def _weekday_days(self, intent: Intent, window: TimeWindow) -> int:
        """Rulebook §2: how many days of the named weekday the window holds, days without orders included."""
        days: set[int] = set()
        first = True
        for item in intent.filters:
            if item.get("dim") != "weekday":
                continue
            if item.get("exclude") is True:
                raise Unsupported("weekday exclusion in an average")
            day = _weekday_of(item["value"])
            days = {day} if first else days & {day}
            first = False
        count = 0
        cursor = window.start
        while cursor < window.end_exclusive:
            if cursor.isoweekday() % 7 in days:
                count += 1
            cursor += timedelta(days=1)
        if not count:
            raise Unsupported("no such weekday in the window")
        return count

    def _scalar(self, intent: Intent, window: TimeWindow) -> float | None:
        """One value. A weekday filter only filters, unless the average per weekday is asked for."""
        has_weekday = any(item.get("dim") == "weekday" for item in intent.filters)
        if _asks_weekday_average(intent) and not has_weekday:
            # The average needs a weekday to divide by; the plain total would answer a different question.
            raise Unsupported("average per weekday without a weekday")
        value = self.value(intent, window)
        if _asks_weekday_average(intent):
            if value is None:
                return None
            return value / self._weekday_days(intent, window)
        return value

    def table(self, intent: Intent, windows: list[TimeWindow]) -> Answer:
        """The top-k ranking with the value of each entity beside its name, in rank order.

        The values are formatted as the scalar answers of the measure are. Column names are generic; the
        scorer compares a table by position when its column names differ from the reference.
        """
        if intent.shape != "top_k" or intent.group_by is None:
            raise Unsupported("table needs a top_k with grouping")
        if intent.location is not None or intent.threshold is not None:
            raise Unsupported("table with a location or threshold")
        if intent.measure2 not in (None, intent.measure):
            raise Unsupported("table with a second measure")
        _check_age(intent)
        target = [w for w in windows if w.role != "comparison"]
        if not target:
            raise Unsupported("no time window")
        k = 1 if intent.top_k is None else intent.top_k
        if k < 1:
            raise Unsupported("top_k below one")
        picked = self._ranked(intent, target[0])[:k]
        if not picked:
            raise Unsupported("empty ranking")
        if any(label is None for label, _ in picked):
            raise Unsupported("ranked entity has no name")
        rows = [[label, self._format(intent.measure, value).value] for label, value in picked]
        return Answer("table", {"columns": ["name", "value"], "rows": rows})

    def answer(self, intent: Intent, windows: list[TimeWindow]) -> Answer:
        shape = intent.shape
        if intent.location is not None:
            # A place has no column in the canonical tables; dropping it would answer a wider question.
            raise Unsupported("location filter has no column")
        if intent.threshold is not None and shape not in THRESHOLD_SHAPES:
            # A condition the shape does not apply to would be dropped silently; that is a wrong answer.
            raise Unsupported(f"threshold on shape {shape}")
        _check_age(intent)
        if intent.age_days is not None and shape in THRESHOLD_SHAPES:
            # The threshold path reads its own metric, so an age would be dropped silently.
            raise Unsupported(f"age on shape {shape}")
        pair = _same_measure_pair(intent, windows)
        if pair is not None:
            window, members = pair
            values = [self._scalar(member, window) for member in members]
            if any(value is None for value in values):
                raise Unsupported("member value missing")
            return Answer("list", [self._format(intent.measure, value).value for value in values])
        intent = _as_scalar_pair(intent, windows)
        shape = intent.shape
        if intent.measure2 is not None and shape != "scalar":
            raise Unsupported(f"second measure on shape {shape}")
        if _asks_weekday_average(intent) and shape != "scalar":
            # Only the scalar path divides by the weekday count; any other shape would answer the total instead.
            raise Unsupported(f"average per weekday on shape {shape}")
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
            first = self._format(intent.measure, self._scalar(intent, window))
            if intent.measure2 is None:
                return first
            second_intent = replace(intent, measure=intent.measure2, measure2=None)
            second = self._format(intent.measure2, self._scalar(second_intent, window))
            return Answer("list", [first.value, second.value])
        if shape == "compare_periods":
            if other:
                return self._compare(intent, self.value(intent, window), self.value(intent, other[0]))
            shifts = [item for item in intent.filters if item.get("dim") == "shift"]
            if len(target) == 1 and len(shifts) == 1:
                return self._compare_shift(intent, window, shifts[0])
            return self._compare_members(intent, target)
        if shape == "share_of_total":
            part, whole = self.value(intent, window), self.value(intent, window, skip_filters=True)
            if part is None or not whole:
                raise Unsupported("empty share")
            return Answer("percent", _round2(100.0 * part / whole))
        if shape == "trend_check":
            # Each window must be strictly above the one before it; a window the question did not ask for would change it.
            if other or intent.group_by or intent.top_k is not None:
                raise Unsupported("trend check with a comparison, grouping or top_k")
            if len(target) < 2:
                raise Unsupported("trend check needs two windows")
            values = [self.value(intent, w) for w in target]
            if any(v is None for v in values):
                raise Unsupported("empty period")
            return Answer("boolean", all(later > earlier for earlier, later in zip(values, values[1:])))
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
            if len(members) == 1:
                # A breakdown of several members answered with one value would be a different question.
                raise Unsupported("breakdown names one member")
            values = [self.value(Intent(measure=intent.measure, filters=[*others, m]), window) for m in members]
            return Answer("list", [self._format(intent.measure, v).value for v in values])
        raise Unsupported(f"shape {shape}")


def as_of(value: Any) -> date:
    return date.fromisoformat(str(value)[:10])
