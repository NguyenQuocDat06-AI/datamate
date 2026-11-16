"""The ASCII normalization fast path must preserve the Unicode normalizer's semantics."""
import random
import string

import pytest

from bizreason.semantic.value_linking import ValueIndex, _BOUNDARY, _canonical
from bizreason.utils import normalize_lookup


@pytest.mark.parametrize("text", [
    "", "   ", "Widget128GB", "  A--B__C / 50ml\t2L ", "A\x00B\r\nC",
    "Crème Brûlée 200g", "Đường 500ml", "ＡＢＣ１２３", "Straße\u0301 20g",
])
def test_fast_normalization_matches_existing_lookup_and_digit_boundaries(text):
    assert _canonical(text) == _BOUNDARY.sub(" ", normalize_lookup(text))


def test_ascii_fast_path_matches_lookup_for_random_printable_and_control_characters():
    rng = random.Random(23)
    alphabet = string.printable + "\x00\x01\x1f\x7f"
    for _ in range(200):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(80)))
        assert _canonical(text) == _BOUNDARY.sub(" ", normalize_lookup(text))


def test_optimized_vectors_keep_unit_length_and_repeated_trigram_counts():
    index = ValueIndex({"product": ["Cacao Cocoa 200g", "Cacao Cocoa 500g", "Simple Mug"]})
    for vec in index._vec:
        assert sum(weight * weight for weight in vec.values()) == pytest.approx(1.0)
    assert set(index.candidates("cacao cocoa")["product"]) == {"Cacao Cocoa 200g", "Cacao Cocoa 500g"}
