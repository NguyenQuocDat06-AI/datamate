"""Build canonical business tables from column meanings; independent of source table names."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from bizreason.contracts import BusinessContext, TableMeta
from bizreason.data.parsing import ParseStatus, parse_date, parse_number
from bizreason.semantic.vocabulary import (
    CANCEL_TOKENS,
    CURRENCY_SUFFIXES,
    INGESTION_ROLES,
    SERVICE_FEE_NAMES,
    WALK_IN_NAMES,
)
from bizreason.utils import normalize_lookup, quote_identifier as q

LINE_FIELDS = {
    "order_id": "order_id", "date": "ordered_at", "ts": "ordered_at", "status": "order_status",
    "product_id": "product_id", "product_name": "product_name", "customer_id": "customer_id",
    "customer_name": "customer_name", "customer_group": "customer_group", "employee": "employee_id",
    "channel": "channel", "payment_method": "payment_method", "unit": "unit", "category": "product_category",
}
LINE_NUMBERS = {
    "qty": "qty", "qty_base": "qty_base", "unit_price": "unit_price", "line_amount": "line_amount",
    "line_discount": "line_discount", "line_cost": "line_cost", "goods_total": "order_goods_total",
    "total_due": "order_total_due", "paid": "order_paid", "outstanding": "order_outstanding",
    "order_discount": "order_discount", "shipping_fee": "shipping_fee",
}
PURCHASE_FIELDS = {
    "purchase_id": "purchase_id", "date": "purchased_at", "supplier_id": "supplier_id",
    "supplier_name": "supplier_name", "status": "purchase_status", "product_id": "product_id",
    "product_name": "product_name",
}
PURCHASE_NUMBERS = {
    "qty": "qty_purchased", "unit_cost": "purchase_unit_cost", "amount": "purchase_line_amount",
    "receipt_total": "purchase_total",
}
PAYMENT_FIELDS = {"order_id": "order_id", "date": "paid_at", "method": "payment_method"}
PAYMENT_NUMBERS = {"amount": "paid_amount"}
CANONICAL_STATUS = ("completed", "shipping", "cancelled", "returned")
LINE_KINDS = ("sales", "service_ticket")  # kinds whose rows become raw_lines
SOURCE_ROW = "__source_row"  # ingestion adds this column to every source table
LINE_ITEM_KINDS = ("product", "discount", "shipping_fee", "service_fee")  # what a sale line can be classified as
NOT_REVENUE_LINE_KINDS = ("discount", "shipping_fee")  # lines that neither add to revenue nor appear in lines

_CURRENCY = "(?:" + "|".join(re.escape(suffix) for suffix in CURRENCY_SUFFIXES) + ")?"
COMMA_GROUPED = re.compile(r"[+-]?\d{1,3}(?:,\d{3})+\s*" + _CURRENCY, re.IGNORECASE)
DOT_GROUPED = re.compile(r"[+-]?\d{1,3}(?:\.\d{3})+\s*" + _CURRENCY, re.IGNORECASE)
TIME_OF_DAY = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")


def _number(value: Any, locale: str) -> float | None:
    result = parse_number(value, locale=locale, unit="VND")
    return float(result.value) if result.status == ParseStatus.OK else None


def to_number(value: Any) -> float | None:
    return _number(value, "unknown")


def to_date(value: Any) -> str | None:
    result = parse_date(value)
    return str(result.value)[:10] if result.status == ParseStatus.OK else None


def to_timestamp(order_time: Any, order_date: Any) -> str | None:
    """A full datetime is kept as it is. A time of day is joined to the row's date. Anything else is None."""
    if order_time is None or not str(order_time).strip():
        return None
    text = str(order_time).strip()
    clock = TIME_OF_DAY.fullmatch(text)
    if clock:
        day = to_date(order_date)
        hour, minute, second = int(clock[1]), int(clock[2]), int(clock[3] or 0)
        if day is None or hour > 23 or minute > 59 or second > 59:
            return None
        return f"{day} {hour:02d}:{minute:02d}:{second:02d}"
    result = parse_date(text)
    if result.status == ParseStatus.OK and " " in str(result.value):
        return str(result.value)
    return None


