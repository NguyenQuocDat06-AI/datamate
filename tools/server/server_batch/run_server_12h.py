"""Autonomous 12-Hour Persistent LLM API Server on Kaggle GPU (2 x Tesla T4).

Serves Shaleen123/gemma4-12b-sql as an authenticated OpenAI-compatible HTTP API
(/v1/chat/completions, /v1/models) via cloudflared tunnel with live ntfy relay discovery
and an automatic watchdog daemon running up to 11 hours 50 minutes (42,600s).

Architecture: Configuration A (2 Process-Isolated 4-bit NF4 GPU Workers +
Gateway Load Balancer on 127.0.0.1:8000 + True Token-by-Token SSE Streaming).
"""

from __future__ import annotations

import os

# Prevent CUDA memory fragmentation during multiple concurrent streams
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

import json
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# ==============================================================================
# 0. CONFIGURATION & SERVER CREDENTIALS
# ==============================================================================
SERVER_BEARER_TOKEN = os.environ.get("SERVER_BEARER_TOKEN", "__SECRET_0__")
RELAY_CHANNEL = os.environ.get("RELAY_CHANNEL", "__SECRET_2__")
SERVED_MODEL_NAME = "Shaleen123/gemma4-12b-sql"
TOTAL_RUNTIME_LIMIT_S = 42600  # 11 hours 50 minutes (clean exit before 12h Kaggle kill)
CHECK_INTERVAL_S = 30
HEARTBEAT_INTERVAL_S = 180
LOCAL_PORT = 8000


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ==============================================================================
# 1. PROCESS-ISOLATED WORKER & GATEWAY SERVER TEMPLATES
# ==============================================================================
WORKER_SERVER_TEMPLATE = r'''"""Isolated 4-bit NF4 GPU Worker Server."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import json
import os
import queue
import sys
import threading
import time
import uuid
from pathlib import Path

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8001, help="Worker HTTP port")
args, _ = parser.parse_known_args()
PORT = args.port

import torch

if torch.cuda.is_available():
    torch.cuda.set_device(0)

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from transformers import AutoTokenizer, AutoProcessor, BitsAndBytesConfig, TextIteratorStreamer

model_dir = r"__MODEL_DIR__"
EXPECTED_TOKEN = "__SECRET_1__"
SERVED_MODEL_NAME = "__SERVED_MODEL_NAME__"

print(f"[Worker {PORT}] Initializing on CUDA_VISIBLE_DEVICES={os.environ.get('CUDA_VISIBLE_DEVICES', 'unset')}...", flush=True)
print(f"[Worker {PORT}] Loading tokenizer from {model_dir}...", flush=True)

try:
    processor = AutoProcessor.from_pretrained(model_dir, trust_remote_code=True)
    tokenizer = getattr(processor, "tokenizer", processor)
except Exception:
    tokenizer = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=True)

# Resolve EOS token IDs
eos_ids = [1, 106, 50]
gen_cfg_path = Path(model_dir) / "generation_config.json"
if gen_cfg_path.exists():
    try:
        with open(gen_cfg_path, "r", encoding="utf-8") as f:
            gc = json.load(f)
            eid = gc.get("eos_token_id")
            if isinstance(eid, list):
                eos_ids = [int(x) for x in eid]
            elif isinstance(eid, int):
                eos_ids = [int(eid)]
    except Exception as e:
        print(f"[Worker {PORT}] Notice reading generation_config.json: {e}", flush=True)

if hasattr(tokenizer, "eos_token_id") and tokenizer.eos_token_id is not None:
    tok_eos = tokenizer.eos_token_id
    if isinstance(tok_eos, list):
        for x in tok_eos:
            if int(x) not in eos_ids:
                eos_ids.append(int(x))
    elif int(tok_eos) not in eos_ids:
        eos_ids.append(int(tok_eos))
print(f"[Worker {PORT}] Resolved eos_ids: {eos_ids}", flush=True)

try:
    from transformers import Gemma4UnifiedForConditionalGeneration
    model_cls = Gemma4UnifiedForConditionalGeneration
except Exception:
    try:
        from transformers import AutoModelForImageTextToText
        model_cls = AutoModelForImageTextToText
    except Exception:
        from transformers import AutoModelForCausalLM
        model_cls = AutoModelForCausalLM

print(f"[Worker {PORT}] Using model class: {model_cls.__name__}", flush=True)

def _cast_bfloat16_to_fp16(m):
    for p in m.parameters():
        if p.dtype == torch.bfloat16:
            p.data = p.data.to(torch.float16)
    for b in m.buffers():
        if b.dtype == torch.bfloat16:
            b.data = b.data.to(torch.float16)
    return m

bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_compute_dtype=torch.float16,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_use_double_quant=True,
)

print(f"[Worker {PORT}] Loading 4-bit NF4 model weights on device_map={{'': 0}}...", flush=True)
load_kwargs = {
    "device_map": {"": 0} if torch.cuda.is_available() else "cpu",
    "quantization_config": bnb_config if torch.cuda.is_available() else None,
    "attn_implementation": "sdpa",
    "trust_remote_code": True,
    "torch_dtype": torch.float16,
}
try:
    model = model_cls.from_pretrained(model_dir, **load_kwargs)
except TypeError:
    load_kwargs.pop("torch_dtype", None)
    load_kwargs["dtype"] = torch.float16
    model = model_cls.from_pretrained(model_dir, **load_kwargs)

_cast_bfloat16_to_fp16(model)
model.eval()

gpu_lock = threading.Lock()

# Micro-batching config (see MicroBatcher). Batched prompts are left-padded.
BATCH_WAIT_MS = int(os.environ.get("BATCH_WAIT_MS", "40"))
BATCH_MAX = int(os.environ.get("BATCH_MAX", "8"))
# Shared-prefix KV cache (PrefixCacheStore). PREFIX_CACHE=0 turns it off entirely.
PREFIX_CACHE = os.environ.get("PREFIX_CACHE", "1") == "1"
PREFIX_CACHE_SLOTS = int(os.environ.get("PREFIX_CACHE_SLOTS", "2"))
PREFIX_MIN_TOKENS = int(os.environ.get("PREFIX_MIN_TOKENS", "512"))
try:
    tokenizer.padding_side = "left"
except Exception:
    pass
PAD_ID = tokenizer.pad_token_id if getattr(tokenizer, "pad_token_id", None) is not None else eos_ids[0]

# Startup self-test: 2-token generation test on SELECT 1
print(f"[Worker {PORT}] Running startup self-test (SELECT 1)...", flush=True)
dev = "cuda:0" if torch.cuda.is_available() else "cpu"
if torch.cuda.is_available():
    torch.cuda.set_device(0)
test_inputs = tokenizer("SELECT 1", return_tensors="pt").to(dev)
cuda_ctx = torch.cuda.device(0) if torch.cuda.is_available() else contextlib.nullcontext()
with gpu_lock, cuda_ctx, torch.inference_mode():
    try:
        model.generate(**test_inputs, max_new_tokens=2, logits_to_keep=1)
    except (TypeError, ValueError):
        model.generate(**test_inputs, max_new_tokens=2)
del test_inputs
if torch.cuda.is_available():
    torch.cuda.empty_cache()
print(f"[Worker {PORT}] Startup self-test passed successfully!", flush=True)

app = FastAPI()

def is_authorized(req: Request) -> bool:
    auth = req.headers.get("authorization", "")
    parts = auth.strip().split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1] == EXPECTED_TOKEN
    return False

# ---- BATCH-CORE-BEGIN (pure Python, no torch; exec'd by test_batching.py) ----
def use_batch_path(stream, temperature, batch_max):
    """Only non-streaming greedy requests are micro-batched, and only when BATCH_MAX > 1."""
    return (not stream) and (not temperature > 0) and batch_max > 1

def left_pad(seqs, pad_id):
    width = max(len(s) for s in seqs)
    ids = [[pad_id] * (width - len(s)) + list(s) for s in seqs]
    mask = [[0] * (width - len(s)) + [1] * len(s) for s in seqs]
    return ids, mask, width

def cut_new_tokens(row, max_tokens, eos_ids):
    """Keep new tokens up to and including the first eos, and at most max_tokens (as un-batched output does)."""
    limit = min(len(row), max(int(max_tokens), 0))
    for i in range(limit):
        if row[i] in eos_ids:
            return list(row[: i + 1])
    return list(row[:limit])

def is_oom_error(exc):
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
        self._generate_batch = generate_batch  # (id lists, max_new_tokens, boundaries) -> list of raw new-token rows
        self._lock = lock
        self._eos_ids = set(int(x) for x in eos_ids)
        self._max_batch = max(1, int(max_batch))
        self._wait_s = max(0, int(wait_ms)) / 1000.0
        self._on_oom = on_oom
        self._log = log or (lambda msg: None)
        self._q = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="micro-batcher")

    def start(self):
        self._thread.start()

    def submit(self, ids, max_tokens, boundary=None):
        """Blocking. Returns (new_tokens, input_len, n_tokens) like run_generation."""
        item = _BatchItem(ids, max_tokens, boundary)
        self._q.put(item)
        item.done.wait()
        if item.error is not None:
            raise item.error
        return item.result

    def _loop(self):
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
            except Exception as exc:  # defensive: the GPU thread must never die
                for it in batch:
                    if not it.done.is_set():
                        it.error = exc
                        it.done.set()

    def _run_group(self, items):
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

def common_prefix_len(a, b):
    """Length of the common leading run of two token-id sequences."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i

class PrefixCacheStore:
    """Small LRU of shared-prefix KV entries (one store per worker process).

    Bookkeeping only: the model work is injected. make_entry(prefix_ids) runs the prefix forward
    pass and returns an opaque entry; on_free(entry) releases one. An entry is only handed out for a
    prompt whose token ids start with exactly the entry's ids, so the output never depends on what
    else is cached. Any failure while creating an entry disables the store for the process.
    """
    def __init__(self, make_entry, slots=2, min_tokens=512, on_free=None, log=None):
        self._make = make_entry
        self._slots = max(1, int(slots))
        self._min = max(1, int(min_tokens))
        self._on_free = on_free or (lambda entry: None)
        self._log = log or (lambda msg: None)
        self._entries = []      # [(prefix_ids tuple, entry)], least recently used first
        self._last_ids = None   # previous request's ids (fallback boundary source)
        self.enabled = True
        self.hits = 0
        self.created = 0
        self.none = 0
        self.evicted = 0
        self.skipped_tokens = 0

    def disable(self, reason):
        self.enabled = False
        self.clear()
        self._log(f"prefix cache disabled: {reason}")

    def clear(self):
        entries, self._entries = self._entries, []
        self._last_ids = None
        for _, entry in entries:
            self._on_free(entry)

    def _find(self, ids):
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
        """Returns (entry, reused_tokens). entry is None when this request runs without the cache.
        reused_tokens > 0 only when an entry created earlier was reused (a creation request reuses 0)."""
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

def prefix_generate_rows(store, ops, seqs, boundaries=None, max_new_tokens=0, log=None):
    """Raw new-token rows (aligned with seqs) for one greedy batch, reusing shared-prefix KV where possible.

    ops.plain_batch(seqs, max_new) -> list of new-token lists, no cache (requests without an entry).
    ops.single(ids, max_new, entry) -> new-token list (entry None = no cache).
    ops.batch_prefix(seqs, max_new, entry) -> list of new-token lists, or None if the cache layout
    cannot be batched (then the requests of that entry run one by one).
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
    groups = {}
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

def prefix_self_check(ops, seqs, boundary, eos_ids, max_new=8):
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
# ---- BATCH-CORE-END ----

def run_generation(prompt_text, max_tokens, temperature, streamer=None):
    dev = "cuda:0" if torch.cuda.is_available() else "cpu"
    if torch.cuda.is_available():
        torch.cuda.set_device(0)
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
    if streamer is not None:
        gen_kwargs["streamer"] = streamer

    cuda_ctx = torch.cuda.device(0) if torch.cuda.is_available() else contextlib.nullcontext()
    try:
        with gpu_lock, cuda_ctx, torch.inference_mode():
            try:
                outputs = model.generate(**inputs, logits_to_keep=1, **gen_kwargs)
            except (TypeError, ValueError):
                outputs = model.generate(**inputs, **gen_kwargs)
        if streamer is not None:
            return None, input_len, 0
        new_tokens = outputs[0][input_len:]
        return new_tokens, input_len, len(new_tokens)
    except (RuntimeError, torch.cuda.OutOfMemoryError) as exc:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        raise exc
    finally:
        del inputs

def _plain_generate_batch(seqs, max_new_tokens):
    """One greedy generate over a left-padded batch; returns raw new-token rows (caller cuts them)."""
    dev = "cuda:0" if torch.cuda.is_available() else "cpu"
    if torch.cuda.is_available():
        torch.cuda.set_device(0)
    ids, mask, width = left_pad(seqs, PAD_ID)
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
    cuda_ctx = torch.cuda.device(0) if torch.cuda.is_available() else contextlib.nullcontext()
    with cuda_ctx, torch.inference_mode():
        try:
            outputs = model.generate(input_ids=input_ids, attention_mask=attention_mask, logits_to_keep=1, **gen_kwargs)
        except (TypeError, ValueError):
            outputs = model.generate(input_ids=input_ids, attention_mask=attention_mask, **gen_kwargs)
    return [row[width:].tolist() for row in outputs]

def _empty_cuda_cache():
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

def _cuda_ctx():
    return torch.cuda.device(0) if torch.cuda.is_available() else contextlib.nullcontext()

# ---- PREFIX-TORCH-BEGIN (torch/transformers ops for the prefix cache; exec'd by test_prefix_cache.py) ----
class PrefixEntry:
    """Prefix token ids plus the KV cache computed for them (never mutated after creation)."""
    def __init__(self, ids, cache):
        self.ids = tuple(int(x) for x in ids)
        self.cache = cache

class PrefixOps:
    def __init__(self, model, eos_ids, pad_id, device, ctx_factory, plain_batch):
        self.model = model
        self.eos_ids = eos_ids
        self.pad_id = int(pad_id)
        self.device = device
        self.ctx = ctx_factory
        self.plain_batch = plain_batch  # (seqs, max_new) -> rows, the existing left-padded path

    def _check_entry(self, ids, entry):
        n = len(entry.ids)
        if len(ids) <= n or tuple(int(x) for x in ids[:n]) != entry.ids:
            raise ValueError("prefix cache: request does not start with the cached prefix")

    def _generate(self, input_ids, attention_mask, max_new, cache=None):
        kw = {
            "max_new_tokens": int(max_new),
            "temperature": None,
            "do_sample": False,
            "eos_token_id": self.eos_ids,
            "pad_token_id": 0,
            "use_cache": True,
        }
        if cache is not None:
            kw["past_key_values"] = cache
        with self.ctx(), torch.inference_mode():
            try:
                return self.model.generate(input_ids=input_ids, attention_mask=attention_mask, logits_to_keep=1, **kw)
            except (TypeError, ValueError):
                return self.model.generate(input_ids=input_ids, attention_mask=attention_mask, **kw)

    def make_entry(self, prefix_ids):
        ids = torch.tensor([list(prefix_ids)], dtype=torch.long, device=self.device)
        with self.ctx(), torch.inference_mode():
            try:
                out = self.model(input_ids=ids, use_cache=True, logits_to_keep=1)
            except TypeError:
                out = self.model(input_ids=ids, use_cache=True)
        return PrefixEntry(prefix_ids, out.past_key_values)

    def single(self, ids, max_new, entry):
        input_ids = torch.tensor([list(ids)], dtype=torch.long, device=self.device)
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
            ids.append(list(entry.ids) + [self.pad_id] * pad + s)
            mask.append([1] * n + [0] * pad + [1] * len(s))
        input_ids = torch.tensor(ids, dtype=torch.long, device=self.device)
        attn = torch.tensor(mask, dtype=torch.long, device=self.device)
        cache = copy.deepcopy(entry.cache)
        cache.batch_repeat_interleave(len(seqs))
        out = self._generate(input_ids, attn, max_new, cache)
        width = n + w
        return [row[width:].tolist() for row in out]
# ---- PREFIX-TORCH-END ----

prefix_ops = PrefixOps(model, eos_ids, PAD_ID, dev, _cuda_ctx, _plain_generate_batch)
_prefix_log = lambda msg: print(f"[Worker {PORT}] {msg}", flush=True)
prefix_store = PrefixCacheStore(
    prefix_ops.make_entry, PREFIX_CACHE_SLOTS, PREFIX_MIN_TOKENS,
    on_free=lambda entry: _empty_cuda_cache(), log=_prefix_log,
)

def generate_batch(seqs, max_new_tokens, boundaries=None):
    """Greedy rows for a micro-batch (called under gpu_lock). Uses the prefix cache when it is enabled."""
    if not prefix_store.enabled:
        return _plain_generate_batch(seqs, max_new_tokens)
    return prefix_generate_rows(prefix_store, prefix_ops, seqs, boundaries, max_new_tokens, log=_prefix_log)

_probe_memo = {}

def prefix_boundary(messages, ids):
    """Token index where the system part of the prompt ends, from rendering the template with the
    system messages and an empty user turn. None when it cannot be determined."""
    sys_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]
    if not sys_msgs or not hasattr(tokenizer, "apply_chat_template"):
        return None
    key = json.dumps(sys_msgs, sort_keys=True, default=str)
    probe = _probe_memo.get(key)
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
        if len(_probe_memo) < 16:
            _probe_memo[key] = probe
    return common_prefix_len(probe, ids)

def _model_sliding_window():
    for cfg in (getattr(model, "config", None), getattr(getattr(model, "config", None), "text_config", None)):
        w = getattr(cfg, "sliding_window", None)
        if w:
            return int(w)
    return 0

def _build_prefix_selfcheck():
    """Two synthetic prompts sharing a prefix longer than any sliding window, so windowed layers are exercised."""
    target = max(200, _model_sliding_window() + 64)
    shared = "You are a careful SQL assistant. Answer with one query. "
    while len(tokenizer(shared)["input_ids"]) < target:
        shared = shared * 2
    a = list(tokenizer(shared + "Question: count the rows of table one.")["input_ids"])
    b = list(tokenizer(shared + "Question: list the names in table two.")["input_ids"])
    return a, b, common_prefix_len(a, b)

def _run_prefix_self_check():
    try:
        a, b, n = _build_prefix_selfcheck()
    except Exception as exc:
        prefix_store.disable(f"self-check prompts failed ({type(exc).__name__})")
        return
    with gpu_lock:
        reason = prefix_self_check(prefix_ops, [a, b], n, eos_ids)
    _empty_cuda_cache()
    if reason is None:
        print(f"[Worker {PORT}] Prefix cache self-check passed (prefix_tokens={n})", flush=True)
    else:
        prefix_store.disable(reason)

batcher = MicroBatcher(
    generate_batch, gpu_lock, eos_ids, BATCH_MAX, BATCH_WAIT_MS,
    on_oom=_empty_cuda_cache,
    log=lambda msg: print(f"[Worker {PORT}] {msg}", flush=True),
)
if BATCH_MAX > 1:
    batcher.start()
    print(f"[Worker {PORT}] Micro-batching on: max={BATCH_MAX} wait_ms={BATCH_WAIT_MS} pad_id={PAD_ID}", flush=True)

if PREFIX_CACHE:
    _run_prefix_self_check()
else:
    prefix_store.disable("PREFIX_CACHE=0")

@app.get("/v1/models")
def get_models(req: Request):
    if not is_authorized(req):
        return JSONResponse(status_code=401, content={"error": {"message": "Unauthorized: Invalid or missing Bearer token", "type": "auth_error"}})
    return {"object": "list", "data": [{"id": SERVED_MODEL_NAME, "object": "model"}]}

@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    if not is_authorized(req):
        return JSONResponse(status_code=401, content={"error": {"message": "Unauthorized: Invalid or missing Bearer token", "type": "auth_error"}})

    body = await req.json()
    messages = body.get("messages", [])
    max_tokens = int(body.get("max_tokens", 4096))
    temperature = float(body.get("temperature", 0.0))
    stream = bool(body.get("stream", False))

    if hasattr(tokenizer, "apply_chat_template"):
        try:
            prompt_text = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True, enable_thinking=False
            )
        except Exception:
            try:
                prompt_text = tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True
                )
            except Exception:
                prompt_text = "\n".join([f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages]) + "\nassistant:\n"
    else:
        prompt_text = "\n".join([f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages]) + "\nassistant:\n"

    cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if stream:
        def sse_gen():
            initial_chunk = {
                "id": cmpl_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": SERVED_MODEL_NAME,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(initial_chunk)}\n\n"

            streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True, timeout=120.0)
            gen_thread = None
            gen_error = []

            def _worker():
                if torch.cuda.is_available():
                    torch.cuda.set_device(0)
                try:
                    run_generation(prompt_text, max_tokens, temperature, streamer=streamer)
                except Exception as eg:
                    import traceback
                    traceback.print_exc()
                    gen_error.append(eg)
                finally:
                    try:
                        streamer.end()
                    except Exception:
                        pass

            try:
                gen_thread = threading.Thread(target=_worker, daemon=True)
                gen_thread.start()

                try:
                    for piece in streamer:
                        if piece:
                            chunk = {
                                "id": cmpl_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": SERVED_MODEL_NAME,
                                "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                except queue.Empty:
                    pass

                if gen_error:
                    err_chunk = {
                        "error": {
                            "message": str(gen_error[0]),
                            "code": 500,
                            "type": type(gen_error[0]).__name__,
                        }
                    }
                    yield f"data: {json.dumps(err_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                stop_chunk = {
                    "id": cmpl_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": SERVED_MODEL_NAME,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(stop_chunk)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                if gen_thread is not None:
                    gen_thread.join(timeout=5)

        return StreamingResponse(sse_gen(), media_type="text/event-stream")
    else:
        def _sync_generate():
            if use_batch_path(stream, temperature, BATCH_MAX):
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
                new_tokens, in_len, out_len = run_generation(
                    prompt_text, max_tokens, temperature, streamer=None
                )
            generated_text = tokenizer.decode(new_tokens, skip_special_tokens=True)
            return generated_text, in_len, out_len

        try:
            generated_text, in_len, out_len = await asyncio.to_thread(_sync_generate)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            return JSONResponse(
                status_code=500,
                content={"error": {"message": f"{type(exc).__name__}: {exc}", "type": "server_error"}},
            )

        return JSONResponse({
            "id": cmpl_id,
            "object": "chat.completion",
            "created": created,
            "model": SERVED_MODEL_NAME,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": generated_text},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": int(in_len),
                "completion_tokens": int(out_len),
                "total_tokens": int(in_len + out_len),
            },
        })

if __name__ == "__main__":
    print(f"[Worker {PORT}] Starting uvicorn on 127.0.0.1:{PORT}...", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
'''


