"""Multi-threaded live evaluation runner for Next-100 on ag/gpt-oss-120b-medium."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from dotenv import dotenv_values


def setup_env(root: Path) -> dict:
    env = os.environ.copy()
    env_parent = root.parent / ".env"
    env_source = root / ".env"

    key = None
    if env_parent.exists():
        vals_parent = dotenv_values(env_parent)
        if vals_parent.get("TRAMMAY_KEY"):
            key = vals_parent["TRAMMAY_KEY"]

    if not key and env_source.exists():
        vals_source = dotenv_values(env_source)
        if vals_source.get("TRAMMAY_KEY"):
            key = vals_source["TRAMMAY_KEY"]

    assert bool(key), "TRAMMAY_KEY could not be loaded from environment or .env"
    env["TRAMMAY_KEY"] = key
    env["BIZREASON_API_KEY"] = key
    env["BIZREASON_BASE_URL"] = "https://trammay.xyz/v1"
    return env


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    env = setup_env(root)

    out_dir = root / "artifacts" / "live_trammay_eval_next100_gptoss"
    out_dir.mkdir(parents=True, exist_ok=True)

    pred_file = out_dir / "predictions.jsonl"
    trace_dir = out_dir / "traces"
    score_report = out_dir / "score_report.json"

    if pred_file.exists():
        pred_file.unlink()
    if trace_dir.exists():
        shutil.rmtree(trace_dir)
    if score_report.exists():
        score_report.unlink()

    run_cmd = [
        sys.executable,
        "-m",
        "bizreason.cli",
        "run",
        "--data-root",
        "data/train",
        "--questions",
        "artifacts/live_trammay_eval_next100_gptoss/questions_next100.jsonl",
        "--out",
        "artifacts/live_trammay_eval_next100_gptoss/predictions.jsonl",
        "--trace-dir",
        "artifacts/live_trammay_eval_next100_gptoss/traces",
        "--provider",
        "trammay",
        "--base-url",
        "https://trammay.xyz/v1",
        "--model",
        "ag/gpt-oss-120b-medium",
        "--workers",
        "16",
        "--stream",
        "--group-by-business",
        "--enable-fast-triage",
        "--question-timeout",
        "240",
    ]

    print("=" * 70)
    print("STARTING LIVE EVALUATION ON ag/gpt-oss-120b-medium (NEXT-100)...")
    print("=" * 70)

    start_time = time.perf_counter()
    wall_start = time.time()

    proc = subprocess.run(run_cmd, env=env)

    end_time = time.perf_counter()
    wall_end = time.time()
    elapsed = end_time - start_time

    timing = {
        "start_timestamp": wall_start,
        "end_timestamp": wall_end,
        "elapsed_seconds": elapsed,
        "return_code": proc.returncode,
    }
    with open(out_dir / "execution_time.json", "w", encoding="utf-8") as f:
        json.dump(timing, f, indent=2)

    print(f"\n[OK] Evaluation finished in {elapsed:.2f}s with code {proc.returncode}")
    if proc.returncode != 0:
        raise RuntimeError(f"Run failed with return code {proc.returncode}")

    score_cmd = [
        sys.executable,
        "-m",
        "bizreason.cli",
        "score",
        "--pred",
        "artifacts/live_trammay_eval_next100_gptoss/predictions.jsonl",
        "--gold",
        "artifacts/live_trammay_eval_next100_gptoss/gold_next100.jsonl",
        "--questions",
        "artifacts/live_trammay_eval_next100_gptoss/questions_next100.jsonl",
        "--out",
        "artifacts/live_trammay_eval_next100_gptoss/score_report.json",
    ]
    print("\nScoring predictions...")
    score_proc = subprocess.run(score_cmd, env=env)
    if score_proc.returncode != 0:
        raise RuntimeError(f"Scoring failed with return code {score_proc.returncode}")
    print("[OK] Scoring finished successfully.")


if __name__ == "__main__":
    main()
