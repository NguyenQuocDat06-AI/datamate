"""Two-stage annotator: grouping, role validation, error records, relevant_readme and cache behaviour."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from bizreason.contracts import BusinessContext, ColumnMeta, SourceRef, TableMeta
from bizreason.semantic.annotator import (
    SCOPE,
    Annotator,
    group_tables,
    load_or_annotate,
    load_status_map,
    load_value_groups,
    needs_reannotation,
    order_status_values,
    _sibling_fit,
    relevant_readme,
)
from bizreason.semantic.layer import ModelFailure
from bizreason.semantic.vocabulary import PAYMENT_CANONICAL
from bizreason.utils import normalize_lookup


class FakeModel:
    """Answers stage 1 (kind) and stage 2 (roles) from a script; records every call."""

    def __init__(self, kind: str = "sales", roles: dict[str, Any] | None = None, fail: Exception | None = None):
        self.kind = kind
        self.roles = roles
        self.fail = fail
        self.calls: list[tuple[str, str]] = []
        self.usage = None

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]:
        self.calls.append((system, prompt))
        if self.fail is not None:
            raise self.fail
        if system.startswith("Classify"):
            return {"kind": self.kind}
        if self.roles is not None:
            return self.roles
        return {column: "other" for column in _columns_in(prompt)}


def _columns_in(prompt: str) -> list[str]:
    return [line[2:].split(" | ", 1)[0] for line in prompt.splitlines() if line.startswith("- ")]


def _table(name: str, columns: list[str], path: str = "data/source_a.csv", original: dict[str, str] | None = None,
           grain: str = "unknown") -> TableMeta:
    original = original or {}
    return TableMeta(
        name=name,
        sources=[SourceRef(path=path)],
        columns=[
            ColumnMeta(name=c, original_name=original.get(c, c), dtype="string", examples=["x", "y"])
            for c in columns
        ],
        grain=grain,
        row_count=2,
        raw_row_count=2,
    )


def _context(tables: list[TableMeta], readme: str = "", revision: str = "rev-1") -> BusinessContext:
    return BusinessContext(
        business_id="shop_a",
        revision=revision,
        database=Path("/tmp/unused.duckdb"),
        readme=readme,
        tables=tables,
        manifest={},
    )


def test_group_tables_groups_identical_column_tuples_in_first_seen_order() -> None:
    first = _table("t_one", ["a", "b"])
    second = _table("t_two", ["c", "d"])
    third = _table("t_three", ["a", "b"])
    fourth = _table("t_four", ["a", "b", "c"])

    groups = group_tables(_context([first, second, third, fourth]))

    assert [[t.name for t in g] for g in groups] == [["t_one", "t_three"], ["t_two"], ["t_four"]]


def test_two_stage_calls_run_and_roles_are_returned() -> None:
    table = _table("t_one", ["ref_no", "amount"])
    model = FakeModel(kind="sales", roles={"ref_no": "order_id", "amount": "line_amount"})

    records = Annotator(model, workers=1).annotate(_context([table]))

    assert len(model.calls) == 2
    assert model.calls[0][0].startswith("Classify")
    assert model.calls[1][0].startswith("Give the meaning")
    assert records == [
        {
            "biz": "shop_a",
            "table": "t_one",
            "columns": ["ref_no", "amount"],
            "kind": "sales",
            "kind_source": "model",
            "roles": {"ref_no": "order_id", "amount": "line_amount"},
            "seconds": records[0]["seconds"],
        }
    ]


def test_invalid_role_becomes_other() -> None:
    table = _table("t_one", ["ref_no", "amount"])
    model = FakeModel(kind="sales", roles={"ref_no": "not_a_real_role", "amount": "line_amount"})

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert record["roles"] == {"ref_no": "other", "amount": "line_amount"}


def test_role_valid_for_another_kind_becomes_other() -> None:
    table = _table("t_one", ["amount"])
    # "line_amount" exists for the sales scope but not for the "other" scope.
    model = FakeModel(kind="other", roles={"amount": "line_amount"})

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert "line_amount" not in SCOPE["other"]
    assert record["kind"] == "other"
    assert record["roles"] == {"amount": "other"}


def test_invalid_kind_becomes_other_and_uses_other_scope() -> None:
    table = _table("t_one", ["ref_no"])
    model = FakeModel(kind="made_up_kind", roles={"ref_no": "order_id"})

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert record["kind"] == "other"
    assert "made_up_kind" not in SCOPE
    assert record["roles"] == {"ref_no": "order_id"}
    assert "Roles:\n" in model.calls[1][0]


def test_model_exception_yields_error_record() -> None:
    table = _table("t_one", ["ref_no"])
    model = FakeModel(fail=RuntimeError("boom"))

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert record["error"] == "RuntimeError: boom"
    assert "kind" not in record
    assert "roles" not in record
    assert record["table"] == "t_one"
    assert record["columns"] == ["ref_no"]
    assert "seconds" in record


def test_annotate_preserves_group_order_with_parallel_workers() -> None:
    tables = [_table(f"t_{i}", [f"col_{i}"]) for i in range(6)]
    model = FakeModel(kind="other")

    records = Annotator(model, workers=4).annotate(_context(tables))

    assert [r["table"] for r in records] == [f"t_{i}" for i in range(6)]


def test_relevant_readme_keeps_paragraph_naming_a_header_and_drops_unrelated() -> None:
    readme = "\n\n".join(
        [
            "Intro paragraph about the shop in general.",
            "The column lot_code tracks which batch an item came from.",
            "Unrelated paragraph about opening hours and holidays.",
        ]
    )
    table = _table("t_one", ["lot_code"], path="data/other_file.csv", original={"lot_code": "lot_code"})

    doc = relevant_readme(readme, table)

    assert "lot_code tracks which batch" in doc
    assert "opening hours" not in doc
    assert "Intro paragraph" not in doc


def test_relevant_readme_respects_limit() -> None:
    readme = "column_x explained here.\n\n" + ("column_x filler. " * 50)
    table = _table("t_one", ["column_x"])

    doc = relevant_readme(readme, table, limit=40)

    assert doc == "column_x explained here."


def test_cache_hit_does_not_call_model(tmp_path: Path) -> None:
    table = _table("t_one", ["ref_no"])
    context = _context([table], revision="rev-1")
    cache = tmp_path / "annotations.json"
    cache.write_text(
        json.dumps({"revision": "rev-1", "records": [{"biz": "shop_a", "table": "t_one", "columns": ["ref_no"]}]}),
        encoding="utf-8",
    )
    model = FakeModel(kind="sales")

    records = load_or_annotate(Annotator(model, workers=1), context, cache)

    assert model.calls == []
    assert records == [{"biz": "shop_a", "table": "t_one", "columns": ["ref_no"]}]


def test_cache_with_different_revision_is_ignored_and_rewritten(tmp_path: Path) -> None:
    table = _table("t_one", ["ref_no"])
    context = _context([table], revision="rev-2")
    cache = tmp_path / "annotations.json"
    cache.write_text(
        json.dumps({"revision": "rev-1", "records": [{"stale": True}]}),
        encoding="utf-8",
    )
    model = FakeModel(kind="sales", roles={"ref_no": "order_id"})

    records = load_or_annotate(Annotator(model, workers=1), context, cache)

    assert len(model.calls) == 2
    assert records[0]["kind"] == "sales"
    stored = json.loads(cache.read_text(encoding="utf-8"))
    assert stored["revision"] == "rev-2"
    assert stored["records"] == records
    assert not list(tmp_path.glob("*.tmp"))


def test_missing_cache_is_written_atomically(tmp_path: Path) -> None:
    table = _table("t_one", ["ref_no"])
    cache = tmp_path / "nested" / "annotations.json"
    model = FakeModel(kind="other")

    records = load_or_annotate(Annotator(model, workers=1), _context([table]), cache)

    assert json.loads(cache.read_text(encoding="utf-8")) == {"revision": "rev-1", "records": records}
    assert not list(cache.parent.glob("*.tmp"))


class FlakyModel(FakeModel):
    """Raises for the first `failures` calls, then behaves like FakeModel."""

    def __init__(self, failures: int, **kwargs: Any):
        super().__init__(**kwargs)
        self.failures = failures

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]:
        if self.failures > 0:
            self.failures -= 1
            self.calls.append((system, prompt))
            raise RuntimeError("temporary")
        return super().generate_json(system, prompt, timeout=timeout)


class StatusModel:
    """Returns a fixed reply to every call, or raises when `fail` is set; records prompts."""

    def __init__(self, reply: dict[str, Any] | None = None, fail: Exception | None = None):
        self.reply = reply or {}
        self.fail = fail
        self.calls: list[tuple[str, str]] = []
        self.usage = None

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]:
        self.calls.append((system, prompt))
        if self.fail is not None:
            raise self.fail
        return self.reply


def _status_context(tmp_path: Path, values: list[str]) -> tuple[BusinessContext, list[dict[str, Any]]]:
    database = tmp_path / "source.sqlite"
    con = sqlite3.connect(database)
    con.execute('CREATE TABLE "sales" ("state" TEXT, __source_row INTEGER, __raw_json TEXT)')
    con.executemany(
        'INSERT INTO "sales" VALUES (?, ?, ?)',
        [(value, number, "{}") for number, value in enumerate(values, start=1)],
    )
    con.commit()
    con.close()
    table = _table("sales", ["state"])
    context = BusinessContext(
        business_id="shop_a", revision="rev-1", database=database, readme="", manifest={}, tables=[table],
    )
    records = [{"columns": ["state"], "kind": "sales", "roles": {"state": "order_status"}}]
    return context, records


def test_classify_status_values_drops_invalid_answers() -> None:
    model = StatusModel(reply={
        "delivered": "completed",
        "void": "cancelled",
        "odd": "unknown",
        "bad": "maybe",          # not one of the five labels
        "gone": "returned",      # not an input value
    })
    values = ["delivered", "void", "odd", "bad"]

    result = Annotator(model, workers=1).classify_status_values(values)

    assert result == {"delivered": "completed", "void": "cancelled", "odd": "unknown"}
    system, prompt = model.calls[0]
    for line in ("completed: finished order", "shipping: being delivered", "cancelled: order cancelled",
                 "returned: goods returned, fully or partly", "unknown: anything else"):
        assert line in system
    assert json.loads(prompt.split("Values:\n", 1)[1]) == values


def test_classify_status_values_returns_empty_on_model_failure() -> None:
    model = StatusModel(fail=RuntimeError("boom"))

    assert Annotator(model, workers=1).classify_status_values(["delivered"]) == {}


def test_classify_status_values_makes_no_call_for_empty_input() -> None:
    model = StatusModel(reply={})

    assert Annotator(model, workers=1).classify_status_values([]) == {}
    assert model.calls == []


def test_annotate_status_skips_canonical_values_and_unknown_answers(tmp_path: Path) -> None:
    context, records = _status_context(tmp_path, ["completed", "shipping", "delivered", "void", "mystery", "returned"])
    model = StatusModel(reply={"delivered": "completed", "void": "cancelled", "mystery": "unknown"})

    result = Annotator(model, workers=1).annotate_status(context, records)

    assert result == {"delivered": "completed", "void": "cancelled"}
    _, prompt = model.calls[0]
    assert json.loads(prompt.split("Values:\n", 1)[1]) == ["delivered", "mystery", "void"]


def test_annotate_status_makes_no_call_when_all_values_are_canonical(tmp_path: Path) -> None:
    context, records = _status_context(tmp_path, ["completed", "returned"])
    model = StatusModel(reply={})

    assert Annotator(model, workers=1).annotate_status(context, records) == {}
    assert model.calls == []


def test_failed_group_is_retried_once_and_succeeds() -> None:
    table = _table("t_one", ["ref_no"])
    model = FlakyModel(1, kind="sales", roles={"ref_no": "order_id"})

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert "error" not in record
    assert record["kind"] == "sales"
    assert record["roles"] == {"ref_no": "order_id"}
    assert len(model.calls) == 3  # failed kind call, retried kind call, role call


def test_group_failing_twice_records_error_after_one_retry() -> None:
    table = _table("t_one", ["ref_no"])
    model = FakeModel(fail=RuntimeError("boom"))

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert record["error"] == "RuntimeError: boom"
    # attempt 1: kind call, shorter kind retry. attempt 2: kind call, shorter kind retry (kind falls back to
    # "other"), then the role call (3 tries), then its one batch (3 tries).
    assert len(model.calls) == 10


def test_load_status_map_does_not_cache_failure(tmp_path: Path) -> None:
    context, records = _status_context(tmp_path, ["delivered"])
    cache = tmp_path / "status.json"

    failed = load_status_map(Annotator(StatusModel(fail=RuntimeError("down")), workers=1), context, records, cache)

    assert failed == {}
    assert not cache.exists()

    model = StatusModel(reply={"delivered": "completed"})
    recovered = load_status_map(Annotator(model, workers=1), context, records, cache)

    assert recovered == {"delivered": "completed"}
    assert json.loads(cache.read_text(encoding="utf-8")) == {"revision": "rev-1", "status_map": recovered}


def test_load_status_map_cache_hit_skips_model(tmp_path: Path) -> None:
    context, records = _status_context(tmp_path, ["delivered"])
    cache = tmp_path / "status.json"
    cache.write_text(json.dumps({"revision": "rev-1", "status_map": {"delivered": "completed"}}), encoding="utf-8")
    model = StatusModel(reply={"delivered": "returned"})

    result = load_status_map(Annotator(model, workers=1), context, records, cache)

    assert result == {"delivered": "completed"}
    assert model.calls == []


class RoleScriptModel:
    """Kind call answers `kind` (or raises when kind_fails). Role calls are numbered from 1; the numbers in
    fail_role_calls raise, every other role call maps each column in its prompt to "order_id"."""

    def __init__(self, fail_role_calls: set[int] | None = None, kind: str = "sales", kind_fails: bool = False):
        self.fail_role_calls = fail_role_calls or set()
        self.kind = kind
        self.kind_fails = kind_fails
        self.role_calls = 0
        self.calls: list[tuple[str, str]] = []
        self.usage = None

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]:
        self.calls.append((system, prompt))
        if system.startswith("Classify"):
            if self.kind_fails:
                raise RuntimeError("kind down")
            return {"kind": self.kind}
        self.role_calls += 1
        if self.role_calls in self.fail_role_calls:
            raise RuntimeError("invalid json")
        return {column: "order_id" for column in _columns_in(prompt)}


def _wide_table(count: int) -> TableMeta:
    return _table("t_wide", [f"col_{i:02d}" for i in range(count)])


def test_more_than_20_columns_are_batched_and_merged() -> None:
    table = _wide_table(25)
    model = FakeModel(kind="sales", roles=None)

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    role_prompts = [prompt for system, prompt in model.calls[1:]]
    assert [len(_columns_in(p)) for p in role_prompts] == [15, 10]
    assert record["roles"] == {f"col_{i:02d}": "other" for i in range(25)}
    assert "partial" not in record
    assert "error" not in record


def test_failing_single_call_triggers_batching() -> None:
    table = _table("t_one", ["ref_no", "amount", "memo"])
    model = RoleScriptModel(fail_role_calls={1})

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert [len(_columns_in(p)) for _, p in model.calls[1:]] == [3, 3]
    assert record["roles"] == {"ref_no": "order_id", "amount": "order_id", "memo": "order_id"}
    assert record["kind"] == "sales"
    assert "partial" not in record


def test_partial_batch_failure_keeps_good_roles_and_sets_partial() -> None:
    table = _wide_table(25)
    # 25 columns go straight to batches: role call 1 covers the first 15 columns, call 2 the last 10. Call 2 fails
    # all three of its tries.
    model = RoleScriptModel(fail_role_calls={2, 3, 4})

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert record["partial"] is True
    assert "error" not in record
    assert record["kind"] == "sales"
    assert set(record["roles"]) == {f"col_{i:02d}" for i in range(15)}
    assert set(record["roles"].values()) == {"order_id"}


def test_role_call_that_succeeds_on_its_second_try_records_no_failure() -> None:
    table = _table("t_one", ["ref_no", "amount"])
    # the first role call fails; its retry with the same prompt answers
    model = RoleScriptModel(fail_role_calls={1})
    annotator = Annotator(model, workers=1)

    record = annotator.annotate(_context([table]))[0]

    assert record["roles"] == {"ref_no": "order_id", "amount": "order_id"}
    assert "partial" not in record
    assert annotator.take_failures() == []
    role_prompts = [prompt for system, prompt in model.calls if not system.startswith("Classify")]
    assert len(role_prompts) == 2 and role_prompts[0] == role_prompts[1]  # the retry sent the same prompt


def test_batch_failing_every_try_is_recorded_with_its_table_and_kind() -> None:
    table = _wide_table(25)
    model = RoleScriptModel(fail_role_calls={2, 3, 4})
    annotator = Annotator(model, workers=1)

    record = annotator.annotate(_context([table]))[0]

    assert record["partial"] is True
    assert annotator.take_failures() == [ModelFailure("roles:" + table.name, table.name, "sales")]


def test_all_batches_failing_gives_error_record() -> None:
    table = _wide_table(25)
    model = RoleScriptModel(fail_role_calls=set(range(1, 100)))

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert record["error"] == "RuntimeError: invalid json"
    assert "kind" not in record
    assert "roles" not in record
    assert "partial" not in record


def test_kind_failing_on_first_attempt_is_not_replaced_by_other() -> None:
    table = _table("t_one", ["ref_no"])
    model = FlakyModel(1, kind="sales", roles={"ref_no": "order_id"})

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert record["kind"] == "sales"


def test_kind_failing_twice_falls_back_to_other_after_retry() -> None:
    table = _table("t_one", ["ref_no"])
    model = RoleScriptModel(kind_fails=True)

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert "error" not in record
    assert record["kind"] == "other"
    assert record["roles"] == {"ref_no": "order_id"}
    assert model.calls[1][0].startswith("Classify")  # kind call was retried before falling back
    assert "Documentation:" not in model.calls[1][1]  # the retry is the shorter prompt
    assert model.calls[-1][0].startswith("Give the meaning")
    assert len(model.calls) == 5  # two kind calls per attempt, two attempts: four; then the role call


class EchoLineModel:
    """Labels every name in the prompt as "product"; records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.usage = None

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]:
        self.calls.append((system, prompt))
        names = json.loads(prompt.split("Names:\n", 1)[1])
        return {name: "product" for name in names}


