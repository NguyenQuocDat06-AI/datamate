"""Rulebook gates: a measure that rests on a convention answers only when the current rulebook states it."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import pytest

from bizreason.semantic import rulebook_params as rp
from bizreason.semantic.compiler import OBLIGATION_MEASURES, Unsupported
from test_obligations_ledgers import make_db, value

DATA_ROOT = Path(os.environ.get("DATASET_CACHE_ROOT", "/home/ubuntu/datamate/source_test")) / "data"
PUBLIC_RULEBOOK = DATA_ROOT / "public_test" / "rulebook.md"

TEXT_WITH_RULES = (
    "# Quy tac\n## 4. Công nợ\n| Mục | Nghĩa |\n|---|---|\n"
    "| Trả góp | Đơn trả góp là đơn **ghi nợ**: phần góp chưa trả là tiền khách còn nợ. Kỳ góp có hạn **trước** "
    "ngày hỏi mà chưa góp đủ là **quá hạn**; kỳ có hạn đúng ngày hỏi thì chưa quá hạn. |\n"
)
TEXT_WITHOUT_TOPIC = "# Quy tac\n## 4. Công nợ\n| Mục | Nghĩa |\n|---|---|\n| Đơn COD | không phải khách nợ, ghi nợ là đơn chưa trả |\n"


class FakeModel:
    def __init__(self, reply: Any = None, error: Exception | None = None) -> None:
        self.reply, self.error, self.calls = reply if reply is not None else {}, error, []

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> Any:
        self.calls.append(prompt)
        if self.error is not None:
            raise self.error
        return self.reply


def activate(monkeypatch, **fields):
    monkeypatch.setattr(rp, "_active", dataclasses.replace(rp.DEFAULTS, **fields))


STATED = dict(installment_counts_as_debt=True, overdue_due_day_counts_as_overdue=False)


# ---------------------------------------------------------------- the gate on the compiler

def test_defaults_state_nothing():
    assert rp.DEFAULTS.installment_counts_as_debt is None and rp.DEFAULTS.overdue_due_day_counts_as_overdue is None


def test_stated_conventions_give_numbers(monkeypatch):
    activate(monkeypatch, **STATED)
    db = make_db()
    assert value(db, "overdue_amount", "2026-03-31") == 200
    assert value(db, "overdue_debtor_count", "2026-03-31") == 1
    assert value(db, "installments_due_count", "2026-03-31") == 6
    assert value(db, "installment_outstanding", "2026-03-31") == 2500


@pytest.mark.parametrize("measure", sorted(OBLIGATION_MEASURES))
def test_default_rulebook_abstains_on_every_gated_measure(monkeypatch, measure):
    monkeypatch.setattr(rp, "_active", rp.DEFAULTS)
    with pytest.raises(Unsupported, match="rulebook does not define"):
        value(make_db(), measure, "2026-03-31")


def test_missing_overdue_rule_stops_only_the_overdue_measures(monkeypatch):
    activate(monkeypatch, installment_counts_as_debt=True)
    db = make_db()
    assert value(db, "installment_outstanding", "2026-03-31") == 2500
    assert value(db, "installments_due_count", "2026-03-31") == 6
    for measure in ("overdue_amount", "overdue_debtor_count"):
        with pytest.raises(Unsupported, match="rulebook does not define whether an instalment due on the asked day"):
            value(db, measure, "2026-03-31")


def test_rulebook_without_the_debt_rule_stops_all_four(monkeypatch):
    activate(monkeypatch, overdue_due_day_counts_as_overdue=False)
    for measure in sorted(OBLIGATION_MEASURES):
        with pytest.raises(Unsupported, match="rulebook does not define that an instalment order is a debt order"):
            value(make_db(), measure, "2026-03-31")


def test_rulebook_saying_instalments_are_not_debt_abstains(monkeypatch):
    activate(monkeypatch, installment_counts_as_debt=False, overdue_due_day_counts_as_overdue=False)
    with pytest.raises(Unsupported, match="rulebook does not say"):
        value(make_db(), "installment_outstanding", "2026-03-31")


def test_a_due_day_that_counts_as_overdue_changes_the_result(monkeypatch):
    activate(monkeypatch, installment_counts_as_debt=True, overdue_due_day_counts_as_overdue=False)
    db = make_db()
    before = value(db, "overdue_amount", "2026-03-31"), value(db, "overdue_debtor_count", "2026-03-31")
    activate(monkeypatch, installment_counts_as_debt=True, overdue_due_day_counts_as_overdue=True)
    after = value(db, "overdue_amount", "2026-03-31"), value(db, "overdue_debtor_count", "2026-03-31")
    assert before == (200, 1)
    assert after == (200 + 100, 2)  # the instalment due on 03-31 (order 12) is now overdue too
    # the day an instalment falls due still counts as due, whatever the overdue convention
    assert value(db, "installments_due_count", "2026-03-31") == 6


def test_gate_table_covers_exactly_the_obligation_measures():
    assert set(rp.MEASURE_CONVENTIONS) == OBLIGATION_MEASURES
    for rows in rp.MEASURE_CONVENTIONS.values():
        assert all(hasattr(rp.DEFAULTS, field) for field, _, _ in rows)


# ---------------------------------------------------------------- extraction

def stated_reply(**changes):
    return {"installment_counts_as_debt": True, "overdue_due_day_counts_as_overdue": False, **changes}


def test_extraction_reads_the_conventions_from_a_rulebook_that_states_them():
    model = FakeModel(stated_reply())
    params, taken = rp.extract_rulebook_params(TEXT_WITH_RULES, model)
    assert (params.installment_counts_as_debt, params.overdue_due_day_counts_as_overdue) == (True, False)
    assert {"installment_counts_as_debt", "overdue_due_day_counts_as_overdue"} <= set(taken)
    assert "Trả góp" in model.calls[0]  # the cue words bring the instalment row to the model


def test_extraction_reads_a_different_convention():
    params, _ = rp.extract_rulebook_params(TEXT_WITH_RULES, FakeModel(stated_reply(overdue_due_day_counts_as_overdue=True)))
    assert params.overdue_due_day_counts_as_overdue is True


def test_null_and_missing_fields_are_not_stated():
    params, taken = rp.extract_rulebook_params(
        TEXT_WITH_RULES, FakeModel({"installment_counts_as_debt": None}))
    assert params.installment_counts_as_debt is None and params.overdue_due_day_counts_as_overdue is None
    assert not {"installment_counts_as_debt", "overdue_due_day_counts_as_overdue"} & set(taken)


def test_a_model_claim_without_the_topic_in_the_rulebook_is_dropped():
    params, taken = rp.extract_rulebook_params(TEXT_WITHOUT_TOPIC, FakeModel(stated_reply()))
    assert params.installment_counts_as_debt is None and params.overdue_due_day_counts_as_overdue is None
    assert "installment_counts_as_debt" not in taken


def test_an_unusable_value_is_not_stated_and_not_cached(tmp_path):
    rulebook = tmp_path / "rulebook.md"
    rulebook.write_text(TEXT_WITH_RULES, encoding="utf-8")
    model = FakeModel(stated_reply(installment_counts_as_debt="yes"))
    params, _ = rp.load_or_extract(rulebook, model, tmp_path / "p.json")
    assert params.installment_counts_as_debt is None and params.overdue_due_day_counts_as_overdue is False
    assert not (tmp_path / "p.json").exists()


@pytest.mark.parametrize("model", [FakeModel(error=RuntimeError("down")), FakeModel(reply=["not", "an", "object"])])
def test_a_failed_extraction_leaves_the_conventions_unstated(model):
    params, taken = rp.extract_rulebook_params(TEXT_WITH_RULES, model)
    assert params == rp.DEFAULTS and taken == []
    for measure in OBLIGATION_MEASURES:
        with pytest.raises(rp.RulebookGap):
            rp.require(measure, params)


def test_a_rulebook_with_no_cue_line_asks_no_model_and_states_nothing():
    model = FakeModel(stated_reply())
    params, _ = rp.extract_rulebook_params("# Quy tac\nkhong co gi\n", model)
    assert model.calls == [] and params.installment_counts_as_debt is None


# ---------------------------------------------------------------- cache

def test_cache_round_trips_the_conventions_and_misses_when_the_rulebook_changes(tmp_path):
    rulebook, cache = tmp_path / "rulebook.md", tmp_path / "p.json"
    rulebook.write_text(TEXT_WITH_RULES, encoding="utf-8")
    first = FakeModel(stated_reply())
    params, _ = rp.load_or_extract(rulebook, first, cache)
    again = FakeModel(stated_reply(overdue_due_day_counts_as_overdue=True))
    cached, _ = rp.load_or_extract(rulebook, again, cache)
    assert again.calls == [] and cached == params
    assert json.loads(cache.read_text(encoding="utf-8"))["params"]["installment_counts_as_debt"] is True
    # the rulebook changes: the cached answer is not reused and the new convention is read
    rulebook.write_text(TEXT_WITH_RULES.replace("chưa quá hạn", "quá hạn"), encoding="utf-8")
    changed, _ = rp.load_or_extract(rulebook, again, cache)
    assert len(again.calls) == 1 and changed.overdue_due_day_counts_as_overdue is True


def test_the_prompt_asks_for_the_conventions():
    assert "installment_counts_as_debt" in rp.SYSTEM_PROMPT and "overdue_due_day_counts_as_overdue" in rp.SYSTEM_PROMPT


# ---------------------------------------------------------------- the public rulebook

@pytest.mark.skipif(not PUBLIC_RULEBOOK.exists(), reason="public rulebook not available")
def test_the_public_rulebook_text_reaches_the_model_and_a_faithful_reply_gives_the_public_meaning(monkeypatch):
    text = PUBLIC_RULEBOOK.read_text(encoding="utf-8")
    model = FakeModel(stated_reply())  # what a faithful reading of the Trả góp row says
    params, taken = rp.extract_rulebook_params(text, model)
    assert "Kỳ góp có hạn" in model.calls[0]
    assert (params.installment_counts_as_debt, params.overdue_due_day_counts_as_overdue) == (True, False)
    monkeypatch.setattr(rp, "_active", params)
    assert value(make_db(), "overdue_amount", "2026-03-31") == 200
