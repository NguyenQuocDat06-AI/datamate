"""Synthetic checks of bizreason.semantic.debt_sources (no dataset names, no model)."""
import sqlite3
from datetime import date

from bizreason.semantic import debt_sources as ds

README_PLAIN = "# Shop\n\nSo ban hang. Cot: Ngay, Ma don, Ten hang, So luong, Thanh tien.\n"


def make(orders_cols="order_id, date, customer_key, customer_name, total_due, paid, outstanding", rows=(),
         readme=README_PLAIN, catalog=(), payments=()):
    db = sqlite3.connect(":memory:")
    db.execute(f"CREATE TABLE orders ({orders_cols})")
    db.execute("CREATE TABLE payments (order_id, date, amount)")
    db.execute("CREATE TABLE column_catalog (table_name, column_name, role)")
    db.execute("CREATE TABLE shop_docs (key, text)")
    db.execute("INSERT INTO shop_docs VALUES ('readme', ?)", (readme,))
    n = len(orders_cols.split(","))
    for row in rows:
        db.execute(f"INSERT INTO orders VALUES ({','.join('?' * n)})", row)
    for role in catalog:
        db.execute("INSERT INTO column_catalog VALUES ('t', 'c', ?)", (role,))
    for p in payments:
        db.execute("INSERT INTO payments VALUES (?,?,?)", p)
    return db


def test_customers_unrecorded_when_nothing_names_a_customer():
    db = make(rows=[("1", "2026-01-01", None, None, None, None, None)])
    assert ds.customers_unrecorded(db)


def test_customers_recorded_on_orders_is_not_unrecorded():
    db = make(rows=[("1", "2026-01-01", "k1", "An", None, None, None)])
    assert not ds.customers_unrecorded(db)


def test_walk_in_name_only_still_counts_as_recorded():
    db = make(rows=[("1", "2026-01-01", None, "Khach le", None, None, None)])
    assert not ds.customers_unrecorded(db)


def test_customer_role_in_another_table_blocks_the_claim():
    db = make(rows=[("1", "2026-01-01", None, None, None, None, None)], catalog=["customer_name"])
    assert not ds.customers_unrecorded(db)


def test_readme_mentioning_customers_blocks_the_claim():
    db = make(rows=[("1", "2026-01-01", None, None, None, None, None)], readme="Cot Khach: ten khach\n")
    assert not ds.customers_unrecorded(db)


def test_no_readme_means_no_claim():
    db = make(rows=[("1", "2026-01-01", None, None, None, None, None)], readme="")
    assert not ds.customers_unrecorded(db)


def test_payments_unrecorded_when_no_money_state_anywhere():
    db = make(rows=[("1", "2026-01-01", "k", "A", None, None, None)])
    assert ds.payments_unrecorded(db)


def test_filled_paid_column_blocks_the_claim():
    db = make(rows=[("1", "2026-01-01", "k", "A", None, 50.0, None)])
    assert not ds.payments_unrecorded(db)


def test_payment_table_rows_block_the_claim():
    db = make(rows=[("1", "2026-01-01", "k", "A", None, None, None)], payments=[("1", "2026-01-02", 5)])
    assert not ds.payments_unrecorded(db)


def test_payment_amount_role_elsewhere_blocks_the_claim():
    db = make(rows=[("1", "2026-01-01", "k", "A", None, None, None)], catalog=["paid_amount"])
    assert not ds.payments_unrecorded(db)


def test_due_date_role_does_not_count_as_money():
    db = make(rows=[("1", "2026-01-01", "k", "A", None, None, None)], catalog=["due_at", "credit_days"])
    assert ds.payments_unrecorded(db)


def test_readme_about_received_money_blocks_the_claim():
    db = make(rows=[("1", "2026-01-01", "k", "A", None, None, None)], readme=README_PLAIN + "Cot: Đã trả\n")
    assert not ds.payments_unrecorded(db)


def test_settlements_from_outstanding():
    db = make(rows=[("1", "2026-01-01", "k", "A", 100.0, None, 30.0), ("2", "2026-01-02", "k", "A", 50.0, None, None)])
    got = ds.order_state_settlements(db)
    assert got == {"1": [("2026-01-01", 70.0)], "2": [("2026-01-02", 50.0)]}


def test_settlements_from_paid_capped_at_due():
    db = make(rows=[("1", "2026-01-01", "k", "A", 100.0, 40.0, None), ("2", "2026-01-02", "k", "A", 50.0, 80.0, None)])
    got = ds.order_state_settlements(db)
    assert got == {"1": [("2026-01-01", 40.0)], "2": [("2026-01-02", 50.0)]}


def test_settlements_refuse_unknown_paid():
    db = make(rows=[("1", "2026-01-01", "k", "A", 100.0, 40.0, None), ("2", "2026-01-02", "k", "A", 50.0, None, None)])
    assert ds.order_state_settlements(db) is None


def test_settlements_need_amount_payable():
    db = make(rows=[("1", "2026-01-01", "k", "A", None, 40.0, None)])
    assert ds.order_state_settlements(db) is None


def test_cutoff_covers_data_end():
    db = make(rows=[("1", "2026-01-01", "k", "A", 1.0, 1.0, None), ("2", "2026-03-10", "k", "A", 1.0, 1.0, None)])
    assert ds.cutoff_covers_data_end(db, date(2026, 3, 11))
    assert ds.cutoff_covers_data_end(db, date(2026, 4, 1))
    assert not ds.cutoff_covers_data_end(db, date(2026, 3, 10))
