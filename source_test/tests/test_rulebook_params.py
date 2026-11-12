"""Rulebook parameters: defaults, extraction with a fake model, failure policy, cache, and the three consumers."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from bizreason.contracts import BusinessContext, ColumnMeta, SourceRef, TableMeta, TimeWindow
from bizreason.semantic import layer, rulebook_params as rp
from bizreason.semantic.compiler import Compiler, Intent, _cancelled_sql, _valid_sale_sql
from bizreason.semantic.rulebook_params import DEFAULTS, active, set_active
from bizreason.temporal import LunarCalendar
from bizreason.utils import normalize_lookup

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_CALENDAR = ROOT / "data" / "public_test" / "calendar.csv"
DATA_ROOT = Path(os.environ.get("DATASET_CACHE_ROOT", "/home/ubuntu/datamate/source_test")) / "data"
REAL_RULEBOOKS = [DATA_ROOT / "train" / "rulebook.md", DATA_ROOT / "public_test" / "rulebook.md"]

TAKEN_ALL = [
    "shift_windows", "shift_baseline", "shift_afternoon", "walk_in_names", "service_fee_names",
    "debt_method_tokens", "non_debt_method_tokens", "tet_start", "tet_end",
    "valid_sale_statuses", "cancelled_statuses",
]

RULEBOOK_TEXT = (
    "# Quy tac\n"
    "## 2. Thời gian\n"
    "| Cách nói | Nghĩa |\n"
    "|---|---|\n"
    "| Tết năm Y | từ 23 tháng Chạp đến mùng 10 tháng Giêng |\n"
    "| Lãi ròng một tháng | lãi gộp trừ mọi khoản chi phí |\n"
)


class FakeModel:
    """Records every call. Returns `reply`, or raises `error` when it is set."""

    def __init__(self, reply: Any = None, error: Exception | None = None) -> None:
        self.reply = {} if reply is None else reply
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> Any:
        self.calls.append({"system": system, "prompt": prompt, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.reply


def reply(**changes: Any) -> dict[str, Any]:
    """A reply that states the same values as today's constants, with the Vietnamese wording a rulebook uses."""
    base: dict[str, Any] = {
        "shift_windows": {"sáng": ["06:00:00", "13:59:59"], "chiều": ["14:00:00", "21:59:59"]},
        "shift_baseline": "sáng",
        "shift_afternoon": "chiều",
        "walk_in_names": ["khách lẻ", "khách vãng lai"],
        "service_fee_names": ["phí phục vụ"],
        "debt_method_tokens": ["ghi nợ"],
        "non_debt_method_tokens": ["COD", "giao hàng thu tiền", "sàn TMĐT"],
        "tet_start": {"lunar_month": 12, "lunar_day": 23, "year_offset": -1},
        "tet_end": {"lunar_month": 1, "lunar_day": 10},
        "valid_sale_statuses": ["completed", "shipping"],
        "cancelled_statuses": ["cancelled"],
    }
    base.update(changes)
    return base


@pytest.fixture(autouse=True)
def restore_active_params():
    before = active()
    yield
    set_active(before)


# Defaults.

def test_defaults_are_todays_constants_written_out() -> None:
    assert DEFAULTS.shift_windows == {
        "sang": ("06:00:00", "13:59:59"), "chieu": ("14:00:00", "21:59:59"),
        "morning": ("06:00:00", "13:59:59"), "afternoon": ("14:00:00", "21:59:59"),
    }
    assert DEFAULTS.shift_baseline == "sang"
    assert DEFAULTS.shift_afternoon == "chieu"
    assert DEFAULTS.walk_in_names == ("khach le", "khach vang lai")
    assert DEFAULTS.service_fee_names == ("phi phuc vu",)
    assert DEFAULTS.debt_method_tokens == ("ghi no", "debt")
    assert DEFAULTS.non_debt_method_tokens == ("cod", "giao hang thu tien", "san", "marketplace", "tmdt")
    assert DEFAULTS.tet_start == (12, 23, -1)
    assert DEFAULTS.tet_end == (1, 10)
    assert DEFAULTS.valid_sale_statuses == ("completed", "shipping")
    assert DEFAULTS.cancelled_statuses == ("cancelled",)


