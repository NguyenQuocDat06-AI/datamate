"""Annotation check: roles the source values contradict are dropped or swapped before the layer is built
(synthetic data only). Each check has a counterexample whose annotation must stay as it is."""
import sqlite3

from bizreason.contracts import BusinessContext, ColumnMeta, SourceRef, TableMeta
from bizreason.semantic import annotation_check, layer

SALES = ["order", "day", "item", "unit", "qty", "qty_base", "amount"]
SALES_ROLES = {"order": "order_id", "day": "ordered_at", "item": "product_name", "unit": "unit", "qty": "qty",
               "qty_base": "qty_base", "amount": "line_amount"}


def _sales_rows(n=6, unit="box", qty="2", qty_base="20"):
    return [(f"o{i}", f"2024-01-{i + 1:02d}", "Prod A", unit, qty, qty_base, "100") for i in range(n)]


def _context(tmp_path, specs):
    source = tmp_path / f"input{len(list(tmp_path.glob('input*.sqlite')))}.sqlite"
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
    """kinds_roles: table -> (kind, roles). Returns (roles of each table after the check, the fixes)."""
    context = _context(tmp_path, specs)
    dst = sqlite3.connect(":memory:")
    layer._register(dst)
    dst.execute("ATTACH DATABASE ? AS source", (str(context.database),))
    annotated = [(t, {"kind": kinds_roles[t.name][0], "columns": [c.name for c in t.columns],
                      "roles": kinds_roles[t.name][1]}) for t in context.tables]
    result, fixes = annotation_check.check_annotations(dst, annotated)
    return {t.name: note["roles"] for t, note in result}, fixes


def _branch(order_values):
    return {"branch": (["code", "name"], [(v, "x") for v in order_values])}


def test_order_id_on_a_table_sharing_no_order_is_dropped(tmp_path):
    specs = {"sales": (SALES, _sales_rows()), **_branch(["S1"])}
    roles, fixes = _check(tmp_path, specs, {"sales": ("sales", SALES_ROLES),
                                            "branch": ("other", {"code": "order_id", "name": "other"})})
    assert roles["branch"]["code"] == "other"
    assert roles["sales"] == SALES_ROLES
    assert fixes == [{"table": "branch", "column": "code", "from": "order_id", "to": "other",
                      "why": fixes[0]["why"]}]


def test_order_id_sharing_orders_is_kept(tmp_path):
    specs = {"sales": (SALES, _sales_rows()), **_branch(["o0", "o1", "o2", "zz"])}
    roles, fixes = _check(tmp_path, specs, {"sales": ("sales", SALES_ROLES),
                                            "branch": ("other", {"code": "order_id", "name": "other"})})
    assert roles["branch"]["code"] == "order_id"
    assert fixes == []


def test_order_linked_kinds_are_never_judged_by_order_ids(tmp_path):
    pay = {"pay": (["code", "name"], [("unknown", "x")])}
    specs = {"sales": (SALES, _sales_rows()), **pay}
    roles, fixes = _check(tmp_path, specs, {"sales": ("sales", SALES_ROLES),
                                            "pay": ("payment", {"code": "order_id", "name": "other"})})
    assert roles["pay"]["code"] == "order_id" and fixes == []


def test_order_id_is_not_judged_without_a_sales_table(tmp_path):
    roles, fixes = _check(tmp_path, _branch(["S1"]), {"branch": ("other", {"code": "order_id", "name": "other"})})
    assert roles["branch"]["code"] == "order_id" and fixes == []


def test_key_spelled_in_another_case_is_still_dropped(tmp_path):
    specs = {"sales": (SALES, _sales_rows()), **_branch(["S1"])}
    roles, fixes = _check(tmp_path, specs, {"sales": ("sales", SALES_ROLES),
                                            "branch": ("other", {"CODE": "order_id", "name": "other"})})
    assert roles["branch"]["CODE"] == "other" and len(fixes) == 1


def _dated(values):
    return {"t": (["day", "v"], [(v, "1") for v in values])}


def test_date_role_on_a_column_without_dates_is_dropped(tmp_path):
    roles, fixes = _check(tmp_path, _dated(["abc", "def", "ghi", "jkl", "mno", "2024-01-02"]),
                          {"t": ("other", {"day": "ordered_at", "v": "other"})})
    assert roles["t"]["day"] == "other"
    assert fixes[0]["from"] == "ordered_at" and fixes[0]["to"] == "other"


def test_date_role_on_dates_and_on_a_tiny_sample_is_kept(tmp_path):
    ok = ["2024-01-0%d" % i for i in range(1, 7)]
    roles, fixes = _check(tmp_path, _dated(ok), {"t": ("other", {"day": "ordered_at", "v": "other"})})
    assert roles["t"]["day"] == "ordered_at" and fixes == []
    roles, fixes = _check(tmp_path, _dated(["abc", "def"]), {"t": ("other", {"day": "ordered_at", "v": "other"})})
    assert roles["t"]["day"] == "ordered_at" and fixes == []


