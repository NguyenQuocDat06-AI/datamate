"""CPU-only tests for the worker micro-batching core in run_server_12h.py.

The batching core lives between the BATCH-CORE-BEGIN / BATCH-CORE-END markers
inside WORKER_SERVER_TEMPLATE. This test extracts that exact span from the
shipped template and exec's it (no torch, no model). The torch-specific wrapper
`generate_batch` is mirrored here with a fake model that has the same contract
(left-padded input_ids + attention_mask in, input+new tokens out).
"""
from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_server_12h  # noqa: E402  (module has no side effects at import)

BEGIN = "# ---- BATCH-CORE-BEGIN"
END = "# ---- BATCH-CORE-END"
_src = run_server_12h.WORKER_SERVER_TEMPLATE
assert _src.count(BEGIN) == 1 and _src.count(END) == 1, "batch core markers missing"
CORE_SRC = _src[_src.index(BEGIN): _src.index(END)]


def load_core():
    import queue
    import threading as th
    import time

    ns = {"queue": queue, "threading": th, "time": time}
    exec(compile(CORE_SRC, "<batch-core>", "exec"), ns)
    return ns


CORE = load_core()
EOS_ID = 1
PAD_ID = 0
EOS_IDS = [EOS_ID]


class FakeTokenizer:
    """Left-padding tokenizer stand-in; ids are always >= 2 so they never look like eos/pad."""
    pad_token_id = PAD_ID
    padding_side = "left"

    def __call__(self, text):
        return {"input_ids": [2 + (ord(ch) % 90) for ch in text]}


class FakeModel:
    """Mimics HF generate on left-padded batches. New tokens depend only on the real (unmasked)
    prompt tokens, so padding must not change results. eos_at maps real prompt -> eos position."""

    def __init__(self, eos_at=None, oom_if=None):
        self.eos_at = eos_at or {}
        self.oom_if = oom_if
        self.calls = []

    def generate(self, input_ids, attention_mask, max_new_tokens, eos_token_id=None, pad_token_id=None, **kw):
        bsz = len(input_ids)
        self.calls.append({"batch": bsz, "width": len(input_ids[0]), "masks": [list(m) for m in attention_mask],
                           "max_new_tokens": max_new_tokens, "kwargs": sorted(kw)})
        if self.oom_if is not None and self.oom_if(bsz):
            raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 MiB")
        reals = [tuple(t for t, m in zip(row, mask) if m) for row, mask in zip(input_ids, attention_mask)]
        eos_pos = [self.eos_at.get(r) for r in reals]
        finished = [False] * bsz
        steps = []
        for k in range(max_new_tokens):
            if all(finished):
                break
            step = []
            for i in range(bsz):
                if finished[i]:
                    step.append(pad_token_id if pad_token_id is not None else PAD_ID)
                elif eos_pos[i] == k:
                    step.append(EOS_ID)
                    finished[i] = True
                else:
                    step.append(100 + (sum(reals[i]) + 7 * k) % 50)
            steps.append(step)
        out = []
        for i in range(bsz):
            gen = [s[i] for s in steps]
            out.append(list(input_ids[i]) + gen)
        width = max(len(r) for r in out) if out else 0
        # HF returns rectangular output: unfinished-early rows are padded with pad_token_id
        return [r + [pad_token_id if pad_token_id is not None else PAD_ID] * (width - len(r)) for r in out]


def make_generate_batch(model, lock, pad_id=PAD_ID):
    """Same contract as the template's generate_batch: left-pad, one generate, return new-token rows."""

    def gen(seqs, max_new_tokens, boundaries=None):  # boundaries: prefix-cache hints, unused without the cache
        assert lock.locked(), "generate must run while gpu_lock is held"
        ids, mask, width = CORE["left_pad"](seqs, pad_id)
        out = model.generate(input_ids=ids, attention_mask=mask, max_new_tokens=max_new_tokens,
                             eos_token_id=EOS_IDS, pad_token_id=0, use_cache=True, logits_to_keep=1)
        return [row[width:] for row in out]

    return gen


def make_batcher(model, max_batch, wait_ms=2000, on_oom=None, logs=None):
    lock = threading.Lock()
    b = CORE["MicroBatcher"](make_generate_batch(model, lock), lock, EOS_IDS, max_batch, wait_ms,
                             on_oom=on_oom, log=(logs.append if logs is not None else None))
    b.start()
    return b


# Real prompts (ids) of different lengths; A emits eos at new-token index 2.
PROMPT_A = [10, 11, 12]
PROMPT_B = [20, 21, 22, 23, 24]
PROMPT_C = [30, 31]
EOS_AT = {tuple(PROMPT_A): 2}


def run_concurrently(batcher, reqs):
    """reqs: list of (ids, max_tokens). Submits from separate threads, returns results in order."""
    with ThreadPoolExecutor(max_workers=len(reqs)) as ex:
        futs = [ex.submit(batcher.submit, ids, mt) for ids, mt in reqs]
        return [f.result(timeout=30) for f in futs]


