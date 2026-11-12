"""Two-stage semantic annotation of shop tables: classify the kind, then assign a role per column.

Stage 1 picks a table kind from KINDS. Stage 2 assigns each column one role from the
role list for that kind (SCOPE). Tables whose column names are identical share one
model call. Nothing here is specific to a particular dataset.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
from pathlib import Path
from typing import Any

from bizreason.contracts import BusinessContext, ColumnMeta, TableMeta
from bizreason.llm import LanguageModel
from bizreason.semantic.layer import (
    CANONICAL_STATUS, LINE_KINDS, ModelFailure, annotations_by_signature, effective_roles, _load_catalog_keys,
)
from bizreason.semantic.vocabulary import PAYMENT_CANONICAL
from bizreason.utils import normalize_lookup, quote_identifier as q

KINDS = {
    "sales": "transactions in which the shop sells goods or services to customers — invoices, orders, bookings, tickets, treatments, service orders — one row per transaction or per line",
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
    "service_ticket": "repair, maintenance or service jobs done for a customer (visits, service tickets, work orders), one row per visit or job",
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
# Value grouping: roles whose distinct values the model groups by spelling, and the largest list sent per call.
GROUPED_ROLES = ("channel", "customer_group", "product_category", "unit", "base_unit")
VALUE_ROLES = ("payment_method",) + GROUPED_ROLES
VALUE_GROUP_BATCH = 120
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
# An "other" table is promoted to sales when its roles hold an order id, a date and one of these money roles.
PROMOTION_MONEY_ROLES = ("line_amount", "order_goods_total", "order_total_due")
logger = logging.getLogger(__name__)
# A failed table takes the kind and roles of a sibling table of its shop whose column names are this alike: the
# Jaccard similarity of the normalized name sets, or, for equal column counts, the share of equal names by position.
SIBLING_MIN_SHARE = 0.8
# The retried kind call lists at most this many headers, with no documentation and no sample values.
KIND_SHORT_COLUMNS = 40
# A role call is tried this many times, with the same prompt, before its batch counts as failed.
ROLE_CALL_ATTEMPTS = 3


def _has_order_roles(roles: Any) -> bool:
    """True when the roles hold an order id, a date of the order and a money amount of the order."""
    found = set(roles)
    return "order_id" in found and "ordered_at" in found and any(role in found for role in PROMOTION_MONEY_ROLES)


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


def _describe_headers(columns: list[ColumnMeta]) -> str:
    return "\n".join(f"- {c.name} | header: {c.original_name}" for c in columns[:KIND_SHORT_COLUMNS])


def _sibling_fit(names: list[str], other: list[str]) -> tuple[float, bool] | None:
    """How closely another table's column names match: (score, positional) or None when they do not match.

    Names compare after normalize_lookup. positional is True when the counts are equal and at least
    SIBLING_MIN_SHARE of the columns sit at the same position with the same name.
    """
    mine = [normalize_lookup(name) for name in names]
    theirs = [normalize_lookup(name) for name in other]
    union = set(mine) | set(theirs)
    jaccard = len(set(mine) & set(theirs)) / len(union) if union else 0.0
    same_count = bool(mine) and len(mine) == len(theirs)
    positional = sum(a == b for a, b in zip(mine, theirs)) / len(mine) if same_count else 0.0
    if jaccard >= SIBLING_MIN_SHARE:
        return jaccard, positional >= SIBLING_MIN_SHARE
    if same_count and positional >= SIBLING_MIN_SHARE:
        return positional, True
    return None


def _failed(record: dict[str, Any]) -> bool:
    """A record whose table has no usable annotation: the whole call failed, or the kind call failed and the
    kind came from the grain."""
    return "error" in record or bool(record.get("kind_failed"))


def _usable(record: dict[str, Any]) -> bool:
    """A record that can serve as a sibling: annotated by the model with every role call answered."""
    return "kind" in record and not _failed(record) and not record.get("partial")


def _sibling_record(context: BusinessContext, group: list[TableMeta], sibling: dict[str, Any],
                    positional: bool) -> dict[str, Any]:
    """The record for a failed group, taking the sibling's kind. Roles map by normalized column name; when the
    sibling matches positionally, a column whose name does not match takes the role at its position."""
    table = group[0]
    sibling_roles = sibling.get("roles") or {}
    by_name: dict[str, str] = {}
    for name in sibling["columns"]:
        by_name.setdefault(normalize_lookup(name), sibling_roles.get(name, "other"))
    by_position = [sibling_roles.get(name, "other") for name in sibling["columns"]]
    roles: dict[str, str] = {}
    for index, column in enumerate(table.columns):
        role = by_name.get(normalize_lookup(column.name))
        if role is None and positional:
            role = by_position[index]
        roles[column.name] = role or "other"
    return {
        "biz": context.business_id,
        "table": table.name,
        "columns": [column.name for column in table.columns],
        "kind": sibling["kind"],
        "kind_source": "sibling",
        "source": f"sibling:{sibling['table']}",
        "roles": roles,
        "seconds": 0.0,
    }


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


def _value_counts(context: BusinessContext, records: list[dict[str, Any]]) -> dict[str, Counter]:
    """Count of each distinct value of every VALUE_ROLES column, over all tables with an annotation.

    effective_roles picks one column per role per table. A NULL stored value is read from __raw_json, as
    order_status_values does.
    """
    notes = annotations_by_signature(records)
    found: dict[str, Counter] = {role: Counter() for role in VALUE_ROLES}
    # effective_roles reads evidence through the "source" schema, the same layout layer.build uses.
    con = sqlite3.connect(":memory:", uri=True)
    con.execute("ATTACH DATABASE ? AS source", (f"file:{context.database}?mode=ro",))
    con.execute("CREATE TEMP TABLE catalog_ids (role TEXT, id TEXT, name TEXT)")
    for table in context.tables:
        note = notes.get(tuple(c.name for c in table.columns))
        if note is not None and "kind" in note:
            _load_catalog_keys(con, table, note["kind"], effective_roles(table, note))
    try:
        for table in context.tables:
            note = notes.get(tuple(c.name for c in table.columns))
            if note is None:
                continue
            roles = effective_roles(table, note, con)
            names = [c.name for c in table.columns]
            for role in VALUE_ROLES:
                column = roles.get(role)
                if not column:
                    continue
                position = names.index(column)
                rows = con.execute(
                    "SELECT value, COUNT(*) FROM (SELECT COALESCE("
                    f"NULLIF(TRIM(CAST({q(column)} AS TEXT)),''), "
                    f"NULLIF(TRIM(CAST(json_extract({q('__raw_json')}, '$[{position}]') AS TEXT)),'')) AS value "
                    f"FROM source.{q(table.name)}) WHERE value IS NOT NULL GROUP BY value"
                ).fetchall()
                for value, count in rows:
                    found[role][value] += count
    finally:
        con.close()
    return found


def _changed_only(maps: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    changed = {
        role: {raw: canonical for raw, canonical in mapping.items() if raw != canonical}
        for role, mapping in maps.items()
    }
    return {role: entries for role, entries in changed.items() if entries}


def _unit_key(value: str) -> str:
    """Values with the same normalized form are one unit; a value that normalizes to nothing is its own unit."""
    return normalize_lookup(value) or value


def _canonical(members: list[str], counts: dict[str, int]) -> str:
    """The most frequent spelling, then the shortest, then the first in sort order."""
    return min(members, key=lambda value: (-counts.get(value, 0), len(value), value))


def _is_subsequence(short: str, long: str) -> bool:
    remaining = iter(long)
    return all(char in remaining for char in short)


def _compatible(first: str, second: str) -> bool:
    """Two values may share a group when their normalized forms share a word, or one is an abbreviation of the
    other (its letters appear in order in the other, at least two of them)."""
    a, b = normalize_lookup(first), normalize_lookup(second)
    if set(a.split()) & set(b.split()):
        return True
    short, long = sorted((a.replace(" ", ""), b.replace(" ", "")), key=len)
    return len(short) >= 2 and _is_subsequence(short, long)


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
        # Model calls that failed and were replaced by a fallback; see take_failures().
        self.failures: list[ModelFailure] = []
        self._failures_lock = threading.Lock()
        self._failure_seq = 0

    def _record_failure(self, stage: str, table: str | None = None, kind: str | None = None) -> None:
        with self._failures_lock:
            self.failures.append(ModelFailure(stage, table, kind))
            self._failure_seq += 1

    def failure_count(self) -> int:
        """Number of model failures recorded so far; unaffected by take_failures()."""
        with self._failures_lock:
            return self._failure_seq

    def take_failures(self) -> list[ModelFailure]:
        """Return the model failures recorded since the last call, and clear them. Each carries its stage, the
        table it belongs to (None for a shop-wide stage) and that table's kind (None when unknown)."""
        with self._failures_lock:
            taken = list(self.failures)
            self.failures.clear()
        return taken

    def annotate(self, context: BusinessContext) -> list[dict[str, Any]]:
        """One record per group of identical column names, in group order.

        A failed group takes the kind and roles of a sibling table (see _sibling_record) when one qualifies; then
        nothing is recorded as a failure. Otherwise its stages are recorded. A stage of an attempt that a retry
        replaced is never recorded.
        """
        groups = group_tables(context)
        if not groups:
            return []
        with ThreadPoolExecutor(self.workers) as pool:
            results = list(pool.map(lambda group: self._annotate_retrying(context, group), groups))
        records = [record for record, _ in results]
        stages = [list(found) for _, found in results]
        usable = [index for index, record in enumerate(records) if _usable(record)]
        for index, group in enumerate(groups):
            if not _failed(records[index]):
                continue
            best: tuple[float, bool, int] | None = None
            for other in usable:
                if other == index:
                    continue
                fit = _sibling_fit([c.name for c in group[0].columns], records[other]["columns"])
                if fit is not None and (best is None or fit[0] > best[0]):
                    best = (fit[0], fit[1], other)
            if best is not None:
                records[index] = _sibling_record(context, group, records[best[2]], best[1])
                stages[index] = []
        for found in stages:
            for failure in found:
                self._record_failure(failure.stage, failure.table_name, failure.kind)
        return records

    def _annotate_retrying(self, context: BusinessContext, group: list[TableMeta]) -> tuple[dict[str, Any], list[ModelFailure]]:
        """One retry for a failed group, so a transient model error does not skip its tables.

        Returns the record and the failures of the attempt that produced it.
        """
        record, found = self._annotate_group(context, group)
        if "error" in record:
            # Only the retry may fall back to kind "other" when the kind call keeps failing.
            record, found = self._annotate_group(context, group, kind_fallback=True)
        return record, found

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
            self._record_failure("status")
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
                self._record_failure("line_kinds")
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

    def group_values(self, role: str, values: list[str], counts: dict[str, int] | None = None) -> dict[str, str]:
        """Canonical spelling for each distinct value of one role. {} when a model call fails.

        payment_method: each value maps to a PAYMENT_CANONICAL key; unknown or invalid answers are dropped.
        Grouped roles: every value maps to the canonical spelling of its group (the most frequent by counts, then
        the shortest). A value the model leaves out stays alone. A group whose members are not compatible is split.
        """
        if role != "payment_method" and role not in GROUPED_ROLES:
            raise ValueError(f"no value grouping for role {role!r}")
        distinct = list(dict.fromkeys(values))
        if not distinct:
            return {}
        counts = counts or {}
        try:
            if role == "payment_method":
                return self._map_payment(distinct)
            units: dict[str, list[str]] = {}
            for value in distinct:
                units.setdefault(_unit_key(value), []).append(value)
            members_of = {_canonical(members, counts): members for members in units.values()}
            reps = list(members_of)
            if len(reps) < 2:
                groups = [[rep] for rep in reps]
            else:
                groups = []
                for start in range(0, len(reps), VALUE_GROUP_BATCH):
                    groups.extend(self._ask_groups(role, reps[start:start + VALUE_GROUP_BATCH]))
            result: dict[str, str] = {}
            for group in groups:
                members = [value for rep in group for value in members_of[rep]]
                canonical = _canonical(members, counts)
                result.update({value: canonical for value in members})
            return result
        except Exception:
            self._record_failure(f"value_groups:{role}")
            return {}

    def _map_payment(self, values: list[str]) -> dict[str, str]:
        system = (
            "Classify each payment method of a shop's orders into exactly one key, or unknown when no key fits. "
            "Reply with JSON only: one object mapping each input value to a key.\n"
            + _role_list({**PAYMENT_CANONICAL, "unknown": "no key fits"})
        )
        result: dict[str, str] = {}
        for start in range(0, len(values), VALUE_GROUP_BATCH):
            batch = values[start:start + VALUE_GROUP_BATCH]
            reply = self.model.generate_json(system, "Values:\n" + json.dumps(batch, ensure_ascii=False),
                                             timeout=self.timeout)
            if not isinstance(reply, dict):
                raise TypeError("payment reply is not a JSON object")
            result.update({
                value: reply[value]
                for value in batch
                if isinstance(reply.get(value), str) and reply[value] in PAYMENT_CANONICAL
            })
        return result

    def _ask_groups(self, role: str, reps: list[str]) -> list[list[str]]:
        """Partition of reps into groups for one call. Raises when the reply is malformed.

        Each rep appears in exactly one group; reps the model left out become singletons; a group with an
        incompatible pair is split into singletons.
        """
        system = (
            f"Group the distinct values of one field ({role}) of a shop's data. Put values in one group only when "
            "they are the same name written differently: spelling, accents, case, spacing, or an abbreviation of "
            "the same name. Different names stay apart, even when they look similar. Each value appears in at most "
            'one group. Reply with JSON only: {"groups": [[value, value, ...], ...]}'
        )
        reply = self.model.generate_json(system, "Values:\n" + json.dumps(reps, ensure_ascii=False),
                                         timeout=self.timeout)
        if not isinstance(reply, dict) or not isinstance(reply.get("groups"), list):
            raise TypeError("grouping reply has no groups list")
        allowed = set(reps)
        placed: set[str] = set()
        groups: list[list[str]] = []
        for raw_group in reply["groups"]:
            if not isinstance(raw_group, list):
                continue
            group: list[str] = []
            for value in raw_group:
                if isinstance(value, str) and value in allowed and value not in placed:
                    placed.add(value)
                    group.append(value)
            if group:
                groups.append(group)
        groups.extend([rep] for rep in reps if rep not in placed)
        checked: list[list[str]] = []
        for group in groups:
            if len(group) > 1 and not all(_compatible(a, b) for a, b in combinations(group, 2)):
                checked.extend([value] for value in group)
            else:
                checked.append(group)
        return checked

    def _group_context(self, context: BusinessContext, records: list[dict[str, Any]]) -> tuple[dict[str, dict[str, str]], bool]:
        """Full value map of every role with at least two distinct values, and whether any role's call failed."""
        counts = _value_counts(context, records)
        maps: dict[str, dict[str, str]] = {}
        failed = False
        for role in VALUE_ROLES:
            counter = counts[role]
            if len(counter) < 2:
                continue
            mapping = self.group_values(role, list(counter), dict(counter))
            if mapping:
                maps[role] = mapping
            else:
                failed = True
        return maps, failed

    def annotate_value_groups(self, context: BusinessContext, records: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
        """{role: {raw value: canonical}} for the entries that change a value, per role that has any."""
        maps, _ = self._group_context(context, records)
        return _changed_only(maps)

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

    def _ask_roles_retrying(self, doc: str, columns: list[ColumnMeta], roles: dict[str, str]) -> dict[str, str]:
        """_ask_roles, tried up to ROLE_CALL_ATTEMPTS times with the same prompt. Raises the last error."""
        last_error: Exception | None = None
        for _ in range(ROLE_CALL_ATTEMPTS):
            try:
                return self._ask_roles(doc, columns, roles)
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _assign_roles(self, doc: str, table: TableMeta, roles: dict[str, str]) -> tuple[dict[str, str], int]:
        """Roles for every column of the table, asked in batches when needed.

        Each call is retried before it counts as failed. Returns (roles, failed_batches); a batch that failed
        leaves its columns out. Raises the last model error when every call failed.
        """
        columns = list(table.columns)
        if len(columns) <= ROLE_SINGLE_CALL_MAX:
            try:
                return self._ask_roles_retrying(doc, columns, roles), 0
            except Exception:
                pass  # asked again below as batches; only a batch that still fails is a failure
        batches = [columns[i:i + ROLE_BATCH_SIZE] for i in range(0, len(columns), ROLE_BATCH_SIZE)] or [[]]
        merged: dict[str, str] = {}
        failed = 0
        last_error: Exception | None = None
        for batch in batches:
            try:
                merged.update(self._ask_roles_retrying(doc, batch, roles))
            except Exception as exc:
                failed += 1
                last_error = exc
        if failed == len(batches) and last_error is not None:
            raise last_error
        return merged, failed

    def _ask_kind(self, group: list[TableMeta], doc: str) -> str | None:
        """The kind the model names. A failed call, or a reply that is not a JSON object, is retried once with a
        shorter prompt (no documentation, no sample values, a capped header list); if that fails too it raises."""
        table = group[0]
        instruction = (
            'Classify a Vietnamese shop data table. Reply with JSON only: {"kind": "<kind>"}.\n'
            "Kinds:\n" + _role_list(KINDS)
        )
        full = f"Documentation:\n{doc}\n\nFile {table.sources[0].path}, table {table.name}:\n{_describe(group)}"
        short = f"File {table.sources[0].path}, table {table.name}:\n{_describe_headers(table.columns)}"
        try:
            reply = self.model.generate_json(instruction, full, timeout=self.timeout)
        except Exception:
            reply = self.model.generate_json(instruction, short, timeout=self.timeout)
        if not isinstance(reply, dict):
            raise TypeError("kind reply is not a JSON object")
        return reply.get("kind")

    def _annotate_group(self, context: BusinessContext, group: list[TableMeta],
                        kind_fallback: bool = False) -> tuple[dict[str, Any], list[ModelFailure]]:
        """One attempt at a group. Returns the record and the failures of this attempt; the caller records
        them only for the attempt it keeps."""
        table = group[0]
        doc = relevant_readme(context.readme, table)
        record: dict[str, Any] = {
            "biz": context.business_id,
            "table": table.name,
            "columns": [column.name for column in table.columns],
        }
        found: list[ModelFailure] = []
        # The kind this table was annotated as, once known: a failure recorded with it is judged by that kind.
        asked_kind: str | None = None
        started = time.time()
        try:
            kind_source = "model"
            try:
                kind = self._ask_kind(group, doc)
            except Exception:
                if not kind_fallback:
                    raise
                found.append(ModelFailure(f"kind:{table.name}", table.name, None))
                record["kind_failed"] = True
                kind = None
            if not isinstance(kind, str) or kind not in KINDS:
                kind = "other"
            if kind == "other" and table.grain in GRAIN_KIND:
                kind = GRAIN_KIND[table.grain]
                kind_source = "grain"
            asked_kind = kind
            # An "other" table is asked the sales roles too, so that promotion can see an amount and a date.
            roles, failed_batches = self._assign_roles(doc, table, SCOPE["sales" if kind == "other" else kind])
            found.extend(ModelFailure(f"roles:{table.name}", table.name, asked_kind) for _ in range(failed_batches))
            # Only a kind the model answered can be promoted; a failed kind call stays "other" as before.
            if kind == "other" and not record.get("kind_failed") and _has_order_roles(roles.values()):
                kind = "sales"
                kind_source = "roles"
                logger.info("table %s promoted from other to sales: roles hold order id, date and amount", table.name)
            if kind == "sales" and GRAIN_KIND.get(table.grain) != "sales" and not any(
                role in SALES_EVIDENCE_ROLES for role in roles.values()
            ):
                kind = "other"
                logger.info("table %s demoted from sales to other: no sales evidence role", table.name)
            if kind == "other":
                # Not a sales table: keep only the roles that the "other" scope also defines.
                roles = {column: (role if role in SCOPE["other"] else "other") for column, role in roles.items()}
            record["kind"] = kind
            record["kind_source"] = kind_source
            record["roles"] = roles
            if failed_batches:
                record["partial"] = True
        except Exception as exc:
            found.append(ModelFailure(f"annotation:{table.name}", table.name, asked_kind))
            record["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        record["seconds"] = round(time.time() - started, 1)
        return record, found


def load_or_annotate(annotator: Annotator, context: BusinessContext, cache_path: Path) -> list[dict[str, Any]]:
    """Return cached records when they match context.revision; otherwise annotate and cache atomically."""
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    if isinstance(cached, dict) and cached.get("revision") == context.revision and isinstance(cached.get("records"), list):
        return cached["records"]

    failures_before = annotator.failure_count()
    records = annotator.annotate(context)
    if any("error" in record for record in records) or annotator.failure_count() != failures_before:
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

    failures_before = annotator.failure_count()
    status_map = annotator.annotate_status(context, records)
    if (not status_map and order_status_values(context, records)) or annotator.failure_count() != failures_before:
        return status_map  # a transient model failure must not persist
    _write_json_atomic(cache_path, {"revision": context.revision, "status_map": status_map})
    return status_map


def load_value_groups(annotator: Annotator, context: BusinessContext, records: list[dict[str, Any]],
                      cache_path: Path) -> dict[str, dict[str, str]]:
    """Return the cached value groups when they match context.revision; otherwise group the values and cache them.

    Any role whose model call failed makes the result a failure: it is returned but not cached.
    """
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    if isinstance(cached, dict) and cached.get("revision") == context.revision and isinstance(cached.get("value_groups"), dict):
        return cached["value_groups"]

    failures_before = annotator.failure_count()
    maps, failed = annotator._group_context(context, records)
    value_groups = _changed_only(maps)
    if failed or annotator.failure_count() != failures_before:
        return value_groups  # a transient model failure must not persist
    _write_json_atomic(cache_path, {"revision": context.revision, "value_groups": value_groups})
    return value_groups
