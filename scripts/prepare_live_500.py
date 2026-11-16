import json
import os
import random
import shutil
from collections import defaultdict, Counter
from pathlib import Path

def main():
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    # 1. Load previous 4 batches (50 + 100 + 150 + 150 = 450 questions)
    q50_path = root / "artifacts" / "live_trammay_eval_50" / "questions_50.jsonl"
    q100_path = root / "artifacts" / "live_trammay_eval_100" / "questions_100.jsonl"
    q150_path = root / "artifacts" / "live_trammay_eval_150" / "questions_150.jsonl"
    q150_b4_path = root / "artifacts" / "live_trammay_eval_450" / "questions_150_b4.jsonl"

    with open(q50_path, encoding="utf-8") as f:
        q50 = [json.loads(line) for line in f if line.strip()]
    with open(q100_path, encoding="utf-8") as f:
        q100 = [json.loads(line) for line in f if line.strip()]
    with open(q150_path, encoding="utf-8") as f:
        q150 = [json.loads(line) for line in f if line.strip()]
    with open(q150_b4_path, encoding="utf-8") as f:
        q150_b4 = [json.loads(line) for line in f if line.strip()]

    q50_ids = set(q["id"] for q in q50)
    q100_ids = set(q["id"] for q in q100)
    q150_ids = set(q["id"] for q in q150)
    q150_b4_ids = set(q["id"] for q in q150_b4)

    assert len(q50) == 50, f"Expected 50, got {len(q50)}"
    assert len(q100) == 100, f"Expected 100, got {len(q100)}"
    assert len(q150) == 150, f"Expected 150, got {len(q150)}"
    assert len(q150_b4) == 150, f"Expected 150, got {len(q150_b4)}"

    # Mutual disjointness of prior batches
    assert len(q50_ids & q100_ids) == 0, f"Overlap q50 & q100: {len(q50_ids & q100_ids)}"
    assert len(q50_ids & q150_ids) == 0, f"Overlap q50 & q150: {len(q50_ids & q150_ids)}"
    assert len(q50_ids & q150_b4_ids) == 0, f"Overlap q50 & q150_b4: {len(q50_ids & q150_b4_ids)}"
    assert len(q100_ids & q150_ids) == 0, f"Overlap q100 & q150: {len(q100_ids & q150_ids)}"
    assert len(q100_ids & q150_b4_ids) == 0, f"Overlap q100 & q150_b4: {len(q100_ids & q150_b4_ids)}"
    assert len(q150_ids & q150_b4_ids) == 0, f"Overlap q150 & q150_b4: {len(q150_ids & q150_b4_ids)}"

    prev_450 = q50_ids | q100_ids | q150_ids | q150_b4_ids
    assert len(prev_450) == 450, f"Union of prior 4 batches must be 450, got {len(prev_450)}"
    print(f"[OK] Prior 4 batches loaded: disjoint union = {len(prev_450)}")

    # 2. Candidate evaluation questions & gold references
    eval_q_file = root.parent / "data steps challenge" / "data" / "eval" / "questions.jsonl"
    eval_gold_file = root.parent / "answer" / "Step_Data.jsonl"

    if eval_q_file.exists() and eval_gold_file.exists():
        print(f"Loading candidate questions from {eval_q_file}")
        with open(eval_q_file, encoding="utf-8") as f:
            all_questions = {json.loads(line)["id"]: json.loads(line) for line in f if line.strip()}
        with open(eval_gold_file, encoding="utf-8") as f:
            all_gold = {json.loads(line)["id"]: json.loads(line) for line in f if line.strip()}
        candidate_ids = sorted(list(set(all_questions.keys()) - prev_450))
    else:
        print("Using evaluation split from artifacts/live_trammay_eval/split.json and data/train")
        split_path = root / "artifacts" / "live_trammay_eval" / "split.json"
        with open(split_path, encoding="utf-8") as f:
            split_data = json.load(f)
        eval_ids = set(split_data["splits"]["evaluation"]["question_ids"])

        with open(root / "data" / "train" / "questions.jsonl", encoding="utf-8") as f:
            all_questions = {json.loads(line)["id"]: json.loads(line) for line in f if line.strip()}
        with open(root / "data" / "train" / "reference_answers.jsonl", encoding="utf-8") as f:
            all_gold = {json.loads(line)["id"]: json.loads(line) for line in f if line.strip()}

        candidate_ids = sorted(list((eval_ids & set(all_questions.keys())) - prev_450))

    print(f"Total candidate unseen evaluation questions: {len(candidate_ids)}")

    # 3. Stratified sampling: 50 unseen questions across 6 businesses
    # Quotas: biz_104: 9, biz_111: 9, biz_117: 8, biz_121: 8, biz_128: 8, biz_130: 8 (sum = 50)
    biz_targets = {
        "biz_104": 9,
        "biz_111": 9,
        "biz_117": 8,
        "biz_121": 8,
        "biz_128": 8,
        "biz_130": 8,
    }
    assert sum(biz_targets.values()) == 50

    rng = random.Random(20261110)

    selected_ids = []
    by_biz_type_selected = defaultdict(Counter)

    for b, target in biz_targets.items():
        b_cands = sorted([qid for qid in candidate_ids if all_questions[qid]["business_id"] == b])
        N_b = len(b_cands)
        assert N_b >= target, f"Business {b} only has {N_b} remaining candidates, need {target}"

        by_type = defaultdict(list)
        for qid in b_cands:
            t = all_gold[qid]["type"]
            by_type[t].append(qid)

        avail_types = sorted(by_type.keys())
        # Allocate min 1 for each available answer type to guarantee representation
        quotas = {t: 1 for t in avail_types}
        rem_slots = target - len(avail_types)
        assert rem_slots >= 0, f"More types ({len(avail_types)}) than target ({target}) for {b}"

        # Largest Remainder proportional allocation for remaining slots
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

        assert sum(quotas.values()) == target, f"Sum of quotas {sum(quotas.values())} != target {target} for {b}"

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

    # 4. Zero overlap verification
    assert len(selected_set & q50_ids) == 0, f"Overlap with Batch 1 (50): {len(selected_set & q50_ids)}"
    assert len(selected_set & q100_ids) == 0, f"Overlap with Batch 2 (100): {len(selected_set & q100_ids)}"
    assert len(selected_set & q150_ids) == 0, f"Overlap with Batch 3 (150): {len(selected_set & q150_ids)}"
    assert len(selected_set & q150_b4_ids) == 0, f"Overlap with Batch 4 (150): {len(selected_set & q150_b4_ids)}"

    cumulative_500 = selected_set | prev_450
    assert len(cumulative_500) == 500, f"Cumulative union is {len(cumulative_500)}, expected 500"

    biz_counts_500 = Counter(all_questions[qid]["business_id"] for qid in cumulative_500)
    print("\n[OK] Mathematical proof verified: 50 + 100 + 150 + 150 + 50 = 500 distinct questions!")
    print(f"[OK] Cumulative 500 questions per business: {dict(biz_counts_500)}")

    # 5. Output directory
    out_dir = root / "artifacts" / "live_trammay_eval_500"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Clean existing submission or run_artifacts if present
    sub_path = out_dir / "submission.jsonl"
    art_path = out_dir / "run_artifacts"
    if sub_path.exists():
        sub_path.unlink()
        print(f"Removed existing {sub_path}")
    if art_path.exists():
        shutil.rmtree(art_path)
        print(f"Removed existing {art_path}")

    # Write questions_50_b5.jsonl and gold_50_b5.jsonl
    q50_b5_path = out_dir / "questions_50_b5.jsonl"
    g50_b5_path = out_dir / "gold_50_b5.jsonl"

    with open(q50_b5_path, "w", encoding="utf-8") as fq, open(g50_b5_path, "w", encoding="utf-8") as fg:
        for qid in selected_ids:
            fq.write(json.dumps(all_questions[qid], ensure_ascii=False) + "\n")
            fg.write(json.dumps(all_gold[qid], ensure_ascii=False) + "\n")

    print(f"[OK] Wrote 50 questions to {q50_b5_path}")
    print(f"[OK] Wrote 50 gold answers to {g50_b5_path}")

    print("\n--- Breakdown by Business & Answer Type for VER-LIVE-500 (Batch 5) ---")
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