def test_canonical_order_statuses_are_the_layers_canonical_status() -> None:
    # rulebook_params cannot import layer (layer imports it), so the tuple is copied and pinned here.
    assert rp.CANONICAL_ORDER_STATUSES == layer.CANONICAL_STATUS


def test_active_is_defaults_until_set() -> None:
    set_active(DEFAULTS)
    assert active() is DEFAULTS


def test_set_active_rejects_other_types() -> None:
    with pytest.raises(TypeError):
        set_active({"shift_baseline": "sang"})  # type: ignore[arg-type]


# Extraction.

def test_one_model_call_with_timeout_and_the_relevant_lines_only() -> None:
    model = FakeModel()
    rp.extract_rulebook_params(RULEBOOK_TEXT, model)
    assert len(model.calls) == 1
    call = model.calls[0]
    assert call["timeout"] == rp.EXTRACT_TIMEOUT
    assert "Tết năm Y" in call["prompt"]
    assert "Lãi ròng" not in call["prompt"]


def test_a_rulebook_stating_todays_values_reproduces_the_defaults() -> None:
    params, taken = rp.extract_rulebook_params(RULEBOOK_TEXT, FakeModel(reply()))
    assert taken == TAKEN_ALL
    assert params.shift_windows == DEFAULTS.shift_windows
    assert params.shift_baseline == DEFAULTS.shift_baseline
    assert params.shift_afternoon == DEFAULTS.shift_afternoon
    assert params.tet_start == DEFAULTS.tet_start and params.tet_end == DEFAULTS.tet_end
    assert params.service_fee_names == DEFAULTS.service_fee_names
    assert params.walk_in_names == DEFAULTS.walk_in_names


def test_changed_shift_windows_are_taken_and_english_aliases_follow_the_baseline() -> None:
    changed = reply(shift_windows={"sáng": ["07:00:00", "14:59:59"], "chiều": ["15:00:00", "22:59:59"]})
    params, taken = rp.extract_rulebook_params(RULEBOOK_TEXT, FakeModel(changed))
    assert params.shift_windows == {
        "sang": ("07:00:00", "14:59:59"), "chieu": ("15:00:00", "22:59:59"),
        "morning": ("07:00:00", "14:59:59"), "afternoon": ("15:00:00", "22:59:59"),
    }
    assert "shift_windows" in taken


def test_null_field_falls_back_to_default_and_is_not_taken() -> None:
    params, taken = rp.extract_rulebook_params(RULEBOOK_TEXT, FakeModel(reply(walk_in_names=None)))
    assert params.walk_in_names == DEFAULTS.walk_in_names
    assert "walk_in_names" not in taken
    assert "service_fee_names" in taken


def test_extra_walk_in_name_is_normalized_and_quotes_are_removed() -> None:
    params, _ = rp.extract_rulebook_params(RULEBOOK_TEXT, FakeModel(reply(walk_in_names=["Khách' lẻ", "Khách vãng lai"])))
    assert params.walk_in_names == ("khach le", "khach vang lai")


@pytest.mark.parametrize(
    "bad_windows",
    [
        {"sáng": ["06:00:00", "25:00:00"], "chiều": ["14:00:00", "21:59:59"]},  # hour out of range
        {"sáng": ["06:00", "13:59:59"], "chiều": ["14:00:00", "21:59:59"]},  # no seconds
        {"sáng": ["15:00:00", "14:00:00"], "chiều": ["14:00:00", "21:59:59"]},  # starts after it ends
        {"sáng": ["06:00:00", "13:59:59"]},  # one shift only
        {"sáng": "06:00:00-13:59:59", "chiều": ["14:00:00", "21:59:59"]},  # not a pair
    ],
)
def test_invalid_shift_times_fall_back_the_whole_shift_group(bad_windows, caplog) -> None:
    caplog.set_level(logging.WARNING, logger=rp.log.name)
    params, taken = rp.extract_rulebook_params(RULEBOOK_TEXT, FakeModel(reply(shift_windows=bad_windows)))
    assert params.shift_windows == DEFAULTS.shift_windows
    assert params.shift_baseline == DEFAULTS.shift_baseline
    assert params.shift_afternoon == DEFAULTS.shift_afternoon
    assert not {"shift_windows", "shift_baseline", "shift_afternoon"} & set(taken)
    assert "shift_windows/shift_baseline/shift_afternoon" in caplog.text