def test_classify_line_names_drops_invalid_answers() -> None:
    model = StatusModel(reply={
        "Coke": "product",
        "Voucher 10": "discount",
        "Ship": "shipping_fee",
        "Bad": "maybe",          # not one of the four labels
        "Ghost": "product",      # not an input name
    })

    result = Annotator(model, workers=1).classify_line_names(["Coke", "Voucher 10", "Ship", "Bad"])

    assert result == {"Coke": "product", "Voucher 10": "discount", "Ship": "shipping_fee"}
    system, prompt = model.calls[0]
    for line in ("product: goods or services sold", "discount: voucher, points used, or price reduction lines",
                 "shipping_fee: delivery fee lines", "service_fee: a service charge added to the bill"):
        assert line in system
    assert json.loads(prompt.split("Names:\n", 1)[1]) == ["Coke", "Voucher 10", "Ship", "Bad"]


def test_classify_line_names_returns_empty_on_failure() -> None:
    model = StatusModel(fail=RuntimeError("down"))

    assert Annotator(model, workers=1).classify_line_names(["Coke"]) == {}


def test_classify_line_names_splits_long_lists_into_120_name_calls() -> None:
    model = EchoLineModel()
    names = [f"item {i}" for i in range(250)]

    result = Annotator(model, workers=1).classify_line_names(names)

    assert [len(json.loads(p.split("Names:\n", 1)[1])) for _, p in model.calls] == [120, 120, 10]
    assert result == {name: "product" for name in names}


