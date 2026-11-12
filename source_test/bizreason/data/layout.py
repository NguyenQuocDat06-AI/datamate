"""Recognize export summaries/notes without inheriting them into the last order."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from bizreason.data.parsing import ParseStatus, blank, parse_number
from bizreason.data.schema import DATE_ROLES, MONEY_ROLES
from bizreason.data.vocabulary import NOTE_PREFIX_RE
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
        and re.match(NOTE_PREFIX_RE, cells[0], re.IGNORECASE)
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


SUBTOTAL_TOLERANCE = Decimal("0.005")  # 0.5% of the block sum
MIN_SUBTOTAL_BLOCK = 2  # one row repeating its own amount is not a subtotal


def is_key_role(role: str | None) -> bool:
    """Identifier and date columns: a transaction row fills them, a subtotal row leaves them empty."""
    return bool(role) and (role.endswith("_id") or role in DATE_ROLES)


class ArithmeticSubtotals:
    """Finds subtotal rows by arithmetic, whatever their label.

    A row is a subtotal when every key cell (identifier- or date-like) is empty and, on at least one money column,
    its amount equals the sum of the rows since the previous subtotal (or the sheet start), within 0.5%. The block
    needs at least MIN_SUBTOTAL_BLOCK rows and the amount must be non-zero. A row with any filled key cell is never
    a subtotal. Rows that other rules excluded (headers, notes, keyword summaries) are not part of the block; a
    keyword summary resets it.
    """

    def __init__(self, roles: list[str | None], locales: list[str] | None = None) -> None:
        self.keys = [index for index, role in enumerate(roles) if is_key_role(role)]
        self.money = [index for index, role in enumerate(roles) if role in MONEY_ROLES]
        self.locales = locales or []
        self.reset()

    def reset(self) -> None:
        self.sums: dict[int, Decimal] = {index: Decimal(0) for index in self.money}
        self.known: dict[int, bool] = {index: True for index in self.money}
        self.count = 0

    def _amount(self, row: list[Any], index: int) -> Decimal | None:
        if blank(row[index]):
            return Decimal(0)
        locale = self.locales[index] if index < len(self.locales) else "unknown"
        result = parse_number(row[index], locale=locale)
        return result.value if result.status == ParseStatus.OK else None

    def is_subtotal(self, row: list[Any]) -> bool:
        if not self.keys or not self.money or self.count < MIN_SUBTOTAL_BLOCK:
            return False
        if any(not blank(row[index]) for index in self.keys):
            return False
        for index in self.money:
            value = self._amount(row, index)
            total = self.sums[index]
            if value is None or not self.known[index] or value == 0 or total == 0:
                continue
            if abs(value - total) <= SUBTOTAL_TOLERANCE * abs(total):
                return True
        return False

    def add(self, row: list[Any]) -> None:
        self.count += 1
        for index in self.money:
            value = self._amount(row, index)
            if value is None:
                self.known[index] = False
            else:
                self.sums[index] += value
