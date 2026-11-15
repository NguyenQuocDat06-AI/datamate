"""One value per named member ("A và B lần lượt bao nhiêu"), listed months, and an unrecorded closed dimension."""
import sqlite3
from datetime import date

from bizreason.contracts import AnswerType, Question, TimeWindow
from bizreason.semantic.compiler import Intent
from bizreason.semantic.solver import SemanticSolver, _has_list_cue
from bizreason.utils import normalize_lookup


class Parsed:
    def __init__(self, intent):
        self.intent, self.reason = intent, ""


class Parser:
    def __init__(self, intent):
        self.result = Parsed(intent)

    def parse(self, question, choices=None):
        return self.result


class Temporal:
    def __init__(self, windows):
        self.windows = windows

    def resolve(self, question, as_of):
        return self.windows

    def resolve_periods(self, question, as_of):
        return self.windows


def win(start, end):
    return TimeWindow(role="target", start=date.fromisoformat(start), end_exclusive=date.fromisoformat(end))


def make_db(with_method=True):
    con = sqlite3.connect(":memory:")
    con.create_function("lookup", 1, lambda v: normalize_lookup(v) if v is not None else None)
    con.execute("CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, revenue REAL, customer_key TEXT, "
                "channel TEXT, payment_method TEXT)")
    con.execute("CREATE TABLE lines (order_id TEXT, product_key TEXT, product_name TEXT, category TEXT, "
                "unit TEXT, qty REAL, qty_base REAL, amount REAL, line_cost REAL)")
    rows = [
        ("o1", "2026-01-05", "completed", 100.0, "c1", None, "cash" if with_method else None),
        ("o2", "2026-01-06", "completed", 40.0, "c2", None, "ewallet" if with_method else None),
        ("o3", "2026-01-07", "completed", 7.0, "c3", None, "transfer" if with_method else None),
        ("o4", "2026-02-07", "completed", 11.0, "c3", None, "cash" if with_method else None),
        ("o5", "2026-03-07", "completed", 13.0, "c3", None, "cash" if with_method else None),
    ]
    con.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()
    return con


def solve(text, intent, windows, con=None):
    solver = SemanticSolver(Parser(intent), Temporal(windows), con or make_db())
    return solver.solve(Question(id="q", business_id="b", question=text, as_of_date=date(2026, 4, 1)))


def members(*values, exclude_last=False):
    return [{"dim": "payment_method", "value": v, "exclude": exclude_last and i == len(values) - 1}
            for i, v in enumerate(values)]


JAN = [win("2026-01-01", "2026-02-01")]


def test_list_cue_tolerates_one_typo_in_the_last_word_only():
    assert _has_list_cue(" a voi b lan luot bao nhieu ")
    assert _has_list_cue(" a voi b lan nuot bao nhieu ")
    assert not _has_list_cue(" a voi b lan xyzw bao nhieu ")
    assert not _has_list_cue(" a voi b bao nhieu ")


def test_two_members_are_listed_in_the_order_of_the_intent_and_a_false_exclusion_is_ignored():
    intent = Intent(measure="revenue", shape="compare_periods", measure2="revenue",
                    filters=members("ewallet", "cash", exclude_last=True))
    pred, trace = solve("Th1 doanh thu đơn ví với đơn tiền mặt lần lượt bao nhiêu?", intent, JAN)
    assert pred.type == AnswerType.LIST
    assert pred.answer == [40, 100]


def test_three_members_are_listed():
    intent = Intent(measure="revenue", shape="breakdown", filters=members("cash", "ewallet", "transfer"))
    pred, _ = solve("Th1 doanh thu tiền mặt, ví, chuyển khoản lần lượt bao nhiêu?", intent, JAN)
    assert pred.answer == [100, 40, 7]


def test_member_the_shop_never_recorded_makes_the_question_unanswerable():
    intent = Intent(measure="revenue", shape="breakdown", filters=members("cash", "cod"))
    pred, trace = solve("Th1 doanh thu tiền mặt và cod lần lượt bao nhiêu?", intent, JAN)
    assert pred.type == AnswerType.UNANSWERABLE


def test_closed_dimension_with_no_value_in_any_order_is_unanswerable():
    intent = Intent(measure="revenue", shape="breakdown", filters=members("cash", "ewallet"))
    pred, _ = solve("Th1 doanh thu tiền mặt và ví lần lượt bao nhiêu?", intent, JAN, make_db(with_method=False))
    assert pred.type == AnswerType.UNANSWERABLE


def test_counterexample_without_a_list_cue_keeps_the_old_behaviour():
    intent = Intent(measure="revenue", shape="compare_periods", measure2="revenue",
                    filters=members("ewallet", "cash", exclude_last=True))
    pred, trace = solve("Th1 doanh thu đơn ví với đơn tiền mặt bên nào nhiều hơn?", intent, JAN)
    assert pred.type == AnswerType.ABSTAIN


def test_counterexample_exclusion_word_keeps_the_old_behaviour():
    intent = Intent(measure="revenue", shape="compare_periods", measure2="revenue",
                    filters=members("ewallet", "cash", exclude_last=True))
    pred, _ = solve("Th1 doanh thu ví và không phải tiền mặt lần lượt bao nhiêu?", intent, JAN)
    assert pred.type == AnswerType.ABSTAIN


def test_counterexample_single_member_is_not_a_list():
    intent = Intent(measure="revenue", shape="scalar", filters=members("cash"))
    pred, _ = solve("Th1 doanh thu tiền mặt lần lượt bao nhiêu?", intent, JAN)
    assert pred.type == AnswerType.NUMBER and pred.answer == 100


def test_counterexample_members_of_two_dimensions_are_not_listed():
    intent = Intent(measure="revenue", shape="breakdown", measure2="revenue",
                    filters=[{"dim": "payment_method", "value": "cash", "exclude": False},
                             {"dim": "channel", "value": "store", "exclude": False}])
    pred, _ = solve("Th1 doanh thu tiền mặt và quầy lần lượt bao nhiêu?", intent, JAN)
    assert pred.type != AnswerType.LIST


def test_months_read_as_weekday_group_become_a_list_per_month():
    months = [win("2026-01-01", "2026-02-01"), win("2026-02-01", "2026-03-01"), win("2026-03-01", "2026-04-01")]
    intent = Intent(measure="revenue", shape="breakdown", group_by="weekday", compare_as="average_per_weekday")
    pred, _ = solve("Dthu th1, th2, th3 mỗi tháng được bao nhiêu?", intent, months)
    assert pred.type == AnswerType.LIST and pred.answer == [147, 11, 13]


def test_counterexample_named_weekday_keeps_the_weekday_group():
    months = [win("2026-01-01", "2026-02-01"), win("2026-02-01", "2026-03-01")]
    intent = Intent(measure="revenue", shape="breakdown", group_by="weekday", compare_as="average_per_weekday")
    pred, _ = solve("Dthu thứ 4 mỗi tháng được bao nhiêu?", intent, months)
    assert pred.type != AnswerType.LIST
