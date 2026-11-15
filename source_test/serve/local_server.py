#!/usr/bin/env python3
"""Local OpenAI-compatible model server for the offline exam machine (no network, no downloads).

Three roles in this one file:

  orchestrator  (default)  starts one worker per GPU, waits for each to answer /v1/models and a warm-up
                           request, then runs the gateway on 127.0.0.1:PORT. Stops on SIGTERM, SIGINT or
                           POST /admin/shutdown, and stops every worker before it exits.
  worker        (--worker) one process per GPU: 4-bit NF4 model via transformers/bitsandbytes, micro-batched
                           greedy decoding, shared-prefix KV cache with a start-up self-check, eos handling
                           and think-stripping. Imports torch and transformers only in this role.
  gateway                  bearer-authenticated round-robin (least busy first) over the live workers.

    python serve/local_server.py --model-dir /path/to/checkpoint --port 8000 --gpus 0,1 --batch-max 6 \\
        --token-file /path/to/token.txt

The bearer token is taken from --token, then from the DATAMATE_SERVER_TOKEN environment variable. If neither is
set, a random token is generated and written to --token-file (mode 600); it is never printed. The token is passed
to the workers through the environment, not on a command line.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import http.server
import json
import os
import queue
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

TOKEN_ENV = "DATAMATE_SERVER_TOKEN"
GEMMA_EOS_FALLBACK = [1, 106, 50]
GEMMA_MODEL_CHAIN = ["Gemma4UnifiedForConditionalGeneration", "AutoModelForImageTextToText", "AutoModelForCausalLM"]
CAUSAL_MODEL_CHAIN = ["AutoModelForCausalLM"]
# Non-Gemma checkpoints saved as *ForConditionalGeneration load through the image-text-to-text auto class;
# text-only causal LMs keep AutoModelForCausalLM.
CONDITIONAL_MODEL_CHAIN = ["AutoModelForImageTextToText", "AutoModelForCausalLM"]
CAUSAL_EOS_TOKEN_NAMES = ["<|im_end|>", "<|endoftext|>"]
THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"
MAX_REQUEST_BYTES = 64 * 1024 * 1024
GATEWAY_FORWARD_TIMEOUT = 600.0
WORKER_WARMUP_TIMEOUT = 300.0
WARMUP_PROMPT = "SELECT 1;"
UNAUTHORIZED = "Unauthorized: Invalid or missing Bearer token"
# Loopback only: never route these calls through an HTTP proxy from the environment.
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
LOOPBACK_NO_PROXY = "127.0.0.1,localhost"


def log(msg: str) -> None:
    print(f"[local_server {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------------------------------------------
# Pure helpers (no torch): model-class selection, eos ids, think-stripping, batching and prefix-cache logic.
# ---------------------------------------------------------------------------------------------------------------

def load_model_config(model_dir: str | Path) -> dict[str, Any] | None:
    """Parsed config.json as a dict, or None when it is missing or unreadable."""
    try:
        with open(Path(model_dir) / "config.json", "r", encoding="utf-8") as handle:
            cfg = json.load(handle)
        return cfg if isinstance(cfg, dict) else None
    except Exception:
        return None


def select_model_classes(config: dict[str, Any] | None) -> dict[str, Any]:
    """Loader choice from config.json's model_type. Returns class and tokenizer NAMES; the caller resolves them.

    gemma  -> AutoProcessor (fallback AutoTokenizer) and the Gemma class chain.
    causal -> AutoTokenizer and AutoModelForCausalLM (Qwen3 and other text-only causal LMs).
    causal with *ForConditionalGeneration architecture -> AutoProcessor and the image-text-to-text chain.
    legacy -> no readable model_type: the Gemma-first chain.
    """
    model_type = config.get("model_type") if isinstance(config, dict) else None
    if not isinstance(model_type, str) or not model_type:
        return {"family": "legacy", "tokenizer": "AutoProcessor", "model_chain": list(GEMMA_MODEL_CHAIN)}
    if "gemma" in model_type.lower():
        return {"family": "gemma", "tokenizer": "AutoProcessor", "model_chain": list(GEMMA_MODEL_CHAIN)}
    architectures = config.get("architectures") or []
    if any(isinstance(a, str) and a.endswith("ForConditionalGeneration") for a in architectures):
        return {"family": "causal", "tokenizer": "AutoProcessor", "model_chain": list(CONDITIONAL_MODEL_CHAIN)}
    return {"family": "causal", "tokenizer": "AutoTokenizer", "model_chain": list(CAUSAL_MODEL_CHAIN)}


def _int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [int(v) for v in value if v is not None]
    return [int(value)]


def merge_eos_ids(family: str, gen_eos: Any, tok_eos: Any, named_ids=(), unk_id: Any = None) -> list[int]:
    """EOS ids. generation_config.json wins when it has eos_token_id. Without it, Gemma keeps its fixed fallback
    ids and causal models start empty. Tokenizer eos and named chat-end ids are appended, skipping None and unk."""
    ids = _int_list(gen_eos) if gen_eos is not None else ([] if family == "causal" else list(GEMMA_EOS_FALLBACK))
    for cand in _int_list(tok_eos) + [c for c in named_ids if c is not None]:
        if unk_id is not None and int(cand) == int(unk_id):
            continue
        if int(cand) not in ids:
            ids.append(int(cand))
    return ids


def strip_think(text: Any) -> Any:
    """Drop a leading <think>...</think> block. A block that never closes returns an empty string."""
    if not isinstance(text, str):
        return text
    head = text.lstrip()
    if not head.startswith(THINK_OPEN):
        return text
    end = head.find(THINK_CLOSE)
    if end < 0:
        return ""
    return head[end + len(THINK_CLOSE):].lstrip()


def use_batch_path(stream: bool, temperature: float, batch_max: int) -> bool:
    """Only non-streaming greedy requests are micro-batched, and only when batch_max > 1."""
    return (not stream) and (not temperature > 0) and batch_max > 1


def left_pad(seqs: list[list[int]], pad_id: int) -> tuple[list[list[int]], list[list[int]], int]:
    width = max(len(s) for s in seqs)
    ids = [[pad_id] * (width - len(s)) + list(s) for s in seqs]
    mask = [[0] * (width - len(s)) + [1] * len(s) for s in seqs]
    return ids, mask, width


def cut_new_tokens(row: list[int], max_tokens: int, eos_ids) -> list[int]:
    """Keep new tokens up to and including the first eos, and at most max_tokens (as un-batched output does)."""
    limit = min(len(row), max(int(max_tokens), 0))
    for i in range(limit):
        if row[i] in eos_ids:
            return list(row[: i + 1])
    return list(row[:limit])


def is_oom_error(exc: BaseException) -> bool:
    return isinstance(exc, MemoryError) or type(exc).__name__ == "OutOfMemoryError" or "out of memory" in str(exc).lower()


class _BatchItem:
    def __init__(self, ids, max_tokens, boundary=None):
        self.ids = list(ids)
        self.max_tokens = int(max_tokens)
        self.boundary = boundary  # token index where the shared system prefix ends (or None)
        self.done = threading.Event()
        self.result = None
        self.error = None


class MicroBatcher:
    """One queue and one GPU thread: groups concurrent greedy requests into one generate call."""

    def __init__(self, generate_batch, lock, eos_ids, max_batch, wait_ms, on_oom=None, log=None):
        self._generate_batch = generate_batch  # (id lists, max_new_tokens, boundaries) -> raw new-token rows
        self._lock = lock
        self._eos_ids = set(int(x) for x in eos_ids)
        self._max_batch = max(1, int(max_batch))
        self._wait_s = max(0, int(wait_ms)) / 1000.0
        self._on_oom = on_oom
        self._log = log or (lambda msg: None)
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="micro-batcher")

    def start(self) -> None:
        self._thread.start()

    def submit(self, ids, max_tokens, boundary=None):
        """Blocking. Returns (new_tokens, input_len, n_tokens)."""
        item = _BatchItem(ids, max_tokens, boundary)
        self._q.put(item)
        item.done.wait()
        if item.error is not None:
            raise item.error
        return item.result

    def _loop(self) -> None:
        while True:
            batch = [self._q.get()]
            deadline = time.monotonic() + self._wait_s
            while len(batch) < self._max_batch:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(self._q.get(timeout=remaining))
                except queue.Empty:
                    break
            try:
                self._run_group(batch)
            except Exception as exc:  # the GPU thread must never die
                for it in batch:
                    if not it.done.is_set():
                        it.error = exc
                        it.done.set()

    def _run_group(self, items) -> None:
        t0 = time.time()
        max_new = max(it.max_tokens for it in items)
        try:
            with self._lock:
                rows = self._generate_batch([it.ids for it in items], max_new, [it.boundary for it in items])
        except Exception as exc:
            if is_oom_error(exc):
                if self._on_oom is not None:
                    self._on_oom()
                if len(items) > 1:
                    mid = len(items) // 2
                    self._run_group(items[:mid])
                    self._run_group(items[mid:])
                    return
            for it in items:
                it.error = exc
                it.done.set()
            return
        total_in = 0
        total_new = 0
        for it, row in zip(items, rows):
            new_tokens = cut_new_tokens(row, it.max_tokens, self._eos_ids)
            it.result = (new_tokens, len(it.ids), len(new_tokens))
            total_in += len(it.ids)
            total_new += len(new_tokens)
            it.done.set()
        self._log(f"batch size={len(items)} input_tokens={total_in} new_tokens={total_new} seconds={time.time() - t0:.2f}")


def common_prefix_len(a, b) -> int:
    """Length of the common leading run of two token-id sequences."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


