"""Derive base quantities only from supplied per-product unit conversions."""

from __future__ import annotations

import sqlite3
import json
from decimal import Decimal, InvalidOperation

from bizreason.contracts import ColumnMeta, Diagnostic, TableMeta
from bizreason.rules import RuleRegistry
from bizreason.data.parsing import ParseStatus, parse_number
from bizreason.data.unit_graph import UnitGraph
from bizreason.utils import normalize_lookup, quote_identifier

SUPPORTED_GRAINS = {"order_line", "purchase_line", "inventory_event", "snapshot"}


def derive_base_quantities(
    connection: sqlite3.Connection, tables: list[TableMeta], rules: RuleRegistry
) -> tuple[list[TableMeta], list[Diagnostic]]:
    text = normalize_lookup(rules.text)
    if not all(term in text for term in ["don vi co ban", "he so 1", "quy doi"]):
        return tables, []
    conversions = [table for table in tables if table.grain == "unit_conversion"]
    if not conversions:
        return tables, []  # An unread/missing conversion file is not evidence of factor one.
    q = quote_identifier
    updated = []
    diagnostics = []
    for table in tables:
        qty_col = table.role("qty_sale") or table.role("inventory_qty")
        if (
            table.grain not in SUPPORTED_GRAINS
            or table.role("qty_base")
            or not qty_col
            or not table.role("unit")
        ):
            updated.append(table)
            continue
        identity = next(
            (
                role
                for role in ["product_id", "product_name"]
                if table.role(role) and any(other.role(role) for other in conversions)
            ),
            None,
        )
        compatible = [
            other
            for other in conversions
            if identity
            and other.role(identity)
            and other.role("conversion_unit")
            and other.role("conversion_factor")
        ]
        if not compatible:
            updated.append(table)
            continue
        factors = UnitGraph()
        conflicting = False
        for conversion in compatible:
            if any(
                column.parse_errors
                for column in conversion.columns
                if column.semantic_role in {identity, "conversion_unit", "conversion_factor"}
            ):
                conflicting = True
                break
            if not conversion.role("base_unit"):
                conflicting = True
                break
            fields = [
                q(conversion.role(role))
                for role in [identity, "conversion_unit", "conversion_factor", "base_unit"]
            ]
            factor_index = next(i for i, c in enumerate(conversion.columns) if c.semantic_role == "conversion_factor")
            factor_locale = next((e.split(":")[1] for e in conversion.columns[factor_index].evidence if e.startswith("numeric_locale:")), "unknown")
            for product, unit, factor, base_unit, raw_json in connection.execute(
                f"SELECT {','.join(fields)},__raw_json FROM {q(conversion.name)}"
            ):
                if product is None or unit is None or factor is None:
                    conflicting = True
                    break
                try:
                    value = Decimal(str(factor))
                    raw = json.loads(raw_json)[factor_index]
                    parsed = parse_number(raw, locale=factor_locale)
                    if parsed.status != ParseStatus.OK:
                        raise ValueError("Unresolved raw conversion")
                    factors.add(str(product), str(unit), value, str(base_unit or ""), parsed.unit)
                except (InvalidOperation, ValueError, IndexError):
                    conflicting = True
                    break
        if conflicting:
            diagnostics.append(
                Diagnostic(
                    code="UNRESOLVED_CONVERSION",
                    message=f"Conflicting/incomplete conversion data for {table.name}",
                    severity="error",
                )
            )
            updated.append(table)
            continue
        name = "derived_qty_base"
        if table.column(name):
            updated.append(table)
            continue
        connection.execute(f"ALTER TABLE {q(table.name)} ADD COLUMN {q(name)}")
        updates, examples = [], []
        errors = 0
        fields = [q(table.role(identity)), q(table.role("unit")), q(qty_col)]
        for rowid, product, unit, quantity in connection.execute(
            f"SELECT rowid,{','.join(fields)} FROM {q(table.name)}"
        ):
            if quantity is None or product is None or unit is None:
                value = None
                errors += 1
            else:
                try:
                    decimal = Decimal(str(quantity)) * factors.factor(str(product), str(unit))
                    value = (
                        int(decimal)
                        if decimal == decimal.to_integral_value() and -(2**63) <= decimal < 2**63
                        else str(decimal)
                    )
                except (InvalidOperation, ValueError):
                    value = None
                    errors += 1
            if value is not None and len(examples) < 3 and value not in examples:
                examples.append(value)
            updates.append((value, rowid))
            if len(updates) == 2000:
                connection.executemany(
                    f"UPDATE {q(table.name)} SET {q(name)}=? WHERE rowid=?", updates
                )
                updates.clear()
        if updates:
            connection.executemany(f"UPDATE {q(table.name)} SET {q(name)}=? WHERE rowid=?", updates)
        column = ColumnMeta(
            name=name,
            original_name="qty_base [derived from provided conversion]",
            dtype="DECIMAL",
            semantic_role="qty_base",
            examples=examples,
            null_count=errors,
            parse_errors=errors,
            evidence=[
                "rulebook:unit_conversion_default_one",
                *(other.name for other in compatible),
            ],
        )
        updated.append(table.model_copy(update={"columns": [*table.columns, column]}))
    return updated, diagnostics