def infer_locale(values: Iterable[Any]) -> str | None:
    """Read one column's amount separators, or None to keep the default reading.

    "en": every value containing a comma is comma-grouped, so the comma is a thousands separator.
    "vi": the mirror rule for dots. A column mixing both shapes, or holding a value the other
    locale would misread, keeps the default reading.
    """
    texts = [str(value).strip() for value in values if value is not None and str(value).strip()]
    with_comma = [text for text in texts if "," in text]
    with_dot = [text for text in texts if "." in text]
    if with_comma and all(COMMA_GROUPED.fullmatch(text) for text in with_comma) \
            and not any(DOT_GROUPED.fullmatch(text) for text in texts):
        return "en"
    if with_dot and all(DOT_GROUPED.fullmatch(text) for text in with_dot) \
            and not any(COMMA_GROUPED.fullmatch(text) for text in texts):
        return "vi"
    return None


def annotations_by_signature(records: list[dict[str, Any]]) -> dict[tuple[str, ...], dict[str, Any]]:
    return {tuple(r["columns"]): r for r in records if "kind" in r}


def effective_roles(table: TableMeta, note: dict[str, Any]) -> dict[str, str]:
    """Role to column. The model's reading wins; ingestion evidence fills anything it left open."""
    given = note.get("roles") or {}
    chosen: dict[str, str] = {}
    fallback: dict[str, str] = {}
    for column in table.columns:
        role = given.get(column.name)
        if isinstance(role, str) and role != "other":
            chosen.setdefault(role, column.name)
        mapped = INGESTION_ROLES.get(column.semantic_role or "")
        if mapped:
            fallback.setdefault(mapped, column.name)
    for role in ("order_status", "ordered_at", "qty_base"):
        if role in fallback:  # parsed and normalized at ingestion; stronger than a column-name reading
            chosen[role] = fallback[role]
    for role, column in fallback.items():
        chosen.setdefault(role, column)
    return chosen


def _text(column: str | None) -> str:
    return f"NULLIF(TRIM(CAST(src.{q(column)} AS TEXT)),'')" if column else "NULL"


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _column_locale(dst: sqlite3.Connection, table: TableMeta, column: str) -> str | None:
    rows = dst.execute(
        f"SELECT DISTINCT CAST(src.{q(column)} AS TEXT) FROM source.{q(table.name)} AS src "
        f"WHERE src.{q(column)} IS NOT NULL"
    )
    return infer_locale(row[0] for row in rows)


def _distinct_where(dst: sqlite3.Connection, table: TableMeta) -> str | None:
    """Keep the first of each run of source rows identical in every column (bookkeeping columns aside)."""
    columns = [
        row[0] for row in dst.execute("SELECT name FROM pragma_table_info(?, 'source')", (table.name,))
        if not row[0].startswith("__")
    ]
    if not columns:
        return None
    group = ",".join(q(column) for column in columns)
    return f"src.rowid IN (SELECT MIN(rowid) FROM source.{q(table.name)} GROUP BY {group})"


def _insert(dst: sqlite3.Connection, target: str, table: TableMeta, roles: dict[str, str],
            texts: dict[str, str], numbers: dict[str, str], synthetic_prefix: str | None = None, *,
            distinct: bool = False, report: dict[str, Any] | None = None) -> int:
    names, exprs = [], []
    for field, role in texts.items():
        column = roles.get(role)
        names.append(field)
        if field == "date":
            exprs.append(f"to_date(src.{q(column)})" if column else "NULL")
        elif field == "ts" and roles.get("order_time"):
            # the order time supplies the clock; the stored date text is the fallback when it is blank
            day = f"src.{q(column)}" if column else "NULL"
            exprs.append(f"COALESCE(to_timestamp(src.{q(roles['order_time'])}, {day}), {_text(column)})")
        elif field == "order_id" and column is None and synthetic_prefix is not None:
            # no order column: each row is its own order, keyed by the table and the row's source position
            exprs.append(f"{_sql_text(synthetic_prefix)} || src.{q(SOURCE_ROW)}")
        else:
            exprs.append(_text(column))
    for field, role in numbers.items():
        column = roles.get(role)
        names.append(field)
        if column is None:
            exprs.append("NULL")
            continue
        locale = _column_locale(dst, table, column)
        if locale and report is not None:
            report["columns_with_locale_inferred"] += 1
        exprs.append(f"to_number_{locale}(src.{q(column)})" if locale else f"to_number(src.{q(column)})")
    where = _distinct_where(dst, table) if distinct else None
    cursor = dst.execute(
        f"INSERT INTO {target} ({','.join(names)}) SELECT {','.join(exprs)} "
        f"FROM source.{q(table.name)} AS src" + (f" WHERE {where}" if where else "")
    )
    if where and report is not None:
        total = dst.execute(f"SELECT COUNT(*) FROM source.{q(table.name)}").fetchone()[0]
        report["duplicate_lines_removed"] += total - cursor.rowcount
    return cursor.rowcount


