"""Definitions preserve identity and provenance across document presentation."""
from dataclasses import FrozenInstanceError, replace

import pytest

from bizreason.semantic.evidence import extract_definitions


@pytest.mark.parametrize("heading", ["", "# Notes\n", "## Notes\n", "### Notes\n", "##### Notes\n"])
@pytest.mark.parametrize("definition", [
    "| Term | Definition |\n| :--- | ---: |\n| Reward | amount * rate |\n",
    "- **Reward**: amount * rate\n",
    "* Reward = amount * rate\n",
    "Reward means amount * rate\n",
    "Reward is defined as amount * rate\n",
])
def test_definition_identity_does_not_depend_on_format_or_heading(heading, definition):
    expected = extract_definitions("Reward means amount * rate").definitions[0]
    text = heading + definition
    document = extract_definitions(text, source_id="shop_document")
    assert len(document.definitions) == 1
    found = document.definitions[0]
    assert (found.term, found.body, found.definition_id) == (
        "Reward", "amount * rate", expected.definition_id)
    for span in found.evidence:
        assert span.source_id == "shop_document"
        assert span.validate(text)
        assert text[span.start:span.end] == span.quote


def test_duplicate_definitions_keep_every_span_but_one_semantic_identity():
    text = "- Reward: amount * rate\n\nReward means amount * rate.\n"
    document = extract_definitions(text)
    assert len(document.definitions) == 1
    assert len(document.definitions[0].evidence) == 2
    assert document.conflicts == ()
    assert all(span.validate(text) for span in document.definitions[0].evidence)


def test_conflicting_operators_are_not_normalized_away_or_silently_selected():
    document = extract_definitions("- Balance: incoming + outgoing\n- Balance: incoming - outgoing\n")
    assert len(document.definitions) == 2
    assert len({d.definition_id for d in document.definitions}) == 2
    assert document.conflicts == ("balance",)


def test_decimal_separators_and_arithmetic_are_part_of_semantic_identity():
    definitions = extract_definitions("- Factor: amount * 0.1\n- Factor: amount * 01\n").definitions
    assert len(definitions) == 2


def test_table_header_separator_and_fenced_examples_are_not_definitions():
    text = ("```markdown\n- Imagined: hidden\n```\n"
            "~~~\n| Hidden | ignored |\n~~~\n"
            "| Term | Meaning |\n| --- | :---: |\n| Reward | actual |\n")
    assert [(d.term, d.body) for d in extract_definitions(text).definitions] == [("Reward", "actual")]


def test_standard_markdown_table_without_outer_pipes_is_supported():
    text = "Term | Meaning\n--- | ---\nReward | amount * rate\n\nA prose note | not a table row\n"
    assert [(d.term, d.body) for d in extract_definitions(text).definitions] == [
        ("Reward", "amount * rate")]


def test_escaped_and_inline_code_pipes_stay_in_their_cell():
    text = "| Label | Body |\n|---|---|\n| Flag | `a|b` or a\\|b |\n"
    document = extract_definitions(text)
    assert [(d.term, d.body) for d in document.definitions] == [("Flag", "a|b or a|b")]
    assert document.definitions[0].evidence[0].validate(text)


def test_multiline_bullet_retains_one_exact_source_span():
    text = "# Notes\r\n- **Reward**: amount multiplied\r\n  by the employee rate.\r\n- Other: value\r\n"
    document = extract_definitions(text)
    first = document.definitions[0]
    assert first.body == "amount multiplied by the employee rate."
    assert len(first.evidence) == 1 and first.evidence[0].validate(text)
    assert "\r\n  by" in first.evidence[0].quote
    assert document.definitions[1].term == "Other"


def test_multiline_paragraph_is_not_split_into_different_definitions():
    text = "Reward means amount multiplied\nby the employee rate.\n\nNext: other\n"
    first = extract_definitions(text).definitions[0]
    assert first.body == "amount multiplied by the employee rate."
    assert first.evidence[0].validate(text)


def test_unicode_offsets_and_whole_document_fingerprint_prevent_stale_evidence():
    text = "## Quy ước\n- **Hoa hồng**: doanh thu nhân tỷ lệ.\n"
    span = extract_definitions(text).definitions[0].evidence[0]
    assert span.validate(text)
    assert not span.validate(text + " ")
    assert not replace(span, start=span.start + 1).validate(text)
    assert not replace(span, quote="invented").validate(text)
    with pytest.raises(FrozenInstanceError):
        span.start = 0


def test_empty_or_unstructured_document_returns_no_invented_definition():
    assert extract_definitions("").definitions == ()
    assert extract_definitions("## Example\nJust a note without a definition.\n").definitions == ()