def test_baseline_that_is_not_a_shift_makes_the_group_invalid() -> None:
    params, taken = rp.extract_rulebook_params(RULEBOOK_TEXT, FakeModel(reply(shift_baseline="toi")))
    assert params.shift_windows == DEFAULTS.shift_windows
    assert "shift_baseline" not in taken


def test_tet_window_with_start_after_end_in_the_same_year_is_invalid() -> None:
    bad = reply(tet_start={"lunar_month": 1, "lunar_day": 10, "year_offset": 0},
                tet_end={"lunar_month": 1, "lunar_day": 5})
    params, taken = rp.extract_rulebook_params(RULEBOOK_TEXT, FakeModel(bad))
    assert params.tet_start == DEFAULTS.tet_start and params.tet_end == DEFAULTS.tet_end
    assert "tet_start" not in taken


@pytest.mark.parametrize("bad_day", [0, 31, 1.5, True, "23"])
def test_tet_fields_must_be_whole_lunar_numbers(bad_day) -> None:
    bad = reply(tet_start={"lunar_month": 12, "lunar_day": bad_day, "year_offset": -1})
    params, taken = rp.extract_rulebook_params(RULEBOOK_TEXT, FakeModel(bad))
    assert params.tet_start == DEFAULTS.tet_start
    assert "tet_end" not in taken


def test_empty_or_non_list_name_list_falls_back() -> None:
    params, taken = rp.extract_rulebook_params(
        RULEBOOK_TEXT, FakeModel(reply(walk_in_names="khách lẻ", service_fee_names=[]))
    )
    assert params.walk_in_names == DEFAULTS.walk_in_names
    assert params.service_fee_names == DEFAULTS.service_fee_names
    assert "walk_in_names" not in taken and "service_fee_names" not in taken


def test_a_rulebook_that_counts_only_completed_orders_replaces_the_default() -> None:
    # A definition set replaces the default: 'shipping' is dropped, not kept beside 'completed'.
    params, taken = rp.extract_rulebook_params(RULEBOOK_TEXT, FakeModel(reply(valid_sale_statuses=["completed"])))
    assert params.valid_sale_statuses == ("completed",)
    assert "valid_sale_statuses" in taken


def test_status_names_are_normalized_and_kept_in_canonical_order() -> None:
    params, _ = rp.extract_rulebook_params(
        RULEBOOK_TEXT, FakeModel(reply(valid_sale_statuses=[" Shipping", "COMPLETED", "shipping"])),
    )
    assert params.valid_sale_statuses == ("completed", "shipping")


def test_a_rulebook_can_state_cancelled_orders_as_a_set() -> None:
    params, taken = rp.extract_rulebook_params(RULEBOOK_TEXT, FakeModel(reply(cancelled_statuses=["cancelled", "returned"])))
    assert params.cancelled_statuses == ("cancelled", "returned")
    assert "cancelled_statuses" in taken


@pytest.mark.parametrize(
    "field, bad",
    [
        ("valid_sale_statuses", ["completed", "paid"]),  # not a canonical name
        ("valid_sale_statuses", ["completed", "cancelled"]),  # a cancelled order is never a sale
        ("valid_sale_statuses", []),  # empty
        ("valid_sale_statuses", "completed"),  # not a list
        ("valid_sale_statuses", [None]),  # not text
        ("cancelled_statuses", ["cancelled", "shipping"]),  # overlaps the default valid set
        ("cancelled_statuses", ["hoàn thành"]),  # Vietnamese wording is not canonical
        ("cancelled_statuses", []),  # empty
    ],
)
def test_invalid_status_set_falls_back_to_the_default_on_its_own(field, bad) -> None:
    params, taken = rp.extract_rulebook_params(RULEBOOK_TEXT, FakeModel(reply(**{field: bad})))
    assert getattr(params, field) == getattr(DEFAULTS, field)
    assert field not in taken
    # The other status set is still taken from the reply.
    other = "cancelled_statuses" if field == "valid_sale_statuses" else "valid_sale_statuses"
    assert other in taken


