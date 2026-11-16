from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from bizreason.config import Settings
from bizreason.contracts import ExecutionStatus, Question
from bizreason.execution import (
    execute_sql,
    get_worker_pool,
    shutdown_sql_pool,
)
from bizreason.planning import Planner
from bizreason.temporal import TemporalResolver
from bizreason.utils import (
    WORKSPACE,
    PathPolicy,
    configure_path_policy,
    digest,
    dumps,
    prompt_dumps,
    register_input_data_dir,
    writable_path,
)


def test_writable_path_dev_mode(workspace_tmp):
    # Allowed inside WORKSPACE
    valid_path = workspace_tmp / "output.json"
    assert writable_path(valid_path) == valid_path.resolve()

    # Rejected outside WORKSPACE in default dev mode
    outside_path = workspace_tmp.parents[4] / "outside.json"
    with pytest.raises(ValueError, match=f"Outputs must stay within {WORKSPACE.name}/"):
        writable_path(outside_path)


def test_writable_path_competition_mode(workspace_tmp):
    with tempfile.TemporaryDirectory() as external_tmp:
        ext_root = Path(external_tmp).resolve()
        with PathPolicy(mode="competition", allowed_roots=[ext_root]):
            # Paths in configured allowed roots are permitted
            allowed_file = ext_root / "results" / "answer.json"
            assert writable_path(allowed_file) == allowed_file.resolve()

            # Paths inside WORKSPACE remain permitted
            ws_file = workspace_tmp / "cache" / "local.json"
            assert writable_path(ws_file) == ws_file.resolve()

            # Arbitrary system paths outside WORKSPACE and allowed roots are rejected
            system_file = ext_root.parent / "escape.json"
            with pytest.raises(ValueError, match="Outputs must stay within"):
                writable_path(system_file)


def test_writable_path_forbidden_roots(workspace_tmp):
    # In competition mode, /data is unconditionally forbidden
    with PathPolicy(mode="competition"):
        with pytest.raises(ValueError, match="Refusing to write inside read-only data directory"):
            writable_path(Path("/data/sales.csv"))

    # Explicitly registered forbidden root / input data directory is rejected unconditionally
    dataset_dir = workspace_tmp / "input_dataset"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    with PathPolicy(mode="competition", forbidden_roots=[dataset_dir]):
        with pytest.raises(ValueError, match="Refusing to write inside read-only data directory"):
            writable_path(dataset_dir / "records.csv")

    # Globally registered input data directory
    global_dataset = workspace_tmp / "registered_input"
    global_dataset.mkdir(parents=True, exist_ok=True)
    register_input_data_dir(global_dataset)
    try:
        with pytest.raises(ValueError, match="Refusing to write inside read-only data directory"):
            writable_path(global_dataset / "sub" / "file.parquet")
    finally:
        configure_path_policy(forbidden_roots=[])


def test_path_policy_env_vars(monkeypatch):
    with tempfile.TemporaryDirectory() as ext:
        ext_path = Path(ext).resolve()
        monkeypatch.setenv("BIZREASON_RUNTIME_MODE", "competition")
        monkeypatch.setenv("BIZREASON_ALLOWED_WRITE_ROOTS", str(ext_path))

        assert writable_path(ext_path / "submission.json") == (ext_path / "submission.json").resolve()
        with pytest.raises(ValueError, match="Refusing to write inside read-only data directory"):
            writable_path(Path("/data/input.csv"))


class RecordingModel:
    def __init__(self, table_name: str, rule_ref: str = "rb:01") -> None:
        self.table_name = table_name
        self.rule_ref = rule_ref
        self.prompts: list[str] = []

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict:
        self.prompts.append(prompt)
        return {
            "metric": "generic_sql",
            "tables": [self.table_name],
            "rule_refs": [self.rule_ref],
            "sql": f'SELECT COUNT(*) FROM "{self.table_name}"',
            "output": {"type": "number", "shape": "scalar"},
        }