def test_annotate_line_kinds_classifies_only_names_missing_from_catalog() -> None:
    model = StatusModel(reply={"Voucher 10": "discount", "Ship fee": "shipping_fee"})
    context = _context([])

    result = Annotator(model, workers=1).annotate_line_kinds(
        context, [], ["Coke", "Bread"], ["COKE", "Voucher 10", " bread ", "Ship fee"],
    )

    assert result == {normalize_lookup("Voucher 10"): "discount", normalize_lookup("Ship fee"): "shipping_fee"}
    _, prompt = model.calls[0]
    assert json.loads(prompt.split("Names:\n", 1)[1]) == ["Voucher 10", "Ship fee"]


def test_annotate_line_kinds_makes_no_call_when_everything_is_catalogued() -> None:
    model = StatusModel(reply={})

    result = Annotator(model, workers=1).annotate_line_kinds(
        _context([]), [], ["Coke"], ["coke", "COKE"],
    )

    assert result == {}
    assert model.calls == []


def test_annotate_line_kinds_without_catalog_makes_no_call() -> None:
    model = StatusModel(reply={"Voucher 10": "discount"})

    result = Annotator(model, workers=1).annotate_line_kinds(_context([]), [], [], ["Voucher 10"])

    assert result == {}
    assert model.calls == []


