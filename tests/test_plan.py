import sqlite3
from dataclasses import dataclass
from datetime import date

import pytest

from bizreason.contracts import AnswerType
from bizreason.semantic import knowledge
from bizreason.semantic.plan import SYSTEM, PlanCompiler, PlanError, PlanFallback, named_values, question_check
from test_knowledge import add_staff_catalog, build_shop


def _db():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, revenue REAL, employee TEXT, x_plate TEXT)")
    con.execute("CREATE TABLE lines (order_id TEXT, date TEXT, status TEXT, product_name TEXT, qty REAL, amount REAL)")
    con.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?)", [
        ("o1", "2026-01-03", "completed", 100, "An", "P1"), ("o2", "2026-01-04", "completed", 50, "Binh", "P1"),
        ("o3", "2026-01-05", "cancelled", 70, "An", "P2"), ("o4", "2026-01-06", "completed", 30, "An", "P3"),
        ("o5", "2026-02-01", "completed", 10, "Binh", "P4")])
    con.executemany("INSERT INTO lines VALUES (?,?,?,?,?,?)", [
        ("o1", "2026-01-03", "completed", "tea, cake", 2, 100), ("o2", "2026-01-04", "completed", "tea", 1, 50),
        ("o4", "2026-01-06", "completed", "cake", 1, 30)])
    return con


JAN = (date(2026, 1, 1), date(2026, 2, 1))
FEB = (date(2026, 2, 1), date(2026, 3, 1))
TODAY = date(2026, 3, 15)
PLANS = "src_plans_sheet1_ab12"
WARRANTY = "src_warranty_sheet1_cd34"
SPECS = "src_specs_sheet1_ef56"


def _shop(period=JAN, comparison=None, con=None):
    return PlanCompiler(con or build_shop(), TODAY, period, comparison=comparison)


def test_distinct_count_of_a_kept_column_over_valid_orders():
    plan = {"source": "orders", "measure": {"agg": "count_distinct", "expr": "x_plate"}, "output": "scalar"}
    assert PlanCompiler(_db(), date(2026, 3, 1), JAN).run(plan) == (AnswerType.NUMBER, 2)


def test_contains_filter_on_list_cells_counts_orders():
    plan = {"source": "lines", "filters": [{"column": "product_name", "op": "contains", "value": "tea"}],
            "measure": {"agg": "count", "expr": None}, "output": "scalar"}
    assert PlanCompiler(_db(), date(2026, 3, 1), JAN).run(plan) == (AnswerType.NUMBER, 2)


def test_ranking_table_and_share():
    c = PlanCompiler(_db(), date(2026, 3, 1), JAN)
    ranking = {"source": "orders", "group_by": "employee", "measure": {"agg": "sum", "expr": "revenue"},
               "output": "table"}
    assert c.run(ranking)[1]["rows"] == [["An", 130], ["Binh", 50]]
    share = {"source": "orders", "measure": {"agg": "sum", "expr": "revenue"}, "output": "share",
             "share_filters": [{"column": "employee", "op": "eq", "value": "Binh"}]}
    assert c.run(share) == (AnswerType.PERCENT, 27.78)


def test_unknown_column_or_unstored_value_is_rejected():
    c = PlanCompiler(_db(), date(2026, 3, 1), JAN)
    with pytest.raises(PlanError):
        c.run({"source": "orders", "measure": {"agg": "sum", "expr": "commission"}, "output": "scalar"})
    with pytest.raises(PlanError):
        c.run({"source": "orders", "filters": [{"column": "employee", "op": "eq", "value": "Chi"}],
               "measure": {"agg": "sum", "expr": "revenue"}, "output": "scalar"})


def test_fallback_returns_none_when_model_declines():
    class Model:
        def generate_json(self, system, prompt, *, timeout):
            return {"source": None}
    assert PlanFallback(Model(), None).answer("q", _db(), date(2026, 3, 1)) is None


# ------------------------------------------------------------------------------------------- linked src_ tables

OVERDUE = [{"column": "due_on", "op": "before_today"}, {"column": "paid_on", "op": "is_empty"}]


