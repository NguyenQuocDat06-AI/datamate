"""A full-document control must isolate retrieval from schema validation and caches."""
import json

import pytest

from bizreason.semantic import rulebook_params as rp


class Model:
    def __init__(self, reply=None, error=None):
        self.reply = {} if reply is None else reply
        self.error = error
        self.prompts = []

    def generate_json(self, system, prompt, *, timeout):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.reply


UNFAMILIAR_RULE = "# Processing policy\nOnly finalized records qualify; queued entries are excluded.\n"


def test_full_context_recovers_a_definition_with_no_lexical_cue():
    assert rp.relevant_rulebook_text(UNFAMILIAR_RULE) == ""
    model = Model({"valid_sale_statuses": ["completed"]})
    params, taken = rp.extract_rulebook_params(UNFAMILIAR_RULE, model, context_mode="full")
    assert len(model.prompts) == 1 and UNFAMILIAR_RULE.strip() in model.prompts[0]
    assert params.valid_sale_statuses == ("completed",)
    assert taken == ["valid_sale_statuses"]


def test_full_context_keeps_uncued_evidence_when_another_section_has_cues():
    text = UNFAMILIAR_RULE + "\nService fee: service charge is optional.\n"
    assert "finalized" not in rp.relevant_rulebook_text(text)
    model = Model()
    rp.extract_rulebook_params(text, model, context_mode="full")
    assert text.strip() in model.prompts[0]


def test_unspecified_full_document_fields_remain_defaults():
    model = Model()
    params, taken = rp.extract_rulebook_params("# Field notes\nNo entries available.", model, context_mode="full")
    assert len(model.prompts) == 1
    assert params == rp.DEFAULTS and taken == []


def test_full_context_does_not_bypass_status_validation():
    model = Model({"valid_sale_statuses": ["cancelled"]})
    params, taken = rp.extract_rulebook_params(UNFAMILIAR_RULE, model, context_mode="full")
    assert params == rp.DEFAULTS and taken == []


def test_empty_full_context_does_not_call_model():
    model = Model(error=AssertionError("empty input must not request extraction"))
    assert rp.extract_rulebook_params(" \n", model, context_mode="full") == (rp.DEFAULTS, [])
    assert model.prompts == []


def test_cache_cannot_cross_context_profiles(tmp_path):
    document = tmp_path / "policy.md"
    cache = tmp_path / "cache.json"
    document.write_text(UNFAMILIAR_RULE)
    legacy = Model()
    rp.load_or_extract(document, legacy, cache)
    assert legacy.prompts == []
    # Simulate the old on-disk format, which has no context mode.
    data = json.loads(cache.read_text())
    data.pop("context_mode", None)
    cache.write_text(json.dumps(data))
    full = Model({"valid_sale_statuses": ["completed"]})
    params, _ = rp.load_or_extract(document, full, cache, context_mode="full")
    assert len(full.prompts) == 1 and params.valid_sale_statuses == ("completed",)
    assert json.loads(cache.read_text())["context_mode"] == "full"
    cached = Model(error=AssertionError("same-profile cache should be reused"))
    assert rp.load_or_extract(document, cached, cache, context_mode="full")[0] == params
    assert cached.prompts == []
    # A subsequent lexical run must also reject the full-profile cache.
    assert rp.load_or_extract(document, Model(), cache)[0] == rp.DEFAULTS
    assert json.loads(cache.read_text())["context_mode"] == "cues"


def test_failed_full_extraction_is_not_cached(tmp_path):
    document = tmp_path / "policy.md"
    cache = tmp_path / "cache.json"
    document.write_text(UNFAMILIAR_RULE)
    assert rp.load_or_extract(document, Model(error=TimeoutError()), cache, context_mode="full") == (rp.DEFAULTS, [])
    assert not cache.exists()


def test_unknown_context_profile_is_rejected_before_model_call():
    model = Model()
    with pytest.raises(ValueError, match="context"):
        rp.extract_rulebook_params(UNFAMILIAR_RULE, model, context_mode="unknown")
    assert model.prompts == []
