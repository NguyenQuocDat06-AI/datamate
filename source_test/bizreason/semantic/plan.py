"""Typed query plans: a compositional fallback for questions the fixed intent cannot express.

The model composes a small JSON plan from fixed parts (source table, conditions on any column, an aggregate over a
column expression, a second aggregation level, arithmetic between aggregates, grouping with a condition on groups,
and an output form). The compiler checks every table, column, link, operator and stored value against the shop's
own layer, applies the rulebook's defaults itself (which orders count as sales, the question's period), and builds
parameterised SQL. The model never writes SQL.

The language (every key optional except source and measure):
  {"source": <table>, "scope": "valid"|"cancelled"|"all", "period": "question"|"comparison"|"until_today"|"none",
   "filters": [<filter>], "measure": <measure>,
   "per": {"by": <column>, "agg": "count"|"avg"|"sum"|"min"|"max"|"share", "having": <having>},
   "group_by": <column>, "having": <having>,
   "output": "scalar"|"ranking"|"list"|"table"|"share"|"yesno", "as_percent": <bool>,
   "order": "desc"|"asc", "limit": <int>, "share_filters": [<filter>], "test": {"op": <cmp>, "value": <number>}}
  column  : "<col>" of the source, "orders.<col>" (the order of the row), "<table>.<col>" of a table that links to
            the source by order id or by product (see knowledge.detect_links)
  filter  : {"column": <column>, "op": <op>, "value": ...} | {"column": a, "op": <cmp>, "column2": b}
            | {"expr": <expr>, "op": <cmp>, "value": <number>}
  measure : {"agg": "sum"|"count"|"count_rows"|"count_distinct"|"avg"|"min"|"max", "expr": <expr>|null,
             "filters": [<filter>], "scope": <scope>, "period": <period>}
            | {"op": "+"|"-"|"*"|"/", "left": <measure>|<number>, "right": <measure>|<number>}
  expr    : <column> | <number> | {"op": "+"|"-"|"*"|"/", "left": <expr>, "right": <expr>}
            | {"days": [<date column>|"today"|"period_start"|"period_end", <same>]}
  having  : {"op": <cmp>, "value": <number>} on the group's measure, or with its own "agg"/"expr"/"filters"

Used only after the typed intent abstained because it could not express the question (see solver.FALLBACK_REASONS),
so answers that the intent path already gives are untouched.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from bizreason.contracts import AnswerType
from bizreason.semantic import knowledge, rulebook_params
from bizreason.semantic.compiler import _round2, _round_money, _status_sql
from bizreason.utils import normalize_lookup

AGGS = ("sum", "count", "count_rows", "count_distinct", "avg", "min", "max")
PER_AGGS = ("count", "avg", "sum", "min", "max", "share")
DATE_OPS = ("after_today", "before_today", "on_or_before_today", "on_or_after_today", "in_period", "before_period",
            "after_period", "on_or_before_period_end")
CMP = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "=", "ne": "<>"}
OPS = ("eq", "ne", "in", "not_in", "contains", "not_contains", "gt", "gte", "lt", "lte", "is_empty", "not_empty",
       *DATE_OPS)
NEGATIVE_OPS = {"ne": "eq", "not_in": "in", "not_contains": "contains"}
PERIOD_OPS = ("in_period", "before_period", "after_period", "on_or_before_period_end")
ARITH = ("+", "-", "*", "/")
OUTPUTS = ("scalar", "ranking", "list", "table", "share", "yesno")
SCOPES = ("valid", "cancelled", "all")
PERIODS = ("question", "comparison", "until_today", "none")
DATE_REFS = ("today", "period_start", "period_end")
SOURCES = knowledge.CANONICAL
MAX_ROWS = 50
# A filter column with more NULL cells than this share is not trusted (unless the shop recorded the blanks itself).
MAX_NULL_SHARE = 0.9
# Canonical columns that hold money; any other numeric column is money when its annotated role says so.
MONEY_COLUMNS = {
    "orders": {"revenue", "total_due", "paid", "outstanding", "line_total", "goods_total"},
    "lines": {"amount", "unit_price", "line_cost"},
    "purchases": {"unit_cost", "amount", "receipt_total"},
    "payments": {"amount"},
}
MONEY_ROLE_WORDS = ("amount", "price", "cost", "total", "fee", "paid", "money", "revenue", "discount", "debt",
                    "outstanding", "commission", "value", "deposit")
_NAME = re.compile(r"[a-z_][a-z0-9_]*")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# What the canonical columns hold, shown to the model next to the shop's kept columns.
GLOSS = {
    "orders": {"order_id": "order id", "date": "order date", "status": "order status", "customer_key": "customer id",
               "customer_name": "customer name", "customer_group": "customer group", "employee": "staff",
               "channel": "sales channel", "payment_method": "payment method", "revenue": "order revenue",
               "total_due": "amount the customer must pay", "paid": "amount paid", "outstanding": "amount still owed"},
    "lines": {"order_id": "order id", "date": "order date", "status": "order status", "product_name": "product",
              "category": "product group", "unit": "unit sold", "qty": "quantity in the unit sold",
              "qty_base": "quantity in base units", "amount": "line revenue", "unit_price": "unit price",
              "line_cost": "cost of one unit sold", "product_kind": "product kind", "employee": "staff",
              "customer_name": "customer name", "channel": "sales channel"},
    "purchases": {"purchase_id": "receipt id", "date": "receipt date", "supplier_name": "supplier",
                  "product_name": "product", "qty": "quantity received", "unit": "unit received",
                  "unit_cost": "cost of one unit", "amount": "line cost", "cancelled": "1 when the receipt is cancelled"},
    "deliveries": {"order_id": "order id", "date": "delivery date", "trip": "delivery trip number",
                   "qty": "quantity delivered (base units)"},
    "payments": {"order_id": "order id", "date": "payment date", "amount": "amount paid", "method": "payment method"},
}

SYSTEM = (
    "You turn a shop owner's question (Vietnamese, often informal) into a query plan over the shop's tables. The "
    "tables, what each column holds (the shop's own header and README description) and how tables link are listed "
    "in the prompt. Reply with one JSON object:\n"
    '{"source": <table>, "scope": "valid"|"cancelled"|"all", "period": "question"|"comparison"|"until_today"|"none",'
    ' "filters": [<filter>], "measure": <measure>, "per": <per or null>, "group_by": <column or null>,'
    ' "having": <having or null>, "output": "scalar"|"ranking"|"list"|"table"|"share"|"yesno", "as_percent": false,'
    ' "order": "desc"|"asc", "limit": <int or null>, "share_filters": [<filter>], "test": <test or null>}\n'
    "column: a column of the source; 'orders.<col>' for the order of the row; '<table>.<col>' for a table listed as "
    "linking to orders or to lines products (filters on it keep source rows that have a matching row).\n"
    'filter: {"column": c, "op": op, "value": v}; ops eq, ne, in, not_in (value a list), contains, not_contains '
    "(text inside a cell that lists several things), gt, gte, lt, lte (a number, or a date 'YYYY-MM-DD'), is_empty, "
    "not_empty, and on a date column before_today, after_today, on_or_before_today, on_or_after_today, in_period, "
    "before_period, after_period, on_or_before_period_end. Compare two columns with "
    '{"column": a, "op": "lt", "column2": b}; compare a computed number with {"expr": <expr>, "op": "gt", "value": n}.\n'
    'measure: {"agg": "sum"|"avg"|"min"|"max"|"count"|"count_rows"|"count_distinct", "expr": <expr or null>, '
    '"filters": [...], "scope": ..., "period": ...} (filters/scope/period here apply to this aggregate only), or '
    '{"op": "+"|"-"|"*"|"/", "left": <measure or number>, "right": <measure or number>}. count with expr null '
    "counts orders on order tables and rows on other tables; count_rows counts rows; count_distinct counts different "
    'values of a column.\nexpr: a column, a number, {"op": "-", "left": e, "right": e}, or {"days": [a, b]} = days from '
    "date b to date a (a, b: date columns or \"today\", \"period_start\", \"period_end\").\n"
    'per: {"by": <column>, "agg": "count"|"avg"|"sum"|"min"|"max"|"share", "having": <having>}: first the measure for '
    "each value of by (each order, each customer, each day...), keep those meeting having, then aggregate them; "
    "share = percent of them meeting having.\n"
    'having: {"op": "gte", "value": 3} on the group\'s measure (or with its own "agg"/"expr").\n'
    "output: scalar (one number; as_percent true multiplies a ratio by 100), ranking (names of the top limit groups, "
    "limit 1 gives one name), list (names of all groups meeting having), table (each group with its value), "
    'share (percent of the measure that share_filters keep), yesno (test the number: {"op": "gt", "value": n}).\n'
    "Rules: scope 'valid' keeps orders that count as sales (cancelled and returned left out by the system); "
    "'cancelled' keeps cancelled orders; 'all' keeps every order. period 'question' applies the question's period to "
    "the order date (or the table's own date); 'comparison' applies the period the question compares with; "
    "'until_today' keeps everything up to the end of today; use 'none' with in_period on another date column when "
    "the period is about that date. Every period the question states must be used. When the question names a stored "
    "value (a product, person, kind) filter on it, copying the stored spelling. Use only listed tables and columns; "
    "number and date columns are compared as numbers and dates automatically. If the question cannot be answered "
    'from these tables, reply {"source": null}.\n'
    "Examples (synthetic tables):\n"
    '1. "How many customers bought 3 or more times this month?" -> {"source": "orders", "measure": {"agg": "count"}, '
    '"per": {"by": "customer_key", "agg": "count", "having": {"op": "gte", "value": 3}}, "output": "scalar"}\n'
    '2. "Average number of items in one order last week" -> {"source": "lines", "measure": {"agg": "sum", "expr": '
    '"qty"}, "per": {"by": "order_id", "agg": "avg"}, "output": "scalar"}\n'
    '3. "Up to today, how many instalment plans are overdue?" with src_instalments (plan_no; order_ref links '
    'orders.order_id; due_on date; settled_on date) -> {"source": "src_instalments", "period": "until_today", '
    '"filters": [{"column": "due_on", "op": "before_today"}, {"column": "settled_on", "op": "is_empty"}], '
    '"measure": {"agg": "count_distinct", "expr": "plan_no"}, "output": "scalar"}\n'
    '4. "What percent of orders this month were cancelled?" -> {"source": "orders", "scope": "all", "measure": {"op": '
    '"/", "left": {"agg": "count", "scope": "cancelled"}, "right": {"agg": "count"}}, "as_percent": true, '
    '"output": "scalar"}\n'
    '5. "How many repair tickets this month included a brake check?" where product_name lists all jobs of a ticket '
    '-> {"source": "lines", "filters": [{"column": "product_name", "op": "contains", "value": "brake check"}], '
    '"measure": {"agg": "count"}, "output": "scalar"}\n'
    '6. "Revenue of each staff member last month, highest first" -> {"source": "orders", "group_by": "employee", '
    '"measure": {"agg": "sum", "expr": "revenue"}, "output": "table", "order": "desc"}\n'
    '7. "Top 3 products by quantity this week" -> {"source": "lines", "group_by": "product_name", "measure": '
    '{"agg": "sum", "expr": "qty"}, "output": "ranking", "limit": 3}\n'
    '8. "Revenue this month minus last month, in percent of last month" -> {"source": "orders", "period": "none", '
    '"measure": {"op": "/", "left": {"op": "-", "left": {"agg": "sum", "expr": "revenue", "period": "question"}, '
    '"right": {"agg": "sum", "expr": "revenue", "period": "comparison"}}, "right": {"agg": "sum", "expr": '
    '"revenue", "period": "comparison"}}, "as_percent": true, "output": "scalar"}'
)


@dataclass
class PlanResult:
    type: AnswerType
    answer: Any
    plan: dict[str, Any]


class PlanError(Exception):
    pass


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in db.execute(f"PRAGMA table_info({_q(table)})")]
    except sqlite3.Error:
        return []


def _tables(db: sqlite3.Connection) -> list[str]:
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return [t for t in SOURCES if t in names] + sorted(n for n in names if n.startswith("src_"))


def schema_text(db: sqlite3.Connection, catalog: dict[tuple[str, str], dict[str, Any]] | None = None) -> str:
    """The shop's tables and columns: the whole knowledge card (kept for callers of the earlier interface)."""
    return knowledge.build_card(db).render()


