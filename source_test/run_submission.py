#!/usr/bin/env python3
"""One command for the exam machine: start the local model server, run the V2 pipeline, write the submission.

    python run_submission.py --data <test_root> --model-dir <checkpoint_dir> --out <dir> --submission <file> \\
        [--fewshot-bank <file>] [--total-seconds 21000] [--workers 12]

Steps:
  1. start serve/local_server.py (one worker per GPU) with a random bearer token passed in the environment,
     and wait until /v1/models answers;
  2. write <out>/connection.json (mode 600) with the served model id and the token;
  3. run `python -m bizreason.run_v2` against http://127.0.0.1:<port>/v1, with the time budget left after the
     server start and a 300 s safety margin;
  4. copy <out>/submission.final.jsonl to --submission. If the pipeline did not finish, the final file is rebuilt
     from the checkpoint (<out>/submission.jsonl) with abstains for the questions that were not answered.

The server and the pipeline are stopped in every case (including Ctrl-C and SIGTERM). The exit status is non-zero
only when no submission file could be written.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

REPO_ROOT = Path(__file__).resolve().parent
SERVER_SCRIPT = REPO_ROOT / "serve" / "local_server.py"
TOKEN_ENV = "DATAMATE_SERVER_TOKEN"
SAFETY_MARGIN_SECONDS = 300.0  # subtracted from the total budget before the pipeline starts
EXIT_MARGIN_SECONDS = 120.0  # the pipeline process is killed this long before the total budget ends
SERVER_STOP_GRACE_SECONDS = 60.0
PIPELINE_STOP_GRACE_SECONDS = 60.0
OUTAGE_WAIT_SECONDS = 60  # run_v2 waits this long for an endpoint that stops answering; a local server does not recover
ABSTAIN_REASON = "not answered"
# Loopback only: never route these calls through an HTTP proxy from the environment.
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
LOOPBACK_NO_PROXY = "127.0.0.1,localhost"


def log(msg: str) -> None:
    print(f"[run_submission {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class ServerError(RuntimeError):
    """The model server did not start, or stopped answering."""


class _Terminated(BaseException):
    """SIGTERM from the harness. Unwinds like Ctrl-C, so the server and the pipeline are stopped in finally blocks."""


def _on_sigterm(signum: int, frame: Any) -> None:
    raise _Terminated(f"signal {signum}")


@dataclass
class ModelServer:
    proc: subprocess.Popen
    port: int
    token: str
    log_path: Path
    log_file: IO[str]
    model_name: str | None = None


# ---------------------------------------------------------------------------------------------------------------
# Process handling. Each child runs in its own session so that its whole process group can be stopped.
# ---------------------------------------------------------------------------------------------------------------

def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


def _stop_process_group(proc: subprocess.Popen, grace: float) -> None:
    """SIGTERM to the group, wait up to grace seconds, then SIGKILL. Safe to call on a finished process."""
    _signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        log("process did not stop in time; killing it")
        _signal_group(proc, signal.SIGKILL)
        proc.wait(timeout=30)


def _add_loopback_no_proxy(env: dict[str, str]) -> None:
    for key in ("NO_PROXY", "no_proxy"):
        current = env.get(key, "")
        env[key] = f"{current},{LOOPBACK_NO_PROXY}" if current else LOOPBACK_NO_PROXY


def probe_models(port: int, token: str, timeout: float = 5.0) -> str | None:
    """The served model id when /v1/models answers 200 with this token, else None."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models", headers={"Authorization": f"Bearer {token}"})
    try:
        with _LOCAL_OPENER.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8")).get("data") or []
            return str(data[0]["id"]) if data else None
    except Exception:
        return None


