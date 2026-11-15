"""Shop knowledge card: README parsing, column descriptions, links and per-question selection."""
import json
import sqlite3

README = """# Shop sample

A small bike shop that sells, rents on instalments and repairs.

## Files
- `Ledger.xlsx` (xlsx)
- `Plans.xlsx` (xlsx)

## Columns in `Ledger.xlsx`
- **Ngày bán**: sale date
- **Voucher code**: code of the voucher the customer used (blank: none)
- **Ghi chú**: free note of the order

## Columns in `Plans.xlsx`
- **Contract No**: instalment contract number
- **Order Ref**: the order the contract pays for
- **Due On**: date the instalment is due
- **Paid On**: date the instalment was paid (blank: not yet)
- **Amount**: money of the instalment

## Columns in `Warranty.xlsx`
- **Ticket**: warranty ticket
- **Claim Date**: date of the claim
- **Order Ref**: the order under warranty

## Columns in `Specs.xlsx`
- **Item**: product name
- **Brand**: maker of the product

## Conventions
- Data runs to the end of March.
- **Instalments**: an instalment is overdue when its due date has passed and it is not paid.
"""


def build_shop(path=":memory:"):
    """A small synthetic shop layer (no real shop's names or values); test_plan.py uses it too."""
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, customer_key TEXT, customer_name TEXT,
                         employee TEXT, revenue REAL, outstanding REAL, x_voucher TEXT, x_ghi_chu TEXT);
    CREATE TABLE lines (order_id TEXT, date TEXT, status TEXT, customer_key TEXT, product_name TEXT, category TEXT,
                        qty REAL, amount REAL, x_cat_size TEXT);
    CREATE TABLE deliveries (order_id TEXT, date TEXT, trip INTEGER, qty REAL);
    CREATE TABLE purchases (purchase_id TEXT, date TEXT, supplier_name TEXT, product_name TEXT, qty REAL,
                            amount REAL, cancelled INTEGER);
    CREATE TABLE payments (order_id TEXT, date TEXT, amount REAL, method TEXT);
    CREATE TABLE src_plans_sheet1_ab12 (contract_no TEXT, order_ref TEXT, due_on TEXT, xd_due_on TEXT, paid_on TEXT,
                                        xd_paid_on TEXT, amount TEXT, xn_amount REAL);
    CREATE TABLE src_warranty_sheet1_cd34 (ticket TEXT, claim_date TEXT, xd_claim_date TEXT, order_ref TEXT);
    CREATE TABLE src_specs_sheet1_ef56 (item TEXT, brand TEXT);
    CREATE TABLE column_catalog (table_name TEXT, column_name TEXT, source_table TEXT, source_header TEXT,
                                 role TEXT, value_type TEXT, distinct_count INTEGER, sample_values TEXT,
                                 join_key_to TEXT);
    CREATE TABLE shop_docs (key TEXT, text TEXT);
    """)
    orders = [
        ("o1", "2026-01-05", "completed", "c1", "Ann Lee", "Kim", 1000.0, 0, None, None),
        ("o2", "2026-01-10", "completed", "c1", "Ann Lee", "Kim", 2500.0, 500, "V10", None),
        ("o3", "2026-01-12", "cancelled", "c2", "Bob Ray", "Lan", 700.0, 0, None, "warranty visit"),
        ("o4", "2026-01-20", "completed", "c2", "Bob Ray", "Lan", 1500.5, 1500.5, None, None),
        ("o5", "2026-01-25", "completed", "c3", "Cy Dao", "Kim", 300.0, 0, None, None),
        ("o6", "2026-02-03", "completed", "c3", "Cy Dao", "Lan", 900.0, 900, None, None),
        ("o7", "2026-02-14", "completed", "c1", "Ann Lee", "Lan", 1200.0, 0, None, None),
    ]
    con.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)", orders)
    lines = [
        ("o1", "2026-01-05", "completed", "c1", "Road Bike", "bikes", 1, 800, "L"),
        ("o1", "2026-01-05", "completed", "c1", "Bell", "parts", 2, 200, None),
        ("o2", "2026-01-10", "completed", "c1", "Road Bike", "bikes", 2, 2000, None),
        ("o2", "2026-01-10", "completed", "c1", "brake check, chain oil", "service", 1, 500, None),
        ("o3", "2026-01-12", "cancelled", "c2", "Bell", "parts", 1, 700, None),
        ("o4", "2026-01-20", "completed", "c2", "City Bike", "bikes", 1, 1500.5, None),
        ("o5", "2026-01-25", "completed", "c3", "chain oil", "service", 3, 300, None),
        ("o6", "2026-02-03", "completed", "c3", "City Bike", "bikes", 1, 900, None),
        ("o7", "2026-02-14", "completed", "c1", "Bell", "parts", 4, 1200, None),
    ]
    con.executemany("INSERT INTO lines VALUES (?,?,?,?,?,?,?,?,?)", lines)
    con.executemany("INSERT INTO deliveries VALUES (?,?,?,?)", [
        ("o1", "2026-01-06", 1, 1), ("o1", "2026-01-08", 2, 2), ("o2", "2026-01-11", 1, 3),
        ("o4", "2026-01-21", 1, 1), ("o4", "2026-01-23", 2, 0), ("o6", "2026-02-04", 1, 1)])
    plans = [  # several instalments per order
        ("K1", "o2", "2026-02-10 00:00:00", "2026-02-10", "2026-02-09 00:00:00", "2026-02-09", "250", 250.0),
        ("K1", "o2", "2026-03-10 00:00:00", "2026-03-10", None, None, "250", 250.0),
        ("K2", "o4", "2026-02-20 00:00:00", "2026-02-20", "2026-02-25 00:00:00", "2026-02-25", "750", 750.0),
        ("K2", "o4", "2026-03-20 00:00:00", "2026-03-20", None, None, "750.5", 750.5),
        ("K3", "o3", "2026-02-12 00:00:00", "2026-02-12", None, None, "700", 700.0),
        ("K4", "o6", "2026-03-03 00:00:00", "2026-03-03", None, None, "900", 900.0),
    ]
    con.executemany("INSERT INTO src_plans_sheet1_ab12 VALUES (?,?,?,?,?,?,?,?)", plans)
    con.executemany("INSERT INTO src_warranty_sheet1_cd34 VALUES (?,?,?,?)", [
        ("W1", "2026-01-30", "2026-01-30", "o1"), ("W2", "2026-02-20", "2026-02-20", "o4"),
        ("W3", "2026-03-01", "2026-03-01", "o6")])
    con.executemany("INSERT INTO src_specs_sheet1_ef56 VALUES (?,?)", [
        ("Road Bike", "Falcon"), ("City Bike", "Heron"), ("Bell", "Heron")])
    catalog = [
        ("orders", "x_voucher", "ledger_sheet1_aa11", "Voucher code", "other", "code", 1, ["V10"], None),
        ("orders", "x_ghi_chu", "ledger_sheet1_aa11", "Ghi chu", "note", "text", 1, ["warranty visit"], None),
        ("lines", "x_cat_size", "catalog_sheet1_bb22", "Size", "other", "text", 1, ["L"], None),
        ("src_plans_sheet1_ab12", "contract_no", "plans_sheet1_ab12", "Contract No", "other", "code", 4,
         ["K1", "K2"], None),
        ("src_plans_sheet1_ab12", "order_ref", "plans_sheet1_ab12", "Order Ref", "order_id", "code", 4,
         ["o2", "o4"], "orders.order_id"),
        ("src_plans_sheet1_ab12", "due_on", "plans_sheet1_ab12", "DUE ON", "due_at", "date", 6,
         ["2026-02-10 00:00:00"], None),
        ("src_plans_sheet1_ab12", "paid_on", "plans_sheet1_ab12", "Paid On", "paid_at", "date", 2,
         ["2026-02-09 00:00:00"], None),
        ("src_plans_sheet1_ab12", "amount", "plans_sheet1_ab12", "Amount", "installment_amount", "number", 4,
         ["250", "750"], None),
        ("src_warranty_sheet1_cd34", "ticket", "warranty_sheet1_cd34", "Ticket", "other", "code", 3, ["W1"], None),
        ("src_warranty_sheet1_cd34", "claim_date", "warranty_sheet1_cd34", "Claim Date", "other", "date", 3,
         ["2026-01-30"], None),
        ("src_warranty_sheet1_cd34", "order_ref", "warranty_sheet1_cd34", "Order Ref", "order_id", "code", 3,
         ["o1"], "orders.order_id"),
        ("src_specs_sheet1_ef56", "item", "specs_sheet1_ef56", "Item", "product_name", "text", 3, ["Road Bike"],
         None),
        ("src_specs_sheet1_ef56", "brand", "specs_sheet1_ef56", "Brand", "other", "text", 2, ["Falcon", "Heron"],
         None),
    ]
    con.executemany("INSERT INTO column_catalog VALUES (?,?,?,?,?,?,?,?,?)",
                    [(*row[:7], json.dumps(row[7]), row[8]) for row in catalog])
    con.execute("INSERT INTO shop_docs VALUES ('readme', ?)", (README,))
    con.commit()
    return con


# ------------------------------------------------------------------------------------------------------- tests

from bizreason.semantic import knowledge  # noqa: E402
from bizreason.semantic.knowledge import build_card, parse_readme  # noqa: E402


def test_readme_parser_finds_sections_files_and_bold_bullets():
    intro, sections = parse_readme(README)
    assert intro == ["A small bike shop that sells, rents on instalments and repairs."]
    titles = [s.title for s in sections]
    assert titles[0] == "Files" and sections[0].is_file_list
    plans = next(s for s in sections if s.file == "Plans.xlsx")
    assert [b.key for b in plans.bullets][:3] == ["Contract No", "Order Ref", "Due On"]
    assert plans.bullets[3].text == "date the instalment was paid (blank: not yet)"
    conventions = sections[-1]
    assert conventions.file is None
    assert conventions.bullets[0].key is None and conventions.bullets[1].key == "Instalments"


def test_readme_parser_keeps_a_wrapped_line_and_a_header_with_parentheses():
    _, sections = parse_readme("## Cols in `A.csv`\n- **Rate (%)**: share of the\n  total kept\n- plain note\n")
    assert sections[0].bullets[0].key == "Rate (%)"
    assert sections[0].bullets[0].text == "share of the total kept"
    assert sections[0].bullets[1].key is None


def test_card_attaches_readme_descriptions_by_normalized_header_and_file():
    card = build_card(build_shop())
    plans = card.tables["src_plans_sheet1_ab12"]
    assert plans.file == "Plans.xlsx"
    assert plans.columns["due_on"].description == "date the instalment is due"  # header "DUE ON" vs "Due On"
    assert plans.columns["due_on"].samples == ["2026-02-10"]  # dates shown as dates
    assert card.tables["orders"].columns["x_ghi_chu"].description == "free note of the order"  # diacritics
    # the same header in two files: each column takes its own file's description
    assert card.tables["src_warranty_sheet1_cd34"].columns["order_ref"].description == "the order under warranty"
    assert plans.columns["order_ref"].description == "the order the contract pays for"
    # a bullet that names no kept column stays as a file note; a section with no file is a convention
    assert any(n.startswith("(Ledger.xlsx) Ngày bán") for n in card.file_notes)
    assert card.conventions[1].startswith("Instalments: an instalment is overdue")
    assert card.intro == ["A small bike shop that sells, rents on instalments and repairs."]


def test_card_skips_empty_tables_and_unfilled_canonical_columns():
    card = build_card(build_shop())
    assert "purchases" not in card.tables and "payments" not in card.tables
    orders = card.tables["orders"].columns
    assert orders["revenue"].core and orders["revenue"].gloss == "order revenue"
    assert "channel" not in orders  # not a column of this layer
    assert orders["employee"].samples == ["Kim", "Lan"] or set(orders["employee"].samples) == {"Kim", "Lan"}


def test_links_by_order_id_and_by_product_with_uniqueness():
    card = build_card(build_shop())
    plans = card.link("src_plans_sheet1_ab12", "order")
    assert plans.column == "order_ref" and not plans.unique  # several instalments per order
    warranty = card.link("src_warranty_sheet1_cd34", "order")
    assert warranty.column == "order_ref" and warranty.unique
    specs = card.link("src_specs_sheet1_ef56", "product")
    assert specs.column == "item" and specs.kind == "product_name" and specs.unique
    assert card.link("lines", "order").column == "order_id" and not card.link("lines", "order").unique
    assert card.link("deliveries", "order") is not None


def test_link_is_detected_from_values_without_a_catalog_join_key():
    con = build_shop()
    con.execute("UPDATE column_catalog SET join_key_to = NULL")
    card = build_card(con)
    assert card.link("src_plans_sheet1_ab12", "order").column == "order_ref"
    con.execute("CREATE TABLE src_misc (code TEXT, label TEXT)")
    con.executemany("INSERT INTO src_misc VALUES (?,?)", [("x1", "a"), ("x2", "b"), ("o1", "c")])
    assert knowledge.detect_links(con, "src_misc", ["code", "label"]) == []  # 1 of 3 is below half


def test_select_shows_core_columns_and_the_tables_the_question_names():
    card = build_card(build_shop())
    text = card.select("how many warranty claims were made this month", budget_tokens=250)
    assert "- orders (" in text and "revenue: order revenue" in text  # core always
    assert "src_warranty_sheet1_cd34" in text
    assert 'claim_date "Claim Date"' in text and 'ticket "Ticket"' in text  # whole small table
    assert "contract_no" not in text  # an unrelated src_ column is left out under a tight budget
    assert "src_plans_sheet1_ab12 (" in text  # but every table's head line stays


def test_select_matches_samples_and_respects_the_budget():
    card = build_card(build_shop())
    text = card.select("revenue of Falcon products", budget_tokens=250)
    assert 'brand "Brand"' in text
    full = card.render()
    small = card.select("revenue", budget_tokens=10)
    assert len(small) < len(full)
    assert "Shop documentation" in card.select("anything", budget_tokens=5000)


def test_render_lists_links_and_shop_documentation():
    text = build_card(build_shop()).render()
    assert "order_ref links orders.order_id (several rows each)" in text
    assert "item links lines.product_name (one row each)" in text
    assert "Shop documentation (README):" in text and "Data runs to the end of March." in text


def test_card_without_docs_or_catalog_still_builds():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, revenue REAL)")
    con.execute("INSERT INTO orders VALUES ('a', '2026-01-01', 'completed', 5)")
    card = build_card(con)
    assert list(card.tables) == ["orders"] and card.conventions == [] and card.file_notes == []
    assert "order revenue" in card.select("q")


def test_a_canonical_column_described_in_the_catalog_takes_its_readme_description():
    con = build_shop()
    con.execute("INSERT INTO column_catalog VALUES ('lines', 'product_name', 'ledger_sheet1_aa11', 'Ngày bán', "
                "'product_name', 'text', 5, '[]', NULL)")
    col = build_card(con).tables["lines"].columns["product_name"]
    assert col.core and col.header == "Ngày bán" and col.description == "sale date"


def add_staff_catalog(con, rows=None):
    con.execute("CREATE TABLE src_staff (person TEXT, rate REAL, category TEXT)")
    con.executemany("INSERT INTO src_staff VALUES (?,?,?)", rows or [
        (" KIM ", 10, "senior"), ("lan", 20, "junior")])
    con.executemany("INSERT INTO column_catalog VALUES (?,?,?,?,?,?,?,?,?)", [
        ("src_staff", "person", "staff_sheet", "Staff", "employee_name", "text", 2, '[]', None),
        ("src_staff", "rate", "staff_sheet", "Rate", "other", "number", 2, '[]', None),
        ("src_staff", "category", "staff_sheet", "Category", "other", "text", 2, '[]', None)])


def test_dimension_links_require_roles_and_observed_overlap_and_render_the_target():
    db = build_shop()
    add_staff_catalog(db)
    card = build_card(db)
    link = card.link("src_staff", "employee_name")
    assert link.column == "person" and link.share == 1 and link.unique
    assert "person links orders.employee (one row each)" in card.render()
    db.execute("UPDATE column_catalog SET role='other' WHERE table_name='src_staff'")
    assert build_card(db).link("src_staff", "employee_name") is None
    db.execute("UPDATE column_catalog SET role='employee_name' WHERE table_name='src_staff' AND column_name='person'")
    db.execute("UPDATE src_staff SET person='Unrelated Person'")
    assert build_card(db).link("src_staff", "employee_name") is None


def test_dimension_links_do_not_treat_a_numeric_attribute_as_an_identifier():
    db = build_shop()
    add_staff_catalog(db)
    db.execute("UPDATE orders SET order_id=CASE WHEN employee='Kim' THEN '10' ELSE '20' END")
    links = build_card(db).tables["src_staff"].links
    assert [(link.column, link.kind) for link in links] == [("person", "employee_name")]


def test_equally_supported_dimension_key_columns_are_ambiguous():
    db = build_shop()
    add_staff_catalog(db)
    db.execute("ALTER TABLE src_staff ADD COLUMN alternate TEXT")
    db.execute("UPDATE src_staff SET alternate=person")
    db.execute("INSERT INTO column_catalog VALUES ('src_staff','alternate','staff_sheet','Alternate',"
               "'employee_name','text',2,'[]',NULL)")
    assert build_card(db).link("src_staff", "employee_name") is None


def test_dimension_uniqueness_uses_normalized_names():
    db = build_shop()
    add_staff_catalog(db, [("Kim", 10, "senior"), (" KIM ", 15, "senior"), ("Lan", 20, "junior")])
    link = build_card(db).link("src_staff", "employee_name")
    assert link is not None and not link.unique


def test_customer_code_and_name_links_use_their_own_comparison_rules():
    db = build_shop()
    db.execute("CREATE TABLE src_customers (code TEXT, person TEXT, tier TEXT)")
    db.executemany("INSERT INTO src_customers VALUES (?,?,?)", [
        ("c1", " ANN LEE ", "gold"), ("c2", "bob ray", "silver")])
    db.executemany("INSERT INTO column_catalog VALUES (?,?,?,?,?,?,?,?,?)", [
        ("src_customers", "code", "customer_sheet", "Code", "customer_id", "code", 2, '[]', None),
        ("src_customers", "person", "customer_sheet", "Name", "customer_name", "text", 2, '[]', None)])
    card = build_card(db)
    assert card.link("src_customers", "customer_id").column == "code"
    assert card.link("src_customers", "customer_name") is None  # a code is the stronger customer key
    db.execute("UPDATE src_customers SET code=upper(code)")
    card = build_card(db)
    assert card.link("src_customers", "customer_id") is None
    assert card.link("src_customers", "customer_name") is not None


def test_single_row_staff_catalog_links_with_a_verified_role_and_matching_name():
    db = build_shop()
    add_staff_catalog(db, [("Kim", 10, "senior")])
    link = build_card(db).link("src_staff", "employee_name")
    assert link is not None and link.unique and link.share == 1


def test_customer_code_and_name_that_contradict_orders_are_not_linked():
    db = build_shop()
    db.execute("CREATE TABLE src_customers (code TEXT, person TEXT)")
    db.executemany("INSERT INTO src_customers VALUES (?,?)", [("c1", "Bob Ray"), ("c2", "Ann Lee")])
    db.executemany("INSERT INTO column_catalog VALUES (?,?,?,?,?,?,?,?,?)", [
        ("src_customers", "code", "customer_sheet", "Code", "customer_id", "code", 2, '[]', None),
        ("src_customers", "person", "customer_sheet", "Name", "customer_name", "text", 2, '[]', None)])
    card = build_card(db)
    assert card.link("src_customers", "customer_id") is None
    assert card.link("src_customers", "customer_name") is None
