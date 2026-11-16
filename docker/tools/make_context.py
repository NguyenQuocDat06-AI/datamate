#!/usr/bin/env python3
"""Assemble the Docker build context from the committed source tree.

The image needs three kinds of content:

* inference code (`source_test/bizreason`, `serve/local_server.py`, `run_submission.py`, `container/`), copied from the
  repository, plus the *container-only* observability hooks that the competition's logging contract requires;
* static packaging files kept in `docker/` (entry point, README, model manifest, training-code description);
* data that is deliberately NOT stored in git (training split, few-shot bank, verification evidence), supplied with
  `--extra`.

The script is idempotent: it wipes `<out>/context` and rebuilds it. Nothing in it calls a model or the network.

    python docker/tools/make_context.py --out docker/build \
        --extra /path/to/dir_with_training_code_data_and_assets

`--extra DIR` is merged over `<out>/context/code` (for example `DIR/training_code/data/...`,
`DIR/assets/fewshot_bank.jsonl`, `DIR/verification/...`).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DOCKER_DIR = Path(__file__).resolve().parents[1]
REPO = DOCKER_DIR.parent
SOURCE = REPO / "source_test"
HOOKED_FILES = ("src/bizreason/run_v2.py", "src/serve/local_server.py")


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"expected exactly one match in {path.name}: {old[:70]!r} (found {text.count(old)})")
    path.write_text(text.replace(old, new), encoding="utf-8")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False).stdout


def copy_tree(src: Path, dst: Path, ignore=None) -> None:
    shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True)


def apply_container_hooks(code: Path) -> None:
    """Per-question observability required by the guide (/result/logs). The hooks never change an answer."""
    run_v2 = code / "src/bizreason/run_v2.py"
    replace_once(run_v2, 'if __name__ == "__main__":\n    raise SystemExit(main())',
                 'if __name__ == "__main__":\n    import sys as _sys\n'
                 '    from bizreason.submission_audit import install as _install_submission_audit\n'
                 '    _install_submission_audit(_sys.modules[__name__])\n    raise SystemExit(main())')
    server = code / "src/serve/local_server.py"
    replace_once(server,
                 "    def sync_generate(messages: list[dict[str, Any]], max_tokens: int, temperature: float) -> tuple[str, int, int]:",
                 "    generation_audit = threading.local()\n\n"
                 "    def sync_generate(messages: list[dict[str, Any]], max_tokens: int, temperature: float) -> tuple[str, int, int]:")
    # The worker runs as a standalone script, so sys.path[0] is serve/, not src/: add src/ before importing bizreason.
    replace_once(server,
                 "        text = strip_think(tokenizer.decode(new_tokens, skip_special_tokens=True))\n        return text, in_len, out_len",
                 "        import pathlib as _pathlib, sys as _sys\n"
                 "        _src_root = str(_pathlib.Path(__file__).resolve().parents[1])\n"
                 "        if _src_root not in _sys.path:\n            _sys.path.insert(0, _src_root)\n"
                 "        from bizreason.submission_audit import generated_thinking\n"
                 "        generation_audit.thinking = generated_thinking(tokenizer.decode(new_tokens, skip_special_tokens=False))\n"
                 "        text = strip_think(tokenizer.decode(new_tokens, skip_special_tokens=True))\n        return text, in_len, out_len")
    replace_once(server, "            send_json(self, 200, completion_payload(served_model, text, in_len, out_len))",
                 "            payload = completion_payload(served_model, text, in_len, out_len)\n"
                 "            payload[\"choices\"][0][\"message\"][\"reasoning_content\"] = getattr(generation_audit, \"thinking\", None)\n"
                 "            send_json(self, 200, payload)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DOCKER_DIR / "build", help="output directory (default: docker/build)")
    parser.add_argument("--extra", type=Path, action="append", default=[],
                        help="directory merged over context/code (training data, few-shot bank, verification evidence)")
    args = parser.parse_args()

    status = git("status", "--porcelain", "--", "source_test/bizreason", "source_test/serve/local_server.py",
                 "source_test/run_submission.py", "source_test/container", "docker")
    head = git("rev-parse", "HEAD").strip()
    context = args.out / "context"
    code = context / "code"
    shutil.rmtree(context, ignore_errors=True)
    code.mkdir(parents=True)
    (args.out / "source_state.txt").write_text(
        f"HEAD {head}\nuncommitted paths in packaged areas:\n{status or '(none)'}\n")

    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    # Build files and static packaging content.
    for name in ("Dockerfile", "Dockerfile.build", "requirements.txt"):
        shutil.copy2(DOCKER_DIR / name, context / name)
    for item in (DOCKER_DIR / "image").iterdir():
        shutil.copy2(item, code / item.name)
    copy_tree(DOCKER_DIR / "training_code", code / "training_code", ignore)

    # Inference code from the repository, plus container-only entry points.
    (code / "src/serve").mkdir(parents=True)
    copy_tree(SOURCE / "bizreason", code / "src/bizreason", ignore)
    shutil.copy2(SOURCE / "serve/local_server.py", code / "src/serve/local_server.py")
    shutil.copy2(SOURCE / "run_submission.py", code / "src/run_submission.py")
    shutil.copy2(SOURCE / "pyproject.toml", code / "src/pyproject.toml")
    copy_tree(SOURCE / "container", code / "container", ignore)
    shutil.copy2(DOCKER_DIR / "overlay/submission_audit.py", code / "src/bizreason/submission_audit.py")
    apply_container_hooks(code)

    for extra in args.extra:
        copy_tree(extra, code, ignore)

    print(f"context rebuilt from {head[:10]} at {context} | packaged areas dirty: {bool(status.strip())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
