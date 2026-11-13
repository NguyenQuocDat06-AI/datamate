"""Fallback answers: the model writes one read-only SQLite query over the canonical layer.

Used only when the typed pipeline abstains. Two generations are asked, each with its own prompt. Each reply must be a
single SELECT or WITH statement. Both queries run read-only (an authorizer, a time limit, a row cap), and their results
must agree before anything is answered. Any doubt returns None: the scoring gives a wrong answer -1 and an abstention 0.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, NamedTuple

from bizreason.contracts import AnswerType
from bizreason.semantic import rulebook_params
from bizreason.utils import normalize_lookup

log = logging.getLogger(__name__)

ANSWER_TYPES = frozenset(
    t.value for t in AnswerType if t not in (AnswerType.ABSTAIN, AnswerType.UNANSWERABLE)
)
SCALAR_TYPES = frozenset({"number", "percent", "string", "date", "boolean"})
MODEL_TIMEOUT_S = 60.0
PROGRESS_OPS = 10_000  # SQLite VM instructions between progress-handler calls
RULEBOOK_CHARS = 6_000  # about 1500 to 2500 tokens, depending on the script
SAMPLE_LIMIT = 8
# Internal build tables that carry no shop facts; they are left out of the schema text.
EXCLUDED_TABLES = frozenset({"build_stats", "stock_sources", "model_failures"})
# Low-cardinality text columns whose distinct values are shown to the model.
CATEGORICAL_COLUMNS = frozenset({
    "status", "channel", "payment_method", "category", "unit", "base_unit", "customer_group", "method",
    "product_kind", "line_kind", "key",
})
# Actions a read-only SELECT needs; every other action (writes, ATTACH, PRAGMA, transactions...) is denied.
_ALLOWED_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33),
})
_STATEMENT_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|reindex|begin|commit|rollback|"
    r"savepoint|release)\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# English glosses of the canonical layer (bizreason/semantic/layer.py). Columns not listed here are shown by name only.
_LINE_COLUMNS = {
    "order_id": "order the line belongs to",
    "date": "order date, YYYY-MM-DD",
    "ts": "order timestamp",
    "status": "canonical status of the line's order",
    "customer_key": "customer identity; NULL for an order with no customer",
    "customer_name": "customer name as recorded",
    "customer_group": "customer group label",
    "channel": "sales channel",
    "payment_method": "payment method wording as recorded",
    "employee": "employee who took the order",
    "product_id": "product code",
    "product_name": "product name as recorded",
    "product_key": "normalized product key (product code, else normalized name)",
    "category": "product category",
    "unit": "unit of sale",
    "qty": "quantity in the unit of sale",
    "qty_base": "quantity in the base unit",
    "amount": "line money total after line discount",
    "line_cost": "line cost total",
    "unit_price": "unit price",
    "is_item": "1 for a product line, 0 for a service fee line",
    "base_unit": "base unit of the product",
    "prescription": "1 when the product is a prescription product",
    "active_ingredient": "active ingredient of the product",
    "product_kind": "kind of product",
}
_GLOSS: dict[str, tuple[str, dict[str, str]]] = {
    "orders": ("one row per order, all statuses", {
        "order_id": "order identifier",
        "date": "order date, YYYY-MM-DD",
        "ts": "order timestamp",
        "status": "canonical order status: completed, shipping, cancelled or returned",
        "customer_key": "customer identity; NULL for an order with no customer",
        "customer_id": "customer code as recorded",
        "customer_name": "customer name as recorded",
        "customer_group": "customer group label",
        "employee": "employee who took the order",
        "channel": "sales channel",
        "payment_method": "payment method wording as recorded",
        "line_total": "sum of the order's product line amounts, without discount and shipping lines",
        "goods_total": "goods total recorded on the order",
        "total_due": "amount due recorded on the order",
        "paid": "amount paid recorded on the order",
        "outstanding": "amount still owed on the order",
        "revenue": "order revenue: line_total, or goods_total when the order has no product lines",
    }),
    "lines": ("one row per product line of an order, for sales and product questions", _LINE_COLUMNS),
    "raw_lines": (
        "every line row before filtering, including discount and shipping rows; do not sum it for sales, use lines",
        {**_LINE_COLUMNS, "has_product": "1 when the row is a product row", "line_kind":
         "row kind: product, discount, shipping_fee or service_fee"},
    ),
    "purchases": ("one row per purchase line (goods bought from a supplier)", {
        "purchase_id": "purchase identifier",
        "date": "purchase date, YYYY-MM-DD",
        "supplier_id": "supplier code",
        "supplier_name": "supplier name",
        "status": "purchase status as recorded",
        "product_id": "product code",
        "product_name": "product name",
        "qty": "purchased quantity",
        "unit_cost": "purchase unit cost",
        "amount": "purchase line amount",
        "receipt_total": "receipt total recorded",
        "cancelled": "1 when the purchase is cancelled",
    }),
    "payments": ("one row per payment received on an order", {
        "order_id": "order paid",
        "date": "payment date, YYYY-MM-DD",
        "amount": "amount paid",
        "method": "payment method wording",
    }),
    "deliveries": ("one row per delivery of an order", {
        "order_id": "order delivered",
        "date": "delivery date, YYYY-MM-DD",
        "trip": "delivery trip",
        "qty": "quantity delivered",
    }),
    "stock": ("stock snapshot: one row per product", {
        "name_key": "normalized product key",
        "product_name": "product name",
        "unit": "unit of the stock quantity",
        "qty_base": "quantity on hand in the base unit",
        "unit_cost": "unit cost",
        "large_factor": "conversion factor to the large unit",
        "received": "1 when the product was received",
        "stock_date": "snapshot date, YYYY-MM-DD",
    }),
    "lot_balance": ("remaining quantity per product lot", {
        "name_key": "normalized product key",
        "product_name": "product name",
        "product_id": "product code",
        "lot_id": "lot identifier",
        "expires_at": "expiry date of the lot",
        "balance": "remaining quantity in the lot",
    }),
    "expenses": ("one row per shop expense", {
        "date": "expense date, YYYY-MM-DD",
        "amount": "expense amount",
        "category": "expense category",
    }),
    "price_changes": ("one row per product price change", {
        "product_id": "product code",
        "product_name": "product name",
        "name_key": "normalized product key",
        "date": "date the price changed, YYYY-MM-DD",
        "old_price": "price before the change",
        "new_price": "price after the change",
    }),
    "shop_facts": ("yes/no facts about the shop", {
        "key": "fact name: has_address, has_customer_group or has_orders",
        "value": "1 for yes, 0 for no",
    }),
    "status_domain": ("the canonical order statuses present in orders", {"status": "canonical order status"}),
}


class _Reject(Exception):
    """A reason to abstain. Its message is kept as last_reason."""


class _Attempt(NamedTuple):
    sql: str
    answer_type: str
    definition: str
    columns: list[str]
    rows: list[tuple[Any, ...]]


@dataclass(frozen=True)
class FallbackResult:
    type: AnswerType
    answer: Any
    sql: str
    reason: str


SYSTEM_DIRECT = """You answer one shop question by writing a single read-only SQLite query over the canonical layer \
described in the user message. Reply with one JSON object and nothing else:
{"sql": "<one SELECT or WITH statement>", "answer_type": "<number | percent | string | date | boolean | list | table>", \
"definition": "<one sentence: the exact definition you applied>"}