def test_status_sets_are_validated_against_each_other() -> None:
    # The cancelled set is checked against the valid set that was taken: 'completed' cannot be both.
    params, taken = rp.extract_rulebook_params(
        RULEBOOK_TEXT, FakeModel(reply(valid_sale_statuses=["completed"], cancelled_statuses=["completed"])),
    )
    assert params.valid_sale_statuses == ("completed",)
    assert params.cancelled_statuses == ("cancelled",)
    assert taken.count("valid_sale_statuses") == 1 and "cancelled_statuses" not in taken


def test_a_reply_that_is_not_an_object_gives_defaults_and_logs(caplog) -> None:
    caplog.set_level(logging.WARNING, logger=rp.log.name)
    params, taken = rp.extract_rulebook_params(RULEBOOK_TEXT, FakeModel(reply=["not", "an", "object"]))
    assert params == DEFAULTS and taken == []
    assert "not a JSON object" in caplog.text


def test_invalid_json_from_the_client_gives_defaults_and_does_not_raise(caplog) -> None:
    caplog.set_level(logging.WARNING, logger=rp.log.name)
    error = json.JSONDecodeError("Expecting value", "not json", 0)
    params, taken = rp.extract_rulebook_params(RULEBOOK_TEXT, FakeModel(error=error))
    assert params == DEFAULTS and taken == []
    assert "parameter call failed" in caplog.text


@pytest.mark.parametrize("error", [TimeoutError("slow"), RuntimeError("endpoint down")])
def test_failed_call_gives_defaults_and_no_raise(error) -> None:
    params, taken = rp.extract_rulebook_params(RULEBOOK_TEXT, FakeModel(error=error))
    assert params == DEFAULTS and taken == []


def test_rulebook_without_cue_lines_makes_no_call() -> None:
    model = FakeModel(reply())
    params, taken = rp.extract_rulebook_params("# Title\nNothing about the parameters here.\n", model)
    assert params == DEFAULTS and taken == []
    assert model.calls == []


# Section selection on the two real rulebooks.

@pytest.mark.parametrize("path", REAL_RULEBOOKS, ids=lambda p: p.parent.name)
def test_selection_keeps_the_cue_rows_of_a_real_rulebook(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"rulebook not available: {path}")
    text = path.read_text(encoding="utf-8")
    selected = rp.relevant_rulebook_text(text)

    # Headings of the sections that carry the parameters are kept with their rows.
    assert "## 2. Thời gian" in selected
    assert "## 4. Công nợ" in selected
    assert "Ca sáng, ca chiều" in selected
    assert "23 tháng Chạp" in selected
    assert "Phí phục vụ" in selected
    assert "Đơn COD, đơn bán qua sàn TMĐT" in selected
    assert "khách vãng lai" in selected
    # The §1 definitions of a sale, a cancelled order and the order status words are kept.
    assert "| Đơn tính vào doanh thu |" in selected
    assert "Đơn đang giao vẫn tính" in selected
    assert "| Đơn bị huỷ |" in selected
    assert "| Trạng thái đơn |" in selected
    # Rows without a cue are not sent.
    assert "Lãi ròng một tháng" not in selected
    assert "Hiệu ứng tăng giá" not in selected
    # The whole call stays short.
    model = FakeModel()
    rp.extract_rulebook_params(text, model)
    assert len(model.calls) == 1
    sent = model.calls[0]["system"] + model.calls[0]["prompt"]
    assert len(sent) < 6000


def test_table_header_is_kept_only_with_a_selected_row() -> None:
    text = "## 5. Kho\n| Luật | Mặc định |\n|---|---|\n| Lãi ròng | không có |\n"
    assert rp.relevant_rulebook_text(text) == ""


# Cache.

def test_cache_is_keyed_by_the_sha256_of_the_rulebook(tmp_path: Path) -> None:
    rulebook = tmp_path / "rulebook.md"
    rulebook.write_text(RULEBOOK_TEXT, encoding="utf-8")
    cache = tmp_path / "out" / "rulebook_params.json"
    first = FakeModel(reply(shift_windows={"sáng": ["07:00:00", "14:59:59"], "chiều": ["15:00:00", "22:59:59"]}))

    params, taken = rp.load_or_extract(rulebook, first, cache)

    stored = json.loads(cache.read_text(encoding="utf-8"))
    assert stored["sha256"] == hashlib.sha256(rulebook.read_bytes()).hexdigest()
    assert stored["taken"] == taken
    assert len(first.calls) == 1

    second = FakeModel(error=RuntimeError("must not be called"))
    cached_params, cached_taken = rp.load_or_extract(rulebook, second, cache)
    assert second.calls == []
    assert cached_params == params and cached_taken == taken