class PrefixCacheStore:
    """Small LRU of shared-prefix KV entries (one store per worker process).

    Bookkeeping only: the model work is injected. make_entry(prefix_ids) runs the prefix forward pass and returns
    an opaque entry; on_free(entry) releases one. An entry is only handed out for a prompt whose token ids start
    with exactly the entry's ids, so the output never depends on what else is cached. Any failure while creating
    an entry disables the store for the process.
    """

    def __init__(self, make_entry, slots=2, min_tokens=512, on_free=None, log=None):
        self._make = make_entry
        self._slots = max(1, int(slots))
        self._min = max(1, int(min_tokens))
        self._on_free = on_free or (lambda entry: None)
        self._log = log or (lambda msg: None)
        self._entries: list[tuple[tuple, Any]] = []  # least recently used first
        self._last_ids = None  # previous request's ids (fallback boundary source)
        self.enabled = True
        self.hits = 0
        self.created = 0
        self.none = 0
        self.evicted = 0
        self.skipped_tokens = 0

    def disable(self, reason: str) -> None:
        self.enabled = False
        self.clear()
        self._log(f"prefix cache disabled: {reason}")

    def clear(self) -> None:
        entries, self._entries = self._entries, []
        self._last_ids = None
        for _, entry in entries:
            self._on_free(entry)

    def _find(self, ids) -> int:
        """Index of the longest cached prefix that ids starts with (leaving >= 1 suffix token), or -1."""
        best, best_n = -1, 0
        for i, (key, _) in enumerate(self._entries):
            n = len(key)
            if best_n < n < len(ids) and tuple(ids[:n]) == key:
                best, best_n = i, n
        return best

    def _choose_boundary(self, ids, boundary):
        if boundary is not None:
            n = int(boundary)
        elif self._last_ids is not None:
            n = common_prefix_len(self._last_ids, ids)
        else:
            return None
        return n if self._min <= n < len(ids) else None

    def acquire(self, ids, boundary=None):
        """Returns (entry, reused_tokens). entry is None when this request runs without the cache."""
        if not self.enabled:
            return None, 0
        i = self._find(ids)
        if i >= 0:
            item = self._entries.pop(i)
            self._entries.append(item)
            n = len(item[0])
            self.hits += 1
            self.skipped_tokens += n
            self._last_ids = list(ids)
            return item[1], n
        n = self._choose_boundary(ids, boundary)
        self._last_ids = list(ids)
        if n is None:
            self.none += 1
            return None, 0
        while len(self._entries) >= self._slots:
            _, old = self._entries.pop(0)
            self.evicted += 1
            self._on_free(old)
        try:
            entry = self._make(list(ids[:n]))
        except Exception as exc:
            if is_oom_error(exc):
                self.disable("out of memory during prefix cache creation")
            else:
                self.disable(f"error during prefix cache creation ({type(exc).__name__})")
            return None, 0
        self._entries.append((tuple(ids[:n]), entry))
        self.created += 1
        return entry, 0


