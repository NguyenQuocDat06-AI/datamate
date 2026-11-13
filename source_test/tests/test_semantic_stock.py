"""Stock questions: the stock report loaded by layer.build, the compiler's stock answers, and the solver's routing
(unanswerable when the shop records no stock or lots, abstain when the layer cannot give the answer)."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from bizreason.contracts import AnswerType, BusinessContext, ColumnMeta, Question, SourceRef, TableMeta, TimeWindow
from bizreason.semantic import layer
from bizreason.semantic.compiler import Compiler, Intent, Unsupported
from bizreason.semantic.solver import SemanticSolver, asks_unit_split
from bizreason.utils import normalize_lookup

Specs = dict[str, tuple[list[str], list[tuple[Any, ...]]]]


def _source(tmp_path: Path, specs: Specs) -> Path:
    path = tmp_path / "source.sqlite"
    con = sqlite3.connect(path)
    for name, (columns, rows) in specs.items():
        defs = ", ".join(f'"{c}" TEXT' for c in columns)
        con.execute(f'CREATE TABLE "{name}" ({defs}, __source_row INTEGER, __raw_json TEXT)')
        marks = ",".join("?" * (len(columns) + 2))
        for number, row in enumerate(rows, start=1):
            con.execute(f'INSERT INTO "{name}" VALUES ({marks})', (*row, number, "{}"))
    con.commit()
    con.close()
    return path


def _context(path: Path, specs: Specs) -> BusinessContext:
    tables = [
        TableMeta(
            name=name,
            sources=[SourceRef(path=f"{name}.csv")],
            columns=[ColumnMeta(name=c, original_name=c, dtype="text") for c in columns],
            row_count=len(rows),
            raw_row_count=len(rows),
        )
        for name, (columns, rows) in specs.items()
    ]
    return BusinessContext(business_id="test", revision="r1", database=path, readme="", manifest={}, tables=tables)


def _record(columns: list[str], kind: str, roles: dict[str, str]) -> dict[str, Any]:
    return {"columns": columns, "kind": kind, "roles": roles}


SALES = ["order", "when", "item", "qty", "amount"]
CATALOG = ["code", "name", "unit_base", "cost"]
CONVERSION = ["name", "big", "factor"]
STOCK = ["code", "name", "unit", "on_hand"]
SALE_ROWS = [
    ("O1", "2026-03-01", "Item Alpha", "2", "100"),
    ("O2", "2026-03-02", "Item Beta", "1", "50"),
    ("O3", "2026-03-03", "Item Alpha", "1", "40"),
]
CATALOG_ROWS = [("A1", "Item Alpha", "pc", "10"), ("B1", "Item Beta", "pc", "7")]
CONVERSION_ROWS = [("Item Alpha", "box", "6")]
STOCK_ROWS = [("A1", "Item Alpha", "pc", "9"), ("B1", "Item Beta", "pc", "0")]
PURCHASE = ["receipt", "day", "item", "unit", "qty"]
PURCHASE_ROWS = [("R1", "2026-03-01", "Item Alpha", "box", "2"), ("R2", "2026-03-01", "Item Beta", "pc", "3")]


def _specs(stock_rows=STOCK_ROWS) -> Specs:
    return {
        "sales": (SALES, SALE_ROWS),
        "catalog": (CATALOG, CATALOG_ROWS),
        "conversion": (CONVERSION, CONVERSION_ROWS),
        "stock": (STOCK, stock_rows),
        "receipts": (PURCHASE, PURCHASE_ROWS),
    }


def _records() -> list[dict[str, Any]]:
    return [
        _record(SALES, "sales", {"order": "order_id", "when": "ordered_at", "item": "product_name",
                                 "qty": "qty", "amount": "line_amount"}),
        _record(CATALOG, "product_catalog", {"code": "product_id", "name": "product_name",
                                             "unit_base": "base_unit", "cost": "catalog_cost"}),
        _record(CONVERSION, "unit_conversion", {"name": "product_name", "big": "large_unit",
                                                "factor": "conversion_factor"}),
        _record(STOCK, "inventory", {"code": "product_id", "name": "product_name", "unit": "unit",
                                     "on_hand": "inventory_qty"}),
        _record(PURCHASE, "purchase", {"receipt": "purchase_id", "day": "purchased_at", "item": "product_name",
                                       "unit": "unit", "qty": "qty_purchased"}),
    ]


def _build(tmp_path: Path, specs: Specs | None = None, records: list[dict[str, Any]] | None = None) -> Path:
    specs = specs or _specs()
    out = tmp_path / "canonical.sqlite"
    layer.build(_context(_source(tmp_path, specs), specs), records if records is not None else _records(), out)
    return out


def _rows(path: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_stock_report_row_carries_quantity_cost_factor_and_report_date(tmp_path: Path) -> None:
    out = _build(tmp_path)
    assert _rows(out, "SELECT product_name, unit, qty_base, unit_cost, large_factor, received, stock_date "
                      "FROM stock ORDER BY product_name") == [
        ("Item Alpha", "pc", 9.0, 10.0, 6.0, 1, "2026-03-03"),
        ("Item Beta", "pc", 0.0, 7.0, None, 1, "2026-03-03"),
    ]
    assert _rows(out, "SELECT kind, rows FROM stock_sources WHERE kind != 'unannotated'") == [("inventory", 2)]


def test_a_ledger_with_two_quantity_columns_is_not_a_snapshot(tmp_path: Path) -> None:
    columns = ["code", "name", "moved", "balance"]
    specs = _specs()
    specs["stock"] = (columns, [("A1", "Item Alpha", "2", "9")])
    records = _records()
    records[3] = _record(columns, "inventory", {"code": "product_id", "name": "product_name",
                                                "moved": "inventory_qty", "balance": "inventory_qty"})
    out = _build(tmp_path, specs, records)
    assert _rows(out, "SELECT COUNT(*) FROM stock") == [(0,)]
    assert _rows(out, "SELECT kind, rows FROM stock_sources WHERE kind != 'unannotated'") == [("inventory", 0)]


def test_lot_table_is_recorded_as_a_stock_source_and_not_loaded(tmp_path: Path) -> None:
    lot_columns = ["lot", "item", "expires", "qty"]
    specs = _specs()
    specs["lots"] = (lot_columns, [("L1", "Item Alpha", "2026-09-01", "4")])
    records = _records()[:3] + [_record(lot_columns, "lot", {"lot": "lot_id", "item": "product_name",
                                                             "expires": "expires_at", "qty": "qty_purchased_base"})]
    out = _build(tmp_path, specs, records)
    assert _rows(out, "SELECT kind, rows FROM stock_sources WHERE kind != 'unannotated'") == [("lot", 0)]


def _compiler(path: Path) -> Compiler:
    con = sqlite3.connect(path)
    con.row_factory = None
    return Compiler(con)


def _window(day: str) -> TimeWindow:
    start = date.fromisoformat(day)
    return TimeWindow(role="target", start=date.min, end_exclusive=start.fromordinal(start.toordinal() + 1))


def _stock_intent(measure: str = "inventory_qty", product: str | None = "Item Alpha") -> Intent:
    filters = [{"dim": "product", "exclude": False, "value": product}] if product else []
    return Intent(measure=measure, filters=filters)


def test_stock_quantity_on_the_report_day_is_a_count_of_base_units(tmp_path: Path) -> None:
    out = _build(tmp_path)
    answer = _compiler(out).stock(_stock_intent(), _window("2026-03-03"))
    assert (answer.type, answer.value) == ("number", 9)


def test_stock_quantity_as_whole_large_units_and_loose_units(tmp_path: Path) -> None:
    out = _build(tmp_path)
    answer = _compiler(out).stock(_stock_intent(), _window("2026-03-03"), split=True)
    assert (answer.type, answer.value) == ("list", [1, 3])


def test_stock_on_another_day_is_not_answered_without_a_roll_back(tmp_path: Path) -> None:
    out = _build(tmp_path)
    with pytest.raises(Unsupported):
        _compiler(out).stock(_stock_intent(), _window("2026-03-02"))


def test_stock_value_sums_positive_stock_at_catalog_cost(tmp_path: Path) -> None:
    out = _build(tmp_path)
    answer = _compiler(out).stock(_stock_intent("inventory_value", product=None), _window("2026-03-03"))
    assert (answer.type, answer.value) == ("number", 90)


def test_out_of_stock_count_counts_bought_in_products_at_zero(tmp_path: Path) -> None:
    out = _build(tmp_path)
    answer = _compiler(out).stock(_stock_intent("out_of_stock_count", product=None), _window("2026-03-03"))
    assert (answer.type, answer.value) == ("number", 1)


def test_product_listed_twice_in_the_report_is_refused(tmp_path: Path) -> None:
    out = _build(tmp_path, _specs(stock_rows=[("A1", "Item Alpha", "pc", "9"), ("A2", "Item Alpha", "pc", "4")]))
    with pytest.raises(Unsupported):
        _compiler(out).stock(_stock_intent("inventory_value", product=None), _window("2026-03-03"))


def test_unit_split_cues_need_a_whole_word_and_the_loose_word():
    assert asks_unit_split("tồn cuối hôm nay là mấy hộp chẵn, mấy cái lẻ?")
    assert asks_unit_split("bao nhiêu hộp nguyên và bao nhiêu cái lẻ")
    assert not asks_unit_split("tồn cuối hôm nay còn bao nhiêu cái?")
    assert not asks_unit_split("còn bao nhiêu hộp chẵn")


class _Parsed:
    def __init__(self, intent: Intent | None, reason: str = ""):
        self.intent = intent
        self.reason = reason


class _Parser:
    def __init__(self, result: _Parsed):
        self.result = result

    def parse(self, question: str, choices: dict[str, list[str]] | None = None) -> _Parsed:
        return self.result


class _Temporal:
    def __init__(self, windows: list[TimeWindow]):
        self.windows = windows

    def resolve(self, question: str, as_of: date) -> list[TimeWindow]:
        return list(self.windows)

    def resolve_periods(self, question: str, as_of: date) -> list[TimeWindow]:
        return list(self.windows)


def _solve(path: Path, intent: Intent | None, text: str = "q", windows: list[TimeWindow] | None = None):
    conn = sqlite3.connect(path)
    conn.create_function("lookup", 1, lambda v: normalize_lookup(v) if v is not None else None)
    solver = SemanticSolver(_Parser(_Parsed(intent)), _Temporal(windows or [_window("2026-03-03")]), conn)
    prediction, trace = solver.solve(Question(id="q1", business_id="b", question=text, as_of_date=date(2026, 3, 3)))
    return prediction, trace


def test_solver_answers_stock_question_from_the_report(tmp_path: Path) -> None:
    out = _build(tmp_path)
    prediction, trace = _solve(out, _stock_intent(), "ton kho item alpha cuoi ngay")
    assert trace.decision == "value"
    assert prediction.type == AnswerType.NUMBER and prediction.answer == 9


def test_solver_answers_split_stock_question_as_large_and_loose_units(tmp_path: Path) -> None:
    out = _build(tmp_path)
    prediction, trace = _solve(out, _stock_intent(), "cuoi ngay item alpha may box chan, may pc le")
    assert trace.decision == "value"
    assert prediction.answer == [1, 3]


def test_unannotated_table_keeps_the_shop_undecided(tmp_path: Path) -> None:
    specs = {"sales": (SALES, SALE_ROWS), "catalog": (CATALOG, CATALOG_ROWS), "notes": (["a"], [("x",)])}
    records = [_records()[0], _records()[1]]
    out = _build(tmp_path, specs, records)
    prediction, trace = _solve(out, _stock_intent(), "ton kho item alpha")
    assert trace.decision == "abstain"


def test_shop_without_stock_report_or_lots_abstains(tmp_path: Path) -> None:
    # Public test: such shops still had stock answers, so a missing source abstains instead of being unanswerable.
    specs = {"sales": (SALES, SALE_ROWS), "catalog": (CATALOG, CATALOG_ROWS)}
    records = [_records()[0], _records()[1]]
    out = _build(tmp_path, specs, records)
    prediction, trace = _solve(out, _stock_intent(), "ton kho item alpha")
    assert trace.decision == "abstain"


def test_shop_without_lots_is_unanswerable_for_expiry_questions(tmp_path: Path) -> None:
    out = _build(tmp_path)
    prediction, trace = _solve(out, Intent(measure="expiry_lot", shape="list_filter"), "lo nao sap het han")
    assert trace.decision == "unanswerable"


def test_lot_only_shop_abstains_on_stock_it_cannot_read(tmp_path: Path) -> None:
    lot_columns = ["lot", "item", "expires", "qty"]
    specs = _specs()
    specs["lots"] = (lot_columns, [("L1", "Item Alpha", "2026-09-01", "4")])
    records = _records()[:3] + [_record(lot_columns, "lot", {"lot": "lot_id", "item": "product_name",
                                                             "expires": "expires_at", "qty": "qty_purchased_base"})]
    out = _build(tmp_path, specs, records)
    prediction, trace = _solve(out, _stock_intent(), "ton kho item alpha")
    assert trace.decision == "abstain"


RECEIPT = ["receipt", "pid", "lot", "exp", "qty"]
RECEIPT_ROWS = [
    ("R1", "A1", "L1", "2026-06-01", "5"),
    ("R2", "A1", "L2", "2026-09-01", "4"),
    ("R3", "B1", "L3", "2026-04-01", "3"),
]
LOT_SALES = ["order", "lot", "sold", "ret"]
LOT_SALE_ROWS = [("O1", "L1", "2", "0"), ("O3", "L2", "1", "0"), ("O2", "L3", "1", "1")]


def _lot_specs() -> Specs:
    specs = _specs(stock_rows=[])
    del specs["stock"]
    specs["receipts"] = (RECEIPT, RECEIPT_ROWS)
    specs["lot_sales"] = (LOT_SALES, LOT_SALE_ROWS)
    return specs


def _lot_records() -> list[dict[str, Any]]:
    return [
        _record(SALES, "sales", {"order": "order_id", "when": "ordered_at", "item": "product_name",
                                 "qty": "qty", "amount": "line_amount"}),
        _record(CATALOG, "product_catalog", {"code": "product_id", "name": "product_name",
                                             "unit_base": "base_unit", "cost": "catalog_cost"}),
        _record(CONVERSION, "unit_conversion", {"name": "product_name", "big": "large_unit",
                                                "factor": "conversion_factor"}),
        _record(RECEIPT, "purchase", {"receipt": "purchase_id", "pid": "product_id", "lot": "lot_id",
                                      "exp": "expires_at", "qty": "qty_purchased"}),
        _record(LOT_SALES, "lot", {"order": "order_id", "lot": "lot_id", "sold": "qty_sold_base",
                                   "ret": "qty_returned"}),
    ]


def _lot_build(tmp_path: Path) -> Path:
    return _build(tmp_path, _lot_specs(), _lot_records())


def test_lot_balance_is_received_less_sold_plus_returned(tmp_path: Path) -> None:
    out = _lot_build(tmp_path)
    assert _rows(out, "SELECT lot_id, product_name, balance FROM lot_balance ORDER BY lot_id") == [
        ("L1", "Item Alpha", 3.0), ("L2", "Item Alpha", 3.0), ("L3", "Item Beta", 3.0),
    ]
    assert _rows(out, "SELECT product_name, qty_base, unit_cost, received FROM stock ORDER BY product_name") == [
        ("Item Alpha", 6.0, 10.0, 1), ("Item Beta", 3.0, 7.0, 1),
    ]


def test_lot_stock_answers_quantity_and_value_like_a_report(tmp_path: Path) -> None:
    out = _lot_build(tmp_path)
    compiler = _compiler(out)
    assert compiler.stock(_stock_intent(), _window("2026-03-03")).value == 6
    value = compiler.stock(_stock_intent("inventory_value", product=None), _window("2026-03-03"))
    assert value.value == 81


def _expiry(out: Path, intent: Intent, start: str | None = None, end: str | None = None) -> Any:
    window = (TimeWindow(role="target", start=date.fromisoformat(start),
                         end_exclusive=date.fromisoformat(end)) if start else
              TimeWindow(role="target", start=date.min, end_exclusive=date(2026, 3, 4)))
    return Compiler(sqlite3.connect(out)).lots(intent, window, date(2026, 3, 3))


def test_earliest_lot_of_a_product_is_the_first_expiry_still_in_stock(tmp_path: Path) -> None:
    out = _lot_build(tmp_path)
    answer = _expiry(out, Intent(measure="expiry_lot", shape="argmax_date", filters=[
        {"dim": "product", "exclude": False, "value": "Item Alpha"}]))
    assert (answer.type, answer.value) == ("date", "2026-06-01")


def test_count_of_lots_expiring_in_a_range(tmp_path: Path) -> None:
    out = _lot_build(tmp_path)
    answer = _expiry(out, Intent(measure="expiry_lot", shape="scalar"), "2026-03-03", "2026-07-01")
    assert (answer.type, answer.value) == ("number", 2)


def test_products_expiring_in_a_range_ordered_by_earliest_expiry(tmp_path: Path) -> None:
    out = _lot_build(tmp_path)
    answer = _expiry(out, Intent(measure="expiry_lot", shape="list_filter"), "2026-03-03", "2026-12-31")
    assert (answer.type, answer.value) == ("list", ["Item Beta", "Item Alpha"])


def test_expiry_without_a_product_is_refused_for_argmax(tmp_path: Path) -> None:
    out = _lot_build(tmp_path)
    with pytest.raises(Unsupported):
        _expiry(out, Intent(measure="expiry_lot", shape="argmax_date"))


def test_expiry_on_another_day_is_refused(tmp_path: Path) -> None:
    out = _lot_build(tmp_path)
    with pytest.raises(Unsupported):
        Compiler(sqlite3.connect(out)).lots(Intent(measure="expiry_lot", shape="scalar"),
                                            TimeWindow(role="target", start=date.min,
                                                       end_exclusive=date(2026, 3, 9)), date(2026, 3, 8))


def test_solver_answers_earliest_lot_question(tmp_path: Path) -> None:
    out = _lot_build(tmp_path)
    prediction, trace = _solve(out, Intent(measure="expiry_lot", shape="argmax_date", filters=[
        {"dim": "product", "exclude": False, "value": "Item Alpha"}]), "lo item alpha gan het han nhat",
        windows=[TimeWindow(role="target", start=date.min, end_exclusive=date(2026, 3, 4))])
    assert trace.decision == "value"
    assert prediction.answer == "2026-06-01"
