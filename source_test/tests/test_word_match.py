from bizreason.semantic.compiler import _word_match


def test_longest_stored_name_inside_the_filter_text():
    values = ["Alpha beta", "Alpha beta 30ml", "Gamma"]
    assert _word_match(values, "so chai alpha beta 30ml") == ["Alpha beta 30ml"]


def test_every_filter_word_inside_one_stored_name():
    assert _word_match(["Delta cuc 90g", "Delta 200g"], "delta 90g") == ["Delta cuc 90g"]


def test_ambiguous_word_match_gives_nothing():
    assert _word_match(["Delta cuc 90g", "Delta hop 90g"], "delta 90g") == []
    assert _word_match(["Ab", "Cd"], "x ab cd") == []


def test_pair_with_redundant_grouping_is_answered_as_two_members():
    from datetime import date

    from bizreason.contracts import TimeWindow
    from bizreason.semantic.compiler import Intent, _same_measure_pair

    intent = Intent(measure="revenue", measure2="revenue", shape="breakdown", group_by="channel",
                    filters=[{"dim": "channel", "value": "a", "exclude": False},
                             {"dim": "channel", "value": "b", "exclude": False}])
    window = TimeWindow(role="target", start=date(2026, 1, 1), end_exclusive=date(2026, 2, 1), evidence=["question"])
    _, members = _same_measure_pair(intent, [window])
    assert [m.filters[0]["value"] for m in members] == ["a", "b"]
    assert all(m.group_by is None and m.shape == "scalar" for m in members)


def test_spellings_of_one_name_count_as_one_match():
    values = ["Xà bông Delta 90g", "Xa bong Delta 90g", "Xà  bông Delta 90g", "Delta 200g"]
    assert sorted(_word_match(values, "delta 90g")) == sorted(values[:3])


def test_spacing_variants_are_kept_together():
    values = ["Siro Alpha chai 30ml", "Siro Alpha chai 30 ml", "Beta"]
    assert sorted(_word_match(values, "so chai siro alpha chai 30ml")) == sorted(values[:2])


def test_exact_product_match_keeps_every_spelling():
    from bizreason.semantic.compiler import Compiler
    values = ["Siro Alpha chai 100ml", "Siro Alpha chai 100 ml", "Siro  Alpha chai 100ml", "Beta"]
    assert sorted(Compiler._match("product", values, "siro alpha chai 100ml")) == sorted(values[:3])


def test_device_line_covers_every_capacity():
    from bizreason.semantic import rulebook_params
    rulebook_params.set_rulebook_text("| Dòng máy, hãng | phần tên trước dung lượng GB |")
    values = ["iPhone 16 Pro Max 256GB", "iPhone 16 Pro Max 512GB", "iPhone 16 Pro 128GB", "Ốp lưng iPhone 16 Pro Max"]
    assert sorted(_word_match(values, "iphone 16 pro max")) == ["iPhone 16 Pro Max 256GB", "iPhone 16 Pro Max 512GB"]


def test_device_line_needs_the_rulebook_to_define_it():
    from bizreason.semantic import rulebook_params
    try:
        rulebook_params.set_rulebook_text("| Doanh thu | tổng tiền |")
        assert _word_match(["Phone A 256GB", "Phone A 512GB"], "phone a") == []
    finally:
        rulebook_params.set_rulebook_text(None)
