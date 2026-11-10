import json
import os
import sys
import time
import subprocess
from pathlib import Path
from dotenv import dotenv_values

def main():
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    # Load TRAMMAY_KEY from .env without printing or logging it
    env = os.environ.copy()
    env_source = root / ".env"
    env_parent = root.parent / ".env"
    
    if env_parent.exists():
        vals_parent = dotenv_values(env_parent)
        if vals_parent.get("TRAMMAY_KEY"):
            env["TRAMMAY_KEY"] = vals_parent["TRAMMAY_KEY"]
            
    if env_source.exists() and "TRAMMAY_KEY" not in env:
        vals_source = dotenv_values(env_source)
        if vals_source.get("TRAMMAY_KEY"):
            env["TRAMMAY_KEY"] = vals_source["TRAMMAY_KEY"]

    assert bool(env.get("TRAMMAY_KEY")), "TRAMMAY_KEY is missing from environment and .env!"

    cmd = [
        sys.executable,
        "-m", "bizreason.cli", "run",
        "--data-root", "../data steps challenge/data",
        "--questions", "artifacts/live_trammay_eval_500/questions_50_b5.jsonl",
        "--out", "artifacts/live_trammay_eval_500/submission.jsonl",
        "--artifact-dir", "artifacts/live_trammay_eval_500/run_artifacts",
        "--provider", "trammay",
        "--base-url", "https://trammay.xyz/v1",
        "--model", "gemini3.8-flash-high",
        "--workers", "16",
        "--stream",
        "--group-by-business",
        "--enable-fast-triage",
        "--question-timeout", "240",
    ]

    print("Starting VER-LIVE-500 evaluation run...")
    print(f"Command: {' '.join(cmd)}")
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

    out_time_file = root / "artifacts" / "live_trammay_eval_500" / "execution_time.json"
    with open(out_time_file, "w", encoding="utf-8") as f:
        json.dump(timing, f, indent=2)

    print(f"\nCompleted VER-LIVE-500 in {elapsed:.2f}s with return code {proc.returncode}")
    sys.exit(proc.returncode)

if __name__ == "__main__":
    main()

