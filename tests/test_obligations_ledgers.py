"""Debts and instalments read from side ledgers of the form "first row + follow-up rows" (bizreason.semantic.obligations).

The shops here are invented; column names are neutral. Expected numbers are worked by hand next to each fixture.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import date

import pytest

from bizreason.contracts import TimeWindow
from bizreason.semantic import debt_sources, obligations, rulebook_params
from bizreason.semantic.compiler import DEBT_MEASURES, Compiler, Intent, Unsupported
from bizreason.semantic.vocabulary import MEASURES
from bizreason.utils import normalize_lookup

SCHEMA = """
CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, customer_key TEXT, payment_method TEXT,
                     total_due REAL, revenue REAL);
CREATE TABLE lines (order_id TEXT, date TEXT, status TEXT, product_name TEXT, amount REAL);
CREATE TABLE purchases (purchase_id TEXT, date TEXT, cancelled INTEGER, amount REAL);
CREATE TABLE payments (order_id TEXT, date TEXT, amount REAL, method TEXT);
CREATE TABLE shop_docs (key TEXT, text TEXT);
CREATE TABLE column_catalog (table_name TEXT, column_name TEXT, source_table TEXT, source_header TEXT, role TEXT,
                             value_type TEXT, distinct_count INTEGER, sample_values TEXT, join_key_to TEXT,
                             derived INTEGER DEFAULT 0);
