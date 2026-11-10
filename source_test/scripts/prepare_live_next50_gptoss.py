import json
import os
import random
import shutil
from collections import defaultdict, Counter
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    # 1. Load q50
    q50_path = root / "artifacts" / "live_trammay_eval_50" / "questions_50.jsonl"
    with open(q50_path, encoding="utf-8") as f:
        q50 = [json.loads(line) for line in f if line.strip()]
    q50_ids = set(q["id"] for q in q50)
    assert len(q50_ids) == 50, f"Expected 50 IDs in q50, got {len(q50_ids)}"

    # 2. Load q100 and g100
    q100_path = root / "artifacts" / "live_trammay_eval_100" / "questions_100.jsonl"
    g100_path = root / "artifacts" / "live_trammay_eval_100" / "gold_100.jsonl"
    with open(q100_path, encoding="utf-8") as f:
        q100 = [json.loads(line) for line in f if line.strip()]
    with open(g100_path, encoding="utf-8") as f:
        g100 = [json.loads(line) for line in f if line.strip()]

    assert len(q100) == 100, f"Expected 100 in q100, got {len(q100)}"
    assert len(g100) == 100, f"Expected 100 in g100, got {len(g100)}"

    q100_dict = {q["id"]: q for q in q100}
    g100_dict = {g["id"]: g for g in g100}
    q100_ids = set(q100_dict.keys())
    assert len(q100_ids) == 100

    # Ensure q50 and q100 are completely disjoint
    assert len(q50_ids & q100_ids) == 0, f"Overlap between q50 and q100: {len(q50_ids & q100_ids)}"
    print("[OK] q50 (50) and q100 (100) loaded. Overlap = 0.")

    # 3. Stratified sampling of 50 questions from q100
    biz_targets = {
        "biz_104": 8,
        "biz_111": 8,
        "biz_117": 9,
        "biz_121": 9,
        "biz_128": 8,
        "biz_130": 8,
    }
    assert sum(biz_targets.values()) == 50

    rng = random.Random(20261111)

    selected_ids = []
    by_biz_type_selected = defaultdict(Counter)

    for b, target in biz_targets.items():
        b_cands = sorted([qid for qid in q100_ids if q100_dict[qid]["business_id"] == b])
        by_type = defaultdict(list)
        for qid in b_cands:
            t = g100_dict[qid]["type"]
            by_type[t].append(qid)

        avail_types = sorted(by_type.keys())
        quotas = {t: 1 for t in avail_types}
        rem_slots = target - len(avail_types)
        assert rem_slots >= 0, f"More types than target in {b}"

        denom = sum(len(by_type[t]) - 1 for t in avail_types)
        remainders = {}
        for t in avail_types:
            exact = rem_slots * (len(by_type[t]) - 1) / denom if denom > 0 else 0
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

    assert len(selected_ids) == 50
    selected_set = set(selected_ids)
    assert len(selected_set) == 50, "Duplicate IDs in selected_ids!"

    # 4. Verify mathematical disjointness
    assert len(selected_set & q50_ids) == 0, f"Overlap with first 50: {len(selected_set & q50_ids)}"
    cumulative_100 = selected_set | q50_ids
    assert len(cumulative_100) == 100, f"Expected 100 unique questions, got {len(cumulative_100)}"
    print("[OK] Mathematical proof: set(q_first50) & set(q_next50) == set() (50 + 50 = 100 unique IDs)")

    # 5. Output directory
    out_dir = root / "artifacts" / "live_trammay_eval_next50_gptoss"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Clean existing submission or run_artifacts if present
    sub_path = out_dir / "submission.jsonl"
    art_path = out_dir / "run_artifacts"
    if sub_path.exists():
        sub_path.unlink()
        print(f"Removed stale {sub_path}")
    if art_path.exists():
        shutil.rmtree(art_path)
        print(f"Removed stale {art_path}")

    # Write questions_next50.jsonl and gold_next50.jsonl
    q_out_path = out_dir / "questions_next50.jsonl"
    g_out_path = out_dir / "gold_next50.jsonl"

    with open(q_out_path, "w", encoding="utf-8") as fq, open(g_out_path, "w", encoding="utf-8") as fg:
        for qid in selected_ids:
            fq.write(json.dumps(q100_dict[qid], ensure_ascii=False) + "\n")
            fg.write(json.dumps(g100_dict[qid], ensure_ascii=False) + "\n")

    print(f"[OK] Wrote 50 questions to {q_out_path}")
    print(f"[OK] Wrote 50 gold answers to {g_out_path}")

    # Print breakdown
    print("\n--- Breakdown by Business & Answer Type for Next 50 ---")
    businesses = list(biz_targets.keys())
    all_types = sorted(list(set(t for b in businesses for t in by_biz_type_selected[b])))
    print(f"{'Business':<10} " + " ".join(f"{t:>12}" for t in all_types) + f" {'Total':>8}")
    for b in businesses:
        row = f"{b:<10} " + " ".join(f"{by_biz_type_selected[b][t]:>12}" for t in all_types) + f" {sum(by_biz_type_selected[b].values()):>8}"
        print(row)
    total_by_type = Counter()
    for b in businesses:
        for t in all_types:
            total_by_type[t] += by_biz_type_selected[b][t]
    print(f"{'TOTAL':<10} " + " ".join(f"{total_by_type[t]:>12}" for t in all_types) + f" {sum(total_by_type.values()):>8}")


if __name__ == "__main__":
    main()

