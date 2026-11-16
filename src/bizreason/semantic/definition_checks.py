"""Check explicit document population clauses against compiled status predicates.

This is a deliberately narrow semantic check, not a natural-language proof system.
It only recognizes unambiguous clauses naming runtime status values. It never
rewrites a metric or invents a status mapping from its business name.
"""
from __future__ import annotations

import re
from typing import Any

from bizreason.semantic import rulebook_params, vocabulary as v
from bizreason.utils import normalize_lookup


class DefinitionPopulationError(ValueError):
    """A plan contradicts a population clause recognized directly in its evidence."""


def explicit_population(body: str, statuses: tuple[str, ...]) -> frozenset[str | None] | None:
    """Return a required status set, or None when outside the small recognized grammar."""
    names = {normalize_lookup(value): value for value in statuses if value}
    if not names:
        return None
    # Preserve sentence boundaries so a qualifier cannot bind across sentences.
    text = body.casefold()
    names_re = "(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + ")"
    values_re = names_re + "(?:" + v.POPULATION_CONNECTOR_RE + names_re + ")*"
    entities = v.POPULATION_ENTITY_RE
    positive: set[frozenset[str]] = set()
    for pattern in v.POPULATION_POSITIVE_RE:
        for match in re.finditer(pattern.format(values=values_re, entities=entities), text):
            if re.search(r"\b(?:not|never)\s+$", text[:match.start()]):
                continue
            values = frozenset(names[n] for n in re.split(v.POPULATION_CONNECTOR_RE, match['values']))
            positive.add(values)
    all_rows = re.search(v.POPULATION_ALL_RE.format(entities=entities), text)
    if len(positive) == 1 and not all_rows:
        return next(iter(positive))
    if positive:
        return None  # multiple populations or a ratio of subsets: do not guess attachment
    if all_rows:
        # "All ... including X ..." preserves all records. Other mentions of a
        # status may define a numerator/subset; those need a richer representation.
        remainder = re.sub(v.POPULATION_INCLUDE_RE.format(values=values_re, entities=entities), "", text)
        if not re.search(r"\b" + names_re + r"\b", remainder):
            return frozenset((*statuses, None))
    return None


def _status_filters(items: list[dict], source: str) -> list[dict]:
    result = []
    for item in items:
        column = item.get('column')
        if column in ('status', source + '.status'):
            if item.get('op') not in ('eq', 'ne', 'in', 'not_in', 'is_empty', 'not_empty') or 'column2' in item:
                raise DefinitionPopulationError('cannot verify metric status predicate')
            result.append(item)
        elif 'expr' in item and 'status' in str(item['expr']):
            raise DefinitionPopulationError('cannot verify metric status expression')
    return result


def validate_population(spec: Any, definitions: list[Any], compiler: Any) -> None:
    """Reject a plan that contradicts an explicit, recognized population clause.

    Compare predicates on a finite status domain including missing values and
    canonical states absent from the current rows. Evaluate compiler-generated SQL,
    rather than assuming 'valid' and 'completed only' are interchangeable.
    """
    if spec.source not in ('orders', 'lines') or 'status' not in compiler.cols(spec.source):
        return
    observed = compiler.db.execute('SELECT DISTINCT status FROM "' + spec.source + '" LIMIT 130').fetchall()
    if len(observed) > 128:
        return
    statuses = tuple(sorted(set(rulebook_params.CANONICAL_ORDER_STATUSES) |
                            {row[0] for row in observed if isinstance(row[0], str) and row[0]}))
    required = [explicit_population(item.body, statuses) for item in definitions]
    required = [value for value in required if value is not None]
    if not required:
        return
    if any(value != required[0] for value in required):
        raise DefinitionPopulationError('conflicting explicit population clauses')
    expected = required[0]
    base = _status_filters(spec.plan.get('filters', []), spec.source)
    global_status = compiler._status(spec.population)
    domain = (*statuses, None)

    def check(aggregate: dict) -> None:
        filters = compiler._filters([*base, *_status_filters(aggregate.get('filters', []), spec.source)])
        conditions = [filters.text]
        if global_status:
            conditions.append(global_status)
        if 'scope' in aggregate:
            status = compiler._status(aggregate['scope'])
            if status:
                conditions.append(status)
        predicate = ' AND '.join('(' + part + ')' for part in conditions)
        actual = frozenset(value for value in domain if compiler.db.execute(
            'SELECT COALESCE((' + predicate + '),0) FROM (SELECT ? AS status) t',
            [*filters.args, value]).fetchone()[0])
        if actual != expected:
            wanted = sorted(str(value) for value in expected)
            raise DefinitionPopulationError('metric population contradicts explicit definition; required statuses=' + str(wanted))

    def walk(measure: Any) -> None:
        if not isinstance(measure, dict):
            return
        if 'agg' in measure:
            check(measure)
        else:
            walk(measure.get('left'))
            walk(measure.get('right'))
    walk(spec.plan['measure'])
    having = (spec.plan.get('per') or {}).get('having')
    if having and 'agg' in having:
        check(having)