"""

# order_id, date, status, customer_key (orders 1-6 sit in the debt ledger, 11-16 in the instalment ledger)
ORDERS = [
    ("1", "2026-01-10", "completed", "cA"), ("2", "2026-01-15", "completed", "cB"),
    ("3", "2026-01-20", "completed", "cC"), ("4", "2026-02-01", "completed", "cA"),
    ("5", "2026-02-02", "cancelled", "cD"), ("6", "2026-02-03", "completed", "cE"),
    ("7", "2026-02-04", "completed", "cK"), ("8", "2026-02-05", "completed", None),
    ("11", "2026-01-10", "completed", "cF"), ("12", "2026-01-31", "completed", "cJ"),
    ("13", "2026-02-10", "completed", "cG"), ("14", "2026-02-15", "completed", "cF"),
    ("15", "2026-03-01", "completed", "cH"), ("16", "2026-03-02", "completed", "cI"),
]

# Debt ledger: order -> (day, still owed after the sale, repayments (day, amount), first repayment on the first row)
DEBTS = {
    "1": ("2026-01-10", 100, [("2026-01-20", 30), ("2026-02-05", 20)], True),
    "2": ("2026-01-15", 40, [], False),
    "3": ("2026-01-20", 90, [("2026-01-25", 90)], True),
    "4": ("2026-02-01", 200, [("2026-02-10", 50), ("2026-03-10", 50)], False),
    "5": ("2026-02-02", 70, [("2026-02-03", 10)], True),  # cancelled order: never counts
    "6": ("2026-02-03", 25, [], False),
}
# Instalment ledger: order -> (day, price, deposit, terms, per term, due day, repayments)
PLANS = {
    "11": ("2026-01-10", 1000, 400, 3, 200, 5, [("2026-02-05", 200), ("2026-03-06", 200)]),
    "12": ("2026-01-31", 600, 0, 6, 100, 31, [("2026-03-01", 100)]),
    "13": ("2026-02-10", 900, 300, 3, 200, 15, []),
    "14": ("2026-02-15", 500, 100, 4, 100, 20, [("2026-03-20", 100), ("2026-04-20", 100)]),
    "15": ("2026-03-01", 300, 0, 3, 100, 1, [("2026-04-01", 100)]),
    "16": ("2026-03-02", 700, 100, 6, 100, 2, [("2026-04-02", 100), ("2026-05-02", 100)]),
}


def window(day: str) -> TimeWindow:
    end = date.fromisoformat(day)
    return TimeWindow(start=end, end_exclusive=date.fromordinal(end.toordinal() + 1))


def add_table(db: sqlite3.Connection, name: str, spec: list[tuple[str, str, str]], rows: list[tuple],
              join_key: str | None = None) -> None:
    """A carried src_ table: plain text columns, xn_ for numbers, xd_ for days, and their catalog rows.
    spec = [(column, role, value type)]; value types: number, date, text."""
    plain = ", ".join(f'"{c}" TEXT' for c, _, _ in spec)
    typed = ", ".join(part for part in (", ".join(f'"xn_{c}" REAL' for c, _, t in spec if t == "number"),
                                        ", ".join(f'"xd_{c}" TEXT' for c, _, t in spec if t == "date")) if part)
    db.execute(f'CREATE TABLE "{name}" ({plain}, {typed})')
    for row in rows:
        values = list(row)
        extra = [None if v is None else float(v) for v, (_, _, t) in zip(row, spec) if t == "number"] + \
                [v for v, (_, _, t) in zip(row, spec) if t == "date"]
        marks = ",".join("?" * (len(values) + len(extra)))
        db.execute(f'INSERT INTO "{name}" VALUES ({marks})', [None if v is None else str(v) for v in values] + extra)
    for column, role, kind in spec:
        db.execute("INSERT INTO column_catalog (table_name, column_name, role, value_type, join_key_to) "
                   "VALUES (?,?,?,?,?)", (name, column, role, kind, join_key if role == "order_id" else None))


def debt_rows(debts=DEBTS) -> list[tuple]:
    rows = []
    for order, (day, owed, paid, first_on_head) in debts.items():
        head_paid = paid[0] if (paid and first_on_head) else (None, None)
        rows.append((day, owed, head_paid[1], head_paid[0], f"#{order}"))
        for paid_on, amount in (paid[1:] if first_on_head else paid):
            rows.append((None, None, amount, paid_on, f"#{order}"))
    return rows


DEBT_SPEC = [("opened", "ordered_at", "date"), ("owed", "order_outstanding", "number"),
             ("paid", "paid_amount", "number"), ("paid_on", "other", "date"), ("memo", "note", "text")]
PLAN_SPEC = [("opened", "ordered_at", "date"), ("ref", "order_id", "text"), ("price", "other", "number"),
             ("deposit", "paid_amount", "number"), ("terms", "installment_count", "number"),
             ("per_term", "installment_amount", "number"), ("due_day", "due_at", "number"),
             ("paid", "paid_amount", "number"), ("paid_on", "other", "date")]


@pytest.fixture(autouse=True)
def rulebook_states_the_instalment_conventions(monkeypatch):
    """These tests model a rulebook that states the instalment conventions; see test_rulebook_gates for the rest."""
    monkeypatch.setattr(rulebook_params, "_active", dataclasses.replace(
        rulebook_params.DEFAULTS, installment_counts_as_debt=True, overdue_due_day_counts_as_overdue=False))


def plan_rows(plans=PLANS) -> list[tuple]:
    rows = []
    for order, (day, price, deposit, terms, per, due_day, paid) in plans.items():
        rows.append((day, order, price, deposit, terms, per, due_day, *(paid[0][::-1] if paid else (None, None))))
        for paid_on, amount in paid[1:]:
            rows.append((None, order, None, None, None, None, None, amount, paid_on))
    return rows


class Db(sqlite3.Connection):
    report: dict = {}


def make_db(debts=DEBTS, plans=PLANS, payments=(), orders=ORDERS, build=True) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", factory=Db)
    db.create_function("lookup", 1, lambda v: None if v is None else normalize_lookup(v))
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO orders VALUES (?,?,?,?,NULL,NULL,NULL)", orders)
    db.executemany("INSERT INTO payments VALUES (?,?,?,?)", payments)
    if debts is not None:
        add_table(db, "src_debts", DEBT_SPEC, debt_rows(debts))
    if plans is not None:
        add_table(db, "src_plans", PLAN_SPEC, plan_rows(plans), join_key="orders.order_id")
    if build:
        db.report = obligations.build(db)
    return db


def value(db: sqlite3.Connection, measure: str, day: str, **kwargs) -> float | None:
    return Compiler(db).value(Intent(measure=measure, shape="scalar", **kwargs), window(day))


# ---------------------------------------------------------------- building

def test_debt_ledger_with_a_note_key_builds_obligations_and_events():
    db = make_db(plans=None)
    assert db.report["built"] == 6
    kinds = dict(db.execute("SELECT order_id, kind FROM obligations"))
    assert set(kinds.values()) == {"debt"} and set(kinds) == set(DEBTS)
    assert dict(db.execute("SELECT order_id, amount_due FROM obligations"))["4"] == 200
    # one event per repayment row; the first repayment on the first row is an event too
    assert db.execute("SELECT COUNT(*), SUM(amount) FROM obligation_events").fetchone() == (6, 250)
    assert db.execute("SELECT COUNT(*) FROM obligation_schedule").fetchone() == (0,)


def test_instalment_ledger_builds_amount_due_events_and_schedule():
    db = make_db(debts=None)
    assert dict(db.execute("SELECT order_id, amount_due FROM obligations")) == {
        "11": 600, "12": 600, "13": 600, "14": 400, "15": 300, "16": 600}
    assert {r[0] for r in db.execute("SELECT kind FROM obligations")} == {"installment"}
    assert db.execute("SELECT COUNT(*) FROM obligation_events").fetchone() == (8,)
    assert db.execute("SELECT COUNT(*) FROM obligation_schedule").fetchone() == (3 + 6 + 3 + 4 + 3 + 6,)


def test_schedule_starts_the_month_after_the_order_and_clamps_the_day():
    db = make_db(debts=None)
    due = [r[0] for r in db.execute("SELECT due_date FROM obligation_schedule WHERE order_id='12' ORDER BY k")]
    assert due[:3] == ["2026-02-28", "2026-03-31", "2026-04-30"]
    due = [r[0] for r in db.execute("SELECT due_date FROM obligation_schedule WHERE order_id='11' ORDER BY k")]
    assert due == ["2026-02-05", "2026-03-05", "2026-04-05"]


def plans_on(day: str, due_day: int, paid_on: str) -> tuple[dict, list]:
    """Six plans of one day each, every first repayment on `paid_on` and a second later (a ledger needs follow-up rows
    to tell a repayment column from a column of the first row); and the orders that carry them."""
    plans = {str(11 + n): (day, 600, 0, 3, 200, due_day, [(paid_on, 100), ("2026-02-28", 100)]) for n in range(6)}
    orders = [o for o in ORDERS if o[0] not in plans] + [(k, day, "completed", "c" + k) for k in plans]
    return plans, orders


def test_schedule_follows_the_month_offset_the_repayments_show():
    # Every first repayment falls a day after the instalment day of the order's own month: that month is the first.
    plans, orders = plans_on("2026-01-05", 10, "2026-01-11")
    db = make_db(debts=None, plans=plans, orders=orders)
    first = db.execute("SELECT due_date FROM obligation_schedule WHERE order_id='11' AND k=1").fetchone()[0]
    assert first == "2026-01-10"


def test_schedule_is_not_built_when_repayments_cannot_tell_the_offset():
    # A repayment 16 days after one reading's instalment day and 15 before the other's: no clear winner.
    plans, orders = plans_on("2026-01-01", 15, "2026-01-31")
    db = make_db(debts=None, plans=plans, orders=orders)
    assert db.report["built"] == 6  # the obligations are still built
    assert db.execute("SELECT COUNT(*) FROM obligation_schedule").fetchone() == (0,)
    with pytest.raises(Unsupported):
        value(db, "overdue_amount", "2026-04-01")


def test_instalment_equation_failing_builds_nothing():
    plans = dict(PLANS)
    day, price, deposit, terms, per, due_day, paid = plans["11"]
    plans["11"] = (day, price + 50, deposit, terms, per, due_day, paid)
    plans["13"] = (*plans["13"][:1], 901, *plans["13"][2:])
    plans["15"] = (*plans["15"][:1], 330, *plans["15"][2:])
    db = make_db(debts=None, plans=plans)
    assert db.report["built"] == 0
    assert not obligations.active(db)


def test_repayments_exceeding_what_is_owed_build_nothing():
    debts = {k: (d, owed, [(day, amount * 5) for day, amount in paid], head) for k, (d, owed, paid, head) in DEBTS.items()}
    db = make_db(debts=debts, plans=None)
    assert db.report["built"] == 0
    assert any("exceed" in reason for reason in db.report["skipped"])


def test_unresolved_keys_build_nothing():
    rows = debt_rows()
    rows = [(*r[:4], "ref " + r[4].replace("#", "9") + "99") for r in rows]  # numbers that name no order
    db = make_db(debts=None, plans=None, build=False)
    add_table(db, "src_debts", DEBT_SPEC, rows)
    report = obligations.build(db)
    assert report["built"] == 0 and not obligations.active(db)


def test_a_repayment_without_a_day_builds_nothing():
    rows = debt_rows()
    rows[1] = (*rows[1][:3], None, rows[1][4])
    db = make_db(debts=None, plans=None, build=False)
    add_table(db, "src_debts", DEBT_SPEC, rows)
    report = obligations.build(db)
    assert report["built"] == 0 and any("no day" in reason for reason in report["skipped"])


def test_a_date_column_without_typed_companion_is_named_in_the_report():
    db = make_db(debts=None, plans=None, build=False)
    add_table(db, "src_debts", DEBT_SPEC, debt_rows())
    db.execute("UPDATE src_debts SET paid_on = '03/02/2026' WHERE paid_on IS NOT NULL")
    db.execute("ALTER TABLE src_debts DROP COLUMN xd_paid_on")
    report = obligations.build(db)
    assert report["built"] == 0
    assert any("xd_" in reason and "paid_on" in reason for reason in report["skipped"])


def test_ledger_days_that_differ_from_the_orders_build_nothing():
    shifted = [(o, "2026-12-31" if i < 6 else d, s, c) for i, (o, d, s, c) in enumerate(ORDERS)]
    db = make_db(plans=None, orders=shifted)
    assert db.report["built"] == 0
    assert any("order days" in reason for reason in db.report["skipped"])


def test_two_ledgers_naming_the_same_order_build_nothing():
    plans = dict(PLANS)
    debts = dict(DEBTS)
    debts["11"] = ("2026-01-10", 10, [("2026-01-12", 10)], True)
    db = make_db(debts=debts, plans=plans)
    assert db.report["built"] == 0
    assert any("same order" in reason for reason in db.report["skipped"])


def test_a_shop_with_a_real_payment_table_builds_nothing_and_reads_payments_as_before():
    payments = [("1", "2026-01-12", 100, None)]
    db = make_db(payments=payments)
    assert db.report["built"] == 0 and not obligations.active(db)
    assert "obligations" not in {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
    assert Compiler(db)._payment_index() == {"1": [("2026-01-12", 100)]}


def test_obligation_tables_do_not_apply_when_payments_have_rows():
    db = make_db()
    assert obligations.active(db)
    db.execute("INSERT INTO payments VALUES ('1', '2026-02-01', 5, NULL)")
    assert not obligations.active(db)
    assert Compiler(db)._payment_index() == {"1": [("2026-02-01", 5)]}


def test_debt_sources_defer_to_the_ledgers():
    db = make_db()
    assert debt_sources.payments_unrecorded(db) is False
    assert debt_sources.order_state_settlements(db) is None


# ---------------------------------------------------------------- compiler

def test_debt_outstanding_reads_repayments_by_the_cutoff_day():
    db = make_db(plans=None)
    # End of 02-28: order 1 owes 100-50, 2 owes 40, 3 is paid, 4 owes 200-50, 6 owes 25; 5 is cancelled.
    assert value(db, "debt_outstanding", "2026-02-28") == 50 + 40 + 150 + 25
    # End of 02-05 the repayment of 02-10 has not happened and 02-05's own counts.
    assert value(db, "debt_outstanding", "2026-02-05") == 50 + 40 + 200 + 25


def test_debtor_count_counts_customers_once_not_orders():
    db = make_db(plans=None)
    assert value(db, "debtor_count", "2026-02-28") == 3  # cA (two orders), cB, cE; cC is paid


def test_debt_aging_buckets_of_a_ledger_debt():
    db = make_db(plans=None)
    answer = Compiler(db).debt_aging(Intent(measure="debt_outstanding", shape="breakdown"), window("2026-02-05"),
                                     [(0, 7), (8, 15), (16, None)])
    # Ages on 02-05: order 4 is 4 days old (200), 6 is 2 (25), 2 is 21 (40), 1 is 26 (50).
    assert answer.value == [225, 0, 90]


def test_debt_ranking_by_customer():
    db = make_db(plans=None)
    ranked = Compiler(db)._ranked_debtors(Intent(measure="debt_outstanding", shape="top_k", group_by="customer"),
                                          window("2026-02-28"))
    assert ranked == [("cA", 200), ("cB", 40), ("cE", 25)]


def test_both_ledgers_count_in_the_debt_of_the_shop():
    db = make_db()
    # Debts: 1 owes 50, 2 owes 40, 4 owes 100 (both repayments by 03-31), 6 owes 25 -> 215. Instalments: 2500.
    assert value(db, "debt_outstanding", "2026-03-31") == 215 + 2500
    assert value(db, "installment_outstanding", "2026-03-31") == 2500


def test_a_debt_cutoff_before_an_order_ignores_it():
    db = make_db()
    assert value(db, "debt_outstanding", "2026-01-12") == 100 + 600  # orders 1 and 11 only; 11: 1000-400 owed


def test_installments_due_count_includes_the_day_it_falls_due():
    db = make_db(debts=None)
    assert value(db, "installments_due_count", "2026-03-31") == 2 + 2 + 1 + 1  # 12's second falls due on 03-31
    assert value(db, "installments_due_count", "2026-03-30") == 2 + 1 + 1 + 1
    # one customer with two plans (orders 11 and 14), filtered by customer
    both = Intent(measure="installments_due_count", shape="scalar",
                  filters=[{"dim": "customer", "value": "cF", "exclude": False}])
    assert Compiler(db).value(both, window("2026-03-31")) == 2 + 1


def test_overdue_amount_is_money_of_instalments_due_before_the_day_less_paid():
    db = make_db(debts=None)
    assert value(db, "overdue_amount", "2026-03-31") == 200  # 13 only: 03-15 unpaid; 12's 03-31 is not yet overdue
    assert value(db, "overdue_amount", "2026-04-01") == 100 + 200  # 12's 03-31 falls overdue
    assert value(db, "overdue_amount", "2026-05-01") == 200 + 200 + 400  # 11, 12 and 13 (14, 15, 16 are paid up)


def test_overdue_amount_is_never_negative_when_paid_ahead():
    plans = dict(PLANS)
    plans["13"] = ("2026-02-10", 900, 300, 3, 200, 15, [("2026-02-12", 600)])  # paid in full early
    db = make_db(debts=None, plans=plans)
    assert value(db, "overdue_amount", "2026-05-01") == 200 + 200


def test_overdue_debtor_count_is_customers_not_instalments():
    db = make_db(debts=None)
    assert value(db, "overdue_debtor_count", "2026-03-31") == 1
    assert value(db, "overdue_debtor_count", "2026-04-01") == 2
    assert value(db, "overdue_debtor_count", "2026-05-01") == 3


def test_overdue_measures_ignore_a_cancelled_order():
    orders = [(o, d, "cancelled" if o == "13" else s, c) for o, d, s, c in ORDERS]
    db = make_db(debts=None, orders=orders)
    assert value(db, "overdue_amount", "2026-03-31") == 0
    assert value(db, "overdue_debtor_count", "2026-03-31") == 0


def test_overdue_needs_an_instalment_ledger():
    db = make_db(plans=None)
    with pytest.raises(Unsupported):
        value(db, "overdue_amount", "2026-03-31")
    with pytest.raises(Unsupported):
        value(db, "installment_outstanding", "2026-03-31")


def test_overdue_measures_take_no_age():
    db = make_db(debts=None)
    with pytest.raises(Unsupported):
        value(db, "overdue_amount", "2026-03-31", age_days=7)


def test_obligation_measures_are_declared_for_the_intent_stage():
    for measure in ("installments_due_count", "overdue_amount", "overdue_debtor_count", "installment_outstanding"):
        assert measure in MEASURES and measure in DEBT_MEASURES


def test_catalog_lists_the_derived_tables():
    db = make_db()
    tables = {r[0] for r in db.execute("SELECT DISTINCT table_name FROM column_catalog WHERE derived=1")}
    assert tables == {"obligations", "obligation_events", "obligation_schedule"}