def test_failing_kind_call_uses_grain() -> None:
    table = _table("t_one", ["ref_no"], grain="purchase_line")
    model = RoleScriptModel(kind_fails=True)

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert "error" not in record
    assert record["kind"] == "purchase"
    assert record["kind_source"] == "grain"


def test_model_other_is_overridden_by_mapped_grain() -> None:
    table = _table("t_one", ["sku"], grain="product_dimension")
    model = FakeModel(kind="other", roles={"sku": "product_id"})

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert record["kind"] == "product_catalog"
    assert record["kind_source"] == "grain"
    assert record["roles"] == {"sku": "product_id"}
    assert "Give the meaning" in model.calls[-1][0]
    assert "Roles:\n- order_id:" not in model.calls[-1][0]


def test_unmapped_grain_keeps_other_from_model() -> None:
    table = _table("t_one", ["memo"], grain="unknown")
    model = FakeModel(kind="other")

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert record["kind"] == "other"
    assert record["kind_source"] == "model"


def test_unmapped_grain_keeps_other_after_failed_kind_call() -> None:
    table = _table("t_one", ["memo"], grain="unknown")
    model = RoleScriptModel(kind_fails=True)

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert "error" not in record
    assert record["kind"] == "other"
    assert record["kind_source"] == "model"


def test_sales_without_sales_roles_is_downgraded_for_unmapped_grain() -> None:
    table = _table("t_one", ["code", "unit"], grain="unknown")
    model = FakeModel(kind="sales", roles={"code": "product_id", "unit": "unit"})

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert record["kind"] == "other"
    assert record["kind_source"] == "model"
    # "unit" is a sales-only role, so it is no longer valid and becomes "other".
    assert record["roles"] == {"code": "product_id", "unit": "other"}


def test_sales_without_sales_roles_is_downgraded_for_non_sales_grain() -> None:
    table = _table("t_one", ["sku"], grain="product_dimension")
    model = FakeModel(kind="sales", roles={"sku": "product_id"})

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert record["kind"] == "other"
    assert record["roles"] == {"sku": "product_id"}


def test_sales_grain_is_never_downgraded() -> None:
    table = _table("t_one", ["code"], grain="order_header")
    model = FakeModel(kind="sales", roles={"code": "product_id"})

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert record["kind"] == "sales"
    assert record["kind_source"] == "model"
    assert record["roles"] == {"code": "product_id"}


def test_delivery_scope_has_delivery_qty_role() -> None:
    assert SCOPE["delivery"]["delivery_qty"] == "quantity delivered"