def prefix_generate_rows(store: PrefixCacheStore, ops, seqs, boundaries=None, max_new_tokens=0, log=None):
    """Raw new-token rows (aligned with seqs) for one greedy batch, reusing shared-prefix KV where possible.

    ops.plain_batch(seqs, max_new) -> rows without cache. ops.single(ids, max_new, entry) -> one row.
    ops.batch_prefix(seqs, max_new, entry) -> rows, or None when that cache layout cannot be batched.
    """
    log = log or (lambda msg: None)
    boundaries = list(boundaries) if boundaries is not None else [None] * len(seqs)
    entries = []
    for ids, b in zip(seqs, boundaries):
        entry, reused = store.acquire(ids, b)
        status = "none" if entry is None else ("hit" if reused else "created")
        log(f"prefix cache: {status} prompt_tokens={len(ids)} skipped_tokens={reused}")
        entries.append(entry)
    rows = [None] * len(seqs)
    plain = [i for i, e in enumerate(entries) if e is None]
    groups: dict[int, list[int]] = {}
    for i, e in enumerate(entries):
        if e is not None:
            groups.setdefault(id(e), []).append(i)
    if plain:
        for i, row in zip(plain, ops.plain_batch([seqs[i] for i in plain], max_new_tokens)):
            rows[i] = row
    for idxs in groups.values():
        entry = entries[idxs[0]]
        sub = ops.batch_prefix([seqs[i] for i in idxs], max_new_tokens, entry) if len(idxs) > 1 else None
        if sub is None:
            sub = [ops.single(seqs[i], max_new_tokens, entry) for i in idxs]
        for i, row in zip(idxs, sub):
            rows[i] = row
    return rows


def prefix_self_check(ops, seqs, boundary, eos_ids, max_new: int = 8) -> str | None:
    """Greedy output with the prefix cache must equal the plain output token for token.
    Returns None when it does, otherwise a short reason string (the caller disables the cache)."""
    check = PrefixCacheStore(ops.make_entry, slots=2, min_tokens=1)

    def run_plain():
        rows = [ops.single(s, max_new, None) for s in seqs] + list(ops.plain_batch(list(seqs), max_new))
        return [cut_new_tokens(r, max_new, eos_ids) for r in rows]

    def run_cached():
        rows = [prefix_generate_rows(check, ops, [s], [boundary], max_new)[0] for s in seqs]
        rows += prefix_generate_rows(check, ops, list(seqs), [boundary] * len(seqs), max_new)
        if not check.enabled or check.created != 1 or check.hits < 2:
            raise RuntimeError("prefix path not exercised")
        return [cut_new_tokens(r, max_new, eos_ids) for r in rows]

    try:
        try:
            expected = run_plain()
        except Exception as exc:
            return f"self-check reference failed ({type(exc).__name__})"
        try:
            got = run_cached()
        except Exception as exc:
            return f"self-check error ({type(exc).__name__})"
        return None if got == expected else "mismatch"
    finally:
        check.clear()


def resolve_model_cls(chain: list[str]):
    """First class name in chain that transformers provides; the last candidate's error is raised."""
    import transformers as _transformers

    for i, name in enumerate(chain):
        try:
            return getattr(_transformers, name)
        except Exception:
            if i == len(chain) - 1:
                raise
    raise RuntimeError("empty model class chain")


# ---------------------------------------------------------------------------------------------------------------
# HTTP helpers shared by the worker and the gateway (stdlib only).
# ---------------------------------------------------------------------------------------------------------------

def bearer_ok(headers, token: str) -> bool:
    parts = (headers.get("Authorization") or "").strip().split()
    return len(parts) == 2 and parts[0].lower() == "bearer" and hmac.compare_digest(parts[1].encode(), token.encode())


def send_raw(handler, status: int, data: bytes, content_type: str = "application/json") -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def send_json(handler, status: int, payload: Any) -> None:
    send_raw(handler, status, json.dumps(payload).encode("utf-8"))


def send_error_json(handler, status: int, message: str, kind: str = "error") -> None:
    send_json(handler, status, {"error": {"message": message, "type": kind}})


def read_body(handler) -> bytes | None:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        return None
    if length < 0 or length > MAX_REQUEST_BYTES:
        return None
    return handler.rfile.read(length)


def parse_json_object(raw: bytes) -> dict[str, Any] | None:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def completion_payload(model: str, text: str, in_len: int, out_len: int) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{secrets.token_hex(6)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": int(in_len), "completion_tokens": int(out_len), "total_tokens": int(in_len + out_len)},
    }


def sse_body(payload: dict[str, Any]) -> bytes:
    """A complete chat completion as the two SSE chunks an OpenAI stream would send (no real streaming)."""
    text = payload["choices"][0]["message"]["content"]
    base = {
        "id": payload.get("id") or f"chatcmpl-{secrets.token_hex(6)}",
        "object": "chat.completion.chunk",
        "created": payload.get("created") or int(time.time()),
        "model": payload.get("model") or "",
    }
    chunks = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode("utf-8")


# ---------------------------------------------------------------------------------------------------------------
# Worker role: loads the model on the GPU selected by CUDA_VISIBLE_DEVICES and serves /v1/chat/completions.
# ---------------------------------------------------------------------------------------------------------------

