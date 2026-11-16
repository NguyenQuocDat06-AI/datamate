"""Experimental routing, evidence cache identity and refusal paths without network."""
from copy import deepcopy
from datetime import date
import json
import sqlite3
import time

import pytest

from bizreason.contracts import AnswerType, Question
from bizreason.semantic.contract_runtime import ContractDecision, ContractRuntime
from bizreason.semantic.solver import SemanticSolver, set_contract_runtime, set_fallback
from bizreason.temporal import TemporalResolver
from bizreason.utils import PathPolicy
from test_metric_catalog import DOC, contract, db, raw


@pytest.fixture(autouse=True)
def _temporary_output_policy(tmp_path):
    with PathPolicy(mode="competition", allowed_roots=[tmp_path]):
        yield


class Replies:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def generate_json(self, system, prompt, *, timeout):
        self.calls += 1
        assert 0 < timeout <= 120
        if isinstance(self.reply, Exception):
            raise self.reply
        return deepcopy(self.reply)


def shop(tmp_path):
    source = db()
    source.commit()
    destination = sqlite3.connect(tmp_path / "shop.sqlite")
    source.backup(destination)
    source.close()
    destination.execute("CREATE TABLE model_failures (stage TEXT)")
    destination.commit()
    return destination


def runtime(tmp_path, extract=None, query=None, **kwargs):
    return ContractRuntime(extract or Replies({"metrics": [raw()]}), query or Replies(contract()),
                           TemporalResolver(), DOC, tmp_path / "metrics", model_identity="fake-model", **kwargs)


def question(text="Staff reward tháng 8/2024"):
    return Question(id="sample", business_id="synthetic", question=text, as_of_date=date(2024, 8, 31))


def test_catalog_extracted_once_and_persisted_for_same_identity(tmp_path):
    connection = shop(tmp_path)
    extraction, query = Replies({"metrics": [raw()]}), Replies(contract())
    engine = runtime(tmp_path, extraction, query)
    first = engine.answer(question(), connection)
    second = engine.answer(question(), connection)
    assert (first.type, first.answer) == (AnswerType.NUMBER, 70)
    assert second.answer == 70 and extraction.calls == 1 and query.calls == 2
    assert first.trace["evidence"][0]["evidence"][0]["quote"] in DOC
    cached = list((tmp_path / "metrics").glob("*.json"))
    assert len(cached) == 1 and json.loads(cached[0].read_text())["stamp"]["model"] == "fake-model"
    another_extraction = Replies(RuntimeError("should not be called"))
    assert runtime(tmp_path, another_extraction).answer(question(), connection).answer == 70
    assert another_extraction.calls == 0


def test_no_definition_match_makes_no_model_calls(tmp_path):
    connection = shop(tmp_path)
    extraction, query = Replies({}), Replies({})
    assert not runtime(tmp_path, extraction, query).answer(question("unrelated topic"), connection).handled
    assert extraction.calls == query.calls == 0


class SequenceReplies(Replies):
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def generate_json(self, system, prompt, *, timeout):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return deepcopy(reply)


def test_population_repair_uses_validator_feedback_and_preserves_evidence(tmp_path):
    connection = shop(tmp_path)
    document = '- **Staff reward**: Sum revenue multiplied by each employee rate of completed orders only.\n'
    bad = raw(document, population='all')
    bad['plan']['scope'] = 'all'
    good = deepcopy(bad)
    good['dependencies'].append('status')
    good['plan']['filters'] = [{'column':'status', 'op':'eq', 'value':'completed'}]
    extraction = SequenceReplies({'metrics':[bad]}, {'metrics':[good]})
    engine = ContractRuntime(extraction, Replies(contract(population='all')), TemporalResolver(),
                             document, tmp_path/'metrics', model_identity='fake')
    result = engine.answer(question(), connection)
    assert result.answer == 70 and extraction.calls == 2
    assert result.trace['catalog_diagnostics'][0]['issues'][0]['code'] == 'definition_population'
    # Shipping is valid for sales, but explicitly outside this metric's population.
    connection.execute("INSERT INTO orders VALUES ('d','2024-08-04','shipping',1000,'Ada',.1,'2024-08-04')")
    assert engine.answer(question(), connection).answer == 70


