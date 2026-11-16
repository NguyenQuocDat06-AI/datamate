"""Independent population regressions, including statuses absent in the source rows."""
from copy import deepcopy

import pytest

from bizreason.semantic.definition_checks import explicit_population
from bizreason.semantic.evidence import extract_definitions
from bizreason.semantic.metric_catalog import Catalog
from bizreason.semantic.rulebook_params import CANONICAL_ORDER_STATUSES
from test_metric_catalog import raw, db


@pytest.mark.parametrize('body,expected', [
    ('Sum revenue of completed orders.', {'completed'}),
    ('Use only shipping orders.', {'shipping'}),
    ('Use returned orders only.', {'returned'}),
    ('Use completed or shipping orders only.', {'completed', 'shipping'}),
    ('All orders including cancelled orders.', {*CANONICAL_ORDER_STATUSES, None}),
    ('All orders.', {*CANONICAL_ORDER_STATUSES, None}),
    ('Orders whose status is returned.', {'returned'}),
    ('Count cancelled orders divided by all orders.', None),
    ('Revenue of completed orders minus revenue of returned orders.', None),
    ('Not only completed orders.', None),
    ('Completed orders divided by all orders.', None),
    ('Revenue is defined elsewhere.', None),
])
def test_explicit_population_grammar(body, expected):
    actual = explicit_population(body, CANONICAL_ORDER_STATUSES)
    assert actual == (None if expected is None else frozenset(expected))


def registration(body, **changes):
    document = '- **Staff reward**: ' + body + '\n'
    spec = raw(document, **changes)
    return document, spec


@pytest.mark.parametrize('population', ['all', 'valid'])
def test_scope_alone_does_not_mean_completed_only(population):
    # Source has no shipping rows: the plan can return the right number by accident.
    connection = db()
    document, spec = registration('Sum revenue of completed orders only.', population=population,
        dependencies=['revenue'], plan={'source':'orders', 'scope':population,
                                       'measure':{'agg':'sum', 'expr':'revenue'}})
    result = Catalog.from_reply({'metrics':[spec]}, {'rulebook':document}, connection)
    assert not result.metrics
    assert result.errors[0].code == 'definition_population'


@pytest.mark.parametrize('op,value', [('eq','completed'), ('in',['completed'])])
def test_exact_status_filter_satisfies_explicit_clause(op, value):
    connection = db()
    document, spec = registration('Sum revenue of completed orders only.', population='all',
        dependencies=['revenue','status'], plan={'source':'orders', 'scope':'all',
            'filters':[{'column':'status','op':op,'value':value}], 'measure':{'agg':'sum','expr':'revenue'}})
    result = Catalog.from_reply({'metrics':[spec]}, {'rulebook':document}, connection)
    assert not result.errors and result.metrics


def test_aggregate_local_scope_cannot_discard_explicit_population():
    connection = db()
    document, spec = registration('Sum revenue of completed orders only.', population='all',
        dependencies=['revenue','status'], plan={'source':'orders', 'scope':'all',
            'filters':[{'column':'status','op':'eq','value':'completed'}],
            'measure':{'agg':'sum','expr':'revenue','scope':'cancelled'}})
    result = Catalog.from_reply({'metrics':[spec]}, {'rulebook':document}, connection)
    assert result.errors[0].code == 'definition_population'


def test_explicit_all_requires_inclusion_of_missing_and_non_sale_statuses():
    connection = db()
    document, spec = registration('Sum revenue of all orders including cancelled orders.',
        dependencies=['revenue'], plan={'source':'orders','scope':'valid','measure':{'agg':'sum','expr':'revenue'}})
    result = Catalog.from_reply({'metrics':[spec]}, {'rulebook':document}, connection)
    assert result.errors[0].code == 'definition_population'


def test_runtime_status_values_are_not_a_fixed_business_dictionary():
    assert explicit_population('Use only verified records.', ('verified','pending')) == frozenset({'verified'})


def test_compound_ratio_does_not_receive_an_invented_global_population():
    connection = db()
    document, spec = registration('Count cancelled orders divided by all orders.', population='all', unit='ratio',
        dependencies=[], plan={'source':'orders','scope':'all','measure':{'op':'/',
            'left':{'agg':'count','scope':'cancelled'}, 'right':{'agg':'count'}}})
    result = Catalog.from_reply({'metrics':[spec]}, {'rulebook':document}, connection)
    assert not result.errors