def run_worker(args: argparse.Namespace) -> int:
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        log(f"worker: {TOKEN_ENV} is not set")
        return 2
    port = int(args.worker_port)
    served_model = args.served_model_name
    batch_max = max(1, int(args.batch_max))
    batch_wait_ms = int(args.batch_wait_ms)
    prefix_on = args.prefix_cache == "on"

    def wlog(msg: str) -> None:
        print(f"[Worker {port}] {msg}", flush=True)

    import copy

    import torch
    torch.manual_seed(int(os.environ.get("DATAMATE_SEED", "42")))
    from transformers import AutoProcessor, AutoTokenizer, BitsAndBytesConfig

    cuda = torch.cuda.is_available()
    if cuda:
        torch.cuda.set_device(0)
    dev = "cuda:0" if cuda else "cpu"

    def cuda_ctx():
        return torch.cuda.device(0) if cuda else contextlib.nullcontext()

    model_dir = str(Path(args.model_dir))
    config = load_model_config(model_dir)
    sel = select_model_classes(config)
    family = sel["family"]
    wlog(f"model_type={(config or {}).get('model_type')} family={family} device={dev}")

    if sel["tokenizer"] == "AutoProcessor":
        try:
            processor = AutoProcessor.from_pretrained(model_dir, trust_remote_code=True)
            tokenizer = getattr(processor, "tokenizer", processor)
        except Exception:
            tokenizer = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=True)
    else:
        tokenizer = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=True)

    gen_eos = None
    gen_cfg_path = Path(model_dir) / "generation_config.json"
    if gen_cfg_path.exists():
        try:
            gen_eos = json.loads(gen_cfg_path.read_text(encoding="utf-8")).get("eos_token_id")
        except Exception as exc:
            wlog(f"notice reading generation_config.json: {exc}")
    named_eos = []
    if family == "causal":
        for name in CAUSAL_EOS_TOKEN_NAMES:
            try:
                named_eos.append(tokenizer.convert_tokens_to_ids(name))
            except Exception:
                pass
    eos_ids = merge_eos_ids(family, gen_eos, getattr(tokenizer, "eos_token_id", None), named_eos,
                            getattr(tokenizer, "unk_token_id", None))
    wlog(f"resolved eos_ids: {eos_ids}")

    model_cls = resolve_model_cls(sel["model_chain"])
    wlog(f"using model class: {model_cls.__name__}")

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.float16,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
    )
    load_kwargs: dict[str, Any] = {
        "device_map": {"": 0} if cuda else "cpu",
        "quantization_config": bnb_config if cuda else None,
        "attn_implementation": "sdpa",
        "trust_remote_code": True,
        "torch_dtype": torch.float16,
    }
    wlog("loading 4-bit NF4 weights")
    try:
        model = model_cls.from_pretrained(model_dir, **load_kwargs)
    except TypeError:
        load_kwargs.pop("torch_dtype", None)
        load_kwargs["dtype"] = torch.float16
        model = model_cls.from_pretrained(model_dir, **load_kwargs)
    for p in model.parameters():
        if p.dtype == torch.bfloat16:
            p.data = p.data.to(torch.float16)
    for b in model.buffers():
        if b.dtype == torch.bfloat16:
            b.data = b.data.to(torch.float16)
    model.eval()

    gpu_lock = threading.Lock()
    try:
        tokenizer.padding_side = "left"
    except Exception:
        pass
    pad_id = tokenizer.pad_token_id if getattr(tokenizer, "pad_token_id", None) is not None else (eos_ids[0] if eos_ids else 0)

    wlog("startup self-test")
    test_inputs = tokenizer("SELECT 1", return_tensors="pt").to(dev)
    with gpu_lock, cuda_ctx(), torch.inference_mode():
        try:
            model.generate(**test_inputs, max_new_tokens=2, logits_to_keep=1)
        except (TypeError, ValueError):
            model.generate(**test_inputs, max_new_tokens=2)
    del test_inputs
    _empty_cuda_cache(cuda)
    wlog("startup self-test passed")

    def render_prompt(messages: list[dict[str, Any]]) -> str:
        if hasattr(tokenizer, "apply_chat_template"):
            try:
                return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
            except Exception:
                try:
                    return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
                except Exception:
                    pass
        return "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages) + "\nassistant:\n"

    def run_generation(prompt_text: str, max_tokens: int, temperature: float):
        inputs = tokenizer(prompt_text, return_tensors="pt").to(dev)
        input_len = inputs.input_ids.shape[1]
        gen_kwargs = {
            "max_new_tokens": max_tokens,
            "temperature": max(temperature, 1e-4) if temperature > 0 else None,
            "do_sample": bool(temperature > 0),
            "eos_token_id": eos_ids,
            "pad_token_id": 0,
            "use_cache": True,
        }
        try:
            with gpu_lock, cuda_ctx(), torch.inference_mode():
                try:
                    outputs = model.generate(**inputs, logits_to_keep=1, **gen_kwargs)
                except (TypeError, ValueError):
                    outputs = model.generate(**inputs, **gen_kwargs)
            new_tokens = outputs[0][input_len:].tolist()
            return new_tokens, input_len, len(new_tokens)
        except (RuntimeError, torch.cuda.OutOfMemoryError):
            _empty_cuda_cache(cuda)
            raise
        finally:
            del inputs

    def plain_generate_batch(seqs, max_new_tokens: int):
        """One greedy generate over a left-padded batch; returns raw new-token rows (caller cuts them)."""
        ids, mask, width = left_pad(seqs, pad_id)
        input_ids = torch.tensor(ids, dtype=torch.long, device=dev)
        attention_mask = torch.tensor(mask, dtype=torch.long, device=dev)
        gen_kwargs = {
            "max_new_tokens": max_new_tokens,
            "temperature": None,
            "do_sample": False,
            "eos_token_id": eos_ids,
            "pad_token_id": 0,
            "use_cache": True,
        }
        with cuda_ctx(), torch.inference_mode():
            try:
                outputs = model.generate(input_ids=input_ids, attention_mask=attention_mask, logits_to_keep=1, **gen_kwargs)
            except (TypeError, ValueError):
                outputs = model.generate(input_ids=input_ids, attention_mask=attention_mask, **gen_kwargs)
        return [row[width:].tolist() for row in outputs]

    class PrefixEntry:
        """Prefix token ids plus the KV cache computed for them (never mutated after creation)."""

        def __init__(self, ids, cache):
            self.ids = tuple(int(x) for x in ids)
            self.cache = cache

    class PrefixOps:
        def _check_entry(self, ids, entry) -> None:
            n = len(entry.ids)
            if len(ids) <= n or tuple(int(x) for x in ids[:n]) != entry.ids:
                raise ValueError("prefix cache: request does not start with the cached prefix")

        def _generate(self, input_ids, attention_mask, max_new, cache=None):
            kw: dict[str, Any] = {
                "max_new_tokens": int(max_new),
                "temperature": None,
                "do_sample": False,
                "eos_token_id": eos_ids,
                "pad_token_id": 0,
                "use_cache": True,
            }
            if cache is not None:
                kw["past_key_values"] = cache
            with cuda_ctx(), torch.inference_mode():
                try:
                    return model.generate(input_ids=input_ids, attention_mask=attention_mask, logits_to_keep=1, **kw)
                except (TypeError, ValueError):
                    return model.generate(input_ids=input_ids, attention_mask=attention_mask, **kw)

        def make_entry(self, prefix_ids):
            ids = torch.tensor([list(prefix_ids)], dtype=torch.long, device=dev)
            with cuda_ctx(), torch.inference_mode():
                try:
                    out = model(input_ids=ids, use_cache=True, logits_to_keep=1)
                except TypeError:
                    out = model(input_ids=ids, use_cache=True)
            return PrefixEntry(prefix_ids, out.past_key_values)

        def single(self, ids, max_new, entry):
            input_ids = torch.tensor([list(ids)], dtype=torch.long, device=dev)
            cache = None
            if entry is not None:
                self._check_entry(ids, entry)
                cache = copy.deepcopy(entry.cache)  # generate() appends to the cache it is given
            out = self._generate(input_ids, torch.ones_like(input_ids), max_new, cache)
            return out[0][len(ids):].tolist()

        def batch_prefix(self, seqs, max_new, entry):
            """Rows laid out as prefix | left-pad | suffix, with the prefix cache repeated per row."""
            if not hasattr(entry.cache, "batch_repeat_interleave"):
                return None
            for s in seqs:
                self._check_entry(s, entry)
            n = len(entry.ids)
            suffixes = [list(s[n:]) for s in seqs]
            w = max(len(s) for s in suffixes)
            ids, mask = [], []
            for s in suffixes:
                pad = w - len(s)
                ids.append(list(entry.ids) + [pad_id] * pad + s)
                mask.append([1] * n + [0] * pad + [1] * len(s))
            input_ids = torch.tensor(ids, dtype=torch.long, device=dev)
            attn = torch.tensor(mask, dtype=torch.long, device=dev)
            cache = copy.deepcopy(entry.cache)
            cache.batch_repeat_interleave(len(seqs))
            out = self._generate(input_ids, attn, max_new, cache)
            width = n + w
            return [row[width:].tolist() for row in out]

    prefix_ops = PrefixOps()
    prefix_store = PrefixCacheStore(
        prefix_ops.make_entry, slots=args.prefix_cache_slots, min_tokens=args.prefix_min_tokens,
        on_free=lambda entry: _empty_cuda_cache(cuda), log=wlog,
    )
    # prefix_generate_rows and prefix_self_check take one object with these operations, bound to this model.
    ops = SimpleNamespace(
        make_entry=prefix_ops.make_entry,
        single=prefix_ops.single,
        batch_prefix=prefix_ops.batch_prefix,
        plain_batch=plain_generate_batch,
    )

    def generate_batch(seqs, max_new_tokens: int, boundaries=None):
        """Greedy rows for a micro-batch (called under gpu_lock). Uses the prefix cache when it is enabled."""
        if not prefix_store.enabled:
            return plain_generate_batch(seqs, max_new_tokens)
        return prefix_generate_rows(prefix_store, ops, seqs, boundaries, max_new_tokens, log=wlog)

    probe_memo: dict[str, list[int]] = {}

    def prefix_boundary(messages, ids):
        """Token index where the system part of the prompt ends, from rendering the template with the system
        messages and an empty user turn. None when it cannot be determined."""
        sys_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]
        if not sys_msgs or not hasattr(tokenizer, "apply_chat_template"):
            return None
        key = json.dumps(sys_msgs, sort_keys=True, default=str)
        probe = probe_memo.get(key)
        if probe is None:
            probe_msgs = sys_msgs + [{"role": "user", "content": ""}]
            try:
                try:
                    text = tokenizer.apply_chat_template(probe_msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
                except Exception:
                    text = tokenizer.apply_chat_template(probe_msgs, tokenize=False, add_generation_prompt=True)
                probe = list(tokenizer(text)["input_ids"])
            except Exception:
                return None
            if len(probe_memo) < 16:
                probe_memo[key] = probe
        return common_prefix_len(probe, ids)

    def model_sliding_window() -> int:
        for cfg in (getattr(model, "config", None), getattr(getattr(model, "config", None), "text_config", None)):
            w = getattr(cfg, "sliding_window", None)
            if w:
                return int(w)
        return 0

    def run_prefix_self_check() -> None:
        """Two synthetic prompts sharing a prefix longer than any sliding window, so windowed layers are exercised."""
        try:
            target = max(200, model_sliding_window() + 64)
            shared = "You are a careful SQL assistant. Answer with one query. "
            while len(tokenizer(shared)["input_ids"]) < target:
                shared = shared * 2
            a = list(tokenizer(shared + "Question: count the rows of table one.")["input_ids"])
            b = list(tokenizer(shared + "Question: list the names in table two.")["input_ids"])
            n = common_prefix_len(a, b)
        except Exception as exc:
            prefix_store.disable(f"self-check prompts failed ({type(exc).__name__})")
            return
        with gpu_lock:
            reason = prefix_self_check(ops, [a, b], n, eos_ids)
        _empty_cuda_cache(cuda)
        if reason is None:
            wlog(f"prefix cache self-check passed (prefix_tokens={n})")
        else:
            prefix_store.disable(reason)

    batcher = MicroBatcher(generate_batch, gpu_lock, eos_ids, batch_max, batch_wait_ms,
                           on_oom=lambda: _empty_cuda_cache(cuda), log=wlog)
    if batch_max > 1:
        batcher.start()
        wlog(f"micro-batching on: max={batch_max} wait_ms={batch_wait_ms} pad_id={pad_id}")

    if prefix_on:
        run_prefix_self_check()
    else:
        prefix_store.disable("prefix cache off (--prefix-cache off)")

    def sync_generate(messages: list[dict[str, Any]], max_tokens: int, temperature: float) -> tuple[str, int, int]:
        prompt_text = render_prompt(messages)
        if use_batch_path(False, temperature, batch_max):
            ids = tokenizer(prompt_text)["input_ids"]
            boundary = prefix_boundary(messages, ids) if prefix_store.enabled else None
            new_tokens, in_len, out_len = batcher.submit(ids, max_tokens, boundary)
        elif prefix_store.enabled and not (temperature > 0):
            ids = tokenizer(prompt_text)["input_ids"]
            boundary = prefix_boundary(messages, ids)
            with gpu_lock:
                (new_tokens,) = generate_batch([ids], max_tokens, [boundary])
            in_len, out_len = len(ids), len(new_tokens)
        else:
            new_tokens, in_len, out_len = run_generation(prompt_text, max_tokens, temperature)
        text = strip_think(tokenizer.decode(new_tokens, skip_special_tokens=True))
        return text, in_len, out_len

    class WorkerHandler(http.server.BaseHTTPRequestHandler):
        server_version = "DatamateWorker/1"

        def log_message(self, fmt, *a) -> None:  # request logging stays off
            return

        def do_GET(self) -> None:
            if self.path.rstrip("/") != "/v1/models":
                return send_error_json(self, 404, "not found", "not_found")
            if not bearer_ok(self.headers, token):
                return send_error_json(self, 401, UNAUTHORIZED, "auth_error")
            send_json(self, 200, {"object": "list", "data": [{"id": served_model, "object": "model"}]})

        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                return send_error_json(self, 404, "not found", "not_found")
            if not bearer_ok(self.headers, token):
                return send_error_json(self, 401, UNAUTHORIZED, "auth_error")
            raw = read_body(self)
            body = parse_json_object(raw) if raw is not None else None
            if body is None:
                return send_error_json(self, 400, "request body must be a JSON object", "invalid_request")
            messages = body.get("messages", [])
            if not isinstance(messages, list):
                return send_error_json(self, 400, "messages must be a list", "invalid_request")
            try:
                max_tokens = int(body.get("max_tokens", 4096))
                temperature = float(body.get("temperature", 0.0))
                text, in_len, out_len = sync_generate(messages, max_tokens, temperature)
            except Exception as exc:
                traceback.print_exc()
                return send_error_json(self, 500, f"{type(exc).__name__}: {exc}", "server_error")
            send_json(self, 200, completion_payload(served_model, text, in_len, out_len))

    class WorkerServer(http.server.ThreadingHTTPServer):
        daemon_threads = True

    wlog(f"listening on 127.0.0.1:{port}")
    httpd = WorkerServer(("127.0.0.1", port), WorkerHandler)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
    return 0


def _empty_cuda_cache(cuda: bool) -> None:
    if cuda:
        import torch

        torch.cuda.empty_cache()


# ---------------------------------------------------------------------------------------------------------------
# Gateway: bearer auth, least-busy round-robin over live workers, /admin/shutdown.
# ---------------------------------------------------------------------------------------------------------------

class WorkerPool:
    """Live worker ports with in-flight counts. The least busy worker is chosen; ties go round-robin."""

    def __init__(self, ports: list[int]):
        self._lock = threading.Lock()
        self._live = list(ports)
        self._active = {p: 0 for p in ports}
        self._rr = 0

    def live(self) -> list[int]:
        with self._lock:
            return list(self._live)

    def acquire(self) -> int | None:
        with self._lock:
            if not self._live:
                return None
            n = len(self._live)
            best = min(range(n), key=lambda i: (self._active[self._live[i]], (i - self._rr) % n))
            port = self._live[best]
            self._active[port] += 1
            self._rr = (best + 1) % n
            return port

    def release(self, port: int) -> None:
        with self._lock:
            if port in self._active:
                self._active[port] = max(0, self._active[port] - 1)

    def drop(self, port: int) -> None:
        with self._lock:
            if port in self._live:
                self._live.remove(port)


class GatewayState:
    def __init__(self, token: str, served_model: str, worker_ports: list[int], stop_event: threading.Event):
        self.token = token
        self.served_model = served_model
        self.stop_event = stop_event
        self.pool = WorkerPool(worker_ports)
        self.metadata: dict[str, Any] = {}
        self.activity_file: Path | None = None

    def touch(self) -> None:
        if self.activity_file is not None:
            self.activity_file.touch()

    def forward_chat(self, raw: bytes) -> tuple[int, bytes]:
        """Send the request to the least busy live worker. A worker that refuses connections is dropped and the
        request is retried on another one; any other failure is returned to the client."""
        last_error = "no live worker"
        while True:
            port = self.pool.acquire()
            if port is None:
                return 503, _error_bytes(f"no live worker: {last_error}", "server_error")
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    data=raw,
                    headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                    method="POST",
                )
                with _LOCAL_OPENER.open(req, timeout=GATEWAY_FORWARD_TIMEOUT) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()
            except urllib.error.URLError as exc:
                last_error = str(exc.reason)
                if isinstance(exc.reason, ConnectionError):
                    log(f"worker on 127.0.0.1:{port} refused the connection; removed from the pool")
                    self.pool.drop(port)
                    continue
                return 502, _error_bytes(f"worker on port {port}: {exc.reason}", "server_error")
            except OSError as exc:
                return 502, _error_bytes(f"worker on port {port}: {type(exc).__name__}", "server_error")
            finally:
                self.pool.release(port)


