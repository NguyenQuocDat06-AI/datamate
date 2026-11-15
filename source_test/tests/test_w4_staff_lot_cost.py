"""Layer: staff names from the codes orders write, and the purchase cost of each lot (synthetic data only)."""
import sqlite3

from bizreason.contracts import BusinessContext, ColumnMeta, SourceRef, TableMeta
from bizreason.semantic import layer


def _build(tmp_path, specs, records):
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
    context = BusinessContext(
        business_id="synthetic", revision="r", database=source, readme="", manifest={}, tables=[
            TableMeta(name=name, sources=[SourceRef(path=name + ".csv")],
                      columns=[ColumnMeta(name=c, original_name=c, dtype="text") for c in columns],
                      row_count=len(rows), raw_row_count=len(rows)) for name, (columns, rows) in specs.items()])
    target = tmp_path / "canonical.sqlite"
    layer.build(context, records, target)
    return sqlite3.connect(target)


# ---- staff written as codes ----

ORDER_COLS = ["order", "day", "who", "status", "amount"]
ORDER_ROLES = {"order": "order_id", "day": "ordered_at", "who": "employee_id", "status": "order_status",
               "amount": "line_amount"}


def _staff(tmp_path, who_values, staff_rows):
    orders = [(f"o{i}", "2024-01-10", who, "completed", "10") for i, who in enumerate(who_values, 1)]
    specs = {"orders": (ORDER_COLS, orders), "staff": (["code", "full"], staff_rows)}
    records = [{"columns": ORDER_COLS, "kind": "sales", "roles": ORDER_ROLES},
               {"columns": ["code", "full"], "kind": "employee_catalog",
                "roles": {"code": "employee_id", "full": "employee_name"}}]
    return _build(tmp_path, specs, records)


def _by_employee(db):
    return dict(db.execute("SELECT employee, COUNT(*) FROM orders GROUP BY employee"))


def test_codes_on_orders_stay_the_employee_and_the_staff_names_are_kept_beside_them(tmp_path):
    # Rulebook §8: a file with codes goes by the codes, so answers and rankings name the code; the staff list's full
    # name is kept in employee_raw, where a filter naming the person finds the code.
    with _staff(tmp_path, ["E01", "E01", "E02", "E03"],
                [("E01", "Tran Van An"), ("E02", "Le Thi Binh"), ("E03", "Pham Quoc Cuong")]) as db:
        assert _by_employee(db) == {"E01": 2, "E02": 1, "E03": 1}
        assert dict(db.execute("SELECT employee, employee_raw FROM orders")) == {
            "E01": "Tran Van An", "E02": "Le Thi Binh", "E03": "Pham Quoc Cuong"}
        from bizreason.semantic.compiler import Compiler
        sql, args, _ = Compiler(db).resolve_filter("employee", "Le Thi Binh")
        assert args == ["E02"]


def test_code_missing_from_the_list_leaves_codes_alone_when_too_many_unknown(tmp_path):
    with _staff(tmp_path, ["E01", "E02", "E09", "E08"], [("E01", "Tran Van An"), ("E02", "Le Thi Binh")]) as db:
        assert _by_employee(db) == {"E01": 1, "E02": 1, "E09": 1, "E08": 1}


def test_code_with_two_names_is_not_mapped(tmp_path):
    with _staff(tmp_path, ["E01", "E02"], [("E01", "Tran Van An"), ("E01", "Tran Van Binh"), ("E02", "Le Thi Binh")]) as db:
        assert _by_employee(db) == {"E01": 1, "E02": 1}


def test_two_codes_sharing_a_name_are_not_merged(tmp_path):
    with _staff(tmp_path, ["E01", "E02"], [("E01", "Tran Van An"), ("E02", "Tran Van An")]) as db:
        assert _by_employee(db) == {"E01": 1, "E02": 1}


def test_orders_that_write_names_keep_the_spelling_match(tmp_path):
    with _staff(tmp_path, ["An", "An", "Binh"], [("E01", "Tran Van An"), ("E02", "Le Thi Binh")]) as db:
        assert _by_employee(db) == {"Tran Van An": 2, "Le Thi Binh": 1}


def test_list_without_codes_keeps_full_names_unchanged(tmp_path):
    specs = {"orders": (ORDER_COLS, [("o1", "2024-01-10", "Tran Van An", "completed", "10")]),
             "staff": (["full"], [("Tran Van An",)])}
    records = [{"columns": ORDER_COLS, "kind": "sales", "roles": ORDER_ROLES},
               {"columns": ["full"], "kind": "employee_catalog", "roles": {"full": "employee_name"}}]
    with _build(tmp_path, specs, records) as db:
        assert _by_employee(db) == {"Tran Van An": 1}


# ---- the purchase cost of each lot ----

SALES_COLS = ["order", "day", "pid", "item", "unit", "qty", "status", "amount"]
SALES = [("o1", "2024-01-10", "p1", "Item One", "piece", "5", "completed", "50"),
         ("o2", "2024-01-11", "p2", "Item Two", "piece", "1", "completed", "10")]
LOTSALE_COLS = ["ord", "line", "lot", "sold"]
LOTSALES = [("o1", "1", "L1", "5"), ("o2", "1", "L3", "1")]
SALES_ROLES = {"order": "order_id", "day": "ordered_at", "pid": "product_id", "item": "product_name",
               "unit": "unit", "qty": "qty", "status": "order_status", "amount": "line_amount"}


