"""Push the batching server to the existing Kaggle kernel. Fills the secret placeholders without printing them.

Usage: gen_push_server.py [--dry-run] [--account 0|1] [--original] [--slug NEW-SLUG] [--src DIR]
--original pushes the unmodified server script (no batching), e.g. to resume a running measurement unchanged.
Account 1 uses KAGGLE_USERNAME1/KAGGLE_KEY1 and pushes the same kernel slug under that user.
The pushed folder is built in a private temp dir (mode 700) and removed afterwards.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ORIG = Path("/home/ubuntu/datamate/source_test/artifacts/kaggle_12h_server")
SRC = Path("/home/ubuntu/datamate_review_data/server_batch")


def main():
    dry = "--dry-run" in sys.argv
    account = int(sys.argv[sys.argv.index("--account") + 1]) if "--account" in sys.argv else 0
    sys.path.insert(0, "/home/ubuntu/datamate_review_data")
    import gen_kaggle_auth
    mapping = json.load(open(ORIG / ".secret_map.json"))
    source = ORIG if "--original" in sys.argv else (Path(sys.argv[sys.argv.index("--src") + 1]) if "--src" in sys.argv else SRC)
    text = (source / "run_server_12h.py").read_text(encoding="utf-8")
    for key, value in mapping.items():
        text = text.replace(key, value)
    left = text.count("__SECRET_")
    if left:
        raise SystemExit(f"unfilled placeholders: {left}")
    tmp = Path(tempfile.mkdtemp(prefix="kpush_"))
    tmp.chmod(0o700)
    try:
        (tmp / "run_server_12h.py").write_text(text, encoding="utf-8")
        meta = json.load(open(SRC / "kernel-metadata.json"))
        slug = sys.argv[sys.argv.index("--slug") + 1] if "--slug" in sys.argv else meta["id"].split("/", 1)[1]
        meta["id"] = gen_kaggle_auth.kernel_id(account, slug)
        if "--slug" in sys.argv:
            meta["title"] = slug.replace("-", " ").title()
        meta["is_private"] = True
        (tmp / "kernel-metadata.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        if dry:
            print("dry run ok: account", account, "original" if source == ORIG else "batching", "placeholders filled, files", sorted(p.name for p in tmp.iterdir()))
            return
        result = gen_kaggle_auth.api(account).kernels_push(str(tmp))
        session = getattr(result, "kernel_session_id", None)
        print("pushed version", getattr(result, "version_number", None), "session", session, "error", getattr(result, "error", None) or "")
        if session:
            log = Path("/home/ubuntu/datamate_review_data/kaggle_sessions.jsonl")
            with log.open("a") as fh:
                fh.write(json.dumps({"account": account, "slug": meta["id"].split("/", 1)[1], "version": getattr(result, "version_number", None), "session": session, "source": str(source), "at": __import__("time").strftime("%Y-%m-%d %H:%M:%S")}) + "\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