def test_src_source_with_a_date_condition_keeps_rows_of_valid_orders():
    plan = {"source": PLANS, "period": "none", "filters": OVERDUE,
            "measure": {"agg": "count_distinct", "expr": "contract_no"}, "output": "scalar"}
    assert _shop().run(plan) == (AnswerType.NUMBER, 2)  # K1, K4; K3 belongs to a cancelled order
    assert _shop().run({**plan, "scope": "all"}) == (AnswerType.NUMBER, 3)
    # count without an expression counts rows on a src_ table
    rows = {**plan, "measure": {"agg": "count"}}
    assert _shop().run(rows) == (AnswerType.NUMBER, 2)


def test_filter_on_a_linked_table_does_not_multiply_rows():
    plan = {"source": "orders", "period": "none",
            "filters": [{"column": f"{PLANS}.contract_no", "op": "not_empty"}],
            "measure": {"agg": "sum", "expr": "revenue"}, "output": "scalar"}
    # o2 and o4 have two instalments each; the order total counts once, and money rounds half up
    assert _shop().run(plan) == (AnswerType.NUMBER, 4901)
    overdue = {**plan, "filters": [{"column": f"{PLANS}.{f['column']}", "op": f["op"]} for f in OVERDUE]}
    assert _shop().run(overdue) == (AnswerType.NUMBER, 3400)  # both conditions hold on one instalment row


def test_negative_filter_on_a_linked_table_excludes_matching_orders():
    plan = {"source": "orders", "period": "none",
            "filters": [{"column": "lines.product_name", "op": "ne", "value": "Bell"}],
            "measure": {"agg": "count"}, "output": "scalar"}
    assert _shop().run(plan) == (AnswerType.NUMBER, 4)  # valid orders without a Bell line: o2, o4, o5, o6


def test_value_through_a_link_with_several_rows_is_rejected():
    plan = {"source": "orders", "period": "none", "group_by": f"{PLANS}.contract_no",
            "measure": {"agg": "sum", "expr": "revenue"}, "output": "table"}
    with pytest.raises(PlanError, match="multiplies"):
        _shop().run(plan)


def test_group_by_a_product_attribute_from_a_linked_product_table():
    plan = {"source": "lines", "group_by": f"{SPECS}.brand", "measure": {"agg": "sum", "expr": "amount"},
            "output": "table"}
    assert _shop().run(plan) == (AnswerType.TABLE, {"columns": ["name", "value"],
                                                    "rows": [["Falcon", 2800], ["Heron", 1701]]})


def test_date_filter_on_a_linked_table_with_the_question_period():
    plan = {"source": "orders", "period": "none",
            "filters": [{"column": f"{WARRANTY}.claim_date", "op": "in_period"}],
            "measure": {"agg": "count"}, "output": "scalar"}
    assert _shop(period=FEB).run(plan) == (AnswerType.NUMBER, 1)


def test_order_column_summed_over_lines_is_rejected():
    plan = {"source": "lines", "measure": {"agg": "sum", "expr": "orders.revenue"}, "output": "scalar"}
    with pytest.raises(PlanError, match="several per order"):
        _shop().run(plan)
    ok = {"source": "lines", "measure": {"agg": "count_distinct", "expr": "orders.customer_key"}, "output": "scalar"}
    assert _shop().run(ok) == (AnswerType.NUMBER, 3)


def test_unlinked_table_is_rejected():
    plan = {"source": "deliveries", "filters": [{"column": f"{SPECS}.brand", "op": "eq", "value": "Falcon"}],
            "measure": {"agg": "count"}, "output": "scalar"}
    with pytest.raises(PlanError, match="no link"):
        _shop().run(plan)


# ------------------------------------------------------------------------------------------------- date logic

def test_day_difference_between_two_date_columns_and_column_comparison():
    base = {"source": PLANS, "scope": "all", "period": "none",
            "filters": [{"column": "paid_on", "op": "not_empty"}]}
    late = {**base, "measure": {"agg": "avg", "expr": {"days": ["paid_on", "due_on"]}}, "output": "scalar"}
    assert _shop().run(late) == (AnswerType.NUMBER, 2)  # -1 and +5 days
    paid_late = {**base, "filters": [{"column": "paid_on", "op": "gt", "column2": "due_on"}],
                 "measure": {"agg": "count_rows"}, "output": "scalar"}
    assert _shop().run(paid_late) == (AnswerType.NUMBER, 1)
    by_expr = {**base, "filters": [{"expr": {"days": ["today", "due_on"]}, "op": "gt", "value": 30}],
               "measure": {"agg": "count_rows"}, "output": "scalar"}
    assert _shop().run(by_expr) == (AnswerType.NUMBER, 2)  # due on 02-10 and 02-12