def _error_bytes(message: str, kind: str) -> bytes:
    return json.dumps({"error": {"message": message, "type": kind}}).encode("utf-8")


class GatewayHandler(http.server.BaseHTTPRequestHandler):
    server_version = "DatamateGateway/1"

    def log_message(self, fmt, *a) -> None:
        return

    def do_GET(self) -> None:
        state: GatewayState = self.server.state  # type: ignore[attr-defined]
        path = self.path.rstrip("/")
        if path not in ("/v1/models", "/v1/metadata", "/health"):
            return send_error_json(self, 404, "not found", "not_found")
        if not bearer_ok(self.headers, state.token):
            return send_error_json(self, 401, UNAUTHORIZED, "auth_error")
        if path != "/v1/models":
            if not state.metadata:
                return send_error_json(self, 404, "metadata not configured", "not_found")
            return send_json(self, 200, {**state.metadata, "ready": True})
        send_json(self, 200, {"object": "list", "data": [{"id": state.served_model, "object": "model"}]})

    def do_POST(self) -> None:
        state: GatewayState = self.server.state  # type: ignore[attr-defined]
        if not bearer_ok(self.headers, state.token):
            return send_error_json(self, 401, UNAUTHORIZED, "auth_error")
        if self.path == "/admin/shutdown":
            instance = state.metadata.get("instance_id")
            if instance and not hmac.compare_digest(self.headers.get("X-Datamate-Instance", ""), instance):
                return send_error_json(self, 409, "service instance does not match", "instance_mismatch")
            state.stop_event.set()
            log("shutdown requested via /admin/shutdown")
            return send_json(self, 200, {"ok": True})
        if self.path != "/v1/chat/completions":
            return send_error_json(self, 404, "not found", "not_found")
        raw = read_body(self)
        body = parse_json_object(raw) if raw is not None else None
        if body is None:
            return send_error_json(self, 400, "request body must be a JSON object", "invalid_request")
        state.touch()
        try:
            status, data = state.forward_chat(raw)
        finally:
            state.touch()
        if status != 200 or not body.get("stream"):
            return send_raw(self, status, data)
        payload = parse_json_object(data)
        if payload is None or "choices" not in payload:
            return send_raw(self, 502, _error_bytes("worker returned no completion", "server_error"))
        out = sse_body(payload)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class GatewayServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_gateway(port: int, worker_ports: list[int], token: str, served_model: str,
                  stop_event: threading.Event) -> tuple[GatewayServer, GatewayState]:
    state = GatewayState(token, served_model, worker_ports, stop_event)
    httpd = GatewayServer(("127.0.0.1", port), GatewayHandler)
    httpd.state = state  # type: ignore[attr-defined]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True, name="gateway").start()
    return httpd, state


