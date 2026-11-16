"""Prepare disjoint Next-100 benchmark for ag/gpt-oss-120b-medium.

Zero overlap with questions_50.jsonl and questions_next50.jsonl, reaching 200
unique evaluated questions.
"""

import json
import os
import random
from collections import Counter, defaultdict
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    # 1. Load previously evaluated IDs
    q50_path = root / "artifacts" / "live_trammay_eval_50" / "questions_50.jsonl"
    qn50_path = (
        root
        / "artifacts"
        / "live_trammay_eval_next50_gptoss"
        / "questions_next50.jsonl"
    )

    with open(q50_path, encoding="utf-8") as f:
        q50 = [json.loads(line) for line in f if line.strip()]
    with open(qn50_path, encoding="utf-8") as f:
        qn50 = [json.loads(line) for line in f if line.strip()]

    q50_ids = set(q["id"] for q in q50)
    qn50_ids = set(q["id"] for q in qn50)
    already_seen = q50_ids | qn50_ids
    assert len(q50_ids) == 50, f"Expected 50 in q50, got {len(q50_ids)}"
    assert len(qn50_ids) == 50, f"Expected 50 in qn50, got {len(qn50_ids)}"
    assert (
        len(already_seen) == 100
    ), f"Expected 100 in already_seen, got {len(already_seen)}"
    print(f"[OK] Loaded already seen IDs: {len(already_seen)}")

    # 2. Load candidate pool from held-out sets
    q100_path = (
        root / "artifacts" / "live_trammay_eval_100" / "questions_100.jsonl"
    )
    g100_path = root / "artifacts" / "live_trammay_eval_100" / "gold_100.jsonl"
    q150_path = (
        root / "artifacts" / "live_trammay_eval_150" / "questions_150.jsonl"
    )
    g150_path = root / "artifacts" / "live_trammay_eval_150" / "gold_150.jsonl"

    with open(q100_path, encoding="utf-8") as f:
        q100 = [json.loads(line) for line in f if line.strip()]
    with open(g100_path, encoding="utf-8") as f:
        g100 = [json.loads(line) for line in f if line.strip()]
    with open(q150_path, encoding="utf-8") as f:
        q150 = [json.loads(line) for line in f if line.strip()]
    with open(g150_path, encoding="utf-8") as f:
        g150 = [json.loads(line) for line in f if line.strip()]

    pool_q = {}
    pool_g = {}

    for q, g in zip(q100, g100):
        assert q["id"] == g["id"]
        if q["id"] not in already_seen:
            pool_q[q["id"]] = q
            pool_g[g["id"]] = g

    for q, g in zip(q150, g150):
        assert q["id"] == g["id"]
        if q["id"] not in already_seen:
            pool_q[q["id"]] = q
            pool_g[g["id"]] = g

    print(
        f"[OK] Candidate pool size (excluding already_seen): {len(pool_q)} questions."
    )
    assert (
        len(pool_q) >= 100
    ), f"Candidate pool too small: {len(pool_q)} < 100"

    # 3. Stratified sampling
    biz_targets = {
        "biz_104": 17,
        "biz_111": 17,
        "biz_117": 17,
        "biz_121": 17,
        "biz_128": 16,
        "biz_130": 16,
    }
    assert sum(biz_targets.values()) == 100

    rng = random.Random(20261107)

    selected_ids = []
    by_biz_type_selected = defaultdict(Counter)

    for b, target in biz_targets.items():
        b_cands = sorted(
            [qid for qid in pool_q if pool_q[qid]["business_id"] == b]
        )
        assert len(b_cands) >= target, (
            f"Business {b} candidate pool ({len(b_cands)}) < target ({target})"
        )

        by_type = defaultdict(list)
        for qid in b_cands:
            t = pool_g[qid]["type"]
            by_type[t].append(qid)

        avail_types = sorted(by_type.keys())
        quotas = {t: 1 for t in avail_types}
        rem_slots = target - len(avail_types)
        assert rem_slots >= 0, f"More types than target in {b}"

        denom = sum(len(by_type[t]) - 1 for t in avail_types)
        remainders = {}
        for t in avail_types:
            exact = (
                rem_slots * (len(by_type[t]) - 1) / denom if denom > 0 else 0
            )
            base = int(exact)
            quotas[t] += base
            remainders[t] = exact - base

        deficit = target - sum(quotas.values())
        sorted_rem = sorted(remainders.items(), key=lambda x: (-x[1], x[0]))
        for t, _ in sorted_rem[:deficit]:
            quotas[t] += 1

        assert sum(quotas.values()) == target

        b_selected = []
        for t in avail_types:
            cands_t = sorted(by_type[t])
            rng.shuffle(cands_t)
            k = quotas[t]
            chosen = cands_t[:k]
            b_selected.extend(chosen)
            by_biz_type_selected[b][t] += len(chosen)

        assert len(b_selected) == target
        selected_ids.extend(b_selected)

    assert len(selected_ids) == 100
    new_100_ids = set(selected_ids)

    # Assertions required by specification
    assert len(new_100_ids) == 100, f"Expected 100 unique IDs, got {len(new_100_ids)}"
    assert len(new_100_ids & q50_ids) == 0, f"Overlap with q50: {len(new_100_ids & q50_ids)}"
    assert len(new_100_ids & qn50_ids) == 0, f"Overlap with q_next50: {len(new_100_ids & qn50_ids)}"
    assert len(already_seen | new_100_ids) == 200, f"Cumulative unique: {len(already_seen | new_100_ids)}"

    print("[OK] All disjointness assertions passed:")
    print(f"  - len(new_100_ids) == {len(new_100_ids)}")
    print(f"  - len(new_100_ids & q50_ids) == {len(new_100_ids & q50_ids)}")
    print(f"  - len(new_100_ids & qn50_ids) == {len(new_100_ids & qn50_ids)}")
    print(f"  - len(already_seen | new_100_ids) == {len(already_seen | new_100_ids)}")

    # 4. Save to artifacts/live_trammay_eval_next100_gptoss
    out_dir = root / "artifacts" / "live_trammay_eval_next100_gptoss"
    out_dir.mkdir(parents=True, exist_ok=True)

    q_out = out_dir / "questions_next100.jsonl"
    g_out = out_dir / "gold_next100.jsonl"

    with open(q_out, "w", encoding="utf-8") as fq, open(
        g_out, "w", encoding="utf-8"
    ) as fg:
        for qid in selected_ids:
            fq.write(json.dumps(pool_q[qid], ensure_ascii=False) + "\n")
            fg.write(json.dumps(pool_g[qid], ensure_ascii=False) + "\n")

    print(f"[OK] Wrote 100 questions to: {q_out}")
    print(f"[OK] Wrote 100 gold answers to: {g_out}")

    # 5. Print stratification table
    businesses = list(biz_targets.keys())
    all_types = sorted(
        list(set(t for b in businesses for t in by_biz_type_selected[b]))
    )
    col_w = 12
    hdr = f"{'Business':<10} " + " ".join(f"{t:>{col_w}}" for t in all_types) + "  Total"
    print("\n" + "=" * len(hdr))
    print("STRATIFICATION BREAKDOWN: NEXT-100 HELD-OUT BENCHMARK")
    print("=" * len(hdr))
    print(hdr)
    print("-" * len(hdr))
    for b in businesses:
        row = (
            f"{b:<10} "
            + " ".join(f"{by_biz_type_selected[b][t]:>{col_w}}" for t in all_types)
            + f"  {sum(by_biz_type_selected[b].values()):>5}"
        )
        print(row)
    print("-" * len(hdr))
    total_by_type = Counter()
    for b in businesses:
        for t in all_types:
            total_by_type[t] += by_biz_type_selected[b][t]
    print(
        f"{'TOTAL':<10} "
        + " ".join(f"{total_by_type[t]:>{col_w}}" for t in all_types)
        + f"  {sum(total_by_type.values()):>5}"
    )
    print("=" * len(hdr))


if __name__ == "__main__":
    main()