def _load_lines(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str],
                synthetic_prefix: str, has_product: int, report: dict[str, Any], *, distinct: bool = False) -> None:
    _insert(dst, "raw_lines", table, roles, LINE_FIELDS, LINE_NUMBERS, synthetic_prefix,
            distinct=distinct, report=report)
    dst.execute("UPDATE raw_lines SET has_product=? WHERE has_product IS NULL", (has_product,))


def _load_returns(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str]) -> None:
    """Collect the order ids a returns table lists; blank ids are ignored."""
    column = roles.get("order_id")
    if not column:
        return
    dst.execute(
        f"INSERT INTO temp.returned_orders (order_id) SELECT DISTINCT id FROM "
        f"(SELECT {_text(column)} AS id FROM source.{q(table.name)} AS src) WHERE id IS NOT NULL"
    )


def _load_customer_groups(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str]) -> None:
    group = roles.get("customer_group")
    if not group:
        return
    dst.execute(
        "INSERT INTO temp.customer_groups (customer_id, name_key, customer_group) "
        f"SELECT cid, NULLIF(lookup(nm), ''), grp FROM (SELECT {_text(roles.get('customer_id'))} AS cid, "
        f"{_text(roles.get('customer_name'))} AS nm, {_text(group)} AS grp "
        f"FROM source.{q(table.name)} AS src) WHERE grp IS NOT NULL AND (cid IS NOT NULL OR nm IS NOT NULL)"
    )


def _load_product_facts(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str]) -> None:
    """Category and base unit per product, from a product catalog or a unit conversion table."""
    if not (roles.get("product_id") or roles.get("product_name")):
        return
    if not (roles.get("product_category") or roles.get("base_unit")):
        return
    dst.execute(
        "INSERT INTO temp.product_facts (product_id, name_key, category, base_unit) "
        f"SELECT pid, NULLIF(lookup(nm), ''), cat, base FROM (SELECT {_text(roles.get('product_id'))} AS pid, "
        f"{_text(roles.get('product_name'))} AS nm, {_text(roles.get('product_category'))} AS cat, "
        f"{_text(roles.get('base_unit'))} AS base FROM source.{q(table.name)} AS src) "
        "WHERE pid IS NOT NULL OR nm IS NOT NULL"
    )


def _load_aliases(dst: sqlite3.Connection, table: TableMeta, roles: dict[str, str]) -> None:
    """Alias to full product name. When an alias repeats, the first row wins."""
    key, name = roles.get("alias_key"), roles.get("product_name")
    if not key or not name:
        return
    dst.execute(
        "INSERT OR IGNORE INTO temp.alias_map (alias_key, full_name) "
        f"SELECT ak, fn FROM (SELECT NULLIF(lookup({_text(key)}), '') AS ak, {_text(name)} AS fn "
        f"FROM source.{q(table.name)} AS src) WHERE ak IS NOT NULL AND fn IS NOT NULL"
    )


def _resolve_aliases(dst: sqlite3.Connection) -> int:
    """A line whose name is an alias takes the full product name. Only exact normalized matches change."""
    return dst.execute(
        "UPDATE raw_lines SET product_name=(SELECT a.full_name FROM temp.alias_map a "
        "WHERE a.alias_key=lookup(raw_lines.product_name)) "
        "WHERE lookup(raw_lines.product_name) IN (SELECT alias_key FROM temp.alias_map)"
    ).rowcount


def _classify_lines(dst: sqlite3.Connection, pairs: list[tuple[str, str]]) -> int:
    """Set line_kind on raw_lines by normalized product name; unmapped lines are products.

    Returns the number of lines that are not products.
    """
    dst.executemany("INSERT OR REPLACE INTO temp.line_kind_map (name, kind) VALUES (?,?)", pairs)
    dst.execute(
        "UPDATE raw_lines SET line_kind=COALESCE("
        "(SELECT m.kind FROM temp.line_kind_map m WHERE m.name=lookup(product_name)), 'product')"
    )
    return dst.execute("SELECT COUNT(*) FROM raw_lines WHERE line_kind<>'product'").fetchone()[0]


