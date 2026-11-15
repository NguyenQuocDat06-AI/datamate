"""The contract opt-in reads definitions independently of Markdown heading shape."""
import pytest

from bizreason.semantic.definitions import provider, rulebook_definitions


@pytest.mark.parametrize("heading", ["", "# Rules\n", "## Rules\n", "### Rules\n"])
@pytest.mark.parametrize("definition", [
    "| Term | Definition |\n|---|---|\n| Hoa hồng | doanh thu nhân tỷ lệ |\n",
    "- **Hoa hồng**: doanh thu nhân tỷ lệ\n",
    "Hoa hồng là doanh thu nhân tỷ lệ\n",
])
def test_all_definition_provider_is_format_and_heading_invariant(heading, definition):
    block = provider(heading + definition, include_all=True)
    assert block is not None
    assert "doanh thu nhân tỷ lệ" in block("hoa hồng tháng này bao nhiêu")
    assert block("số xe hôm nay") == ""


def test_provider_keeps_all_conflicting_evidence_and_warns_instead_of_overwriting():
    block = provider("- Reward: amount * rate\n- Reward: amount + rate\n", include_all=True)
    answer = block("reward")
    assert "CONFLICT" in answer
    assert "amount * rate" in answer and "amount + rate" in answer


def test_legacy_industry_bullets_and_ranking_prose_are_supported():
    block = provider("### Industry\n- Thu máy cũ: tiền riêng\n")
    assert "tiền riêng" in block("tháng này thu máy cũ bao nhiêu")
    block = provider("## General\nKhách trung thành nhất là khách có nhiều ngày mua nhất\n")
    assert "nhiều ngày mua nhất" in block("ai trung thành nhất")


def test_tuple_interface_and_legacy_eligibility_remain_compatible():
    text = "## General\n| Doanh thu ngày | tổng tiền |\n"
    assert provider(text) is None
    assert rulebook_definitions(text, include_all=True) == [
        ("Doanh thu ngày", "tổng tiền", frozenset({"doanh", "thu", "ngay"}))]


def test_definition_prompt_stays_bounded_without_dropping_conflict_marker():
    text = "\n".join(f"- Reward: formula {index}" for index in range(5))
    result = provider(text, include_all=True)("reward")
    assert "CONFLICT" in result
    assert sum(line.startswith("- ") for line in result.splitlines()) == 3