def test_a_changed_rulebook_misses_the_cache(tmp_path: Path) -> None:
    rulebook = tmp_path / "rulebook.md"
    rulebook.write_text(RULEBOOK_TEXT, encoding="utf-8")
    cache = tmp_path / "rulebook_params.json"
    rp.load_or_extract(rulebook, FakeModel(reply()), cache)

    rulebook.write_text(RULEBOOK_TEXT + "| Khách lẻ | khác |\n", encoding="utf-8")
    model = FakeModel(reply())
    rp.load_or_extract(rulebook, model, cache)
    assert len(model.calls) == 1


def test_failed_extraction_is_never_cached(tmp_path: Path) -> None:
    rulebook = tmp_path / "rulebook.md"
    rulebook.write_text(RULEBOOK_TEXT, encoding="utf-8")
    cache = tmp_path / "rulebook_params.json"

    params, taken = rp.load_or_extract(rulebook, FakeModel(error=TimeoutError("slow")), cache)
    assert params == DEFAULTS and taken == []
    assert not cache.exists()

    later = FakeModel(reply())
    rp.load_or_extract(rulebook, later, cache)
    assert len(later.calls) == 1
    assert cache.exists()


def test_corrupt_or_foreign_cache_is_ignored(tmp_path: Path) -> None:
    rulebook = tmp_path / "rulebook.md"
    rulebook.write_text(RULEBOOK_TEXT, encoding="utf-8")
    cache = tmp_path / "rulebook_params.json"
    digest = hashlib.sha256(rulebook.read_bytes()).hexdigest()
    cache.write_text("{not json", encoding="utf-8")
    model = FakeModel(reply())
    rp.load_or_extract(rulebook, model, cache)
    assert len(model.calls) == 1

    cache.write_text(json.dumps({"sha256": digest, "params": {"walk_in_names": "x"}, "taken": []}), encoding="utf-8")
    model = FakeModel(reply())
    rp.load_or_extract(rulebook, model, cache)
    assert len(model.calls) == 1


def test_cache_written_before_the_status_sets_is_re_extracted(tmp_path: Path) -> None:
    rulebook = tmp_path / "rulebook.md"
    rulebook.write_text(RULEBOOK_TEXT, encoding="utf-8")
    cache = tmp_path / "rulebook_params.json"
    rp.load_or_extract(rulebook, FakeModel(reply()), cache)
    stored = json.loads(cache.read_text(encoding="utf-8"))
    del stored["params"]["valid_sale_statuses"], stored["params"]["cancelled_statuses"]
    cache.write_text(json.dumps(stored), encoding="utf-8")

    model = FakeModel(reply(valid_sale_statuses=["completed"]))
    params, _ = rp.load_or_extract(rulebook, model, cache)
    assert len(model.calls) == 1
    assert params.valid_sale_statuses == ("completed",)


# Consumers.

def timed_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.create_function("lookup", 1, lambda v: None if v is None else normalize_lookup(v))
    db.executescript("""
        CREATE TABLE orders (order_id TEXT, date TEXT, ts TEXT, status TEXT, customer_key TEXT,
                             customer_name TEXT, channel TEXT, revenue REAL, total_due REAL);
        CREATE TABLE lines (order_id TEXT, date TEXT, ts TEXT, status TEXT, customer_key TEXT, channel TEXT,
                            product_key TEXT, product_name TEXT, qty REAL, qty_base REAL, amount REAL);
        CREATE TABLE purchases (purchase_id TEXT, date TEXT, supplier_name TEXT, supplier_id TEXT, status TEXT,
                                product_name TEXT, qty REAL, unit_cost REAL, amount REAL, receipt_total REAL,
                                cancelled INTEGER);
    """)
    orders = [
        ("O1", "2026-03-02", "2026-03-02 07:30:00", "completed", "c1", "Lan", "dine", 100, 100),
        ("O2", "2026-03-02", "2026-03-02 13:30:00", "completed", "c1", "Lan", "dine", 50, 50),
        ("O3", "2026-03-02", "2026-03-02 14:30:00", "completed", "c2", "Hoa", "take", 40, 40),
        ("O4", "2026-03-03", "2026-03-03 15:30:00", "completed", "c2", "Hoa", "take", 70, 70),
        ("O5", "2026-03-03", "2026-03-03 06:30:00", "completed", "c3", "Nam", "dine", 20, 20),
    ]
    db.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)", orders)
    db.commit()
    return db


