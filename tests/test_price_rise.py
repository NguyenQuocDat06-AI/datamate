import sqlite3
from datetime import date

from bizreason.semantic.compiler import Intent
from bizreason.semantic.solver import SemanticSolver


def _solver(rows):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE price_changes (product_id TEXT, product_name TEXT, name_key TEXT, date TEXT, "
                "old_price REAL, new_price REAL)")
    con.executemany("INSERT INTO price_changes VALUES (?,?,?,?,?,?)", rows)
    return SemanticSolver(None, None, con)


def _intent():
    return Intent(measure="qty_base_sold", shape="compare_periods",
                  filters=[{"dim": "product", "value": "Alpha chai", "exclude": False}])


def test_first_rise_to_the_named_price_sets_both_windows():
    solver = _solver([("1", "Alpha chai", "alpha chai", "2025-01-01", None, 90000),
                      ("1", "Alpha chai", "alpha chai", "2025-03-01", 90000, 97500),
                      ("1", "Alpha chai", "alpha chai", "2025-05-01", 97500, 90000),
                      ("1", "Alpha chai", "alpha chai", "2025-07-01", 90000, 97500)])
    target, comparison = solver._price_rise_windows(
        "Alpha chai lên giá 97.500đ, so 14 ngày kể từ ngày tăng với 14 ngày liền trước", _intent())
    assert (target.start, target.end_exclusive) == (date(2025, 3, 1), date(2025, 3, 15))
    assert (comparison.start, comparison.end_exclusive) == (date(2025, 2, 15), date(2025, 3, 1))


def test_no_rise_to_that_price_gives_no_window():
    solver = _solver([("1", "Alpha chai", "alpha chai", "2025-01-01", None, 97500)])
    assert solver._price_rise_windows("Alpha chai lên giá 97.500đ, so 7 ngày với 7 ngày trước", _intent()) == []
