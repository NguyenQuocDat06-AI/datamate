"""Concurrency benchmark for the Kaggle worker server, through bizreason.llm.TrammayClient.

For each concurrency level, sends n chat-completion requests (temperature 0) from a thread pool and
records per-request latency and output text. Reports requests/minute, p50/p90 latency, and the share of
outputs that are byte-identical to the concurrency-1 outputs for the same prompt index.

Usage:
  python gen_bench_server.py --connection conn.json --endpoint-file endpoint.json \
      --prompts prompts.jsonl --concurrency 1 4 8 16 --n 32 --out bench_out/

Prompts are JSONL rows {"system", "prompt", "max_tokens"}; request i uses row i % len(rows).
The connection and endpoint files are read but their values are never printed.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_SOURCE = Path("/home/ubuntu/agy-worktrees/arch9/source_test")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Concurrency benchmark for the worker server via TrammayClient.")
    p.add_argument("--connection", type=Path, required=True, help='JSON with "model" and "bearer_token"')
    p.add_argument("--endpoint-file", type=Path, required=True, help='JSON with "base_url"')
    p.add_argument("--prompts", type=Path, required=True, help='JSONL rows of {"system","prompt","max_tokens"}')
    p.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 8, 16], help="concurrency levels")
    p.add_argument("--n", type=int, default=32, help="requests per concurrency level")
    p.add_argument("--out", type=Path, required=True, help="output directory (bench_results.json is written here)")
    p.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="source_test directory that provides bizreason")
    p.add_argument("--request-timeout", type=float, default=280.0)
    args = p.parse_args(argv)
    if args.n < 1 or any(c < 1 for c in args.concurrency):
        p.error("--n and --concurrency values must be >= 1")
    return args


def load_prompts(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        for key in ("system", "prompt", "max_tokens"):
            if key not in row:
                raise ValueError(f"prompt row missing {key!r}")
        rows.append(row)
    if not rows:
        raise ValueError("prompts file has no rows")
    return rows


def build_client(args: argparse.Namespace, prompts: list[dict]):
    sys.path.insert(0, str(args.source))
    from bizreason.config import Settings  # noqa: E402
    from bizreason.llm import TrammayClient, endpoint_file_resolver  # noqa: E402

    conn = json.loads(args.connection.read_text(encoding="utf-8"))
    endpoint = json.loads(args.endpoint_file.read_text(encoding="utf-8"))
    settings = Settings(
        base_url=str(endpoint["base_url"]),
        model=str(conn["model"]),
        api_key=str(conn["bearer_token"]),
        max_output_tokens=max(int(r["max_tokens"]) for r in prompts),
        request_timeout=args.request_timeout,
    )
    return TrammayClient(
        settings,
        endpoint_resolver=endpoint_file_resolver(args.endpoint_file),
        outage_wait_seconds=1800,
    )


def call_chat(client, row: dict, timeout: float) -> tuple[str, str | None, int]:
    """One chat-completion at temperature 0 through the client's own transport, with per-prompt max_tokens."""
    from bizreason.llm import build_chat_messages  # noqa: E402

    messages = build_chat_messages(client.settings.model, row["system"], row["prompt"], merge_system=False)
    body = client._request(
        "POST",
        "chat/completions",
        count_call=True,
        timeout=timeout,
        json={
            "model": client.settings.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": int(row["max_tokens"]),
        },
    )
    choice = (body.get("choices") or [{}])[0]
    text = choice.get("message", {}).get("content", "")
    if isinstance(text, list):
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    usage = body.get("usage") or {}
    return text or "", choice.get("finish_reason"), int(usage.get("completion_tokens") or 0)


def run_level(client, prompts: list[dict], concurrency: int, n: int, timeout: float) -> tuple[list[dict], float]:
    def one(index: int) -> dict:
        row = prompts[index % len(prompts)]
        t0 = time.monotonic()
        try:
            text, finish, out_tokens = call_chat(client, row, timeout)
            return {
                "index": index,
                "ok": True,
                "latency_s": round(time.monotonic() - t0, 3),
                "output": text,
                "finish_reason": finish,
                "completion_tokens": out_tokens,
            }
        except Exception as exc:  # record and continue; the error text is truncated
            return {
                "index": index,
                "ok": False,
                "latency_s": round(time.monotonic() - t0, 3),
                "output": None,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        records = list(pool.map(one, range(n)))  # map preserves index order
    return records, time.monotonic() - started


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def summarize(records: list[dict], wall_s: float, reference: list[dict] | None) -> dict:
    ok_latencies = [r["latency_s"] for r in records if r["ok"]]
    ok_count = len(ok_latencies)
    identical = 0
    if reference is not None:
        for rec, ref in zip(records, reference):
            if rec["ok"] and ref["ok"] and rec["output"].encode("utf-8") == ref["output"].encode("utf-8"):
                identical += 1
    return {
        "requests": len(records),
        "ok": ok_count,
        "errors": len(records) - ok_count,
        "wall_s": round(wall_s, 2),
        "requests_per_minute": round(ok_count / wall_s * 60.0, 2) if wall_s > 0 else None,
        "p50_latency_s": quantile(ok_latencies, 0.50),
        "p90_latency_s": quantile(ok_latencies, 0.90),
        "identical_to_concurrency_1": identical / len(records) if reference is not None else None,
    }


def main(argv=None) -> int:
    args = parse_args(argv)
    prompts = load_prompts(args.prompts)
    client = build_client(args, prompts)

    levels = sorted(set(args.concurrency))
    reference_only = 1 not in levels
    if reference_only:
        levels = [1] + levels  # concurrency-1 outputs are the byte-equality reference

    results: dict[str, dict] = {}
    reference: list[dict] | None = None
    try:
        for level in levels:
            records, wall = run_level(client, prompts, level, args.n, args.request_timeout)
            if level == 1:
                reference = records
            summary = summarize(records, wall, reference)
            summary["reference_only"] = reference_only and level == 1
            summary["records"] = records
            results[str(level)] = summary
            print(
                f"concurrency={level:>3} n={summary['requests']} ok={summary['ok']} err={summary['errors']} "
                f"rpm={summary['requests_per_minute']} p50={summary['p50_latency_s']} "
                f"p90={summary['p90_latency_s']} identical={summary['identical_to_concurrency_1']}",
                flush=True,
            )
    finally:
        try:
            client.close()
        except Exception:
            pass

    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / "bench_results.json"
    payload = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prompts_file": str(args.prompts),
        "prompt_rows": len(prompts),
        "n_per_level": args.n,
        "reference_concurrency": 1,
        "levels": results,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
