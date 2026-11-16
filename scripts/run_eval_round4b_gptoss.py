"""Round 4b Evaluation runner for ag/gpt-oss-120b-medium."""

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


def run_batch_1(root: Path, env: dict) -> float:
    out_dir = root / "artifacts" / "live_trammay_eval_50_gptoss_v6"
    if out_dir.exists():
        sub_file = out_dir / "predictions.jsonl"
        if sub_file.exists():
            sub_file.unlink()
        trace_dir = out_dir / "traces"
        if trace_dir.exists():
            shutil.rmtree(trace_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "bizreason.cli",
        "run",
        "--data-root",
        "data/train",
        "--questions",
        "artifacts/live_trammay_eval_50/questions_50.jsonl",
        "--out",
        "artifacts/live_trammay_eval_50_gptoss_v6/predictions.jsonl",
        "--trace-dir",
        "artifacts/live_trammay_eval_50_gptoss_v6/traces",
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

    print("\n" + "=" * 60)
    print("STARTING BATCH 1 (50 Qs) EVALUATION (V6) ON ag/gpt-oss-120b-medium...")
    print("=" * 60)
    start_time = time.perf_counter()
    wall_start = time.time()

    proc = subprocess.run(cmd, env=env)

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

    print(f"\n[OK] Batch 1 finished in {elapsed:.2f}s with code {proc.returncode}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"Batch 1 run failed with return code {proc.returncode}"
        )

    # Score Batch 1
    score_cmd = [
        sys.executable,
        "-m",
        "bizreason.cli",
        "score",
        "--pred",
        "artifacts/live_trammay_eval_50_gptoss_v6/predictions.jsonl",
        "--gold",
        "artifacts/live_trammay_eval_50/gold_50.jsonl",
        "--questions",
        "artifacts/live_trammay_eval_50/questions_50.jsonl",
        "--out",
        "artifacts/live_trammay_eval_50_gptoss_v6/score_report.json",
    ]
    print("Scoring Batch 1...")
    score_proc = subprocess.run(score_cmd, env=env)
    if score_proc.returncode != 0:
        raise RuntimeError(
            f"Batch 1 scoring failed with return code {score_proc.returncode}"
        )
    print("[OK] Batch 1 scored successfully.")
    return elapsed


def run_batch_2(root: Path, env: dict) -> float:
    out_dir = root / "artifacts" / "live_trammay_eval_next50_gptoss_v4"
    if out_dir.exists():
        sub_file = out_dir / "predictions.jsonl"
        if sub_file.exists():
            sub_file.unlink()
        trace_dir = out_dir / "traces"
        if trace_dir.exists():
            shutil.rmtree(trace_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "bizreason.cli",
        "run",
        "--data-root",
        "data/train",
        "--questions",
        "artifacts/live_trammay_eval_next50_gptoss/questions_next50.jsonl",
        "--out",
        "artifacts/live_trammay_eval_next50_gptoss_v4/predictions.jsonl",
        "--trace-dir",
        "artifacts/live_trammay_eval_next50_gptoss_v4/traces",
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

    print("\n" + "=" * 60)
    print(
        "STARTING BATCH 2 (Next 50 Qs) EVALUATION (V4) ON ag/gpt-oss-120b-medium..."
    )
    print("=" * 60)
    start_time = time.perf_counter()
    wall_start = time.time()

    proc = subprocess.run(cmd, env=env)

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

    print(f"\n[OK] Batch 2 finished in {elapsed:.2f}s with code {proc.returncode}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"Batch 2 run failed with return code {proc.returncode}"
        )

    # Score Batch 2
    score_cmd = [
        sys.executable,
        "-m",
        "bizreason.cli",
        "score",
        "--pred",
        "artifacts/live_trammay_eval_next50_gptoss_v4/predictions.jsonl",
        "--gold",
        "artifacts/live_trammay_eval_next50_gptoss/gold_next50.jsonl",
        "--questions",
        "artifacts/live_trammay_eval_next50_gptoss/questions_next50.jsonl",
        "--out",
        "artifacts/live_trammay_eval_next50_gptoss_v4/score_report.json",
    ]
    print("Scoring Batch 2...")
    score_proc = subprocess.run(score_cmd, env=env)
    if score_proc.returncode != 0:
        raise RuntimeError(
            f"Batch 2 scoring failed with return code {score_proc.returncode}"
        )
    print("[OK] Batch 2 scored successfully.")
    return elapsed


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    env = setup_env(root)

    t1 = run_batch_1(root, env)
    t2 = run_batch_2(root, env)
    print(
        f"\nAll 100 questions evaluated and scored! Batch 1: {t1:.2f}s, Batch 2: {t2:.2f}s, Total: {t1+t2:.2f}s"
    )


if __name__ == "__main__":
    main()
