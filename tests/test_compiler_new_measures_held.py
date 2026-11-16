"""Measures and attribute gates added from the held-shop diagnosis: new members, average unit price, average total
due, a share whose whole keeps its scope, device-line rankings, and customer attributes a shop does not record.

All data here is synthetic; every definition comes from the rulebook text the tests activate."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from bizreason.contracts import TimeWindow
from bizreason.semantic import rulebook_params, solver, vocabulary
from bizreason.semantic.compiler import (
    Compiler, Intent, NotRecorded, Unsupported, _device_line,
)

NEW_MEMBER_RULE = "| Khách thành viên mới trong tháng | số khách có **Ngày đăng ký** trong tháng đó |"
TOTAL_RULE = "| Nợ một đơn | phần chưa trả của **tổng đơn** (số khách phải trả cả đơn) |"
DEVICE_RULE = "| Dòng máy | phần tên hàng đứng trước dung lượng GB |"

JUNE = TimeWindow(role="target", start=date(2026, 6, 1), end_exclusive=date(2026, 7, 1))


@pytest.fixture(autouse=True)
def _reset_rulebook():
    rulebook_params.set_rulebook_text(None)
    yield
    rulebook_params.set_rulebook_text(None)


def make_db(*, total_due=True, kinds=True) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE orders (order_id TEXT, date TEXT, ts TEXT, status TEXT, customer_key TEXT, "
                "customer_name TEXT, customer_group TEXT, employee TEXT, channel TEXT, payment_method TEXT, "
                "revenue REAL" + (", total_due REAL" if total_due else "") + ")")
    con.execute("CREATE TABLE lines (order_id TEXT, date TEXT, ts TEXT, status TEXT, customer_key TEXT, "
                "customer_name TEXT, product_name TEXT, product_key TEXT, category TEXT, unit TEXT, qty REAL, "
                "qty_base REAL, amount REAL, line_cost REAL, is_item INTEGER"
                + (", product_kind TEXT" if kinds else "") + ")")
    con.execute("CREATE TABLE shop_facts (key TEXT, value INTEGER)")
    return con


def add_order(con, oid, day, lines, *, status="completed", due=None):
    """lines: (product_name, qty, amount, kind[, is_item])"""
    revenue = sum(line[2] for line in lines)
    cols = [r[1] for r in con.execute("PRAGMA table_info(orders)")]
    values = {"order_id": oid, "date": day, "ts": day + " 10:00:00", "status": status, "revenue": revenue,
              "total_due": due}
    con.execute(f"INSERT INTO orders ({','.join(c for c in cols if c in values)}) VALUES "
                f"({','.join('?' for c in cols if c in values)})", [values[c] for c in cols if c in values])
    lcols = [r[1] for r in con.execute("PRAGMA table_info(lines)")]
    for line in lines:
        name, qty, amount, kind = line[:4]
        row = {"order_id": oid, "date": day, "ts": day + " 10:00:00", "status": status, "product_name": name,
               "product_key": name.lower(), "qty": qty, "qty_base": qty, "amount": amount,
               "is_item": line[4] if len(line) > 4 else 1, "product_kind": kind}
        con.execute(f"INSERT INTO lines ({','.join(c for c in lcols if c in row)}) VALUES "
                    f"({','.join('?' for c in lcols if c in row)})", [row[c] for c in lcols if c in row])


def add_catalog(con, rows):
    """rows: (table, column, source_header, role, value_type)"""
    con.execute("CREATE TABLE column_catalog (table_name TEXT, column_name TEXT, source_table TEXT, source_header TEXT, "
                "role TEXT, value_type TEXT, distinct_count INTEGER, sample_values TEXT, join_key_to TEXT, "
                "derived INTEGER DEFAULT 0)")
    con.executemany("INSERT INTO column_catalog (table_name, column_name, source_table, source_header, role, value_type) "
                    "VALUES (?,?,?,?,?,?)", rows)


def customer_file(con, registered, *, role="registered_at", header="Ngày đăng ký"):
    con.execute("CREATE TABLE src_customers (name TEXT, registered TEXT, xd_registered TEXT)")
    for i, day in enumerate(registered):
        con.execute("INSERT INTO src_customers VALUES (?,?,?)", (f"c{i}", day, day))
    add_catalog(con, [("src_customers", "registered", "src", header, role, "date"),
                      ("src_customers", "xd_registered", "src", header, role, "date")])


def value(con, intent, window=JUNE, **kw):
    return Compiler(con).value(intent, window, **kw)


def answer(con, intent, window=JUNE):
    return Compiler(con).answer(intent, [window])


# ------------------------------------------------------------------ vocabulary and registration

def test_new_measures_are_declared_and_registered():
    for m in ("new_member_count", "avg_price_per_unit", "avg_order_total_due"):
        assert m in vocabulary.MEASURES
    assert "device_line" in vocabulary.DIMENSIONS
    assert {"avg_price_per_unit", "avg_order_total_due"} <= solver.MONEY_MEASURES
    assert {"avg_price_per_unit", "avg_order_total_due"} <= solver.ORDER_MEASURES
    assert "avg_price_per_unit" in solver.QUANTITY_MEASURES
    assert "new_member_count" not in solver.POINT_IN_TIME


# ------------------------------------------------------------------ (1) new_member_count

def test_new_members_count_registrations_in_the_window_including_never_bought():
    con = make_db()
    customer_file(con, ["2026-05-31", "2026-06-01", "2026-06-15", "2026-06-30", "2026-07-01", "2024-06-10"])
    rulebook_params.set_rulebook_text(NEW_MEMBER_RULE)
    assert value(con, Intent(measure="new_member_count")) == 3
    assert answer(con, Intent(measure="new_member_count")).value == 3


def test_new_members_read_the_customer_file_not_the_orders():
    con = make_db()
    customer_file(con, ["2026-06-02"])
    add_order(con, "o1", "2026-06-05", [("Sua", 1, 100, None)])
    assert value(con, Intent(measure="new_member_count")) == 1


def test_new_members_without_a_registration_column_are_not_recorded():
    con = make_db()
    con.execute("CREATE TABLE src_customers (name TEXT, phone TEXT)")
    add_catalog(con, [("src_customers", "name", "src", "Tên KH", "customer_name", "text"),
                      ("src_customers", "phone", "src", "SĐT", "customer_phone", "code")])
    with pytest.raises(NotRecorded):
        value(con, Intent(measure="new_member_count"))


def test_new_members_abstain_when_a_header_says_registration_but_no_role_was_given():
    con = make_db()
    customer_file(con, ["2026-06-02"], role="other")
    with pytest.raises(Unsupported) as info:
        value(con, Intent(measure="new_member_count"))
    assert not isinstance(info.value, NotRecorded)


def test_new_members_abstain_without_a_column_catalog():
    with pytest.raises(Unsupported) as info:
        value(make_db(), Intent(measure="new_member_count"))
    assert not isinstance(info.value, NotRecorded)


def test_new_members_need_the_rulebook_to_define_them():
    con = make_db()
    customer_file(con, ["2026-06-02"])
    rulebook_params.set_rulebook_text("| Doanh thu | tổng tiền các dòng hàng |")
    with pytest.raises(Unsupported, match="does not define"):
        value(con, Intent(measure="new_member_count"))
    rulebook_params.set_rulebook_text(NEW_MEMBER_RULE)
    assert value(con, Intent(measure="new_member_count")) == 1


def test_new_members_with_a_filter_abstain_rather_than_widen():
    con = make_db()
    customer_file(con, ["2026-06-02"])
    with pytest.raises(Unsupported):
        value(con, Intent(measure="new_member_count", filters=[{"dim": "customer_group", "value": "vip"}]))


def test_new_members_with_non_iso_dates_abstain():
    con = make_db()
    customer_file(con, ["02/06/2026"])
    with pytest.raises(Unsupported):
        value(con, Intent(measure="new_member_count"))


# ------------------------------------------------------------------ (3) avg_price_per_unit

def sale_db():
    con = make_db()
    add_order(con, "o1", "2026-06-03", [("Phone A 128GB", 2, 20_000_000, "may moi"), ("Cap 1m", 5, 500_000, "phu kien")])
    add_order(con, "o2", "2026-06-10", [("Phone B 64GB", 1, 5_000_001, "may cu"), ("Phi giao", 1, 30_000, "may moi", 0)])
    add_order(con, "o3", "2026-06-12", [("Phone A 128GB", 3, 99_000_000, "may moi")], status="cancelled")
    add_order(con, "o4", "2026-05-12", [("Phone A 128GB", 1, 9_000_000, "may moi")])
    return con


def test_average_price_per_unit_is_money_over_base_units_of_the_scope():
    con = sale_db()
    scope = Intent(measure="avg_price_per_unit", filters=[{"dim": "product_kind", "value": "phu kien", "exclude": True}])
    # valid sales in June, accessories out, the fee line is not an item: (20,000,000 + 5,000,001) / (2 + 1)
    assert value(con, scope) == pytest.approx(25_000_001 / 3)
    assert answer(con, scope).value == 8_333_334  # rounded to the dong, half up


def test_average_price_per_unit_of_everything_includes_accessories():
    con = sale_db()
    got = value(con, Intent(measure="avg_price_per_unit"))
    assert got == pytest.approx((20_000_000 + 500_000 + 5_000_001) / 8)


def test_average_price_per_unit_prefers_base_units_over_sold_units():
    con = make_db()
    add_order(con, "o1", "2026-06-03", [("Water", 2, 100, None)])
    con.execute("UPDATE lines SET qty_base = 24")
    assert value(con, Intent(measure="avg_price_per_unit")) == pytest.approx(100 / 24)


def test_average_price_per_unit_with_nothing_sold_is_empty():
    con = make_db()
    add_order(con, "o1", "2026-05-03", [("Water", 2, 100, None)])
    with pytest.raises(Unsupported):
        answer(con, Intent(measure="avg_price_per_unit"))


def test_average_price_per_unit_ranks_by_group():
    con = sale_db()
    intent = Intent(measure="avg_price_per_unit", shape="argmax_entity", group_by="product")
    assert Compiler(con)._ranked(intent, JUNE)[0][0] == "Phone A 128GB"


# ------------------------------------------------------------------ (4) avg_order_total_due

def due_db(dues):
    con = make_db()
    for i, due in enumerate(dues):
        add_order(con, f"o{i}", "2026-06-05", [("X", 1, 1000, None)], due=due)
    return con


def test_average_order_total_due_is_the_mean_of_the_orders_totals():
    con = due_db([1000, 2001])
    assert value(con, Intent(measure="avg_order_total_due")) == pytest.approx(1500.5)
    assert answer(con, Intent(measure="avg_order_total_due")).value == 1501  # half up


def test_average_order_total_due_counts_valid_orders_only():
    con = due_db([1000, 3000])
    add_order(con, "oc", "2026-06-06", [("X", 1, 1000, None)], status="cancelled", due=99_999)
    add_order(con, "on", "2026-05-06", [("X", 1, 1000, None)], due=99_999)
    assert value(con, Intent(measure="avg_order_total_due")) == 2000


def test_average_order_total_due_is_not_recorded_when_no_order_has_it():
    with pytest.raises(NotRecorded):
        value(due_db([None, None]), Intent(measure="avg_order_total_due"))


def test_average_order_total_due_is_not_recorded_without_the_column():
    con = make_db(total_due=False)
    add_order(con, "o1", "2026-06-05", [("X", 1, 1000, None)])
    with pytest.raises(NotRecorded):
        value(con, Intent(measure="avg_order_total_due"))


def test_average_order_total_due_abstains_when_some_orders_lack_the_total():
    with pytest.raises(Unsupported) as info:
        value(due_db([1000, None]), Intent(measure="avg_order_total_due"))
    assert not isinstance(info.value, NotRecorded)


def test_average_order_total_due_with_a_line_filter_abstains():
    con = due_db([1000])
    intent = Intent(measure="avg_order_total_due", filters=[{"dim": "product_kind", "value": "may cu"}])
    with pytest.raises(Unsupported):
        value(con, intent)


def test_average_order_total_due_needs_a_rulebook_that_defines_the_whole_order_total():
    con = due_db([1000])
    rulebook_params.set_rulebook_text("| Doanh thu | tổng thành tiền |")
    with pytest.raises(Unsupported, match="does not define"):
        value(con, Intent(measure="avg_order_total_due"))
    rulebook_params.set_rulebook_text(TOTAL_RULE)
    assert value(con, Intent(measure="avg_order_total_due")) == 1000
    rulebook_params.set_rulebook_text("| Thu máy cũ | **Khách phải trả** là tiền của đơn sau bớt |")
    assert value(con, Intent(measure="avg_order_total_due")) == 1000


# ------------------------------------------------------------------ (5) share_of_total whole

def share_intent(filters):
    return Intent(measure="revenue", shape="share_of_total", filters=filters)


def kind(value, exclude=False):
    return {"dim": "product_kind", "value": value, "exclude": exclude}


def test_share_keeps_an_exclusion_that_sits_beside_its_member_in_the_whole():
    con = sale_db()
    # new devices (20,000,000 + 30,000 fee line is also "may moi") over everything but accessories
    got = answer(con, share_intent([kind("may moi"), kind("phu kien", True)]))
    part = 20_000_000 + 30_000
    whole = part + 5_000_001
    assert got.type == "percent" and got.value == round(100.0 * part / whole, 2)


def test_share_with_a_lone_exclusion_keeps_the_old_whole():
    con = sale_db()
    got = answer(con, share_intent([kind("phu kien", True)]))
    part = 20_000_000 + 5_000_001 + 30_000
    whole = part + 500_000
    assert got.value == round(100.0 * part / whole, 2)


def test_share_with_only_a_member_keeps_the_old_whole():
    con = sale_db()
    got = answer(con, share_intent([kind("may cu")]))
    assert got.value == round(100.0 * 5_000_001 / (20_000_000 + 500_000 + 5_000_001 + 30_000), 2)


def test_share_scope_only_keeps_exclusions_of_a_dimension_that_has_a_member():
    f = Compiler._share_scope
    assert f(share_intent([kind("a"), kind("b", True)])) == [kind("b", True)]
    assert f(share_intent([kind("b", True)])) == []
    assert f(share_intent([kind("a")])) == []
    other = {"dim": "channel", "value": "web", "exclude": True}
    assert f(share_intent([kind("a"), other])) == []


# ------------------------------------------------------------------ (6) device lines

@pytest.mark.parametrize("name, line", [
    ("iPhone 15 Pro Max 256GB Đen", "iPhone 15 Pro Max"),
    ("iPhone 12 64gb cu 98%", "iPhone 12"),
    ("Galaxy A06 4GB/64GB", "Galaxy A06"),
    ("Nokia 105 4G", "Nokia 105 4G"),
    ("Laptop 1TB", "Laptop"),
    ("Cap 1m", "Cap 1m"),
])
def test_device_line_is_the_name_before_the_capacity(name, line):
    assert _device_line(name) == line


def test_device_line_of_nothing():
    assert _device_line(None) is None


def phone_db():
    con = make_db()
    add_order(con, "o1", "2026-06-03", [
        ("iPhone 15 128GB Den", 3, 3_000, "may moi"), ("IPHONE 15 256GB Trang", 2, 2_500, "may moi"),
        ("Galaxy S24 256GB", 4, 4_000, "may moi"), ("Nokia 105 4G", 1, 100, "may moi"),
        ("Cap Type-C 1m", 50, 500, "phu kien"), ("Op lung", 40, 400, "phu kien"),
    ])
    add_order(con, "o2", "2026-06-04", [("iPhone 12 64GB cu", 1, 900, "may cu")])
    return con


def ranked_lines(con, measure="qty_base_sold"):
    intent = Intent(measure=measure, shape="top_k", group_by="device_line", top_k=10)
    return Compiler(con)._ranked(intent, JUNE)


def test_device_lines_group_across_capacities_ignoring_case_and_leave_accessories_out():
    rulebook_params.set_rulebook_text(DEVICE_RULE)
    rows = ranked_lines(phone_db())
    assert [(str(name).lower(), score) for name, score in rows] == [
        ("iphone 15", 5), ("galaxy s24", 4), ("iphone 12", 1), ("nokia 105 4g", 1)]


def test_device_lines_by_revenue():
    rulebook_params.set_rulebook_text(DEVICE_RULE)
    rows = ranked_lines(phone_db(), "revenue")
    assert str(rows[0][0]).lower() == "iphone 15" and rows[0][1] == 5_500


def test_device_line_ranking_needs_the_rulebook_to_define_a_device_line():
    rulebook_params.set_rulebook_text("| Doanh thu | tổng |")
    with pytest.raises(Unsupported, match="device line"):
        ranked_lines(phone_db())


def test_device_line_ranking_needs_product_kinds():
    con = make_db(kinds=False)
    add_order(con, "o1", "2026-06-03", [("iPhone 15 128GB", 1, 10, None)])
    with pytest.raises(Unsupported):
        ranked_lines(con)


def test_device_line_ranking_is_unsupported_when_no_kind_holds_devices():
    con = make_db()
    add_order(con, "o1", "2026-06-03", [("Cap 1m", 1, 10, "phu kien")])
    with pytest.raises(Unsupported):
        ranked_lines(con)


def test_plain_product_ranking_is_unchanged_by_the_device_line_path():
    rows = Compiler(phone_db())._ranked(Intent(measure="qty_base_sold", shape="top_k", group_by="product", top_k=3), JUNE)
    assert rows[0] == ("Cap Type-C 1m", 50)


# ------------------------------------------------------------------ (2) attributes a shop does not record

def customers_db(*, gender_header=None, readme=None):
    con = make_db()
    con.execute("UPDATE orders SET customer_key=customer_key")
    for i, name in enumerate(["An", "Binh", "Nam"]):
        add_order(con, f"o{i}", "2026-06-05", [("X", 1, 100, None)])
        con.execute("UPDATE orders SET customer_key=?, customer_name=? WHERE order_id=?", (f"k{i}", name, f"o{i}"))
        con.execute("UPDATE lines SET customer_key=?, customer_name=? WHERE order_id=?", (f"k{i}", name, f"o{i}"))
    rows = [("src_customers", "name", "src", "Tên KH", "customer_name", "text")]
    if gender_header:
        rows.append(("src_customers", "gt", "src", gender_header, "other", "text"))
    add_catalog(con, rows)
    con.execute("CREATE TABLE shop_docs (key TEXT, text TEXT)")
    if readme:
        con.execute("INSERT INTO shop_docs VALUES ('README', ?)", (readme,))
    return con


def customer_filter(text):
    return Intent(measure="revenue", filters=[{"dim": "customer", "value": text, "exclude": False}])


def test_a_gender_word_for_a_shop_that_records_no_gender_is_not_recorded():
    con = Compiler(customers_db())
    with pytest.raises(NotRecorded):
        con.resolve_filter("customer", "khách nữ")
    with pytest.raises(NotRecorded):
        con.resolve_filter("customer", "Nu")


def test_a_gender_word_abstains_when_a_column_records_gender():
    with pytest.raises(Unsupported) as info:
        Compiler(customers_db(gender_header="Giới tính")).resolve_filter("customer", "nữ")
    assert not isinstance(info.value, NotRecorded)


def test_a_gender_word_abstains_when_the_readme_talks_about_gender():
    with pytest.raises(Unsupported) as info:
        Compiler(customers_db(readme="Cột giới tính ghi nam/nữ")).resolve_filter("customer", "nu")
    assert not isinstance(info.value, NotRecorded)


def test_a_customer_really_named_like_a_gender_word_still_resolves():
    predicate, values, _ = Compiler(customers_db()).resolve_filter("customer", "Nam")
    assert "customer_key IN" in predicate and values == ["k2"]


def test_an_unresolved_name_that_is_not_a_gender_word_stays_an_unresolved_customer():
    with pytest.raises(Unsupported, match="unresolved customer") as info:
        Compiler(customers_db()).resolve_filter("customer", "Trường Chinh")
    assert not isinstance(info.value, NotRecorded)


def test_gender_filter_flows_to_the_answer_as_not_recorded():
    con = customers_db()
    with pytest.raises(NotRecorded):
        answer(con, customer_filter("nữ"))


@pytest.mark.parametrize("address, orders, expected", [
    (0, 1, NotRecorded), (1, 1, Unsupported), (0, 0, Unsupported), (None, None, Unsupported),
])
def test_a_location_is_not_recorded_only_when_the_shop_records_no_address(address, orders, expected):
    con = make_db()
    add_order(con, "o1", "2026-06-05", [("X", 1, 100, None)])
    for key, v in (("has_address", address), ("has_orders", orders)):
        if v is not None:
            con.execute("INSERT INTO shop_facts VALUES (?, ?)", (key, v))
    intent = Intent(measure="revenue", location="Lạc Long Quân")
    with pytest.raises(expected) as info:
        answer(con, intent)
    assert isinstance(info.value, NotRecorded) is (expected is NotRecorded)
