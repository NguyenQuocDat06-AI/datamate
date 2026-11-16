"""Layer: multi-level unit conversions written as "<number> <unit>", and stock by lot (synthetic data only)."""
import sqlite3

from bizreason.contracts import BusinessContext, ColumnMeta, SourceRef, TableMeta
from bizreason.semantic import layer


def _build(tmp_path, conversions, sales):
    source = tmp_path / "input.sqlite"
    sales_cols = ["order", "day", "item", "unit", "qty", "status", "amount"]
    conv_cols = ["item", "large", "factor", "base"]
    specs = {"sales": (sales_cols, sales), "packing": (conv_cols, conversions)}
    db = sqlite3.connect(source)
    for name, (columns, rows) in specs.items():
        db.execute("CREATE TABLE " + name + " (" + ",".join('"' + c + '" TEXT' for c in columns)
                   + ",__source_row INTEGER,__raw_json TEXT)")
        for index, row in enumerate(rows, 1):
            db.execute("INSERT INTO " + name + " VALUES (" + ",".join("?" for _ in range(len(columns) + 2)) + ")",
                       (*row, index, "{}"))
    db.commit()
    db.close()
    context = BusinessContext(
        business_id="synthetic", revision="r", database=source, readme="", manifest={}, tables=[
            TableMeta(name=name, sources=[SourceRef(path=name + ".csv")],
                      columns=[ColumnMeta(name=c, original_name=c, dtype="text") for c in columns],
                      row_count=len(rows), raw_row_count=len(rows)) for name, (columns, rows) in specs.items()])
    records = [
        {"columns": sales_cols, "kind": "sales", "roles": {
            "order": "order_id", "day": "ordered_at", "item": "product_name", "unit": "unit", "qty": "qty",
            "status": "order_status", "amount": "line_amount"}},
        {"columns": conv_cols, "kind": "unit_conversion", "roles": {
            "item": "product_name", "large": "large_unit", "base": "base_unit", "factor": "conversion_factor"}}]
    target = tmp_path / "canonical.sqlite"
    layer.build(context, records, target)
    return sqlite3.connect(target)


def _qty_base(db, product, unit):
    return db.execute("SELECT qty_base FROM lines WHERE product_name=? AND unit=?", [product, unit]).fetchone()[0]


def _sales(*items):
    return [(f"o{i}", "2024-01-12", item, unit, "2", "completed", "10") for i, (item, unit) in enumerate(items, 1)]


def test_large_unit_counted_in_a_middle_unit_multiplies_through(tmp_path):
    conv = [("Prod A", "strip", "4", "piece"), ("Prod A", "box", "25 strip", "piece")]
    with _build(tmp_path, conv, _sales(("Prod A", "box"), ("Prod A", "strip"), ("Prod A", "piece"))) as db:
        assert _qty_base(db, "Prod A", "box") == 2 * 25 * 4
        assert _qty_base(db, "Prod A", "strip") == 2 * 4
        assert _qty_base(db, "Prod A", "piece") == 2


def test_three_levels_and_row_order_does_not_matter(tmp_path):
    conv = [("Prod A", "case", "3 box", "piece"), ("Prod A", "box", "5 strip", "piece"),
            ("Prod A", "strip", "4", "piece")]
    with _build(tmp_path, conv, _sales(("Prod A", "case"))) as db:
        assert _qty_base(db, "Prod A", "case") == 2 * 3 * 5 * 4


def test_unit_named_is_the_base_unit_keeps_the_number(tmp_path):
    conv = [("Prod A", "box", "10 piece", "piece")]
    with _build(tmp_path, conv, _sales(("Prod A", "box"))) as db:
        assert _qty_base(db, "Prod A", "box") == 20


def test_number_alone_is_unchanged_counterexample(tmp_path):
    conv = [("Prod A", "box", "12", "piece"), ("Prod B", "pack", 6, "piece")]
    with _build(tmp_path, conv, _sales(("Prod A", "box"), ("Prod B", "pack"))) as db:
        assert _qty_base(db, "Prod A", "box") == 24
        assert _qty_base(db, "Prod B", "pack") == 12


def test_unit_that_is_another_products_unit_is_not_borrowed(tmp_path):
    conv = [("Prod A", "strip", "4", "piece"), ("Prod B", "box", "25 strip", "piece")]
    with _build(tmp_path, conv, _sales(("Prod B", "box"))) as db:
        assert _qty_base(db, "Prod B", "box") is None  # no guess: strip is not a unit of Prod B


def test_loop_gives_no_factor(tmp_path):
    conv = [("Prod A", "box", "2 case", "piece"), ("Prod A", "case", "3 box", "piece")]
    with _build(tmp_path, conv, _sales(("Prod A", "box"), ("Prod A", "case"))) as db:
        assert _qty_base(db, "Prod A", "box") is None
        assert _qty_base(db, "Prod A", "case") is None