# ------------------------------------------------------------------------------------------------- the compiler

@dataclass
class _Col:
    sql: str  # reference with its alias
    table: str
    column: str
    owner: str  # "source", "parent" (the row's order), "joined" (a unique linked row), "remote" (filter only)
    remote: tuple[str, str] | None = None  # (outer key sql, inner key sql template with {a}) for remote columns


@dataclass
class _Sql:
    text: str
    args: list[Any] = field(default_factory=list)


@dataclass
class _Value:
    sql: _Sql
    kind: str  # "count", "money", "number", "ratio", "date", "percent"


class PlanCompiler:
    """Validates a plan against the shop's layer and runs it as parameterised SQL."""

    def __init__(self, db: sqlite3.Connection, as_of: date, period: tuple[date, date] | None,
                 card: knowledge.ShopCard | None = None, comparison: tuple[date, date] | None = None):
        self.db = db
        self.as_of = as_of
        self.period = period
        self.comparison = comparison
        self.db.create_function("lookup", 1, lambda v: None if v is None else normalize_lookup(str(v)),
                                deterministic=True)
        self.card = card if card is not None else knowledge.build_card(db)
        self._cols: dict[str, list[str]] = {}

    # ---------------------------------------------------------------------------------------------- references

    def cols(self, table: str) -> list[str]:
        if table not in self._cols:
            self._cols[table] = _columns(self.db, table)
        return self._cols[table]

    def _setup(self, plan: dict[str, Any]) -> None:
        source = plan.get("source")
        if source not in _tables(self.db):
            raise PlanError(f"unknown source {source!r}")
        self.source = source
        self.joins: dict[str, tuple[str, str]] = {}  # table -> (alias, join sql)
        self.need_parent = False
        self.checked: list[_Sql] = []  # value filters, re-checked one by one when the result is zero
        cols = self.cols(source)
        if source == "orders":
            self.order_key = "t.order_id"
        elif source in SOURCES:
            self.order_key = "t.order_id" if "order_id" in cols and source != "purchases" else None
        else:
            link = self.card.link(source, "order")
            self.order_key = f"t.{_q(link.column)}" if link else None
        # several source rows per order: an order column summed over them would count each order several times
        link = self.card.link(source, "order")
        self.rows_per_order_unique = source == "orders" or bool(link and link.unique)

    def _ref(self, name: Any, purpose: str) -> _Col:
        """A plan column. purpose 'filter' allows a linked table with several rows per key (as a remote filter);
        'value' needs one value per source row."""
        if not isinstance(name, str):
            raise PlanError(f"bad column {name!r}")
        parts = name.split(".")
        if len(parts) > 2 or not all(_NAME.fullmatch(p) for p in parts):
            raise PlanError(f"bad column {name!r}")
        table, column = (parts[0], parts[1]) if len(parts) == 2 else (self.source, parts[0])
        if table not in _tables(self.db):
            raise PlanError(f"unknown table {table}")
        if column not in self.cols(table):
            raise PlanError(f"unknown column {table}.{column}")
        if table == self.source:
            return _Col(f"t.{_q(column)}", table, column, "source")
        if table == "orders":
            if self.order_key is None:
                raise PlanError(f"{self.source} has no link to orders")
            self.need_parent = True
            return _Col(f"o.{_q(column)}", table, column, "parent")
        outer, inner, unique = self._link(table)
        if purpose == "filter":
            return _Col("{a}." + _q(column), table, column, "remote", (outer, inner))
        if not unique:
            raise PlanError(f"join multiplies rows: {table} has several rows per {self.source} row")
        if table not in self.joins:
            alias = f"j{len(self.joins)}"
            self.joins[table] = (alias, f"LEFT JOIN {_q(table)} {alias} ON {inner.format(a=alias)} = {outer}")
        alias = self.joins[table][0]
        return _Col(f"{alias}.{_q(column)}", table, column, "joined")

    def _link(self, table: str) -> tuple[str, str, bool]:
        """(outer key sql, inner key template, one row per key) linking a table to the source rows."""
        order = self.card.link(table, "order")
        if order is not None and self.order_key is not None:
            return self.order_key, "{a}." + _q(order.column), order.unique
        product = self.card.link(table, "product")
        if product is not None:
            cols = self.cols(self.source)
            if product.kind == "product_id" and "product_id" in cols:
                return "t.product_id", "{a}." + _q(product.column), product.unique
            if product.kind == "product_name" and "product_name" in cols:
                return "lookup(t.product_name)", "lookup({a}." + _q(product.column) + ")", product.unique
        raise PlanError(f"{table} has no link to {self.source}")

    def _twin(self, col: _Col, prefix: str) -> str | None:
        base = col.column[2:] if col.column.startswith("x_") else col.column
        twin = prefix + base
        if col.column.startswith(prefix):
            return col.sql
        if twin in self.cols(col.table):
            return col.sql.rsplit(".", 1)[0] + "." + _q(twin)
        return None

    def _numeric(self, col: _Col) -> str:
        return self._twin(col, "xn_") or f"CAST({col.sql} AS REAL)"

    def _is_date(self, col: _Col) -> bool:
        if col.column in ("date",) and col.table in SOURCES or col.column.startswith("xd_"):
            return True
        if self._twin(col, "xd_") is not None:
            return True
        meta = self.card.column(col.table, col.column)
        return bool(meta and meta.value_type == "date")

    def _dated(self, col: _Col) -> str:
        if not self._is_date(col):
            raise PlanError(f"{col.table}.{col.column} is not a date column")
        return f"date({self._twin(col, 'xd_') or col.sql})"

    def _is_money(self, col: _Col) -> bool:
        if col.column in MONEY_COLUMNS.get(col.table, set()):
            return True
        meta = self.card.column(col.table, col.column)
        role = (meta.role if meta else "") or ""
        return any(word in role for word in MONEY_ROLE_WORDS)

    def _verbatim(self, col: _Col) -> bool:
        """A column copied cell by cell from the shop's sheet: its blanks are the shop's own record."""
        return col.table.startswith("src_") or (col.column.startswith("x_") and not col.column.startswith("x_cat_"))

    # ---------------------------------------------------------------------------------------------- conditions

    def _filters(self, items: Any) -> _Sql:
        """AND of the filters; filters on a linked table become [NOT] IN over that table, one per table and sign."""
        if items is None:
            return _Sql("1")
        if not isinstance(items, list):
            raise PlanError("filters must be a list")
        local: list[_Sql] = []
        remote: dict[tuple[str, bool], list[tuple[_Col, _Sql]]] = {}
        for item in items:
            if not isinstance(item, dict):
                raise PlanError(f"bad filter {item!r}")
            if "expr" in item and "column" not in item:
                local.append(self._expr_test(item))
                continue
            col = self._ref(item.get("column"), "filter")
            if col.owner != "remote":
                local.append(self._test(col, item))
                continue
            negative = item.get("op") in NEGATIVE_OPS
            positive = {**item, "op": NEGATIVE_OPS[item["op"]]} if negative else item
            inner = _Col(col.sql.format(a="e"), col.table, col.column, "remote")
            remote.setdefault((col.table, negative), []).append((col, self._test(inner, positive)))
        parts = list(local)
        for (table, negative), tests in remote.items():
            outer, inner_key = tests[0][0].remote
            where = " AND ".join(s.text for _, s in tests)
            args = [a for _, s in tests for a in s.args]
            sql = (f"{outer} {'NOT ' if negative else ''}IN (SELECT {inner_key.format(a='e')} FROM {_q(table)} e "
                   f"WHERE {inner_key.format(a='e')} IS NOT NULL AND {where})")
            parts.append(_Sql(sql, args))
        if not parts:
            return _Sql("1")
        return _Sql(" AND ".join(f"({p.text})" for p in parts), [a for p in parts for a in p.args])

    def _null_check(self, col: _Col, op: str) -> None:
        if op in ("is_empty", "not_empty") or self._verbatim(col):
            return
        total, filled = self.db.execute(
            f"SELECT COUNT(*), COUNT(NULLIF(TRIM(CAST({_q(col.column)} AS TEXT)), '')) FROM {_q(col.table)}").fetchone()
        if total and 1 - filled / total > MAX_NULL_SHARE:
            raise PlanError(f"filter column {col.table}.{col.column} is mostly empty")

    def _test(self, col: _Col, item: dict[str, Any]) -> _Sql:
        op, value = item.get("op"), item.get("value")
        if op not in OPS:
            raise PlanError(f"bad filter op {op!r}")
        self._null_check(col, op)
        if "column2" in item:
            if op not in CMP:
                raise PlanError("two columns are compared with gt, gte, lt, lte, eq or ne")
            other = self._ref(item["column2"], "value")
            if col.owner == "remote":
                raise PlanError("column2 cannot compare with a linked table's column")
            if self._is_date(col) and self._is_date(other):
                return _Sql(f"{self._dated(col)} {CMP[op]} {self._dated(other)}")
            return _Sql(f"{self._numeric(col)} {CMP[op]} {self._numeric(other)}")
        if op in ("eq", "ne") and isinstance(value, (int, float)) and not isinstance(value, bool):
            return _Sql(f"{self._numeric(col)} {CMP[op]} ?", [float(value)])
        if op in ("eq", "ne", "in", "not_in"):
            values = value if isinstance(value, list) else [value]
            keys = sorted({normalize_lookup(str(v)) for v in values if v is not None and str(v).strip()})
            if not keys or any(not k for k in keys):
                raise PlanError("empty value")
            # every value must exist in the column, or the condition names something the shop does not have
            stored = {r[0] for r in self.db.execute(
                f"SELECT DISTINCT lookup({_q(col.column)}) FROM {_q(col.table)}")}
            if not set(keys) <= stored:
                raise PlanError(f"value not stored in {col.table}.{col.column}: {sorted(set(keys) - stored)[:3]}")
            neg = "NOT " if op in ("ne", "not_in") else ""
            sql = _Sql(f"lookup({col.sql}) {neg}IN ({','.join('?' for _ in keys)})", keys)
            if not neg and col.owner != "remote":
                self.checked.append(sql)
            return sql
        if op in ("contains", "not_contains"):
            needle = normalize_lookup(str(value or ""))
            if not needle:
                raise PlanError("empty value")
            if self.db.execute(f"SELECT 1 FROM {_q(col.table)} WHERE lookup({_q(col.column)}) LIKE ? LIMIT 1",
                               [f"%{needle}%"]).fetchone() is None:
                raise PlanError(f"no stored value of {col.table}.{col.column} contains {needle!r}")
            neg = "NOT " if op == "not_contains" else ""
            sql = _Sql(f"COALESCE(lookup({col.sql}), '') {neg}LIKE ?", [f"%{needle}%"])
            if not neg and col.owner != "remote":
                self.checked.append(sql)
            return sql
        if op in ("gt", "gte", "lt", "lte"):
            if isinstance(value, str) and _ISO_DATE.fullmatch(value.strip()[:10]) and self._is_date(col):
                return _Sql(f"{self._dated(col)} {CMP[op]} ?", [value.strip()[:10]])
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise PlanError("comparison needs a number or a date") from exc
            return _Sql(f"{self._numeric(col)} {CMP[op]} ?", [number])
        if op in ("is_empty", "not_empty"):
            return _Sql(f"NULLIF(TRIM(CAST({col.sql} AS TEXT)), '') {'IS NULL' if op == 'is_empty' else 'IS NOT NULL'}")
        dated = self._dated(col)
        today = self.as_of.isoformat()
        if op in ("after_today", "before_today", "on_or_before_today", "on_or_after_today"):
            sign = {"after_today": ">", "before_today": "<", "on_or_before_today": "<=", "on_or_after_today": ">="}[op]
            return _Sql(f"{dated} {sign} ?", [today])
        if self.period is None:
            raise PlanError("no period in the question")
        start, end = self.period[0].isoformat(), self.period[1].isoformat()
        if op == "in_period":
            return _Sql(f"{dated} >= ? AND {dated} < ?", [start, end])
        if op == "before_period":
            return _Sql(f"{dated} < ?", [start])
        if op == "after_period":
            return _Sql(f"{dated} >= ?", [end])
        return _Sql(f"{dated} < ?", [end])  # on_or_before_period_end

    def _expr_test(self, item: dict[str, Any]) -> _Sql:
        if item.get("op") not in ("gt", "gte", "lt", "lte", "eq", "ne"):
            raise PlanError("an expression is compared with gt, gte, lt, lte, eq or ne")
        expr, _ = self._expr(item["expr"])
        try:
            number = float(item.get("value"))
        except (TypeError, ValueError) as exc:
            raise PlanError("comparison needs a number") from exc
        return _Sql(f"{expr} {CMP[item['op']]} ?", [number])

    # ---------------------------------------------------------------------------------------------- expressions

    def _date_point(self, ref: Any) -> str:
        if ref == "today":
            return f"'{self.as_of.isoformat()}'"
        if ref in ("period_start", "period_end"):
            if self.period is None:
                raise PlanError("no period in the question")
            point = self.period[0] if ref == "period_start" else self.period[1] - timedelta(days=1)
            return f"'{point.isoformat()}'"
        return self._dated(self._ref(ref, "value"))

    def _expr(self, expr: Any, depth: int = 0) -> tuple[str, set[str]]:
        """(sql, kinds) of a numeric expression; kinds holds 'money', 'other', 'ratio', 'days'."""
        if depth > 3:
            raise PlanError("expression too deep")
        if isinstance(expr, (int, float)) and not isinstance(expr, bool):
            return repr(float(expr)), set()
        if isinstance(expr, str):
            col = self._ref(expr, "value")
            if self._is_date(col) and not self._is_money(col):
                raise PlanError(f"{expr} is a date: use {{'days': [...]}}")
            return self._numeric(col), {"money" if self._is_money(col) else "other"} | (
                {"parent"} if col.owner == "parent" else set())
        if isinstance(expr, dict) and isinstance(expr.get("days"), list) and len(expr["days"]) == 2:
            a, b = (self._date_point(x) for x in expr["days"])
            return f"(julianday({a}) - julianday({b}))", {"days"}
        if isinstance(expr, dict) and expr.get("op") in ARITH:
            left, lk = self._expr(expr.get("left"), depth + 1)
            right, rk = self._expr(expr.get("right"), depth + 1)
            kinds = lk | rk
            if expr["op"] == "/":
                return f"(CAST({left} AS REAL) / NULLIF({right}, 0))", kinds | {"ratio"}
            if expr["op"] == "*" and lk and rk:
                kinds |= {"ratio"}
            return f"({left} {expr['op']} {right})", kinds
        raise PlanError(f"bad expression {expr!r}")

    # ---------------------------------------------------------------------------------------------- aggregates

    def _status(self, scope: str) -> str | None:
        if scope not in SCOPES:
            raise PlanError("bad scope")
        if scope == "all":
            return None
        if self.source == "purchases":
            return "COALESCE(t.cancelled, 0) = 0" if scope == "valid" and "cancelled" in self.cols("purchases") \
                else None
        if self.source in ("orders", "lines"):
            ref = "t.status"
        elif self.order_key is not None:
            self.need_parent = True
            ref = "o.status"
        else:
            return None
        params = rulebook_params.active()
        statuses = params.valid_sale_statuses if scope == "valid" else params.cancelled_statuses
        return _status_sql(ref, statuses)

    def _date_ref(self) -> str | None:
        if "date" in self.cols(self.source):
            return "t.date"
        if self.order_key is not None:
            self.need_parent = True
            return "o.date"
        return None

    def _period(self, period: str) -> _Sql | None:
        if period not in PERIODS:
            raise PlanError("bad period")
        if period == "none":
            return None
        ref = self._date_ref()
        if ref is None:
            raise PlanError("source has no date: use period none and in_period on one of its date columns")
        if period == "until_today":
            return _Sql(f"{ref} < ?", [(self.as_of + timedelta(days=1)).isoformat()])
        window = self.period if period == "question" else self.comparison
        if window is None:
            raise PlanError(f"no {period} period in the question")
        return _Sql(f"{ref} >= ? AND {ref} < ?", [window[0].isoformat(), window[1].isoformat()])

    def _measure(self, m: Any, depth: int = 0) -> _Value:
        if depth > 3:
            raise PlanError("measure too deep")
        if isinstance(m, (int, float)) and not isinstance(m, bool):
            return _Value(_Sql(repr(float(m))), "number")
        if not isinstance(m, dict):
            raise PlanError("bad measure")
        if m.get("op") in ARITH and "agg" not in m:
            left, right = self._measure(m.get("left"), depth + 1), self._measure(m.get("right"), depth + 1)
            op = m["op"]
            text = f"(CAST({left.sql.text} AS REAL) / NULLIF({right.sql.text}, 0))" if op == "/" else \
                f"({left.sql.text} {op} {right.sql.text})"
            if op == "/":
                kind = "ratio"
            elif op in "+-":
                kind = left.kind if left.kind == right.kind or right.kind == "number" else "number"
            else:
                kind = "number"
            return _Value(_Sql(text, left.sql.args + right.sql.args), kind)
        agg, expr = m.get("agg"), m.get("expr")
        if agg not in AGGS:
            raise PlanError("bad measure")
        conds: list[_Sql] = []
        if m.get("filters"):
            conds.append(self._filters(m["filters"]))
        if "scope" in m:
            status = self._status(m["scope"])
            if status:
                conds.append(_Sql(status))
        if m.get("period") not in (None, "none"):
            conds.append(self._period(m["period"]))
        cond = _Sql(" AND ".join(f"({c.text})" for c in conds), [a for c in conds for a in c.args]) if conds else None

        def gated(value: str) -> str:
            return f"CASE WHEN {cond.text} THEN {value} END" if cond else value

        args = list(cond.args) if cond else []
        if agg in ("count", "count_rows") and expr is None:
            key = self.order_key if agg == "count" and self.source in SOURCES and self.order_key else None
            if key:
                return _Value(_Sql(f"COUNT(DISTINCT {gated(key)})", args), "count")
            return _Value(_Sql(f"COUNT({gated('1')})", args), "count")
        if agg == "count_distinct":
            if not isinstance(expr, str):
                raise PlanError("count_distinct needs a column")
            col = self._ref(expr, "value")
            return _Value(_Sql(f"COUNT(DISTINCT {gated(f'lookup({col.sql})')})", args), "count")
        if expr is None:
            raise PlanError(f"{agg} needs an expression")
        if agg in ("min", "max") and isinstance(expr, str):
            col = self._ref(expr, "value")
            if self._is_date(col):
                return _Value(_Sql(f"{agg.upper()}({gated(self._dated(col))})", args), "date")
        sql, kinds = self._expr(expr)
        if "parent" in kinds and agg in ("sum", "avg", "count") and not self.rows_per_order_unique:
            raise PlanError(f"an order column is aggregated over {self.source} rows (several per order): "
                            "use source orders")
        if agg in ("count", "count_rows"):
            return _Value(_Sql(f"COUNT({gated(sql)})", args), "count")
        if "ratio" in kinds:
            kind = "ratio"
        elif kinds - {"parent"} == {"money"}:
            kind = "money"
        else:
            kind = "number"
        return _Value(_Sql(f"{agg.upper()}({gated(sql)})", args), kind)

    def _having(self, having: Any, measure: _Value) -> _Sql:
        """The test of a having on a group: on its own aggregate, or on the group's measure."""
        if not isinstance(having, dict) or having.get("op") not in CMP:
            raise PlanError("bad having")
        try:
            bound = float(having.get("value"))
        except (TypeError, ValueError) as exc:
            raise PlanError("having needs a number") from exc
        value = self._measure(having) if having.get("agg") is not None else measure
        return _Sql(f"{value.sql.text} {CMP[having['op']]} ?", value.sql.args + [bound])

    # --------------------------------------------------------------------------------------------------- running

    def run(self, plan: dict[str, Any]) -> tuple[AnswerType, Any]:
        if not isinstance(plan, dict):
            raise PlanError("plan is not an object")
        self._setup(plan)
        output = plan.get("output", "scalar")
        if output not in OUTPUTS:
            raise PlanError("bad output")
        # resolve every part first: references add the joins the FROM clause needs
        where: list[_Sql] = []
        status = self._status(plan.get("scope", "valid"))
        if status:
            where.append(_Sql(status))
        side_periods = _side_periods(plan.get("measure"))
        if "period" in plan and plan["period"] is not None:
            period = plan["period"]
        elif side_periods:
            period = "none"
        else:
            period = "question" if self.period else ("until_today" if self._date_ref() else "none")
        if side_periods and period != "none":
            raise PlanError("a measure with its own period needs the plan's period none")
        p = self._period(period)
        if p:
            where.append(p)
        base = list(where)
        where.append(self._filters(plan.get("filters")))
        measure = self._measure(plan.get("measure"))
        group, per = plan.get("group_by"), plan.get("per")
        gcol = self._ref(group, "value") if group else None
        pcol = per_having = having = part = None
        if per is not None:
            if not isinstance(per, dict) or per.get("agg") not in PER_AGGS:
                raise PlanError("bad per")
            pcol = self._ref(per.get("by"), "value")
            if per.get("having") is not None:
                per_having = self._having(per["having"], measure)
            if per["agg"] == "share" and per_having is None:
                raise PlanError("per share needs having")
            if plan.get("having") is not None:
                raise PlanError("having belongs inside per")
        elif plan.get("having") is not None:
            having = self._having(plan["having"], measure)
        if output == "share":
            if group or per:
                raise PlanError("share with grouping")
            if not plan.get("share_filters"):
                raise PlanError("share needs share_filters")
            part = self._filters(plan.get("share_filters"))
        frm = f"{_q(self.source)} t"
        if self.need_parent:
            frm += f" JOIN orders o ON o.order_id = {self.order_key}"
        for _, join in self.joins.values():
            frm += " " + join
        clause = _joined(where)
        base_clause = _joined(base)
        as_percent = bool(plan.get("as_percent"))

        if output == "share":
            whole = self._scalar(measure.sql, frm, clause)
            share = self._scalar(measure.sql, frm, _joined([clause, part]))
            if not whole or share is None:
                raise PlanError("empty share")
            return AnswerType.PERCENT, _round2(100.0 * share / whole)
        if per is not None:
            return self._run_per(plan, per, pcol, gcol, measure, per_having, frm, clause, output, as_percent,
                                 base_clause)
        if output in ("scalar", "yesno"):
            if gcol is not None:
                raise PlanError(f"{output} with grouping")
            value = self._scalar(measure.sql, frm, clause)
            self._zero_check(value, frm, base_clause)
            return self._final(value, measure.kind, as_percent, plan, output)
        if gcol is None:
            raise PlanError(f"{output} needs group_by")
        hsql = f" HAVING {having.text}" if having else ""
        sql = (f"SELECT {gcol.sql}, {measure.sql.text} AS v FROM {frm} WHERE {clause.text} AND {gcol.sql} IS NOT NULL "
               f"GROUP BY lookup({gcol.sql}){hsql}")
        rows = self.db.execute(sql, measure.sql.args + clause.args + (having.args if having else [])).fetchall()
        return self._groups(rows, plan, output, measure.kind, as_percent, gcol)

    def _scalar(self, measure: _Sql, frm: str, clause: _Sql) -> Any:
        return self.db.execute(f"SELECT {measure.text} FROM {frm} WHERE {clause.text}",
                               measure.args + clause.args).fetchone()[0]

    def _run_per(self, plan, per, pcol, gcol, measure, having, frm, clause, output, as_percent, base_clause):
        outer_key = f"{gcol.sql} AS g, " if gcol else ""
        gkey = f"lookup({gcol.sql}), " if gcol else ""
        not_null = f" AND {pcol.sql} IS NOT NULL" + (f" AND {gcol.sql} IS NOT NULL" if gcol else "")
        flag, hsql, hargs, fargs = "", "", [], []
        if per["agg"] == "share":
            flag, fargs = f", CASE WHEN {having.text} THEN 1 ELSE 0 END AS h", having.args
        elif having is not None:
            hsql, hargs = f" HAVING {having.text}", having.args
        inner = (f"SELECT {outer_key}{pcol.sql} AS k, {measure.sql.text} AS v{flag} FROM {frm} "
                 f"WHERE {clause.text}{not_null} GROUP BY {gkey}lookup({pcol.sql}){hsql}")
        args = measure.sql.args + fargs + clause.args + hargs
        agg = per["agg"]
        outer = {"count": "COUNT(*)", "share": "100.0 * SUM(h) / COUNT(*)"}.get(agg, f"{agg.upper()}(v)")
        kind = {"count": "count", "share": "percent"}.get(agg, measure.kind)
        if agg == "avg" and kind == "count":
            kind = "number"
        if gcol is None:
            if output not in ("scalar", "yesno"):
                raise PlanError(f"{output} needs group_by")
            value = self.db.execute(f"SELECT {outer} FROM ({inner})", args).fetchone()[0]
            if agg == "count" and not value:
                self._zero_check(0, frm, base_clause)
            return self._final(value, kind, as_percent, plan, output)
        rows = self.db.execute(f"SELECT MIN(g), {outer} AS v FROM ({inner}) GROUP BY lookup(g)", args).fetchall()
        return self._groups(rows, plan, output, kind, as_percent, gcol)

    def _zero_check(self, value: Any, frm: str, base: _Sql) -> None:
        """A zero is trusted only when every value filter matches some row in scope and period on its own."""
        if value is None:
            raise PlanError("empty result")
        try:
            zero = float(value) == 0
        except (TypeError, ValueError):
            return
        if not zero:
            return
        for test in self.checked:
            row = self.db.execute(f"SELECT 1 FROM {frm} WHERE {base.text} AND ({test.text}) LIMIT 1",
                                  base.args + test.args).fetchone()
            if row is None:
                raise PlanError("a filter value matched no rows in the scope and period")

    def _final(self, value: Any, kind: str, as_percent: bool, plan: dict[str, Any],
               output: str) -> tuple[AnswerType, Any]:
        kind_out, number = _rounded(value, kind, as_percent)
        if output == "yesno" and kind_out == AnswerType.DATE:
            raise PlanError("yesno on a date")
        if output == "yesno":
            test = plan.get("test")
            if not isinstance(test, dict) or test.get("op") not in CMP:
                raise PlanError("yesno needs a test")
            try:
                bound = float(test.get("value"))
            except (TypeError, ValueError) as exc:
                raise PlanError("test needs a number") from exc
            ops = {"gt": float.__gt__, "gte": float.__ge__, "lt": float.__lt__, "lte": float.__le__,
                   "eq": float.__eq__, "ne": float.__ne__}
            return AnswerType.BOOLEAN, bool(ops[test["op"]](float(number), bound))
        return kind_out, number

    def _groups(self, rows: list[tuple[Any, Any]], plan: dict[str, Any], output: str, kind: str,
                as_percent: bool, gcol: _Col) -> tuple[AnswerType, Any]:
        rows = [(name, value) for name, value in rows if value is not None and name is not None]
        if not rows:
            raise PlanError("no groups")
        desc = plan.get("order", "desc") != "asc"
        rows.sort(key=lambda r: ((-1 if desc else 1) * float(r[1]), normalize_lookup(str(r[0]))))
        limit = plan.get("limit")
        if output == "ranking" and limit is None:
            limit = 1
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                raise PlanError("bad limit")
            if len(rows) > limit and round(float(rows[limit - 1][1]), 6) == round(float(rows[limit][1]), 6):
                raise PlanError("tie at the cut of the ranking")
            rows = rows[:limit]
        elif len(rows) > MAX_ROWS:
            raise PlanError("too many groups")
        date_group = self._is_date(gcol)

        def name(n: Any) -> str:
            return str(n)[:10] if date_group else str(n)

        if output == "ranking" and limit == 1:
            return AnswerType.STRING, name(rows[0][0])
        if output in ("ranking", "list"):
            return AnswerType.LIST, [name(n) for n, _ in rows]
        if output != "table":
            raise PlanError(f"{output} with grouping")
        return AnswerType.TABLE, {"columns": ["name", "value"],
                                  "rows": [[name(n), _rounded(v, kind, as_percent)[1]] for n, v in rows]}


