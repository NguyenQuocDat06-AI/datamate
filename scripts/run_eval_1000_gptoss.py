"""Runner for 800-question live evaluation on ag/gpt-oss-120b-medium in 2 waves.

Wave 1: questions_800_wave1.jsonl (400 questions)
Wave 2: questions_800_wave2.jsonl (400 questions)
"""

import argparse
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


def run_wave(wave_num: int) -> None:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    env = setup_env(root)

    out_dir = root / "artifacts" / "live_trammay_eval_1000_gptoss"
    out_dir.mkdir(parents=True, exist_ok=True)

    q_file = out_dir / f"questions_800_wave{wave_num}.jsonl"
    gold_file = out_dir / f"gold_800_wave{wave_num}.jsonl"
    pred_file = out_dir / f"predictions_wave{wave_num}.jsonl"
    trace_dir = out_dir / f"traces_wave{wave_num}"
    score_report = out_dir / f"score_report_wave{wave_num}.json"
    timing_file = out_dir / f"execution_time_wave{wave_num}.json"

    assert q_file.exists(), f"Missing question file: {q_file}"

    # Clean existing wave outputs if any
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
        f"artifacts/live_trammay_eval_1000_gptoss/questions_800_wave{wave_num}.jsonl",
        "--out",
        f"artifacts/live_trammay_eval_1000_gptoss/predictions_wave{wave_num}.jsonl",
        "--trace-dir",
        f"artifacts/live_trammay_eval_1000_gptoss/traces_wave{wave_num}",
        "--provider",
        "trammay",
        "--base-url",
        "https://trammay.xyz/v1",
        "--model",
        "ag/gpt-oss-120b-medium",
        "--workers",
        "24",
        "--stream",
        "--group-by-business",
        "--enable-fast-triage",
        "--question-timeout",
        "240",
    ]

    print("=" * 70)
    print(f"STARTING LIVE EVALUATION WAVE {wave_num} (400 Qs) ON ag/gpt-oss-120b-medium...")
    print("=" * 70)

    start_time = time.perf_counter()
    wall_start = time.time()

    proc = subprocess.run(run_cmd, env=env)

    end_time = time.perf_counter()
    wall_end = time.time()
    elapsed = end_time - start_time

    timing = {
        "wave": wave_num,
        "start_timestamp": wall_start,
        "end_timestamp": wall_end,
        "elapsed_seconds": elapsed,
        "return_code": proc.returncode,
    }
    with open(timing_file, "w", encoding="utf-8") as f:
        json.dump(timing, f, indent=2)

    print(f"\n[OK] Wave {wave_num} finished in {elapsed:.2f}s with code {proc.returncode}")
    if proc.returncode != 0:
        raise RuntimeError(f"Wave {wave_num} run failed with return code {proc.returncode}")

    # Official scoring for this wave
    if gold_file.exists():
        print(f"Scoring Wave {wave_num} predictions...")
        score_cmd = [
            sys.executable,
            "-m",
            "bizreason.cli",
            "score",
            "--pred",
            f"artifacts/live_trammay_eval_1000_gptoss/predictions_wave{wave_num}.jsonl",
            "--gold",
            f"artifacts/live_trammay_eval_1000_gptoss/gold_800_wave{wave_num}.jsonl",
            "--questions",
            f"artifacts/live_trammay_eval_1000_gptoss/questions_800_wave{wave_num}.jsonl",
            "--out",
            f"artifacts/live_trammay_eval_1000_gptoss/score_report_wave{wave_num}.json",
        ]
        score_proc = subprocess.run(score_cmd, env=env)
        print(f"[OK] Wave {wave_num} scoring return code: {score_proc.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wave", type=int, required=True, choices=[1, 2], help="Wave number (1 or 2)")
    args = parser.parse_args()
    run_wave(args.wave)


if __name__ == "__main__":
    main()