def test_needs_reannotation_cases() -> None:
    mapped = [_table("t_one", ["a"], grain="snapshot")]
    unmapped = [_table("t_two", ["a"], grain="unknown")]

    assert needs_reannotation({"table": "t_one", "error": "RuntimeError: boom"}, unmapped) is True
    assert needs_reannotation({"table": "t_one", "error": "RuntimeError: boom"}, mapped) is True
    assert needs_reannotation({"table": "t_one", "kind": "other"}, mapped) is True
    assert needs_reannotation({"table": "t_two", "kind": "other"}, unmapped) is False
    assert needs_reannotation({"table": "t_one", "kind": "inventory"}, mapped) is False
    assert needs_reannotation({"table": "t_two", "kind": "sales"}, unmapped) is False


def _ingested_status_context(tmp_path: Path) -> tuple[BusinessContext, list[dict[str, Any]]]:
    """A sales table as ingestion leaves it: an unrecognized status is NULL, its original is in __raw_json."""
    database = tmp_path / "source.sqlite"
    con = sqlite3.connect(database)
    con.execute('CREATE TABLE "sales" ("state" TEXT, __source_row INTEGER, __raw_json TEXT)')
    rows = [(None, ["Bom hang"]), ("completed", ["completed"]), (None, ["Mystery"]), (None, [None])]
    con.executemany(
        'INSERT INTO "sales" VALUES (?, ?, ?)',
        [(stored, number, json.dumps(raw)) for number, (stored, raw) in enumerate(rows, start=1)],
    )
    con.commit()
    con.close()
    context = BusinessContext(
        business_id="shop_a", revision="rev-1", database=database, readme="", manifest={},
        tables=[_table("sales", ["state"])],
    )
    return context, [{"columns": ["state"], "kind": "sales", "roles": {"state": "order_status"}}]


def test_order_status_values_includes_unrecognized_originals_from_raw(tmp_path: Path) -> None:
    context, records = _ingested_status_context(tmp_path)

    assert order_status_values(context, records) == ["Bom hang", "Mystery"]


def test_annotate_status_maps_a_recovered_original(tmp_path: Path) -> None:
    context, records = _ingested_status_context(tmp_path)
    model = StatusModel(reply={"Bom hang": "cancelled", "Mystery": "unknown"})

    result = Annotator(model, workers=1).annotate_status(context, records)

    assert result == {"Bom hang": "cancelled"}