def test_date_column_with_a_few_bad_cells_is_kept(tmp_path):
    values = ["2024-01-0%d" % (i % 9 + 1) for i in range(19)] + ["n/a"]
    roles, fixes = _check(tmp_path, _dated(values), {"t": ("other", {"day": "ordered_at", "v": "other"})})
    assert roles["t"]["day"] == "ordered_at" and fixes == []


def test_money_and_quantity_roles_need_numbers(tmp_path):
    words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
    specs = {"t": (["a", "b", "c"], [(w, "1.200.000đ", str(i)) for i, w in enumerate(words)])}
    roles, fixes = _check(tmp_path, specs, {"t": ("sales", {"a": "line_amount", "b": "line_amount", "c": "qty"})})
    assert roles["t"] == {"a": "other", "b": "line_amount", "c": "qty"}
    assert [f["column"] for f in fixes] == ["a"]


UNITS = ["item", "unit", "factor"]
UNIT_ROLES = {"item": "product_name", "unit": "base_unit", "factor": "conversion_factor"}


def _units(rows, sales=None):
    specs = {"conv": (UNITS, rows)}
    kinds = {"conv": ("unit_conversion", UNIT_ROLES)}
    if sales is not None:
        specs["sales"] = (SALES, sales)
        kinds["sales"] = ("sales", SALES_ROLES)
    return specs, kinds


def test_unit_column_with_several_unit_rows_per_product_becomes_large_unit(tmp_path):
    specs, kinds = _units([("A", "box", "180"), ("A", "strip", "12"), ("B", "box", "24")])
    roles, fixes = _check(tmp_path, specs, kinds)
    assert roles["conv"]["unit"] == "large_unit"
    assert fixes[0]["from"] == "base_unit" and fixes[0]["to"] == "large_unit"


def test_unit_column_is_large_when_sales_count_it_in_a_different_quantity(tmp_path):
    specs, kinds = _units([("A", "box", "10"), ("B", "pack", "6")],
                          _sales_rows(unit="box") + _sales_rows(unit="pack"))
    roles, _ = _check(tmp_path, specs, kinds)
    assert roles["conv"]["unit"] == "large_unit"


def test_base_unit_counted_as_is_on_sales_stays_base(tmp_path):
    specs, kinds = _units([("A", "piece", "10"), ("B", "piece", "6")], _sales_rows(unit="piece", qty_base="2"))
    roles, fixes = _check(tmp_path, specs, kinds)
    assert roles["conv"]["unit"] == "base_unit" and fixes == []


def test_one_row_per_product_without_sales_evidence_stays(tmp_path):
    specs, kinds = _units([("A", "piece", "10"), ("B", "piece", "6")])
    roles, fixes = _check(tmp_path, specs, kinds)
    assert roles["conv"]["unit"] == "base_unit" and fixes == []


def test_factor_not_above_one_or_a_chain_cell_stays(tmp_path):
    for rows in ([("A", "box", "180"), ("A", "strip", "1")], [("A", "box", "180"), ("A", "strip", "12 piece")]):
        specs, kinds = _units(rows)
        roles, fixes = _check(tmp_path, specs, kinds)
        assert roles["conv"]["unit"] == "base_unit" and fixes == []


def test_table_that_already_has_a_large_unit_stays(tmp_path):
    specs = {"conv": (["item", "big", "unit", "factor"], [("A", "box", "piece", "180"), ("A", "strip", "piece", "12")])}
    roles = {"item": "product_name", "big": "large_unit", "unit": "base_unit", "factor": "conversion_factor"}
    result, fixes = _check(tmp_path, specs, {"conv": ("unit_conversion", roles)})
    assert result["conv"] == roles and fixes == []


def test_build_reports_fixes_and_the_corrected_unit_makes_the_factor(tmp_path):
    specs = {"sales": (SALES, [(f"o{i}", f"2024-01-{i + 1:02d}", "A", "box", "2", "", "100") for i in range(6)]),
             "conv": (UNITS, [("A", "box", "180"), ("A", "strip", "12")]), **_branch(["S1"])}
    context = _context(tmp_path, specs)
    records = [{"columns": SALES, "kind": "sales", "roles": SALES_ROLES},
               {"columns": UNITS, "kind": "unit_conversion", "roles": UNIT_ROLES},
               {"columns": ["code", "name"], "kind": "other", "roles": {"code": "order_id", "name": "other"}}]
    target = tmp_path / "canonical.sqlite"
    report = layer.build(context, records, target)
    assert {(f["table"], f["from"], f["to"]) for f in report["annotation_fixes"]} == {
        ("conv", "base_unit", "large_unit"), ("branch", "order_id", "other")}
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT DISTINCT qty_base FROM lines").fetchall() == [(360.0,)]


def test_build_without_contradictions_reports_no_fixes(tmp_path):
    context = _context(tmp_path, {"sales": (SALES, _sales_rows())})
    report = layer.build(context, [{"columns": SALES, "kind": "sales", "roles": SALES_ROLES}],
                         tmp_path / "canonical.sqlite")
    assert report["annotation_fixes"] == []