def _joined(parts: list[_Sql]) -> _Sql:
    if not parts:
        return _Sql("1")
    return _Sql(" AND ".join(f"({p.text})" for p in parts), [a for p in parts for a in p.args])


def _side_periods(measure: Any) -> bool:
    """True when some aggregate of the measure has its own period."""
    if not isinstance(measure, dict):
        return False
    if measure.get("op") in ARITH and "agg" not in measure:
        return _side_periods(measure.get("left")) or _side_periods(measure.get("right"))
    return measure.get("period") not in (None, "none")


def _rounded(value: Any, kind: str, as_percent: bool) -> tuple[AnswerType, Any]:
    """Money to an integer (half up), percents and other fractions to two places, counts to integers."""
    if value is None:
        raise PlanError("empty result")
    if kind == "date":
        return AnswerType.DATE, str(value)[:10]
    value = float(value)
    if as_percent:
        return AnswerType.PERCENT, _round2(100.0 * value)
    if kind == "percent":
        return AnswerType.PERCENT, _round2(value)
    if kind == "count" or abs(value - round(value)) < 1e-9:
        return AnswerType.NUMBER, int(round(value))
    if kind == "money" or (kind == "number" and abs(value) >= 1000):
        return AnswerType.NUMBER, _round_money(value)
    return AnswerType.NUMBER, _round2(value)


