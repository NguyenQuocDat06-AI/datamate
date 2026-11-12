"""`python -m bizreason` must run the V2 pipeline entry point, run_v2.main."""

from __future__ import annotations

import runpy

import pytest

from bizreason import run_v2


def test_python_m_bizreason_runs_run_v2_main(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def fake_main(argv: list[str] | None = None) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(run_v2, "main", fake_main)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("bizreason", run_name="__main__")
    assert exit_info.value.code == 0
    assert calls == [None]  # argv is read by run_v2.parse_args from sys.argv


def test_python_m_bizreason_propagates_run_v2_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_v2, "main", lambda argv=None: 3)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("bizreason", run_name="__main__")
    assert exit_info.value.code == 3
