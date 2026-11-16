"""Independent raw-source helpers for public questions 51--100."""

from collections import defaultdict
from decimal import Decimal
import re

from scripts.audit_last20_sources import DATA, answer, rounded
from scripts.review_public_first10 import clean, csv_rows, day, deduplicate, excel, grouped, key, number, quantity
from scripts.review_public_first10 import invalid_status as previous_invalid_status
from scripts.review_public_first10 import plain as plain
from scripts.review_public_next20 import customer_name

__all__ = ["DATA", "answer", "rounded", "clean", "csv_rows", "day", "deduplicate", "excel",
           "grouped", "invalid_status", "key", "number", "plain", "quantity", "sales",
           "total", "order_ids", "customer_counts", "repeat_percentage"]


def invalid_status(value):
    if previous_invalid_status(value):
        return True
    # Return descriptions in handwritten ledgers can put a verb between
    # customer and return: "khách mang trả", "khách đem máy trả lại".
    # This is status/return text, not a payment amount or payment-method field.
    return bool(re.search(r"\b(?:khach )?(?:mang|dem)(?: hang| may)? tra(?: lai)?\b", plain(value)))


def sales(bid, filenames, sheet, header, oid, product, inherited, *, qty=None, extra_bad=()):
    rows, bad = [], set(extra_bad)
    for name in filenames:
        path = DATA / bid / name
        iterator = csv_rows(path) if path.suffix == ".csv" else excel(path, sheet, header)
        for tab, line, row in deduplicate(grouped(iterator, oid, product, inherited)):
            if invalid_status(row.get(inherited[1])) or qty and quantity(row[qty]) < 0:
                bad.add(clean(row[oid]))
            rows.append((tab, line, row))
    return [r for r in rows if clean(r[2][oid]) not in bad]


def in_period(row, field, start, end, year=None):
    return start <= day(row[field], year).isoformat() < end


def total(rows, date_field, amount_field, start, end):
    return sum((number(r[amount_field]) for _, _, r in rows if in_period(r, date_field, start, end)), Decimal(0))


def order_ids(rows, date_field, oid, start, end):
    return {clean(r[oid]) for _, _, r in rows if in_period(r, date_field, start, end)}


def customer_counts(rows, date_field, oid, name_field, start, end):
    by_order, parent = defaultdict(set), {}
    for _, _, r in rows:
        name = customer_name(r.get(name_field))
        if key(name) in {"", "kl", "lẻ", "le", "khách lẻ", "khach le", "khách vãng lai", "khach vang lai", "vang lai", "vãng lai"}:
            continue
        parent.setdefault(name, name)
        by_order[clean(r[oid])].add(name)
    def find(name):
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name
    for names in by_order.values():
        names = sorted(names)
        for name in names[1:]:
            parent[find(name)] = find(names[0])
    counts = defaultdict(set)
    for _, _, r in rows:
        if not in_period(r, date_field, start, end):
            continue
        for name in by_order[clean(r[oid])]:
            counts[find(name)].add(clean(r[oid]))
    return counts


def repeat_percentage(counts, minimum):
    numerator = sum(len(ids) >= minimum for ids in counts.values())
    return answer(rounded(Decimal(numerator) * 100 / len(counts), 2), "percent",
        numerator=numerator, denominator=len(counts), minimum_orders=minimum)