def test_date_comparisons_with_a_literal_and_the_period_edges():
    plan = {"source": PLANS, "scope": "all", "period": "none",
            "filters": [{"column": "due_on", "op": "gte", "value": "2026-03-01"}],
            "measure": {"agg": "count_rows"}, "output": "scalar"}
    assert _shop().run(plan) == (AnswerType.NUMBER, 3)
    after = {**plan, "filters": [{"column": "due_on", "op": "after_period"}]}
    assert _shop(period=FEB).run(after) == (AnswerType.NUMBER, 3)
    with pytest.raises(PlanError, match="not a date"):
        _shop().run({**plan, "filters": [{"column": "contract_no", "op": "before_today"}]})


def test_latest_date_is_a_date_answer():
    plan = {"source": "orders", "period": "none",
            "filters": [{"column": "customer_name", "op": "eq", "value": "bob ray"}],
            "measure": {"agg": "max", "expr": "date"}, "output": "scalar"}
    assert _shop().run(plan) == (AnswerType.DATE, "2026-01-20")


# ------------------------------------------------------------------------------------- per-level aggregation

def test_average_per_order():
    plan = {"source": "lines", "measure": {"agg": "sum", "expr": "qty"}, "per": {"by": "order_id", "agg": "avg"},
            "output": "scalar"}
    assert _shop().run(plan) == (AnswerType.NUMBER, 2.5)


def test_count_and_share_of_customers_with_two_or_more_orders():
    plan = {"source": "orders", "measure": {"agg": "count"},
            "per": {"by": "customer_key", "agg": "count", "having": {"op": "gte", "value": 2}}, "output": "scalar"}
    assert _shop().run(plan) == (AnswerType.NUMBER, 1)
    share = {**plan, "per": {**plan["per"], "agg": "share"}}
    assert _shop().run(share) == (AnswerType.PERCENT, 33.33)


def test_per_level_inside_groups():
    plan = {"source": "orders", "group_by": "employee", "measure": {"agg": "count"},
            "per": {"by": "customer_key", "agg": "count"}, "output": "table"}
    assert _shop().run(plan)[1]["rows"] == [["Kim", 2], ["Lan", 1]]


def test_orders_delivered_in_two_or_more_trips_by_order_date():
    plan = {"source": "deliveries", "period": "until_today",
            "filters": [{"column": "orders.date", "op": "in_period"}],
            "measure": {"agg": "count_distinct", "expr": "trip"},
            "per": {"by": "order_id", "agg": "count", "having": {"op": "gte", "value": 2}}, "output": "scalar"}
    assert _shop().run(plan) == (AnswerType.NUMBER, 2)  # o1 and o4


# -------------------------------------------------------------------------------- arithmetic between aggregates

def test_ratio_of_two_aggregates_with_their_own_scope():
    plan = {"source": "orders", "scope": "all",
            "measure": {"op": "/", "left": {"agg": "count", "scope": "cancelled"}, "right": {"agg": "count"}},
            "as_percent": True, "output": "scalar"}
    assert _shop().run(plan) == (AnswerType.PERCENT, 20.0)


def test_difference_with_side_filters():
    plan = {"source": "orders", "measure": {
        "op": "-", "left": {"agg": "sum", "expr": "revenue", "filters": [{"column": "employee", "op": "eq",
                                                                           "value": "Kim"}]},
        "right": {"agg": "sum", "expr": "revenue", "filters": [{"column": "employee", "op": "eq", "value": "Lan"}]}},
        "output": "scalar"}
    assert _shop().run(plan) == (AnswerType.NUMBER, 2300)  # 3800 - 1500.5, money half up


def test_change_against_the_comparison_period():
    rev = {"agg": "sum", "expr": "revenue"}
    plan = {"source": "orders", "period": "none", "measure": {
        "op": "/", "left": {"op": "-", "left": {**rev, "period": "question"}, "right": {**rev, "period": "comparison"}},
        "right": {**rev, "period": "comparison"}}, "as_percent": True, "output": "scalar"}
    assert _shop(period=FEB, comparison=JAN).run(plan) == (AnswerType.PERCENT, -60.38)
    with pytest.raises(PlanError, match="period none"):
        _shop(period=FEB, comparison=JAN).run({**plan, "period": "question"})
    with pytest.raises(PlanError, match="comparison"):
        _shop(period=FEB).run(plan)


# ----------------------------------------------------------------------------------------------------- outputs

