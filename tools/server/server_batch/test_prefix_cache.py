"""CPU tests for the shared-prefix KV cache in run_server_12h.py.

Two marked spans of WORKER_SERVER_TEMPLATE are exec'd straight from the shipped template:

* BATCH-CORE (pure Python, always tested): PrefixCacheStore, prefix_generate_rows and
  prefix_self_check. The model work is injected through an `ops` object, so the decision logic
  (LRU, boundary choice, grouping, counters, self-check reasons) runs without torch.
* PREFIX-TORCH (torch/transformers): PrefixOps, the real cache handling. These tests need torch
  and transformers and are skipped when they are not installed. They use tiny randomly
  initialised models (Llama, and Gemma3 text if the installed transformers has it); nothing is
  downloaded.
"""
from __future__ import annotations

import contextlib
import copy
import random
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_server_12h  # noqa: E402  (no side effects at import)

_src = run_server_12h.WORKER_SERVER_TEMPLATE


def _span(begin: str, end: str) -> str:
    assert _src.count(begin) == 1 and _src.count(end) == 1, f"markers missing: {begin}"
    return _src[_src.index(begin): _src.index(end)]


CORE_SRC = _span("# ---- BATCH-CORE-BEGIN", "# ---- BATCH-CORE-END")
TORCH_SRC = _span("# ---- PREFIX-TORCH-BEGIN", "# ---- PREFIX-TORCH-END")


def _load_core():
    import queue
    import threading
    import time

    ns = {"queue": queue, "threading": threading, "time": time}
    exec(compile(CORE_SRC, "<batch-core>", "exec"), ns)
    return ns


CORE = _load_core()
EOS_IDS = [1]
MAX_NEW = 6
PREFIX = [2, 3, 4, 5, 6]          # 5-token shared "system" prefix
PREFIX_LEN = len(PREFIX)


# ---------------------------------------------------------------- fake ops (no torch)

def gen_tokens(ids, max_new):
    """Deterministic 'model': the output depends on the full prompt only."""
    s = sum(ids)
    return [100 + (s * 7 + k * 13) % 50 for k in range(max_new)]


class FakeEntry:
    def __init__(self, ids):
        self.ids = tuple(ids)


class FakeOps:
    """Stand-in for PrefixOps. broken=True makes the cached single path ignore the prefix."""

    def __init__(self, broken=False, batch_ok=True, make_fail=None, cached_raises=None):
        self.broken = broken
        self.batch_ok = batch_ok
        self.make_fail = make_fail
        self.cached_raises = cached_raises
        self.made = []
        self.single_cached_calls = 0
        self.batch_calls = []
        self.plain_calls = []

    def make_entry(self, prefix_ids):
        if self.make_fail is not None:
            raise self.make_fail
        self.made.append(tuple(prefix_ids))
        return FakeEntry(prefix_ids)

    def single(self, ids, max_new, entry):
        if entry is None:
            return gen_tokens(ids, max_new)
        assert tuple(ids[: len(entry.ids)]) == entry.ids, "request must start with the cached prefix"
        self.single_cached_calls += 1
        if self.cached_raises is not None:
            raise self.cached_raises
        if self.broken:
            return gen_tokens(ids[len(entry.ids):], max_new)
        return gen_tokens(ids, max_new)

    def batch_prefix(self, seqs, max_new, entry):
        if not self.batch_ok:
            return None
        self.batch_calls.append(len(seqs))
        return [self.single(s, max_new, entry) for s in seqs]

    def plain_batch(self, seqs, max_new):
        self.plain_calls.append(len(seqs))
        return [gen_tokens(s, max_new) for s in seqs]


def make_store(ops, slots=2, min_tokens=4, logs=None):
    return CORE["PrefixCacheStore"](ops.make_entry, slots, min_tokens,
                                    log=(logs.append if logs is not None else None))


def req(prefix, suffix):
    return list(prefix) + list(suffix)


# ---------------------------------------------------------------- pure-Python tests

def test_common_prefix_len():
    cpl = CORE["common_prefix_len"]
    assert cpl([1, 2, 3], [1, 2, 9]) == 2
    assert cpl([1, 2], [1, 2, 3]) == 2
    assert cpl([], [1]) == 0
    assert cpl([5], [6]) == 0


