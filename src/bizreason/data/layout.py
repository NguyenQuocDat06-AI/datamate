"""Recognize export summaries/notes without inheriting them into the last order."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from bizreason.data.parsing import ParseStatus, blank, parse_date, parse_number
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


DENSE_SHARE = Decimal("0.9")  # a column filled in this share of a block's rows is one every record fills
ABSOLUTE_TOLERANCE = Decimal("0.01")  # sums written with two decimals


def _within(value: Decimal, total: Decimal) -> bool:
    return abs(value - total) <= max(SUBTOTAL_TOLERANCE * abs(total), ABSOLUTE_TOLERANCE)


class BlockSums:
    """Finds subtotal and running-total rows by shape and arithmetic alone, wherever their label and sums sit.

    Detection does not require roles, so it works when the label lands in an identifier or amount column, when the
    sums sit on quantity columns only, or when the label is missing. Known roles only protect numeric record keys.
    A row is a subtotal when all of these hold:

    - the block (rows since the previous subtotal, or the region start) has at least MIN_SUBTOTAL_BLOCK rows;
    - the row holds no date and at most one text cell (the label); every other filled cell is a number;
    - every column that the block fills in DENSE_SHARE of its rows and that is not numeric (identifiers, names,
      dates, units) is blank in the row, except the label cell; at least one such column must actually be blank;
    - every number of the row in a column whose block cells all read as numbers equals that column's block sum
      (or, for a running total, its sum since the region start) within 0.5%, and at least one of them is non-zero.

    A row filling its only identity column is ambiguous even when its amount matches: it can be a real product
    row. The role-free path keeps it. A role-based shape gate can use independent known key evidence instead.
    locales gives the separator reading of each column (see infer_locale).
    """

    def __init__(
        self,
        width: int,
        locales: list[str] | None = None,
        *,
        roles: list[str | None] | None = None,
    ) -> None:
        self.width = width
        self.keys = {index for index, role in enumerate(roles or []) if is_key_role(role)}
        self.locales = list(locales or []) + ["unknown"] * max(0, width - len(locales or []))
        self.total: dict[int, Decimal] = {index: Decimal(0) for index in range(width)}
        self.total_known: dict[int, bool] = {index: True for index in range(width)}
        self.reset()

    def reset(self) -> None:
        self.count = 0
        self.sums: dict[int, Decimal] = {index: Decimal(0) for index in range(self.width)}
        self.known: dict[int, bool] = {index: True for index in range(self.width)}
        self.numbers: dict[int, int] = {index: 0 for index in range(self.width)}
        self.filled: dict[int, int] = {index: 0 for index in range(self.width)}

    def _number(self, value: Any, index: int) -> Decimal | None:
        """The cell as a number, or None when it is not one (text, a date, an unreadable spelling)."""
        if isinstance(value, (date, datetime, time)):
            return None
        result = parse_number(value, locale=self.locales[index])
        return result.value if result.status == ParseStatus.OK else None

    def add(self, row: list[Any]) -> None:
        self.count += 1
        for index in range(self.width):
            value = row[index] if index < len(row) else None
            if blank(value):
                continue
            self.filled[index] += 1
            number = self._number(value, index)
            if number is None:
                self.known[index] = False
                self.total_known[index] = False
            else:
                self.numbers[index] += 1
                self.sums[index] += number
                self.total[index] += number

    def _numeric(self, index: int) -> bool:
        return self.known[index] and self.numbers[index] > 0

    def _total_shape(self, row: list[Any], *, require_dense: bool = True) -> dict[int, Decimal] | None:
        """The numbers of a row shaped like a total (see the class docstring), or None when it is not."""
        if self.count < MIN_SUBTOTAL_BLOCK:
            return None
        labels: list[int] = []
        numbers: dict[int, Decimal] = {}
        for index in range(self.width):
            value = row[index] if index < len(row) else None
            if blank(value):
                continue
            if isinstance(value, (date, datetime, time)):
                return None
            number = self._number(value, index)
            if number is not None:
                # A numeric identifier is still a record key, even when it happens
                # to equal the preceding identifiers' sum (e.g. 1 + 2 = 3).
                if index in self.keys:
                    return None
                numbers[index] = number
            else:
                labels.append(index)
                if len(labels) > 1:
                    return None
        if not numbers:
            return None
        # Most records have several text cells. Check dates only after rejecting those,
        # rather than trying every date format on each product, identifier and status.
        if labels and parse_date(row[labels[0]]).status == ParseStatus.OK:
            return None
        if not require_dense:
            return numbers
        dense = [index for index in range(self.width)
                 if not self._numeric(index) and self.filled[index] >= DENSE_SHARE * self.count]
        if not dense or not any(index >= len(row) or blank(row[index]) for index in dense):
            return None
        if any(index in numbers or (index not in labels and not blank(row[index]))
               for index in dense if index < len(row)):
            return None
        return numbers

    def looks_like_total(self, row: list[Any]) -> bool:
        """Shape gate for the role-based detector, which already checks the key cells.

        Sparse continuation rows can leave every key below DENSE_SHARE, so density is
        required only for the role-free detector. Two text cells (e.g. product and unit)
        still distinguish a continuation line from a labelled subtotal here.
        """
        return self._total_shape(row, require_dense=False) is not None

    def is_subtotal(self, row: list[Any]) -> bool:
        numbers = self._total_shape(row)
        if numbers is None:
            return False
        return self._matches(numbers, self.sums, self.known) or self._matches(numbers, self.total, self.total_known)

    def ambiguous_single_amount(self, row: list[Any]) -> bool:
        """One numerical coincidence cannot identify a subtotal without a summary label.

        A continuation record can omit all keys and carry one arbitrary text cell.
        Keep the arithmetic detectors as candidate generators, but preserve such a
        row unless a recognized summary label supplies independent evidence.
        """
        numeric = 0
        for index, value in enumerate(row[:self.width]):
            if blank(value):
                continue
            if SUMMARY_PATTERN.fullmatch(normalize_lookup(value)):
                return False
            if self._numeric(index) and self._number(value, index) is not None:
                numeric += 1
        return numeric < 2

    def evidence(self, row: list[Any]) -> dict[str, Any]:
        """Numerical observations for an audit; this is not proof of row semantics."""
        comparisons = []
        for index, value in enumerate(row[:self.width]):
            number = self._number(value, index)
            if not blank(value) and number is not None and self._numeric(index):
                comparisons.append({"column_index": index, "observed": str(number),
                                    "block_sum": str(self.sums[index]),
                                    "running_sum": str(self.total[index])})
        return {"block_rows": self.count, "comparisons": comparisons,
                "relative_tolerance": str(SUBTOTAL_TOLERANCE)}

    def _matches(self, numbers: dict[int, Decimal], sums: dict[int, Decimal], known: dict[int, bool]) -> bool:
        nonzero = False
        for index, value in numbers.items():
            if not (self._numeric(index) and known[index]):
                continue  # a number under a text column says nothing either way
            if not _within(value, sums[index]):
                return False
            nonzero = nonzero or value != 0
        return nonzero