def test_three_requests_one_generate_each_gets_own_cut_tokens():
    model = FakeModel(eos_at=EOS_AT)
    logs = []
    b = make_batcher(model, max_batch=3, logs=logs)
    reqs = [(PROMPT_A, 4), (PROMPT_B, 6), (PROMPT_C, 5)]
    results = run_concurrently(b, reqs)

    # one generate call for all three, padded to the longest prompt, max over the batch
    assert len(model.calls) == 1
    call = model.calls[0]
    assert call["batch"] == 3 and call["width"] == 5
    assert call["max_new_tokens"] == 6
    assert call["masks"][0] == [0, 0, 1, 1, 1]  # left padding for the 3-token prompt
    assert call["masks"][2] == [0, 0, 0, 1, 1]  # left padding (2 real tokens)
    assert call["kwargs"] == ["logits_to_keep", "use_cache"]

    (a_new, a_in, a_n), (b_new, b_in, b_n), (c_new, c_in, c_n) = results
    # input_len is the item's own unpadded length
    assert (a_in, b_in, c_in) == (3, 5, 2)
    # A: cut at its eos (eos included, as un-batched output includes it), 3 tokens
    assert a_new[-1] == EOS_ID and len(a_new) == 3 and a_n == 3
    assert EOS_ID not in a_new[:-1]
    # B: no eos, runs to max_tokens=6
    assert len(b_new) == 6 and b_n == 6 and EOS_ID not in b_new
    # C: max_tokens=5 cuts a row that was generated to the batch max of 6
    assert len(c_new) == 5 and c_n == 5
    # the logs carry sizes and counts only, never prompt text
    assert len(logs) == 1
    assert "size=3" in logs[0] and "input_tokens=10" in logs[0] and "new_tokens=14" in logs[0]
    assert "seconds=" in logs[0]


def test_batched_outputs_equal_unbatched_outputs():
    model = FakeModel(eos_at=EOS_AT)
    batched = run_concurrently(make_batcher(model, max_batch=3), [(PROMPT_A, 4), (PROMPT_B, 6), (PROMPT_C, 5)])
    single = make_batcher(FakeModel(eos_at=EOS_AT), max_batch=1)
    unbatched = [single.submit(ids, mt) for ids, mt in [(PROMPT_A, 4), (PROMPT_B, 6), (PROMPT_C, 5)]]
    assert [r[0] for r in batched] == [r[0] for r in unbatched]


def test_batch_max_one_is_one_generate_per_request_without_padding():
    model = FakeModel(eos_at=EOS_AT)
    b = make_batcher(model, max_batch=1)
    results = [b.submit(ids, mt) for ids, mt in [(PROMPT_A, 4), (PROMPT_B, 6), (PROMPT_C, 5)]]
    assert len(model.calls) == 3
    for call, ids in zip(model.calls, [PROMPT_A, PROMPT_B, PROMPT_C]):
        assert call["batch"] == 1
        assert call["width"] == len(ids)  # no padding at all
        assert call["masks"] == [[1] * len(ids)]
    assert results[0][1] == 3 and results[1][1] == 5 and results[2][1] == 2


def test_streaming_and_sampling_requests_do_not_enter_batch_path():
    # handler routing: only non-streaming greedy requests are batched, and only when BATCH_MAX > 1
    route = CORE["use_batch_path"]
    assert route(False, 0.0, 8) is True
    assert route(False, 0.0, 1) is False          # BATCH_MAX=1 keeps today's path
    assert route(True, 0.0, 8) is False           # streaming
    assert route(False, 0.7, 8) is False          # temperature > 0
    assert route(True, 0.7, 8) is False


def test_oom_on_batch_of_four_splits_two_plus_two_and_succeeds():
    model = FakeModel(eos_at=EOS_AT, oom_if=lambda bsz: bsz > 2)
    on_oom_calls = []
    b = make_batcher(model, max_batch=4, on_oom=lambda: on_oom_calls.append(1))
    prompts = [PROMPT_A, PROMPT_B, PROMPT_C, [40, 41, 42, 43]]
    results = run_concurrently(b, [(p, 4) for p in prompts])
    assert [c["batch"] for c in model.calls] == [4, 2, 2]
    assert len(on_oom_calls) == 1
    assert [r[1] for r in results] == [len(p) for p in prompts]
    assert all(len(r[0]) >= 1 for r in results)


def test_single_request_oom_raises_to_caller():
    model = FakeModel(oom_if=lambda bsz: True)
    on_oom_calls = []
    b = make_batcher(model, max_batch=1, on_oom=lambda: on_oom_calls.append(1))
    with pytest.raises(RuntimeError, match="out of memory"):
        b.submit(PROMPT_C, 4)
    assert len(on_oom_calls) == 1


def test_non_oom_error_fails_every_request_in_the_batch():
    def boom(seqs, max_new_tokens, boundaries=None):
        raise ValueError("bad shape")

    lock = threading.Lock()
    b = CORE["MicroBatcher"](boom, lock, EOS_IDS, 2, 2000)
    b.start()
    with pytest.raises(ValueError, match="bad shape"):
        run_concurrently(b, [(PROMPT_A, 3), (PROMPT_C, 3)])


def test_fake_tokenizer_feeds_batcher_and_pads_left():
    tok = FakeTokenizer()
    model = FakeModel()
    b = make_batcher(model, max_batch=2)
    prompts = ["ab", "abcd"]
    results = run_concurrently(b, [(tok(p)["input_ids"], 3) for p in prompts])
    assert [r[1] for r in results] == [2, 4]
    assert model.calls[0]["masks"][0] == [0, 0, 1, 1]
    assert tok.padding_side == "left"
