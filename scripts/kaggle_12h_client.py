"""Kaggle 12-Hour Persistent GPU Server Discovery & Control CLI.

Allows on-demand querying, live chat testing, and batch evaluation against
the remote Kaggle T4 x 2 persistent server running Shaleen123/gemma4-12b-sql.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import dotenv_values


def get_root_dir() -> Path:
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent


def get_authenticated_api():
    root_dir = get_root_dir()
    env_file = root_dir / ".env"

    if env_file.exists():
        env_vars = dotenv_values(str(env_file))
        key = env_vars.get("KAGGLE_KEY") or env_vars.get("KAGGLE_API_TOKEN")
        if env_vars.get("KAGGLE_USERNAME"):
            os.environ["KAGGLE_USERNAME"] = env_vars["KAGGLE_USERNAME"]
        if key:
            os.environ["KAGGLE_KEY"] = key
            os.environ["KAGGLE_API_TOKEN"] = key

    from kaggle.api.kaggle_api_extended import KaggleApi
    api = KaggleApi()
    api.authenticate()
    if "KAGGLE_KEY" in os.environ:
        api.config_values[api.CONFIG_NAME_TOKEN] = os.environ["KAGGLE_KEY"]
    return api


def load_connection_config() -> dict:
    root_dir = get_root_dir()
    cfg_file = root_dir / "artifacts" / "kaggle_12h_server" / "connection_config.json"
    if not cfg_file.exists():
        raise FileNotFoundError(f"Server connection config not found at {cfg_file}")
    with open(cfg_file, "r", encoding="utf-8") as f:
        return json.load(f)


def query_relay_channel(channel: str, since: str = "all") -> dict | None:
    url = f"https://ntfy.sh/{channel}/json?poll=1&since={since}"
    req = urllib.request.Request(url, headers={"User-Agent": "Kaggle12hClient/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw_text = resp.read().decode("utf-8")
    except Exception as e:
        print(f"Relay query notice: {e}", file=sys.stderr)
        return None

    latest_data = None
    for line in raw_text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("event") == "message":
                msg_content = entry.get("message", "")
                try:
                    payload = json.loads(msg_content)
                except Exception:
                    payload = {"message": msg_content}
                if isinstance(payload, dict) and "base_url" in payload:
                    latest_data = payload
        except Exception:
            continue

    return latest_data


def ping_endpoint(base_url: str, token: str, timeout: int = 6) -> tuple[bool, int, float, str]:
    target = f"{base_url.rstrip('/')}/models"
    req = urllib.request.Request(
        target,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "Kaggle12hClient/1.0",
        },
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency_ms = (time.time() - t0) * 1000.0
            return resp.status == 200, resp.status, latency_ms, "OK"
    except urllib.error.HTTPError as he:
        latency_ms = (time.time() - t0) * 1000.0
        return False, he.code, latency_ms, he.reason
    except Exception as e:
        latency_ms = (time.time() - t0) * 1000.0
        return False, 0, latency_ms, str(e)


def save_active_endpoint(data: dict) -> None:
    root_dir = get_root_dir()
    out_dir = root_dir / "artifacts" / "kaggle_12h_server"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "active_endpoint.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_cached_endpoint() -> dict | None:
    root_dir = get_root_dir()
    out_file = root_dir / "artifacts" / "kaggle_12h_server" / "active_endpoint.json"
    if out_file.exists():
        try:
            with open(out_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def resolve_live_base_url(cfg: dict, max_retries: int = 1) -> tuple[str | None, dict | None]:
    relay_channel = cfg["relay_channel"]
    bearer_token = cfg["bearer_token"]

    for _ in range(max_retries):
        relay_info = query_relay_channel(relay_channel, since="all")
        if relay_info and "base_url" in relay_info:
            base_url = relay_info["base_url"]
            ok, _, latency_ms, _ = ping_endpoint(base_url, bearer_token)
            if ok:
                endpoint_record = {
                    "base_url": base_url,
                    "model": cfg.get("model", "Shaleen123/gemma4-12b-sql"),
                    "status": "online",
                    "latency_ms": round(latency_ms, 1),
                    "tier": relay_info.get("tier"),
                    "gpu_count": relay_info.get("gpu_count", 2),
                    "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                save_active_endpoint(endpoint_record)
                return base_url, endpoint_record

    cached = get_cached_endpoint()
    if cached and "base_url" in cached:
        ok, _, _, _ = ping_endpoint(cached["base_url"], bearer_token, timeout=3)
        if ok:
            return cached["base_url"], cached

    return None, None


def cmd_status(api, cfg: dict) -> int:
    kernel_slug = cfg["kernel_slug"]
    relay_channel = cfg["relay_channel"]
    bearer_token = cfg["bearer_token"]
    model_name = cfg.get("model", "Shaleen123/gemma4-12b-sql")

    print("======================================================================")
    print("        KAGGLE 12-HOUR PERSISTENT GPU SERVER TELEMETRY & STATUS        ")
    print("======================================================================")
    print(f"Kernel Slug:    {kernel_slug}")
    print(f"Relay Channel:  https://ntfy.sh/{relay_channel}")
    print(f"Target Model:   {model_name}")

    try:
        status_resp = api.kernels_status(kernel_slug)
        k_status = getattr(status_resp, "status", None) or str(status_resp)
    except Exception as e:
        k_status = f"Unknown / Error ({e})"

    print(f"Kaggle Session: {k_status}")

    relay_info = query_relay_channel(relay_channel, since="all")
    if relay_info and "base_url" in relay_info:
        base_url = relay_info["base_url"]
        tier = relay_info.get("tier", "Unknown")
        gpus = relay_info.get("gpu_count", 2)
        uptime_s = relay_info.get("uptime_s", 0)
        print(f"Discovered URL: {base_url}")
        print(f"Server Tier:    Tier {tier} | GPUs: {gpus} x Tesla T4")
        print(f"Server Uptime:  {uptime_s // 60}m {uptime_s % 60}s")

        print("Testing tunnel connectivity via /v1/models...")
        ok, code, lat, msg = ping_endpoint(base_url, bearer_token)
        if ok:
            print(f"Health Check:   200 OK (Latency: {lat:.1f} ms)")
            print("Server Status:  [LIVE & READY]")
            save_active_endpoint({
                "base_url": base_url,
                "model": model_name,
                "status": "online",
                "latency_ms": round(lat, 1),
                "tier": tier,
                "gpu_count": gpus,
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            print("======================================================================")
            print("Recommended client invocations:")
            print('  python scripts/kaggle_12h_client.py --ask "Select doanh thu nam 2024"')
            print("  python scripts/kaggle_12h_client.py --eval-limit 10")
            print("  python scripts/kaggle_12h_client.py --eval-50")
            print("")
            print("Direct bizreason CLI invocation example:")
            print(
                '$env:BIZREASON_API_KEY="<configured-in-connection_config.json>"; '
                f'python -m bizreason.cli run --data data/train --questions data/train/questions.jsonl '
                f'--base-url {base_url} --model {model_name} --stream --request-timeout 300'
            )
            print("======================================================================")
            return 0
        else:
            print(f"Health Check:   HTTP {code} ({msg}) - Latency: {lat:.1f} ms")
            print("Server Status:  [TUNNEL DETECTED BUT UNREACHABLE / WARMING UP]")
            return 0
    else:
        print("Discovered URL: (None yet - waiting for Kaggle VM to boot & start server)")
        print("Server Status:  [PROVISIONING / INITIALIZING]")
        return 0


def cmd_wait(api, cfg: dict, timeout: int = 1800, interval: int = 20) -> int:
    kernel_slug = cfg["kernel_slug"]
    relay_channel = cfg["relay_channel"]
    bearer_token = cfg["bearer_token"]
    model_name = cfg.get("model", "Shaleen123/gemma4-12b-sql")

    print(f"Waiting for Kaggle 12h server '{kernel_slug}' to become LIVE & READY...")
    print(f"Relay channel: https://ntfy.sh/{relay_channel}")
    print(f"Timeout: {timeout}s | Polling every {interval}s")

    t_start = time.time()
    while time.time() - t_start < timeout:
        elapsed = int(time.time() - t_start)
        try:
            status_resp = api.kernels_status(kernel_slug)
            k_status = str(getattr(status_resp, "status", None) or status_resp).strip().lower()
        except Exception as e:
            k_status = f"error ({e})"

        if k_status in ("failed", "error", "cancelled"):
            print(f"\n[FAIL] Kaggle kernel halted with status '{k_status}'.")
            return 1

        relay_info = query_relay_channel(relay_channel, since="all")
        if relay_info and "base_url" in relay_info:
            base_url = relay_info["base_url"]
            ok, code, lat, _ = ping_endpoint(base_url, bearer_token)
            if ok:
                print(f"\n[SUCCESS] Server is LIVE & READY at {base_url} (Latency: {lat:.1f}ms) after {elapsed}s!")
                save_active_endpoint({
                    "base_url": base_url,
                    "model": model_name,
                    "status": "online",
                    "latency_ms": round(lat, 1),
                    "tier": relay_info.get("tier"),
                    "gpu_count": relay_info.get("gpu_count", 2),
                    "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
                return 0
            else:
                sys.stdout.write(f"\r[{elapsed}s] Kaggle: {k_status} | Tunnel found ({base_url}), pinging HTTP {code}...   ")
                sys.stdout.flush()
        else:
            sys.stdout.write(f"\r[{elapsed}s] Kaggle: {k_status} | Waiting for cloudflared tunnel announcement...   ")
            sys.stdout.flush()

        time.sleep(interval)

    print(f"\n[TIMEOUT] Reached wait timeout ({timeout}s).")
    return 1


def cmd_ask(cfg: dict, prompt: str) -> int:
    bearer_token = cfg["bearer_token"]
    model_name = cfg.get("model", "Shaleen123/gemma4-12b-sql")

    base_url, _ = resolve_live_base_url(cfg, max_retries=3)
    if not base_url:
        print("Error: Could not resolve a live reachable base_url from ntfy relay or cache.", file=sys.stderr)
        print("Run `python scripts/kaggle_12h_client.py --status` to check server state.", file=sys.stderr)
        return 1

    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 1024,
        "stream": False,
    }

    print(f"Target:  {endpoint}")
    print(f"Model:   {model_name}")
    print(f"Prompt:  {prompt}\n")

    t0 = time.time()
    try:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Content-Type": "application/json",
                "User-Agent": "Kaggle12hClient/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            dur = time.time() - t0
            resp_body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Chat completion failed: {e}", file=sys.stderr)
        return 1

    choice = resp_body.get("choices", [{}])[0]
    content = choice.get("message", {}).get("content", "")
    usage = resp_body.get("usage", {})

    print("=== MODEL RESPONSE ===")
    print(content)
    print("======================")
    print(f"Latency: {dur:.2f}s | Tokens: Prompt={usage.get('prompt_tokens', '?')}, Completion={usage.get('completion_tokens', '?')}")
    return 0


def cmd_eval(cfg: dict, limit: int, workers_override: int | None = None) -> int:
    bearer_token = cfg["bearer_token"]
    model_name = cfg.get("model", "Shaleen123/gemma4-12b-sql")

    base_url, endpoint_record = resolve_live_base_url(cfg, max_retries=3)
    if not base_url:
        print("Error: Could not resolve live base_url. Check server with --status.", file=sys.stderr)
        return 1

    tier = (endpoint_record or {}).get("tier") if endpoint_record else None
    gpu_count_field = (endpoint_record or {}).get("gpu_count") if endpoint_record else None
    tier_str = str(tier).lower() if tier is not None else ""
    is_nf4_dp = tier_str in ("nf4_dp", "config_a", "4bit_dp", "2x_nf4_dp") or (gpu_count_field is not None and gpu_count_field >= 2 and tier not in (1, 2, "1", "2"))
    is_tier2 = tier == 2 or str(tier) == "2"

    if workers_override is not None:
        workers = workers_override
        worker_source = f"override (--workers {workers_override})"
    elif is_nf4_dp:
        workers = 4
        worker_source = "default for Configuration A (4 client threads pipelining 2 independent 4-bit NF4 GPU replicas)"
    elif is_tier2:
        workers = 1
        worker_source = "default for Tier 2 (Transformers gpu_lock sequential inference)"
    elif tier == 1 or str(tier) == "1":
        workers = 6 if limit <= 100 else 8
        worker_source = f"default for Tier {tier or 1} (vLLM continuous batching)"
    else:
        workers = 4 if (gpu_count_field and gpu_count_field >= 2) else 1
        worker_source = "default for active server"

    if is_nf4_dp:
        ctx_characters = "30000"
        max_output_tokens = "768"
    elif is_tier2:
        ctx_characters = "11000"
        max_output_tokens = "768"
    else:
        ctx_characters = "18000"
        max_output_tokens = "768"

    root_dir = get_root_dir()
    data_dir = root_dir / "data" / "train"
    questions_file = data_dir / "questions.jsonl"
    gold_file = data_dir / "reference_answers.jsonl"
    if not gold_file.exists():
        gold_file = data_dir / "answers.jsonl"

    dest_dir = root_dir / "artifacts" / f"kaggle_12h_eval_{limit}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    predictions_file = dest_dir / "predictions.jsonl"
    score_report_file = dest_dir / "score_report.json"

    print("======================================================================")
    print(f"  LAUNCHING BATCH EVALUATION (LIMIT={limit}) AGAINST KAGGLE 12H SERVER")
    print(f"  Base URL:    {base_url}")
    print(f"  Model:       {model_name}")
    print(f"  Tier:        {tier} ({worker_source})")
    print(f"  Workers:     {workers}")
    print(f"  Data:        {data_dir}")
    print(f"  Questions:   {questions_file}")
    print(f"  Output:      {predictions_file}")
    print("======================================================================")

    env = os.environ.copy()
    env["BIZREASON_API_KEY"] = bearer_token
    env["BIZREASON_BASE_URL"] = base_url
    env["BIZREASON_MODEL"] = model_name
    env["BIZREASON_TIMEOUT"] = "300"
    env["BIZREASON_CONTEXT_CHARS"] = ctx_characters
    env["TRAMMAY_KEY"] = bearer_token
    env["TRAMMAY_BASE_URL"] = base_url
    env["OPENROUTER_API"] = bearer_token
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{root_dir}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else str(root_dir)
    )

    run_cmd = [
        sys.executable,
        "-m",
        "bizreason.cli",
        "run",
        "--data",
        str(data_dir),
        "--questions",
        str(questions_file),
        "--output",
        str(predictions_file),
        "--model",
        model_name,
        "--base-url",
        base_url,
        "--limit",
        str(limit),
        "--stream",
        "--enable-fast-triage",
        "--group-by-business",
        "--no-fair-pacing",
        "--question-timeout",
        "300",
        "--workers",
        str(workers),
        "--request-timeout",
        "300",
        "--max-output-tokens",
        max_output_tokens,
        "--context-characters",
        ctx_characters,
    ]
    if is_tier2 or is_nf4_dp:
        run_cmd.extend(["--max-model-calls", "1", "--max-repairs", "0"])

    print(f"Command: {' '.join(run_cmd)}")
    t0 = time.time()
    proc = subprocess.run(run_cmd, env=env, check=False)
    dur = time.time() - t0

    if proc.returncode != 0:
        print(f"Evaluation finished with error code {proc.returncode} in {dur:.1f}s.")
        return proc.returncode

    print(f"\nEvaluation successfully finished in {dur:.1f}s!")

    if gold_file.exists() and predictions_file.exists():
        print("\nRunning official score verification...")
        score_cmd = [
            sys.executable,
            "-m",
            "bizreason.cli",
            "score",
            "--predictions",
            str(predictions_file),
            "--references",
            str(gold_file),
            "--questions",
            str(questions_file),
            "--output",
            str(score_report_file),
        ]
        if limit < 3000:
            score_cmd.append("--subset-predicted")
        score_proc = subprocess.run(score_cmd, env=env, check=False)
        if score_proc.returncode == 0 and score_report_file.exists():
            print("\n=== SCORE REPORT ===")
            print(score_report_file.read_text(encoding="utf-8"))

    return 0


def main():
    parser = argparse.ArgumentParser(description="Kaggle 12-Hour Persistent LLM API Server Client")
    parser.add_argument("--status", action="store_true", help="Query Kaggle session & live tunnel reachability (default)")
    parser.add_argument("--wait", action="store_true", help="Wait until tunnel URL is online & passes health check")
    parser.add_argument("--timeout", type=int, default=1800, help="Wait timeout in seconds (default: 1800)")
    parser.add_argument("--interval", type=int, default=20, help="Wait polling interval in seconds (default: 20)")
    parser.add_argument("--ask", type=str, default=None, help="Send instant chat completion prompt to the live server")
    parser.add_argument("--eval-20", action="store_true", help="Run 20-question evaluation locally against live Kaggle server")
    parser.add_argument("--eval-50", action="store_true", help="Run 50-question evaluation locally against live Kaggle server")
    parser.add_argument("--eval-1000", action="store_true", help="Run 1,000-question evaluation locally against live Kaggle server")
    parser.add_argument("--eval-limit", type=int, default=None, help="Run batch evaluation with custom question limit")
    parser.add_argument("--workers", type=int, default=None, help="Worker concurrency override (defaults: Tier 1=6/8, Tier 2=1)")
    args = parser.parse_args()

    cfg = load_connection_config()
    api = get_authenticated_api()

    if args.ask:
        sys.exit(cmd_ask(cfg, args.ask))
    elif args.eval_limit is not None:
        sys.exit(cmd_eval(cfg, limit=args.eval_limit, workers_override=args.workers))
    elif args.eval_20:
        sys.exit(cmd_eval(cfg, limit=20, workers_override=args.workers))
    elif args.eval_50:
        sys.exit(cmd_eval(cfg, limit=50, workers_override=args.workers))
    elif args.eval_1000:
        sys.exit(cmd_eval(cfg, limit=1000, workers_override=args.workers))
    elif args.wait:
        sys.exit(cmd_wait(api, cfg, timeout=args.timeout, interval=args.interval))
    else:
        sys.exit(cmd_status(api, cfg))


if __name__ == "__main__":
    main()