def _map_status(dst: sqlite3.Connection, status_map: dict[str, str]) -> int:
    """Canonical values stay; mapped values are rewritten; everything else becomes NULL. Returns mapped count."""
    values = [row[0] for row in dst.execute("SELECT DISTINCT status FROM raw_lines WHERE status IS NOT NULL")]
    mapped = 0
    for value in values:
        if value in CANONICAL_STATUS:
            continue
        target = status_map.get(value)
        if target in CANONICAL_STATUS:
            mapped += 1
            dst.execute("UPDATE raw_lines SET status=? WHERE status=?", (target, value))
        else:
            dst.execute("UPDATE raw_lines SET status=NULL WHERE status=?", (value,))
    return mapped


def _register(con: sqlite3.Connection) -> None:
    con.create_function("to_number", 1, to_number)
    con.create_function("to_number_en", 1, lambda v: _number(v, "en"))
    con.create_function("to_number_vi", 1, lambda v: _number(v, "vi"))
    con.create_function("to_date", 1, to_date)
    con.create_function("to_timestamp", 2, to_timestamp)
    con.create_function("lookup", 1, lambda v: None if v is None else normalize_lookup(v))


def build(context: BusinessContext, records: list[dict[str, Any]], path: Path,
          status_map: dict[str, str] | None = None, line_kinds: dict[str, str] | None = None) -> dict[str, Any]:
    """line_kinds maps a product name (normalized by normalize_lookup) to a LINE_ITEM_KINDS value."""
    pairs: list[tuple[str, str]] = []
    for name, kind in (line_kinds or {}).items():
        if kind not in LINE_ITEM_KINDS:
            raise ValueError(f"unknown line kind {kind!r}; expected one of {LINE_ITEM_KINDS}")
        pairs.append((normalize_lookup(name), kind))
    notes = annotations_by_signature(records)
    path.unlink(missing_ok=True)
    dst = sqlite3.connect(path)
    dst.close()
    dst = sqlite3.connect(f"file:{path}", uri=True)
    _register(dst)
    dst.execute("ATTACH DATABASE ? AS source", (str(context.database),))
    line_columns = list(LINE_FIELDS) + list(LINE_NUMBERS) + ["has_product", "line_kind"]
    dst.execute(f"CREATE TABLE raw_lines ({','.join(line_columns)})")
    purchase_columns = list(PURCHASE_FIELDS) + list(PURCHASE_NUMBERS)
    dst.execute(f"CREATE TABLE purchases ({','.join(purchase_columns)})")
    dst.execute("CREATE TABLE payments (order_id, date, amount, method)")
    dst.execute("CREATE TEMP TABLE returned_orders (order_id TEXT)")
    dst.execute("CREATE TEMP TABLE line_kind_map (name TEXT PRIMARY KEY, kind TEXT)")
    dst.execute("CREATE TEMP TABLE customer_groups (customer_id TEXT, name_key TEXT, customer_group TEXT)")
    dst.execute("CREATE TEMP TABLE product_facts (product_id TEXT, name_key TEXT, category TEXT, base_unit TEXT)")
    dst.execute("CREATE TEMP TABLE alias_map (alias_key TEXT PRIMARY KEY, full_name TEXT)")
    report: dict[str, Any] = {
        "sales_tables": 0, "service_tables": 0, "purchase_tables": 0, "return_tables": 0, "unannotated": [],
        "duplicate_lines_removed": 0, "columns_with_locale_inferred": 0,
    }
    for table in context.tables:
        note = notes.get(tuple(c.name for c in table.columns))
        if note is None:
            report["unannotated"].append(table.name)
            continue
        roles = effective_roles(table, note)
        if not roles.get("employee_id") and roles.get("employee_name"):
            roles["employee_id"] = roles["employee_name"]
        synthetic = f"{table.name}:"  # order id prefix for rows that have no order column
        if note["kind"] == "sales":
            product = roles.get("product_name") or roles.get("product_id")
            # rows of one order that repeat in every cell are one export error; rows without an order id are not
            _load_lines(dst, table, roles, synthetic, 1 if product else 0, report,
                        distinct=bool(roles.get("order_id")))
            report["sales_tables"] += 1
        elif note["kind"] == "service_ticket":
            # one row per visit: the ticket is the order, its amount is the line, the work done is the product
            ticket = {role: column for role, column in roles.items() if role != "order_id"}
            if roles.get("ticket_id"):
                ticket["order_id"] = roles["ticket_id"]
            if roles.get("ticket_amount"):
                ticket["line_amount"] = roles["ticket_amount"]
            if roles.get("service_item"):
                ticket["product_name"] = roles["service_item"]
            _load_lines(dst, table, ticket, synthetic, 1, report)
            report["service_tables"] += 1
        elif note["kind"] == "purchase" and (roles.get("purchase_id") or roles.get("purchased_at")):
            _insert(dst, "purchases", table, roles, PURCHASE_FIELDS, PURCHASE_NUMBERS, report=report)
            report["purchase_tables"] += 1
        elif note["kind"] == "payment" and (roles.get("order_id") or roles.get("paid_at")):
            _insert(dst, "payments", table, roles, PAYMENT_FIELDS, PAYMENT_NUMBERS, report=report)
        elif note["kind"] == "sales_return":
            _load_returns(dst, table, roles)
            report["return_tables"] += 1
        elif note["kind"] == "customer_catalog":
            _load_customer_groups(dst, table, roles)
        elif note["kind"] in ("product_catalog", "unit_conversion"):
            _load_product_facts(dst, table, roles)
        elif note["kind"] == "alias":
            _load_aliases(dst, table, roles)
    report["alias_lines_resolved"] = _resolve_aliases(dst)
    report["non_product_lines"] = _classify_lines(dst, pairs)
    report["status_mapped"] = _map_status(dst, status_map or {})
    _derive(dst)
    report["orders_marked_returned"] = _mark_returned(dst)
    report["status_null_orders"] = dst.execute("SELECT COUNT(*) FROM orders WHERE status IS NULL").fetchone()[0]
    report["orders"] = dst.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    report["lines"] = dst.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
    report["purchases"] = dst.execute("SELECT COUNT(*) FROM purchases").fetchone()[0]
    report["payments"] = dst.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    dst.commit()
    dst.execute("DETACH DATABASE source")
    dst.commit()
    dst.close()
    return report