MARCH = TimeWindow(start=date(2026, 3, 1), end_exclusive=date(2026, 4, 1))


def shift(name: str) -> Intent:
    return Intent(measure="revenue", filters=[{"dim": "shift", "value": name}])


def test_shift_filter_uses_the_default_windows_unchanged() -> None:
    # Morning 06:00 to 13:59:59: O1 100, O2 50, O5 20 = 170. Afternoon 14:00 to 21:59:59: O3 40, O4 70 = 110.
    compiler = Compiler(timed_db())
    assert compiler.value(shift("sáng"), MARCH) == 170
    assert compiler.value(shift("chiều"), MARCH) == 110


def test_shift_filter_follows_changed_windows() -> None:
    set_active(replace(DEFAULTS, shift_windows={
        "sang": ("07:00:00", "14:59:59"), "chieu": ("15:00:00", "22:59:59"),
    }))
    # Morning 07:00 to 14:59:59: O1 100, O2 50, O3 40 = 190 (O5 at 06:30 is out). Afternoon: O4 70.
    compiler = Compiler(timed_db())
    assert compiler.value(shift("sáng"), MARCH) == 190
    assert compiler.value(shift("chiều"), MARCH) == 70


def test_shift_filter_from_an_extracted_rulebook_is_used_after_set_active() -> None:
    changed = reply(shift_windows={"sáng": ["07:00:00", "14:59:59"], "chiều": ["15:00:00", "22:59:59"]})
    params, _ = rp.extract_rulebook_params(RULEBOOK_TEXT, FakeModel(changed))
    set_active(params)
    compiler = Compiler(timed_db())
    assert compiler.value(shift("chiều"), MARCH) == 70
    # The English word still finds its shift through the alias.
    assert compiler.value(shift("afternoon"), MARCH) == 70


def test_lunar_tet_window_follows_changed_params() -> None:
    calendar = LunarCalendar(PUBLIC_CALENDAR)
    default = calendar.tet(2025)
    assert (default.start, default.end_exclusive) == (date(2025, 1, 22), date(2025, 2, 8))
    assert default.evidence == ["calendar.csv", "tet:23/12_previous_lunar_year..10/1"]

    set_active(replace(DEFAULTS, tet_start=(12, 20, -1), tet_end=(1, 12)))
    changed = calendar.tet(2025)
    assert (changed.start, changed.end_exclusive) == (date(2025, 1, 19), date(2025, 2, 10))
    assert changed.evidence == ["calendar.csv", "tet:20/12_previous_lunar_year..12/1"]


def status_db() -> sqlite3.Connection:
    """timed_db's five completed March orders (revenue 280), plus one shipping order of 80, one cancelled order of
    60 and one returned order of 30, all on March 4."""
    db = timed_db()
    db.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)", [
        ("O6", "2026-03-04", "2026-03-04 10:00:00", "shipping", "c1", "Lan", "dine", 80, 80),
        ("O7", "2026-03-04", "2026-03-04 11:00:00", "cancelled", "c2", "Hoa", "take", 60, 60),
        ("O8", "2026-03-04", "2026-03-04 12:00:00", "returned", "c3", "Nam", "dine", 30, 30),
    ])
    db.commit()
    return db


def test_status_sql_is_byte_identical_to_the_old_constants_by_default() -> None:
    set_active(DEFAULTS)
    assert _valid_sale_sql() == "status IN ('completed','shipping')"
    assert _cancelled_sql() == "status='cancelled'"


def test_revenue_counts_shipping_orders_by_default() -> None:
    # 280 completed + 80 shipping; the cancelled 60 and the returned 30 are out.
    set_active(DEFAULTS)
    compiler = Compiler(status_db())
    assert compiler.value(Intent(measure="revenue", filters=[]), MARCH) == 360
    assert compiler.value(Intent(measure="order_count", filters=[]), MARCH) == 6


