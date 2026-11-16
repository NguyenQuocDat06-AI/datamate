"""Only public pinned metadata may enable rulebook cache reuse across CLI invocations."""
import json
from types import SimpleNamespace

import pytest

from bizreason.run_v2 import _rulebook_model_identity


def test_unknown_runtime_identity_is_different_on_each_run():
    client = SimpleNamespace(settings=SimpleNamespace(model="test-model"))
    a = _rulebook_model_identity(client, None)
    b = _rulebook_model_identity(client, None)
    assert a["model"] == b["model"] == "test-model"
    assert a["unverified_run"] != b["unverified_run"]


def test_identity_selects_generation_fields_and_ignores_instance_expiry(tmp_path):
    path = tmp_path / "public_metadata.json"
    fields = {"model": "test-model", "revision": "revision-a", "weights_sha256": "synthetic-sha",
              "quantization": "NF4", "compute_dtype": "float16", "weights_verified": True,
              "instance_id": "instance-a", "expires_at": 100, "seed": 42}
    client = SimpleNamespace(settings=SimpleNamespace(model="test-model"))
    path.write_text(json.dumps(fields))
    a = _rulebook_model_identity(client, path)
    fields.update(instance_id="instance-b", expires_at=200)
    path.write_text(json.dumps(fields))
    assert _rulebook_model_identity(client, path) == a
    assert "instance_id" not in a and "expires_at" not in a and a["revision"] == "revision-a"


@pytest.mark.parametrize("changes", [{"model": "other-model"}, {"revision": ""}, {"weights_verified": False}])
def test_unverified_or_mismatched_metadata_is_rejected(tmp_path, changes):
    path = tmp_path / "public_metadata.json"
    fields = {"model": "test-model", "revision": "revision-a", "weights_sha256": "synthetic-sha",
              "quantization": "NF4", "compute_dtype": "float16", "weights_verified": True, **changes}
    path.write_text(json.dumps(fields))
    client = SimpleNamespace(settings=SimpleNamespace(model="test-model"))
    with pytest.raises(ValueError, match="identity metadata"):
        _rulebook_model_identity(client, path)
