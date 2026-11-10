from bizreason.contracts import ColumnMeta, SourceRef, TableMeta
from bizreason.schema_context import compact_catalog
from bizreason.utils import dumps


def test_partition_compaction_keeps_every_name_and_the_shared_contract():
    tables = [
        TableMeta(
            name=f"month_{i}",
            sources=[SourceRef(path="sales.xlsx", table=str(i))],
            grain="order_line",
            row_count=10,
            raw_row_count=10,
            columns=[
                ColumnMeta(
                    name="amount",
                    original_name="Thành tiền",
                    dtype="INTEGER",
                    semantic_role="line_amount",
                    unit="VND",
                    examples=[100],
                    parse_errors=1 if i == 2 else 0,
                )
            ],
        )
        for i in range(12)
    ]
    compact = compact_catalog(tables)
    assert len(compact) == 1
    assert [entry["name"] for entry in compact[0]["physical_tables"]] == [
        table.name for table in tables
    ]
    assert compact[0]["shared_columns"][0]["unit"] == "VND"
    assert compact[0]["shared_columns"][0]["parse_errors_across_partitions"] == 1
    assert len(dumps(compact)) < len(dumps([table.model_dump(mode="json") for table in tables]))
    assert "examples" not in compact_catalog(tables, samples=False)[0]["shared_columns"][0]


def test_validator_requires_overlapping_partitions_and_preserves_future_return_policy(
    business, rules, plan_factory
):
    from bizreason.contracts import Question
    from bizreason.planning import validate_plan

    original = next(table for table in business.tables if table.grain == "order_line")
    field = original.role("ordered_at")
    later = original.model_copy(
        update={"name": "later_partition", "coverage": {field: ("2026-04-01", "2026-04-30")}}
    )
    context = business.model_copy(update={"tables": [*business.tables, later]})
    question = Question(
        id="q",
        business_id=business.business_id,
        question="doanh thu tháng này",
        as_of_date="2026-03-04",
    )
    plan = plan_factory()
    assert not any(
        error.startswith("MISSING_PARTITION")
        for error in validate_plan(plan, context, rules, question)
    )
    overlapping = later.model_copy(update={"coverage": {field: ("2026-03-01", "2026-04-30")}})
    context = business.model_copy(update={"tables": [*business.tables, overlapping]})
    assert "MISSING_PARTITION:later_partition" in validate_plan(plan, context, rules, question)
