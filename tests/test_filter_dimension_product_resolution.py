"""A product written loosely resolves to the one stored product it names; ambiguity or no match stays unresolved."""

from __future__ import annotations

import pytest

from bizreason.semantic.compiler import Compiler
from bizreason.utils import normalize_lookup

CATALOG = [
    "Roasted Duck", "ROASTED DUCK", "Fried Rice", "Fried Noodle", "Garden Salad Large", "Garden Salad Small",
    "Soap Bar Alpha 90g", "Soap Bar Beta 90g", "Water Bottle Alpha 500ml", "Water Bottle Alpha 1.5L",
    "Valve Kit Gamma",
]


def match(needle: str) -> list[str]:
    return Compiler._match("product", CATALOG, normalize_lookup(needle))


@pytest.mark.parametrize("needle, expected", [
    ("roasted duck", {"Roasted Duck", "ROASTED DUCK"}),        # every spelling of one name
    ("plate roasted duck", {"Roasted Duck", "ROASTED DUCK"}),  # a serving word in front
    ("bowl fried rice", {"Fried Rice"}),
    ("water bottle alpha 500ml", {"Water Bottle Alpha 500ml"}),
    ("water bottle alpha 500 ml", {"Water Bottle Alpha 500ml"}),
    ("valve kit gama", {"Valve Kit Gamma"}),                   # one slip
])
def test_loose_name_resolves_to_its_one_product(needle: str, expected: set[str]) -> None:
    assert set(match(needle)) == expected


@pytest.mark.parametrize("needle", [
    "fried",                    # a word shared by two products
    "garden salad",             # two sizes, no way to pick
    "soap bar 90g",             # two brands
    "water bottle alpha 750ml",  # a number the shop does not have
    "unrelated thing",
])
def test_ambiguous_or_unknown_name_stays_unresolved(needle: str) -> None:
    assert match(needle) == []
