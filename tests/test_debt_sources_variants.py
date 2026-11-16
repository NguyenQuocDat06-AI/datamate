"""Variants of how a shop records money received or owed, read by bizreason.semantic.debt_sources.

Synthetic layers only: no dataset names, no model. Every case states what the shop records and what the
payment index (compiler `_payment_index`) should be able to conclude from it."""
import sqlite3
from datetime import date

from bizreason.semantic import debt_sources as ds

README_PLAIN = "# Shop\n\nSo ban hang. Cot: Ngay, Ma don, Ten hang, So luong, Thanh tien.\n"
README_DEBT_TERMS = ("# Shop\n\nCac file: So ban hang, Chi tiet cong no theo han.\n\n"
                     "## Cot trong Chi tiet cong no theo han\n- Ma don\n- So ngay duoc no\n- Han thanh toan\n\n"
                     "## Quy uoc\n- Han thanh toan: chi don ban no co han.\n")


def layer(orders_cols="order_id, date, total_due, paid, outstanding", orders=(), readme=README_PLAIN,
          catalog=(), payments=()):
    """catalog: (table, column, role) triples. orders: tuples matching orders_cols."""
    db = sqlite3.connect(":memory:")
    db.execute(f"CREATE TABLE orders ({orders_cols})")
    db.execute("CREATE TABLE payments (order_id, date, amount)")
    db.execute("CREATE TABLE column_catalog (table_name, column_name, role)")
    db.execute("CREATE TABLE shop_docs (key, text)")
    db.execute("INSERT INTO shop_docs VALUES ('readme', ?)", (readme,))
    width = len(orders_cols.split(","))
    for row in orders:
        db.execute(f"INSERT INTO orders VALUES ({','.join('?' * width)})", row)
    for entry in catalog:
        db.execute("INSERT INTO column_catalog VALUES (?, ?, ?)", entry)
    for payment in payments:
        db.execute("INSERT INTO payments VALUES (?,?,?)", payment)
    return db


def test_terms_table_only_with_debt_named_in_readme_is_unrecorded():
    db = layer(orders=[("a", "2026-01-01", None, None, None)], readme=README_DEBT_TERMS,
               catalog=[("src_terms", "ngay_no", "credit_days"), ("src_terms", "han", "due_at")])
    assert ds.payments_unrecorded(db)
    assert ds.order_state_settlements(db) is None


def test_debt_named_in_readme_without_any_terms_table_is_still_not_claimed():
    db = layer(orders=[("a", "2026-01-01", None, None, None)], readme=README_DEBT_TERMS)
    assert not ds.payments_unrecorded(db)


def test_terms_table_does_not_hide_a_readme_line_about_amounts_owed():
    readme = README_DEBT_TERMS + "- Con no: so tien khach con no cua don.\n"
    db = layer(orders=[("a", "2026-01-01", None, None, None)], readme=readme,
               catalog=[("src_terms", "han", "due_at")])
    assert not ds.payments_unrecorded(db)


def test_terms_table_next_to_an_amount_role_is_not_unrecorded():
    db = layer(orders=[("a", "2026-01-01", None, None, None)], readme=README_DEBT_TERMS,
               catalog=[("src_terms", "han", "due_at"), ("src_other", "tien", "paid_amount")])
    assert not ds.payments_unrecorded(db)


def test_filled_paid_column_on_orders_is_not_unrecorded_even_with_terms():
    db = layer(orders=[("a", "2026-01-01", 100.0, 100.0, None)], readme=README_DEBT_TERMS,
               catalog=[("src_terms", "han", "due_at")])
    assert not ds.payments_unrecorded(db)


def test_payment_table_rows_beat_a_terms_table():
    db = layer(orders=[("a", "2026-01-01", 100.0, None, None)], readme=README_DEBT_TERMS,
               catalog=[("src_terms", "han", "due_at")], payments=[("a", "2026-01-05", 40.0)])
    assert not ds.payments_unrecorded(db)


def test_nothing_recorded_at_all_is_unrecorded():
    db = layer(orders=[("a", "2026-01-01", None, None, None)])
    assert ds.payments_unrecorded(db)


def test_outstanding_column_on_orders_gives_settled_amount_per_order():
    db = layer(orders=[("a", "2026-01-01", 100.0, None, 30.0), ("b", "2026-01-02", 50.0, None, None)])
    settled = ds.order_state_settlements(db)
    assert settled == {"a": [("2026-01-01", 70.0)], "b": [("2026-01-02", 50.0)]}


def test_paid_column_gives_settled_amount_capped_at_the_amount_payable():
    db = layer(orders=[("a", "2026-01-01", 100.0, 120.0, None), ("b", "2026-01-02", 50.0, 20.0, None)])
    settled = ds.order_state_settlements(db)
    assert settled == {"a": [("2026-01-01", 100.0)], "b": [("2026-01-02", 20.0)]}


def test_paid_column_with_a_blank_is_unknown_not_zero():
    db = layer(orders=[("a", "2026-01-01", 100.0, 100.0, None), ("b", "2026-01-02", 50.0, None, None)])
    assert ds.order_state_settlements(db) is None


def test_outstanding_is_the_state_when_both_columns_are_filled():
    # an order with nothing paid and nothing owed (for instance a cancelled one) is not a contradiction
    db = layer(orders=[("a", "2026-01-01", 100.0, 60.0, 40.0), ("b", "2026-01-02", 50.0, 0.0, 0.0)])
    assert ds.order_state_settlements(db) == {"a": [("2026-01-01", 60.0)], "b": [("2026-01-02", 50.0)]}


def test_no_amount_payable_means_no_settlement():
    db = layer(orders=[("a", "2026-01-01", None, 10.0, None)])
    assert ds.order_state_settlements(db) is None


def test_cutoff_must_reach_the_last_day_with_orders():
    db = layer(orders=[("a", "2026-01-10", 100.0, None, 0.0)])
    assert ds.cutoff_covers_data_end(db, date(2026, 1, 11))
    assert not ds.cutoff_covers_data_end(db, date(2026, 1, 10))