def test_ranking_one_name_top_k_list_and_ties():
    plan = {"source": "orders", "group_by": "employee", "measure": {"agg": "sum", "expr": "revenue"},
            "output": "ranking"}
    assert _shop().run(plan) == (AnswerType.STRING, "Kim")
    assert _shop().run({**plan, "limit": 2}) == (AnswerType.LIST, ["Kim", "Lan"])
    tie = {"source": "orders", "group_by": "customer_name", "measure": {"agg": "count"}, "output": "ranking"}
    with pytest.raises(PlanError, match="tie"):
        _shop(period=FEB).run(tie)
    assert _shop(period=FEB).run({**tie, "limit": 2}) == (AnswerType.LIST, ["Ann Lee", "Cy Dao"])
    assert _shop(period=FEB).run({**tie, "output": "list"}) == (AnswerType.LIST, ["Ann Lee", "Cy Dao"])


def test_list_with_having_and_yes_no():
    plan = {"source": "orders", "group_by": "customer_name", "measure": {"agg": "count"},
            "having": {"op": "gte", "value": 2}, "output": "list"}
    assert _shop().run(plan) == (AnswerType.LIST, ["Ann Lee"])
    yes = {"source": "orders", "measure": {"agg": "sum", "expr": "revenue"}, "output": "yesno",
           "test": {"op": "gt", "value": 5000}}
    assert _shop().run(yes) == (AnswerType.BOOLEAN, True)
    with pytest.raises(PlanError, match="test"):
        _shop().run({**yes, "test": None})


def test_rounding_of_money_fractions_and_counts():
    avg_money = {"source": "orders", "measure": {"agg": "avg", "expr": "revenue"}, "output": "scalar"}
    assert _shop().run(avg_money) == (AnswerType.NUMBER, 1325)  # 5300.5 / 4
    avg_qty = {"source": "lines", "measure": {"agg": "avg", "expr": "qty"}, "output": "scalar"}
    assert _shop().run(avg_qty) == (AnswerType.NUMBER, 1.67)
    ratio = {"source": "orders", "measure": {"op": "/", "left": {"agg": "sum", "expr": "revenue"},
                                             "right": {"agg": "count"}}, "output": "scalar"}
    assert _shop().run(ratio) == (AnswerType.NUMBER, 1325.13)  # a ratio keeps two places


# ------------------------------------------------------------------------------------------------- self-checks

def test_contains_value_that_no_cell_holds_is_rejected():
    plan = {"source": "lines", "filters": [{"column": "product_name", "op": "contains", "value": "brake pads"}],
            "measure": {"agg": "count"}, "output": "scalar"}
    with pytest.raises(PlanError, match="contains"):
        _shop().run(plan)


def test_mostly_empty_filter_column_is_rejected_unless_the_shop_wrote_the_blanks():
    con = build_shop()
    con.executemany("INSERT INTO lines (order_id, date, status, product_name, qty, amount) VALUES (?,?,?,?,?,?)",
                    [(f"z{i}", "2025-06-01", "cancelled", "Bell", 1, 1) for i in range(5)])
    con.executemany("INSERT INTO orders (order_id, date, status, revenue) VALUES (?,?,?,?)",
                    [(f"z{i}", "2025-06-01", "cancelled", 1) for i in range(8)])
    sized = {"source": "lines", "filters": [{"column": "x_cat_size", "op": "eq", "value": "L"}],
             "measure": {"agg": "count"}, "output": "scalar"}
    with pytest.raises(PlanError, match="mostly empty"):
        _shop(con=con).run(sized)
    noted = {"source": "orders", "scope": "all", "filters": [{"column": "x_ghi_chu", "op": "contains",
                                                              "value": "warranty"}],
             "measure": {"agg": "count"}, "output": "scalar"}
    assert _shop(con=con).run(noted) == (AnswerType.NUMBER, 1)


def test_zero_from_a_filter_that_matches_nothing_in_scope_is_rejected():
    kim = {"source": "orders", "filters": [{"column": "employee", "op": "eq", "value": "Kim"}],
           "measure": {"agg": "count"}, "output": "scalar"}
    with pytest.raises(PlanError, match="matched no rows"):
        _shop(period=FEB).run(kim)  # Kim sold nothing in February
    real_zero = {"source": "orders", "filters": [{"column": "employee", "op": "eq", "value": "Lan"},
                                                 {"column": "revenue", "op": "gt", "value": 5000}],
                 "measure": {"agg": "count"}, "output": "scalar"}
    assert _shop(period=FEB).run(real_zero) == (AnswerType.NUMBER, 0)