def test_counts_do_not_change(tmp_path):
    conv = [("Prod A", "strip", "4", "piece"), ("Prod A", "box", "25 strip", "piece")]
    with _build(tmp_path, conv, _sales(("Prod A", "box"), ("Prod A", "strip"))) as db:
        assert db.execute("SELECT COUNT(*), COUNT(DISTINCT order_id), SUM(amount) FROM lines").fetchone() \
            == (2, 2, 20)


# ---- stock by lot: a table of sales from lots that the annotation took for sale lines ----

def _build_lots(tmp_path, lot_sales, *, sold_role="qty_base", kind="sales", lot_ids=None, extra_orders=()):
    source = tmp_path / "input.sqlite"
    specs = {
        "sales": (["order", "day", "pid", "item", "unit", "qty", "status", "amount"],
                  [("o1", "2024-01-10", "p1", "Item One", "piece", "5", "completed", "50"),
                   ("o2", "2024-01-11", "p1", "Item One", "piece", "4", "completed", "40"),
                   ("o3", "2024-01-12", "p2", "Item Two", "piece", "1", "cancelled", "10"), *extra_orders]),
        "catalog": (["pid", "name", "unit"], [("p1", "Item One", "piece"), ("p2", "Item Two", "piece")]),
        "lots": (["lot", "pid", "expiry", "qty"],
                 [("L1", "p1", "2024-06-01", "6"), ("L2", "p1", "2024-09-01", "10"), ("L3", "p2", "2024-09-01", "7")]),
        "lotsales": (["ord", "line", "lot", "sold", "back"][:len(lot_sales[0])], lot_sales),
    }
    db = sqlite3.connect(source)
    for name, (columns, rows) in specs.items():
        db.execute("CREATE TABLE " + name + " (" + ",".join('"' + c + '" TEXT' for c in columns)
                   + ",__source_row INTEGER,__raw_json TEXT)")
        for index, row in enumerate(rows, 1):
            db.execute("INSERT INTO " + name + " VALUES (" + ",".join("?" for _ in range(len(columns) + 2)) + ")",
                       (*row, index, "{}"))
    db.commit()
    db.close()
    context = BusinessContext(
        business_id="synthetic", revision="r", database=source, readme="", manifest={}, tables=[
            TableMeta(name=name, sources=[SourceRef(path=name + ".csv")],
                      columns=[ColumnMeta(name=c, original_name=c, dtype="text") for c in columns],
                      row_count=len(rows), raw_row_count=len(rows)) for name, (columns, rows) in specs.items()])
    records = [
        {"columns": specs["sales"][0], "kind": "sales", "roles": {
            "order": "order_id", "day": "ordered_at", "pid": "product_id", "item": "product_name", "unit": "unit",
            "qty": "qty", "status": "order_status", "amount": "line_amount"}},
        {"columns": specs["catalog"][0], "kind": "product_catalog", "roles": {
            "pid": "product_id", "name": "product_name", "unit": "base_unit"}},
        {"columns": specs["lots"][0], "kind": "lot", "roles": {
            "lot": "lot_id", "pid": "product_id", "expiry": "expires_at", "qty": "qty_purchased_base"}},
        {"columns": specs["lotsales"][0], "kind": kind, "roles": {"ord": "order_id", "sold": sold_role}}]
    target = tmp_path / "canonical.sqlite"
    layer.build(context, records, target)
    return sqlite3.connect(target)


LOT_SALES = [("o1", "1", "L1", "5", "0"), ("o2", "1", "L1", "1", "0"), ("o2", "2", "L2", "3", "1"),
             ("o3", "1", "L3", "1", "0")]


def test_lot_sales_table_builds_balances_and_adds_no_sale_lines(tmp_path):
    with _build_lots(tmp_path, LOT_SALES) as db:
        balances = dict(db.execute("SELECT lot_id, balance FROM lot_balance"))
        # received - sold (orders not cancelled) + returned
        assert balances == {"L1": 6 - 5 - 1, "L2": 10 - 3 + 1, "L3": 7}
        assert db.execute("SELECT COUNT(*), COUNT(DISTINCT order_id), SUM(amount) FROM lines").fetchone() == (3, 3, 100)
        assert db.execute("SELECT COUNT(*) FROM raw_lines").fetchone()[0] == 3


def test_lot_sales_with_no_return_column(tmp_path):
    rows = [(o, line, lot, sold) for o, line, lot, sold, _ in LOT_SALES]
    with _build_lots(tmp_path, rows) as db:
        assert dict(db.execute("SELECT lot_id, balance FROM lot_balance")) == {"L1": 0, "L2": 7, "L3": 7}


def test_table_whose_values_are_not_lot_ids_is_left_as_annotated(tmp_path):
    rows = [(o, line, "X" + lot, sold, back) for o, line, lot, sold, back in LOT_SALES]
    with _build_lots(tmp_path, rows) as db:
        assert db.execute("SELECT COUNT(*) FROM lot_balance").fetchone()[0] == 0


