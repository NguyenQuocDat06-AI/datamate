"""Identifier repair must not rewrite a valid business program to pass validation."""
from copy import deepcopy
from datetime import date
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from bizreason.semantic import plan as module
from bizreason.semantic.plan import PlanFallback
from bizreason.semantic.plan_repair import references


def database():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE orders (order_id TEXT, status TEXT, revenue REAL)')
    db.execute('CREATE TABLE lines (order_id TEXT, status TEXT, product_name TEXT, amount REAL, x_rate REAL)')
    db.executemany('INSERT INTO orders VALUES (?,?,?)', [('a', 'completed', 35), ('b', 'completed', 0)])
    db.executemany('INSERT INTO lines VALUES (?,?,?,?,?)', [
        ('a', 'completed', 'alpha', 20, .25), ('a', 'completed', 'alpha', 15, .4)])
    return db


def initial_plan():
    return {'source': 'lines', 'period': 'none', 'scope': 'valid',
            'filters': [{'column': 'product_name', 'op': 'eq', 'value': 'alpha'}],
            'measure': {'agg': 'sum', 'expr': {'op': '*', 'left': 'raw amount', 'right': 'x_rate'}},
            'output': 'scalar'}


def repaired_plan():
    out = initial_plan(); out['measure']['expr']['left'] = 'amount'
    return out


class Model:
    def __init__(self, first=None, corrected=None, clock=None, first_seconds=0, repair_seconds=0):
        self.first = first if first is not None else initial_plan()
        self.corrected = corrected if corrected is not None else repaired_plan()
        self.calls = []; self.clock = clock
        self.first_seconds = first_seconds; self.repair_seconds = repair_seconds

    def generate_json(self, system, prompt, *, timeout):
        self.calls.append(('initial', timeout, system, prompt))
        if self.clock is not None:
            self.clock[0] += self.first_seconds
        if isinstance(self.first, Exception):
            raise self.first
        return deepcopy(self.first)

    def reask_json(self, system, prompt, previous, feedback, *, timeout):
        self.calls.append(('repair', timeout, system, prompt, previous, feedback))
        if self.clock is not None:
            self.clock[0] += self.repair_seconds
        return deepcopy(self.corrected)


def test_repairs_only_invalid_reference_with_original_history_and_remaining_budget(monkeypatch):
    clock = [0.0]; monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    model = Model(clock=clock, first_seconds=6, repair_seconds=5)
    fallback = PlanFallback(model, None, timeout=20, binding_retries=1)
    result = fallback.answer('q', database(), date.today())
    assert result.answer == 11
    assert [c[:2] for c in model.calls] == [('initial', 20), ('repair', 14)]
    assert model.calls[0][2:4] == model.calls[1][2:4]
    assert model.calls[1][4] == initial_plan()
    assert 'raw amount' in model.calls[1][5]
    assert result.diagnostics['binding_only'] is True
    assert result.diagnostics['changed_paths'] == [['measure', 'expr', 'left']]


@pytest.mark.parametrize('change', ['scope', 'operator', 'valid_reference', 'filter_value', 'delete_filter', 'source'])
def test_repair_cannot_change_semantic_or_already_valid_fields(change):
    corrected = repaired_plan()
    if change == 'scope': corrected['scope'] = 'all'
    elif change == 'operator': corrected['measure']['expr']['op'] = '+'
    elif change == 'valid_reference': corrected['measure']['expr']['right'] = 'amount'
    elif change == 'filter_value': corrected['filters'][0]['value'] = 'other'
    elif change == 'delete_filter': corrected['filters'] = []
    else: corrected['source'] = 'orders'
    model = Model(corrected=corrected)
    fallback = PlanFallback(model, None, binding_retries=1)
    assert fallback.answer('q', database(), date.today()) is None
    assert 'changed nonbinding fields' in fallback.last_reason
    assert len(model.calls) == 2


def test_retry_is_off_by_default_and_bounded_to_one():
    model = Model(corrected=initial_plan())
    assert PlanFallback(model, None).answer('q', database(), date.today()) is None
    assert len(model.calls) == 1
    model = Model(corrected=initial_plan())
    assert PlanFallback(model, None, binding_retries=1).answer('q', database(), date.today()) is None
    assert len(model.calls) == 2


@pytest.mark.parametrize('failure', ['stored_value', 'join', 'transport', 'no_plan'])
def test_nonbinding_failures_do_not_trigger_reask(failure):
    first = repaired_plan()
    if failure == 'stored_value': first['filters'][0]['value'] = 'not recorded'
    elif failure == 'join': first = {'source': 'orders', 'period': 'none',
                                   'measure': {'agg': 'sum', 'expr': 'lines.amount'}}
    elif failure == 'transport': first = OSError('unavailable')
    else: first = {'source': None}
    model = Model(first=first)
    fallback = PlanFallback(model, None, binding_retries=1)
    assert fallback.answer('q', database(), date.today()) is None
    assert len(model.calls) == 1


@pytest.mark.parametrize('first_seconds,repair_seconds,calls', [(25, 0, 1), (6, 25, 2)])
def test_late_responses_are_not_accepted(monkeypatch, first_seconds, repair_seconds, calls):
    clock = [0.0]; monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    model = Model(clock=clock, first_seconds=first_seconds, repair_seconds=repair_seconds)
    fallback = PlanFallback(model, None, timeout=20, binding_retries=1)
    assert fallback.answer('q', database(), date.today()) is None
    assert 'TimeoutError' in fallback.last_reason
    assert len(model.calls) == calls


def test_reference_paths_distinguish_remote_filters_from_aggregated_values():
    plan = {'source': 'orders', 'filters': [{'column': 'lines.product_name', 'op': 'eq', 'value': 'alpha'}],
            'measure': {'agg': 'sum', 'expr': 'revenue'}}
    paths = references(plan)
    assert paths[('filters', 0, 'column')] == 'filter'
    assert paths[('measure', 'expr')] == 'value'
    assert ('filters', 0, 'value') not in paths


def test_repair_of_bad_measure_keeps_a_valid_many_row_filter():
    first = {'source': 'orders', 'period': 'none',
             'filters': [{'column': 'lines.product_name', 'op': 'eq', 'value': 'alpha'}],
             'measure': {'agg': 'sum', 'expr': 'raw total'}}
    corrected = deepcopy(first); corrected['measure']['expr'] = 'revenue'
    fallback = PlanFallback(Model(first, corrected), None, binding_retries=1)
    result = fallback.answer('q', database(), date.today())
    assert result.answer == 35
    assert result.diagnostics['invalid_paths'] == [['measure', 'expr']]


def test_thread_local_reason_and_diagnostics_do_not_leak_between_questions():
    barrier = threading.Barrier(2)
    class ByQuestion:
        def generate_json(self, system, prompt, *, timeout):
            return {'source': None} if 'decline' in prompt else repaired_plan()
    fallback = PlanFallback(ByQuestion(), None)
    def answer(question):
        with database() as db:
            result = fallback.answer(question, db, date.today())
        barrier.wait()
        return result is not None, fallback.last_reason, fallback.last_diagnostics['initial_plan']
    with ThreadPoolExecutor(max_workers=2) as pool:
        ok, declined = list(pool.map(answer, ['answer', 'decline']))
    assert ok[0] and ok[1] == 'answered by plan' and ok[2] == repaired_plan()
    assert not declined[0] and declined[1] == 'no plan' and declined[2] == {'source': None}


@pytest.mark.parametrize('value', [-1, 2, True, 1.0])
def test_invalid_retry_budget_rejected(value):
    with pytest.raises(ValueError): PlanFallback(Model(), None, binding_retries=value)