def test_population_repair_is_bounded_and_cannot_cache_a_contradiction(tmp_path):
    connection = shop(tmp_path)
    document = '- **Staff reward**: Sum revenue of completed orders only.\n'
    bad = raw(document)
    extraction = SequenceReplies({'metrics':[bad]}, {'metrics':[bad]})
    engine = ContractRuntime(extraction, Replies(contract()), TemporalResolver(), document,
                             tmp_path/'metrics', model_identity='fake')
    result = engine.answer(question(), connection)
    assert result.type is None and extraction.calls == 2
    assert not list((tmp_path/'metrics').glob('*.json'))
    assert engine.answer(question(), connection).type is None and extraction.calls == 2


def test_invalid_json_has_one_bounded_regeneration(tmp_path):
    from bizreason.llm import InvalidJSONError
    connection = shop(tmp_path)
    extraction = SequenceReplies(InvalidJSONError('arbitrary sensitive message'), {'metrics':[raw()]})
    result = runtime(tmp_path, extraction).answer(question(), connection)
    assert result.answer == 70 and extraction.calls == 2
    assert result.trace['catalog_diagnostics'][0]['category'] == 'invalid_json'
    assert 'arbitrary sensitive message' not in json.dumps(result.trace)


def test_provider_failure_is_not_retried_as_a_semantic_repair(tmp_path):
    from bizreason.llm import ProviderError
    connection = shop(tmp_path)
    extraction = SequenceReplies(ProviderError('do not log credentials'))
    result = runtime(tmp_path, extraction).answer(question(), connection)
    assert result.type is None and extraction.calls == 1
    assert result.trace['catalog_diagnostics'][0]['category'] == 'provider_error'
    assert 'credentials' not in json.dumps(result.trace)


@pytest.mark.parametrize('failure', ['missing_percent_flag', 'misplaced_filters'])
def test_closed_schema_feedback_can_repair_one_proposal_without_weakening_validator(tmp_path, failure):
    connection = shop(tmp_path)
    if failure == 'missing_percent_flag':
        bad = raw(unit='percent', dependencies=['revenue'], plan={'source':'orders','scope':'valid',
            'measure':{'op':'/','left':{'agg':'sum','expr':'revenue'},
                       'right':{'agg':'sum','expr':'revenue'}}})
        good = deepcopy(bad)
        good['plan']['as_percent'] = True
        expected = 100
    else:
        bad = raw()
        bad['filters'] = [{'column':'status','op':'eq','value':'completed'}]
        good = deepcopy(bad)
        good['plan']['filters'] = good.pop('filters')
        good['dependencies'].append('status')
        expected = 70
    extraction = SequenceReplies({'metrics':[bad]}, {'metrics':[good]})
    result = runtime(tmp_path, extraction).answer(question(), connection)
    assert result.answer == expected and extraction.calls == 2
    assert result.trace['catalog_diagnostics'][0]['issues'][0]['code'] == 'invalid_metric'


def test_failed_extraction_is_not_cached_or_repeated_in_same_run(tmp_path):
    connection = shop(tmp_path)
    extraction, query = Replies(RuntimeError("private provider body")), Replies(contract())
    engine = runtime(tmp_path, extraction, query)
    for _ in range(2):
        result = engine.answer(question(), connection)
        assert result.handled and result.type is None
        assert "RuntimeError" in result.reason and "private provider body" not in result.reason
    assert extraction.calls == 1 and query.calls == 0
    assert not list((tmp_path / "metrics").glob("*.json"))


def test_model_identity_and_document_change_invalidate_persistent_cache(tmp_path):
    connection = shop(tmp_path)
    runtime(tmp_path).answer(question(), connection)
    for field, value in [("model_identity", "other-model"), ("rulebook", DOC + "\nExtra notes.\n")]:
        extraction = Replies({"metrics": [raw()]})
        engine = runtime(tmp_path, extraction)
        setattr(engine, field, value)
        assert engine.answer(question(), connection).answer == 70
        assert extraction.calls == 1


def test_invalid_catalog_or_question_cannot_silently_use_legacy_formula(tmp_path):
    connection = shop(tmp_path)
    engine = runtime(tmp_path, query=Replies(contract(measure={"agg": "sum", "expr": "revenue"})))
    result = engine.answer(question(), connection)
    assert result.handled and result.type is None and "rejected" in result.reason
    empty = runtime(tmp_path, extract=Replies({"metrics": []}))
    # Clear the valid cache to test a fresh catalog version without modifying the saved artifact.
    empty.model_identity = "empty-catalog-model"
    result = empty.answer(question(), connection)
    assert result.handled and result.type is None and "not compiled" in result.reason


def test_deadline_prevents_model_call(tmp_path):
    connection = shop(tmp_path)
    extraction = Replies({"metrics": [raw()]})
    engine = runtime(tmp_path, extraction, deadline=time.monotonic() - 1)
    with pytest.raises(TimeoutError):
        engine.answer(question(), connection)
    assert extraction.calls == 0