def test_lru_two_slots_evicts_least_recently_used():
    ops = FakeOps()
    store = make_store(ops, slots=2, min_tokens=4)
    A, B, C = [2, 3, 4, 5, 6], [7, 8, 9, 10, 11], [12, 13, 14, 15, 16]

    _, reused = store.acquire(req(A, [40]), 5)
    assert reused == 0 and store.created == 1
    store.acquire(req(B, [40]), 5)
    entry, reused = store.acquire(req(A, [41]), 5)      # hit: A becomes most recent
    assert entry is not None and reused == 5
    store.acquire(req(C, [40]), 5)                      # full: evicts B (least recent)
    assert store.evicted == 1
    assert ops.made == [tuple(A), tuple(B), tuple(C)]
    entry, reused = store.acquire(req(A, [42]), 5)      # A survived
    assert reused == 5 and store.hits == 2
    entry, reused = store.acquire(req(B, [42]), 5)      # B was evicted: must be recreated
    assert entry is not None and reused == 0
    assert ops.made[-1] == tuple(B)
    assert store.evicted == 2
    assert store.hits == 2 and store.created == 4
    assert store.skipped_tokens == 2 * 5               # two hits of 5 prefix tokens each


def test_hit_counts_and_skipped_tokens_for_shared_prefix():
    ops = FakeOps()
    store = make_store(ops, slots=2, min_tokens=4)
    rows = CORE["prefix_generate_rows"](store, ops, [req(PREFIX, [40]), req(PREFIX, [41, 42]), req(PREFIX, [43])],
                                        [PREFIX_LEN] * 3, MAX_NEW)
    assert store.created == 1 and store.hits == 2
    assert store.skipped_tokens == 2 * PREFIX_LEN
    assert rows == [gen_tokens(req(PREFIX, s), MAX_NEW) for s in ([40], [41, 42], [43])]


def test_prefix_shorter_than_min_tokens_gets_no_entry():
    ops = FakeOps()
    store = make_store(ops, slots=2, min_tokens=8)
    entry, reused = store.acquire(req(PREFIX, [40]), PREFIX_LEN)   # 5 < 8
    assert entry is None and reused == 0
    assert ops.made == [] and store.none == 1


def test_boundary_must_leave_a_suffix_token():
    ops = FakeOps()
    store = make_store(ops, slots=2, min_tokens=4)
    entry, _ = store.acquire(req(PREFIX, [40]), len(PREFIX) + 1)   # boundary == len(ids): no suffix
    assert entry is None and ops.made == []


def test_fallback_boundary_uses_previous_request_when_long_enough():
    ops = FakeOps()
    store = make_store(ops, slots=2, min_tokens=4)
    store.acquire([2, 3, 4, 5, 6, 40])                  # no previous request: no boundary, no entry
    assert ops.made == []
    entry, reused = store.acquire([2, 3, 4, 5, 6, 41])  # common prefix with previous = 5 >= 4
    assert entry is not None and reused == 0 and ops.made == [(2, 3, 4, 5, 6)]
    entry, reused = store.acquire([2, 3, 4, 5, 6, 42])
    assert reused == 5


def test_fallback_boundary_below_min_gives_no_entry():
    ops = FakeOps()
    store = make_store(ops, slots=2, min_tokens=4)
    store.acquire([1, 2, 40])
    entry, _ = store.acquire([1, 3, 41])                # common prefix = 1 < 4
    assert entry is None and ops.made == []


def test_oom_during_creation_disables_store_and_continues_without_cache():
    ops = FakeOps(make_fail=RuntimeError("CUDA out of memory. Tried to allocate 2.00 MiB"))
    logs = []
    store = make_store(ops, slots=2, min_tokens=4, logs=logs)
    entry, reused = store.acquire(req(PREFIX, [40]), PREFIX_LEN)
    assert entry is None and reused == 0
    assert store.enabled is False
    assert "prefix cache disabled: out of memory during prefix cache creation" in logs
    assert store.acquire(req(PREFIX, [41]), PREFIX_LEN) == (None, 0)


def test_non_oom_creation_error_disables_and_logs_type_only():
    ops = FakeOps(make_fail=ValueError("secret prompt text must not be logged"))
    logs = []
    store = make_store(ops, slots=2, min_tokens=4, logs=logs)
    store.acquire(req(PREFIX, [40]), PREFIX_LEN)
    assert store.enabled is False
    assert logs == ["prefix cache disabled: error during prefix cache creation (ValueError)"]