Rules:
- Use only the tables and columns listed. Never invent a column.
- Exactly one statement, starting with SELECT or WITH. No semicolons, no writes, no PRAGMA.
- For sales, revenue or order counts use only the valid sale statuses named in the user message. Cancelled orders are \
not sales.
- "number" is a whole number: wrap every money or count result in ROUND(x) so the value is an integer. "percent" is \
100 times a ratio, rounded to 2 decimals in SQL. "list" is one column with one row per item. "table" has two or more \
columns. Every other type returns one row and one column.
- If the tables cannot answer the question, or it needs a judgement the rulebook does not define, reply with \
{"sql": "", "answer_type": "", "definition": "<why>"}."""

SYSTEM_DEFINITION_FIRST = """You answer one shop question by writing a single read-only SQLite query over the canonical \
layer described in the user message. Work in this order: first write the "definition" (the exact definition you \
apply, one sentence), then write the "sql", which must compute that definition and nothing else. Reply with one JSON \
object and nothing else:
{"definition": "<one sentence>", "sql": "<one SELECT or WITH statement>", "answer_type": "<number | percent | string | \
date | boolean | list | table>"}

Rules:
- Use only the tables and columns listed. Never invent a column.
- Exactly one statement, starting with SELECT or WITH. No semicolons, no writes, no PRAGMA.
- For sales, revenue or order counts use only the valid sale statuses named in the user message. Cancelled orders are \
not sales.
- "number" is a whole number: wrap every money or count result in ROUND(x). "percent" is 100 times a ratio, rounded to \
2 decimals in SQL. "list" is one column with one row per item. "table" has two or more columns. Every other type \
returns one row and one column.
- If the tables cannot answer the question, or it needs a judgement the rulebook does not define, reply with \
{"definition": "<why>", "sql": "", "answer_type": ""}."""


def _authorize(action: int, arg1: Any, arg2: Any, db_name: Any, trigger: Any) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({_quote(table)})")]


def _distinct_values(connection: sqlite3.Connection, table: str, column: str) -> list[str]:
    rows = connection.execute(
        f"SELECT DISTINCT {_quote(column)} FROM {_quote(table)} WHERE {_quote(column)} IS NOT NULL "
        f"ORDER BY 1 LIMIT {SAMPLE_LIMIT + 1}"
    ).fetchall()
    if len(rows) > SAMPLE_LIMIT:
        return []  # not low-cardinality: show no values
    return [str(row[0]) for row in rows]


def schema_text(connection: sqlite3.Connection) -> str:
    """The canonical tables that exist, with English glosses, sample values and the order date range."""
    names = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    out = ["Tables of the canonical layer:"]
    for table in [name for name in _GLOSS if name in names]:
        description, glosses = _GLOSS[table]
        out.append(f"{table}: {description}")
        for column in _columns(connection, table):
            entry = f"  - {column}"
            if column in glosses:
                entry += f": {glosses[column]}"
            if column in CATEGORICAL_COLUMNS:
                values = _distinct_values(connection, table, column)
                if values:
                    entry += ". Values: " + " | ".join(values)
            out.append(entry)
    for table in names:
        if table in _GLOSS or table in EXCLUDED_TABLES:
            continue
        out.append(f"{table} (not described): columns " + ", ".join(_columns(connection, table)))
    if "orders" in names and "date" in _columns(connection, "orders"):
        low, high = connection.execute("SELECT MIN(date), MAX(date) FROM orders").fetchone()
        out.append(f"orders.date ranges from {low} to {high}.")
    return "\n".join(out)


def _status_text() -> str:
    params = rulebook_params.active()
    return (
        "Order statuses (orders.status and lines.status) are canonical: "
        + ", ".join(rulebook_params.CANONICAL_ORDER_STATUSES) + ".\n"
        f"Valid sale statuses (orders that count as sales and revenue): {', '.join(params.valid_sale_statuses)}.\n"
        f"Cancelled statuses: {', '.join(params.cancelled_statuses)}."
    )


def _rulebook_section(rulebook: str | None, question: str) -> str:
    """Rulebook lines that carry a cue word, plus lines sharing a word of the question, capped in length."""
    if not rulebook:
        return "(no rulebook was given)"
    words = {word for word in normalize_lookup(question).split() if len(word) >= 4}
    shared = [
        line for line in rulebook.splitlines()
        if line.strip() and words & set(normalize_lookup(line).split())
    ]
    lines = list(dict.fromkeys(rulebook_params.relevant_rulebook_text(rulebook).splitlines() + shared))
    text = "\n".join(line for line in lines if line.strip())
    if len(text) > RULEBOOK_CHARS:
        text = text[:RULEBOOK_CHARS].rsplit("\n", 1)[0] + "\n(rulebook excerpt truncated)"
    return text or "(no rulebook line applies)"


def _prompt_direct(question: str, as_of: date, status: str, rules: str, schema: str) -> str:
    return "\n\n".join([
        f"Question: {question}",
        f"As-of date (today): {as_of.isoformat()}",
        status,
        "Rulebook lines that apply:\n" + rules,
        schema,
    ])


def _prompt_definition_first(question: str, as_of: date, status: str, rules: str, schema: str) -> str:
    return "\n\n".join([
        "Rulebook lines that apply:\n" + rules,
        status,
        schema,
        f"As-of date (today): {as_of.isoformat()}",
        f"Question: {question}",
    ])


def _single_select(sql: str) -> str:
    text = sql.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    if not text:
        raise _Reject("empty sql")
    if ";" in text:
        raise _Reject("more than one statement")
    if not _STATEMENT_START.match(text):
        raise _Reject("not a SELECT or WITH statement")
    if _WRITE_WORDS.search(text):
        raise _Reject("contains a write or admin keyword")
    return text


def _parse_reply(reply: Any) -> tuple[str, str, str]:
    if not isinstance(reply, dict):
        raise _Reject("reply is not a JSON object")
    sql, answer_type = reply.get("sql"), reply.get("answer_type")
    definition = reply.get("definition", "")
    if not isinstance(sql, str) or not isinstance(answer_type, str):
        raise _Reject("reply lacks sql or answer_type")
    answer_type = answer_type.strip().lower()
    if answer_type not in ANSWER_TYPES:
        raise _Reject(f"answer type {answer_type!r} is not allowed")
    if not isinstance(definition, str):
        definition = ""
    return _single_select(sql), answer_type, definition.strip()[:300]


def _whole(value: Any) -> bool:
    return isinstance(value, int) or (isinstance(value, float) and value.is_integer())


def _same(first: Any, second: Any) -> bool:
    """Equal values. Numbers within 0.5% or 0.01 absolute; whole numbers exactly; strings after normalize_lookup."""
    if first is None or second is None:
        return False
    if isinstance(first, str) or isinstance(second, str):
        return isinstance(first, str) and isinstance(second, str) and (
            normalize_lookup(first) == normalize_lookup(second))
    if isinstance(first, (int, float)) and isinstance(second, (int, float)):
        if not (math.isfinite(first) and math.isfinite(second)):
            return False
        if _whole(first) and _whole(second):
            return first == second
        return abs(first - second) <= max(0.01, 0.005 * max(abs(first), abs(second)))
    return False


def _agree(rows_a: list[tuple[Any, ...]], rows_b: list[tuple[Any, ...]]) -> bool:
    if len(rows_a) != len(rows_b):
        return False
    for row_a, row_b in zip(rows_a, rows_b):
        if len(row_a) != len(row_b) or not all(_same(x, y) for x, y in zip(row_a, row_b)):
            return False
    return True


def _plain(value: Any) -> Any:
    """A value that can be shown as is: a string, or a whole number as int. NULL, fractions and blobs abstain."""
    if value is None:
        raise _Reject("NULL in result")
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    raise _Reject("value is not a string or a whole number")


def _round2(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _convert(answer_type: str, columns: list[str], rows: list[tuple[Any, ...]]) -> Any:
    """The answer value for an agreed result, in the same formats the compiler's Answer uses."""
    if answer_type in SCALAR_TYPES:
        if len(rows) != 1 or len(rows[0]) != 1:
            raise _Reject("a scalar answer needs one row and one column")
        value = rows[0][0]
        if answer_type == "number":
            number = _plain(value)
            if isinstance(number, str):
                raise _Reject("number answer is not numeric")
            return number
        if answer_type == "percent":
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise _Reject("percent answer is not numeric")
            return _round2(float(value))
        if answer_type == "string":
            if not isinstance(value, str) or not value.strip():
                raise _Reject("string answer is empty or not text")
            return value
        if answer_type == "date":
            if not isinstance(value, str) or not _ISO_DATE.match(value):
                raise _Reject("date answer is not YYYY-MM-DD")
            date.fromisoformat(value)
            return value
        if isinstance(value, bool) or not (isinstance(value, int) and value in (0, 1)):
            raise _Reject("boolean answer is not 0 or 1")
        return bool(value)
    if answer_type == "list":
        if len(columns) != 1:
            raise _Reject("a list answer needs one column")
        return [_plain(row[0]) for row in rows]
    if len(columns) < 2:
        raise _Reject("a table answer needs two or more columns")
    return {"columns": list(columns), "rows": [[_plain(value) for value in row] for row in rows]}


