from __future__ import annotations

from datetime import date

import pytest

from bizreason.run_v2 import parse_args
from bizreason.semantic import solver as solver_mod
from bizreason.semantic.time_reader import TimeReader, rulebook_time_section, validate_periods
from bizreason.temporal import TemporalResolver

AS_OF = date(2026, 4, 10)
Q_DAYS = "ngày 01/03/2026 khách đặt, đến khi giao thì đã là ngày 05/03/2026"


class Fake:
    def __init__(self, payload=None, exc=None):
        self.payload, self.exc, self.calls = payload, exc, 0

    def generate_json(self, system, prompt, *, timeout):
        self.calls += 1
        self.prompt = prompt
        if self.exc:
            raise self.exc
        return self.payload


def periods(*items):
    return {"periods": [{"start": s, "end": e, "role": r} for s, e, r in items]}


def read(question, payload, as_of=AS_OF):
    return TimeReader(Fake(payload)).read(question, as_of)


def test_accepts_grounded_days():
    out = read(Q_DAYS, periods(("2026-03-01", "2026-03-01", "target"), ("2026-03-05", "2026-03-05", "comparison")))
    assert out and out[0].start == date(2026, 3, 1) and out[1].end_exclusive == date(2026, 3, 6)
    assert "model_time" in out[0].evidence


def test_ungrounded_boundary_rejects_everything():
    assert read(Q_DAYS, periods(("2026-03-01", "2026-03-05", "target"), ("2026-02-15", "2026-02-20", "comparison"))) is None
    assert read(Q_DAYS, periods(("2026-02-15", "2026-03-05", "target"))) is None


@pytest.mark.parametrize("payload", [
    periods(("2026-03-05", "2026-03-01", "target")),
    periods(("2026-03-01", "2026-03-05", "other")),
    periods(*[("2026-03-01", "2026-03-05", "target")] * 5),
    periods(("2026-03-01", "2026-03-05", "comparison")),
    periods(("2026-03-01", "2026-03-05", "target"), ("2026-03-01", "2026-03-05", "target"),
            ("2026-03-01", "2026-03-05", "comparison")),
    ["x"], {"periods": []}, {"periods": [{"start": "03/01/2026", "end": "2026-03-05", "role": "target"}]},
])
def test_invalid_shapes(payload):
    assert read(Q_DAYS, payload) is None


def test_exception_is_none_with_reason():
    reader = TimeReader(Fake(exc=RuntimeError("boom")))
    assert reader.read(Q_DAYS, AS_OF) is None and "boom" in reader.last_reason


def test_month_with_year():
    out = read("doanh thu tháng 3/2026?", periods(("2026-03-01", "2026-03-31", "target")))
    assert out and out[0].end_exclusive == date(2026, 4, 1)
    assert read("doanh thu tháng 3/2026?", periods(("2026-03-01", "2026-03-30", "target"))) is None


def test_month_without_year_follows_rulebook_year_rule():
    feb = date(2026, 2, 10)
    assert read("tháng 3 bán được gì", periods(("2025-03-01", "2025-03-31", "target")), feb)
    assert read("tháng 3 bán được gì", periods(("2026-03-01", "2026-03-31", "target")), feb) is None
    assert read("tháng 3 bán được gì", periods(("2026-03-01", "2026-03-31", "target")), AS_OF)


def test_quarter_and_year():
    assert read("quý 2/2025", periods(("2025-04-01", "2025-06-30", "target")))
    assert read("năm 2025", periods(("2025-01-01", "2025-12-31", "target")))


def test_last_n_days():
    assert read("30 ngày gần nhất", periods(("2026-03-12", "2026-04-10", "target")))
    assert read("30 ngày gần nhất", periods(("2026-03-01", "2026-04-10", "target"))) is None
    # no number written: today-29 is not grounded
    assert read("gần đây", periods(("2026-03-12", "2026-04-10", "target"))) is None


def test_comparison_adjacent_with_written_count():
    q = "7 ngày gần nhất so với 7 ngày trước đó"
    ok = periods(("2026-04-04", "2026-04-10", "target"), ("2026-03-28", "2026-04-03", "comparison"))
    assert read(q, ok)
    assert read("so với kỳ trước", periods(("2026-04-10", "2026-04-10", "target"),
                                          ("2026-04-03", "2026-04-09", "comparison"))) is None


def test_rulebook_section_extraction():
    rb = "# R\n## 1. Đơn\nabc\n## 2. Thời gian\nluật X\n### con\nY\n## 3. Khác\nZ\n"
    sec = rulebook_time_section(rb)
    assert "luật X" in sec and "Y" in sec and "Z" not in sec
    fake = Fake(periods(("2026-03-01", "2026-03-31", "target")))
    TimeReader(fake, rulebook_text=rb).read("tháng 3/2026", AS_OF)
    assert "luật X" in fake.prompt
    fake2 = Fake(periods(("2026-03-01", "2026-03-31", "target")))
    TimeReader(fake2, rulebook_text="# R\n## Khác\nZ").read("tháng 3/2026", AS_OF)
    assert "Time rules" not in fake2.prompt


class _Q:
    def __init__(self, question):
        self.question, self.as_of_date = question, AS_OF


def _hook(question, reader):
    """Mirror of the solver hook through the real module-level setter."""
    solver_mod.set_time_reader(reader)
    try:
        windows = TemporalResolver().resolve(question, AS_OF)
        if not windows and solver_mod._TIME_READER is not None:
            windows = solver_mod._TIME_READER.read(question, AS_OF) or windows
        return windows
    finally:
        solver_mod.set_time_reader(None)


def test_hook_not_called_when_rules_give_window():
    fake = Fake(periods(("2026-03-01", "2026-03-31", "target")))
    assert _hook("doanh thu tháng 3/2026", TimeReader(fake))
    assert fake.calls == 0


def test_solver_source_has_hook_after_resolve():
    import inspect
    src = inspect.getsource(solver_mod)
    assert "_TIME_READER.read(question.question, question.as_of_date)" in src
    assert solver_mod._TIME_READER is None


def test_run_v2_flag_default_off_and_manifest():
    import inspect
    from bizreason import run_v2
    base = ["--data", "d", "--out", "o", "--connection", "c"]
    assert parse_args(base).time_reader == "off"
    assert parse_args(base + ["--time-reader", "on"]).time_reader == "on"
    assert '"time_reader": args.time_reader' in inspect.getsource(run_v2)
    assert "set_time_reader(None)" in inspect.getsource(run_v2)
