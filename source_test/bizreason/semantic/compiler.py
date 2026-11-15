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
from bizreason.semantic import debt_sources, rulebook_params, stock_sources
from bizreason.semantic.vocabulary import GENERIC_FILTER_WORDS, LOT_MEASURES, STOCK_MEASURES
from bizreason.semantic.vocabulary import RULE_DEVICE_LINE
from bizreason.semantic import value_resolver
from bizreason.utils import normalize_lookup


def _status_sql(column: str, statuses: tuple[str, ...], *, negate: bool = False) -> str:
    """SQL that tests a set of canonical order statuses. One status reads as '=' and several as IN, the forms the
    SQL has always had, so the default sets give the same text as before the rulebook could change them."""
    if not statuses or any(s not in rulebook_params.CANONICAL_ORDER_STATUSES for s in statuses):
        raise ValueError(f"not canonical order statuses: {statuses!r}")
    if len(statuses) == 1:
        return f"{column} <> '{statuses[0]}'" if negate else f"{column}='{statuses[0]}'"
    quoted = ",".join(f"'{s}'" for s in statuses)
    return f"{column} {'NOT IN' if negate else 'IN'} ({quoted})"


def _valid_sale_sql() -> str:
    """Rulebook §1: the orders that count as sales, from the active rulebook parameters."""
    return _status_sql("status", rulebook_params.active().valid_sale_statuses)


def _cancelled_sql() -> str:
    """Rulebook §1: the status of a cancelled order, from the active rulebook parameters."""
    return _status_sql("status", rulebook_params.active().cancelled_statuses)


