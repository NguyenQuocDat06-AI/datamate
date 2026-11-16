"""Two layer regressions: a service log with no money column is not a set of orders, and one customer written with
different phone spellings is one customer (so a debt asked by customer is neither lost nor read as zero).

The shops are invented; column names are neutral.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from bizreason.contracts import BusinessContext, ColumnMeta, SourceRef, TableMeta, TimeWindow
from bizreason.data.identities import unify_phone_spellings
from bizreason.semantic import layer
from bizreason.semantic.compiler import Compiler, Intent, Unsupported

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
            name=name, sources=[SourceRef(path=f"{name}.csv")],
            columns=[ColumnMeta(name=c, original_name=c, dtype="text") for c in columns],
            row_count=len(rows), raw_row_count=len(rows),
        )
        for name, (columns, rows) in specs.items()
    ]
    return BusinessContext(business_id="test", revision="r1", database=path, readme="", manifest={}, tables=tables)


def _record(columns: list[str], kind: str, roles: dict[str, str]) -> dict[str, Any]:
    return {"columns": columns, "kind": kind, "roles": roles}


def _run(tmp_path: Path, specs: Specs, records: list[dict[str, Any]]) -> tuple[dict, Path]:
    out = tmp_path / "canonical.sqlite"
    return layer.build(_context(_source(tmp_path, specs), specs), records, out), out


def _query(out: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(out)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


SALES_COLUMNS = ["order", "day", "who", "item", "amount", "state"]
SALES_ROLES = {"order": "order_id", "day": "ordered_at", "who": "customer_name", "item": "product_name",
               "amount": "line_amount", "state": "order_status"}
LOG_COLUMNS = ["received", "who", "device", "fault"]
LOG_ROLES = {"received": "ordered_at", "who": "customer_name", "device": "vehicle_model", "fault": "note"}


# --- A. a service log without money --------------------------------------------------------------------------

def _sales_and_log(log_roles: dict[str, str], log_columns: list[str] | None = None,
                   log_rows: list[tuple] | None = None) -> tuple[Specs, list[dict[str, Any]]]:
    columns = log_columns or LOG_COLUMNS
    specs: Specs = {
        "sales": (SALES_COLUMNS, [("O1", "2026-03-01", "An", "tea", "100", "completed"), ("O2", "2026-03-02", "Bo", "tea", "300", "completed")]),
        "log": (columns, log_rows or [("2026-03-03", "An", "phone x", "screen"), ("2026-03-04", "Bo", "phone y", "battery"),
                                      ("2026-03-05", "An", "phone x", "screen")]),
    }
    return specs, [_record(SALES_COLUMNS, "sales", SALES_ROLES), _record(columns, "service_ticket", log_roles)]


def test_ticket_log_without_money_adds_no_orders(tmp_path: Path) -> None:
    specs, records = _sales_and_log(LOG_ROLES)
    report, out = _run(tmp_path, specs, records)

    assert _query(out, "SELECT order_id FROM orders ORDER BY order_id") == [("O1",), ("O2",)]
    assert _query(out, "SELECT COUNT(*) FROM lines") == [(2,)]
    assert _query(out, "SELECT COUNT(*) FROM raw_lines WHERE order_id LIKE 'log:%'") == [(0,)]
    assert report["service_tables"] == 0


def test_ticket_log_without_money_keeps_its_rows_as_a_source_table(tmp_path: Path) -> None:
    specs, records = _sales_and_log(LOG_ROLES)
    report, out = _run(tmp_path, specs, records)

    assert "src_log" in report["src_tables"]
    assert _query(out, "SELECT COUNT(*) FROM src_log") == [(3,)]


def test_per_order_average_is_not_diluted_by_a_log_without_money(tmp_path: Path) -> None:
    specs, records = _sales_and_log(LOG_ROLES)
    _, out = _run(tmp_path, specs, records)
    window = TimeWindow(start=date(2026, 3, 1), end_exclusive=date(2026, 4, 1))
    con = sqlite3.connect(out)
    try:
        compiler = Compiler(con)
        assert compiler.answer(Intent(measure="avg_per_order"), [window]).value == 200
        assert compiler.answer(Intent(measure="order_count"), [window]).value == 2
    finally:
        con.close()


def test_ticket_table_with_an_amount_still_becomes_orders(tmp_path: Path) -> None:
    columns = ["ticket", "visit", "job", "bill"]
    specs: Specs = {"repairs": (columns, [("T1", "2026-03-01", "oil", "200"), ("T2", "2026-03-02", "brake", "450")])}
    roles = {"ticket": "ticket_id", "visit": "ordered_at", "job": "service_item", "bill": "ticket_amount"}
    report, out = _run(tmp_path, specs, [_record(columns, "service_ticket", roles)])

    assert report["service_tables"] == 1
    assert _query(out, "SELECT order_id, revenue FROM orders ORDER BY order_id") == [("T1", 200.0), ("T2", 450.0)]


def test_ticket_table_with_zero_amounts_is_still_money_bearing(tmp_path: Path) -> None:
    # a warranty visit collects 0: the column exists, so the visits stay orders (the shop's conventions decide)
    columns = ["ticket", "visit", "bill"]
    specs: Specs = {"repairs": (columns, [("T1", "2026-03-01", "0"), ("T2", "2026-03-02", "450")])}
    roles = {"ticket": "ticket_id", "visit": "ordered_at", "bill": "ticket_amount"}
    report, out = _run(tmp_path, specs, [_record(columns, "service_ticket", roles)])

    assert report["service_tables"] == 1
    assert _query(out, "SELECT COUNT(*) FROM orders") == [(2,)]


def test_ticket_table_with_a_line_amount_role_loads(tmp_path: Path) -> None:
    columns = ["ticket", "visit", "bill"]
    specs: Specs = {"repairs": (columns, [("T1", "2026-03-01", "200")])}
    roles = {"ticket": "ticket_id", "visit": "ordered_at", "bill": "line_amount"}
    report, out = _run(tmp_path, specs, [_record(columns, "service_ticket", roles)])

    assert report["service_tables"] == 1
    assert _query(out, "SELECT order_id, revenue FROM orders") == [("T1", 200.0)]


def _warranty_shop(tmp_path: Path, conventions: list[dict[str, Any]] | None) -> tuple[dict, Path]:
    specs, records = _sales_and_log(LOG_ROLES)
    out = tmp_path / "canonical.sqlite"
    report = layer.build(_context(_source(tmp_path, specs), specs), records, out, conventions=conventions)
    return report, out


WARRANTY_CONVENTION = [{"type": "zero_amount_meaning", "meaning": "warranty", "note_words": ["free"]}]


def test_money_free_log_rows_are_kept_as_service_events(tmp_path: Path) -> None:
    report, out = _warranty_shop(tmp_path, None)

    assert report["service_event_rows"] == 3
    assert _query(out, "SELECT date FROM service_events ORDER BY date") == [("2026-03-03",), ("2026-03-04",), ("2026-03-05",)]


def test_visit_count_comes_from_the_log_when_the_shop_says_zero_money_is_warranty(tmp_path: Path) -> None:
    _, out = _warranty_shop(tmp_path, WARRANTY_CONVENTION)
    window = TimeWindow(start=date(2026, 3, 4), end_exclusive=date(2026, 3, 6))
    con = sqlite3.connect(out)
    try:
        assert Compiler(con).answer(Intent(measure="warranty_count"), [window]).value == 2
    finally:
        con.close()


def test_visit_count_is_not_answered_without_the_shop_convention(tmp_path: Path) -> None:
    _, out = _warranty_shop(tmp_path, None)
    window = TimeWindow(start=date(2026, 3, 1), end_exclusive=date(2026, 4, 1))
    con = sqlite3.connect(out)
    try:
        with pytest.raises(Unsupported, match="not identified"):
            Compiler(con).answer(Intent(measure="warranty_count"), [window])
    finally:
        con.close()


def test_visit_count_with_a_filter_or_as_a_rate_is_not_guessed(tmp_path: Path) -> None:
    _, out = _warranty_shop(tmp_path, WARRANTY_CONVENTION)
    window = TimeWindow(start=date(2026, 3, 1), end_exclusive=date(2026, 4, 1))
    con = sqlite3.connect(out)
    try:
        compiler = Compiler(con)
        with pytest.raises(Unsupported):
            compiler.answer(Intent(measure="warranty_count", filters=[{"dim": "product_kind", "value": "tea"}]), [window])
        with pytest.raises(Unsupported):
            compiler.answer(Intent(measure="warranty_rate"), [window])
    finally:
        con.close()


@pytest.mark.parametrize("roles,expected", [
    ({"ordered_at": "a", "customer_name": "b"}, False),
    ({"ticket_id": "a", "service_item": "b"}, False),
    ({"ticket_amount": "a"}, True),
    ({"line_amount": "a"}, True),
    ({"ticket_amount": None}, False),
    ({}, False),
])
def test_ticket_has_money_reads_only_amount_roles(roles: dict[str, str], expected: bool) -> None:
    assert layer._ticket_has_money(roles) is expected


# --- B. one customer, several phone spellings ----------------------------------------------------------------

def _names_db(rows: list[tuple[Any, str | None]]) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (customer_id TEXT, customer_name TEXT)")
    con.executemany("INSERT INTO t VALUES (?,?)", rows)
    return con


def _names(con: sqlite3.Connection) -> list[str | None]:
    return [row[0] for row in con.execute("SELECT customer_name FROM t ORDER BY rowid")]


def test_spellings_of_one_phone_become_one_name() -> None:
    con = _names_db([(None, "Mai Lan 0353 838 285"), (None, "Mai Lan (0353838285)"), (None, "Mai Lan - 0353838285"),
                     (None, "Mai Lan 0353838285")])
    changed = unify_phone_spellings(con, [("t", "customer_name", "customer_id")])

    assert len(set(_names(con))) == 1
    assert changed == 3  # the most common spelling is kept; the other three are rewritten, one already agrees


def test_same_name_with_different_phones_stays_two_customers() -> None:
    con = _names_db([(None, "Mai Lan 0353838285"), (None, "Mai Lan 0911222333")])
    unify_phone_spellings(con, [("t", "customer_name", "customer_id")])

    assert _names(con) == ["Mai Lan 0353838285", "Mai Lan 0911222333"]


def test_same_phone_under_different_names_stays_two_customers() -> None:
    con = _names_db([(None, "Mai Lan 0353838285"), (None, "Hoa Binh (0353 838 285)")])
    unify_phone_spellings(con, [("t", "customer_name", "customer_id")])

    assert _names(con) == ["Mai Lan 0353838285", "Hoa Binh (0353 838 285)"]


def test_names_without_a_phone_and_rows_with_a_code_are_untouched() -> None:
    con = _names_db([(None, "Mai Lan"), ("C1", "Mai Lan 0353 838 285"), (None, "Mai Lan 0353838285"), (None, None)])
    unify_phone_spellings(con, [("t", "customer_name", "customer_id")])

    assert _names(con) == ["Mai Lan", "Mai Lan 0353 838 285", "Mai Lan 0353838285", None]


def test_country_code_and_national_spellings_agree() -> None:
    con = _names_db([(None, "Mai Lan 0353838285"), (None, "Mai Lan +84 353 838 285")])
    unify_phone_spellings(con, [("t", "customer_name", "customer_id")])

    assert len(set(_names(con))) == 1


DEBT_COLUMNS = ["order", "day", "who", "item", "amount", "owed", "state"]


def _debt_shop(tmp_path: Path, rows: list[tuple]) -> tuple[dict, Path]:
    specs: Specs = {"sales": (DEBT_COLUMNS, rows)}
    roles = {**SALES_ROLES, "owed": "outstanding"}
    return _run(tmp_path, specs, [_record(DEBT_COLUMNS, "sales", roles)])


def test_layer_gives_one_customer_key_to_spellings_of_one_phone(tmp_path: Path) -> None:
    # no ingestion role is set on these columns (the header lookup was off): only the annotation names the customer
    report, out = _debt_shop(tmp_path, [
        ("O1", "2026-03-01", "Mai Lan 0353838285", "tea", "100", "0", "completed"),
        ("O2", "2026-03-02", "Mai Lan - 0353 838 285", "tea", "300", "300", "completed"),
        ("O3", "2026-03-03", "Hoa 0911222333", "tea", "50", "0", "completed"),
    ])

    assert report["phone_spellings_unified"] >= 1
    assert _query(out, "SELECT COUNT(DISTINCT customer_key) FROM orders") == [(2,)]
    keys = {row[0]: row[1] for row in _query(out, "SELECT order_id, customer_key FROM orders")}
    assert keys["O1"] == keys["O2"] != keys["O3"]


def test_customer_filter_sees_every_order_of_the_customer(tmp_path: Path) -> None:
    _, out = _debt_shop(tmp_path, [
        ("O1", "2026-03-01", "Mai Lan 0353838285", "tea", "100", "0", "completed"),
        ("O2", "2026-03-02", "Mai Lan - 0353 838 285", "tea", "300", "300", "completed"),
        ("O3", "2026-03-03", "Hoa 0911222333", "tea", "50", "0", "completed"),
    ])
    window = TimeWindow(start=date(2026, 3, 1), end_exclusive=date(2026, 4, 1))
    con = sqlite3.connect(out)
    try:
        compiler = Compiler(con)
        intent = Intent(measure="order_count", filters=[{"dim": "customer", "value": "Mai Lan 0353838285"}])
        assert compiler.answer(intent, [window]).value == 2
    finally:
        con.close()


def _customer_compiler(tmp_path: Path, names: list[str]) -> tuple[Compiler, sqlite3.Connection]:
    rows = [(f"O{i}", "2026-03-01", name, "tea", "100", "0", "completed") for i, name in enumerate(names, start=1)]
    _, out = _debt_shop(tmp_path, rows)
    con = sqlite3.connect(out)
    return Compiler(con), con


def _predicate_keys(compiler: Compiler, text: str) -> list[str]:
    return compiler._customer_predicate(text)[1]


def test_customer_named_without_the_phone_resolves_to_the_one_customer(tmp_path: Path) -> None:
    compiler, con = _customer_compiler(tmp_path, ["Mai Lan 0353838285", "Hoa 0911222333"])
    try:
        assert len(_predicate_keys(compiler, "Mai Lan")) == 1
    finally:
        con.close()


def test_customer_named_with_the_phone_spaced_differently_resolves(tmp_path: Path) -> None:
    compiler, con = _customer_compiler(tmp_path, ["Mai Lan 0353838285", "Hoa 0911222333"])
    try:
        assert len(_predicate_keys(compiler, "Mai Lan 0353 838 285")) == 1
    finally:
        con.close()


def test_name_shared_by_two_phones_is_ambiguous_not_guessed(tmp_path: Path) -> None:
    compiler, con = _customer_compiler(tmp_path, ["Mai Lan 0353838285", "Mai Lan 0911222333"])
    try:
        with pytest.raises(Unsupported, match="ambiguous customer"):
            compiler._customer_predicate("Mai Lan")
        assert len(_predicate_keys(compiler, "Mai Lan 0911 222 333")) == 1
    finally:
        con.close()


def test_a_phone_that_belongs_to_nobody_is_unresolved(tmp_path: Path) -> None:
    compiler, con = _customer_compiler(tmp_path, ["Mai Lan 0353838285", "Hoa 0911222333"])
    try:
        with pytest.raises(Unsupported, match="unresolved customer"):
            compiler._customer_predicate("Mai Lan 0999999999")
    finally:
        con.close()


def test_an_unknown_name_is_unresolved_not_zero(tmp_path: Path) -> None:
    compiler, con = _customer_compiler(tmp_path, ["Mai Lan 0353838285", "Hoa 0911222333"])
    try:
        with pytest.raises(Unsupported, match="unresolved customer"):
            compiler._customer_predicate("Nobody Here")
    finally:
        con.close()
