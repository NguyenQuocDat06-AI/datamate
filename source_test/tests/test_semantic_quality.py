from __future__ import annotations

import sqlite3

from bizreason.semantic.quality import assess


def stats_db(**stats: object) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE build_stats (key TEXT, value TEXT)")
    conn.executemany("INSERT INTO build_stats VALUES (?, ?)", [(k, str(v)) for k, v in stats.items()])
    return conn


def add_orders(conn: sqlite3.Connection, rows: list[tuple[str, str | None, str | None]]) -> None:
    """Rows of (order_id, date, status)."""
    conn.execute("CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT)")
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?)", rows)


def add_payments(conn: sqlite3.Connection, order_ids: list[str]) -> None:
    conn.execute("CREATE TABLE payments (order_id TEXT, date TEXT, amount REAL, method TEXT)")
    conn.executemany("INSERT INTO payments (order_id, amount) VALUES (?, 1)", [(o,) for o in order_ids])


def no_flags(flags) -> bool:
    return not any([
        flags.qty_unreliable, flags.money_unreliable, flags.order_structure_broken,
        flags.status_unreliable, flags.payments_unlinked,
    ])


def test_qty_fires_above_threshold_and_not_at_it():
    above = assess(stats_db(qty_cells_nonblank=1000, qty_cells_unparsed=6))
    assert above.qty_unreliable is True
    assert any(r.startswith("qty_unreliable:") for r in above.reasons)

    at = assess(stats_db(qty_cells_nonblank=1000, qty_cells_unparsed=5))
    assert at.qty_unreliable is False
    assert at.reasons == []


def test_qty_needs_nonblank_cells():
    flags = assess(stats_db(qty_cells_nonblank=0, qty_cells_unparsed=3))
    assert flags.qty_unreliable is False


def test_money_fires_above_threshold_and_not_at_it():
    above = assess(stats_db(money_cells_nonblank=200, money_cells_unparsed=2))
    assert above.money_unreliable is True
    assert any(r.startswith("money_unreliable:") for r in above.reasons)

    at = assess(stats_db(money_cells_nonblank=200, money_cells_unparsed=1))
    assert at.money_unreliable is False


def test_money_needs_nonblank_cells():
    assert assess(stats_db(money_cells_nonblank=0, money_cells_unparsed=9)).money_unreliable is False


def test_order_structure_fires_on_any_positive_key():
    for key in ("product_rows_without_order", "annotation_keys_unresolved", "tables_skipped_missing_order_id"):
        flags = assess(stats_db(**{key: 1}))
        assert flags.order_structure_broken is True, key
        assert any(r.startswith("order_structure_broken:") and key in r for r in flags.reasons)


def test_order_structure_does_not_fire_at_zero():
    flags = assess(stats_db(
        product_rows_without_order=0, annotation_keys_unresolved=0, tables_skipped_missing_order_id=0,
    ))
    assert flags.order_structure_broken is False


def test_status_fires_above_threshold_and_not_at_it():
    rows = [(f"o{i}", "2026-01-05", None if i < 6 else "completed") for i in range(100)]
    conn = sqlite3.connect(":memory:")
    add_orders(conn, rows)
    flags = assess(conn)
    assert flags.status_unreliable is True
    assert any(r.startswith("status_unreliable:") for r in flags.reasons)

    at_rows = [(f"o{i}", "2026-01-05", None if i < 5 else "completed") for i in range(100)]
    conn = sqlite3.connect(":memory:")
    add_orders(conn, at_rows)
    assert assess(conn).status_unreliable is False


def test_status_needs_orders_rows():
    conn = sqlite3.connect(":memory:")
    add_orders(conn, [])
    assert assess(conn).status_unreliable is False


def test_payments_never_fire_when_payments_is_empty():
    conn = sqlite3.connect(":memory:")
    add_orders(conn, [(f"o{i}", "2026-01-05", "completed") for i in range(10)])
    add_payments(conn, [])
    assert assess(conn).payments_unlinked is False


def test_payments_fire_above_threshold_and_not_at_it():
    conn = sqlite3.connect(":memory:")
    add_orders(conn, [(f"o{i}", "2026-01-05", "completed") for i in range(10)])
    add_payments(conn, [f"o{i}" for i in range(8)])  # 2 of 10 unlinked: exactly at the threshold
    assert assess(conn).payments_unlinked is False

    conn = sqlite3.connect(":memory:")
    add_orders(conn, [(f"o{i}", "2026-01-05", "completed") for i in range(10)])
    add_payments(conn, [f"o{i}" for i in range(7)])  # 3 of 10 unlinked
    flags = assess(conn)
    assert flags.payments_unlinked is True
    assert any(r.startswith("payments_unlinked:") for r in flags.reasons)


def test_undated_orders_are_not_counted_for_payments():
    conn = sqlite3.connect(":memory:")
    add_orders(conn, [
        ("o1", "2026-01-05", "completed"), ("o2", "2026-01-06", "completed"),
        ("u1", None, "completed"), ("u2", None, "completed"), ("u3", None, "completed"),
    ])
    add_payments(conn, ["o1", "o2"])
    assert assess(conn).payments_unlinked is False


def test_missing_build_stats_gives_no_build_stats_flags():
    conn = sqlite3.connect(":memory:")
    add_orders(conn, [(f"o{i}", "2026-01-05", "completed") for i in range(10)])
    add_payments(conn, [f"o{i}" for i in range(10)])
    flags = assess(conn)
    assert flags.qty_unreliable is False
    assert flags.money_unreliable is False
    assert flags.order_structure_broken is False
    assert flags.reasons == []


def test_empty_database_gives_no_flags():
    assert no_flags(assess(sqlite3.connect(":memory:")))


def test_missing_keys_and_unreadable_values_do_not_fire():
    flags = assess(stats_db(qty_cells_nonblank=100, money_cells_unparsed="many", money_cells_nonblank=100))
    assert flags.qty_unreliable is False
    assert flags.money_unreliable is False
    assert flags.order_structure_broken is False


def test_negative_stat_is_unknown():
    assert assess(stats_db(qty_cells_nonblank=100, qty_cells_unparsed=-4)).qty_unreliable is False


def test_zero_unresolved_annotation_keys_do_not_block_orders():
    conn = stats_db(annotation_keys_unresolved=0, product_rows_without_order=0, tables_skipped_missing_order_id=0)
    flags = assess(conn)
    assert flags.order_structure_broken is False
    assert flags.reasons == []


def test_unresolved_annotation_key_blocks_orders_with_its_flag_name():
    flags = assess(stats_db(annotation_keys_unresolved=1))
    assert flags.order_structure_broken is True
    assert flags.reasons == ["order_structure_broken: annotation_keys_unresolved is 1"]