# ---------------------------------------------------------------------------------------------------------------
# Orchestrator: workers, start-up checks, gateway, supervision and shutdown.
# ---------------------------------------------------------------------------------------------------------------

@dataclass
class WorkerProc:
    gpu: int
    port: int
    proc: subprocess.Popen
    healthy: bool = False


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _add_loopback_no_proxy(env: dict[str, str]) -> None:
    """Child processes must reach 127.0.0.1 directly even when the environment sets a proxy."""
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


def warm_up(port: int, token: str, timeout: float = WORKER_WARMUP_TIMEOUT) -> bool:
    payload = {"messages": [{"role": "user", "content": WARMUP_PROMPT}], "temperature": 0.0, "max_tokens": 32}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _LOCAL_OPENER.open(req, timeout=timeout) as resp:
            return bool(json.loads(resp.read().decode("utf-8")).get("choices"))
    except Exception as exc:
        log(f"warm-up on port {port} failed: {type(exc).__name__}")
        return False


def parse_gpus(spec: str) -> list[int]:
    gpus: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            gpu = int(part)
        except ValueError as exc:
            raise ValueError(f"--gpus must be a comma-separated list of GPU indices, got {spec!r}") from exc
        if gpu < 0:
            raise ValueError("--gpus indices must be 0 or more")
        if gpu not in gpus:
            gpus.append(gpu)
    if not gpus:
        raise ValueError("--gpus names no GPU")
    return gpus


