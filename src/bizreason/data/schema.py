"""Conservative label mapping. Labels suggest roles; README supplies their meaning."""

from __future__ import annotations

import re

from bizreason.data.vocabulary import (
    ALIASES,
    CURRENCY_PARENTHESIS_RE,
    HEADER_SYNONYMS,
    HEADER_SYNONYMS_EXTRA,
    INDEPENDENT_ROW_HINT,
)
from bizreason.utils import normalize_lookup

ROLE_LOOKUP = {
    normalize_lookup(alias): role
    for role, aliases in ALIASES.items()
    for alias in aliases.split("|")
}
for _synonyms in (HEADER_SYNONYMS, HEADER_SYNONYMS_EXTRA):
    ROLE_LOOKUP.update(
        {
            normalize_lookup(alias): role
            for role, aliases in _synonyms.items()
            for alias in aliases.split("|")
        }
    )
NUMERIC_ROLES = {
    "order_revenue",
    "contract_amount",
    "upfront_amount",
    "qty_sale",
    "qty_base",
    "line_amount",
    "unit_price",
    "cost_sale",
    "total_due",
    "paid_amount",
    "conversion_factor",
    "inventory_qty",
    "qty_returned",
    "expense_amount",
}
DATE_ROLES = {
    "due_at",
    "contract_at",
    "ordered_at",
    "ordered_date",
    "paid_at",
    "purchased_at",
    "expires_at",
    "event_at",
    "price_effective_at",
    "returned_at",
    "expense_at",
}
MONEY_ROLES = {"line_amount", "unit_price", "cost_sale", "total_due", "paid_amount", "expense_amount", "contract_amount", "upfront_amount", "order_revenue"}


def column_descriptions(readme: str, filename: str, sheet: str) -> dict[str, str]:
    """Read scoped column descriptions, including partitioned exports of a base file."""
    descriptions = {}
    applicable = False
    for line in readme.splitlines():
        if line.startswith("## "):
            names = re.findall(r"`([^`]+)`", line)
            applicable = False
            if names:
                wanted = normalize_lookup(names[0].rsplit(".", 1)[0])
                actual = normalize_lookup(filename.rsplit(".", 1)[0])
                applicable = actual == wanted or bool(
                    re.fullmatch(re.escape(wanted) + r" (?:\d+|20\d{2})", actual)
                )
                if len(names) > 1:
                    applicable = applicable and normalize_lookup(names[1]) == normalize_lookup(
                        sheet
                    )
        match = re.match(r"\s*-\s*\*\*(.+?)\*\*\s*:\s*(.+)", line)
        if applicable and match:
            descriptions[normalize_lookup(match.group(1))] = match.group(2)
    return descriptions


def described_role(label: str, description: str, table_label: str) -> str | None:
    text = normalize_lookup(description)
    table = normalize_lookup(table_label)
    if text.startswith("tien hang cua don") and "thanh tien" in text:
        return "order_revenue"  # Documented item subtotal, not payment or total including shipping.
    for prefix, role in [
        ("ma don", "order_id"),
        ("ma phieu nhap", "purchase_id"),
        ("ma hang", "product_id"),
        ("ma khach", "customer_id"),
        ("ten khach", "customer_name"),
        ("ten hang", "product_name"),
        ("trang thai don", "status"),
        ("ngay ban", "ordered_at"),
        ("ngay dat", "ordered_at"),
        ("ngay sua", "event_at"),
        ("tong tien ca phieu", "line_amount"),
        ("thanh tien", "line_amount"),
        ("so luong da doi ra don vi co ban", "qty_base"),
        ("thoi diem thu", "paid_at"),
        ("so khach phai tra", "total_due"),
        ("tong tien don", "total_due"),
        ("don gia", "unit_price"),
    ]:
        if text.startswith(prefix):
            return role
    if text == "ten" and any(word in table for word in ["san pham", "sachsanpham", "thuoc"]):
        return "product_name"
    if text == "ten" and any(word in table for word in ["khach hang", "khachhang"]):
        return "customer_name"
    if text.startswith("so tien") and any(
        word in table for word in ["thanh toan", "thanhtoan", "phieu thu"]
    ):
        return "paid_amount"
    if text == "so luong" or text.startswith("so luong ghi ") or text.startswith("so luong ban "):
        return "qty_sale"
    return None


def infer_role(label: str, readme: str = "") -> str | None:
    normalized = normalize_lookup(label)
    role = ROLE_LOOKUP.get(normalized)
    if role:
        return role
    # Parenthetical currency labels retain the meaning of the base column.
    base = re.sub(CURRENCY_PARENTHESIS_RE, "", label, flags=re.IGNORECASE)
    if base != label and normalize_lookup(base) in ROLE_LOOKUP:
        return ROLE_LOOKUP[normalize_lookup(base)]
    if normalized == "tong tien":
        for line in readme.splitlines():
            if normalize_lookup(label) in normalize_lookup(
                line
            ) and "thanh tien" in normalize_lookup(line):
                return "line_amount"
    return None


def infer_grain(roles: set[str], table_label: str, readme: str) -> str:
    label = normalize_lookup(table_label)
    if {"contract_id", "contract_at", "contract_amount", "paid_at", "paid_amount"} <= roles:
        return "installment_ledger"
    if "order_id" in roles and roles & {"product_id", "product_name"} and "qty_sale" in roles:
        return "order_line"
    if {"order_id", "paid_amount", "paid_at"} <= roles:
        return "payment_event"
    if {"order_id", "ordered_at"} <= roles:
        return "order_header"
    if "conversion_factor" in roles:
        return "unit_conversion"
    if "purchase_id" in roles and roles & {"product_id", "product_name"}:
        return "purchase_line"
    if "purchase_id" in roles and "purchased_at" in roles:
        return "purchase_header"
    if "inventory_qty" in roles:
        return "inventory_event" if roles & DATE_ROLES else "snapshot"
    if (
        roles & {"product_id", "product_name"}
        and roles & {"line_amount", "ordered_at"}
        and "order_id" not in roles
    ):
        return "independent_ledger_row"
    if roles & {"product_id", "product_name"} and not roles & {
        "qty_sale",
        "order_id",
        "purchase_id",
    }:
        return "product_dimension"
    if "so" in label or INDEPENDENT_ROW_HINT in readme.lower():
        return "independent_ledger_row"
    return "unknown"


def sheet_year(label: str) -> int | None:
    years = set(re.findall(r"(?<!\d)(20\d{2})(?!\d)", label))
    return int(years.pop()) if len(years) == 1 else None
