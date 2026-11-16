"""Value linking: stored values near a question's phrases, from a layer database built here in memory."""

from __future__ import annotations

import json
import random
import sqlite3
import time

import pytest

from bizreason.semantic import value_linking, vocabulary
from bizreason.semantic.value_linking import ValueIndex


def _layer(
    products=(), categories=(), kinds=(), units=(), employees=(), customers=(), channels=(),
    payments=(), groups=(), suppliers=(),
) -> sqlite3.Connection:
    """A layer database with the columns the index reads; each argument lists the values of one column."""
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE lines (order_id, product_key, product_name, category, product_kind, unit, base_unit)")
    connection.execute("CREATE TABLE orders (order_id, employee, customer_name, customer_key, channel, payment_method, customer_group)")
    connection.execute("CREATE TABLE purchases (purchase_id, supplier_name)")
    for i, name in enumerate(products):
        connection.execute("INSERT INTO lines VALUES (?, ?, ?, ?, ?, ?, ?)", (i, name, name, None, None, None, None))
    for i, value in enumerate(categories):
        connection.execute("INSERT INTO lines (order_id, category) VALUES (?, ?)", (i, value))
    for i, value in enumerate(kinds):
        connection.execute("INSERT INTO lines (order_id, product_kind) VALUES (?, ?)", (i, value))
    for i, value in enumerate(units):
        connection.execute("INSERT INTO lines (order_id, unit) VALUES (?, ?)", (i, value))
    for i, value in enumerate(employees):
        connection.execute("INSERT INTO orders (order_id, employee) VALUES (?, ?)", (i, value))
    for i, value in enumerate(customers):
        connection.execute("INSERT INTO orders (order_id, customer_name, customer_key) VALUES (?, ?, ?)", (i, value, i))
    for i, value in enumerate(channels):
        connection.execute("INSERT INTO orders (order_id, channel) VALUES (?, ?)", (i, value))
    for i, value in enumerate(payments):
        connection.execute("INSERT INTO orders (order_id, payment_method) VALUES (?, ?)", (i, value))
    for i, value in enumerate(groups):
        connection.execute("INSERT INTO orders (order_id, customer_group) VALUES (?, ?)", (i, value))
    for i, value in enumerate(suppliers):
        connection.execute("INSERT INTO purchases VALUES (?, ?)", (i, value))
    connection.commit()
    return connection


PRODUCTS = [
    "Zorvian Widget 128GB",
    "Zorvian Widget 256GB",
    "Blarg Tonic 500ml",
    "Kelmar Gizmo Pro",
    "Quadrant Lamp 40W",
]


def test_from_connection_indexes_each_dimension():
    connection = _layer(
        products=PRODUCTS,
        employees=["Quenta Mirelle Ostrand"],
        channels=["Riverside Kiosk"],
        suppliers=["Vantor Supply Co"],
    )
    index = ValueIndex.from_connection(connection)

    found = index.candidates("Zorvian Widget sales by Quenta Mirelle Ostrand at Riverside Kiosk from Vantor Supply Co")

    assert "Zorvian Widget 128GB" in found["product"]
    assert "Quenta Mirelle Ostrand" in found["employee"]
    assert "Riverside Kiosk" in found["channel"]
    assert "Vantor Supply Co" in found["supplier"]


def test_accent_free_and_misspelled_mentions_link():
    index = ValueIndex.from_connection(_layer(products=PRODUCTS))

    assert "Zorvian Widget 128GB" in index.candidates("zorvian widget 128 gb sold last month")["product"]
    assert "Blarg Tonic 500ml" in index.candidates("how many blarg tonik 500 ml")["product"]


def test_accented_stored_value_matches_plain_question():
    index = ValueIndex.from_connection(_layer(products=["Crème Brûlée 200g"]))

    assert index.candidates("creme brulee 200g")["product"] == ["Crème Brûlée 200g"]


def test_partial_name_links_every_variant_of_it():
    index = ValueIndex.from_connection(_layer(products=PRODUCTS))

    product = index.candidates("Zorvian Widget revenue this month")["product"]

    assert set(product[:2]) == {"Zorvian Widget 128GB", "Zorvian Widget 256GB"}
    assert "Kelmar Gizmo Pro" not in product


def test_word_prefix_of_a_longer_name_links_its_family():
    index = ValueIndex.from_connection(_layer(products=[
        "Harbor Kettle 1L Steel", "Harbor Kettle 2L Steel", "Harbor Kettle 2L Copper", "Unrelated Mug 300ml",
    ]))

    product = index.candidates("harbor kettle sales in march")["product"]

    assert {"Harbor Kettle 1L Steel", "Harbor Kettle 2L Steel", "Harbor Kettle 2L Copper"} <= set(product)
    assert "Unrelated Mug 300ml" not in product


def test_person_name_is_not_matched_by_one_word():
    index = ValueIndex.from_connection(_layer(employees=["Quenta Mirelle Ostrand"]))

    assert "employee" not in index.candidates("revenue of ostrand this week")
    assert "Quenta Mirelle Ostrand" in index.candidates("revenue of mirelle ostrand this week")["employee"]