def write_secret_file(path: Path, text: str) -> None:
    """Write text to path with mode 600, replacing any existing file (its old mode is not kept)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)
    os.replace(tmp, path)


def resolve_token(token_arg: str | None, token_file: str | None) -> str:
    token = (token_arg or os.environ.get(TOKEN_ENV) or "").strip()
    if token:
        return token
    if not token_file:
        raise ValueError(f"no bearer token: pass --token, set {TOKEN_ENV}, or pass --token-file to receive a generated one")
    token = secrets.token_urlsafe(32)
    write_secret_file(Path(token_file), token)
    log(f"generated a bearer token and wrote it to {token_file} (mode 600)")
    return token


def spawn_worker(args: argparse.Namespace, gpu: int, token: str) -> WorkerProc:
    port = free_port()
    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = str(gpu)
    env[TOKEN_ENV] = token
    env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    _add_loopback_no_proxy(env)
    argv = [
        sys.executable, str(Path(__file__).resolve()), "--worker", "--worker-port", str(port),
        "--model-dir", str(args.model_dir), "--served-model-name", args.served_model_name,
        "--batch-max", str(args.batch_max), "--batch-wait-ms", str(args.batch_wait_ms),
        "--prefix-cache", args.prefix_cache, "--prefix-cache-slots", str(args.prefix_cache_slots),
        "--prefix-min-tokens", str(args.prefix_min_tokens),
    ]
    proc = subprocess.Popen(argv, env=env)
    log(f"started worker pid={proc.pid} for GPU {gpu} on 127.0.0.1:{port}")
    return WorkerProc(gpu=gpu, port=port, proc=proc)


def wait_for_workers(workers: list[WorkerProc], token: str, timeout: float, stop: threading.Event) -> list[WorkerProc]:
    deadline = time.monotonic() + timeout
    pending = list(workers)
    next_report = time.monotonic() + 60
    while pending and not stop.is_set() and time.monotonic() < deadline:
        for w in list(pending):
            rc = w.proc.poll()
            if rc is not None:
                log(f"worker for GPU {w.gpu} exited with code {rc} during start-up")
                pending.remove(w)
                continue
            if probe_models(w.port, token) is None:
                continue
            if warm_up(w.port, token):
                w.healthy = True
                log(f"worker for GPU {w.gpu} is healthy and warm")
            else:
                log(f"worker for GPU {w.gpu} failed its warm-up; it will not be used")
            pending.remove(w)
        if pending and time.monotonic() >= next_report:
            log(f"waiting for {len(pending)} worker(s) to load")
            next_report += 60
        if pending:
            stop.wait(2.0)
    if pending and not stop.is_set():
        log(f"start-up timed out for {len(pending)} worker(s)")
    return [w for w in workers if w.healthy]


def terminate_workers(workers: list[WorkerProc], grace: float = 30.0) -> None:
    for w in workers:
        if w.proc.poll() is None:
            w.proc.terminate()
    deadline = time.monotonic() + grace
    for w in workers:
        if w.proc.poll() is None:
            try:
                w.proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                w.proc.kill()
                w.proc.wait(timeout=30)


def supervise(stop: threading.Event, workers: list[WorkerProc], state: GatewayState) -> int:
    while not stop.is_set():
        stop.wait(1.0)
        live = set(state.pool.live())
        for w in workers:
            rc = w.proc.poll()
            if w.port in live and rc is not None:
                log(f"worker for GPU {w.gpu} exited with code {rc}; removed from the pool")
                state.pool.drop(w.port)
                live.discard(w.port)
        if not live:
            log("no live workers left; stopping")
            return 1
    log("stop requested; shutting down")
    return 0


def serve_main(args: argparse.Namespace) -> int:
    model_dir = Path(args.model_dir)
    if not model_dir.is_dir():
        log(f"model directory not found: {model_dir}")
        return 2
    if not (model_dir / "config.json").exists():
        log(f"warning: {model_dir} has no config.json; the Gemma class chain will be tried")
    gpus = parse_gpus(args.gpus)
    token = resolve_token(args.token, args.token_file)

    stop = threading.Event()

    def on_signal(signum, frame) -> None:
        stop.set()

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    workers: list[WorkerProc] = []
    httpd: GatewayServer | None = None
    try:
        for gpu in gpus:
            workers.append(spawn_worker(args, gpu, token))
        healthy = wait_for_workers(workers, token, float(args.startup_timeout), stop)
        if stop.is_set():
            log("stop requested during start-up")
            return 0
        if not healthy:
            log("no worker became healthy; stopping")
            return 1
        try:
            httpd, state = start_gateway(args.port, [w.port for w in healthy], token, args.served_model_name, stop)
            metadata_file = getattr(args, "metadata_file", None)
            if metadata_file:
                metadata = json.loads(Path(metadata_file).read_text(encoding="utf-8"))
                if not isinstance(metadata, dict) or any(
                    key in metadata for key in ("token", "bearer_token", "api_key", "base_url")
                ):
                    raise ValueError("metadata must contain public model information only")
                state.metadata = metadata
            if getattr(args, "activity_file", None):
                state.activity_file = Path(args.activity_file)
                state.touch()
        except OSError as exc:
            log(f"gateway could not bind 127.0.0.1:{args.port}: {exc}")
            return 1
        log(f"READY: gateway on 127.0.0.1:{args.port}, {len(healthy)} worker(s), served model {args.served_model_name}")
        return supervise(stop, workers, state)
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        terminate_workers(workers)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Local OpenAI-compatible server: one 4-bit worker per GPU behind a gateway.")
    p.add_argument("--model-dir", required=True, help="local checkpoint directory (weights, config, tokenizer)")
    p.add_argument("--port", type=int, default=8000, help="gateway port on 127.0.0.1 (default 8000)")
    p.add_argument("--gpus", default="0,1", help="comma-separated GPU indices, one worker each (default 0,1)")
    p.add_argument("--batch-max", type=int, default=6, help="greedy requests micro-batched per GPU (default 6)")
    p.add_argument("--batch-wait-ms", type=int, default=40, help="how long a batch waits for more requests (default 40)")
    p.add_argument("--prefix-cache", choices=["on", "off"], default="on", help="shared-prefix KV cache (default on)")
    p.add_argument("--prefix-cache-slots", type=int, default=2)
    p.add_argument("--prefix-min-tokens", type=int, default=512)
    p.add_argument("--token", default=None, help="bearer token (prefer DATAMATE_SERVER_TOKEN: argv is visible to ps)")
    p.add_argument("--token-file", default=None,
                   help="where a generated token is written (mode 600) when no token is given")
    p.add_argument("--served-model-name", default=None, help="model id reported by /v1/models (default: model dir name)")
    p.add_argument("--startup-timeout", type=float, default=1800.0,
                   help="seconds to wait for the workers to load and warm up (default 1800)")
    p.add_argument("--metadata-file", help="public model/instance metadata exposed behind bearer authentication")
    p.add_argument("--activity-file", help="touch this file when a generation starts/finishes, for an idle watchdog")
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--worker-port", type=int, default=0, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.served_model_name is None:
        args.served_model_name = Path(args.model_dir).resolve().name
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.worker:
        return run_worker(args)
    try:
        return serve_main(args)
    except ValueError as exc:
        log(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