GATEWAY_SERVER_TEMPLATE = r'''"""Lightweight Load-Balancing Gateway Server for Isolated GPU Workers."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8000, help="Gateway port (default 8000)")
parser.add_argument("--worker-ports", type=str, required=True, help="Comma-separated list of worker ports")
args, _ = parser.parse_known_args()

GATEWAY_PORT = args.port
ports_raw = [p.strip() for p in args.worker_ports.split(",") if p.strip()]
worker_ports = [int(p) for p in ports_raw]

EXPECTED_TOKEN = "__SECRET_1__"
SERVED_MODEL_NAME = "__SERVED_MODEL_NAME__"

active_counts = {p: 0 for p in worker_ports}
rr_lock = threading.Lock()
rr_counter = 0

def acquire_worker_port() -> int:
    global rr_counter
    with rr_lock:
        best_port = min(
            worker_ports,
            key=lambda p: (active_counts.get(p, 0), (worker_ports.index(p) - rr_counter) % len(worker_ports)),
        )
        active_counts[best_port] = active_counts.get(best_port, 0) + 1
        rr_counter = (rr_counter + 1) % len(worker_ports)
        return best_port

def release_worker_port(p: int) -> None:
    with rr_lock:
        active_counts[p] = max(0, active_counts.get(p, 1) - 1)

print(f"[Gateway] Initialized on 127.0.0.1:{GATEWAY_PORT} with {len(worker_ports)} worker(s): {worker_ports}", flush=True)

app = FastAPI()

def is_authorized(req: Request) -> bool:
    auth = req.headers.get("authorization", "")
    parts = auth.strip().split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1] == EXPECTED_TOKEN
    return False

@app.get("/v1/models")
def get_models(req: Request):
    if not is_authorized(req):
        return JSONResponse(status_code=401, content={"error": {"message": "Unauthorized: Invalid or missing Bearer token", "type": "auth_error"}})
    return {"object": "list", "data": [{"id": SERVED_MODEL_NAME, "object": "model"}]}

@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    if not is_authorized(req):
        return JSONResponse(status_code=401, content={"error": {"message": "Unauthorized: Invalid or missing Bearer token", "type": "auth_error"}})

    raw_body = await req.body()
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except Exception:
        body = {}
    stream = bool(body.get("stream", False))

    fwd_headers = {
        "Authorization": f"Bearer {EXPECTED_TOKEN}",
        "Content-Type": "application/json",
    }

    if stream:
        def sse_proxy():
            wport = acquire_worker_port()
            try:
                target_url = f"http://127.0.0.1:{wport}/v1/chat/completions"
                req_fwd = urllib.request.Request(target_url, data=raw_body, headers=fwd_headers, method="POST")
                try:
                    with urllib.request.urlopen(req_fwd, timeout=240) as resp:
                        for line in resp:
                            if line:
                                yield line.decode("utf-8")
                except urllib.error.HTTPError as he:
                    err_text = he.read().decode("utf-8", errors="replace")
                    err_chunk = {
                        "error": {
                            "message": f"Worker {wport} HTTP {he.code}: {err_text}",
                            "code": he.code,
                            "type": "worker_error",
                        }
                    }
                    yield f"data: {json.dumps(err_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception as exc:
                    err_chunk = {
                        "error": {
                            "message": f"Gateway stream error forward to worker {wport}: {exc}",
                            "code": 500,
                            "type": type(exc).__name__,
                        }
                    }
                    yield f"data: {json.dumps(err_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
            finally:
                release_worker_port(wport)

        return StreamingResponse(sse_proxy(), media_type="text/event-stream")
    else:
        def _forward_sync():
            wport = acquire_worker_port()
            try:
                target_url = f"http://127.0.0.1:{wport}/v1/chat/completions"
                req_fwd = urllib.request.Request(target_url, data=raw_body, headers=fwd_headers, method="POST")
                with urllib.request.urlopen(req_fwd, timeout=240) as resp:
                    resp_bytes = resp.read()
                    return JSONResponse(status_code=resp.status, content=json.loads(resp_bytes.decode("utf-8")))
            except urllib.error.HTTPError as he:
                err_body = he.read().decode("utf-8", errors="replace")
                try:
                    err_json = json.loads(err_body)
                except Exception:
                    err_json = {"error": {"message": err_body, "code": he.code}}
                return JSONResponse(status_code=he.code, content=err_json)
            except Exception as exc:
                return JSONResponse(
                    status_code=500,
                    content={"error": {"message": f"Worker {wport} error: {exc}", "type": type(exc).__name__}},
                )
            finally:
                release_worker_port(wport)

        return await asyncio.to_thread(_forward_sync)

if __name__ == "__main__":
    print(f"[Gateway] Starting uvicorn on 127.0.0.1:{GATEWAY_PORT}...", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=GATEWAY_PORT, log_level="warning")
'''


