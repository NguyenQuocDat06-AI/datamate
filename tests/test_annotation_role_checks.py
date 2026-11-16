"""Annotation role checks for order keys and paid_amount (synthetic data only): a small-domain numeric column is no
key to the orders, and a paid column that holds order totals is demoted while the real payment column stays."""
import sqlite3

from bizreason.contracts import BusinessContext, ColumnMeta, SourceRef, TableMeta
from bizreason.semantic import annotation_check, layer

SALES = ["order", "day", "item", "qty", "amount"]
SALES_ROLES = {"order": "order_id", "day": "ordered_at", "item": "product_name", "qty": "qty", "amount": "line_amount"}
N = 40


def _sales_rows(n=N, lines=2, amount=50):
    return [(f"o{i}", f"2024-01-{i % 28 + 1:02d}", f"P{j}", "1", str(amount)) for i in range(n) for j in range(lines)]


def _context(tmp_path, specs):
    source = tmp_path / "input.sqlite"
    db = sqlite3.connect(source)
    for name, (columns, rows) in specs.items():
        db.execute("CREATE TABLE " + name + " (" + ",".join('"' + c + '" TEXT' for c in columns)
                   + ",__source_row INTEGER,__raw_json TEXT)")
        for index, row in enumerate(rows, 1):
            db.execute("INSERT INTO " + name + " VALUES (" + ",".join("?" for _ in range(len(columns) + 2)) + ")",
                       (*row, index, "{}"))
    db.commit()
    db.close()
    return BusinessContext(
        business_id="synthetic", revision="r", database=source, readme="", manifest={}, tables=[
            TableMeta(name=name, sources=[SourceRef(path=name + ".csv")],
                      columns=[ColumnMeta(name=c, original_name=c, dtype="text") for c in columns],
                      row_count=len(rows), raw_row_count=len(rows)) for name, (columns, rows) in specs.items()])


def _check(tmp_path, specs, kinds_roles):
    context = _context(tmp_path, specs)
    dst = sqlite3.connect(":memory:")
    layer._register(dst)
    dst.execute("ATTACH DATABASE ? AS source", (str(context.database),))
    annotated = [(t, {"kind": kinds_roles[t.name][0], "columns": [c.name for c in t.columns],
                      "roles": kinds_roles[t.name][1]}) for t in context.tables]
    result, fixes = annotation_check.check_annotations(dst, annotated)
    return {t.name: note["roles"] for t, note in result}, fixes


def _ledger(price_of=lambda i: 100, prepaid_of=lambda i: 30, columns=("ref", "price", "prepaid")):
    rows = [(f"o{i}", str(price_of(i)), str(prepaid_of(i))) for i in range(N)]
    return {"ledger": (list(columns), rows)}


LEDGER_ROLES = {"ref": "order_id", "price": "paid_amount", "prepaid": "paid_amount"}


def _run(tmp_path, ledger, roles=LEDGER_ROLES, kind="payment"):
    return _check(tmp_path, {"sales": (SALES, _sales_rows()), **ledger},
                  {"sales": ("sales", SALES_ROLES), "ledger": (kind, roles)})


# paid_amount ---------------------------------------------------------------------------------------------------

def test_total_like_paid_column_is_demoted_and_real_payment_stays(tmp_path):
    roles, fixes = _run(tmp_path, _ledger())  # order total is 2 lines x 50 = 100
    assert roles["ledger"]["price"] == "other" and roles["ledger"]["prepaid"] == "paid_amount"
    assert len(fixes) == 1 and fixes[0]["column"] == "price" and fixes[0]["from"] == "paid_amount"
    assert "order totals" in fixes[0]["why"] and "40 of 40" in fixes[0]["why"]


def test_total_above_goods_total_is_still_a_total(tmp_path):
    roles, _ = _run(tmp_path, _ledger(price_of=lambda i: 130))  # goods 100 + fee
    assert roles["ledger"]["price"] == "other" and roles["ledger"]["prepaid"] == "paid_amount"


def test_partial_payments_in_both_columns_are_kept(tmp_path):
    roles, fixes = _run(tmp_path, _ledger(price_of=lambda i: 60, prepaid_of=lambda i: 30))
    assert roles["ledger"] == LEDGER_ROLES and fixes == []


def test_lone_paid_column_equal_to_totals_is_kept(tmp_path):
    ledger = {"ledger": (["ref", "price"], [(f"o{i}", "100") for i in range(N)])}
    roles, fixes = _run(tmp_path, ledger, {"ref": "order_id", "price": "paid_amount"})
    assert roles["ledger"]["price"] == "paid_amount" and fixes == []


def test_both_total_like_columns_are_kept_so_a_payment_column_remains(tmp_path):
    roles, fixes = _run(tmp_path, _ledger(prepaid_of=lambda i: 100))
    assert roles["ledger"] == LEDGER_ROLES and fixes == []


