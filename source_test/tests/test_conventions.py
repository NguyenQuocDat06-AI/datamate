import sqlite3

from bizreason.semantic import conventions
from bizreason.semantic.layer import _apply_unit_conventions


class FakeModel:
    def __init__(self, reply):
        self.reply = reply

    def generate_json(self, system, prompt, *, timeout):
        return self.reply


def test_extract_keeps_only_well_formed_directives():
    reply = {"directives": [
        {"type": "unit_factor", "products": ["Pipe A"], "large_unit": "bundle", "factor": 10, "base_unit": "piece"},
        {"type": "unit_factor", "products": ["Pipe A"], "large_unit": "bundle", "factor": 1, "base_unit": "piece"},
        {"type": "zero_amount_meaning", "meaning": "warranty", "note_words": ["warranty"]},
        {"type": "made_up"},
    ]}
    directives, ok = conventions.extract(FakeModel(reply), "readme text")
    assert ok and [d["type"] for d in directives] == ["unit_factor", "zero_amount_meaning"]


def test_unit_convention_applies_only_to_products_sold_by_that_unit():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE raw_lines (product_name TEXT, unit TEXT)")
    con.executemany("INSERT INTO raw_lines VALUES (?,?)", [("Pipe A", "bundle"), ("Pipe A", "piece"), ("Pipe B", "piece")])
    con.execute("CREATE TEMP TABLE unit_factors (product_id, name_key, large_key, factor)")
    con.execute("CREATE TEMP TABLE product_facts (product_id, name_key, category, base_unit, unit_cost)")
    directive = {"type": "unit_factor", "products": ["Pipe A", "Pipe B"], "large_unit": "bundle", "factor": 10,
                 "base_unit": "piece"}
    assert _apply_unit_conventions(con, [directive]) == 1
    assert ("pipe a", "bundle", 10.0) in con.execute("SELECT name_key, large_key, factor FROM temp.unit_factors").fetchall()
