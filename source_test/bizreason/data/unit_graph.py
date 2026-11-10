"""Resolve documented conversion chains without dropping the intermediate unit."""

from __future__ import annotations

from decimal import Decimal

from bizreason.utils import normalize_lookup


class UnitGraph:
    def __init__(self):
        self.edges: dict[tuple[str, str], tuple[str, Decimal]] = {}
        self.bases: dict[str, str] = {}

    def add(self, product: str, unit: str, factor: Decimal, base: str, target: str | None = None):
        unit, base = normalize_lookup(unit), normalize_lookup(base)
        target = normalize_lookup(target) if target else base
        if factor <= 0 or not unit or not base:
            raise ValueError("Invalid documented conversion")
        if product in self.bases and self.bases[product] != base:
            raise ValueError("Conflicting base units")
        self.bases[product] = base
        edge = target, factor
        if (product, unit) in self.edges and self.edges[product, unit] != edge:
            raise ValueError("Conflicting conversion edges")
        self.edges[product, unit] = edge

    def factor(self, product: str, unit: str) -> Decimal:
        unit = normalize_lookup(unit)
        if product not in self.bases:
            return Decimal(1)  # Released rule: no large-unit conversion means base unit.
        base = self.bases[product]
        visited = set()
        value = Decimal(1)
        while unit != base:
            if unit in visited or (product, unit) not in self.edges:
                raise ValueError("Cyclic or incomplete conversion chain")
            visited.add(unit)
            unit, multiplier = self.edges[product, unit]
            value *= multiplier
        return value
