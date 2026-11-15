"""A cache entry must describe the effective header mapping that parsed its source."""

from bizreason.data import schema
from bizreason.data.ingestion import BusinessBuilder
from bizreason.utils import file_hash


def _shop(root):
    directory = root / "shop"
    directory.mkdir()
    (directory / "sales.csv").write_text(
        "Custom Key,Payload\nA1,1\nA2,2\n", encoding="utf-8")
    return directory


def _role(context):
    return next(column.semantic_role for column in context.tables[0].columns
                if column.original_name == "Custom Key")


def _must_not_rebuild(*args, **kwargs):
    raise AssertionError("an unchanged effective mapping should reuse its parsed cache")


def test_alias_modes_have_separate_cache_and_switching_back_reuses_the_original(
    workspace_tmp, monkeypatch,
):
    shop = _shop(workspace_tmp)
    source_hash = file_hash(shop / "sales.csv")
    mapping = {"custom key": "order_id"}
    monkeypatch.setattr(schema, "ROLE_LOOKUP", mapping)
    builder = BusinessBuilder(workspace_tmp / "cache", rule_hash="synthetic")
    aliases_on = builder.prepare(shop)
    assert _role(aliases_on) == "order_id"

    monkeypatch.setattr(schema, "ROLE_LOOKUP", {})
    aliases_off = builder.prepare(shop)
    assert aliases_on.revision != aliases_off.revision
    assert aliases_on.database != aliases_off.database
    assert _role(aliases_off) is None

    monkeypatch.setattr(builder, "_ingest_sheet", _must_not_rebuild)
    assert builder.prepare(shop).revision == aliases_off.revision
    monkeypatch.setattr(schema, "ROLE_LOOKUP", mapping)
    restored = builder.prepare(shop)
    assert restored.revision == aliases_on.revision
    assert restored.database == aliases_on.database
    assert _role(restored) == "order_id"
    assert mapping == {"custom key": "order_id"}  # ingestion never changes the caller's mapping
    assert file_hash(shop / "sales.csv") == source_hash


def test_same_mapping_has_stable_cache_identity_independent_of_insertion_order(
    workspace_tmp, monkeypatch,
):
    shop = _shop(workspace_tmp)
    items = [("custom key", "order_id"), ("another header", "product_name")]
    monkeypatch.setattr(schema, "ROLE_LOOKUP", dict(items))
    builder = BusinessBuilder(workspace_tmp / "cache", rule_hash="synthetic")
    first = builder.prepare(shop)
    monkeypatch.setattr(schema, "ROLE_LOOKUP", dict(reversed(items)))
    monkeypatch.setattr(builder, "_ingest_sheet", _must_not_rebuild)
    assert builder.prepare(shop).revision == first.revision


def test_changing_a_mapping_value_invalidates_cache_even_when_aliases_remain_enabled(
    workspace_tmp, monkeypatch,
):
    shop = _shop(workspace_tmp)
    monkeypatch.setattr(schema, "ROLE_LOOKUP", {"custom key": "order_id"})
    builder = BusinessBuilder(workspace_tmp / "cache", rule_hash="synthetic")
    first = builder.prepare(shop)
    monkeypatch.setattr(schema, "ROLE_LOOKUP", {"custom key": "customer_name"})
    changed = builder.prepare(shop)
    assert changed.revision != first.revision
    assert _role(changed) == "customer_name"