def test_empty_result_is_rejected():
    plan = {"source": "orders", "filters": [{"column": "revenue", "op": "gt", "value": 99999}],
            "measure": {"agg": "sum", "expr": "revenue"}, "output": "scalar"}
    with pytest.raises(PlanError, match="empty"):
        _shop().run(plan)


def test_closed_language_rejects_unknown_parts():
    c = _shop()
    for bad in ({"source": "sqlite_master", "measure": {"agg": "count"}},
                {"source": "orders", "measure": {"agg": "median", "expr": "revenue"}},
                {"source": "orders", "measure": {"agg": "count"}, "filters": [{"column": "revenue; drop", "op": "eq"}]},
                {"source": "orders", "measure": {"agg": "count"}, "output": "chart"},
                {"source": "orders", "measure": {"agg": "count"}, "per": {"by": "customer_key", "agg": "median"}},
                {"source": "orders", "measure": {"agg": "count"}, "group_by": "employee", "output": "table",
                 "limit": 0}):
        with pytest.raises(PlanError):
            c.run(bad)


@dataclass
class _Window:
    role: str
    start: date
    end_exclusive: date


class _Temporal:
    def __init__(self, windows):
        self.windows = windows

    def resolve(self, question, as_of):
        return self.windows


class _Index:
    def __init__(self, found):
        self.found = found

    def candidates(self, question):
        return self.found

    def block(self, question):
        return "Stored values near phrases in the question: " + str(self.found)


def test_named_values_keep_whole_multiword_or_distinctive_names():
    index = _Index({"product": ["Road Bike", "City Bike"], "employee": ["Kim"], "unit": ["box set"],
                    "x": ["Falcon9"]})
    assert named_values("how many road bikes and road bike frames, Kim, box set, falcon9", index) == \
        ["Road Bike", "Falcon9"]


def test_question_check_period_and_named_values():
    plan = {"source": "orders", "measure": {"agg": "count"}}
    assert question_check(plan, JAN, None, []) is None  # the period applies by default
    assert "period" in question_check({**plan, "period": "none"}, JAN, None, [])
    in_period = {**plan, "period": "none", "filters": [{"column": f"{WARRANTY}.claim_date", "op": "in_period"}]}
    assert question_check(in_period, JAN, None, []) is None
    assert "compares" in question_check(plan, JAN, FEB, [])
    today = (TODAY, date(2026, 3, 16))
    assert question_check({**plan, "period": "until_today"}, today, None, [], TODAY) is None
    assert "period" in question_check({**plan, "period": "until_today"}, JAN, None, [], TODAY)
    named = {**plan, "filters": [{"column": "lines.product_name", "op": "in", "value": ["road bike", "Bell"]}]}
    assert question_check(named, JAN, None, ["Road Bike"]) is None
    assert "Road Bike" in question_check(plan, JAN, None, ["Road Bike"])


def test_fallback_prompt_card_checks_and_cache(tmp_path, monkeypatch):
    path = tmp_path / "shop.sqlite"
    build_shop(str(path)).close()
    built = []
    real = knowledge.build_card
    monkeypatch.setattr(knowledge, "build_card", lambda db: built.append(1) or real(db))

    class Model:
        plan = {"source": PLANS, "period": "none", "filters": OVERDUE,
                "measure": {"agg": "count_distinct", "expr": "contract_no"}, "output": "scalar"}

        def generate_json(self, system, prompt, *, timeout):
            self.system, self.prompt = system, prompt
            return self.plan

    model = Model()
    fallback = PlanFallback(model, _Temporal([]), definitions=lambda q: "- overdue: past due and unpaid",
                            value_index=lambda con: _Index({}))
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    result = fallback.answer("how many instalments are overdue", con, TODAY)
    assert (result.type, result.answer) == (AnswerType.NUMBER, 2) and fallback.last_reason == "answered by plan"
    assert "Today: 2026-03-15" in model.prompt and "The question states no period." in model.prompt
    assert 'due_on "DUE ON": date the instalment is due' in model.prompt
    assert "- overdue: past due and unpaid" in model.prompt and model.system == SYSTEM
    fallback.answer("again", con, TODAY)
    assert len(built) == 1  # one card per shop
    # a stated period the plan leaves out is a rejection, not an answer
    fallback.temporal = _Temporal([_Window("target", JAN[0], JAN[1])])
    assert fallback.answer("overdue in January", con, TODAY) is None
    assert "period" in fallback.last_reason
    assert "Question's period: 2026-01-01 to 2026-01-31" in model.prompt