def _number(value: Any, plan: dict[str, Any]) -> tuple[AnswerType, Any]:
    agg = (plan.get("measure") or {}).get("agg")
    return _rounded(value, "count" if agg in ("count", "count_distinct", "count_rows") else "number", False)


# ---------------------------------------------------------------------------------- checks against the question

def _all_filters(plan: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("filters", "share_filters"):
                for item in node.get(key) or []:
                    if isinstance(item, dict):
                        found.append(item)
            for key in ("measure", "left", "right", "having", "per"):
                walk(node.get(key))
    walk(plan)
    return found


def _mentions_period(plan: dict[str, Any], which: str) -> bool:
    text = json.dumps(plan)
    if which == "comparison":
        return '"comparison"' in text
    if '"period": "question"' in text:
        return True
    if plan.get("period") is None and not _side_periods(plan.get("measure")):
        return True  # the compiler applies the question's period by default
    if any(item.get("op") in PERIOD_OPS for item in _all_filters(plan)):
        return True
    return '"period_start"' in text or '"period_end"' in text


def named_values(question: str, index: Any) -> list[str]:
    """Stored values the question names word for word (a product, a person, a kind), from a ValueIndex.

    Only values of two or more words, or one word of at least six characters or with a digit, are kept: a short
    single word is too often an ordinary word of the question."""
    if index is None:
        return []
    try:
        found = index.candidates(question)
    except Exception:
        return []
    asked = f" {_canonical(question)} "
    out = []
    for dim, values in found.items():
        if dim == "unit":
            continue
        for value in values:
            norm = _canonical(value)
            words = norm.split()
            if not words or norm.replace(" ", "").isdigit():
                continue
            if len(words) < 2 and not (len(norm) >= 6 or any(ch.isdigit() for ch in norm)):
                continue
            if f" {norm} " in asked and value not in out:
                out.append(value)
    return out


def _canonical(text: str) -> str:
    return re.sub(r"(?<=[0-9])(?=[a-z])|(?<=[a-z])(?=[0-9])", " ", normalize_lookup(text))


def _today_relative(plan: dict[str, Any]) -> bool:
    text = json.dumps(plan)
    return '"until_today"' in text or '"today"' in text or any(
        str(item.get("op", "")).endswith("_today") for item in _all_filters(plan))


def question_check(plan: dict[str, Any], period: tuple[date, date] | None, comparison: tuple[date, date] | None,
                   named: list[str], as_of: date | None = None) -> str | None:
    """Why a plan does not answer the question it was made for (a stated period or a named value left unused).

    A period of today alone is also used by a condition relative to today ("up to today", "due after today")."""
    today_only = period is not None and as_of is not None and period == (as_of, as_of + timedelta(days=1))
    if period is not None and not _mentions_period(plan, "question") and not (today_only and _today_relative(plan)):
        return "plan ignores the question's period"
    if comparison is not None and not _mentions_period(plan, "comparison"):
        return "plan ignores the period the question compares with"
    used: list[str] = []
    for item in _all_filters(plan):
        values = item.get("value")
        for v in values if isinstance(values, list) else [values]:
            if isinstance(v, str) and v.strip():
                used.append(f" {_canonical(v)} ")
    for value in named:
        norm = f" {_canonical(value)} "
        if not any(norm in u or u in norm for u in used if u.strip()):
            return f"plan ignores {value!r} named in the question"
    return None


# ------------------------------------------------------------------------------------------------ the fallback

class PlanFallback:
    """Asks the model for a plan and runs it; None when the plan is missing, invalid, unchecked or empty."""

    # Besides the intent's 'cannot express' reasons, a value the intent could not resolve: a plan may find it in a
    # kept column (a brand or size attached from the product list); equality values are checked against the data.
    reasons = (
        "outside known measures", "measure ", "ranking of ", "group by ", "dimension ", "shape ",
        "unknown filter dimension", "second measure on shape", "table with a second measure", "threshold on shape",
        "threshold metric", "average per weekday", "location filter", "delivery trips with a filter",
        "purchase amount with filter", "debt measure with filter", "breakdown names", "invalid measure",
        "unresolved product", "unresolved product_group", "unresolved unit", "empty share", "empty result",
        "line filter on an order column", "filter on a joined table", "order total with a line filter",
    )

    def __init__(self, model: Any, temporal: Any, definitions: Any = None, value_index: Any = None,
                 timeout: float = 120.0, card_budget: int = 1200):
        self.model = model
        self.temporal = temporal
        self.definitions = definitions  # callable(question) -> str block, or None
        self.value_index = value_index  # callable(connection) -> ValueIndex, or None
        self.timeout = timeout
        self.card_budget = card_budget
        self.last_reason = ""
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[knowledge.ShopCard, Any]] = {}

    def _shop(self, connection: sqlite3.Connection) -> tuple[knowledge.ShopCard, Any]:
        """The shop's card and value index, built once per layer file."""
        row = connection.execute("PRAGMA database_list").fetchone()
        path = str(row[2]) if row and row[2] else ""
        key = f"{path}:{Path(path).stat().st_mtime_ns}" if path and Path(path).exists() else ""
        with self._lock:
            if key and key in self._cache:
                return self._cache[key]
        card = knowledge.build_card(connection)
        index = self.value_index(connection) if self.value_index is not None else None
        if key:
            with self._lock:
                self._cache[key] = (card, index)
        return card, index

    def prompt(self, question: str, card: knowledge.ShopCard, index: Any, as_of: date,
               period: tuple[date, date] | None, comparison: tuple[date, date] | None) -> str:
        parts = [f"Question: {question}", f"Today: {as_of.isoformat()}"]
        if period == (as_of, as_of + timedelta(days=1)):
            parts.append(f"Question's period: today only ({as_of.isoformat()}); a question about everything up to "
                         "today uses period until_today instead")
        elif period:
            parts.append(f"Question's period: {period[0].isoformat()} to {(period[1] - timedelta(days=1)).isoformat()}")
        else:
            parts.append("The question states no period.")
        if comparison:
            parts.append(f"Period it compares with: {comparison[0].isoformat()} to "
                         f"{(comparison[1] - timedelta(days=1)).isoformat()}")
        parts.append(card.select(question, self.card_budget))
        if index is not None:
            block = index.block(question)
            if block:
                parts.append(block)
        if self.definitions is not None:
            defined = self.definitions(question)
            if defined:
                parts.append(defined)
        return "\n\n".join(parts)

    def answer(self, question: str, connection: sqlite3.Connection, as_of: date) -> PlanResult | None:
        try:
            windows = self.temporal.resolve(question, as_of) if self.temporal is not None else []
            target = [w for w in windows if w.role != "comparison"]
            compared = [w for w in windows if w.role == "comparison"]
            period = (target[0].start, target[0].end_exclusive) if len(target) == 1 else None
            if len(target) > 1 or len(compared) > 1:
                self.last_reason = "plan: several periods"
                return None
            comparison = (compared[0].start, compared[0].end_exclusive) if compared else None
            card, index = self._shop(connection)
            text = self.prompt(question, card, index, as_of, period, comparison)
            plan = self.model.generate_json(SYSTEM, text, timeout=self.timeout)
            if not isinstance(plan, dict) or not plan.get("source"):
                self.last_reason = "no plan"
                return None
            problem = question_check(plan, period, comparison, named_values(question, index), as_of)
            if problem:
                raise PlanError(problem)
            kind, value = PlanCompiler(connection, as_of, period, card=card, comparison=comparison).run(plan)
            self.last_reason = "answered by plan"
            return PlanResult(kind, value, plan)
        except PlanError as exc:
            self.last_reason = f"plan rejected: {exc}"
            return None
        except Exception as exc:
            self.last_reason = f"plan failed: {type(exc).__name__}"
            return None
