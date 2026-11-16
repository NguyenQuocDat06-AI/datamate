import json
import os
import random
import shutil
from collections import defaultdict, Counter
from pathlib import Path

def main():
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    # 1. Load q50, q100, and q150
    q50_path = root / "artifacts" / "live_trammay_eval_50" / "questions_50.jsonl"
    q100_path = root / "artifacts" / "live_trammay_eval_100" / "questions_100.jsonl"
    q150_path = root / "artifacts" / "live_trammay_eval_150" / "questions_150.jsonl"

    with open(q50_path, encoding="utf-8") as f:
        q50 = [json.loads(line) for line in f if line.strip()]
    with open(q100_path, encoding="utf-8") as f:
        q100 = [json.loads(line) for line in f if line.strip()]
    with open(q150_path, encoding="utf-8") as f:
        q150 = [json.loads(line) for line in f if line.strip()]

    q50_ids = set(q["id"] for q in q50)
    q100_ids = set(q["id"] for q in q100)
    q150_ids = set(q["id"] for q in q150)

    assert len(q50) == 50, f"Expected 50, got {len(q50)}"
    assert len(q100) == 100, f"Expected 100, got {len(q100)}"
    assert len(q150) == 150, f"Expected 150, got {len(q150)}"

    assert len(q50_ids & q100_ids) == 0, f"Overlap between q50 and q100: {len(q50_ids & q100_ids)}"
    assert len(q50_ids & q150_ids) == 0, f"Overlap between q50 and q150: {len(q50_ids & q150_ids)}"
    assert len(q100_ids & q150_ids) == 0, f"Overlap between q100 and q150: {len(q100_ids & q150_ids)}"

    prev_300 = q50_ids | q100_ids | q150_ids
    assert len(prev_300) == 300, f"Union of q50, q100, and q150 must be 300, got {len(prev_300)}"
    print(f"[OK] q50 (50), q100 (100), and q150 (150) loaded: disjoint union = {len(prev_300)}")

    # 2. Candidate evaluation questions & gold references
    eval_q_file = root.parent / "data steps challenge" / "data" / "eval" / "questions.jsonl"
    eval_gold_file = root.parent / "answer" / "Step_Data.jsonl"

    if eval_q_file.exists() and eval_gold_file.exists():
        print(f"Loading candidate questions from {eval_q_file}")
        with open(eval_q_file, encoding="utf-8") as f:
            all_questions = {json.loads(line)["id"]: json.loads(line) for line in f if line.strip()}
        with open(eval_gold_file, encoding="utf-8") as f:
            all_gold = {json.loads(line)["id"]: json.loads(line) for line in f if line.strip()}
        candidate_ids = sorted(list(set(all_questions.keys()) - prev_300))
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

        candidate_ids = sorted(list((eval_ids & set(all_questions.keys())) - prev_300))

    print(f"Total candidate unseen evaluation questions: {len(candidate_ids)}")

    businesses = ["biz_104", "biz_111", "biz_117", "biz_121", "biz_128", "biz_130"]
    rng = random.Random(20261109)

    selected_ids = []
    by_biz_type_selected = defaultdict(Counter)

    for b in businesses:
        b_cands = sorted([qid for qid in candidate_ids if all_questions[qid]["business_id"] == b])
        N_b = len(b_cands)
        assert N_b >= 25, f"Business {b} only has {N_b} remaining candidates, need 25"

        by_type = defaultdict(list)
        for qid in b_cands:
            t = all_gold[qid]["type"]
            by_type[t].append(qid)

        avail_types = sorted(by_type.keys())
        # Allocate min 1 for each available answer type to guarantee full coverage
        quotas = {t: 1 for t in avail_types}
        rem_slots = 25 - len(avail_types)

        # Largest Remainder proportional allocation for remaining slots
        denom = sum(len(by_type[t]) - 1 for t in avail_types)
        remainders = {}
        for t in avail_types:
            exact = rem_slots * (len(by_type[t]) - 1) / denom
            base = int(exact)
            quotas[t] += base
            remainders[t] = exact - base

        deficit = 25 - sum(quotas.values())
        sorted_rem = sorted(remainders.items(), key=lambda x: (-x[1], x[0]))
        for t, _ in sorted_rem[:deficit]:
            quotas[t] += 1

        assert sum(quotas.values()) == 25

        b_selected = []
        for t in avail_types:
            cands_t = sorted(by_type[t])
            rng.shuffle(cands_t)
            k = quotas[t]
            chosen = cands_t[:k]
            b_selected.extend(chosen)
            by_biz_type_selected[b][t] += len(chosen)

        assert len(b_selected) == 25
        selected_ids.extend(b_selected)

    assert len(selected_ids) == 150
    selected_set = set(selected_ids)
    assert len(selected_set) == 150

    # Zero overlap verification
    assert len(selected_set & q50_ids) == 0, f"Overlap with q50: {len(selected_set & q50_ids)}"
    assert len(selected_set & q100_ids) == 0, f"Overlap with q100: {len(selected_set & q100_ids)}"
    assert len(selected_set & q150_ids) == 0, f"Overlap with q150: {len(selected_set & q150_ids)}"
    cumulative_450 = selected_set | prev_300
    assert len(cumulative_450) == 450, f"Cumulative union is {len(cumulative_450)}, expected 450"

    biz_counts_450 = Counter(all_questions[qid]["business_id"] for qid in cumulative_450)
    for b in businesses:
        assert biz_counts_450[b] == 75, f"Business {b} has {biz_counts_450[b]} questions in cumulative 450, expected 75"

    print("[OK] Mathematical proof verified: 50 + 100 + 150 + 150 = 450 distinct questions!")
    print(f"[OK] Exactly 75 questions per business across all 6 businesses: {dict(biz_counts_450)}")

    # Output directory
    out_dir = root / "artifacts" / "live_trammay_eval_450"
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

    # Write questions_150_b4.jsonl and gold_150_b4.jsonl
    q150_b4_path = out_dir / "questions_150_b4.jsonl"
    g150_b4_path = out_dir / "gold_150_b4.jsonl"

    with open(q150_b4_path, "w", encoding="utf-8") as fq, open(g150_b4_path, "w", encoding="utf-8") as fg:
        for qid in selected_ids:
            fq.write(json.dumps(all_questions[qid], ensure_ascii=False) + "\n")
            fg.write(json.dumps(all_gold[qid], ensure_ascii=False) + "\n")

    print(f"[OK] Wrote 150 questions to {q150_b4_path}")
    print(f"[OK] Wrote 150 gold answers to {g150_b4_path}")

    print("\n--- Breakdown by Business & Answer Type for VER-LIVE-450 (Batch 4) ---")
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