def test_prefix_cache_friendly_prompt_ordering(business, rules):
    q1 = Question(
        id="q1",
        business_id=business.business_id,
        question="Tổng doanh thu tháng này là bao nhiêu?",
        as_of_date="2026-03-04",
    )
    q2 = Question(
        id="q2",
        business_id=business.business_id,
        question="Danh sách 5 khách hàng nợ nhiều nhất đến nay",
        as_of_date="2026-03-04",
    )

    model = RecordingModel(business.tables[0].name, rules.records[0].id)
    planner = Planner(model, rules, TemporalResolver(), Settings())

    plan1 = planner.make_plan(q1, business, deadline=time.monotonic() + 10)
    plan2 = planner.make_plan(q2, business, deadline=time.monotonic() + 10)

    assert plan1 is not None and plan2 is not None
    assert len(model.prompts) == 2
    prompt1, prompt2 = model.prompts[0], model.prompts[1]

    # Valid JSON serialization
    data1 = json.loads(prompt1)
    data2 = json.loads(prompt2)

    expected_keys = [
        "runtime_rules",
        "plan_field_guide",
        "nested_field_definitions_not_output_fields",
        "required_rule_refs_by_metric",
        "business_readme",
        "declared_data_end",
        "catalog",
        "joins_verified_on_full_data",
        "context_diagnostics",
        "question",
        "temporal_hints_not_a_substitute_for_reading_question",
        "entity_retrieval_not_probabilities",
        "controller_budget",
        "probe_results",
        "feedback_on_latest_candidate",
    ]

    for key in expected_keys:
        assert key in data1
        assert key in data2

    # Preserves strict Global Static -> Business Static -> Question Dynamic key order
    assert list(data1.keys()) == expected_keys
    assert list(data2.keys()) == expected_keys

    # Exact matching character prefix up to "question" key
    marker = '"question"'
    assert marker in prompt1 and marker in prompt2
    prefix1 = prompt1[: prompt1.index(marker)]
    prefix2 = prompt2[: prompt2.index(marker)]

    assert prefix1 == prefix2
    assert len(prefix1) > 500
    assert '"runtime_rules"' in prefix1
    assert '"business_readme"' in prefix1
    assert '"catalog"' in prefix1
    assert '"joins_verified_on_full_data"' in prefix1

    # dumps must preserve sort_keys=True for SHA-256 digest stability
    mapping = {"z": 10, "a": 20, "m": 30}
    assert dumps(mapping) == '{"a": 20, "m": 30, "z": 10}'
    assert prompt_dumps(mapping) == '{"z": 10, "a": 20, "m": 30}'
    assert digest(mapping) == digest({"a": 20, "z": 10, "m": 30})


def test_sqlite_pool_consecutive_queries_and_isolation(business):
    shutdown_sql_pool()
    t1 = business.tables[0].name
    t2 = business.tables[1].name

    # 1. Valid query on Table 1
    r1 = execute_sql(business.database, f'SELECT COUNT(*) FROM "{t1}"', [], [t1], timeout=5)
    assert r1.status == ExecutionStatus.SUCCESS
    assert len(r1.rows) == 1
    count1 = r1.rows[0][0]

    # 2. Denied read on unallowed Table 2
    r2 = execute_sql(business.database, f'SELECT * FROM "{t2}"', [], [t1], timeout=5)
    assert r2.status == ExecutionStatus.POLICY_DENIED
    assert r2.error_code == "READ_OUTSIDE_CATALOG"

    # 3. Denied read on internal __ column
    r3 = execute_sql(business.database, f'SELECT __source_row FROM "{t1}"', [], [t1], timeout=5)
    assert r3.status in {ExecutionStatus.POLICY_DENIED, ExecutionStatus.EXECUTION_ERROR}

    # 4. Valid query on Table 1 again succeeds cleanly with zero state leakage
    r4 = execute_sql(business.database, f'SELECT COUNT(*) FROM "{t1}"', [], [t1], timeout=5)
    assert r4.status == ExecutionStatus.SUCCESS
    assert r4.rows[0][0] == count1


def test_sqlite_pool_kills_and_replaces_hung_worker(business):
    shutdown_sql_pool()
    t1 = business.tables[0].name

    # Prime the pool with a valid query
    r1 = execute_sql(business.database, f'SELECT COUNT(*) FROM "{t1}"', [], [t1], timeout=5)
    assert r1.status == ExecutionStatus.SUCCESS

    pool = get_worker_pool()
    assert len(pool._idle_workers) == 1
    initial_worker = pool._idle_workers[0]
    initial_pid = initial_worker.process.pid
    assert initial_worker.is_alive()

    # Hanging recursive CTE query with short timeout
    cte_query = "WITH RECURSIVE infinite(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM infinite) SELECT count(*) FROM infinite;"
    start = time.monotonic()
    r_timeout = execute_sql(business.database, cte_query, [], [t1], timeout=0.3)
    elapsed = time.monotonic() - start

    assert r_timeout.status == ExecutionStatus.TIMEOUT
    assert elapsed < 3.0

    # Worker must be killed/discarded, not retained in the idle pool
    assert not initial_worker.is_alive()
    assert initial_worker not in pool._idle_workers

    # Next query immediately succeeds using a fresh replacement worker
    r2 = execute_sql(business.database, f'SELECT COUNT(*) FROM "{t1}"', [], [t1], timeout=5)
    assert r2.status == ExecutionStatus.SUCCESS
    assert r2.rows == r1.rows

    # Replacement worker is active and has a different PID
    assert len(pool._idle_workers) == 1
    new_worker = pool._idle_workers[0]
    assert new_worker.is_alive()
    assert new_worker.process.pid != initial_pid


def test_settings_and_pool_switch(business):
    s = Settings()
    assert s.runtime_mode == "dev"
    assert s.use_sql_worker_pool is True

    # Fallback to disposable process when use_pool=False
    r = execute_sql(
        business.database,
        f'SELECT COUNT(*) FROM "{business.tables[0].name}"',
        [],
        [business.tables[0].name],
        timeout=15,
        use_pool=False,
    )
    assert r.status == ExecutionStatus.SUCCESS