def _lots(tmp_path, lot_cols, lot_rows, lot_roles, extra=None):
    specs = {"sales": (SALES_COLS, SALES), "catalog": (["pid", "name", "unit"], [("p1", "Item One", "piece"),
                                                                              ("p2", "Item Two", "piece")]),
             "lots": (lot_cols, lot_rows), "lotsales": (LOTSALE_COLS, LOTSALES)}
    records = [
        {"columns": SALES_COLS, "kind": "sales", "roles": SALES_ROLES},
        {"columns": ["pid", "name", "unit"], "kind": "product_catalog",
         "roles": {"pid": "product_id", "name": "product_name", "unit": "base_unit"}},
        {"columns": lot_cols, "kind": "lot", "roles": lot_roles},
        {"columns": LOTSALE_COLS, "kind": "sales", "roles": {"ord": "order_id", "sold": "qty_base"}}]
    if extra:
        specs.update(extra[0])
        records += extra[1]
    return _build(tmp_path, specs, records)


BASE_ROLES = {"lot": "lot_id", "pid": "product_id", "expiry": "expires_at", "qty": "qty_purchased_base",
              "cost": "purchase_unit_cost"}


def _costs(db):
    return dict(db.execute("SELECT lot_id, unit_cost FROM lot_balance"))


def test_lot_cost_from_a_lot_table_with_a_base_quantity(tmp_path):
    rows = [("L1", "p1", "2024-06-01", "6", "1100"), ("L2", "p1", "2024-09-01", "10", "1000"),
            ("L3", "p2", "2024-09-01", "7", "2500")]
    with _lots(tmp_path, ["lot", "pid", "expiry", "qty", "cost"], rows, BASE_ROLES) as db:
        assert _costs(db) == {"L1": 1100, "L2": 1000, "L3": 2500}


def test_lot_without_a_cost_column_has_no_cost(tmp_path):
    rows = [("L1", "p1", "2024-06-01", "6"), ("L2", "p1", "2024-09-01", "10"), ("L3", "p2", "2024-09-01", "7")]
    roles = {k: v for k, v in BASE_ROLES.items() if k != "cost"}
    with _lots(tmp_path, ["lot", "pid", "expiry", "qty"], rows, roles) as db:
        assert _costs(db) == {"L1": None, "L2": None, "L3": None}


def test_cost_on_a_purchase_line_is_divided_by_the_units_factor(tmp_path):
    lines = [("L1", "p1", "2024-06-01", "1", "box", "6000"), ("L2", "p1", "2024-09-01", "2", "box", "6000"),
             ("L3", "p2", "2024-09-01", "7", "piece", "250")]
    cols = ["lot", "pid", "expiry", "qty", "unit", "cost"]
    roles = {"lot": "lot_id", "pid": "product_id", "expiry": "expires_at", "qty": "qty_purchased",
             "unit": "unit", "cost": "purchase_unit_cost"}
    packing = ({"packing": (["pid", "large", "factor"], [("p1", "box", "6")])},
               [{"columns": ["pid", "large", "factor"], "kind": "unit_conversion",
                 "roles": {"pid": "product_id", "large": "large_unit", "factor": "conversion_factor"}}])
    with _lots(tmp_path, cols, lines, roles, packing) as db:
        costs = _costs(db)
    assert costs["L1"] == 1000 and costs["L2"] == 1000  # per base unit
    assert costs["L3"] == 250  # written in the base unit: unchanged


def test_cost_in_a_unit_with_no_known_factor_is_null(tmp_path):
    lines = [("L1", "p1", "2024-06-01", "1", "box", "6000"), ("L2", "p1", "2024-09-01", "2", "box", "6000"),
             ("L3", "p2", "2024-09-01", "7", "piece", "250")]
    cols = ["lot", "pid", "expiry", "qty", "unit", "cost"]
    roles = {"lot": "lot_id", "pid": "product_id", "expiry": "expires_at", "qty": "qty_purchased",
             "unit": "unit", "cost": "purchase_unit_cost"}
    with _lots(tmp_path, cols, lines, roles) as db:
        costs = _costs(db)
    assert costs["L1"] is None and costs["L2"] is None
    assert costs["L3"] == 250


def test_two_readings_of_one_lot_that_disagree_give_no_cost(tmp_path):
    rows = [("L1", "p1", "2024-06-01", "6", "1100"), ("L1", "p1", "2024-06-01", "6", "1500"),
            ("L2", "p1", "2024-09-01", "10", "1000"), ("L3", "p2", "2024-09-01", "7", "2500")]
    with _lots(tmp_path, ["lot", "pid", "expiry", "qty", "cost"], rows, BASE_ROLES) as db:
        costs = _costs(db)
    assert costs["L1"] is None and costs["L2"] == 1000


def test_reading_in_an_unknown_unit_does_not_veto_a_known_base_cost(tmp_path):
    rows = [("L1", "p1", "2024-06-01", "6", "1100"), ("L2", "p1", "2024-09-01", "10", "1000"),
            ("L3", "p2", "2024-09-01", "7", "2500")]
    purchase_cols = ["lot", "pid", "expiry", "qty", "unit", "cost"]
    purchase = [("L1", "p1", "2024-06-01", "1", "box", "6600")]  # box has no known factor
    roles = {"lot": "lot_id", "pid": "product_id", "expiry": "expires_at", "qty": "qty_purchased",
             "unit": "unit", "cost": "purchase_unit_cost"}
    extra = ({"buy": (purchase_cols, purchase)},
             [{"columns": purchase_cols, "kind": "purchase", "roles": roles}])
    with _lots(tmp_path, ["lot", "pid", "expiry", "qty", "cost"], rows, BASE_ROLES, extra) as db:
        assert _costs(db) == {"L1": 1100, "L2": 1000, "L3": 2500}