def test_compact_question_reply_inherits_semantic_fields_from_registered_metric(tmp_path):
    connection = shop(tmp_path)
    reply = {"period": "question", "output": "scalar", "filters": [], "group_by": None,
             "limit": None, "order": None}
    result = runtime(tmp_path, query=Replies(reply)).answer(question(), connection)
    assert result.answer == 70 and result.trace["contract"]["metric_id"] == "reward"


def test_layer_model_failure_blocks_contract_before_extraction(tmp_path):
    connection = shop(tmp_path)
    connection.execute("INSERT INTO model_failures VALUES ('annotation')")
    extraction = Replies({"metrics": [raw()]})
    result = runtime(tmp_path, extraction).answer(question(), connection)
    assert result.handled and result.type is None and "layer model failure" in result.reason
    assert extraction.calls == 0


def test_solver_routes_primary_through_contract_and_does_not_fallback_on_rejection(tmp_path):
    connection = shop(tmp_path)
    class ForbiddenParser:
        def parse(self, *args, **kwargs):
            raise AssertionError("legacy parser must not override matched evidence")
    solver = SemanticSolver(ForbiddenParser(), TemporalResolver(), connection)
    try:
        set_contract_runtime(runtime(tmp_path))
        answer, trace = solver.solve(question())
        assert answer.answer == 70 and trace.semantic_contract["metric_id"] == "reward"
        set_contract_runtime(runtime(tmp_path, query=Replies({})))
        set_fallback(object())
        answer, trace = solver.solve(question())
        assert answer.type == AnswerType.ABSTAIN and trace.intent is None
    finally:
        set_contract_runtime(None)
        set_fallback(None)


def test_deadline_gate_never_calls_provider_after_queue_timeout():
    from bizreason.run_v2 import DeadlineGatedModel
    class BusyGate:
        def acquire(self, *, timeout):
            assert timeout > 0
            return False
        def release(self):
            raise AssertionError("an unacquired gate must not be released")
    provider = Replies({})
    with pytest.raises(TimeoutError):
        DeadlineGatedModel(provider, BusyGate(), time.monotonic() + 1).generate_json("", "", timeout=1)
    assert provider.calls == 0


def test_cli_experimental_path_writes_answer_trace_and_closes_clients(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from bizreason import run_v2
    from bizreason.semantic.contract_runtime import EXTRACT_SYSTEM, CONTRACT_SYSTEM
    connection = shop(tmp_path)
    data, out = tmp_path / "input", tmp_path / "output"
    data.mkdir()
    (data / "rulebook.md").write_text(DOC)
    (data / "questions.jsonl").write_text(json.dumps({
        "id": "sample", "business_id": "synthetic", "question": question().question,
        "as_of_date": "2024-08-31"}) + "\n")
    clients = []
    class Client:
        settings = SimpleNamespace(model="fake-model")
        closed = False
        def generate_json(self, system, prompt, *, timeout):
            if system == EXTRACT_SYSTEM:
                return {"metrics": [raw()]}
            if system == CONTRACT_SYSTEM:
                return contract()
            raise AssertionError("unexpected model call")
        def close(self):
            self.closed = True
    def factory(args):
        def make(*unused):
            client = Client()
            clients.append(client)
            return client
        return make
    def answer_only(args, data, out, questions, parser, temporal, gate, deadline, t0, log, record):
        prediction, trace = SemanticSolver(None, temporal, connection).solve(questions[0])
        record(run_v2.Outcome(questions[0].id, trace.decision, 0.0,
                             run_v2.submission_line(prediction), True, time.monotonic(),
                             run_v2._trace_row(questions[0], trace, None, 0.0)))
    monkeypatch.setattr(run_v2, "_client_factory", factory)
    monkeypatch.setattr(run_v2, "build_and_answer", answer_only)
    base = ["--data", str(data), "--out", str(out), "--connection", str(tmp_path / "unused.json")]
    assert run_v2.parse_args(base).semantic_contracts == "off"
    assert run_v2.main(base + ["--semantic-contracts", "on", "--rulebook-params", "off",
                               "--rule-terms", "off", "--traces"]) == 0
    assert json.loads((out / "submission.final.jsonl").read_text())["answer"] == 70
    assert json.loads((out / "traces.jsonl").read_text())["semantic_contract"]["metric_id"] == "reward"
    assert clients and all(client.closed for client in clients)
    from bizreason.semantic import solver
    assert solver._CONTRACT_RUNTIME is None
