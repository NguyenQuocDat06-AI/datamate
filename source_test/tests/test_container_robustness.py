"""Start-up robustness of the container path: quantization fallback and echoing the server log (no GPU needed)."""
from __future__ import annotations

import pytest

from run_submission import server_log_tail
from serve.local_server import QUANT_ENV, load_with_quant_fallback, quant_mode


class FakeOom(RuntimeError):
    pass


def test_quant_mode_defaults_to_nf4_and_reads_the_override():
    assert quant_mode({}) == "nf4"
    assert quant_mode({QUANT_ENV: "fp16"}) == "fp16"
    assert quant_mode({QUANT_ENV: " FP16 "}) == "fp16"
    assert quant_mode({QUANT_ENV: "int8"}) == "nf4"  # an unknown value is not a silent new mode


def test_nf4_success_is_used_as_is():
    calls = []
    model, mode = load_with_quant_fallback(lambda quantized: calls.append(quantized) or "m", "nf4", lambda m: None)
    assert (model, mode, calls) == ("m", "nf4", [True])


def test_nf4_failure_falls_back_to_fp16_and_releases_memory(capsys):
    logs, released, calls = [], [], []

    def load(quantized):
        calls.append(quantized)
        if quantized:
            raise ImportError("bitsandbytes has no kernel for this GPU")
        return "fp16-model"

    model, mode = load_with_quant_fallback(load, "nf4", logs.append, lambda: released.append(True))
    assert (model, mode, calls, released) == ("fp16-model", "fp16", [True, False], [True])
    assert any("NF4 loading failed" in line and "ImportError" in line for line in logs)
    assert "ImportError" in capsys.readouterr().err  # the traceback goes to the server log


def test_fp16_mode_never_tries_nf4():
    calls = []
    _, mode = load_with_quant_fallback(lambda quantized: calls.append(quantized) or "m", "fp16", lambda m: None)
    assert (mode, calls) == ("fp16", [False])


def test_out_of_memory_is_not_hidden_by_the_fallback():
    def load(quantized):
        raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")

    with pytest.raises(RuntimeError, match="out of memory"):
        load_with_quant_fallback(load, "nf4", lambda m: None)


def test_a_failing_fp16_fallback_raises_its_own_error():
    def load(quantized):
        raise ValueError("nf4 broke" if quantized else "fp16 broke too")

    with pytest.raises(ValueError, match="fp16 broke too"):
        load_with_quant_fallback(load, "nf4", lambda m: None)


def test_server_log_tail_returns_the_last_lines_and_tolerates_a_missing_file(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("\n".join(f"line {i}" for i in range(500)), encoding="utf-8")
    tail = server_log_tail(log, lines=3)
    assert tail.splitlines() == ["line 497", "line 498", "line 499"]
    assert server_log_tail(tmp_path / "absent.log") == ""


def test_server_log_tail_is_bounded_in_bytes(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("x" * 200_000 + "\nlast line\n", encoding="utf-8")
    tail = server_log_tail(log, lines=80, max_bytes=1000)
    assert len(tail) <= 1000 and tail.endswith("last line")
