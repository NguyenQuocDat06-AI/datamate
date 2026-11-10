"""Debt measures (rulebook §4), exclusion filters, and the payments table built by the semantic layer.

Expected numbers are worked by hand in the comments next to each fixture row.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from bizreason.contracts import BusinessContext, ColumnMeta, SourceRef, TableMeta, TimeWindow
from bizreason.semantic import layer
from bizreason.semantic.compiler import Answer, Compiler, Intent, Unsupported
from bizreason.utils import normalize_lookup

SCHEMA = """
CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, customer_key TEXT, payment_method TEXT,
                     total_due REAL, revenue REAL);
CREATE TABLE lines (order_id TEXT, date TEXT, status TEXT, product_name TEXT, amount REAL);
CREATE TABLE purchases (purchase_id TEXT, date TEXT, cancelled INTEGER, amount REAL);
CREATE TABLE payments (order_id TEXT, date TEXT, amount REAL, method TEXT);
"""

# order_id, date, status, customer_key, payment_method, total_due, revenue
ORDERS = [
    ("O1", "2026-03-01", "completed", "c1", "ghi no", 100, 100),   # 30 paid same day, 20 on 03-20, 50 on 04-05
    ("O2", "2026-03-10", "completed", "c1", "debt", 50, 50),       # nothing paid
    ("O3", "2026-03-15", "cancelled", "c2", "ghi no", 200, 200),   # cancelled: never counts
    ("O4", "2026-03-20", "returned", "c2", "ghi no", 80, 80),      # returned: still counts; 30 paid on 03-22
    ("O5", "2026-03-05", "completed", "c3", "cod", 300, 300),      # cash on delivery: not customer debt
    ("O6", "2026-03-25", "completed", "c3", "Debt", 60, 60),       # nothing paid
    ("O7", "2026-03-02", "completed", None, "ghi no", 40, 40),     # walk-in: in totals, not in debtor count
    ("O8", "2026-03-03", "completed", "c4", "ghi no", 30, 30),     # paid 40 the same day: overpaid
    ("O9", "2026-04-02", "completed", "c1", "ghi no", 70, 70),     # placed after March
    ("O10", "2026-03-12", "completed", "c5", "san tmdt", 25, 25),  # marketplace sale: not customer debt
]
# order_id, date, amount, method
PAYMENTS = [
    ("O1", "2026-03-01", 30, "cash"),
    ("O1", "2026-03-20", 20, "transfer"),
    ("O1", "2026-04-05", 50, "transfer"),
    ("O4", "2026-03-22", 30, "transfer"),
    ("O5", "2026-03-15", 100, "cod"),
    ("O7", "2026-03-10", 10, "cash"),
    ("O8", "2026-03-03", 40, "cash"),
]
# order_id, date, status, product_name, amount
LINES = [
    ("O1", "2026-03-01", "completed", "tra sua", 100),
    ("O2", "2026-03-10", "completed", "banh mi", 40),
    ("O4", "2026-03-20", "returned", "tra sua", 25),
    ("O2", "2026-03-12", "completed", None, 7),
]

CUTOFF_31_MAR = TimeWindow(start=date(2026, 3, 31), end_exclusive=date(2026, 4, 1))
MARCH = TimeWindow(start=date(2026, 3, 1), end_exclusive=date(2026, 4, 1))
APRIL = TimeWindow(start=date(2026, 4, 1), end_exclusive=date(2026, 5, 1))


def make_db(payments: list[tuple] = PAYMENTS) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.create_function("lookup", 1, lambda v: None if v is None else normalize_lookup(v))
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", ORDERS)
    db.executemany("INSERT INTO lines VALUES (?,?,?,?,?)", LINES)
    db.executemany("INSERT INTO payments VALUES (?,?,?,?)", payments)
    return db


@pytest.fixture
def compiler() -> Compiler:
    return Compiler(make_db())


def debt(measure: str, window: TimeWindow, compiler: Compiler, filters: list[dict] | None = None) -> float:
    return compiler.value(Intent(measure=measure, filters=filters or []), window)


def test_outstanding_at_cutoff_ignores_later_payments(compiler: Compiler) -> None:
    # Remaining at 31 Mar: O1 100-30-20=50, O2 50, O4 80-30=50, O6 60, O7 40-10=30, O8 max(30-40,0)=0.
    # The 50 paid on 5 Apr is ignored, O9 (2 Apr) is ignored, cancelled O3 and COD O5 are ignored.
    assert debt("debt_outstanding", CUTOFF_31_MAR, compiler) == 240


def test_cancelled_order_is_excluded(compiler: Compiler) -> None:
    # Customer c2 has O3 (cancelled, 200) and O4 (returned, 80 less 30 paid) -> 50 only.
    assert debt("debt_outstanding", CUTOFF_31_MAR, compiler, [{"dim": "customer", "value": "c2"}]) == 50


def test_returned_order_still_counts(compiler: Compiler) -> None:
    # O4 is returned and still owes 50, so it is in the c2 figure above and in the debtor count below.
    assert debt("debtor_count", CUTOFF_31_MAR, compiler) == 3


def test_cod_and_marketplace_orders_are_excluded(compiler: Compiler) -> None:
    # c3 has O5 (COD, 300, unpaid) and O6 (debt, 60). Only O6 counts.
    assert debt("debt_outstanding", CUTOFF_31_MAR, compiler, [{"dim": "customer", "value": "c3"}]) == 60
    # c5 has only the marketplace order O10 (25), which is not customer debt: nothing owed.
    assert debt("debt_outstanding", CUTOFF_31_MAR, compiler, [{"dim": "customer", "value": "c5"}]) == 0


def test_overpayment_does_not_go_negative(compiler: Compiler) -> None:
    # c4 owes 30 and paid 40: remaining 0, not -10. The total 240 would be 230 if it went negative.
    assert debt("debt_outstanding", CUTOFF_31_MAR, compiler, [{"dim": "customer", "value": "c4"}]) == 0


def test_debtor_count_uses_distinct_customers_with_positive_balance(compiler: Compiler) -> None:
    # c1 (100), c2 (50), c3 (60) owe money. c4 is at 0 and the walk-in order O7 has no customer.
    assert debt("debtor_count", CUTOFF_31_MAR, compiler) == 3
    assert debt("debtor_count", CUTOFF_31_MAR, compiler, [{"dim": "customer", "value": "c1"}]) == 1


def test_debt_repaid_ignores_same_day_payment_and_counts_earlier_orders() -> None:
    compiler = Compiler(make_db())
    # March: O1 20 (paid 20 on 20 Mar; the 30 paid on the order day is ignored),
    # O4 30 (paid 22 Mar), O7 10 (paid 10 Mar). O8 is paid the same day, COD O5 is not debt. Total 60.
    assert debt("debt_repaid", MARCH, compiler) == 60
    # April: the 50 paid on 5 Apr on O1, an order from March, is repaid in this period.
    assert debt("debt_repaid", APRIL, compiler) == 50


def test_debt_new_subtracts_same_day_payment(compiler: Compiler) -> None:
    # March orders: O1 100-30=70, O2 50, O4 80, O6 60, O7 40, O8 max(30-40,0)=0. COD O5, marketplace O10 and
    # cancelled O3 are excluded, and O9 is in April. Total 300.
    assert debt("debt_new", MARCH, compiler) == 300
    assert debt("debt_new", MARCH, compiler, [{"dim": "customer", "value": "c1"}]) == 120


def test_customer_filter_applies_to_outstanding_and_exclusion(compiler: Compiler) -> None:
    assert debt("debt_outstanding", CUTOFF_31_MAR, compiler, [{"dim": "customer", "value": "c1"}]) == 100
    assert debt("debt_repaid", MARCH, compiler, [{"dim": "customer", "value": "c1"}]) == 20
    # Excluding c1 keeps the walk-in order O7 (30 unpaid): 50 + 60 + 0 + 30 = 140.
    excluded = [{"dim": "customer", "value": "c1", "exclude": True}]
    assert debt("debt_outstanding", CUTOFF_31_MAR, compiler, excluded) == 140


def test_outstanding_answer_is_a_number(compiler: Compiler) -> None:
    answer = compiler.answer(Intent(measure="debt_outstanding"), [CUTOFF_31_MAR])
    assert answer == Answer("number", 240)


def test_no_payments_raises_unsupported() -> None:
    compiler = Compiler(make_db(payments=[]))
    with pytest.raises(Unsupported):
        debt("debt_outstanding", CUTOFF_31_MAR, compiler)


def test_other_filter_on_debt_measure_raises_unsupported(compiler: Compiler) -> None:
    with pytest.raises(Unsupported):
        debt("debt_outstanding", CUTOFF_31_MAR, compiler, [{"dim": "product", "value": "tra sua"}])


def test_excluded_product_filter_keeps_other_products_and_null_products(compiler: Compiler) -> None:
    included = [{"dim": "product", "value": "tra sua"}]
    # Only the O1 line of tra sua is valid revenue; the returned O4 line is not.
    assert compiler.value(Intent(measure="revenue", filters=included), MARCH) == 100
    excluded = [{"dim": "product", "value": "tra sua", "exclude": True}]
    # Keeps banh mi (40) and the line with no product name (7): 47.
    assert compiler.value(Intent(measure="revenue", filters=excluded), MARCH) == 47


def test_layer_build_creates_payments_table(tmp_path) -> None:
    source = tmp_path / "source.sqlite"
    con = sqlite3.connect(source)
    con.execute("CREATE TABLE receipts (receipt_no TEXT, ref TEXT, when_paid TEXT, amount_paid REAL, how TEXT)")
    con.executemany(
        "INSERT INTO receipts VALUES (?,?,?,?,?)",
        [("R1", "O1", "2026-03-01", 30.0, "cash"), ("R2", "O1", "2026-03-20", 20.0, "transfer")],
    )
    con.commit()
    con.close()
    names = ["receipt_no", "ref", "when_paid", "amount_paid", "how"]
    context = BusinessContext(
        business_id="test", revision="r1", database=source, readme="", manifest={},
        tables=[TableMeta(
            name="receipts", sources=[SourceRef(path="receipts.csv")],
            columns=[ColumnMeta(name=n, original_name=n, dtype="text") for n in names],
            row_count=2, raw_row_count=2,
        )],
    )
    records = [{
        "columns": names, "kind": "payment",
        "roles": {"receipt_no": "payment_id", "ref": "order_id", "when_paid": "paid_at",
                  "amount_paid": "paid_amount", "how": "payment_method"},
    }]
    out = tmp_path / "canonical.sqlite"
    report = layer.build(context, records, out)
    assert report["payments"] == 2
    con = sqlite3.connect(out)
    rows = con.execute("SELECT order_id, date, amount, method FROM payments ORDER BY date").fetchall()
    con.close()
    assert rows == [("O1", "2026-03-01", 30.0, "cash"), ("O1", "2026-03-20", 20.0, "transfer")]


def test_layer_build_creates_empty_payments_table_without_payment_source(tmp_path) -> None:
    context = BusinessContext(
        business_id="test", revision="r1", database=tmp_path / "empty.sqlite", readme="", tables=[], manifest={},
    )
    sqlite3.connect(tmp_path / "empty.sqlite").close()
    out = tmp_path / "canonical.sqlite"
    report = layer.build(context, [], out)
    assert report["payments"] == 0
    con = sqlite3.connect(out)
    columns = [row[1] for row in con.execute("PRAGMA table_info(payments)")]
    con.close()
    assert columns == ["order_id", "date", "amount", "method"]


# Debt age (rulebook §4). Window 1-29 Dec 2026, so the cutoff day (last day included) is 28 Dec.
AGED_ORDERS = [
    # order_id, date, status, customer_key, payment_method, total_due, revenue
    ("D1", "2026-12-21", "completed", "c1", "ghi no", 100, 100),  # age 7 on the cutoff day
    ("D2", "2026-12-20", "completed", "c2", "ghi no", 50, 50),    # age 8
    ("D3", "2026-12-28", "completed", "c3", "ghi no", 30, 30),    # age 0
]
# order_id, date, amount, method. D1 paid 10 and D2 paid 20 before the cutoff; D3 is unpaid.
AGED_PAYMENTS = [
    ("D1", "2026-12-22", 10, "cash"),
    ("D2", "2026-12-22", 20, "transfer"),
]
AGED_WINDOW = TimeWindow(start=date(2026, 12, 1), end_exclusive=date(2026, 12, 29))


def make_aged_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.create_function("lookup", 1, lambda v: None if v is None else normalize_lookup(v))
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", AGED_ORDERS)
    db.executemany("INSERT INTO payments VALUES (?,?,?,?)", AGED_PAYMENTS)
    return db


def aged(measure: str, age_days: int | None, compiler: Compiler | None = None) -> float:
    compiler = compiler or Compiler(make_aged_db())
    return compiler.value(Intent(measure=measure, age_days=age_days), AGED_WINDOW)


def test_debt_age_none_keeps_every_order() -> None:
    # D1 100-10=90, D2 50-20=30, D3 30. Total 150; three debtors.
    assert aged("debt_outstanding", None) == 150
    assert aged("debtor_count", None) == 3


def test_debt_age_boundary_excludes_order_exactly_that_old() -> None:
    # Cutoff 28 Dec, age 7: the cutoff minus 7 is 21 Dec. D1 is exactly 7 days old, so it is left out.
    # D2 is 8 days old, so it stays: 50-20=30. D3 is 0 days old and is left out.
    assert aged("debt_outstanding", 7) == 30
    assert aged("debtor_count", 7) == 1


def test_debt_age_eight_excludes_the_eight_day_order_too() -> None:
    # Age 8 cuts at 20 Dec. D2 is exactly 8 days old, so nothing is older than 8 days.
    assert aged("debt_outstanding", 8) == 0
    assert aged("debtor_count", 8) == 0


def test_debt_age_zero_excludes_orders_from_the_cutoff_day() -> None:
    # Age 0 cuts at 28 Dec. D3 is placed on the cutoff day, so it is left out: D1 90 + D2 30 = 120.
    assert aged("debt_outstanding", 0) == 120
    assert aged("debtor_count", 0) == 2


def test_debt_age_keeps_payments_up_to_the_window_end() -> None:
    # Age does not change which payments count: D2 keeps its 20 payment, which falls before the cutoff.
    compiler = Compiler(make_aged_db())
    assert compiler.value(Intent(measure="debt_outstanding", age_days=7, filters=[
        {"dim": "customer", "value": "c2"},
    ]), AGED_WINDOW) == 30


@pytest.mark.parametrize("measure", ["debt_repaid", "debt_new", "revenue", "order_count"])
def test_age_on_other_measures_raises_unsupported(measure: str) -> None:
    compiler = Compiler(make_aged_db())
    with pytest.raises(Unsupported, match="age on measure"):
        compiler.value(Intent(measure=measure, age_days=7), AGED_WINDOW)


def test_age_on_threshold_shape_raises_unsupported() -> None:
    compiler = Compiler(make_aged_db())
    intent = Intent(
        measure="debtor_count", shape="threshold_count", age_days=7,
        threshold={"subject": "customer", "metric": "order_count", "op": "gte", "value": 1},
    )
    with pytest.raises(Unsupported, match="age on shape"):
        compiler.answer(intent, [AGED_WINDOW])


@pytest.mark.parametrize("bad", [-1, 2.5, True, "7"])
def test_age_must_be_a_non_negative_whole_number(bad) -> None:
    compiler = Compiler(make_aged_db())
    with pytest.raises(Unsupported, match="whole number"):
        compiler.value(Intent(measure="debt_outstanding", age_days=bad), AGED_WINDOW)


def test_answer_with_age_is_a_number() -> None:
    compiler = Compiler(make_aged_db())
    assert compiler.answer(Intent(measure="debt_outstanding", age_days=7), [AGED_WINDOW]) == Answer("number", 30)
