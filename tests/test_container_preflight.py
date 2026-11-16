"""Decision logic of the container entry script (no GPU, no torch needed)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("container_predict", Path(__file__).resolve().parents[1] / "container" / "predict.py")
predict = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(predict)


def gpu(index, total=32.0, arch=True):
    return {"index": index, "name": "GPU", "capability": "12.0", "total_gb": total, "free_gb": total - 0.5,
            "torch_has_arch": arch}


def report(**override):
    base = {"torch": "2.9.1", "cuda_available": True, "arch_list": ["sm_120"], "gpus": [gpu(0), gpu(1)],
            "bitsandbytes": "0.50.2", "nf4_ok": {"0": True, "1": True}}
    base.update(override)
    return base


def test_parse_preflight_takes_the_last_marked_line():
    text = "noise\nPREFLIGHT_JSON " + json.dumps({"a": 1}) + "\nmore noise\n"
    assert predict.parse_preflight(text) == {"a": 1}
    assert predict.parse_preflight("nothing printed") is None
    assert predict.parse_preflight("PREFLIGHT_JSON {broken") is None


def test_healthy_report_keeps_nf4_and_raises_nothing():
    quant, problems, notes = predict.decide_quant(report())
    assert (quant, problems, notes) == (None, [], [])


def test_nf4_failure_on_any_gpu_selects_fp16_for_every_worker():
    quant, problems, notes = predict.decide_quant(report(nf4_ok={"0": True, "1": False},
                                                         nf4_error={"1": "RuntimeError: no kernel image"}))
    assert quant == "fp16" and problems == []
    assert any("NF4 is unusable" in n and "no kernel image" in n for n in notes)


def test_missing_bitsandbytes_selects_fp16():
    quant, _, notes = predict.decide_quant(report(bitsandbytes_error="ImportError: libbitsandbytes", nf4_ok={}))
    assert quant == "fp16" and any("ImportError" in n for n in notes)


def test_small_gpus_are_warned_about_when_fp16_is_chosen():
    quant, _, notes = predict.decide_quant(report(nf4_ok={"0": False, "1": False}, gpus=[gpu(0, 16.0), gpu(1, 16.0)]))
    assert quant == "fp16" and any("WARNING" in n for n in notes)


def test_unusable_environments_are_problems_not_choices():
    assert predict.decide_quant(None)[1]
    assert "torch failed" in predict.decide_quant({"torch_error": "ImportError: libcudart"})[1][0]
    assert "no CUDA" in predict.decide_quant({"cuda_available": False})[1][0]
    assert "found 1" in predict.decide_quant(report(gpus=[gpu(0)]))[1][0]


def test_an_architecture_missing_from_torch_is_reported_as_a_note():
    quant, problems, notes = predict.decide_quant(report(gpus=[gpu(0, arch=False), gpu(1)]))
    assert quant is None and problems == [] and any("lists no kernels" in n for n in notes)