def test_batch_grouping_one_batch_for_shared_prefix_and_plain_for_the_rest():
    ops = FakeOps()
    store = make_store(ops, slots=2, min_tokens=4)
    seqs = [req(PREFIX, [40]), req(PREFIX, [41, 42, 43]), req(PREFIX, [44, 45]), [50, 51]]
    bounds = [PREFIX_LEN, PREFIX_LEN, PREFIX_LEN, None]
    rows = CORE["prefix_generate_rows"](store, ops, seqs, bounds, MAX_NEW)
    assert ops.batch_calls == [3]          # the three shared-prefix requests: one batched call
    assert ops.plain_calls == [1]          # the short unrelated prompt: no cache, plain batch
    assert rows == [gen_tokens(s, MAX_NEW) for s in seqs]


def test_batch_prefix_unsupported_falls_back_to_one_by_one_and_stays_correct():
    ops = FakeOps(batch_ok=False)
    store = make_store(ops, slots=2, min_tokens=4)
    seqs = [req(PREFIX, [40]), req(PREFIX, [41, 42])]
    rows = CORE["prefix_generate_rows"](store, ops, seqs, [PREFIX_LEN, PREFIX_LEN], MAX_NEW)
    assert ops.batch_calls == [] and ops.single_cached_calls == 2
    assert rows == [gen_tokens(s, MAX_NEW) for s in seqs]


def test_disabled_store_is_pure_plain_path():
    ops = FakeOps()
    store = make_store(ops)
    store.disable("PREFIX_CACHE=0")
    rows = CORE["prefix_generate_rows"](store, ops, [req(PREFIX, [40])], [PREFIX_LEN], MAX_NEW)
    assert ops.made == [] and ops.plain_calls == [1]
    assert rows == [gen_tokens(req(PREFIX, [40]), MAX_NEW)]


def test_per_request_log_has_counts_only_never_prompt_text():
    ops = FakeOps()
    logs = []
    store = make_store(ops, slots=2, min_tokens=4)
    CORE["prefix_generate_rows"](store, ops, [req(PREFIX, [40]), req(PREFIX, [41])], [PREFIX_LEN, PREFIX_LEN],
                                 MAX_NEW, log=logs.append)
    pat = re.compile(r"^prefix cache: (hit|created|none) prompt_tokens=\d+ skipped_tokens=\d+$")
    assert len(logs) == 2 and all(pat.match(line) for line in logs), logs
    assert logs[0].startswith("prefix cache: created prompt_tokens=6 skipped_tokens=0")
    assert logs[1].startswith("prefix cache: hit prompt_tokens=6 skipped_tokens=5")


def test_self_check_passes_for_correct_cached_path():
    ops = FakeOps()
    assert CORE["prefix_self_check"](ops, [req(PREFIX, [40]), req(PREFIX, [41, 42])], PREFIX_LEN, EOS_IDS) is None


def test_self_check_disables_cache_when_broken_cache_path_is_injected():
    ops = FakeOps(broken=True)
    logs = []
    store = make_store(ops, slots=2, min_tokens=4, logs=logs)
    reason = CORE["prefix_self_check"](ops, [req(PREFIX, [40]), req(PREFIX, [41, 42])], PREFIX_LEN, EOS_IDS)
    assert reason == "mismatch"
    store.disable(reason)
    assert store.enabled is False
    assert logs == ["prefix cache disabled: mismatch"]
    assert store.acquire(req(PREFIX, [43]), PREFIX_LEN) == (None, 0)


def test_self_check_reports_error_when_cached_path_raises():
    ops = FakeOps(cached_raises=RuntimeError("boom"))
    reason = CORE["prefix_self_check"](ops, [req(PREFIX, [40]), req(PREFIX, [41])], PREFIX_LEN, EOS_IDS)
    assert reason == "self-check error (RuntimeError)"


def test_self_check_fails_when_prefix_path_is_not_exercised():
    ops = FakeOps()
    # boundary 0 is below min_tokens=1 for the check store: no entry is created, so the check must not pass
    reason = CORE["prefix_self_check"](ops, [req(PREFIX, [40]), req(PREFIX, [41])], 0, EOS_IDS)
    assert reason == "self-check error (RuntimeError)"


def test_script_and_rendered_worker_template_compile():
    script = (HERE / "run_server_12h.py").read_text(encoding="utf-8")
    compile(script, str(HERE / "run_server_12h.py"), "exec")
    rendered = (_src.replace("__MODEL_DIR__", "/models/x")
                .replace("__SECRET_1__", "token-for-test")
                .replace("__SERVED_MODEL_NAME__", "served"))
    compile(rendered, "<rendered-worker>", "exec")
    assert "__MODEL_DIR__" not in rendered and "__SERVED_MODEL_NAME__" not in rendered


# ---------------------------------------------------------------- torch tests (skipped without torch)

