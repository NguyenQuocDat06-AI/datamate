"""Invalid extraction must be corrected under the same schema, not normalized by dataset labels."""
import json

import pytest

from bizreason.semantic import rulebook_params as rp


DOCUMENT = "# Policy\nOnly finalized entries count; pending entries are excluded.\n"
BAD = {"valid_sale_statuses": ["source label"], "service_fee_names": ["handling"]}


class Model:
    def __init__(self, first=None, repaired=None):
        self.first = BAD.copy() if first is None else first
        self.repaired = {"valid_sale_statuses": ["completed"]} if repaired is None else repaired
        self.calls = []

    def generate_json(self, system, prompt, *, timeout):
        self.calls.append(("initial", timeout))
        return self.first.copy()

    def reask_json(self, system, prompt, previous, feedback, *, timeout):
        self.calls.append(("repair", timeout, previous, feedback))
        return self.repaired.copy()


def test_repair_is_bounded_and_keeps_already_valid_fields():
    model = Model(repaired={"valid_sale_statuses": ["completed"], "service_fee_names": ["invented"]})
    diagnostics = {}
    params, taken = rp.extract_rulebook_params(DOCUMENT, model, context_mode="full", validation_retries=1,
                                              diagnostics=diagnostics)
    assert params.valid_sale_statuses == ("completed",)
    assert "handling" in params.service_fee_names and "invented" not in params.service_fee_names
    assert "valid_sale_statuses" in taken
    assert [r[0] for r in model.calls] == ["initial", "repair"]
    assert 0 < model.calls[1][1] <= model.calls[0][1]
    assert "source label" in model.calls[1][3] and "completed" in model.calls[1][3]
    assert diagnostics["model_calls"] == 2 and diagnostics["outcome"] == "validated"


def test_valid_or_unstated_fields_do_not_trigger_repair():
    for first in ({}, {"valid_sale_statuses": ["completed"]}):
        model = Model(first=first)
        rp.extract_rulebook_params(DOCUMENT, model, context_mode="full", validation_retries=1)
        assert [r[0] for r in model.calls] == ["initial"]


@pytest.mark.parametrize("repaired", [{"valid_sale_statuses": ["cancelled"]},
                                      {"valid_sale_statuses": None}, {}])
def test_failed_or_erased_fields_never_create_a_success_cache(tmp_path, repaired):
    doc = tmp_path / "policy.md"; doc.write_text(DOCUMENT)
    cache = tmp_path / "cache.json"
    params, taken = rp.load_or_extract(doc, Model(repaired=repaired), cache, context_mode="full", validation_retries=1)
    assert params.valid_sale_statuses == rp.DEFAULTS.valid_sale_statuses
    assert "valid_sale_statuses" not in taken and not cache.exists()


def test_invalid_explicit_fields_are_not_cached_even_when_retries_are_disabled(tmp_path):
    doc = tmp_path / "policy.md"; doc.write_text(DOCUMENT)
    cache = tmp_path / "cache.json"
    rp.load_or_extract(doc, Model(), cache, context_mode="full")
    assert not cache.exists()


def test_repair_timeout_includes_the_first_call(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(rp.time, "monotonic", lambda: clock[0])
    class Slow(Model):
        def generate_json(self, system, prompt, *, timeout):
            result = super().generate_json(system, prompt, timeout=timeout)
            clock[0] += timeout
            return result
    model = Slow(); diagnostics = {}
    rp.extract_rulebook_params(DOCUMENT, model, context_mode="full", validation_retries=1,
                              timeout=2, diagnostics=diagnostics)
    assert [r[0] for r in model.calls] == ["initial"]
    assert diagnostics["outcome"] == "retry_deadline"


def test_prompt_model_and_retry_identity_invalidate_cache(tmp_path, monkeypatch):
    doc = tmp_path / "policy.md"; doc.write_text(DOCUMENT)
    cache = tmp_path / "cache.json"
    good = {"valid_sale_statuses": ["completed"]}
    first = Model(first=good)
    rp.load_or_extract(doc, first, cache, context_mode="full", model_identity={"revision": "a"})
    cached = Model(first=good)
    rp.load_or_extract(doc, cached, cache, context_mode="full", model_identity={"revision": "a"})
    assert cached.calls == []
    changed_model = Model(first=good)
    rp.load_or_extract(doc, changed_model, cache, context_mode="full", model_identity={"revision": "b"})
    assert len(changed_model.calls) == 1
    monkeypatch.setattr(rp, "SYSTEM_PROMPT", rp.SYSTEM_PROMPT + "\nAdditional schema constraint.")
    changed_prompt = Model(first=good)
    rp.load_or_extract(doc, changed_prompt, cache, context_mode="full", model_identity={"revision": "b"})
    assert len(changed_prompt.calls) == 1
    changed_retry = Model(first=good)
    rp.load_or_extract(doc, changed_retry, cache, context_mode="full", model_identity={"revision": "b"}, validation_retries=1)
    assert len(changed_retry.calls) == 1
    stored = json.loads(cache.read_text())
    assert stored["provenance"]["model_identity"] == {"revision": "b"}
    assert stored["provenance"]["validation_retries"] == 1


def test_retry_exhaustion_does_not_make_more_than_two_calls():
    model = Model(repaired={"valid_sale_statuses": ["still invalid"]})
    rp.extract_rulebook_params(DOCUMENT, model, context_mode="full", validation_retries=1)
    assert len(model.calls) == 2


def test_retry_count_must_be_zero_or_one():
    model = Model()
    for retries in (-1, 2, True):
        with pytest.raises(ValueError, match="retr"):
            rp.extract_rulebook_params(DOCUMENT, model, context_mode="full", validation_retries=retries)
    assert model.calls == []
