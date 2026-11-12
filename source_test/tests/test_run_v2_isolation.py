"""bizreason.run_v2 must not load the legacy free-SQL pipeline, and build_v2_context must match cli._builder."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

from bizreason.data.ingestion import BusinessBuilder
from bizreason.utils import WORKSPACE
from bizreason.v2_setup import build_v2_context

SOURCE_TEST = Path(__file__).resolve().parents[1]

ISOLATION_PROBE = (
    "import sys; import bizreason.run_v2; "
    "bad=[m for m in ('bizreason.sql_healers','bizreason.plan_input','bizreason.pipeline',"
    "'bizreason.triage','bizreason.planning','bizreason.compilation','bizreason.cli') "
    "if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)"
)

RULEBOOK = """# Business rules
## 1. Đơn hàng và doanh thu
Đơn không huỷ và không trả hàng. Đơn đang giao vẫn tính.
Doanh thu = thành tiền các dòng hàng; không cộng thuế VAT, không gồm phí giao hàng.
## 2. Thời gian
Ngày đặt, khoảng ngày đến hết ngày chốt.
Cùng một nhóm, đồng dấu hoặc kế thừa.
"""


def _probe(code: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=SOURCE_TEST,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "[]"


def test_run_v2_does_not_load_legacy_modules() -> None:
    _probe(ISOLATION_PROBE)


def test_python_m_bizreason_entrypoint_does_not_load_legacy_modules() -> None:
    _probe(ISOLATION_PROBE.replace("import bizreason.run_v2", "import bizreason.__main__"))


@pytest.fixture
def workspace_tmp():
    # BusinessBuilder only accepts cache roots inside WORKSPACE, so the temp dir lives there (as in conftest).
    root = WORKSPACE / "artifacts" / "test_tmp"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        yield Path(directory)


@pytest.mark.parametrize("with_calendar", [False, True])
def test_build_v2_context_matches_cli_builder(workspace_tmp: Path, with_calendar: bool) -> None:
    from bizreason.cli import _builder  # legacy import, deliberately only in this test

    data = workspace_tmp / "data_root"
    data.mkdir()
    (data / "rulebook.md").write_text(RULEBOOK, encoding="utf-8")
    if with_calendar:
        rows = ["date,lunar_year"] + [f"2026-01-{day:02d},2026" for day in range(1, 29)]
        (data / "calendar.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    legacy_builder, legacy_rules, legacy_temporal = _builder(data, workspace_tmp / "cache_legacy")
    v2_builder, v2_rules, v2_temporal = build_v2_context(data, workspace_tmp / "cache_v2")

    assert type(v2_builder) is BusinessBuilder
    assert type(v2_builder) is type(legacy_builder)
    assert v2_builder.rule_hash == legacy_builder.rule_hash
    assert v2_builder.inheritance_allowed == legacy_builder.inheritance_allowed
    assert v2_builder.implementation_hash == legacy_builder.implementation_hash
    assert v2_builder.cache_root == workspace_tmp / "cache_v2"

    assert v2_rules.revision == legacy_rules.revision
    assert v2_rules.inheritance_allowed == legacy_rules.inheritance_allowed
    assert [r.id for r in v2_rules.records] == [r.id for r in legacy_rules.records]
    assert v2_builder.rules.revision == legacy_builder.rules.revision

    assert (v2_temporal.lunar is None) == (legacy_temporal.lunar is None)
    if with_calendar:
        assert v2_temporal.lunar.rows == legacy_temporal.lunar.rows
    for question in ["doanh thu hôm nay", "tháng này so với tháng trước", "năm nay"]:
        as_of = date(2026, 1, 28)
        assert v2_temporal.resolve(question, as_of) == legacy_temporal.resolve(question, as_of)