def _mark_returned(dst: sqlite3.Connection) -> int:
    """Orders listed by a returns table are returned whole; cancelled orders stay cancelled.

    Returns the number of orders newly set to returned.
    """
    marked = dst.execute(
        "UPDATE orders SET status='returned' WHERE order_id IN (SELECT order_id FROM temp.returned_orders) "
        "AND COALESCE(status,'') NOT IN ('cancelled','returned')"
    ).rowcount
    dst.execute(
        "UPDATE lines SET status=(SELECT o.status FROM orders o WHERE o.order_id=lines.order_id) "
        "WHERE order_id IN (SELECT order_id FROM temp.returned_orders)"
    )
    status = ",".join(_sql_text(value) for value in CANONICAL_STATUS)
    dst.execute("DELETE FROM status_domain")
    dst.execute(f"INSERT INTO status_domain SELECT DISTINCT status FROM orders WHERE status IN ({status})")
    return marked


def _derive(dst: sqlite3.Connection) -> None:
    _derive_orders(dst)
    _fill_customer_groups(dst)
    _derive_lines(dst)
    tokens = " OR ".join(f"lookup(status) LIKE '%{t}%'" for t in CANCEL_TOKENS)
    dst.execute("ALTER TABLE purchases ADD COLUMN cancelled")
    dst.execute(f"UPDATE purchases SET cancelled=CASE WHEN status IS NOT NULL AND ({tokens}) THEN 1 ELSE 0 END")