def test_paid_column_is_kept_when_few_rows_join_an_order(tmp_path):
    rows = [(f"x{i}", "100", "30") for i in range(N)]  # none of these refs is an order
    roles, fixes = _run(tmp_path, {"ledger": (["ref", "price", "prepaid"], rows)})
    assert roles["ledger"] == LEDGER_ROLES and fixes == []


def test_paid_column_is_kept_when_only_a_minority_reach_the_total(tmp_path):
    roles, fixes = _run(tmp_path, _ledger(price_of=lambda i: 100 if i % 4 == 0 else 20))
    assert roles["ledger"] == LEDGER_ROLES and fixes == []


def test_paid_column_without_order_id_is_kept(tmp_path):
    roles, fixes = _run(tmp_path, _ledger(), {"ref": "other", "price": "paid_amount", "prepaid": "paid_amount"})
    assert roles["ledger"]["price"] == "paid_amount" and fixes == []


def test_paid_check_needs_a_sales_table(tmp_path):
    roles, fixes = _check(tmp_path, _ledger(), {"ledger": ("payment", LEDGER_ROLES)})
    assert roles["ledger"] == LEDGER_ROLES and fixes == []


# order key -----------------------------------------------------------------------------------------------------

def _numeric_sales():
    return [(str(i), f"2024-01-{i % 28 + 1:02d}", "P", "1", "50") for i in range(1, 201)]


def _periods(rows=400, domain=12):
    return {"plan": (["periods", "x"], [(str(i % domain + 1), "a") for i in range(rows)])}


def test_small_domain_numeric_column_is_not_an_order_key(tmp_path):
    roles, fixes = _check(tmp_path, {"sales": (SALES, _numeric_sales()), **_periods()},
                          {"sales": ("sales", SALES_ROLES), "plan": ("other", {"periods": "order_id", "x": "other"})})
    assert roles["plan"]["periods"] == "other"
    assert fixes[0]["from"] == "order_id" and "not an order key" in fixes[0]["why"]


def test_real_key_with_one_row_per_order_is_kept(tmp_path):
    ledger = {"plan": (["ref", "x"], [(str(i), "a") for i in range(1, 31)])}
    roles, fixes = _check(tmp_path, {"sales": (SALES, _numeric_sales()), **ledger},
                          {"sales": ("sales", SALES_ROLES), "plan": ("other", {"ref": "order_id", "x": "other"})})
    assert roles["plan"]["ref"] == "order_id" and fixes == []


def test_key_repeated_a_few_times_per_order_is_kept(tmp_path):
    rows = [(str(i % 30 + 1), "a") for i in range(60)]  # 30 orders x 2 rows
    roles, fixes = _check(tmp_path, {"sales": (SALES, _numeric_sales()), "plan": (["ref", "x"], rows)},
                          {"sales": ("sales", SALES_ROLES), "plan": ("other", {"ref": "order_id", "x": "other"})})
    assert roles["plan"]["ref"] == "order_id" and fixes == []


def test_payment_order_key_is_never_judged_by_spread(tmp_path):
    rows = [(str(i % 3 + 1), "5") for i in range(300)]  # three orders, many payments
    roles, fixes = _check(tmp_path, {"sales": (SALES, _numeric_sales()), "pay": (["ref", "amt"], rows)},
                          {"sales": ("sales", SALES_ROLES), "pay": ("payment", {"ref": "order_id", "amt": "paid_amount"})})
    assert roles["pay"]["ref"] == "order_id" and fixes == []


# join key (layer catalog hook) ---------------------------------------------------------------------------------

def _layer_db(order_ids, column_values):
    dst = sqlite3.connect(":memory:")
    dst.execute("CREATE TABLE orders (order_id TEXT)")
    dst.executemany("INSERT INTO orders VALUES (?)", [(o,) for o in order_ids])
    dst.execute("CREATE TABLE src_t (c TEXT)")
    dst.executemany("INSERT INTO src_t VALUES (?)", [(v,) for v in column_values])
    return dst


def test_join_key_verdict_rejects_small_domain_numbers():
    dst = _layer_db([str(i) for i in range(1, 300)], [str(i % 12 + 1) for i in range(600)])
    ok, why = annotation_check.join_key_verdict(dst, "src_t", "c", 0.5)
    assert not ok and "not spread like a key" in why


def test_join_key_verdict_keeps_a_real_key():
    dst = _layer_db([str(i) for i in range(1, 300)], [str(i) for i in range(1, 250)])
    assert annotation_check.join_key_verdict(dst, "src_t", "c", 0.5)[0]


def test_join_key_verdict_keeps_small_table_key():
    dst = _layer_db([f"o{i}" for i in range(300)], [f"o{i}" for i in range(8)])
    assert annotation_check.join_key_verdict(dst, "src_t", "c", 0.5)[0]


def test_join_key_verdict_rejects_low_match():
    dst = _layer_db(["a", "b"], ["x", "y", "z", "a"])
    ok, why = annotation_check.join_key_verdict(dst, "src_t", "c", 0.5)
    assert not ok and "1 of 4" in why
