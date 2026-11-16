#!/usr/bin/env python3
"""Write verification/source_selection.json and artifact_manifest.json for context/code (sizes and SHA-256 of every file
except models/ and the manifest itself). Run after make_context.py (and after any --extra evidence is in place):

    python docker/tools/make_manifest.py --out docker/build
"""
import hashlib
import json
from pathlib import Path

import argparse
_parser = argparse.ArgumentParser(description=__doc__)
_parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "build", help="build directory used with make_context.py")
ROOT = _parser.parse_args().out.resolve()
CODE = ROOT / "context/code"
HEAD = next(l.split()[1] for l in (ROOT / "source_state.txt").read_text().splitlines() if l.startswith("HEAD "))

(CODE / "verification").mkdir(parents=True, exist_ok=True)
(CODE / "verification/source_selection.json").write_text(json.dumps({
    "git_head": HEAD, "branch": "arch/rulebook-ir", "parent_of_session_work": "837da1a5513189bbade04b10724c6bb8e651fe0d",
    "dirty_diff_excluded": False, "source": "committed state; submission-only additions listed in artifact_manifest.json",
    "semantic_contracts": "present, off by default", "plan_fallback": "off", "answer_gate": "off", "time_reader": "off",
    "model_revision": "707f0a3b8a3c7ad586ed01e27eafbad8a27dd0f7"}, indent=2) + "\n")


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


files = []
for path in sorted(CODE.rglob("*")):
    rel = path.relative_to(CODE).as_posix()
    if not path.is_file() or rel == "artifact_manifest.json" or rel.startswith("models/") or "__pycache__" in rel:
        continue
    files.append({"path": rel, "bytes": path.stat().st_size, "sha256": sha(path)})

(CODE / "artifact_manifest.json").write_text(json.dumps({
    "source_base": HEAD,
    "submission_only_patches": [
        "per-question observability (bizreason/submission_audit.py, hook at the end of run_v2.py)",
        "generated reasoning returned by the local server as reasoning_content (serve/local_server.py)",
        "container entry: container/predict.py, container/gpu_preflight.py, NF4 to fp16 fallback, server log echo",
        "Python 3.11 compatible f-string in bizreason/temporal.py (also committed in the repository)",
    ],
    "no_public_private_gold": True,
    "no_parameter_training": True,
    "files": files}, indent=2) + "\n")
print("manifest:", len(files), "files, base", HEAD[:10])