def _derive_orders(dst: sqlite3.Connection) -> None:
    walk_in = ",".join(f"'{name}'" for name in WALK_IN_NAMES)
    not_revenue = ",".join(_sql_text(kind) for kind in NOT_REVENUE_LINE_KINDS)
    dst.executescript(
        f"""
        CREATE INDEX raw_order ON raw_lines(order_id);
        CREATE TABLE orders AS
        SELECT order_id,
               MIN(date) AS date, MIN(COALESCE(ts, date)) AS ts,
               CASE WHEN SUM(status='returned')>0 THEN 'returned'
                    WHEN SUM(status='cancelled')>0 THEN 'cancelled'
                    WHEN SUM(status='shipping')>0 THEN 'shipping'
                    WHEN SUM(status='completed')>0 THEN 'completed' END AS status,
               MAX(customer_id) AS customer_id, MAX(customer_name) AS customer_name,
               MAX(customer_group) AS customer_group, MAX(employee) AS employee,
               MAX(channel) AS channel, MAX(payment_method) AS payment_method,
               SUM(CASE WHEN has_product=1 AND line_kind NOT IN ({not_revenue}) THEN COALESCE(
                    line_amount, qty*unit_price-COALESCE(line_discount,0)) END) AS line_total,
               MAX(goods_total) AS goods_total, MAX(total_due) AS total_due,
               MAX(paid) AS paid, MAX(outstanding) AS outstanding
        FROM raw_lines WHERE order_id IS NOT NULL GROUP BY order_id;
        ALTER TABLE orders ADD COLUMN revenue;
        UPDATE orders SET revenue=COALESCE(line_total, goods_total);
        ALTER TABLE orders ADD COLUMN customer_key;
        UPDATE orders SET customer_key=CASE
            WHEN EXISTS (SELECT 1 FROM orders o WHERE o.customer_id IS NOT NULL) THEN customer_id
            WHEN customer_name IS NULL OR lookup(customer_name) IN ({walk_in}) THEN NULL
            ELSE lookup(customer_name) END;
        CREATE UNIQUE INDEX orders_id ON orders(order_id);
        CREATE INDEX orders_date ON orders(date);
        """
    )


def _fill_customer_groups(dst: sqlite3.Connection) -> None:
    """An order with no group takes its customer's group from a customer catalog: by id when the order
    has an id, by normalized name when it has none (two codes sharing a name are two customers)."""
    dst.execute(
        "UPDATE orders SET customer_group = COALESCE("
        "(SELECT MIN(m.customer_group) FROM temp.customer_groups m "
        " WHERE orders.customer_id IS NOT NULL AND m.customer_id = orders.customer_id), "
        "(SELECT MIN(m.customer_group) FROM temp.customer_groups m "
        " WHERE orders.customer_id IS NULL AND m.name_key = NULLIF(lookup(orders.customer_name), ''))) "
        "WHERE customer_group IS NULL"
    )


def _product_fact(column: str) -> str:
    """A catalog value for a line: by product id, else by normalized product name."""
    return (
        f"COALESCE((SELECT MIN(f.{column}) FROM temp.product_facts f "
        f"WHERE l.product_id IS NOT NULL AND f.product_id = l.product_id), "
        f"(SELECT MIN(f.{column}) FROM temp.product_facts f "
        f"WHERE f.name_key = NULLIF(lookup(l.product_name), '')))"
    )


def _derive_lines(dst: sqlite3.Connection) -> None:
    not_revenue = ",".join(_sql_text(kind) for kind in NOT_REVENUE_LINE_KINDS)
    status = "','".join(CANONICAL_STATUS)
    service_fees = ",".join(_sql_text(normalize_lookup(name)) for name in SERVICE_FEE_NAMES)
    dst.executescript(
        f"""
        CREATE TABLE lines AS
        SELECT l.order_id, o.date, o.ts, o.status, o.customer_key, o.customer_name,
               o.customer_group, o.channel, o.payment_method, o.employee,
               l.product_id, l.product_name, COALESCE(l.product_id, lookup(l.product_name)) AS product_key,
               COALESCE(l.category, {_product_fact('category')}) AS category,
               l.unit, l.qty, l.qty_base,
               COALESCE(l.line_amount, l.qty*l.unit_price-COALESCE(l.line_discount,0)) AS amount,
               l.line_cost, l.unit_price,
               CASE WHEN l.line_kind='service_fee' OR lookup(l.product_name) IN ({service_fees}) THEN 0 ELSE 1 END
                   AS is_item,
               {_product_fact('base_unit')} AS base_unit
        FROM raw_lines l JOIN orders o USING(order_id)
        WHERE l.has_product=1 AND l.line_kind NOT IN ({not_revenue})
          AND (l.product_name IS NOT NULL OR l.product_id IS NOT NULL);
        CREATE INDEX lines_date ON lines(date);
        CREATE TABLE status_domain AS SELECT DISTINCT status FROM orders WHERE status IN ('{status}');
        """
    )
