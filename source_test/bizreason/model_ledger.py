from __future__ import annotations

import json
import math
import struct
from datetime import date
from pathlib import Path
from typing import Any

STANDARD_REQUIRED_FIELDS = [
    "model_id",
    "weights_revision",
    "weights_sha256",
    "release_date",
    "licence",
    "total_parameters_including_moe",
    "quantization",
    "runtime",
    "purpose",
]


def _read_safetensors_header(file_path: Path) -> dict[str, Any]:
    with file_path.open("rb") as f:
        header_len_bytes = f.read(8)
        if len(header_len_bytes) < 8:
            raise ValueError(f"File {file_path} is too small to be a valid safetensors file")
        (header_len,) = struct.unpack("<Q", header_len_bytes)
        header_json = f.read(header_len)
        if len(header_json) < header_len:
            raise ValueError(f"Truncated safetensors header in {file_path}")
        return json.loads(header_json.decode("utf-8"))


def inspect_safetensors_parameters(path: Path) -> dict[str, Any]:
    """Inspect safetensors headers and compute stored tensor elements and logical parameter count."""
    path = Path(path).resolve()
    if path.is_file():
        shard_files = [path]
        dir_path = path.parent
    elif path.is_dir():
        dir_path = path
        index_file = path / "model.safetensors.index.json"
        if index_file.exists():
            index_data = json.loads(index_file.read_text(encoding="utf-8"))
            weight_map = index_data.get("weight_map", {})
            unique_shards = sorted(set(weight_map.values()))
            shard_files = [path / fn for fn in unique_shards]
        else:
            shard_files = sorted(path.glob("*.safetensors"))
            if not shard_files:
                raise FileNotFoundError(f"No .safetensors files found in {path}")
    else:
        raise FileNotFoundError(f"Path does not exist: {path}")

    # Inspect optional quantize_config.json or config.json
    w_bit = 4
    for cfg_name in ("quantize_config.json", "config.json"):
        cfg_file = dir_path / cfg_name
        if cfg_file.is_file():
            try:
                cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
                bits = cfg.get("bits") or cfg.get("w_bit")
                if not bits and isinstance(cfg.get("quantization_config"), dict):
                    bits = cfg["quantization_config"].get("bits") or cfg["quantization_config"].get("w_bit")
                if isinstance(bits, int) and bits in {2, 3, 4, 8, 16}:
                    w_bit = bits
                    break
            except Exception:
                pass

    all_tensors: dict[str, dict[str, Any]] = {}
    stored_tensor_elements = 0

    for shard in shard_files:
        header = _read_safetensors_header(shard)
        for key, info in header.items():
            if key == "__metadata__":
                continue
            shape = info.get("shape", [])
            stored_tensor_elements += math.prod(shape) if shape else 1
            all_tensors[key] = info

    # Find all packed weight prefixes: any key ending with .qweight or .weight_packed
    quantized_prefixes: dict[str, str] = {}
    for key in all_tensors:
        if key.endswith(".qweight") or key.endswith(".weight_packed"):
            prefix = key.rsplit(".", 1)[0]
            quantized_prefixes[prefix] = key

    metadata_keys: set[str] = set()
    for prefix in quantized_prefixes:
        for suffix in ("qzeros", "scales", "g_idx", "weight_scale", "weight_zero_point"):
            metadata_keys.add(f"{prefix}.{suffix}")

    logical_parameters = 0

    # 1. Quantized packed weights
    for prefix, qkey in quantized_prefixes.items():
        info = all_tensors[qkey]
        shape = info.get("shape", [])
        dtype = str(info.get("dtype", "")).upper()

        pack_factor: int | None = None
        scales_info = all_tensors.get(f"{prefix}.scales")
        if scales_info:
            s_shape = scales_info.get("shape", [])
            if len(s_shape) == 2 and len(shape) == 2:
                s0, s1 = s_shape
                d0, d1 = shape
                if d1 > 0 and s1 > d1 and s1 % d1 == 0 and (s1 // d1) in {2, 4, 8, 16}:
                    pack_factor = s1 // d1
                elif d0 > 0 and s0 > d0 and s0 % d0 == 0 and (s0 // d0) in {2, 4, 8, 16}:
                    pack_factor = s0 // d0

        if pack_factor is None:
            if dtype in {"I32", "U32", "INT32", "UINT32"}:
                pack_factor = max(1, 32 // w_bit)
            elif dtype in {"I16", "U16", "INT16", "UINT16"}:
                pack_factor = max(1, 16 // w_bit)
            elif dtype in {"I8", "U8", "INT8", "UINT8"}:
                pack_factor = max(1, 8 // w_bit)
            else:
                pack_factor = 1

        logical_parameters += (math.prod(shape) if shape else 1) * pack_factor

    # 2. All other tensors (not packed weights and not quantization metadata)
    qkeys = set(quantized_prefixes.values())
    for key, info in all_tensors.items():
        if key in qkeys or key in metadata_keys:
            continue
        shape = info.get("shape", [])
        logical_parameters += math.prod(shape) if shape else 1

    return {
        "stored_tensor_elements": stored_tensor_elements,
        "logical_parameters": logical_parameters,
    }


def count_safetensors_parameters(path: Path) -> int:
    """Pure-Python stdlib parameter counter for single or sharded safetensors files."""
    return int(inspect_safetensors_parameters(path)["logical_parameters"])


def verify_model_ledger(
    ledger: dict[str, Any],
    *,
    max_total_parameters: int = 15_000_000_000,
    cutoff_date: date = date(2026, 10, 1),
) -> list[str]:
    """Verify offline model parameter count and release date compliance per competition rules."""
    violations: list[str] = []

    offline_models = ledger.get("offline_models")
    if offline_models is None or not isinstance(offline_models, list):
        return ["MISSING_OR_INVALID_OFFLINE_MODELS"]
    if len(offline_models) == 0:
        return ["NO_OFFLINE_MODELS"]

    required_fields = ledger.get("required_offline_model_fields", STANDARD_REQUIRED_FIELDS)

    valid_models: list[dict[str, Any]] = []

    for idx, item in enumerate(offline_models):
        if not isinstance(item, dict):
            violations.append(f"INVALID_MODEL_ENTRY:{idx}")
            continue

        model_id = item.get("model_id", f"model_{idx}")

        # Check required fields
        for field in required_fields:
            if field not in item or item[field] is None:
                violations.append(f"MISSING_REQUIRED_FIELD:{model_id}:{field}")

        # Parameter count check: exact integer > 0
        raw_params = item.get("total_parameters_including_moe")
        if (
            type(raw_params) is not int
            or isinstance(raw_params, bool)
            or raw_params <= 0
        ):
            violations.append(f"INVALID_PARAMETER_COUNT:{model_id}:{raw_params}")
            continue

        # Release date & origin checks
        raw_date = item.get("release_date")
        rel_date: date | None = None
        if isinstance(raw_date, str):
            try:
                rel_date = date.fromisoformat(raw_date)
            except ValueError:
                violations.append(f"INVALID_RELEASE_DATE_FORMAT:{model_id}:{raw_date}")
        elif isinstance(raw_date, date):
            rel_date = raw_date
        else:
            violations.append(f"MISSING_OR_INVALID_RELEASE_DATE:{model_id}")

        origin = item.get("origin") or item.get("artifact_type") or "pretrained"
        if origin in {"pretrained", "third_party"}:
            if rel_date and rel_date >= cutoff_date:
                violations.append(
                    f"THIRD_PARTY_RELEASE_AFTER_CUTOFF:{model_id}:{rel_date}>={cutoff_date}"
                )
        elif origin == "self_finetuned":
            if rel_date and rel_date >= cutoff_date:
                base_date_raw = item.get("base_model_release_date")
                base_date: date | None = None
                if isinstance(base_date_raw, str):
                    try:
                        base_date = date.fromisoformat(base_date_raw)
                    except ValueError:
                        pass
                elif isinstance(base_date_raw, date):
                    base_date = base_date_raw

                if base_date is None or base_date >= cutoff_date:
                    violations.append(
                        f"BASE_MODEL_RELEASE_AFTER_CUTOFF:{model_id}:{base_date}>={cutoff_date}"
                    )
                if item.get("training_data_compliant") is not True:
                    violations.append(f"TRAINING_DATA_NON_COMPLIANT:{model_id}")
        else:
            if rel_date and rel_date >= cutoff_date:
                violations.append(
                    f"UNKNOWN_ORIGIN_RELEASE_AFTER_CUTOFF:{model_id}:{rel_date}>={cutoff_date}"
                )

        # Base / adapter decomposition check
        if any(k in item for k in ("shared_base_sha256", "base_parameters", "adapter_parameters")):
            shared_base = item.get("shared_base_sha256")
            base_params = item.get("base_parameters")
            adapter_params = item.get("adapter_parameters")
            if not isinstance(shared_base, str) or not shared_base.strip():
                violations.append(f"INVALID_SHARED_BASE_SHA:{model_id}")
            if type(base_params) is not int or isinstance(base_params, bool) or base_params <= 0:
                violations.append(f"INVALID_BASE_PARAMETERS:{model_id}:{base_params}")
            if type(adapter_params) is not int or isinstance(adapter_params, bool) or adapter_params < 0:
                violations.append(f"INVALID_ADAPTER_PARAMETERS:{model_id}:{adapter_params}")
            if (
                type(base_params) is int
                and not isinstance(base_params, bool)
                and type(adapter_params) is int
                and not isinstance(adapter_params, bool)
                and base_params + adapter_params != raw_params
            ):
                violations.append(
                    f"INCONSISTENT_PARAMETER_DECOMPOSITION:{model_id}:{base_params}+{adapter_params}!={raw_params}"
                )

        valid_models.append(item)

    base_param_by_sha: dict[str, int] = {}

    # First, for every standalone model (no shared_base_sha256) that has valid raw_params and weights_sha256:
    for item in valid_models:
        model_id = item.get("model_id")
        raw_params = item.get("total_parameters_including_moe")
        shared_base = item.get("shared_base_sha256")
        weights_sha = item.get("weights_sha256")
        if not shared_base and weights_sha and isinstance(weights_sha, str) and weights_sha.strip():
            if weights_sha in base_param_by_sha and base_param_by_sha[weights_sha] != raw_params:
                violations.append(f"BASE_PARAMETER_MISMATCH:{model_id}:{weights_sha}")
                base_param_by_sha[f"{weights_sha}:{model_id}"] = raw_params
            else:
                base_param_by_sha[weights_sha] = raw_params

    # Next, for every adapter entry (with shared_base_sha256):
    for item in valid_models:
        model_id = item.get("model_id")
        raw_params = item.get("total_parameters_including_moe")
        shared_base = item.get("shared_base_sha256")
        base_params = item.get("base_parameters")
        if shared_base and isinstance(shared_base, str) and shared_base.strip():
            if type(base_params) is int and not isinstance(base_params, bool) and base_params > 0:
                if shared_base in base_param_by_sha and base_param_by_sha[shared_base] != base_params:
                    violations.append(f"BASE_PARAMETER_MISMATCH:{model_id}:{shared_base}")
                base_param_by_sha[shared_base] = base_params
            elif shared_base not in base_param_by_sha:
                base_param_by_sha[shared_base] = raw_params

    # Then compute total_parameters
    total_parameters = sum(base_param_by_sha.values())
    for item in valid_models:
        raw_params = item.get("total_parameters_including_moe")
        shared_base = item.get("shared_base_sha256")
        base_params = item.get("base_parameters")
        adapter_params = item.get("adapter_parameters")
        weights_sha = item.get("weights_sha256")
        if shared_base and isinstance(shared_base, str) and shared_base.strip():
            if (
                type(adapter_params) is int
                and not isinstance(adapter_params, bool)
                and adapter_params >= 0
                and type(base_params) is int
                and not isinstance(base_params, bool)
                and base_params + adapter_params == raw_params
            ):
                total_parameters += adapter_params
            else:
                total_parameters += max(0, raw_params - base_param_by_sha.get(shared_base, 0))
        else:
            if not weights_sha or not isinstance(weights_sha, str) or not weights_sha.strip():
                total_parameters += raw_params

    if total_parameters > max_total_parameters:
        violations.append(
            f"EXCEEDS_MAX_PARAMETERS:{total_parameters}>{max_total_parameters}"
        )

    declared_total = ledger.get("offline_total_parameters")
    if declared_total is not None and declared_total != total_parameters:
        violations.append(
            f"DECLARED_TOTAL_MISMATCH:{declared_total}!={total_parameters}"
        )

    return violations