def start_model_server(*, model_dir: Path, port: int, token: str, out_dir: Path, gpus: str, batch_max: int,
                       server_script: Path) -> ModelServer:
    """Start serve/local_server.py. The token goes in the environment, not on the command line."""
    log_path = out_dir / "server.log"
    log_file = log_path.open("a", encoding="utf-8")
    env = dict(os.environ)
    env[TOKEN_ENV] = token
    _add_loopback_no_proxy(env)
    argv = [sys.executable, str(server_script), "--model-dir", str(model_dir), "--port", str(port),
            "--gpus", gpus, "--batch-max", str(batch_max)]
    try:
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=log_file,
                                stderr=subprocess.STDOUT, start_new_session=True, cwd=str(REPO_ROOT))
    except Exception:
        log_file.close()
        raise
    return ModelServer(proc=proc, port=port, token=token, log_path=log_path, log_file=log_file)


def wait_until_ready(server: ModelServer, *, timeout: float, poll: float = 1.0) -> str:
    """Poll /v1/models until it answers. Returns the served model id. Raises ServerError on exit or timeout."""
    start = time.monotonic()
    deadline = start + timeout
    next_report = start + 60
    while True:
        rc = server.proc.poll()
        if rc is not None:
            raise ServerError(f"model server exited with code {rc}; see {server.log_path}")
        name = probe_models(server.port, server.token)
        if name is not None:
            server.model_name = name
            return name
        now = time.monotonic()
        if now >= deadline:
            raise ServerError(f"model server did not answer /v1/models within {timeout:.0f}s; see {server.log_path}")
        if now >= next_report:
            log(f"waiting for the model server ({now - start:.0f}s elapsed)")
            next_report += 60
        time.sleep(min(poll, max(0.0, deadline - now)))


def stop_model_server(server: ModelServer | None, grace: float = SERVER_STOP_GRACE_SECONDS) -> None:
    if server is None:
        return
    try:
        _stop_process_group(server.proc, grace)
        log(f"model server stopped (exit code {server.proc.returncode})")
    finally:
        server.log_file.close()


def run_pipeline(argv: list[str], *, hard_timeout: float) -> int:
    """Run `python -m bizreason.run_v2 <argv>`. Returns its exit code, or 124 if it was stopped at hard_timeout."""
    env = dict(os.environ)
    _add_loopback_no_proxy(env)
    proc = subprocess.Popen([sys.executable, "-m", "bizreason.run_v2", *argv], cwd=str(REPO_ROOT),
                            env=env, start_new_session=True)
    try:
        return proc.wait(timeout=max(1.0, hard_timeout))
    except subprocess.TimeoutExpired:
        log("pipeline reached its hard time limit; stopping it")
        _stop_process_group(proc, PIPELINE_STOP_GRACE_SECONDS)
        return 124
    finally:
        if proc.poll() is None:
            _stop_process_group(proc, PIPELINE_STOP_GRACE_SECONDS)


# ---------------------------------------------------------------------------------------------------------------
# Files and budget.
# ---------------------------------------------------------------------------------------------------------------

def pipeline_budget_seconds(total_seconds: float, server_seconds: float, margin: float = SAFETY_MARGIN_SECONDS) -> float:
    """Seconds handed to run_v2 as --total-seconds: the total budget minus the server start and the safety margin."""
    return float(total_seconds) - float(server_seconds) - float(margin)