def test_fallback_rejects_a_plan_that_ignores_a_named_value():
    class Model:
        def generate_json(self, system, prompt, *, timeout):
            return {"source": "lines", "measure": {"agg": "sum", "expr": "qty"}, "output": "scalar"}

    fallback = PlanFallback(Model(), _Temporal([_Window("target", JAN[0], JAN[1])]),
                            value_index=lambda con: _Index({"product": ["Road Bike"]}))
    assert fallback.answer("road bike sold in january", build_shop(), TODAY) is None
    assert "Road Bike" in fallback.last_reason


def test_system_prompt_examples_cover_the_language():
    for part in ('"per"', "src_", '"op": "/"', '"contains"', '"output": "table"', '"ranking"', '"comparison"'):
        assert part in SYSTEM


def test_large_nonmoney_quantity_keeps_fractional_precision():
    db = _db()
    db.execute("UPDATE lines SET qty=1000.25")
    plan = {"source": "lines", "measure": {"agg": "avg", "expr": "qty"}}
    assert PlanCompiler(db, TODAY, JAN).run(plan) == (AnswerType.NUMBER, 1000.25)


def test_yesno_compares_the_unrounded_measure():
    db = _db()
    db.execute("UPDATE orders SET revenue=100.49 WHERE order_id='o1'")
    plan = {"source": "orders", "filters": [{"column": "order_id", "op": "eq", "value": "o1"}],
            "measure": {"agg": "sum", "expr": "revenue"}, "output": "yesno",
            "test": {"op": "gt", "value": 100.4}}
    assert PlanCompiler(db, TODAY, JAN).run(plan) == (AnswerType.BOOLEAN, True)
    plan["test"] = {"op": "eq", "value": 100}
    assert PlanCompiler(db, TODAY, JAN).run(plan) == (AnswerType.BOOLEAN, False)


def test_count_rows_of_a_parent_column_rejects_order_multiplication():
    plan = {"source": "lines", "measure": {"agg": "count_rows", "expr": "orders.customer_key"}}
    with pytest.raises(PlanError, match="several per order"):
        _shop().run(plan)


def test_zero_from_a_remote_value_filter_outside_scope_is_rejected():
    plan = {"source": "orders", "filters": [{"column": f"{WARRANTY}.ticket", "op": "eq", "value": "W3"}],
            "measure": {"agg": "count"}}
    with pytest.raises(PlanError, match="matched no rows"):
        _shop().run(plan)  # the linked claim belongs to a February order, not January


def test_zero_from_a_numeric_equality_with_no_scoped_match_is_rejected():
    plan = {"source": "orders", "filters": [{"column": "revenue", "op": "eq", "value": 900}],
            "measure": {"agg": "count"}}
    with pytest.raises(PlanError, match="matched no rows"):
        _shop().run(plan)  # the value is stored in February only


def test_cancelled_purchase_scope_does_not_include_valid_receipts():
    db = build_shop()
    db.executemany("INSERT INTO purchases (purchase_id,date,amount,cancelled) VALUES (?,?,?,?)",
                   [("r1", "2026-01-04", 40, 0), ("r2", "2026-01-05", 70, 1)])
    plan = {"source": "purchases", "scope": "cancelled", "measure": {"agg": "sum", "expr": "amount"}}
    assert _shop(con=db).run(plan) == (AnswerType.NUMBER, 70)


@pytest.mark.parametrize("unused", [
    {"having": {"op": "gt", "value": 50}},
    {"share_filters": [{"column": "employee", "op": "eq", "value": "Kim"}]},
    {"test": {"op": "gt", "value": 10}},
])
def test_constraints_that_the_output_would_ignore_are_rejected(unused):
    with pytest.raises(PlanError):
        _shop().run({"source": "orders", "measure": {"agg": "count"}, **unused})


