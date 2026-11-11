"""Derive status from notes under explicit rulebook mandate."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from bizreason.contracts import ColumnMeta, TableMeta
from bizreason.data.eligibility import propagate_order_returns
from bizreason.data.parsing import ParseStatus, blank, parse_status
from bizreason.rules import RuleRegistry
from bizreason.utils import normalize_lookup, quote_identifier

PAYMENT_PHRASE_RE = re.compile(
    r"\b(?:khach\s+)?(?:da\s+|vua\s+)?(?:tra\s+no|tra\s+tien|tra\s+du|tra\s+bot|tra\s+coc|tra\s+truoc|tra\s+gop|tra\s+lai\s+tien|thu\s+no|thu\s+tien|thanh\s+toan|chuyen\s+khoan|tat\s+toan)\b"
)
NEGATED_CANCEL_RE = re.compile(
    r"\b(?:khong|ko|k|chua|khong\s+he|bao\s+khong)\s+(?:bi\s+|don\s+)?(?:huy\s+don|huy|boom(?:\s+hang)?)\b"
)
NEGATED_RETURN_RE = re.compile(
    r"\b(?:khong|ko|k|chua)\s+(?:bi\s+|co\s+)?(?:doi\s+tra|tra\s+hang|hoan\s+tra|mang\s+tra|gui\s+tra|tra)\b"
)
NEGATED_COMPLETE_RE = re.compile(
    r"\b(?:chua|khong|ko)\s+(?:giao\s+hang|giao\s+xong|da\s+giao|giao|nhan\s+hang|nhan|hoan\s+thanh|ship)\b"
)

STATUS_PATTERNS = {
    "cancelled": re.compile(r"\b(?:da\s+huy|bi\s+huy|huy\s+don|khach\s+huy|don\s+huy|boom\s+hang)\b"),
    "returned": re.compile(
        r"\b(?:tra\s+hang|hoan\s+tra|doi\s+tra|khach\s+mang\s+tra|mang\s+tra|gui\s+tra|khach\s+gui\s+tra|tra\s+lai\s+hang|tra\s+sp|tra\s+san\s+pham|tra\s+mon|doi\s+hang|doi\s+sang|hoan\s+don|khach\s+tra(?!\s+(?:no|tien|du|bot|coc|truoc|gop|khoan)))\b"
    ),
    "shipping": re.compile(r"\b(?:dang\s+giao|cho\s+giao|dang\s+ship|dang\s+van\s+chuyen|cho\s+ship)\b"),
    "completed": re.compile(r"\b(?:hoan\s+thanh|da\s+giao|giao\s+xong|da\s+nhan|thanh\s+cong)\b"),
}


def classify_note_status(note_raw: Any) -> tuple[str | None, bool]:
    """Classify note text into an order status. Returns (status, is_error)."""
    if blank(note_raw):
        return "completed", False

    text = str(note_raw).strip()
    norm_note = normalize_lookup(text)
    working_text = norm_note
    matched_states: set[str] = set()

    has_payment = bool(PAYMENT_PHRASE_RE.search(working_text))
    if has_payment:
        working_text = PAYMENT_PHRASE_RE.sub(" ", working_text)

    has_negation = False
    if NEGATED_CANCEL_RE.search(working_text):
        has_negation = True
        working_text = NEGATED_CANCEL_RE.sub(" ", working_text)

    if NEGATED_RETURN_RE.search(working_text):
        has_negation = True
        working_text = NEGATED_RETURN_RE.sub(" ", working_text)

    if NEGATED_COMPLETE_RE.search(working_text):
        has_negation = True
        matched_states.add("shipping")
        working_text = NEGATED_COMPLETE_RE.sub(" ", working_text)

    for state, pattern in STATUS_PATTERNS.items():
        if pattern.search(working_text):
            matched_states.add(state)

    # Without its tone mark, a lone "huy" is usually a person's name; only the marked word states a cancellation.
    lowered = text.lower()
    if ("hủy" in lowered or "huỷ" in lowered) and re.search(r"\bhuy\b", working_text):
        matched_states.add("cancelled")

    if not (has_payment or has_negation):
        parsed = parse_status(text, domain="order")
        if parsed.status == ParseStatus.OK and parsed.value in {
            "completed",
            "cancelled",
            "returned",
            "shipping",
        }:
            matched_states.add(parsed.value)

    if len(matched_states) > 1:
        return None, True
    elif len(matched_states) == 1:
        return next(iter(matched_states)), False
    else:
        return "completed", False


def derive_status_from_notes(
    connection: sqlite3.Connection,
    tables: list[TableMeta],
    rules: RuleRegistry | None = None,
) -> list[TableMeta]:
    if rules is None or not getattr(rules, "text", None):
        return tables

    norm = normalize_lookup(rules.text)
    if "so khong co cot trang thai" not in norm or "ghi chu" not in norm:
        return tables

    q = quote_identifier
    result: list[TableMeta] = []
    for table in tables:
        if table.grain not in {"order_line", "order_header"}:
            result.append(table)
            continue
        if table.role("status") is not None:
            result.append(table)
            continue
        note_col = next(
            (
                c.name
                for c in table.columns
                if c.semantic_role is None
                and normalize_lookup(c.original_name) in {"ghi chu", "note", "notes"}
            ),
            None,
        )
        if not note_col or table.column("derived_status"):
            result.append(table)
            continue

        rows = connection.execute(f"SELECT rowid, {q(note_col)} FROM {q(table.name)}").fetchall()
        connection.execute(f"ALTER TABLE {q(table.name)} ADD COLUMN {q('derived_status')} TEXT")
        updates = []
        examples: list[Any] = []
        errors = 0
        for rowid, note_val in rows:
            val, is_err = classify_note_status(note_val)
            if is_err:
                errors += 1
                val = None
            if val is not None and len(examples) < 3 and val not in examples:
                examples.append(val)
            updates.append((val, rowid))

        if updates:
            connection.executemany(
                f"UPDATE {q(table.name)} SET {q('derived_status')}=? WHERE rowid=?", updates
            )

        column = ColumnMeta(
            name="derived_status",
            original_name="derived_status",
            dtype="TEXT",
            semantic_role="status",
            examples=examples,
            null_count=errors,
            parse_errors=errors,
            evidence=[f"derived_from:{note_col}", "rulebook:note_status_fallback"],
        )
        updated_table = table.model_copy(update={"columns": [*table.columns, column]})
        result.append(updated_table)

    propagate_order_returns(connection, result, "")
    return result