def write_connection_file(path: Path, model_name: str, token: str) -> None:
    """The file run_v2 reads for --connection. Written with mode 600 from the start."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump({"model": model_name, "bearer_token": token}, handle)
    os.replace(tmp, path)


def build_run_v2_argv(*, data: Path, out: Path, connection: Path, base_url: str, budget_seconds: float,
                      workers: int, fewshot_bank: Path | None, reasoning: str = "off",
                      traces: bool = False) -> list[str]:
    argv = [
        "--data", str(data),
        "--out", str(out),
        "--connection", str(connection),
        "--base-url", base_url,
        "--aliases", "off",
        "--workers", str(workers),
        "--max-model-concurrency", str(workers),
        "--total-seconds", f"{budget_seconds:.0f}",
        "--outage-wait", str(OUTAGE_WAIT_SECONDS),
    ]
    if fewshot_bank is not None:
        argv += ["--fewshot-bank", str(fewshot_bank)]
    if reasoning == "on":
        argv += ["--reasoning", "on"]
    if traces:
        argv += ["--traces"]
    return argv


def rebuild_final(out: Path, data: Path) -> tuple[int, int]:
    """Write <out>/submission.final.jsonl from the checkpoint, the same way run_v2 does at the end of a run.
    Returns (questions, answered)."""
    from bizreason.run_v2 import Checkpoint, load_questions, write_final
    from bizreason.utils import PathPolicy

    # The same write policy run_v2.main applies: outputs may go under --out, nothing may be written into --data.
    with PathPolicy(mode="competition", allowed_roots=[out], forbidden_roots=[data]):
        questions = load_questions(data / "questions.jsonl", None, None)
        if not questions:
            raise ValueError("questions.jsonl holds no questions")
        checkpoint = Checkpoint(out / "submission.jsonl")
        try:
            lines = dict(checkpoint.lines)
        finally:
            checkpoint.close()
        write_final(out / "submission.final.jsonl", questions, lines)
    return len(questions), sum(1 for q in questions if q.id in lines)


def copy_submission(final: Path, submission: Path) -> None:
    submission.parent.mkdir(parents=True, exist_ok=True)
    tmp = submission.with_name(submission.name + ".tmp")
    tmp.write_bytes(final.read_bytes())
    os.replace(tmp, submission)


def answer_types(path: Path) -> Counter:
    counts: Counter = Counter()
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            counts[json.loads(raw).get("type")] += 1
    return counts


# ---------------------------------------------------------------------------------------------------------------
# The run.
# ---------------------------------------------------------------------------------------------------------------

def run_model_and_pipeline(args: argparse.Namespace, data: Path, out: Path, t_start: float,
                           timings: dict[str, float]) -> bool:
    """Start the server, run the pipeline. Returns True when the pipeline exited 0. The server is always stopped."""
    token = secrets.token_urlsafe(32)
    connection = out / "connection.json"
    server: ModelServer | None = None
    try:
        t0 = time.monotonic()
        log(f"starting the model server: {args.model_dir} on 127.0.0.1:{args.port}, GPUs {args.gpus}")
        server = start_model_server(model_dir=args.model_dir, port=args.port, token=token, out_dir=out,
                                    gpus=args.gpus, batch_max=args.batch_max, server_script=SERVER_SCRIPT)
        try:
            model_name = wait_until_ready(server, timeout=args.server_timeout)
        finally:
            timings["server_start_seconds"] = round(time.monotonic() - t0, 1)
        log(f"model server ready after {timings['server_start_seconds']:.1f}s (model {model_name})")

        budget = pipeline_budget_seconds(args.total_seconds, timings["server_start_seconds"])
        if budget <= 0:
            log(f"no time left for the pipeline (budget {budget:.0f}s); skipping it")
            return False
        write_connection_file(connection, model_name, token)
        argv = build_run_v2_argv(
            data=data, out=out, connection=connection, base_url=f"http://127.0.0.1:{args.port}/v1",
            budget_seconds=budget, workers=args.workers, fewshot_bank=args.fewshot_bank,
            reasoning=args.reasoning, traces=args.traces,
        )
        hard_timeout = (t_start + args.total_seconds - EXIT_MARGIN_SECONDS) - time.monotonic()
        log(f"running the pipeline: budget {budget:.0f}s, {args.workers} question threads")
        t1 = time.monotonic()
        rc = run_pipeline(argv, hard_timeout=hard_timeout)
        timings["pipeline_seconds"] = round(time.monotonic() - t1, 1)
        log(f"pipeline exit code {rc} after {timings['pipeline_seconds']:.1f}s")
        return rc == 0
    finally:
        stop_model_server(server)
        connection.unlink(missing_ok=True)


def finish_submission(out: Path, data: Path, submission: Path, pipeline_completed: bool) -> bool:
    final = out / "submission.final.jsonl"
    if pipeline_completed and final.exists():
        source = "run_v2"
    else:
        try:
            total, answered = rebuild_final(out, data)
        except Exception as exc:
            log(f"could not rebuild the submission from the checkpoint: {type(exc).__name__}: {exc}")
            return False
        source = f"checkpoint, {answered} of {total} answered"
    copy_submission(final, submission)
    counts = answer_types(submission)
    log(f"SUBMISSION WRITTEN: {submission} ({sum(counts.values())} lines; source: {source}; "
        f"types: {dict(counts)})")
    if not pipeline_completed:
        log("WARNING: the pipeline did not finish cleanly; questions it did not answer are abstains. "
            "Check server.log and the pipeline output above.")
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Start the local model server, run the V2 pipeline, write the submission.")
    p.add_argument("--data", required=True, type=Path, help="test root with questions.jsonl, rulebook.md, data/<biz>")
    p.add_argument("--model-dir", required=True, type=Path, help="local checkpoint directory (no download)")
    p.add_argument("--out", required=True, type=Path, help="working directory for caches, checkpoints and logs")
    p.add_argument("--submission", required=True, type=Path, help="where the submission file is written")
    p.add_argument("--traces", action="store_true", help="pass --traces to run_v2 (per-question intents in traces.jsonl)")
    p.add_argument("--reasoning", choices=("off", "on"), default="off",
                   help="pass --reasoning on to run_v2 (short analysis before each intent)")
    p.add_argument("--fewshot-bank", type=Path, default=None, help="few-shot bank jsonl (optional)")
    p.add_argument("--total-seconds", type=float, default=21000.0,
                   help="wall budget for the whole run, server start included (default 21000)")
    p.add_argument("--workers", type=int, default=12, help="parallel question threads (default 12)")
    p.add_argument("--port", type=int, default=8000, help="model server port on 127.0.0.1 (default 8000)")
    p.add_argument("--gpus", default="0,1", help="GPU indices, one server worker each (default 0,1)")
    p.add_argument("--batch-max", type=int, default=6, help="requests micro-batched per GPU (default 6)")
    p.add_argument("--server-timeout", type=float, default=1800.0,
                   help="seconds to wait for the model server to answer /v1/models (default 1800)")
    args = p.parse_args(argv)
    if args.workers < 1:
        p.error("--workers must be at least 1")
    if args.total_seconds <= SAFETY_MARGIN_SECONDS + EXIT_MARGIN_SECONDS:
        p.error("--total-seconds is too small to leave any time for the pipeline")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data = args.data.resolve()
    out = args.out.resolve()
    submission = args.submission.resolve()
    args.model_dir = args.model_dir.resolve()
    if args.fewshot_bank is not None:
        args.fewshot_bank = args.fewshot_bank.resolve()

    problems = []
    if not (data / "questions.jsonl").is_file():
        problems.append(f"no questions.jsonl under {data}")
    if not args.model_dir.is_dir():
        problems.append(f"model directory not found: {args.model_dir}")
    if args.fewshot_bank is not None and not args.fewshot_bank.is_file():
        problems.append(f"few-shot bank not found: {args.fewshot_bank}")
    if problems:
        for problem in problems:
            log(f"error: {problem}")
        return 2
    out.mkdir(parents=True, exist_ok=True)

    t_start = time.monotonic()
    timings = {"server_start_seconds": 0.0, "pipeline_seconds": 0.0}
    previous = signal.signal(signal.SIGTERM, _on_sigterm)
    completed = False
    try:
        completed = run_model_and_pipeline(args, data, out, t_start, timings)
    except BaseException as exc:  # the submission is still written from whatever was checkpointed
        log(f"run stopped early: {type(exc).__name__}{': ' + str(exc) if str(exc) else ''}")
    finally:
        signal.signal(signal.SIGTERM, previous)

    ok = finish_submission(out, data, submission, completed)
    total = time.monotonic() - t_start
    log(f"TIMING server_start_seconds={timings['server_start_seconds']:.1f} "
        f"pipeline_seconds={timings['pipeline_seconds']:.1f} total_seconds={total:.1f}")
    if not ok:
        log("ERROR: no submission could be written")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
