"""Two-stage semantic annotation of shop tables: classify the kind, then assign a role per column.

Stage 1 picks a table kind from KINDS. Stage 2 assigns each column one role from the
role list for that kind (SCOPE). Tables whose column names are identical share one
model call. Nothing here is specific to a particular dataset.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from bizreason.contracts import BusinessContext, ColumnMeta, TableMeta
from bizreason.llm import LanguageModel
from bizreason.semantic.layer import CANONICAL_STATUS, LINE_KINDS, annotations_by_signature, effective_roles
from bizreason.utils import normalize_lookup, quote_identifier as q

KINDS = {
    "sales": "orders or order lines of goods sold to customers",
    "payment": "one row per money receipt paid by a customer for an order",
    "purchase": "goods bought from suppliers (import receipts and their lines)",
    "inventory": "stock on hand or stock movements",
    "lot": "batches or lots of goods with expiry dates, or sales per lot",
    "product_catalog": "list of products with attributes",
    "customer_catalog": "list of customers",
    "employee_catalog": "list of staff",
    "unit_conversion": "conversion between large and base units",
    "price_history": "price changes over time",
    "expense": "operating expense ledger",
    "shift_report": "cash report per work shift",
    "sales_return": "returned goods or refunds",
    "service_ticket": "repair or service visits, one row per visit",
    "delivery": "delivery trips of orders",
    "debt_ledger": "credit sale or debt notes, installment plans",
    "warranty": "warranty claims",
    "alias": "abbreviation or alternative-name lookup",
    "promotion": "promotion programs",
    "other": "none of the above",
}
C = {
    "order_id": "identifier of the order or bill",
    "ordered_at": "date or datetime the order was placed",
    "order_time": "time of day the order was placed",
    "order_status": "state of the order (done, cancelled, returned, shipping)",
    "order_note": "free-text note on the order",
    "product_id": "product code",
    "product_name": "product name",
    "customer_id": "customer code",
    "customer_name": "customer name",
    "employee_id": "staff code",
    "employee_name": "staff name or nickname",
    "payment_method": "how it was paid (cash, transfer, cod, debt)",
    "note": "free-text note",
    "other": "anything else",
}
SALE = dict(C) | {
    "unit": "unit written on this line", "qty": "quantity in the unit of the line",
    "qty_base": "quantity converted to the base unit", "unit_price": "price of one unit",
    "line_discount": "discount of this line", "line_amount": "money of this line after line discount",
    "line_cost": "cost recorded on the sale line", "order_goods_total": "goods total of the whole order",
    "order_discount": "discount applied to the whole order", "shipping_fee": "delivery fee of the order",
    "order_total_due": "what the customer must pay for the whole order",
    "order_paid": "amount the customer already paid for the order",
    "order_outstanding": "amount still owed on the order",
    "customer_group": "customer segment or type", "channel": "sales channel",
    "shift": "work shift", "imei": "device serial number",
}
SCOPE = {
    "sales": SALE,
    "payment": {k: C[k] for k in ("order_id", "payment_method", "customer_id", "customer_name", "note", "other")}
    | {"payment_id": "receipt identifier", "paid_at": "date of this payment", "paid_amount": "money of this payment"},
    "purchase": {k: C[k] for k in ("product_id", "product_name", "note", "other")}
    | {"purchase_id": "import receipt identifier", "purchased_at": "receipt date", "supplier_id": "supplier code",
       "supplier_name": "supplier name", "unit": "unit of the line", "qty_purchased": "quantity imported in the line unit",
       "qty_purchased_base": "quantity imported in base units", "purchase_unit_cost": "import price of one unit",
       "purchase_line_amount": "money of this line", "purchase_total": "total of the whole receipt",
       "purchase_status": "state of the receipt", "lot_id": "batch code", "expires_at": "expiry date"},
    "inventory": {k: C[k] for k in ("product_id", "product_name", "note", "other")}
    | {"inventory_qty": "quantity on hand", "inventory_date": "date of the snapshot or movement", "unit": "unit"},
    "lot": {k: C[k] for k in ("product_id", "product_name", "order_id", "note", "other")}
    | {"lot_id": "batch code", "expires_at": "expiry date", "qty_purchased_base": "quantity received in base units",
       "qty_sold_base": "quantity sold from the lot in base units", "qty_returned": "quantity returned to the lot",
       "purchase_unit_cost": "import price"},
    "product_catalog": {k: C[k] for k in ("product_id", "product_name", "note", "other")}
    | {"product_category": "product group or category", "brand": "brand", "size": "size or number attribute",
       "unit": "selling unit", "base_unit": "smallest unit", "large_unit": "larger packing unit",
       "conversion_factor": "base units per large unit", "catalog_cost": "unit cost in the catalog",
       "catalog_price": "list price", "active_ingredient": "active ingredient of a drug",
       "prescription_flag": "whether a prescription is required", "product_alias": "other names of the product"},
    "customer_catalog": {k: C[k] for k in ("customer_id", "customer_name", "note", "other")}
    | {"customer_group": "customer segment or type", "customer_phone": "phone", "registered_at": "date joined"},
    "employee_catalog": {k: C[k] for k in ("employee_id", "employee_name", "note", "other")}
    | {"hired_at": "date joined", "commission_rate": "commission percent"},
    "unit_conversion": {k: C[k] for k in ("product_id", "product_name", "note", "other")}
    | {"base_unit": "smallest unit", "large_unit": "larger unit", "conversion_factor": "base units per large unit"},
    "price_history": {k: C[k] for k in ("product_id", "product_name", "note", "other")}
    | {"new_price": "new price", "old_price": "previous price", "price_effective_at": "date the price applies"},
    "expense": {"expense_date": "date", "expense_amount": "amount", "expense_category": "group", "note": C["note"], "other": C["other"]},
    "shift_report": {"shift": "work shift", "report_date": "date", "cash_collected": "cash received",
                     "cash_refunded": "cash paid back", "net_cash": "net cash", "note": C["note"], "other": C["other"]},
    "sales_return": {k: C[k] for k in ("order_id", "product_id", "product_name", "note", "other")}
    | {"returned_at": "return date", "qty_returned": "quantity returned", "refund_amount": "money refunded"},
    "service_ticket": {k: C[k] for k in ("employee_id", "employee_name", "customer_name", "order_status", "note", "other")}
    | {"ticket_id": "ticket number", "ordered_at": "visit date", "service_item": "work done",
       "ticket_amount": "total money of the ticket", "vehicle_plate": "license plate", "vehicle_model": "vehicle model"},
    "delivery": {k: C[k] for k in ("order_id", "note", "other")}
    | {"delivered_at": "delivery date", "delivery_trip": "trip number of the order",
       "delivery_qty": "quantity delivered"},
    "debt_ledger": {k: C[k] for k in ("order_id", "customer_id", "customer_name", "ordered_at", "note", "other")}
    | {"order_outstanding": "amount still owed", "paid_amount": "amount paid later", "due_at": "due date",
       "credit_days": "allowed days of credit", "installment_count": "number of installments", "installment_amount": "money per installment"},
    "warranty": {k: C[k] for k in ("product_name", "note", "other")} | {"received_at": "date received", "imei": "device serial number", "sold_at": "date sold"},
    "alias": {"alias_key": "the short or alternative name", "product_name": "the full name", "product_kind": "kind of product", "note": C["note"], "other": C["other"]},
    "promotion": {"promo_name": "program", "valid_from": "start", "valid_to": "end", "product_name": "product", "note": C["note"], "other": C["other"]},
    "other": dict(C),
}
EX = 'Example (invented shop). Columns "ref", "paid_on", "memo" give {"ref":"order_id","paid_on":"paid_at","memo":"note"}.'
# Role stage: a table with more columns than ROLE_SINGLE_CALL_MAX is asked in batches straight away; a
# failed single call is retried as batches of ROLE_BATCH_SIZE columns.
ROLE_SINGLE_CALL_MAX = 20
ROLE_BATCH_SIZE = 15
# Kinds of sales-line names, used by classify_line_names.
LINE_NAME_KINDS = {
    "product": "goods or services sold",
    "discount": "voucher, points used, or price reduction lines",
    "shipping_fee": "delivery fee lines",
    "service_fee": "a service charge added to the bill",
}
LINE_NAME_BATCH = 120
# Table kind implied by the grain ingestion measured from the data. Used when the kind call fails or answers "other".
GRAIN_KIND = {
    "order_line": "sales",
    "order_header": "sales",
    "purchase_line": "purchase",
    "purchase_header": "purchase",
    "product_dimension": "product_catalog",
    "payment_event": "payment",
    "unit_conversion": "unit_conversion",
    "inventory_event": "inventory",
    "snapshot": "inventory",
}
# A sales table needs at least one of these roles, unless its grain is a sales grain.
SALES_EVIDENCE_ROLES = ("order_id", "ordered_at", "qty", "line_amount", "order_goods_total", "order_total_due")


def group_tables(context: BusinessContext) -> list[list[TableMeta]]:
    """Group tables whose column-name tuples are identical; groups keep first-seen order."""
    grouped: dict[tuple[str, ...], list[TableMeta]] = {}
    for table in context.tables:
        grouped.setdefault(tuple(column.name for column in table.columns), []).append(table)
    return list(grouped.values())


def needs_reannotation(record: dict[str, Any], group: list[TableMeta]) -> bool:
    """True when a stored record failed, or answered "other" for a table whose grain implies a kind."""
    if "error" in record:
        return True
    return record.get("kind") == "other" and bool(group) and group[0].grain in GRAIN_KIND


def relevant_readme(readme: str, table: TableMeta, limit: int = 3500) -> str:
    """Paragraphs mentioning this table's source file or column headers, in document order."""
    stem = Path(table.sources[0].path).stem.lower() if table.sources else ""
    headers = [str(c.original_name).lower() for c in table.columns if len(str(c.original_name)) >= 3]
    parts = [p for p in re.split(r"\n\s*\n|\n(?=#)", readme) if p.strip()]
    scored = []
    for i, p in enumerate(parts):
        low = p.lower()
        score = (5 if stem and stem in low else 0) + sum(h in low for h in headers)
        scored.append((score, i, p))
    keep: set[int] = set()
    total = 0
    for score, i, p in sorted(scored, key=lambda x: (-x[0], x[1])):
        if score <= 0 or total + len(p) > limit:
            continue
        keep.add(i)
        total += len(p)
    return "\n".join(p for _, i, p in scored if i in keep) or readme[:limit]


