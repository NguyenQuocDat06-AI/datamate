"""The measure family gate: a cue in the question must agree with the measure the model chose."""

from __future__ import annotations

import pytest

from bizreason.semantic import intent as intent_module
from bizreason.semantic.compiler import Intent
from bizreason.semantic.intent import IntentParser, apply_measure_family_gate
from bizreason.semantic import vocabulary


def _intent(measure="debt_outstanding", shape="scalar", **kw) -> Intent:
    return Intent(measure=measure, shape=shape, **kw)


def _gate(question, **kw):
    return apply_measure_family_gate(question, _intent(**kw))


@pytest.mark.parametrize("question", [
    "Có bao nhiêu khách quá hạn góp?",
    "Co bao nhieu khach qua han gop?",
    "có bnhiu khách trễ hạn góp",
    "Số khách nợ xấu là mấy?",
    "How many customers are overdue?",
])
def test_count_of_customers_repaired(question):
    gated, reason = _gate(question)
    assert reason == "" and gated.measure == "overdue_debtor_count"


@pytest.mark.parametrize("question", [
    "Tổng tiền quá hạn là bao nhiêu?",
    "tien cham gop cua khach tra gop tong cong bnhiu",
    "Số tiền trễ hạn hôm nay",
    "How much money is past due?",
])
def test_money_repaired(question):
    gated, reason = _gate(question, measure="debtor_count")
    assert reason == "" and gated.measure == "overdue_amount"


def test_other_intent_fields_survive_repair():
    original = _intent(filters=[{"dim": "location", "value": "x", "exclude": False}])
    gated, _ = apply_measure_family_gate("Tổng tiền quá hạn?", original)
    assert gated.filters == original.filters and gated.shape == "scalar"


@pytest.mark.parametrize("question", [
    "Có bao nhiêu khách quá hạn và tổng tiền là bao nhiêu?",  # both readings
    "Quá hạn góp thì sao?",                                  # neither reading
    "Có bao nhiêu đơn quá hạn?",                              # counts orders, not customers
])
def test_ambiguous_abstains(question):
    gated, reason = _gate(question)
    assert gated is None and "inconsistent" in reason and "overdue" in reason


def test_non_scalar_shape_abstains_instead_of_repair():
    gated, reason = _gate("Top 3 khách quá hạn nhiều tiền nhất", shape="top_k", group_by="customer", top_k=3)
    assert gated is None and "overdue" in reason


def test_consistent_measure_unchanged():
    original = _intent(measure="overdue_amount")
    gated, reason = apply_measure_family_gate("Tiền quá hạn bao nhiêu?", original)
    assert gated is original and reason == ""


def test_age_bucket_is_allowed():
    original = _intent(measure="debt_age_bucket", shape="breakdown")
    assert apply_measure_family_gate("Nợ quá hạn theo độ tuổi", original)[0] is original


def test_expiry_wording_is_not_an_overdue_instalment():
    original = _intent(measure="expiry_lot")
    assert apply_measure_family_gate("Lô nào quá hạn sử dụng?", original)[0] is original
    original = _intent(measure="inventory_qty")
    assert apply_measure_family_gate("Hàng quá hạn sử dụng còn bao nhiêu?", original)[0] is original


@pytest.mark.parametrize("question", [
    "Doanh thu hôm qua bao nhiêu?",
    "Có bao nhiêu khách đang nợ?",
    "Tổng nợ còn lại là bao nhiêu?",
    "Đơn trả góp có bao nhiêu?",
])
def test_question_without_cue_is_untouched(question):
    original = _intent(measure="debt_outstanding")
    gated, reason = apply_measure_family_gate(question, original)
    assert gated is original and reason == ""


def test_word_inside_another_word_is_not_a_cue():
    original = _intent(measure="revenue")
    assert apply_measure_family_gate("Doanh thu thang truoc", original)[0] is original


def test_family_without_measures_is_skipped(monkeypatch):
    families = {"ghost": {"cues": ("ma quai",), "veto": (), "allowed": frozenset({"revenue"}),
                          "money": "no_such_money", "count": "no_such_count"}}
    monkeypatch.setattr(vocabulary, "MEASURE_FAMILIES", families)
    original = _intent(measure="order_count")
    assert apply_measure_family_gate("ma quai gi do", original)[0] is original


def test_a_second_family_can_be_declared(monkeypatch):
    families = {"new": {"cues": ("thanh vien moi",), "veto": (), "allowed": frozenset({"new_member_count"}),
                        "money": "revenue", "count": "new_member_count"}}
    monkeypatch.setattr(vocabulary, "MEASURE_FAMILIES", families)
    gated, reason = apply_measure_family_gate("Co bao nhieu khach thanh vien moi?", _intent(measure="distinct_customers"))
    assert reason == "" and gated.measure == "new_member_count"


class _Model:
    def __init__(self, reply):
        self.reply = reply

    def generate_json(self, system, prompt, timeout=None):
        return self.reply


def test_parser_abstains_with_reason_on_inconsistent_measure():
    reply = {"measure": "debt_outstanding", "shape": "scalar"}
    parsed = IntentParser(_Model(reply)).parse("Quá hạn góp thì sao?")
    assert parsed.intent is None and "overdue" in parsed.reason


def test_parser_repairs_measure():
    reply = {"measure": "debt_outstanding", "shape": "scalar"}
    parsed = IntentParser(_Model(reply)).parse("Tổng tiền quá hạn góp bao nhiêu?")
    assert parsed.reason == "" and parsed.intent.measure == "overdue_amount"


def test_parser_untouched_without_cue():
    reply = {"measure": "debt_outstanding", "shape": "scalar"}
    parsed = IntentParser(_Model(reply)).parse("Tổng nợ còn lại bao nhiêu?")
    assert parsed.intent.measure == "debt_outstanding"


def test_module_exposes_gate():
    assert intent_module.apply_measure_family_gate is apply_measure_family_gate