def test_two_quantities_where_neither_is_a_return_is_not_guessed(tmp_path):
    rows = [(o, line, lot, sold, "9") for o, line, lot, sold, _ in LOT_SALES]  # second quantity exceeds the first
    with _build_lots(tmp_path, rows) as db:
        assert db.execute("SELECT COUNT(*) FROM lot_balance").fetchone()[0] == 0


# ---- stock movement ledger with a running balance ----

def _build_ledger(tmp_path, ledger):
    source = tmp_path / "input.sqlite"
    specs = {
        "sales": (["order", "day", "pid", "item", "unit", "qty", "status", "amount"],
                  [("o1", "2024-01-10", "p1", "Item One", "piece", "5", "completed", "50")]),
        "catalog": (["pid", "name", "unit"], [("p1", "Item One", "piece"), ("p2", "Item Two", "piece")]),
        "moves": (["pid", "at", "kind", "change", "after"], ledger),
    }
    db = sqlite3.connect(source)
    for name, (columns, rows) in specs.items():
        db.execute("CREATE TABLE " + name + " (" + ",".join('"' + c + '" TEXT' for c in columns)
                   + ",__source_row INTEGER,__raw_json TEXT)")
        for index, row in enumerate(rows, 1):
            db.execute("INSERT INTO " + name + " VALUES (" + ",".join("?" for _ in range(len(columns) + 2)) + ")",
                       (*row, index, "{}"))
    db.commit()
    db.close()
    context = BusinessContext(
        business_id="synthetic", revision="r", database=source, readme="", manifest={}, tables=[
            TableMeta(name=name, sources=[SourceRef(path=name + ".csv")],
                      columns=[ColumnMeta(name=c, original_name=c, dtype="text") for c in columns],
                      row_count=len(rows), raw_row_count=len(rows)) for name, (columns, rows) in specs.items()])
    records = [
        {"columns": specs["sales"][0], "kind": "sales", "roles": {
            "order": "order_id", "day": "ordered_at", "pid": "product_id", "item": "product_name", "unit": "unit",
            "qty": "qty", "status": "order_status", "amount": "line_amount"}},
        {"columns": specs["catalog"][0], "kind": "product_catalog", "roles": {
            "pid": "product_id", "name": "product_name", "unit": "base_unit"}},
        {"columns": specs["moves"][0], "kind": "inventory", "roles": {
            "pid": "product_id", "change": "inventory_qty", "after": "inventory_qty"}}]
    target = tmp_path / "canonical.sqlite"
    layer.build(context, records, target)
    return sqlite3.connect(target)


LEDGER = [("p1", "2024-01-01 08:00:00", "in", "20", "20"), ("p1", "2024-01-05 09:00:00", "out", "5", "15"),
          ("p2", "2024-01-03 10:00:00", "in", "8", "8"), ("p1", "2024-01-10 11:00:00", "out", "7", "8"),
          ("p2", "2024-01-04 10:00:00", "out", "8", "0")]


def test_ledger_with_running_balance_gives_history_and_stock(tmp_path):
    with _build_ledger(tmp_path, LEDGER) as db:
        assert db.execute("SELECT product_name, ts, balance FROM stock_ledger ORDER BY product_name, ts").fetchall() == [
            ("Item One", "2024-01-01 08:00:00", 20), ("Item One", "2024-01-05 09:00:00", 15),
            ("Item One", "2024-01-10 11:00:00", 8), ("Item Two", "2024-01-03 10:00:00", 8),
            ("Item Two", "2024-01-04 10:00:00", 0)]
        assert dict(db.execute("SELECT product_name, qty_base FROM stock")) == {"Item One": 8, "Item Two": 0}


def test_ledger_whose_balance_does_not_follow_the_movements_is_not_read(tmp_path):
    broken = [(p, t, k, c, "99" if i % 2 else a) for i, (p, t, k, c, a) in enumerate(LEDGER)]
    with _build_ledger(tmp_path, broken) as db:
        assert db.execute("SELECT COUNT(*) FROM stock_ledger").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM stock").fetchone()[0] == 0


def test_ledger_with_a_product_the_catalog_does_not_name_is_not_built(tmp_path):
    with _build_ledger(tmp_path, [*LEDGER, ("p9", "2024-01-02 08:00:00", "in", "3", "3")]) as db:
        assert db.execute("SELECT COUNT(*) FROM stock_ledger").fetchone()[0] == 0


def test_unit_word_never_used_as_packing_keeps_the_number_counterexample(tmp_path):
    # The base unit column is blank, so "can" is not known as the base; it is no packing unit of any row either:
    # the number counts base units, as it did before units were read from the cell.
    conv = [("Prod C", "crate", "24 can", None)]
    with _build(tmp_path, conv, _sales(("Prod C", "crate"))) as db:
        assert _qty_base(db, "Prod C", "crate") == 48
