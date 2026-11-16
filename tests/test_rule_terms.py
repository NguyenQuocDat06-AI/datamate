from bizreason.semantic import rule_terms
from bizreason.semantic.compiler import Intent

RULEBOOK = (
    "| Luật | Mặc định |\n|---|---|\n"
    "| Khách sộp nhất | khách có giá trị trung bình một đơn cao nhất, chỉ xét khách có từ 3 đơn trở lên |\n"
    "| Ngày đắt khách nhất | ngày có nhiều đơn nhất |\n"
    "| Khách khó tính nhất | khách hay phàn nàn nhất |\n"
)


class FakeModel:
    def __init__(self, reply):
        self.reply = reply

    def generate_json(self, system, prompt, *, timeout):
        return self.reply


def test_extract_keeps_only_rankable_translations():
    reply = {"terms": [{"n": 1, "measure": "avg_per_order", "group_by": "customer", "min_orders": 3},
                       {"n": 2, "measure": "order_count", "group_by": "date", "min_orders": None},
                       {"n": 3, "measure": None, "group_by": None, "min_orders": None},
                       {"n": 9, "measure": "revenue", "group_by": "customer", "min_orders": None}]}
    rules, ok = rule_terms.extract(RULEBOOK, FakeModel(reply))
    assert ok and [(r.measure, r.group_by, r.min_orders) for r in rules] == [
        ("avg_per_order", "customer", 3), ("order_count", "date", None)]


def test_apply_patches_ranking_questions_only():
    rules, _ = rule_terms.extract(RULEBOOK, FakeModel({"terms": [
        {"n": 1, "measure": "avg_per_order", "group_by": "customer", "min_orders": 3},
        {"n": 2, "measure": "order_count", "group_by": "date", "min_orders": None}]}))
    try:
        rule_terms.set_active(rules)
        patched, term = rule_terms.apply(Intent(measure="revenue", shape="argmax_entity", group_by="customer"),
                                         "Khách sộp nhất tháng 5 là ai?")
        assert term and (patched.measure, patched.group_by, patched.min_orders) == ("avg_per_order", "customer", 3)
        day, _ = rule_terms.apply(Intent(measure="revenue", shape="argmax_entity", group_by="customer"),
                                  "ngày đắt khách nhất tháng 5")
        assert (day.shape, day.measure, day.group_by) == ("argmax_date", "order_count", None)
        scalar, term = rule_terms.apply(Intent(measure="revenue"), "Khách sộp nhất mua bao nhiêu?")
        assert term is None and scalar.measure == "revenue"
    finally:
        rule_terms.set_active(())


def test_failed_model_call_is_not_ok():
    class Boom:
        def generate_json(self, *a, **k):
            raise RuntimeError("down")
    rules, ok = rule_terms.extract(RULEBOOK, Boom())
    assert rules == [] and not ok
