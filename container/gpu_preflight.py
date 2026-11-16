#!/usr/bin/env python3
"""GPU/software preflight, run in a child process so the parent never holds a CUDA context.

Prints one line `PREFLIGHT_JSON {...}`: library versions, each GPU's name, compute capability and memory, whether the
installed torch build contains kernels for that architecture, and the result of a real NF4 (bitsandbytes) layer
forward pass on each GPU. The parent decides from it whether to load NF4 or the unquantized fp16 weights."""
import json
import sys
import traceback


def short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc).strip().splitlines()[-1][:300] if str(exc).strip() else ''}"


def main() -> int:
    out: dict = {"python": sys.version.split()[0], "gpus": [], "nf4_ok": {}}
    torch = None
    try:
        import torch  # noqa: PLC0415
        out.update(torch=torch.__version__, torch_cuda=torch.version.cuda, cuda_available=bool(torch.cuda.is_available()))
        out["arch_list"] = list(torch.cuda.get_arch_list())
        for index in range(torch.cuda.device_count()):
            props = torch.cuda.get_device_properties(index)
            free, total = torch.cuda.mem_get_info(index)
            arch = f"sm_{props.major}{props.minor}"
            out["gpus"].append({"index": index, "name": props.name, "capability": f"{props.major}.{props.minor}",
                                "total_gb": round(total / 2**30, 1), "free_gb": round(free / 2**30, 1),
                                "torch_has_arch": arch in out["arch_list"] or f"compute_{props.major}{props.minor}" in out["arch_list"]})
    except BaseException as exc:  # noqa: BLE001
        out["torch_error"] = short(exc)
        traceback.print_exc()
    for name in ("transformers", "accelerate", "safetensors"):
        try:
            out[name] = __import__(name).__version__
        except BaseException as exc:  # noqa: BLE001
            out[name + "_error"] = short(exc)
    bnb = None
    try:
        import bitsandbytes as bnb  # noqa: PLC0415
        out["bitsandbytes"] = bnb.__version__
    except BaseException as exc:  # noqa: BLE001
        out["bitsandbytes_error"] = short(exc)
        traceback.print_exc()
    if torch is not None and bnb is not None:
        for gpu in out["gpus"]:
            index = gpu["index"]
            try:
                layer = bnb.nn.Linear4bit(256, 256, bias=False, compute_dtype=torch.float16, quant_type="nf4")
                layer = layer.to(f"cuda:{index}")
                x = torch.randn(2, 256, dtype=torch.float16, device=f"cuda:{index}")
                y = layer(x)
                torch.cuda.synchronize(index)
                out["nf4_ok"][str(index)] = bool(torch.isfinite(y).all().item())
                if not out["nf4_ok"][str(index)]:
                    out.setdefault("nf4_error", {})[str(index)] = "non-finite output"
            except BaseException as exc:  # noqa: BLE001
                out["nf4_ok"][str(index)] = False
                out.setdefault("nf4_error", {})[str(index)] = short(exc)
                traceback.print_exc()
    print("PREFLIGHT_JSON " + json.dumps(out, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