def test_period_checks_ignore_period_words_inside_values_and_unused_fields():
    plan = {"source": "orders", "period": "none", "measure": {"agg": "count"},
            "filters": [{"column": "employee", "op": "eq", "value": "comparison"}]}
    assert "compares" in question_check(plan, None, FEB, [])
    assert "period" in question_check({**plan, "unused": {"period": "question"}}, JAN, None, [])
    today = (TODAY, date(2026, 3, 16))
    named_today = {**plan, "filters": [{"column": "employee", "op": "eq", "value": "today"}]}
    assert "period" in question_check(named_today, today, None, [], TODAY)


def test_named_product_cannot_be_satisfied_by_a_broader_partial_filter():
    plan = {"source": "lines", "measure": {"agg": "count"},
            "filters": [{"column": "product_name", "op": "contains", "value": "bike"}]}
    assert "Road Bike" in question_check(plan, JAN, None, ["Road Bike"])


def test_staff_dimension_join_uses_normalized_names_without_changing_money_totals():
    db = build_shop()
    add_staff_catalog(db)
    plan = {"source": "orders", "group_by": "src_staff.category",
            "measure": {"agg": "sum", "expr": "revenue"}, "output": "table"}
    assert _shop(con=db).run(plan) == (AnswerType.TABLE, {"columns": ["name", "value"],
                                                        "rows": [["senior", 3800], ["junior", 1501]]})
    commission = {"source": "orders", "measure": {"agg": "sum", "expr": {"op": "/",
        "left": {"op": "*", "left": "revenue", "right": "src_staff.rate"}, "right": 100}}}
    assert _shop(con=db).run(commission) == (AnswerType.NUMBER, 680.1)


def test_staff_filter_with_duplicate_catalog_names_does_not_fan_out():
    db = build_shop()
    add_staff_catalog(db, [("Kim", 10, "senior"), (" KIM ", 15, "senior"), ("Lan", 20, "junior")])
    plan = {"source": "orders", "filters": [{"column": "src_staff.category", "op": "eq", "value": "senior"}],
            "measure": {"agg": "sum", "expr": "revenue"}}
    assert _shop(con=db).run(plan) == (AnswerType.NUMBER, 3800)
    with pytest.raises(PlanError, match="multiplies"):
        _shop(con=db).run({"source": "orders", "measure": {"agg": "sum", "expr": "src_staff.rate"}})


def test_staff_dimension_can_reach_lines_through_their_order_only():
    db = build_shop()
    add_staff_catalog(db)
    plan = {"source": "lines", "filters": [{"column": "src_staff.category", "op": "eq", "value": "senior"}],
            "measure": {"agg": "sum", "expr": "amount"}}
    assert _shop(con=db).run(plan) == (AnswerType.NUMBER, 3800)
    with pytest.raises(PlanError, match="no link"):
        _shop(con=db).run({**plan, "source": SPECS, "scope": "all", "period": "none"})


def test_multiple_distinct_dimension_relationships_are_not_chosen_arbitrarily():
    db = build_shop()
    db.execute("CREATE TABLE src_people (staff TEXT, customer TEXT, rate REAL)")
    db.executemany("INSERT INTO src_people VALUES (?,?,?)", [("Kim", "c1", 10), ("Lan", "c2", 20)])
    db.executemany("INSERT INTO column_catalog VALUES (?,?,?,?,?,?,?,?,?)", [
        ("src_people", "staff", "people", "Staff", "employee_name", "text", 2, '[]', None),
        ("src_people", "customer", "people", "Customer", "customer_id", "code", 2, '[]', None)])
    with pytest.raises(PlanError, match="ambiguous dimension"):
        _shop(con=db).run({"source": "orders", "measure": {"agg": "sum", "expr": "src_people.rate"}})


@pytest.mark.parametrize("role,keys", [("customer_id", ["c1", "c2"]),
                                     ("customer_name", [" ANN LEE ", "bob ray"])])
def test_customer_dimension_filter_uses_the_verified_customer_key(role, keys):
    db = build_shop()
    db.execute("CREATE TABLE src_customers (person TEXT, tier TEXT)")
    db.executemany("INSERT INTO src_customers VALUES (?,?)", [(keys[0], "gold"), (keys[1], "silver")])
    db.execute("INSERT INTO column_catalog VALUES ('src_customers','person','customer_sheet','Customer',"
               "?,'text',2,'[]',NULL)", [role])
    plan = {"source": "orders", "filters": [{"column": "src_customers.tier", "op": "eq", "value": "gold"}],
            "measure": {"agg": "sum", "expr": "revenue"}}
    assert _shop(con=db).run(plan) == (AnswerType.NUMBER, 3500)
