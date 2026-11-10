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
    relevant_readme,
)
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


def _table(name: str, columns: list[str], path: str = "data/source_a.csv", original: dict[str, str] | None = None) -> TableMeta:
    original = original or {}
    return TableMeta(
        name=name,
        sources=[SourceRef(path=path)],
        columns=[
            ColumnMeta(name=c, original_name=original.get(c, c), dtype="string", examples=["x", "y"])
            for c in columns
        ],
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
    # attempt 1: kind call. retry: kind call (falls back to "other"), role call, one batch call.
    assert len(model.calls) == 4


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
    # 25 columns go straight to batches: role call 1 covers the first 15 columns, call 2 the last 10.
    model = RoleScriptModel(fail_role_calls={2})

    record = Annotator(model, workers=1).annotate(_context([table]))[0]

    assert record["partial"] is True
    assert "error" not in record
    assert record["kind"] == "sales"
    assert set(record["roles"]) == {f"col_{i:02d}" for i in range(15)}
    assert set(record["roles"].values()) == {"order_id"}


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
    assert model.calls[2][0].startswith("Give the meaning")


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