def test_unrelated_question_gives_empty_block():
    index = ValueIndex.from_connection(_layer(products=PRODUCTS, employees=["Quenta Mirelle Ostrand"]))

    assert index.candidates("zq vx pl") == {}
    assert index.block("zq vx pl") == ""


def test_generic_question_words_do_not_link():
    # Count, order and sale words from the vocabulary name no stored value, even when a value is stored under them.
    index = ValueIndex.from_connection(_layer(products=PRODUCTS, groups=["Gold Club"]))
    question = " ".join([vocabulary.HOW_MANY_WORDS[0], vocabulary.ORDER_WORDS[0], vocabulary.SALE_WORDS[0]])

    assert index.candidates(question) == {}


def test_block_lists_dimension_and_values_in_json():
    index = ValueIndex.from_connection(_layer(products=PRODUCTS))

    block = index.block("zorvian widget 128gb")

    assert block.splitlines()[0].startswith("Stored values near phrases in the question")
    line = next(line for line in block.splitlines() if line.startswith("product: "))
    assert json.loads(line[len("product: "):])[0] == "Zorvian Widget 128GB"


def test_one_spelling_per_normalized_name():
    index = ValueIndex.from_connection(_layer(products=["Zorvian Widget 128GB", "zorvian  widget 128gb", "Kelmar Gizmo"]))

    product = index.candidates("zorvian widget 128gb")["product"]

    assert product.count("Zorvian Widget 128GB") + product.count("zorvian  widget 128gb") == 1


def test_per_dimension_and_total_limits():
    products = [f"Item Series {i} Model" for i in range(40)]
    index = ValueIndex.from_connection(_layer(products=products))

    found = index.candidates("item series model")

    assert len(found["product"]) <= value_linking.PER_DIM
    assert sum(len(values) for values in index.candidates("item series model", per_dim=40, max_total=12).values()) <= 12


def test_customer_names_only_indexed_below_the_distinct_limit():
    many = [f"Customer Number {i}" for i in range(5001)]
    too_many = ValueIndex.from_connection(_layer(customers=many))
    few = ValueIndex.from_connection(_layer(customers=["Customer Number 7", "Other Name"]))

    assert "customer" not in too_many.candidates("customer number 7 bought")
    assert few.candidates("customer number 7 bought")["customer"] == ["Customer Number 7"]


def test_missing_tables_and_columns_are_skipped():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE unrelated (x)")

    index = ValueIndex.from_connection(connection)

    assert index.candidates("anything at all") == {}
    assert index.block("anything at all") == ""


def test_column_catalog_text_columns_are_indexed_when_present():
    connection = _layer(products=PRODUCTS)
    connection.execute("CREATE TABLE extras (colour TEXT, weight REAL)")
    connection.executemany("INSERT INTO extras VALUES (?, ?)", [("Sunset Orange", 1.0), ("Glacier Teal", 2.0)])
    connection.execute("CREATE TABLE column_catalog (table_name, column_name, value_type, distinct_count)")
    connection.executemany("INSERT INTO column_catalog VALUES (?, ?, ?, ?)", [
        ("extras", "colour", "text", 2),
        ("extras", "weight", "real", 2),
    ])

    found = ValueIndex.from_connection(connection).candidates("pick the sunset orange one")

    assert found["extras.colour"] == ["Sunset Orange"]
    assert "extras.weight" not in found


def test_column_catalog_with_another_layout_is_ignored():
    connection = _layer(products=PRODUCTS)
    connection.execute("CREATE TABLE column_catalog (name, kind)")
    connection.execute("INSERT INTO column_catalog VALUES ('x', 'y')")

    index = ValueIndex.from_connection(connection)

    assert index.candidates("zorvian widget 128gb")["product"][0] == "Zorvian Widget 128GB"


def test_build_and_query_time_on_ten_thousand_values():
    rng = random.Random(7)
    syllables = ["zor", "vian", "blar", "tonic", "kel", "mar", "quad", "rant", "lamp", "nev", "osh", "pim", "ral"]

    def name() -> str:
        return " ".join("".join(rng.choice(syllables) for _ in range(2)) for _ in range(3)) + f" {rng.randint(1, 999)}"

    products = {name() for _ in range(9000)}
    customers = {name() for _ in range(1000)}
    connection = _layer(products=sorted(products), customers=sorted(customers))

    started = time.perf_counter()
    index = ValueIndex.from_connection(connection)
    build = time.perf_counter() - started

    queries = [f"{rng.choice(sorted(products))} sales last month" for _ in range(20)]
    started = time.perf_counter()
    for query in queries:
        index.candidates(query)
    per_query = (time.perf_counter() - started) / len(queries)

    assert build < 1.0
    assert per_query < 0.02


@pytest.mark.parametrize("question", ["", "   ", "????"])
def test_blank_or_symbol_questions_give_nothing(question):
    index = ValueIndex.from_connection(_layer(products=PRODUCTS))

    assert index.candidates(question) == {}
    assert index.block(question) == ""