class ValueModel:
    """Answers each call with handler(system, prompt); records every call."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls: list[tuple[str, str]] = []
        self.usage = None

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> Any:
        self.calls.append((system, prompt))
        return self.handler(system, prompt)


def _sent(prompt: str) -> list[str]:
    return json.loads(prompt.split("Values:\n", 1)[1])


def _raise(system: str, prompt: str) -> Any:
    raise RuntimeError("down")


def test_payment_mapping_keeps_known_keys_and_drops_unknown_and_invalid() -> None:
    reply = {"Chuyển khoản": "transfer", "CK": "transfer", "Ví MoMo": "ewallet", "Lạ": "unknown",
             "Thẻ": "maybe", "Ghost": "cash"}
    model = ValueModel(lambda system, prompt: reply)

    result = Annotator(model, workers=1).group_values("payment_method", ["Chuyển khoản", "CK", "Ví MoMo", "Lạ", "Thẻ"])

    assert result == {"Chuyển khoản": "transfer", "CK": "transfer", "Ví MoMo": "ewallet"}
    system, _ = model.calls[0]
    for key, meaning in PAYMENT_CANONICAL.items():
        assert f"- {key}: {meaning}" in system
    assert "- unknown: no key fits" in system


def test_grouping_picks_most_frequent_spelling_then_shortest() -> None:
    model = ValueModel(lambda system, prompt: {"groups": [["Bán lẻ tại quầy", "Bán lẻ"]]})
    values = ["Bán lẻ", "Bán lẻ tại quầy", "Bán sỉ"]

    frequent = Annotator(model, workers=1).group_values(
        "channel", values, {"Bán lẻ": 2, "Bán lẻ tại quầy": 5, "Bán sỉ": 1})
    tied = Annotator(model, workers=1).group_values("channel", values, {"Bán lẻ": 3, "Bán lẻ tại quầy": 3})

    assert frequent == {"Bán lẻ": "Bán lẻ tại quầy", "Bán lẻ tại quầy": "Bán lẻ tại quầy", "Bán sỉ": "Bán sỉ"}
    assert tied["Bán lẻ"] == tied["Bán lẻ tại quầy"] == "Bán lẻ"


def test_spelling_variants_of_one_name_are_one_unit_with_the_most_frequent_spelling() -> None:
    model = ValueModel(lambda system, prompt: {"groups": []})

    result = Annotator(model, workers=1).group_values("customer_group", ["Thân thiết", "thân thiết"],
                                                      {"Thân thiết": 1, "thân thiết": 3})

    assert result == {"Thân thiết": "thân thiết", "thân thiết": "thân thiết"}


def test_abbreviation_group_is_kept_and_unrelated_group_is_split() -> None:
    counts = {"Chuyển khoản": 1, "CK": 3, "Tiền mặt": 2}
    kept = ValueModel(lambda system, prompt: {"groups": [["Chuyển khoản", "CK"]]})
    split = ValueModel(lambda system, prompt: {"groups": [["Chuyển khoản", "CK", "Tiền mặt"]]})

    assert Annotator(kept, workers=1).group_values("channel", ["Chuyển khoản", "CK"], counts) == {
        "Chuyển khoản": "CK", "CK": "CK"}
    values = ["Chuyển khoản", "CK", "Tiền mặt"]
    assert Annotator(split, workers=1).group_values("channel", values, counts) == {v: v for v in values}


def test_values_the_model_leaves_out_map_to_themselves_and_repeats_are_ignored() -> None:
    model = ValueModel(lambda system, prompt: {"groups": [["Bán lẻ tại quầy", "Bán lẻ"], ["Bán lẻ", "Ma"]]})
    values = ["Bán lẻ", "Bán lẻ tại quầy", "Bán sỉ", "Đại lý"]

    result = Annotator(model, workers=1).group_values("channel", values)

    assert result == {"Bán lẻ": "Bán lẻ", "Bán lẻ tại quầy": "Bán lẻ", "Bán sỉ": "Bán sỉ", "Đại lý": "Đại lý"}


def test_long_lists_are_split_into_120_value_calls() -> None:
    model = ValueModel(lambda system, prompt: {"groups": [[v] for v in _sent(prompt)]})
    values = [f"item {i}" for i in range(250)]

    result = Annotator(model, workers=1).group_values("unit", values)

    assert [len(_sent(prompt)) for _, prompt in model.calls] == [120, 120, 10]
    assert result == {v: v for v in values}


def test_grouping_failure_returns_empty_for_every_role() -> None:
    values = ["Bán lẻ", "Bán sỉ"]

    assert Annotator(ValueModel(_raise), workers=1).group_values("channel", values) == {}
    assert Annotator(ValueModel(_raise), workers=1).group_values("payment_method", ["CK", "Tiền mặt"]) == {}
    assert Annotator(ValueModel(lambda s, p: ["not", "an", "object"]), workers=1).group_values("unit", values) == {}


def test_unsupported_role_is_rejected() -> None:
    model = ValueModel(lambda system, prompt: {})

    try:
        Annotator(model, workers=1).group_values("brand", ["a", "b"])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    assert model.calls == []


def _value_context(tmp_path: Path, rows: list[tuple[str | None, str | None, list[str | None]]],
                   revision: str = "rev-1") -> tuple[BusinessContext, list[dict[str, Any]]]:
    """A sales table with channel and unit columns. Rows are (stored channel, stored unit, original row)."""
    database = tmp_path / "source.sqlite"
    con = sqlite3.connect(database)
    con.execute('CREATE TABLE "sales" ("channel" TEXT, "unit" TEXT, __source_row INTEGER, __raw_json TEXT)')
    con.executemany(
        'INSERT INTO "sales" VALUES (?, ?, ?, ?)',
        [(channel, unit, number, json.dumps(raw)) for number, (channel, unit, raw) in enumerate(rows, start=1)],
    )
    con.commit()
    con.close()
    context = BusinessContext(
        business_id="shop_a", revision=revision, database=database, readme="", manifest={},
        tables=[_table("sales", ["channel", "unit"])],
    )
    return context, [{"columns": ["channel", "unit"], "kind": "sales",
                      "roles": {"channel": "channel", "unit": "unit"}}]


def _grouping_handler(channel_groups: list[list[str]]) -> Any:
    def handler(system: str, prompt: str) -> dict[str, Any]:
        if "(channel)" in system:
            return {"groups": channel_groups}
        return {"groups": [[v] for v in _sent(prompt)]}
    return handler


def test_annotate_value_groups_reads_raw_values_when_stored_is_null(tmp_path: Path) -> None:
    context, records = _value_context(tmp_path, [
        ("Bán lẻ", "cai", ["Bán lẻ", "cai"]),
        (None, "cai", ["Bán lẻ tại quầy", "cai"]),
        (None, "hop", ["Bán lẻ tại quầy", "hop"]),
    ])
    model = ValueModel(_grouping_handler([["Bán lẻ", "Bán lẻ tại quầy"]]))

    result = Annotator(model, workers=1).annotate_value_groups(context, records)

    assert result == {"channel": {"Bán lẻ": "Bán lẻ tại quầy"}}
    channel_prompts = [prompt for system, prompt in model.calls if "(channel)" in system]
    assert channel_prompts and "Bán lẻ tại quầy" in _sent(channel_prompts[0])


def test_annotate_value_groups_skips_roles_with_fewer_than_two_values(tmp_path: Path) -> None:
    context, records = _value_context(tmp_path, [("Bán lẻ", "cai", ["Bán lẻ", "cai"]),
                                                 ("Bán lẻ", "cai", ["Bán lẻ", "cai"])])
    model = ValueModel(_grouping_handler([]))

    assert Annotator(model, workers=1).annotate_value_groups(context, records) == {}
    assert model.calls == []


def test_load_value_groups_cache_hit_skips_model(tmp_path: Path) -> None:
    context, records = _value_context(tmp_path, [("Bán lẻ", "cai", ["Bán lẻ", "cai"]),
                                                 (None, "hop", ["Bán lẻ tại quầy", "hop"])])
    cache = tmp_path / "groups.json"
    cache.write_text(json.dumps({"revision": "rev-1", "value_groups": {"channel": {"Bán lẻ": "cached"}}}),
                     encoding="utf-8")
    model = ValueModel(_grouping_handler([["Bán lẻ", "Bán lẻ tại quầy"]]))

    result = load_value_groups(Annotator(model, workers=1), context, records, cache)

    assert result == {"channel": {"Bán lẻ": "cached"}}
    assert model.calls == []


def test_load_value_groups_does_not_cache_failure_and_caches_success(tmp_path: Path) -> None:
    context, records = _value_context(tmp_path, [("Bán lẻ", "cai", ["Bán lẻ", "cai"]),
                                                 (None, "hop", ["Bán lẻ tại quầy", "hop"])])
    cache = tmp_path / "groups.json"

    failed = load_value_groups(Annotator(ValueModel(_raise), workers=1), context, records, cache)

    assert failed == {}
    assert not cache.exists()

    recovered = load_value_groups(Annotator(ValueModel(_grouping_handler([["Bán lẻ", "Bán lẻ tại quầy"]])),
                                            workers=1), context, records, cache)

    # One row each, so the spellings tie and the shorter one wins.
    assert recovered == {"channel": {"Bán lẻ tại quầy": "Bán lẻ"}}
    assert json.loads(cache.read_text(encoding="utf-8")) == {"revision": "rev-1", "value_groups": recovered}


def test_load_value_groups_caches_a_successful_run_with_no_changes(tmp_path: Path) -> None:
    context, records = _value_context(tmp_path, [("Bán lẻ", "cai", ["Bán lẻ", "cai"]),
                                                 ("Bán sỉ", "hop", ["Bán sỉ", "hop"])])
    cache = tmp_path / "groups.json"

    assert load_value_groups(Annotator(ValueModel(_grouping_handler([])), workers=1), context, records, cache) == {}
    assert json.loads(cache.read_text(encoding="utf-8")) == {"revision": "rev-1", "value_groups": {}}

    model = ValueModel(_raise)
    assert load_value_groups(Annotator(model, workers=1), context, records, cache) == {}
    assert model.calls == []


def test_load_value_groups_with_another_revision_is_ignored_and_rewritten(tmp_path: Path) -> None:
    context, records = _value_context(tmp_path, [("Bán lẻ", "cai", ["Bán lẻ", "cai"]),
                                                 (None, "hop", ["Bán lẻ tại quầy", "hop"])])
    cache = tmp_path / "groups.json"
    cache.write_text(json.dumps({"revision": "rev-0", "value_groups": {"channel": {"Bán lẻ": "stale"}}}),
                     encoding="utf-8")

    result = load_value_groups(Annotator(ValueModel(_grouping_handler([["Bán lẻ", "Bán lẻ tại quầy"]])), workers=1),
                               context, records, cache)

    assert result == {"channel": {"Bán lẻ tại quầy": "Bán lẻ"}}
    assert json.loads(cache.read_text(encoding="utf-8")) == {"revision": "rev-1", "value_groups": result}


def test_failed_line_classification_is_recorded_and_taken_once() -> None:
    annotator = Annotator(StatusModel(fail=RuntimeError("down")), workers=1)

    result = annotator.annotate_line_kinds(_context([]), [], ["Shirt"], ["Coke", "Voucher 10"])

    assert result == {}
    assert annotator.take_failures() == [ModelFailure("line_kinds")]
    assert annotator.take_failures() == []


def test_successful_stage_records_no_failure() -> None:
    annotator = Annotator(StatusModel(reply={"Coke": "product", "Voucher 10": "discount"}), workers=1)

    annotator.annotate_line_kinds(_context([]), [], ["Shirt"], ["Coke", "Voucher 10"])

    assert annotator.take_failures() == []


def test_failed_value_grouping_records_failure_and_writes_no_cache(tmp_path: Path) -> None:
    context, records = _value_context(tmp_path, [("Bán lẻ", "cai", ["Bán lẻ", "cai"]),
                                                 (None, "hop", ["Bán lẻ tại quầy", "hop"])])
    cache = tmp_path / "groups.json"
    annotator = Annotator(ValueModel(_raise), workers=1)

    assert load_value_groups(annotator, context, records, cache) == {}
    assert not cache.exists()
    failures = annotator.take_failures()
    assert failures and all(item.stage.startswith("value_groups:") and item.table_name is None for item in failures)


class PartialStatusAnnotator(Annotator):
    """Returns a usable map but records a failure, as a stage that lost some of its calls would."""

    def annotate_status(self, context: BusinessContext, records: list[dict[str, Any]]) -> dict[str, str]:
        self._record_failure("status")
        return {"delivered": "completed"}


def test_load_status_map_skips_cache_when_a_failure_was_recorded(tmp_path: Path) -> None:
    context, records = _status_context(tmp_path, ["delivered"])
    cache = tmp_path / "status.json"

    result = load_status_map(PartialStatusAnnotator(StatusModel(), workers=1), context, records, cache)

    assert result == {"delivered": "completed"}
    assert not cache.exists()


SIBLING_ROLES = {
    "order_no": "order_id", "order_date": "ordered_at", "item": "product_name", "qty": "qty",
    "amount": "line_amount", "discount": "line_discount", "note": "note", "cashier": "employee_name",
    "shift": "shift",
}
SIBLING_COLUMNS = list(SIBLING_ROLES)


class ShapeModel:
    """Kind call answers `kind` for every table. Role calls raise when their prompt lists `broken_column`.
    Kind calls raise when their prompt lists `kind_broken_column`. Every other column takes its SIBLING_ROLES role."""

    def __init__(self, broken_column: str | None = None, kind_broken_column: str | None = None):
        self.broken_column = broken_column
        self.kind_broken_column = kind_broken_column
        self.calls: list[tuple[str, str]] = []
        self.usage = None

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]:
        self.calls.append((system, prompt))
        if system.startswith("Classify"):
            if self.kind_broken_column and f"- {self.kind_broken_column} |" in prompt:
                raise RuntimeError("kind down")
            return {"kind": "sales"}
        if self.broken_column and f"- {self.broken_column} |" in prompt:
            raise RuntimeError("invalid json")
        return {column: SIBLING_ROLES.get(column, "other") for column in _columns_in(prompt)}


def test_failed_table_takes_sibling_kind_and_roles_and_records_no_failure() -> None:
    good = _table("sales_jan", SIBLING_COLUMNS)
    # one column renamed: Jaccard 8/10 = 0.8 and 8 of 9 positions equal, so the sibling qualifies
    bad = _table("sales_feb", SIBLING_COLUMNS[:-1] + ["shifts"])
    model = ShapeModel(broken_column="shifts")
    annotator = Annotator(model, workers=1)

    records = annotator.annotate(_context([good, bad]))

    assert "error" not in records[1]
    assert records[1]["kind"] == "sales"
    assert records[1]["source"] == "sibling:sales_jan"
    assert records[1]["kind_source"] == "sibling"
    assert records[1]["roles"]["order_no"] == "order_id"
    assert records[1]["roles"]["shifts"] == "shift"  # no name match: the role at the same position
    assert annotator.take_failures() == []


def test_failed_table_with_no_sibling_stays_an_error_and_is_recorded() -> None:
    good = _table("sales_jan", SIBLING_COLUMNS)
    # six names in common: Jaccard 6/13 is under 0.8, and the counts differ, so nothing qualifies
    bad = _table("sales_feb", SIBLING_COLUMNS[:6] + ["extra_1", "extra_2", "extra_3", "extra_4"])
    model = ShapeModel(broken_column="extra_1")
    annotator = Annotator(model, workers=1)

    records = annotator.annotate(_context([good, bad]))

    assert "error" in records[1]
    assert "source" not in records[1]
    assert annotator.take_failures() == [ModelFailure("annotation:sales_feb", "sales_feb", "sales")]


def test_failed_kind_call_takes_sibling_kind_and_records_no_failure() -> None:
    good = _table("sales_jan", SIBLING_COLUMNS)
    bad = _table("sales_feb", SIBLING_COLUMNS[:-1] + ["shifts"], grain="unknown")
    model = ShapeModel(kind_broken_column="shifts")
    annotator = Annotator(model, workers=1)

    records = annotator.annotate(_context([good, bad]))

    assert records[1]["kind"] == "sales"
    assert records[1]["source"] == "sibling:sales_jan"
    assert annotator.take_failures() == []


def test_failed_kind_call_without_sibling_is_recorded_as_kind_failure() -> None:
    bad = _table("sales_feb", SIBLING_COLUMNS, grain="unknown")
    model = ShapeModel(kind_broken_column="order_no")
    annotator = Annotator(model, workers=1)

    records = annotator.annotate(_context([bad]))

    assert records[0]["kind"] == "other"
    assert "source" not in records[0]
    assert annotator.take_failures() == [ModelFailure("kind:sales_feb", "sales_feb", None)]


def test_a_recovered_kind_call_records_no_failure_and_retries_with_a_shorter_prompt() -> None:
    table = _table("t_one", ["ref_no", "amount"])
    model = FlakyModel(1, kind="sales", roles={"ref_no": "order_id", "amount": "line_amount"})
    annotator = Annotator(model, workers=1)

    record = annotator.annotate(_context([table]))[0]

    assert record["kind"] == "sales"
    assert record["kind_source"] == "model"
    assert "Documentation:" not in model.calls[1][1]  # the retried kind call is the shorter prompt
    assert annotator.take_failures() == []


def test_sibling_fit_needs_eighty_percent_by_name_or_by_position() -> None:
    nine = [f"c{i}" for i in range(9)]
    assert _sibling_fit(nine, nine) == (1.0, True)
    assert _sibling_fit(nine, nine[:-1] + ["other"]) == (0.8, True)  # 8/10 by name, 8/9 by position
    assert _sibling_fit(["a", "b", "c", "d"], ["a", "b", "c", "e"]) is None  # 3/5 by name
    assert _sibling_fit(["a", "b", "c", "d", "e"], ["e", "a", "b", "c", "d"]) == (1.0, False)  # same names, shifted


def test_booking_table_answered_other_is_promoted_to_sales() -> None:
    table = _table("t_booking", ["ma_phieu", "ngay_dat", "thanh_tien"], grain="unknown")
    roles = {"ma_phieu": "order_id", "ngay_dat": "ordered_at", "thanh_tien": "line_amount"}
    model = FakeModel(kind="other", roles=roles)

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert record["kind"] == "sales"
    assert record["kind_source"] == "roles"
    assert record["roles"] == roles
    # The role call for an "other" answer lists the sales roles, so the amount can be seen.
    assert "line_amount:" in model.calls[-1][0]


def test_price_list_answered_other_stays_other() -> None:
    table = _table("t_prices", ["ten_hang", "don_gia"], grain="unknown")
    model = FakeModel(kind="other", roles={"ten_hang": "product_name", "don_gia": "unit_price"})

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert record["kind"] == "other"
    assert record["kind_source"] == "model"
    # unit_price is a sales role, so outside the "other" scope it becomes "other".
    assert record["roles"] == {"ten_hang": "product_name", "don_gia": "other"}


def test_other_table_with_order_and_date_but_no_amount_stays_other() -> None:
    table = _table("t_log", ["ma_phieu", "ngay_dat", "ghi_chu"], grain="unknown")
    model = FakeModel(kind="other", roles={"ma_phieu": "order_id", "ngay_dat": "ordered_at", "ghi_chu": "note"})

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert record["kind"] == "other"
    assert record["kind_source"] == "model"
    assert record["roles"] == {"ma_phieu": "order_id", "ngay_dat": "ordered_at", "ghi_chu": "note"}


def test_promotion_and_demotion_are_logged_with_table_name_only(caplog: Any) -> None:
    booking = _table("t_booking", ["ma_phieu", "ngay_dat", "thanh_tien"], grain="unknown")
    orphan = _table("t_orphan", ["code"], grain="unknown")
    roles = {"ma_phieu": "order_id", "ngay_dat": "ordered_at", "thanh_tien": "line_amount"}
    model = FakeModel(kind="other", roles=roles)

    with caplog.at_level("INFO", logger="bizreason.semantic.annotator"):
        Annotator(model, workers=1).annotate(_context([booking]))
    promoted = [r.getMessage() for r in caplog.records if "promoted" in r.getMessage()]
    assert len(promoted) == 1 and "t_booking" in promoted[0]
    assert "ma_phieu" not in promoted[0]

    caplog.clear()
    model = FakeModel(kind="sales", roles={"code": "product_id"})
    with caplog.at_level("INFO", logger="bizreason.semantic.annotator"):
        record = Annotator(model, workers=1).annotate(_context([orphan]))[0]
    demoted = [r.getMessage() for r in caplog.records if "demoted" in r.getMessage()]
    assert record["kind"] == "other"
    assert len(demoted) == 1 and "t_orphan" in demoted[0]


def test_sales_answer_with_order_roles_keeps_model_source() -> None:
    table = _table("t_booking", ["ma_phieu", "ngay_dat", "thanh_tien"], grain="unknown")
    roles = {"ma_phieu": "order_id", "ngay_dat": "ordered_at", "thanh_tien": "line_amount"}
    model = FakeModel(kind="sales", roles=roles)

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert record["kind"] == "sales"
    assert record["kind_source"] == "model"