def _describe_columns(columns: list[ColumnMeta]) -> str:
    return "\n".join(
        f"- {c.name} | header: {c.original_name} | samples: "
        f"{json.dumps(c.examples[:4], ensure_ascii=False, default=str)[:100]}"
        for c in columns
    )


def _describe(group: list[TableMeta]) -> str:
    return _describe_columns(group[0].columns)


STATUS_MEANINGS = {
    "completed": "finished order",
    "shipping": "being delivered",
    "cancelled": "order cancelled",
    "returned": "goods returned, fully or partly",
    "unknown": "anything else",
}


def _role_list(options: dict[str, str]) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in options.items())


def order_status_values(context: BusinessContext, records: list[dict[str, Any]]) -> list[str]:
    """Distinct raw order_status values of the tables that feed raw_lines, excluding canonical ones.

    Ingestion stores a status it does not recognize as NULL; the original cell is read from __raw_json, where
    each source row is a JSON array in column order.
    """
    notes = annotations_by_signature(records)
    found: set[str] = set()
    con = sqlite3.connect(f"file:{context.database}?mode=ro", uri=True)
    try:
        for table in context.tables:
            note = notes.get(tuple(c.name for c in table.columns))
            if note is None or note["kind"] not in LINE_KINDS:
                continue
            column = effective_roles(table, note).get("order_status")
            if not column:
                continue
            position = [c.name for c in table.columns].index(column)
            rows = con.execute(
                f"SELECT DISTINCT COALESCE(NULLIF(TRIM(CAST({q(column)} AS TEXT)),''), "
                f"NULLIF(TRIM(CAST(json_extract({q('__raw_json')}, '$[{position}]') AS TEXT)),'')) "
                f"FROM {q(table.name)}"
            ).fetchall()
            found.update(row[0] for row in rows if row[0] is not None)
    finally:
        con.close()
    return sorted(value for value in found if value not in CANONICAL_STATUS)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


