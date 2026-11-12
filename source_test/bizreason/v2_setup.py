"""Context construction for the V2 semantic pipeline.

Mirrors what the legacy cli._builder/_registry produced for V2, but imports only the modules V2 uses
(rules, temporal, data ingestion). It must not import bizreason.cli or any legacy free-SQL module.
"""

from __future__ import annotations

from pathlib import Path

from bizreason.data.ingestion import BusinessBuilder
from bizreason.rules import RuleRegistry
from bizreason.temporal import LunarCalendar, TemporalResolver


def build_v2_context(data: Path, cache: Path) -> tuple[BusinessBuilder, RuleRegistry, TemporalResolver]:
    """Return (builder, rules, temporal) for a test root `data` with cache root `cache`."""
    rules = RuleRegistry.load(data / "rulebook.md")
    calendar_path = data / "calendar.csv"
    temporal = TemporalResolver(LunarCalendar(calendar_path) if calendar_path.exists() else None)
    builder = BusinessBuilder(
        cache,
        rule_hash=rules.revision,
        inheritance_allowed=rules.inheritance_allowed,
        rules=rules,
    )
    return builder, rules, temporal
