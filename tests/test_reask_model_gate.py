"""Repairs share the same semaphore and absolute deadline as initial requests."""
from types import SimpleNamespace

import pytest

from bizreason import run_v2


@pytest.mark.parametrize('wrapper', ['ordinary', 'absolute'])
@pytest.mark.parametrize('method', ['generate_json', 'reask_json'])
def test_gate_wait_is_subtracted_from_model_budget(monkeypatch, wrapper, method):
    clock = [0.0]; calls = []
    monkeypatch.setattr(run_v2.time, 'monotonic', lambda: clock[0])
    class Gate:
        released = 0
        def acquire(self, timeout): clock[0] += 4; return True
        def release(self): self.released += 1
    def request(*args, timeout): calls.append(timeout); return {}
    client = SimpleNamespace(generate_json=request, reask_json=request)
    gate = Gate()
    model = (run_v2.GatedModel(client, gate) if wrapper == 'ordinary'
             else run_v2.DeadlineGatedModel(client, gate, 5))
    args = ('system', 'prompt') if method == 'generate_json' else ('system', 'prompt', {}, 'feedback')
    getattr(model, method)(*args, timeout=10)
    assert calls == [6 if wrapper == 'ordinary' else 1]
    assert gate.released == 1


def test_reask_does_not_bypass_busy_gate():
    class Gate:
        def acquire(self, timeout): return False
        def release(self): raise AssertionError('unacquired slot released')
    def forbidden(*args, **kwargs): raise AssertionError('provider called without a slot')
    model = run_v2.GatedModel(SimpleNamespace(reask_json=forbidden), Gate())
    with pytest.raises(TimeoutError): model.reask_json('', '', {}, 'feedback', timeout=1)


def test_plan_flags_are_explicit_and_default_off():
    args = ['--data', '/tmp/data', '--out', '/tmp/output', '--connection', '/tmp/model.json']
    defaults = run_v2.parse_args(args)
    assert (defaults.plan_fallback, defaults.plan_binding_retries, defaults.plan_definitions) == ('off', 0, 'eligible')
    explicit = run_v2.parse_args(args + ['--plan-binding-retries', '1', '--plan-definitions', 'all'])
    assert (explicit.plan_binding_retries, explicit.plan_definitions) == (1, 'all')