class Annotator:
    def __init__(self, model: LanguageModel, *, timeout: float = 280.0, workers: int = 4):
        self.model = model
        self.timeout = timeout
        self.workers = workers

    def annotate(self, context: BusinessContext) -> list[dict[str, Any]]:
        groups = group_tables(context)
        if not groups:
            return []
        with ThreadPoolExecutor(self.workers) as pool:
            return list(pool.map(lambda group: self._annotate_retrying(context, group), groups))

    def _annotate_retrying(self, context: BusinessContext, group: list[TableMeta]) -> dict[str, Any]:
        """One retry for a failed group, so a transient model error does not skip its tables."""
        record = self._annotate_group(context, group)
        if "error" in record:
            # Only the retry may fall back to kind "other" when the kind call keeps failing.
            record = self._annotate_group(context, group, kind_fallback=True)
        return record

    def classify_status_values(self, values: list[str]) -> dict[str, str]:
        """Map raw order status values to a meaning in STATUS_MEANINGS; invalid answers are dropped."""
        if not values:
            return {}
        system = (
            "Classify each order status value of a shop into exactly one meaning. Reply with JSON only: one object "
            "mapping each input value to one of these labels:\n" + _role_list(STATUS_MEANINGS)
        )
        try:
            reply = self.model.generate_json(system, "Values:\n" + json.dumps(values, ensure_ascii=False),
                                             timeout=self.timeout)
        except Exception:
            return {}
        if not isinstance(reply, dict):
            return {}
        return {
            value: reply[value]
            for value in values
            if isinstance(reply.get(value), str) and reply[value] in STATUS_MEANINGS
        }

    def annotate_status(self, context: BusinessContext, records: list[dict[str, Any]]) -> dict[str, str]:
        """Raw order status value to canonical status for every non-canonical value; unknown answers are skipped."""
        classified = self.classify_status_values(order_status_values(context, records))
        return {value: meaning for value, meaning in classified.items() if meaning != "unknown"}

    def classify_line_names(self, names: list[str]) -> dict[str, str]:
        """Map line names to a LINE_NAME_KINDS label. Invalid answers are dropped; a failed batch contributes nothing."""
        distinct = list(dict.fromkeys(names))
        system = (
            "Classify each line name of a shop's sales lines into exactly one kind. Reply with JSON only: one object "
            "mapping each input name to one of these labels:\n" + _role_list(LINE_NAME_KINDS)
        )
        result: dict[str, str] = {}
        for start in range(0, len(distinct), LINE_NAME_BATCH):
            batch = distinct[start:start + LINE_NAME_BATCH]
            try:
                reply = self.model.generate_json(system, "Names:\n" + json.dumps(batch, ensure_ascii=False),
                                                 timeout=self.timeout)
            except Exception:
                continue
            if not isinstance(reply, dict):
                continue
            result.update({
                name: reply[name]
                for name in batch
                if isinstance(reply.get(name), str) and reply[name] in LINE_NAME_KINDS
            })
        return result

    def annotate_line_kinds(self, context: BusinessContext, records: list[dict[str, Any]],
                            catalog_names: list[str], sold_names: list[str]) -> dict[str, str]:
        """Kind of each sold name that the product catalog does not list, keyed by normalize_lookup(name).

        Without a catalog nothing is classified and the model is not called. `records` is accepted for the
        common stage signature and is not read here.
        """
        if not catalog_names:
            return {}
        catalogued = {normalize_lookup(name) for name in catalog_names}
        unlisted: dict[str, str] = {}
        for name in sold_names:
            key = normalize_lookup(name)
            if key and key not in catalogued:
                unlisted.setdefault(key, name)
        if not unlisted:
            return {}
        classified = self.classify_line_names(list(unlisted.values()))
        return {normalize_lookup(name): kind for name, kind in classified.items()}

    def _ask_roles(self, doc: str, columns: list[ColumnMeta], roles: dict[str, str]) -> dict[str, str]:
        """One role call for the given columns; roles outside the scope become "other"."""
        reply = self.model.generate_json(
            "Give the meaning of every column of a Vietnamese shop data table. Use the documentation first, then "
            'the header and samples. Reply with one JSON object mapping each column name to one role. Use "other" '
            f"only when no role fits.\n{EX}\nRoles:\n{_role_list(roles)}",
            f"Documentation:\n{doc}\n\nColumns:\n{_describe_columns(columns)}",
            timeout=self.timeout,
        )
        if not isinstance(reply, dict):
            raise TypeError("role reply is not a JSON object")
        return {
            column: (role if isinstance(role, str) and role in roles else "other")
            for column, role in reply.items()
        }

    def _assign_roles(self, doc: str, table: TableMeta, roles: dict[str, str]) -> tuple[dict[str, str], bool]:
        """Roles for every column of the table, asked in batches when needed.

        Returns (roles, partial). Raises the last model error when every call failed.
        """
        columns = list(table.columns)
        if len(columns) <= ROLE_SINGLE_CALL_MAX:
            try:
                return self._ask_roles(doc, columns, roles), False
            except Exception:
                pass  # retried below as batches
        batches = [columns[i:i + ROLE_BATCH_SIZE] for i in range(0, len(columns), ROLE_BATCH_SIZE)] or [[]]
        merged: dict[str, str] = {}
        failed = 0
        last_error: Exception | None = None
        for batch in batches:
            try:
                merged.update(self._ask_roles(doc, batch, roles))
            except Exception as exc:
                failed += 1
                last_error = exc
        if failed == len(batches) and last_error is not None:
            raise last_error
        return merged, failed > 0

    def _annotate_group(self, context: BusinessContext, group: list[TableMeta], kind_fallback: bool = False) -> dict[str, Any]:
        table = group[0]
        doc = relevant_readme(context.readme, table)
        record: dict[str, Any] = {
            "biz": context.business_id,
            "table": table.name,
            "columns": [column.name for column in table.columns],
        }
        started = time.time()
        try:
            kind_source = "model"
            try:
                kind_reply = self.model.generate_json(
                    'Classify a Vietnamese shop data table. Reply with JSON only: {"kind": "<kind>"}.\n'
                    "Kinds:\n" + _role_list(KINDS),
                    f"Documentation:\n{doc}\n\nFile {table.sources[0].path}, table {table.name}:\n{_describe(group)}",
                    timeout=self.timeout,
                )
                kind = kind_reply.get("kind")
            except Exception:
                if not kind_fallback:
                    raise
                kind = None
            if not isinstance(kind, str) or kind not in KINDS:
                kind = "other"
            if kind == "other" and table.grain in GRAIN_KIND:
                kind = GRAIN_KIND[table.grain]
                kind_source = "grain"
            roles, partial = self._assign_roles(doc, table, SCOPE[kind])
            if kind == "sales" and GRAIN_KIND.get(table.grain) != "sales" and not any(
                role in SALES_EVIDENCE_ROLES for role in roles.values()
            ):
                # Not a sales table: keep only the roles that the "other" scope also defines.
                kind = "other"
                roles = {column: (role if role in SCOPE["other"] else "other") for column, role in roles.items()}
            record["kind"] = kind
            record["kind_source"] = kind_source
            record["roles"] = roles
            if partial:
                record["partial"] = True
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        record["seconds"] = round(time.time() - started, 1)
        return record


def load_or_annotate(annotator: Annotator, context: BusinessContext, cache_path: Path) -> list[dict[str, Any]]:
    """Return cached records when they match context.revision; otherwise annotate and cache atomically."""
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    if isinstance(cached, dict) and cached.get("revision") == context.revision and isinstance(cached.get("records"), list):
        return cached["records"]

    records = annotator.annotate(context)
    if any("error" in record for record in records):
        return records  # a transient model failure must not persist
    _write_json_atomic(cache_path, {"revision": context.revision, "records": records})
    return records


def load_status_map(annotator: Annotator, context: BusinessContext, records: list[dict[str, Any]],
                    cache_path: Path) -> dict[str, str]:
    """Return the cached status map when it matches context.revision; otherwise classify and cache it.

    A non-empty list of values that yields an empty map is a failure and is not cached.
    """
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    if isinstance(cached, dict) and cached.get("revision") == context.revision and isinstance(cached.get("status_map"), dict):
        return cached["status_map"]

    status_map = annotator.annotate_status(context, records)
    if not status_map and order_status_values(context, records):
        return status_map  # a transient model failure must not persist
    _write_json_atomic(cache_path, {"revision": context.revision, "status_map": status_map})
    return status_map