def test_rulebook_that_counts_only_completed_orders_drops_shipping_from_revenue() -> None:
    changed = reply(valid_sale_statuses=["completed"])
    params, _ = rp.extract_rulebook_params(RULEBOOK_TEXT, FakeModel(changed))
    set_active(params)
    assert _valid_sale_sql() == "status='completed'"
    compiler = Compiler(status_db())
    assert compiler.value(Intent(measure="revenue", filters=[]), MARCH) == 280
    assert compiler.value(Intent(measure="order_count", filters=[]), MARCH) == 5
    # avg per order uses the same set: 280 / 5 orders.
    assert compiler.value(Intent(measure="avg_per_order", filters=[]), MARCH) == 56


def test_rulebook_that_counts_a_cancelled_set_changes_cancelled_count_and_rate() -> None:
    # Denominator stays every order placed (8 in March): 'returned' counted as cancelled gives 2 of 8.
    set_active(replace(DEFAULTS, cancelled_statuses=("cancelled", "returned")))
    compiler = Compiler(status_db())
    assert compiler.value(Intent(measure="cancelled_count", filters=[]), MARCH) == 2
    assert compiler.value(Intent(measure="cancel_rate", filters=[]), MARCH) == 25.0


def test_default_cancel_rate_is_one_cancelled_order_of_eight() -> None:
    set_active(DEFAULTS)
    compiler = Compiler(status_db())
    assert compiler.value(Intent(measure="cancelled_count", filters=[]), MARCH) == 1
    assert compiler.value(Intent(measure="cancel_rate", filters=[]), MARCH) == 12.5


def test_debt_exclusion_renders_a_set_with_not_in() -> None:
    from bizreason.semantic.compiler import _status_sql

    assert _status_sql("COALESCE(status, '')", ("cancelled",), negate=True) == "COALESCE(status, '') <> 'cancelled'"
    assert _status_sql("COALESCE(status, '')", ("cancelled", "returned"), negate=True) == (
        "COALESCE(status, '') NOT IN ('cancelled','returned')"
    )


def _layer_context(folder: Path, specs: dict[str, tuple[list[str], list[tuple[Any, ...]]]]) -> BusinessContext:
    """A source database with every table as text plus the ingestion columns, as test_semantic_layer builds them."""
    path = folder / "source.sqlite"
    con = sqlite3.connect(path)
    for name, (columns, rows) in specs.items():
        defs = ", ".join(f'"{c}" TEXT' for c in columns)
        con.execute(f'CREATE TABLE "{name}" ({defs}, __source_row INTEGER, __raw_json TEXT)')
        marks = ",".join("?" * (len(columns) + 2))
        for number, row in enumerate(rows, start=1):
            con.execute(f'INSERT INTO "{name}" VALUES ({marks})', (*row, number, "{}"))
    con.commit()
    con.close()
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


def test_walk_in_names_from_params_drive_the_customer_key(tmp_path: Path) -> None:
    columns = ["order", "name", "item", "amount"]
    specs: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {
        "sales": (columns, [("O1", "Khach Ve", "tea", "100"), ("O2", "Khach Le", "tea", "50"), ("O3", "Lan", "tea", "30")]),
    }
    records = [{
        "columns": columns, "kind": "sales",
        "roles": {"order": "order_id", "name": "customer_name", "item": "product_name", "amount": "line_amount"},
    }]

    def build(name: str) -> list[tuple]:
        folder = tmp_path / name
        folder.mkdir()
        out = folder / "canonical.sqlite"
        layer.build(_layer_context(folder, specs), records, out)
        con = sqlite3.connect(out)
        try:
            return con.execute("SELECT order_id, customer_key FROM orders ORDER BY order_id").fetchall()
        finally:
            con.close()

    default_keys = build("default")
    assert default_keys == [("O1", "khach ve"), ("O2", None), ("O3", "lan")]

    set_active(replace(DEFAULTS, walk_in_names=DEFAULTS.walk_in_names + ("khach ve",)))
    assert build("extended") == [("O1", None), ("O2", None), ("O3", "lan")]
