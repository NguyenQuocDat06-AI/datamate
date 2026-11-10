"""Recognize export summaries/notes without inheriting them into the last order."""

from __future__ import annotations

import re
from typing import Any

from bizreason.data.parsing import ParseStatus, blank, parse_number
from bizreason.data.schema import DATE_ROLES, MONEY_ROLES
from bizreason.utils import normalize_lookup

SUMMARY_LABELS = {"tong", "tong cong", "cong", "total", "grand total", "subtotal"}
SUMMARY_PATTERN = re.compile(
    r"^(?:tong(?: cong)?|cong|grand total|total|subtotal)(?:\s+(?:ngay|thang|quy|nam|nhom|ca)"
    r")?(?:\s+\d[\d\s]*)?$"
)
IDENTITY_ROLES = {"order_id", "purchase_id", "batch_id"}
QUANTITY_ROLES = {"qty_sale", "qty_base", "inventory_qty"}


def is_note(row: list[Any], roles: list[str | None]) -> bool:
    cells = [value for value in row if not blank(value)]
    financial_layout = bool(
        set(roles) & (DATE_ROLES | QUANTITY_ROLES | MONEY_ROLES | IDENTITY_ROLES)
    )
    return bool(
        financial_layout
        and len(cells) == 1
        and isinstance(cells[0], str)
        and re.match(r"^\s*(?:ghi chú|ghi chu|note|notes)\s*:", cells[0], re.IGNORECASE)
    )


def has_record_evidence(row: list[Any], roles: list[str | None]) -> bool:
    if is_note(row, roles):
        return False
    for value, role in zip(row, roles):
        if blank(value) or role not in IDENTITY_ROLES | QUANTITY_ROLES | DATE_ROLES:
            continue
        if SUMMARY_PATTERN.fullmatch(normalize_lookup(value)):
            continue
        return True
    return False


def is_summary(row: list[Any], roles: list[str | None], *, after_last_record: bool) -> bool:
    cells = [(index, value) for index, value in enumerate(row) if not blank(value)]
    labels = {index for index, value in cells if SUMMARY_PATTERN.fullmatch(normalize_lookup(value))}
    if len(cells) < 2 or not labels:
        return False
    if has_record_evidence(row, roles):
        return False
    # A label plus numeric summary cells is stronger evidence than a substring match.
    return all(
        parse_number(value).status in {ParseStatus.OK, ParseStatus.AMBIGUOUS}
        for index, value in cells if index not in labels
    )