MONEY = {
    "revenue", "avg_per_order", "purchase_amount", "gross_profit", "debt_outstanding", "debt_repaid", "debt_new",
    "unit_purchase_price", "unit_sale_price_on_date", "order_total_due", "net_profit",
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
    # Rulebook §3 (giờ cao điểm): the whole hour the order was created in.
    "hour": "CAST(strftime('%H', ts) AS INTEGER)",
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
# Dimensions whose filter text may carry extra words around the stored name (a unit word such as the dish or
# bottle counter, a trailing particle) or only some of its words; see _word_match.
WORD_MATCH_DIMS = {"product", "product_group"}
# Dimensions where a fuzzy (typo / word order) match is tried after every exact step failed; never customers.
FUZZY_MATCH_DIMS = {"product", "product_group", "employee", "supplier"}
# Prescription values: a token for "no" means 0, any other value means 1 (rulebook section 6). ASCII tokens.
NO_TOKENS = ("no", "khong")
# Prescription values that say "yes"; a value with neither a yes nor a no token is not a prescription value.
YES_TOKENS = ("yes", "co", "ke don", "theo don", "can don")
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
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")
# Generic phrases, longest first, so that a phrase is removed whole.
_GENERIC_PHRASES = sorted(
    {tuple(normalize_lookup(word).split()) for word in GENERIC_FILTER_WORDS},
    key=lambda phrase: (-len(phrase), phrase),
)



def _word_match(values: list[Any], needle: str) -> list[Any]:
    """Last-resort match on whole words, used only when it names exactly one stored value.

    1. The longest stored value written as whole words inside the filter text ("so chai <name> 30ml" -> "<name>
       30ml"), when no other stored value of the same length is.
    2. Otherwise the one stored value that contains every word of the filter text, in any order.
    """
    padded = f" {needle} "
    inside = [v for v in values if (n := normalize_lookup(v)) and f" {n} " in padded]
    if inside:
        longest = max(len(normalize_lookup(v)) for v in inside)
        best = [v for v in inside if len(normalize_lookup(v)) == longest]
        return _all_spellings(values, best) if _one_name(best) else []
    words = set(needle.split())
    covering = [v for v in values if words and words <= set(normalize_lookup(v).split())]
    if _one_name(covering):
        return _all_spellings(values, covering)
    # A device line names every capacity of it ("dòng máy" = the name before "…GB"): the stored names that are the
    # filter text followed only by a capacity are one line.
    if not rulebook_params.mentions(*RULE_DEVICE_LINE):
        return []
    line = [v for v in values if re.fullmatch(rf"{re.escape(needle)} \d+ ?(?:gb|tb)(?: .*)?", normalize_lookup(v))]
    return line


def _name_key(value: Any) -> str:
    """A stored name compared without spaces, so "30ml" and "30 ml" are one spelling."""
    return normalize_lookup(value).replace(" ", "")


LIST_CELL_SHARE = 0.3


def _list_cell_match(values: list[Any], needle: str) -> list[Any]:
    """Cells that list several items ("a, b + c"): when such cells are common in the column, a filter names every
    cell that has one item containing the filter text as whole words. When some item is exactly the filter text (equal
    after _name_key), only the cells with such an item are named: an item that merely contains it is another name
    ("matcha latte" is not "latte")."""
    if not values or not needle:
        return []
    listed = [v for v in values if re.search(r"[,;+]", str(v))]
    if len(listed) / len(values) < LIST_CELL_SHARE:
        return []
    padded = f" {needle} "
    key = needle.replace(" ", "")
    exact = [v for v in values if any(_name_key(part) == key for part in re.split(r"[,;+]", str(v)))]
    if exact:
        return exact
    return [v for v in values if any(padded in f" {normalize_lookup(part)} " for part in re.split(r"[,;+]", str(v)))]


def _one_name(values: list[Any]) -> bool:
    """True when the stored values are one name written one or more ways (equal after _name_key)."""
    return bool(values) and len({_name_key(v) for v in values}) == 1


def _all_spellings(values: list[Any], chosen: list[Any]) -> list[Any]:
    """Every stored value written like the one chosen name."""
    keys = {_name_key(v) for v in chosen}
    return [v for v in values if _name_key(v) in keys]


def _uses_customers(intent: Intent) -> bool:
    """The intent counts, ranks, filters or thresholds on individual customers."""
    return (
        "distinct_customers" in (intent.measure, intent.measure2)
        or intent.group_by == "customer"
        or (intent.threshold or {}).get("subject") == "customer"
        or any(item.get("dim") == "customer" for item in intent.filters)
    )

COLUMN_OPS = ("contains", "not_contains", "eq", "nonempty", "after_today", "before_today", "on_or_before_today",
              "on_or_after_today", "in_period")
COLUMN_MEASURE_OPS = ("sum", "count", "count_distinct")


class Unsupported(Exception):
    """The system cannot establish an answer; the caller abstains."""


class NotRecorded(Unsupported):
    """The shop does not record the kind of data the question needs: the question is unanswerable (rulebook §7),
    which is a different thing from a value of 0 and from a failure to compute."""


# Filter dimensions with a short closed list of stored values: a value that matches none of them is not recorded.
CLOSED_DIMS = {"customer_group", "channel", "payment_method"}
# Purchase measures: no receipt in the window means the shop recorded no purchase there (rulebook §7), not 0.
PURCHASE_MEASURES = {"purchase_amount", "unit_purchase_price"}


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
    # Rankings only: an entity needs at least this many valid orders in the window to be ranked (a rulebook term).
    min_orders: int | None = None
    # A measure on any kept column: {"op": "sum|count|count_distinct", "column": name} (see column_catalog).
    column_measure: dict[str, Any] | None = None
    # Conditions on any kept column: [{"column": name, "op": COLUMN_OPS, "value": ...}].
    column_filters: list[dict[str, Any]] = field(default_factory=list)


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
    keys = [key for key in rulebook_params.active().shift_windows if key in needle]
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
    if intent.measure2 != intent.measure and not (intent.measure2 is None and intent.shape == "breakdown"):
        return None
    repeated = _repeated_dims(intent.filters)
    if intent.group_by is not None and list(repeated) == [intent.group_by]:
        # Grouping by the very dimension the two filters name adds nothing: the answer is the two members.
        intent = replace(intent, group_by=None)
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
        self.db.create_function("lookup", 1, lambda v: None if v is None else normalize_lookup(str(v)))
        self.as_of: date | None = None
        self._period: tuple[str, str] | None = None
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
                f"SELECT 1 FROM orders WHERE {_cancelled_sql()} LIMIT 1"
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
        start, end = rulebook_params.active().shift_windows[key]
        return "(clock_of(ts) >= ? AND clock_of(ts) <= ?)", [start, end], False

    @staticmethod
    def _match(dim: str, values: list[Any], needle: str, partial: bool = True) -> list[Any]:
        """Stored values a filter names: exact, then exact after generic words are removed, then one partial match.

        A partial match is used only when it yields exactly one stored value.
        """
        if not needle:
            return []
        if partial and dim in WORD_MATCH_DIMS:
            cells = _list_cell_match(values, needle)
            if cells:
                # A column of item lists: a filter names every cell that lists the item.
                return cells
        exact = [v for v in values if normalize_lookup(v) == needle]
        if exact and dim in WORD_MATCH_DIMS:
            # Every spelling of the same name ("100ml" and "100 ml") is the same product or group.
            return _all_spellings(values, exact)
        if exact or dim in EXACT_ONLY_DIMS:
            return exact
        stripped = _strip_generic(needle)
        if stripped != needle:
            found = [v for v in values if normalize_lookup(v) == stripped]
            if found:
                return found
        if partial and dim not in NO_PARTIAL_DIMS:
            loose = [v for v in values if needle in normalize_lookup(v)]
            if _one_name(loose):
                return _all_spellings(values, loose)
        if partial and dim in WORD_MATCH_DIMS:
            found = _word_match(values, needle)
            if found:
                return found
            found = _list_cell_match(values, needle)
            if found:
                return found
        if partial and dim in FUZZY_MATCH_DIMS:
            # Last resort: a misspelt, unaccented or reordered name that clearly names one stored name.
            return value_resolver.resolve(needle, values, dim) or []
        return []

    def _stored_matches(
        self, table: str, source: str, dim: str, needle: str, text: str, partial: bool = True,
    ) -> list[Any]:
        values = [r[0] for r in self.db.execute(f"SELECT DISTINCT {source} FROM {table} WHERE {source} IS NOT NULL")]
        matched = self._match(dim, values, needle, partial)
        if not matched:
            if dim in CLOSED_DIMS and values:
                raise NotRecorded(f"not recorded {dim}: {text}")
            if dim in CLOSED_DIMS and table == "orders" and self._column_empty(table, source):
                # The column exists but no order has a value (as for the customer group): the shop records none.
                raise NotRecorded(f"no {dim} recorded: {text}")
            if dim == "product_group" and not values:
                # The shop records no product group at all: a question about one is unanswerable (rulebook §7).
                raise NotRecorded(f"no product group recorded: {text}")
            if table == "purchases" and dim == "product" and self.has_lines and "product_name" in self.line_columns:
                sold = [r[0] for r in self.db.execute(
                    "SELECT DISTINCT product_name FROM lines WHERE product_name IS NOT NULL")]
                if self._match(dim, sold, needle, partial):
                    # The shop sells this product but records no purchase of it: no data, not 0 (rulebook §7).
                    raise NotRecorded(f"no purchase recorded for product: {text}")
            raise Unsupported(f"unresolved {dim}: {text}")
        return matched

    def _column_empty(self, table: str, column: str) -> bool:
        """True when the table has rows and none of them has a value in the column."""
        if self.db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is None:
            return False
        return self.db.execute(f"SELECT 1 FROM {table} WHERE {column} IS NOT NULL LIMIT 1").fetchone() is None

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
            if any(f" {token} " in padded for token in NO_TOKENS):
                return "(prescription = ?)", [0], True
            if any(f" {token} " in padded for token in YES_TOKENS):
                return "(prescription = ?)", [1], True
            raise Unsupported(f"prescription value: {text}")
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
        included: dict[str, int] = {}
        if not skip:
            for item in intent.filters:
                try:
                    piece, values, line = self.resolve_filter(item["dim"], item["value"])
                except Unsupported as original:
                    # A group the shop does not have may be one of its product kinds (new / used / accessory).
                    if item["dim"] != "product_group" or "product_kind" not in self.line_columns:
                        raise
                    try:
                        piece, values, line = self.resolve_filter("product_kind", item["value"])
                    except Unsupported:
                        raise original from None
                if item.get("exclude") is True:
                    column = "base_unit" if item["dim"] == "unit" and self.base_unit else _filter_column(item["dim"])
                    piece = f"({column} IS NULL OR NOT ({piece}))"
                    sql.append(piece)
                elif item["dim"] != "product" and included.get(item["dim"]) == len(sql) - 1:
                    # Two members of one dimension in a single value ("máy mới và máy cũ"): either one, not both.
                    at = included[item["dim"]]
                    sql[at] = f"({sql[at]} OR {piece})"
                else:
                    included[item["dim"]] = len(sql)
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
        if subject == "order" and metric == "delivery_trips":
            # Rulebook (giao nhiều đợt): delivery trips of the order made up to the end of today.
            if self.as_of is None or "trip" not in self._table_columns("deliveries"):
                raise Unsupported("delivery trips unavailable")
            today = self.as_of.isoformat()
            metrics = {**metrics, "delivery_trips": (
                "MAX((SELECT COUNT(DISTINCT COALESCE(d.trip, d.date)) FROM deliveries d "
                f"WHERE d.order_id = orders.order_id AND (d.date IS NULL OR d.date <= '{today}')))")}
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
        clause = " AND ".join(where + [_valid_sale_sql(), "date>=? AND date<?", f"{key} IS NOT NULL"])
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
            # "khách mua chịu" as a payment-method filter names the debt orders these measures already read.
            debt_tokens = rulebook_params.active().debt_method_tokens
            kept = [
                item for item in intent.filters
                if not (item["dim"] == "payment_method" and not item.get("exclude")
                        and _mentions(normalize_lookup(str(item.get("value", ""))), debt_tokens))
            ]
            if len(kept) != len(intent.filters):
                intent = replace(intent, filters=kept)
            for item in intent.filters:
                if item["dim"] != "customer":
                    raise Unsupported(f"debt measure with filter {item['dim']}")
        where, args, _ = self._predicates(intent, skip_filters)
        not_cancelled = _status_sql("COALESCE(status, '')", rulebook_params.active().cancelled_statuses, negate=True)
        clause = " AND ".join([not_cancelled, *where])
        rows = self.db.execute(
            f"SELECT order_id, customer_key, date, total_due, payment_method FROM orders WHERE {clause}", args
        ).fetchall()
        params = rulebook_params.active()
        return [
            (order_id, customer, day, due, _mentions(method, params.debt_method_tokens))
            for order_id, customer, day, due, method in rows
            if not _mentions(method, params.non_debt_method_tokens)
        ]

    def _payment_index(self, window: TimeWindow | None = None) -> dict[Any, list[tuple[Any, Any]]]:
        """Payments grouped by order as (paid_on, amount). Raises when the shop has no payment record.

        A shop whose README says a column states what is still owed at the end of the data (read into
        orders.outstanding) gets one settled amount per order, valid only for a cutoff at or after the data's last
        day; an earlier cutoff raises."""
        try:
            payment_rows = self.db.execute("SELECT order_id, date, amount FROM payments").fetchall()
        except sqlite3.OperationalError as exc:
            raise Unsupported("no payments table") from exc
        if not payment_rows and self._fact("debt_state_at_data_end"):
            last = self.db.execute("SELECT MAX(date) FROM orders").fetchone()[0]
            if window is None or last is None or (window.end_exclusive - timedelta(days=1)).isoformat() < last:
                raise Unsupported("debt known only at the end of the data")
            return {order_id: [(day, max(float(due or 0) - float(owed or 0), 0.0))]
                    for order_id, day, due, owed in self.db.execute(
                        "SELECT order_id, date, COALESCE(total_due, revenue), COALESCE(outstanding, 0) FROM orders")}
        if not payment_rows:
            settled = debt_sources.order_state_settlements(self.db)
            if settled is not None:
                if window is None or not debt_sources.cutoff_covers_data_end(self.db, window.end_exclusive):
                    raise Unsupported("debt known only at the end of the data")
                return settled
            if debt_sources.payments_unrecorded(self.db):
                raise NotRecorded("no payment record")
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
        paid = self._payment_index(window if intent.measure in ("debt_outstanding", "debtor_count") else None)
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

    def debt_aging(self, intent: Intent, window: TimeWindow, buckets: list[tuple[int, int | None]]) -> Answer:
        """Rulebook §4: debt still owed at the end of the window, split by age buckets [low, high] in days (high
        None = no upper bound). Age counts days from the order date to the last day of the window."""
        if intent.measure != "debt_outstanding" or not buckets:
            raise Unsupported("debt aging needs debt_outstanding and buckets")
        paid = self._payment_index(window)
        orders = self._debt_orders(intent, False)
        if orders and all(due is None for _, _, _, due, _ in orders):
            raise Unsupported("no total_due")
        owed = self._owed(orders, paid, window, None)
        last = window.end_exclusive - timedelta(days=1)
        ages = [
            (last - date.fromisoformat(str(day)[:10])).days
            for order_id, _, day, _, _ in orders if day is not None and str(day) < window.end_exclusive.isoformat()
        ]
        if len(ages) != len(owed):
            raise Unsupported("debt aging rows misaligned")
        totals = [0.0 for _ in buckets]
        for age, (_, remaining) in zip(ages, owed):
            if remaining is None or remaining <= 0:
                continue
            for index, (low, high) in enumerate(buckets):
                if age >= low and (high is None or age <= high):
                    totals[index] += remaining
                    break
        return Answer("list", [self._format("debt_outstanding", total).value for total in totals])

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
        paid = self._payment_index(window)
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

    def purchase_minus_sold(self, intent: Intent, window: TimeWindow) -> float | None:
        """Rulebook §5 'nhập trừ bán': quantity received (non-cancelled receipts dated in the window) minus quantity
        sold on valid sales (§1), both in base units, for one product."""
        product = self._sole_product(intent, False)
        piece, values, _ = self.resolve_filter("product", product, on="purchases")
        span = [window.start.isoformat(), window.end_exclusive.isoformat()]
        has_unit = "unit" in self._table_columns("purchases")
        unit_expr = "unit" if has_unit else "NULL"
        rows = self.db.execute(
            f"SELECT {unit_expr}, SUM(qty) FROM purchases WHERE cancelled=0 AND qty IS NOT NULL AND {piece} "
            "AND date>=? AND date<? GROUP BY 1", values + span).fetchall()
        received = None
        line_piece, line_values, _ = self.resolve_filter("product", product)
        for unit, qty in rows:
            factor = 1.0
            if unit is not None:
                # the product's own base units per purchase unit, as its sale lines in that unit record it
                found = self.db.execute(
                    f"SELECT qty_base*1.0/qty FROM lines WHERE {line_piece} AND qty>0 AND qty_base IS NOT NULL "
                    "AND lookup(unit)=lookup(?) LIMIT 1", line_values + [unit]).fetchone()
                base = self.db.execute(f"SELECT 1 FROM lines WHERE {line_piece} AND qty_base=qty AND "
                                       "lookup(unit)=lookup(?) LIMIT 1", line_values + [unit]).fetchone()
                if found is not None:
                    factor = float(found[0])
                elif base is None:
                    raise Unsupported(f"purchase unit {unit} has no conversion")
            received = (received or 0.0) + float(qty) * factor
        sold = self.value(replace(intent, measure="qty_base_sold", measure2=None, shape="scalar"), window)
        if received is None:
            raise NotRecorded("no purchase recorded")
        return float(received) - float(sold or 0)

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
        before = " AND ".join(where + [_valid_sale_sql(), "date<?", "product_key IS NOT NULL"])
        inside = " AND ".join(where + [_valid_sale_sql(), "date>=? AND date<?"])
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

    def _has_table(self, name: str) -> bool:
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def _stock_product(self, intent: Intent) -> tuple[float, float | None, str | None]:
        """The one product a stock question names: (base quantity, base units per large unit, unit of the report)."""
        filters = intent.filters
        if len(filters) != 1 or filters[0].get("dim") != "product" or filters[0].get("exclude") is True:
            raise Unsupported("stock needs exactly one product filter")
        text = filters[0]["value"]
        matched = self._stored_matches("stock", "product_name", "product", normalize_lookup(text), text)
        slots = ",".join("?" for _ in matched)
        rows = self.db.execute(
            f"SELECT qty_base, large_factor, unit FROM stock WHERE product_name IN ({slots})", list(matched)
        ).fetchall()
        if len(rows) != 1 or rows[0][0] is None:
            raise Unsupported("product not in the stock report")
        return rows[0]

    def _require_stock_source(self, measure: str) -> None:
        """Rulebook §7: a shop that records no stock report and no lots cannot answer a stock question, and a shop
        with no lots cannot answer an expiry question. A layer with no record of its sources decides nothing here."""
        if not self._has_table("stock_sources"):
            return
        # Presence of each source kind (a table loaded with no rows is still recorded); the count of unannotated tables
        # is kept as its own row, because a table nobody read may hold the stock or lots.
        kinds = {row[0]: row[1] for row in self.db.execute("SELECT kind, COUNT(*) FROM stock_sources GROUP BY kind")}
        unannotated = self.db.execute(
            "SELECT COALESCE(SUM(rows), 0) FROM stock_sources WHERE kind = 'unannotated'").fetchone()[0]
        if unannotated:
            return
        if measure in STOCK_MEASURES and "inventory" not in kinds and "lot" not in kinds:
            raise Unsupported("shop records no stock report or lots")
        if measure in LOT_MEASURES and "lot" not in kinds:
            raise NotRecorded("shop records no lots with expiry dates")

    def _has_lot_balances(self) -> bool:
        return self.db.execute("SELECT 1 FROM stock_sources WHERE kind = 'lot_balance'").fetchone() is not None

    def lots(self, intent: Intent, window: TimeWindow, as_of: date) -> Answer:
        """Rulebook §6 (pharmacy): the lots still in stock today, by expiry date.

        A lot counts when its balance is above zero and it expires from today (as_of) to the stated last day, or from
        today on when the question states no day. Its balance is the one the layer read for the report day, so only a
        question on that day is answered. Expiring stock value is not answered: a lot's purchase cost is not recorded
        per base unit in this layer.
        """
        if intent.measure not in LOT_MEASURES:
            raise Unsupported(f"measure {intent.measure}")
        self._require_stock_source(intent.measure)
        if not self._has_lot_balances():
            raise Unsupported("no lot balances")
        dates = {row[0] for row in self.db.execute("SELECT DISTINCT stock_date FROM stock")}
        if len(dates) != 1 or next(iter(dates)) is None:
            raise Unsupported("lot balances are not on this day")
        report_day = str(next(iter(dates)))[:10]
        if intent.measure == "expiring_lot_value":
            # Balances are the ones after the last recorded movement; they hold for today only when nothing is
            # recorded after the report day. Earlier than that the layer keeps no per-lot sales to roll back.
            if report_day > as_of.isoformat():
                raise Unsupported("lot balances are later than this day")
        elif report_day != as_of.isoformat():
            raise Unsupported("lot balances are not on this day")
        lower, last = stock_sources.lot_expiry_range(window.start, window.end_exclusive, as_of, intent.shape)
        clause, args = "balance > 0 AND expires_at >= ?", [lower.isoformat()]
        if last is not None:
            clause, args = clause + " AND expires_at <= ?", [*args, last.isoformat()]
        filters = intent.filters
        if filters:
            if len(filters) != 1 or filters[0].get("dim") != "product" or filters[0].get("exclude") is True:
                raise Unsupported("lots need at most one product filter")
            text = filters[0]["value"]
            matched = self._stored_matches("lot_balance", "product_name", "product", normalize_lookup(text), text)
            clause += f" AND product_name IN ({','.join('?' for _ in matched)})"
            args += list(matched)
        shape = intent.shape
        if intent.measure == "expiring_lot_value":
            return self._expiring_lot_value(clause, args, shape)
        if shape == "argmax_date":
            if not filters:
                raise Unsupported("earliest lot needs one product")
            value = self.db.execute(f"SELECT MIN(expires_at) FROM lot_balance WHERE {clause}", args).fetchone()[0]
            if value is None:
                raise Unsupported("no lot in stock for the product")
            return Answer("date", str(value)[:10])
        if shape == "scalar":
            return Answer("number", self.db.execute(f"SELECT COUNT(*) FROM lot_balance WHERE {clause}", args).fetchone()[0])
        if shape == "list_filter":
            rows = self.db.execute(
                f"SELECT MIN(product_name), MIN(expires_at), MIN(COALESCE(product_id, name_key)) FROM lot_balance "
                f"WHERE {clause} GROUP BY name_key ORDER BY MIN(expires_at), MIN(COALESCE(product_id, name_key)), name_key",
                args).fetchall()
            return Answer("list", [row[0] for row in rows])
        raise Unsupported(f"shape {shape} for expiry")

    def _expiring_lot_value(self, clause: str, args: list[Any], shape: str) -> Answer:
        """Rulebook §6: the value of what is left of the lots in `clause`, at the price the lot was bought for, per base
        unit. A lot with no recorded cost makes the total unknown, so it is not answered."""
        if shape != "scalar":
            raise Unsupported(f"shape {shape} for expiring stock value")
        if "unit_cost" not in self._columns("lot_balance"):
            raise Unsupported("lot cost per base unit is not in the layer")
        rows = self.db.execute(f"SELECT balance, unit_cost FROM lot_balance WHERE {clause}", args).fetchall()
        if any(cost is None for _, cost in rows):
            raise Unsupported("lot without a cost per base unit")
        return Answer("number", _round_money(sum(balance * cost for balance, cost in rows)))

    def _ledger_stock_on(self, day: date) -> list[tuple[str, float]] | None:
        """(name_key, balance) of each product with a movement by the end of `day`: the balance after its last one
        (by time, then by order in the ledger). None when the layer has no stock ledger."""
        if not self._has_table("stock_ledger"):
            return None
        if self.db.execute("SELECT 1 FROM stock_ledger LIMIT 1").fetchone() is None:
            return None
        before = (day + timedelta(days=1)).isoformat()
        # One pass with a window function: the layer is read-only, so there is no index for a correlated subquery.
        return [(row[0], row[1]) for row in self.db.execute(
            "SELECT name_key, balance FROM (SELECT name_key, balance, ROW_NUMBER() OVER ("
            "PARTITION BY name_key ORDER BY ts DESC, rowid DESC) AS pos FROM stock_ledger WHERE ts < ?) WHERE pos = 1",
            (before,))]

    def _stock_from_ledger(self, intent: Intent, day: date) -> Answer | None:
        """Rulebook §5: stock at the end of any day from the movement ledger. Only the two shop-wide measures are
        answered this way; a product that had no movement by then holds nothing and is not counted."""
        if intent.measure not in ("inventory_value", "out_of_stock_count"):
            return None
        balances = self._ledger_stock_on(day)
        if balances is None:
            return None
        if intent.filters:
            raise Unsupported("stock on a day with a filter")
        info = {row[0]: row[1:] for row in self.db.execute("SELECT name_key, unit_cost, received FROM stock")}
        tracked = {row[0] for row in self.db.execute("SELECT DISTINCT name_key FROM stock_ledger")}
        if not set(info) <= tracked:
            # A product the ledger never mentions may hold stock the ledger does not show: keep the report's answer.
            return None
        if intent.measure == "out_of_stock_count":
            # Bought-in goods only: a product with receipts.
            if any(key not in info for key, _ in balances):
                raise Unsupported("ledger product not in the catalog")
            return Answer("number", sum(1 for key, balance in balances if balance == 0 and info[key][1] == 1))
        total = 0.0
        for key, balance in balances:
            if balance <= 0:
                continue
            cost = info.get(key, (None, None))[0]
            if cost is None:
                raise Unsupported("product without a unit cost")
            total += balance * cost
        return Answer("number", _round_money(total))

    def _ledger_qty_answer(self, intent: Intent, day: date, split: bool) -> Answer | None:
        """One product's stock at the end of any day, from the movement ledger (see stock_sources)."""
        filters = intent.filters
        if intent.measure != "inventory_qty" or len(filters) != 1 or filters[0].get("dim") != "product" \
                or filters[0].get("exclude") is True:
            return None
        text = filters[0]["value"]
        matched = self._stored_matches("stock", "product_name", "product", normalize_lookup(text), text)
        qty = stock_sources.ledger_balance_on(self.db, matched, day)
        if qty is None:
            return None
        factors = {row[0] for row in self.db.execute(
            f"SELECT large_factor FROM stock WHERE product_name IN ({','.join('?' for _ in matched)})", list(matched))}
        result = stock_sources.quantity_answer(qty, factors.pop() if len(factors) == 1 else None, split)
        if result is None:
            raise Unsupported("stock quantity is not a whole count or has no large unit")
        return Answer(result[0], result[1])

    def stock(self, intent: Intent, window: TimeWindow, split: bool = False, question_text: str | None = None) -> Answer:
        """Rulebook §2 and §5: the stock at the end of the window's last day, from the stock report.

        A report gives the balance on the day it was taken (the last day with data, see layer._build_stock). Any
        other day needs a roll-back through the movements, which the layer does not keep, so it is not answered.
        split asks the quantity as [whole large units, loose base units], the rulebook's large-unit form.
        """
        self._require_stock_source(intent.measure)
        if not self._has_table("stock"):
            raise Unsupported("no stock report")
        last_day = window.end_exclusive - timedelta(days=1)
        from_ledger = self._stock_from_ledger(intent, last_day)
        if from_ledger is not None:
            return from_ledger
        dates = {row[0] for row in self.db.execute("SELECT DISTINCT stock_date FROM stock")}
        if len(dates) != 1 or next(iter(dates)) is None:
            raise Unsupported("stock report date unknown")
        report_day = str(next(iter(dates)))[:10]
        day = (window.end_exclusive - timedelta(days=1)).isoformat()
        if day != report_day:
            answered = self._ledger_qty_answer(intent, window.end_exclusive - timedelta(days=1), split)
            if answered is not None:
                return answered
            raise Unsupported("stock on a day other than the report date")
        if self.db.execute("SELECT COUNT(*) - COUNT(DISTINCT name_key) FROM stock").fetchone()[0]:
            raise Unsupported("product listed twice in the stock report")
        m = intent.measure
        if m == "inventory_value":
            if intent.filters:
                raise Unsupported("stock value with a filter")
            rows = self.db.execute("SELECT qty_base, unit_cost FROM stock WHERE qty_base > 0").fetchall()
            if any(cost is None for _, cost in rows):
                raise Unsupported("product without a unit cost")
            return Answer("number", _round_money(sum(qty * cost for qty, cost in rows)))
        if m == "out_of_stock_count":
            # Rulebook §5: bought-in goods (a product with receipts) with no stock at the end of the day.
            if intent.filters:
                raise Unsupported("out of stock count with a filter")
            return Answer("number", self.db.execute(
                "SELECT COUNT(*) FROM stock WHERE qty_base = 0 AND received = 1").fetchone()[0])
        if m != "inventory_qty":
            raise Unsupported(f"measure {m}")
        if split and question_text:
            from bizreason.semantic.stock_split import SplitUnsupported, split_stock
            try:
                pair = split_stock(self.db, question_text, intent.filters)
            except SplitUnsupported as exc:
                raise Unsupported(str(exc)) from exc
            if pair is not None:
                return Answer("list", list(pair))
        qty, factor, _unit = self._stock_product(intent)
        if qty < 0 or float(qty) != int(qty):
            raise Unsupported("stock quantity is not a whole count")
        if not split:
            return Answer("number", int(qty))
        if factor is None or float(factor) != int(factor) or int(factor) < 2:
            raise Unsupported("no large unit for the stock")
        whole, loose = divmod(int(qty), int(factor))
        return Answer("list", [whole, loose])

    def _net_profit(self, intent: Intent, window: TimeWindow, skip_filters: bool) -> float | None:
        """Rulebook §5: gross profit of the period less every expense recorded in the expense ledger for it."""
        if intent.filters and not skip_filters:
            raise Unsupported("net profit with a filter")
        try:
            recorded = self.db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
        except sqlite3.OperationalError:
            recorded = 0
        if not recorded:
            raise NotRecorded("no expense ledger")
        gross = self.value(replace(intent, measure="gross_profit", filters=[]), window, skip_filters=True)
        if gross is None:
            return None
        spent = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE date >= ? AND date < ?",
            (window.start.isoformat(), window.end_exclusive.isoformat()),
        ).fetchone()[0]
        return gross - spent

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
        if m in ("warranty_count", "warranty_rate") and not self._fact("zero_amount_is_warranty"):
            # Only a shop whose README says zero-money tickets are warranty visits has warranty tickets we can count.
            raise Unsupported("warranty tickets not identified by the shop's conventions")
        if m == "net_profit":
            return self._net_profit(intent, window, skip_filters)
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
            dues = [r[0] for r in self.db.execute(f"SELECT total_due FROM orders WHERE {_valid_sale_sql()} AND {clause}", sql_args)]
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
            "revenue": f"SELECT SUM({amount}) FROM {source} WHERE {_valid_sale_sql()} AND {clause}",
            "order_count": f"SELECT COUNT(DISTINCT order_id) FROM {source} WHERE {_valid_sale_sql()} AND {clause}",
            "cancelled_count": f"SELECT COUNT(DISTINCT order_id) FROM {source} WHERE {_cancelled_sql()} AND {clause}",
            "avg_per_order": f"SELECT SUM({amount})*1.0/NULLIF(COUNT(DISTINCT order_id),0) FROM {source} WHERE {_valid_sale_sql()} AND {clause}",
            "qty_base_sold": f"SELECT SUM(COALESCE(qty_base, qty)) FROM lines WHERE {_valid_sale_sql()} AND {self.items_only} AND {clause}",
            "distinct_customers": f"SELECT COUNT(DISTINCT customer_key) FROM {source} WHERE {_valid_sale_sql()} AND {clause}",
            "active_days": f"SELECT COUNT(DISTINCT date) FROM {source} WHERE {_valid_sale_sql()} AND {clause}",
            # Rulebook (sửa xe): every ticket is one visit, warranty tickets included; a warranty ticket collects no
            # money, and the warranty rate divides warranty tickets by tickets that collected money.
            "service_ticket_count": f"SELECT COUNT(DISTINCT order_id) FROM {source} WHERE {_valid_sale_sql()} AND {clause}",
            "warranty_count": (f"SELECT COUNT(DISTINCT CASE WHEN COALESCE({amount},0)=0 THEN order_id END) FROM {source} "
                               f"WHERE {_valid_sale_sql()} AND {clause}"),
            "warranty_rate": (f"SELECT 100.0*COUNT(DISTINCT CASE WHEN COALESCE({amount},0)=0 THEN order_id END)"
                              f"/NULLIF(COUNT(DISTINCT CASE WHEN {amount}>0 THEN order_id END),0) FROM {source} "
                              f"WHERE {_valid_sale_sql()} AND {clause}"),
            "gross_profit": f"SELECT SUM(amount)-SUM(qty*line_cost) FROM lines WHERE {_valid_sale_sql()} AND {clause}",
            "cancel_rate": (
                f"SELECT 100.0*COUNT(DISTINCT CASE WHEN {_cancelled_sql()} THEN order_id END)"
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
            if measure in PURCHASE_MEASURES:
                raise NotRecorded("no purchase recorded")
            raise Unsupported("empty result")
        if measure in ("cancel_rate", "warranty_rate"):
            return Answer("percent", _round2(value))
        if measure in MONEY:
            return Answer("number", _round_money(value))
        return Answer("number", _whole(value))

    def _ranked(self, intent: Intent, window: TimeWindow) -> list[tuple[Any, float]]:
        dim = intent.group_by
        m = intent.measure
        if intent.min_orders and (m in PURCHASE_RANK_MEASURES or m == "debt_outstanding"):
            raise Unsupported("minimum orders on a purchase or debt ranking")
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
            "active_days": "COUNT(DISTINCT date)",
            "avg_per_order": f"SUM({amount})*1.0/NULLIF(COUNT(DISTINCT order_id),0)",
            "service_ticket_count": "COUNT(DISTINCT order_id)",
        }.get(m)
        if score is None:
            raise Unsupported(f"ranking of {m}")
        status = _cancelled_sql() if m == "cancelled_count" else _valid_sale_sql()
        if source == "lines" and dim in LINE_DIMS:
            where = where + [self.items_only]
        clause = " AND ".join(where + [status, "date>=? AND date<?", f"{key} IS NOT NULL"])
        having = f" HAVING COUNT(DISTINCT order_id) >= {int(intent.min_orders)}" if intent.min_orders else ""
        rows = self.db.execute(
            f"SELECT {label}, {score} s, {key} k FROM {source} WHERE {clause} GROUP BY {key}{having} "
            "ORDER BY s DESC, k ASC",
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
            if keys[1] != rulebook_params.active().shift_baseline:
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
        params = rulebook_params.active()
        morning = self.value(
            Intent(measure=intent.measure, filters=[*others, {"dim": "shift", "value": params.shift_baseline}],
                   age_days=intent.age_days),
            window,
        )
        afternoon = self.value(
            Intent(measure=intent.measure, filters=[*others, {"dim": "shift", "value": params.shift_afternoon}],
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

    def _records_customers(self) -> bool:
        """True when some order names a customer (a code or a name); rulebook §3 identifies customers by those."""
        try:
            return self.db.execute(
                "SELECT 1 FROM orders WHERE customer_key IS NOT NULL LIMIT 1").fetchone() is not None
        except sqlite3.OperationalError:
            return False

    def _fact(self, key: str) -> bool:
        try:
            row = self.db.execute("SELECT value FROM shop_facts WHERE key=?", (key,)).fetchone()
        except sqlite3.Error:
            return False
        return bool(row and row[0])

    def _table_columns(self, table: str) -> set[str]:
        try:
            return {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return set()

    def _column_home(self, column: str) -> str | None:
        """The canonical or src_ table holding a kept column (orders first, then lines, then the others)."""
        tables = ["orders", "lines", "deliveries", "purchases", "payments"] + [
            r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'src_%'")]
        for table in tables:
            if column in self._table_columns(table):
                return table
        return None

    @staticmethod
    def _twin(columns: set[str], column: str, kind: str) -> str | None:
        base = column[2:] if column.startswith("x_") else column
        name = f"{kind}_{base}"
        return name if name in columns else None

    def _column_condition(self, alias: str, columns: set[str], item: dict[str, Any]) -> tuple[str, list[Any]]:
        column, op, value = item.get("column"), item.get("op"), item.get("value")
        if column not in columns or op not in COLUMN_OPS:
            raise Unsupported(f"column filter {column} {op}")
        ref = f"{alias}.{column}"
        if op in ("contains", "not_contains", "eq"):
            needle = normalize_lookup(str(value or ""))
            if not needle:
                raise Unsupported("empty column filter value")
            if op == "eq":
                return f"lookup({ref}) = ?", [needle]
            neg = "NOT " if op == "not_contains" else ""
            return f"COALESCE(lookup({ref}), '') {neg}LIKE ?", [f"%{needle}%"]
        if op == "nonempty":
            return f"NULLIF(TRIM(CAST({ref} AS TEXT)), '') IS NOT NULL", []
        dated = self._twin(columns, column, "xd") or (column if column.startswith("xd_") else None)
        if dated is None or (self.as_of is None and op != "in_period"):
            raise Unsupported(f"date condition on {column}")
        if op == "in_period":
            if self._period is None:
                raise Unsupported("in_period needs a window")
            return f"{alias}.{dated} >= ? AND {alias}.{dated} < ?", list(self._period)
        sql = {"after_today": ">", "before_today": "<", "on_or_before_today": "<=", "on_or_after_today": ">="}[op]
        return f"{alias}.{dated} {sql} ?", [self.as_of.isoformat()]

    def _order_link(self, home: str, cols: set[str]) -> str:
        """The one column of a side table that holds order ids (the layer records it by value coverage, not by name)."""
        keys = {row[0] for row in self.db.execute(
            "SELECT column_name FROM column_catalog WHERE table_name=? AND join_key_to LIKE 'orders.%'", (home,))}
        if "order_id" in cols:
            keys.add("order_id")
        keys &= cols
        if not keys:
            raise Unsupported(f"no order link for {home}")
        if len(keys) > 1:
            raise Unsupported(f"several order links in {home}")
        return next(iter(keys))

    def column_value(self, intent: Intent, window: TimeWindow) -> float | None:
        """A sum, count or distinct count on any kept column, over valid sales in the window (rulebook §1)."""
        spec = intent.column_measure or {}
        column, op = spec.get("column"), spec.get("op")
        if op not in COLUMN_MEASURE_OPS or not isinstance(column, str):
            raise Unsupported("column measure")
        home = self._column_home(column)
        if home is None:
            raise Unsupported(f"unknown column {column}")
        cols = self._table_columns(home)
        span = [window.start.isoformat(), window.end_exclusive.isoformat()]
        self._period = None
        if any(f.get("op") == "in_period" for f in intent.column_filters):
            # The question's period applies to that date column; orders count up to the end of today.
            self._period = tuple(span)
            if self.as_of is None:
                raise Unsupported("in_period needs today")
            span = [date.min.isoformat(), (self.as_of + timedelta(days=1)).isoformat()]
        where: list[str] = []
        args: list[Any] = []
        joined = False
        if home in ("orders", "lines"):
            source = f"{home} t"
            where += [_valid_sale_sql().replace("status", "t.status"), "t.date >= ? AND t.date < ?"]
            args += span
            if intent.filters:
                piece, extra, needs_lines = self._predicates(intent)
                if needs_lines and home == "orders":
                    raise Unsupported("line filter on an order column")
                where += [w.replace("product_key", "t.product_key") if False else w for w in piece]
                args += extra
        else:
            key = self._order_link(home, cols)
            # A semi-join: each side-table row is kept when its order is a valid sale in the window (and passes the
            # order-level filters), and no order is counted twice whatever the side table's own cardinality.
            order_where = [_valid_sale_sql(), "date >= ? AND date < ?"]
            order_args: list[Any] = list(span)
            if intent.filters:
                piece, extra, needs_lines = self._predicates(intent)
                if needs_lines:
                    raise Unsupported("line filter on a joined table")
                order_where += piece
                order_args += extra
            source = f"{home} t"
            joined = True
            where.append(f"t.{key} IN (SELECT order_id FROM orders WHERE {' AND '.join(order_where)})")
            args += order_args
        for item in intent.column_filters:
            target = item.get("column")
            if target in cols:
                piece, extra = self._column_condition("t", cols, item)
            elif joined and target in self.order_columns:
                # An order column: the order must satisfy it too (semi-join, so the side table is not multiplied).
                piece, extra = self._column_condition("o", self.order_columns, item)
                piece = f"t.{key} IN (SELECT o.order_id FROM orders o WHERE {piece})"
            else:
                raise Unsupported(f"column filter on {target} outside {home}")
            where.append(piece)
            args += extra
        if op == "sum":
            number = self._twin(cols, column, "xn") or column
            expr = f"SUM(CAST(t.{number} AS REAL))"
        elif op == "count_distinct":
            expr = f"COUNT(DISTINCT lookup(t.{column}))"
        elif joined:
            expr = f"COUNT(DISTINCT t.{key})"
        else:
            expr = "COUNT(DISTINCT t.order_id)" if home == "lines" else "COUNT(*)"
        return self.db.execute(f"SELECT {expr} FROM {source} WHERE {' AND '.join(where)}", args).fetchone()[0]

    def answer(self, intent: Intent, windows: list[TimeWindow]) -> Answer:
        if intent.column_measure:
            target = [w for w in windows if w.role != "comparison"]
            if len(target) != 1 or intent.shape != "scalar" or intent.group_by:
                raise Unsupported("column measure needs one window and a single value")
            value = self.column_value(intent, target[0])
            if value is None:
                raise Unsupported("empty result")
            if (intent.column_measure or {}).get("op") == "sum" and abs(value - round(value)) > 1e-9:
                return Answer("number", _round2(value))
            return Answer("number", int(round(value)))
        if intent.column_filters:
            raise Unsupported("column filters need a column measure")
        if intent.measure in LOT_MEASURES:
            # Lot and expiry questions: the layer keeps no lot balances yet, so an answer is never given here.
            self._require_stock_source(intent.measure)
            raise Unsupported("lot balances are not in the layer")
        # Public test: shops with no customer on their orders still have answers to customer questions (another
        # source names them), so a missing customer only abstains.
        if _uses_customers(intent) and not self._records_customers():
            if debt_sources.customers_unrecorded(self.db):
                raise NotRecorded("customer not recorded")
            raise Unsupported("customer not recorded")
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
            if intent.group_by == "hour":
                # Rulebook §3: the peak hour is the hour the window starts at, as a number.
                return Answer("number", int(picked[0][0]))
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