class SqlFallback:
    """Answers a question that the typed pipeline abstained on, when two independent SQL generations agree."""

    def __init__(self, model: Any, rulebook_text: str | None, *, max_rows: int = 50, time_limit_s: float = 5.0):
        if max_rows < 1 or time_limit_s <= 0:
            raise ValueError("max_rows must be at least 1 and time_limit_s positive")
        self.model = model
        self.rulebook_text = rulebook_text
        self.max_rows = max_rows
        self.time_limit_s = time_limit_s
        self.last_reason = ""

    def answer(self, question: str, connection: sqlite3.Connection, as_of: date) -> FallbackResult | None:
        """The agreed answer, or None to abstain. Never raises."""
        try:
            result = self._answer(question, connection, as_of)
        except Exception as exc:  # any failure is an abstention
            self.last_reason = str(exc) if isinstance(exc, _Reject) else f"error: {type(exc).__name__}: {exc}"
            log.info("sql fallback abstained: %s", self.last_reason)
            return None
        self.last_reason = "answered: two generations agreed"
        return result

    def _answer(self, question: str, connection: sqlite3.Connection, as_of: date) -> FallbackResult:
        schema = schema_text(connection)
        rules = _rulebook_section(self.rulebook_text, question)
        status = _status_text()
        first = self._attempt(connection, SYSTEM_DIRECT,
                              _prompt_direct(question, as_of, status, rules, schema), "A")
        second = self._attempt(connection, SYSTEM_DEFINITION_FIRST,
                               _prompt_definition_first(question, as_of, status, rules, schema), "B")
        if first.answer_type != second.answer_type:
            raise _Reject("the two generations give different answer types")
        if not _agree(first.rows, second.rows):
            raise _Reject("the two generations give different results")
        value = _convert(first.answer_type, first.columns, first.rows)
        return FallbackResult(
            type=AnswerType(first.answer_type),
            answer=value,
            sql=first.sql,
            reason=f"two generations agreed; definition: {first.definition or second.definition}",
        )

    def _attempt(self, connection: sqlite3.Connection, system: str, prompt: str, label: str) -> _Attempt:
        try:
            reply = self.model.generate_json(system, prompt, timeout=MODEL_TIMEOUT_S)
        except Exception as exc:
            raise _Reject(f"{label}: model call failed ({type(exc).__name__})") from exc
        try:
            sql, answer_type, definition = _parse_reply(reply)
            columns, rows = self._run(connection, sql)
        except _Reject as exc:
            raise _Reject(f"{label}: {exc}") from exc
        return _Attempt(sql, answer_type, definition, columns, rows)

    def _run(self, connection: sqlite3.Connection, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Run one query read-only, within the time limit and row cap. Restores the connection's handlers after."""
        deadline = time.monotonic() + self.time_limit_s
        connection.set_authorizer(_authorize)
        connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, PROGRESS_OPS)
        try:
            cursor = connection.execute(sql)
            try:
                columns = [item[0] for item in cursor.description or ()]
                rows = cursor.fetchmany(self.max_rows + 1)
            finally:
                cursor.close()
        except sqlite3.Error as exc:
            if time.monotonic() >= deadline:
                raise _Reject("time limit reached") from exc
            raise _Reject(f"query failed: {exc}") from exc
        finally:
            connection.set_authorizer(None)
            connection.set_progress_handler(None, 0)
        if len(rows) > self.max_rows:
            raise _Reject("too many rows")
        if not rows:
            raise _Reject("empty result")
        return columns, [tuple(row) for row in rows]
