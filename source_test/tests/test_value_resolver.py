from bizreason.semantic.value_resolver import resolve

DRINKS = ["Latte (M)", "Latte (L)", "Aquafina 500ml", "Aquafina 1.5L", "Trà đào cam sả", "Trà vải", "Bánh mì thịt"]
DISHES = ["Gỏi xoài tôm nướng", "Tôm nướng mọi", "Tôm sốt Thái", "Lẩu Thái tôm cá (nhỏ)", "Cơm chiên", "Canh chua cá"]


def test_typo_and_missing_accents():
    assert resolve("aquafina 500 ml", DRINKS, "product") == ["Aquafina 500ml"]
    assert resolve("tra vai", DRINKS, "product") == ["Trà vải"]
    assert resolve("aquafinna 500ml", DRINKS, "product") == ["Aquafina 500ml"]


def test_word_order_for_person_names():
    people = ["Nguyễn Văn An", "Trần Thị Bình", "Lê Hoàng Cường"]
    assert resolve("tran binh thi", people, "employee") == ["Trần Thị Bình"]


def test_leading_word_dropped_only_when_rest_matches():
    assert resolve("mon tom nuong", DISHES, "product") == ["Tôm nướng mọi"]
    assert resolve("mon pho bo", DISHES, "product") is None


def test_size_letter_must_agree():
    assert resolve("Latte (S)", DRINKS, "product") is None
    assert resolve("latte m", ["Latte (M)", "Mocha (M)"], "product") == ["Latte (M)"]
    assert resolve("latte m", ["Latte (L)", "Mocha (M)"], "product") is None


def test_number_must_agree():
    assert resolve("Aquafina 330ml", DRINKS, "product") is None
    assert resolve("aquafina 1.5l", DRINKS, "product") == ["Aquafina 1.5L"]


def test_close_products_are_none():
    assert resolve("xiaomi redmi note", ["Xiaomi Redmi Note 14", "Xiaomi Redmi Note 13"], "product") is None
    assert resolve("tom", DISHES, "product") is None
    assert resolve("tra", DRINKS, "product") is None


def test_absent_value_stays_none():
    assert resolve("duong nguyen tat thanh", ["Nguyễn Văn An", "Trần Thị Bình"], "customer") is None
    assert resolve("", DRINKS, "product") is None
    assert resolve("x", [], "product") is None


def test_every_spelling_of_one_name_returned():
    got = resolve("aquafinna 500ml", ["Aquafina 500ml", "Aquafina 500 ml", "Aquafina 1.5L"], "product")
    assert sorted(got) == ["Aquafina 500 ml", "Aquafina 500ml"]
