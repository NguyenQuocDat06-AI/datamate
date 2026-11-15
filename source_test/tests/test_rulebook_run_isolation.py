"""An earlier run's definitions must not become another run's implicit defaults."""

from dataclasses import replace

from bizreason import run_v2
from bizreason.semantic import rule_terms, rulebook_params
from test_run_v2 import command, fake_client, scripted, setup


def test_new_run_without_rule_extraction_does_not_inherit_previous_status_sets(
    setup, fake_client, scripted, monkeypatch,
):
    saved_params, saved_terms = rulebook_params.active(), rule_terms.active()
    rulebook_params.set_active(replace(rulebook_params.DEFAULTS, valid_sale_statuses=("returned",)))
    # A sentinel ranking rule from a prior run; it must be absent even with extraction off.
    rule_terms.set_active([object()])
    seen = []
    original = run_v2.make_intent_parser
    def capture(*args, **kwargs):
        seen.append((rulebook_params.active(), rule_terms.active()))
        return original(*args, **kwargs)
    monkeypatch.setattr(run_v2, "make_intent_parser", capture)
    try:
        assert run_v2.main(command(setup, "--rulebook-params", "off", "--rule-terms", "off")) == 0
        assert len(seen) == 1
        assert seen[0][0] == rulebook_params.DEFAULTS
        assert seen[0][1] == ()
    finally:
        rulebook_params.set_active(saved_params)
        rule_terms.set_active(saved_terms)