# ==============================================================================
# 2. HEALTH CHECK & NETWORKING UTILITIES
# ==============================================================================
def check_server_health(port: int = LOCAL_PORT, token: str = SERVER_BEARER_TOKEN) -> bool:
    url = f"http://127.0.0.1:{port}/v1/models"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "Kaggle12hHealthCheck/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception:
        return False


def wait_for_server(proc: subprocess.Popen, timeout: int = 300, port: int = LOCAL_PORT, token: str = SERVER_BEARER_TOKEN) -> bool:
    start_t = time.time()
    while time.time() - start_t < timeout:
        if proc.poll() is not None:
            log(f"Server on port {port} process exited early with return code {proc.poll()}")
            return False
        if check_server_health(port=port, token=token):
            return True
        time.sleep(3)
    log(f"Server on port {port} startup timed out after {timeout} seconds")
    return False


def test_warmup(port: int, token: str = SERVER_BEARER_TOKEN, timeout: int = 60) -> bool:
    warmup_payload = {
        "model": SERVED_MODEL_NAME,
        "messages": [{"role": "user", "content": "SELECT 1;"}],
        "temperature": 0.0,
        "max_tokens": 32,
    }
    t_start = time.time()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=json.dumps(warmup_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        dur = time.time() - t_start
        snippet = data.get("choices", [{}])[0].get("message", {}).get("content", "")[:80]
        log(f"Warm-up on port {port} succeeded in {dur:.2f}s | Sample: {repr(snippet)}")
        return True
    except urllib.error.HTTPError as he:
        err_body = ""
        try:
            err_body = he.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        log(f"Warm-up on port {port} HTTP {he.code}: {err_body}")
        return False
    except Exception as e:
        log(f"Warm-up on port {port} failed: {e}")
        return False


def download_cloudflared(dest_dir: Path) -> Path:
    target = dest_dir / "cloudflared"
    if target.exists() and os.access(str(target), os.X_OK):
        return target
    url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
    log(f"Downloading cloudflared binary from {url}...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(target, "wb") as out:
        shutil.copyfileobj(resp, out)
    os.chmod(str(target), 0o755)
    log(f"cloudflared ready at {target} ({target.stat().st_size / (1024*1024):.1f} MB)")
    return target


def start_cloudflared_tunnel(bin_path: Path, port: int = LOCAL_PORT, timeout: int = 60) -> tuple[subprocess.Popen, str | None]:
    cmd = [
        str(bin_path),
        "tunnel",
        "--url",
        f"http://127.0.0.1:{port}",
        "--no-autoupdate",
    ]
    log(f"Starting cloudflared tunnel on port {port}...")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    captured_url: list[str] = []
    found_event = threading.Event()

    def reader():
        if proc.stderr is None:
            return
        for raw_line in proc.stderr:
            line = raw_line.strip()
            if not line:
                continue
            if "trycloudflare.com" in line:
                m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                if m:
                    url = m.group(0)
                    if not captured_url:
                        captured_url.append(url)
                        found_event.set()
                        log(f"[cloudflared] Tunnel URL detected: {url}")
            if "Registered tunnel connection" in line or "error" in line.lower():
                log(f"[cloudflared] {line}")

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    found_event.wait(timeout=timeout)
    url = captured_url[0] if captured_url else None
    return proc, url


def verify_tunnel_reachability(tunnel_url: str, token: str = SERVER_BEARER_TOKEN, timeout: int = 60) -> bool:
    t0 = time.time()
    url = f"{tunnel_url}/v1/models"
    log(f"Verifying tunnel reachability on {url}...")
    while time.time() - t0 < timeout:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "TunnelReachabilityCheck/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    log(f"Tunnel is REACHABLE and 200 OK! ({time.time() - t0:.1f}s)")
                    return True
        except Exception:
            time.sleep(2)
    log(f"Tunnel reachability timed out after {timeout}s.")
    return False


def publish_relay(channel: str, payload: dict, title: str = "Gemma4 Server", tags: str = "gpu") -> bool:
    ntfy_url = f"https://ntfy.sh/{channel}"
    data = json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request(
            ntfy_url,
            data=data,
            headers={
                "Title": title,
                "Tags": tags,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        log(f"Warning: Failed to publish to ntfy.sh/{channel}: {e}")
        return False


def save_endpoint_info(base_url: str, tier: int | str | None, num_gpus: int, start_time: float) -> None:
    endpoint_data = {
        "base_url": base_url,
        "model": SERVED_MODEL_NAME,
        "tier": tier,
        "gpu_count": num_gpus,
        "started_at": start_time,
        "updated_at": time.time(),
    }
    working_dir = Path("/kaggle/working")
    working_dir.mkdir(parents=True, exist_ok=True)
    (working_dir / "endpoint_info.json").write_text(json.dumps(endpoint_data, indent=2), encoding="utf-8")


# ==============================================================================
# 3. MAIN AUTONOMOUS LIFECYCLE
# ==============================================================================
def main():
    t_global_start = time.time()
    log("======================================================================")
    log("  KAGGLE 12-HOUR PERSISTENT GPU API SERVER (T4 x 2, 32 GB VRAM)")
    log("  Configuration A: Process-Isolated 4-bit NF4 Multi-GPU + Gateway")
    log(f"  Target Model:   {SERVED_MODEL_NAME}")
    log(f"  Relay Channel:  https://ntfy.sh/{RELAY_CHANNEL}")
    log(f"  Max Lifetime:   {TOTAL_RUNTIME_LIMIT_S}s (11h 50m)")
    log("======================================================================")

    # ==============================================================================
    # A. TELEMETRY & DISK SELECTION
    # ==============================================================================
    log("=== Step A: System Telemetry & Disk Selection ===")

    try:
        smi = subprocess.run(["nvidia-smi"], capture_output=True, text=True, check=False)
        log("nvidia-smi output:\n" + (smi.stdout or smi.stderr or "No output"))
    except Exception as e:
        log(f"Failed to run nvidia-smi: {e}")

    try:
        import torch
        gpu_count = torch.cuda.device_count()
        log(f"PyTorch CUDA available: {torch.cuda.is_available()} | GPU count: {gpu_count}")
        for i in range(gpu_count):
            props = torch.cuda.get_device_properties(i)
            vram_gb = props.total_memory / (1024 ** 3)
            log(f"  GPU {i}: {props.name} | Total VRAM: {vram_gb:.2f} GB")
    except Exception as e:
        log(f"PyTorch CUDA check: {e}")
        gpu_count = 1

    if gpu_count < 1:
        gpu_count = 1

    disk_candidates = ["/tmp", "/kaggle/tmp", "/root/.cache", "/kaggle/working", "."]
    best_disk = None
    max_free = -1

    for candidate in disk_candidates:
        p = Path(candidate)
        try:
            p.mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(str(p))
            free_gb = usage.free / (1024 ** 3)
            log(f"Disk candidate: {candidate} | Free: {free_gb:.2f} GB")
            if usage.free > max_free:
                max_free = usage.free
                best_disk = p
        except Exception as e:
            log(f"Could not inspect {candidate}: {e}")

    if best_disk is None:
        best_disk = Path("/tmp")
        best_disk.mkdir(parents=True, exist_ok=True)

    log(f"Selected best workspace disk: {best_disk} (Free: {max_free / (1024**3):.2f} GB)")

    hf_cache = best_disk / "hf_cache"
    work_runtime = best_disk / "work_runtime"
    hf_cache.mkdir(parents=True, exist_ok=True)
    work_runtime.mkdir(parents=True, exist_ok=True)

    os.environ["HF_HOME"] = str(hf_cache)
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(hf_cache)
    os.environ["TMPDIR"] = str(best_disk)
    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"

    # ==============================================================================
    # B. PRE-DOWNLOAD MODEL WEIGHTS & INSTALL RUNTIME
    # ==============================================================================
    log("=== Step B: Install Runtime & Download Model Weights ===")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-q",
            "-U",
            "huggingface_hub",
            "hf_transfer",
            "transformers>=5.16.0",
            "accelerate",
            "bitsandbytes",
            "fastapi",
            "uvicorn",
            "sentencepiece",
            "jinja2",
        ],
        check=False,
    )

    from huggingface_hub import snapshot_download

    model_dir = hf_cache / "gemma4-12b-sql"
    model_dir.mkdir(parents=True, exist_ok=True)

    log(f"Downloading {SERVED_MODEL_NAME} to {model_dir}...")
    t_dl_start = time.time()
    snapshot_download(
        repo_id=SERVED_MODEL_NAME,
        local_dir=str(model_dir),
        local_dir_use_symlinks=False,
    )
    dl_duration = time.time() - t_dl_start
    log(f"Model download completed in {dl_duration:.2f}s!")

    # ==============================================================================
    # C. PROCESS-ISOLATED 4-BIT NF4 DUAL GPU WORKERS + GATEWAY ON 127.0.0.1:8000
    # ==============================================================================
    log("=== Step C: Process-Isolated 4-Bit NF4 Multi-GPU Deployment ===")
    worker_server_path = work_runtime / "worker_server.py"
    worker_server_code = (
        WORKER_SERVER_TEMPLATE
        .replace("__MODEL_DIR__", str(model_dir))
        .replace("__SECRET_1__", SERVER_BEARER_TOKEN)
        .replace("__SERVED_MODEL_NAME__", SERVED_MODEL_NAME)
    )
    worker_server_path.write_text(worker_server_code, encoding="utf-8")

    gateway_server_path = work_runtime / "gateway_server.py"
    gateway_server_code = (
        GATEWAY_SERVER_TEMPLATE
        .replace("__SECRET_1__", SERVER_BEARER_TOKEN)
        .replace("__SERVED_MODEL_NAME__", SERVED_MODEL_NAME)
    )
    gateway_server_path.write_text(gateway_server_code, encoding="utf-8")

    # Determine worker ports to launch based on available GPUs
    ports_to_launch = [8001, 8002] if gpu_count >= 2 else [8001]
    worker_procs: dict[int, tuple[subprocess.Popen, int, Path]] = {}

    log(f"Launching {len(ports_to_launch)} worker process(es) IN PARALLEL on ports {ports_to_launch}...")
    for idx, port in enumerate(ports_to_launch):
        w_env = os.environ.copy()
        w_env["CUDA_VISIBLE_DEVICES"] = str(idx)
        w_env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
        w_log_path = Path(f"/kaggle/working/worker_{port}.log")
        w_log_f = open(w_log_path, "w", encoding="utf-8")
        p = subprocess.Popen(
            [sys.executable, str(worker_server_path), "--port", str(port)],
            env=w_env,
            stdout=w_log_f,
            stderr=subprocess.STDOUT,
        )
        worker_procs[port] = (p, idx, w_log_path)
        log(f"Worker {idx} launched (PID {p.pid}) on port {port} with CUDA_VISIBLE_DEVICES='{idx}'.")

    # Wait for all workers and test direct warm-up
    verified_worker_ports: list[int] = []
    for port, (p, idx, log_path) in worker_procs.items():
        log(f"Waiting for worker on port {port} (GPU {idx}) to become healthy...")
        if wait_for_server(p, timeout=300, port=port, token=SERVER_BEARER_TOKEN):
            log(f"Worker on port {port} is healthy! Running direct warm-up SELECT 1; ...")
            if test_warmup(port=port, token=SERVER_BEARER_TOKEN, timeout=90):
                verified_worker_ports.append(port)
                log(f"Worker on port {port} (GPU {idx}) verified and warm!")
            else:
                log(f"WARNING: Worker on port {port} failed warm-up inference!")
        else:
            log(f"ERROR: Worker on port {port} startup health check timed out!")
            if log_path.exists():
                try:
                    tail = "".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-50:])
                    log(f"--- worker_{port}.log tail ---\n{tail}\n--- end log tail ---")
                except Exception as le:
                    log(f"Could not read {log_path}: {le}")

    if not verified_worker_ports:
        raise RuntimeError("No worker servers passed health checks and warm-up!")

    log(f"Verified healthy worker ports: {verified_worker_ports}")

    # Launch Gateway Server on port 8000
    gateway_ports_arg = ",".join(str(p) for p in verified_worker_ports)
    log(f"Starting Gateway Server on port {LOCAL_PORT} load balancing over workers: {gateway_ports_arg}...")
    log_gw_path = Path("/kaggle/working/gateway_server.log")
    log_gw = open(log_gw_path, "w", encoding="utf-8")
    gateway_proc = subprocess.Popen(
        [
            sys.executable,
            str(gateway_server_path),
            "--port",
            str(LOCAL_PORT),
            "--worker-ports",
            gateway_ports_arg,
        ],
        stdout=log_gw,
        stderr=subprocess.STDOUT,
    )

    if not wait_for_server(gateway_proc, timeout=60, port=LOCAL_PORT, token=SERVER_BEARER_TOKEN):
        if log_gw_path.exists():
            tail = log_gw_path.read_text(encoding="utf-8", errors="replace")
            log(f"--- gateway_server.log ---\n{tail}\n--- end ---")
        raise RuntimeError("Gateway server failed to start on port 8000!")

    log("Gateway server is online on 127.0.0.1:8000! Running warm-up via Gateway...")
    if not test_warmup(port=LOCAL_PORT, token=SERVER_BEARER_TOKEN, timeout=90):
        raise RuntimeError("Warm-up inference failed through gateway port 8000!")

    active_tier = "nf4_dp"
    log(f"Active Model Server Tier: {active_tier} (Process-Isolated 4-bit NF4 Data-Parallel, {len(verified_worker_ports)} workers)")

    # ==============================================================================
    # D. CLOUDFLARED TUNNEL & RELAY DISCOVERY
    # ==============================================================================
    log("=== Step D: cloudflared Tunnel & Relay Discovery ===")
    cloudflared_bin = download_cloudflared(best_disk)

    # Start initial cloudflared tunnel
    cloudflared_proc, current_tunnel_url = start_cloudflared_tunnel(cloudflared_bin)
    if not current_tunnel_url:
        log("Warning: Failed to capture tunnel URL on first attempt. Retrying...")
        try:
            cloudflared_proc.terminate()
        except Exception:
            pass
        cloudflared_proc, current_tunnel_url = start_cloudflared_tunnel(cloudflared_bin, timeout=90)

    if not current_tunnel_url:
        raise RuntimeError("Could not establish cloudflared tunnel URL!")

    # Verify reachability
    verify_tunnel_reachability(current_tunnel_url, SERVER_BEARER_TOKEN, timeout=60)

    # Save initial endpoint info
    current_base_url = f"{current_tunnel_url}/v1"
    save_endpoint_info(current_base_url, active_tier, gpu_count, t_global_start)

    # Publish initial announcement
    initial_announcement = {
        "status": "online",
        "base_url": current_base_url,
        "model": SERVED_MODEL_NAME,
        "tier": active_tier,
        "gpu_count": gpu_count,
        "uptime_s": int(time.time() - t_global_start),
    }
    publish_relay(
        RELAY_CHANNEL,
        initial_announcement,
        title="Kaggle Gemma4 12B API Server Online",
        tags="rocket,gpu,sparkles",
    )
    log(f"Published online announcement to https://ntfy.sh/{RELAY_CHANNEL} with base_url: {current_base_url}")

    # ==============================================================================
    # E. 11h 50m (42,600s) PERSISTENT WATCHDOG DAEMON LOOP
    # ==============================================================================
    log("=== Step E: Entering 12-Hour Watchdog Daemon Loop ===")
    last_heartbeat_time = time.time()
    gateway_unhealthy_count = 0
    worker_unhealthy_counts = {wport: 0 for wport in verified_worker_ports}

    while True:
        time.sleep(CHECK_INTERVAL_S)
        now = time.time()
        uptime = now - t_global_start

        # Check 11h 50m limit
        if uptime >= TOTAL_RUNTIME_LIMIT_S:
            log(f"Watchdog: Reached 11h 50m ({uptime:.0f}s). Initiating clean shutdown before Kaggle hard 12h kill.")
            break

        # 1. Cloudflared Watchdog
        if cloudflared_proc.poll() is not None:
            log(f"Watchdog: cloudflared died (code {cloudflared_proc.poll()})! Restarting tunnel...")
            cloudflared_proc, new_url = start_cloudflared_tunnel(cloudflared_bin)
            if new_url:
                current_tunnel_url = new_url
                current_base_url = f"{current_tunnel_url}/v1"
                verify_tunnel_reachability(current_tunnel_url, SERVER_BEARER_TOKEN, timeout=60)
                save_endpoint_info(current_base_url, active_tier, gpu_count, t_global_start)
                publish_relay(
                    RELAY_CHANNEL,
                    {
                        "status": "online",
                        "base_url": current_base_url,
                        "model": SERVED_MODEL_NAME,
                        "tier": active_tier,
                        "gpu_count": gpu_count,
                        "uptime_s": int(uptime),
                    },
                    title="Tunnel Restarted",
                    tags="recycle,link",
                )
                log(f"Watchdog: Tunnel recovered: {current_base_url}")

        # 2. Worker Watchdog
        for wport in list(verified_worker_ports):
            p, idx, w_log_path = worker_procs[wport]
            w_dead = p.poll() is not None
            w_healthy = check_server_health(port=wport, token=SERVER_BEARER_TOKEN) if not w_dead else False
            if not w_healthy:
                worker_unhealthy_counts[wport] = worker_unhealthy_counts.get(wport, 0) + 1
                log(f"Watchdog: Worker {wport} (GPU {idx}) health check failed ({worker_unhealthy_counts[wport]}/6)...")
                if w_dead or worker_unhealthy_counts[wport] >= 6:
                    log(f"Watchdog: Worker on port {wport} (GPU {idx}) dead or unresponsive! Restarting worker {wport}...")
                    try:
                        p.terminate()
                        p.wait(timeout=5)
                    except Exception:
                        try:
                            p.kill()
                        except Exception:
                            pass
                    w_env = os.environ.copy()
                    w_env["CUDA_VISIBLE_DEVICES"] = str(idx)
                    w_env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
                    w_log_f = open(w_log_path, "a", encoding="utf-8")
                    new_p = subprocess.Popen(
                        [sys.executable, str(worker_server_path), "--port", str(wport)],
                        env=w_env,
                        stdout=w_log_f,
                        stderr=subprocess.STDOUT,
                    )
                    worker_procs[wport] = (new_p, idx, w_log_path)
                    worker_unhealthy_counts[wport] = 0
                    if wait_for_server(new_p, timeout=240, port=wport, token=SERVER_BEARER_TOKEN):
                        log(f"Watchdog: Worker {wport} restarted and healthy!")
                        test_warmup(port=wport, token=SERVER_BEARER_TOKEN, timeout=60)
                    else:
                        log(f"Watchdog WARNING: Worker {wport} restart failed!")
            else:
                worker_unhealthy_counts[wport] = 0

        # 3. Gateway Watchdog
        gw_dead = gateway_proc is None or gateway_proc.poll() is not None
        gw_healthy = check_server_health(port=LOCAL_PORT, token=SERVER_BEARER_TOKEN) if not gw_dead else False

        if not gw_healthy:
            gateway_unhealthy_count += 1
            log(f"Watchdog: Gateway health check failed ({gateway_unhealthy_count}/6)...")
            if gw_dead or gateway_unhealthy_count >= 6:
                log("Watchdog: Gateway process down or unresponsive. Restarting gateway...")
                if gateway_proc:
                    try:
                        gateway_proc.terminate()
                        gateway_proc.wait(timeout=5)
                    except Exception:
                        try:
                            gateway_proc.kill()
                        except Exception:
                            pass
                log_gw = open(log_gw_path, "a", encoding="utf-8")
                gateway_proc = subprocess.Popen(
                    [
                        sys.executable,
                        str(gateway_server_path),
                        "--port",
                        str(LOCAL_PORT),
                        "--worker-ports",
                        gateway_ports_arg,
                    ],
                    stdout=log_gw,
                    stderr=subprocess.STDOUT,
                )
                gateway_unhealthy_count = 0
                wait_for_server(gateway_proc, timeout=60, port=LOCAL_PORT, token=SERVER_BEARER_TOKEN)
        else:
            gateway_unhealthy_count = 0

        # 4. Periodic Heartbeat Announcement (Every 180s)
        if now - last_heartbeat_time >= HEARTBEAT_INTERVAL_S:
            if current_base_url:
                publish_relay(
                    RELAY_CHANNEL,
                    {
                        "status": "online",
                        "base_url": current_base_url,
                        "model": SERVED_MODEL_NAME,
                        "tier": active_tier,
                        "gpu_count": gpu_count,
                        "uptime_s": int(uptime),
                    },
                    title=f"Heartbeat ({int(uptime // 60)}m uptime)",
                    tags="heartpulse",
                )
                log(f"[HEARTBEAT] Uptime: {int(uptime // 60)}m ({int(uptime)}s) | URL: {current_base_url}")
            last_heartbeat_time = now

    # Clean shutdown
    log("Shutting down workers, gateway, and tunnel...")
    if cloudflared_proc:
        try:
            cloudflared_proc.terminate()
        except Exception:
            pass
    if gateway_proc:
        try:
            gateway_proc.terminate()
        except Exception:
            pass
    for wport, (p, idx, _) in worker_procs.items():
        try:
            p.terminate()
        except Exception:
            pass

    log("ALL TASKS COMPLETED. Exiting 0 cleanly.")
    sys.exit(0)


if __name__ == "__main__":
    main()