def _torch_env():
    torch = pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")
    ns = {"torch": torch, "copy": copy}
    exec(compile(TORCH_SRC, "<prefix-torch>", "exec"), ns)
    return torch, transformers, ns


def _tiny_models(torch, transformers):
    models = {}
    torch.manual_seed(0)
    llama = transformers.LlamaConfig(vocab_size=128, hidden_size=32, intermediate_size=64, num_hidden_layers=2,
                                     num_attention_heads=4, num_key_value_heads=2, max_position_embeddings=256)
    models["llama"] = transformers.LlamaForCausalLM(llama).eval()
    gemma_cls = getattr(transformers, "Gemma3TextConfig", None)
    gemma_model = getattr(transformers, "Gemma3ForCausalLM", None)
    if gemma_cls is not None and gemma_model is not None:
        torch.manual_seed(0)
        cfg = gemma_cls(vocab_size=128, hidden_size=32, intermediate_size=64, num_hidden_layers=2,
                        num_attention_heads=4, num_key_value_heads=1, head_dim=8, max_position_embeddings=256,
                        sliding_window=16)
        models["gemma3"] = gemma_model(cfg).eval()
    return models


@pytest.fixture(scope="module", params=["llama", "gemma3"])
def torch_case(request):
    torch, transformers, ns = _torch_env()
    models = _tiny_models(torch, transformers)
    if request.param not in models:
        pytest.skip(f"{request.param} not available in installed transformers")
    model = models[request.param]

    def plain_batch(seqs, max_new):
        ids, mask, width = CORE["left_pad"](seqs, 0)
        out = model.generate(input_ids=torch.tensor(ids), attention_mask=torch.tensor(mask), max_new_tokens=max_new,
                             do_sample=False, temperature=None, eos_token_id=[127], pad_token_id=0, use_cache=True)
        return [row[width:].tolist() for row in out]

    ops = ns["PrefixOps"](model, [127], 0, "cpu", contextlib.nullcontext, plain_batch)
    return torch, model, ops, request.param


def _prefix_ids(seed, n):
    rng = random.Random(seed)
    return [rng.randrange(2, 120) for _ in range(n)]


def _with_torch_plain(ops, ids, max_new):
    return ops.single(ids, max_new, None)


def test_torch_single_cached_equals_plain_for_two_suffixes(torch_case):
    torch, model, ops, _ = torch_case
    prefix = _prefix_ids(1, 40)               # longer than the tiny Gemma sliding window (16)
    entry = ops.make_entry(prefix)
    for suffix in (_prefix_ids(2, 5), _prefix_ids(3, 9)):
        full = prefix + suffix
        assert ops.single(full, 8, entry) == _with_torch_plain(ops, full, 8)


def test_torch_batched_different_suffix_lengths_equals_plain(torch_case):
    torch, model, ops, _ = torch_case
    prefix = _prefix_ids(4, 40)
    entry = ops.make_entry(prefix)
    seqs = [prefix + _prefix_ids(5, 3), prefix + _prefix_ids(6, 7), prefix + _prefix_ids(7, 5)]
    rows = ops.batch_prefix(seqs, 8, entry)
    assert rows is not None
    assert rows == [_with_torch_plain(ops, s, 8) for s in seqs]


def test_torch_store_counters_and_rows_through_generate_rows(torch_case):
    torch, model, ops, _ = torch_case
    prefix = _prefix_ids(8, 40)
    seqs = [prefix + _prefix_ids(9, 4), prefix + _prefix_ids(10, 6)]
    store = CORE["PrefixCacheStore"](ops.make_entry, 2, 8)
    rows = CORE["prefix_generate_rows"](store, ops, seqs, [len(prefix)] * 2, 8)
    assert store.created == 1 and store.hits == 1 and store.skipped_tokens == len(prefix)
    assert rows == [_with_torch_plain(ops, s, 8) for s in seqs]


def test_torch_self_check_passes_and_broken_cache_path_disables(torch_case):
    torch, model, ops, _ = torch_case
    prefix = _prefix_ids(11, 40)
    seqs = [prefix + _prefix_ids(12, 4), prefix + _prefix_ids(13, 6)]
    assert CORE["prefix_self_check"](ops, seqs, len(prefix), [127]) is None

    class Broken(type(ops)):
        def single(self, ids, max_new, entry):
            out = super().single(ids, max_new, entry)
            return out if entry is None else [t + 1 for t in out]

    broken = Broken(model, [127], 0, "cpu", contextlib.nullcontext, ops.plain_batch)
    assert CORE["prefix_self_check"](broken, seqs, len(prefix), [127]) == "mismatch"
