"""Prepare disjoint 800-question held-out set and combined 1,000-question benchmark.

Evaluation target: ag/gpt-oss-120b-medium.
"""

import json
import os
import random
from collections import Counter, defaultdict
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    out_dir = root / "artifacts" / "live_trammay_eval_1000_gptoss"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load the 200 already-evaluated questions and predictions
    p50_path = root / "artifacts" / "live_trammay_eval_50_gptoss_v6" / "predictions.jsonl"
    pn50_path = root / "artifacts" / "live_trammay_eval_next50_gptoss_v4" / "predictions.jsonl"
    pn100_path = root / "artifacts" / "live_trammay_eval_next100_gptoss" / "predictions.jsonl"

    assert p50_path.exists(), f"Missing {p50_path}"
    assert pn50_path.exists(), f"Missing {pn50_path}"
    assert pn100_path.exists(), f"Missing {pn100_path}"

    with open(p50_path, encoding="utf-8") as f:
        p50 = [json.loads(line) for line in f if line.strip()]
    with open(pn50_path, encoding="utf-8") as f:
        pn50 = [json.loads(line) for line in f if line.strip()]
    with open(pn100_path, encoding="utf-8") as f:
        pn100 = [json.loads(line) for line in f if line.strip()]

    assert len(p50) == 50, f"Expected 50 in p50, got {len(p50)}"
    assert len(pn50) == 50, f"Expected 50 in pn50, got {len(pn50)}"
    assert len(pn100) == 100, f"Expected 100 in pn100, got {len(pn100)}"

    seen_200_preds = p50 + pn50 + pn100
    seen_200_ids = [p["id"] for p in seen_200_preds]
    assert len(set(seen_200_ids)) == 200, f"Expected 200 unique seen IDs, got {len(set(seen_200_ids))}"
    seen_200_set = set(seen_200_ids)
    print(f"[OK] Step 1: Loaded 200 prior evaluated predictions (unique IDs: {len(seen_200_set)})")

    # 2. Load split.json, data/train/questions.jsonl, data/train/answers.jsonl
    split_path = root / "artifacts" / "split.json"
    with open(split_path, encoding="utf-8") as f:
        splits_data = json.load(f)

    splits = splits_data["splits"]
    eval_ids = set(splits["evaluation"]["question_ids"])
    calib_ids = set(splits["calibration"]["question_ids"])
    fit_ids = set(splits["fit"]["question_ids"])

    questions_path = root / "data" / "train" / "questions.jsonl"
    answers_path = root / "data" / "train" / "answers.jsonl"

    with open(questions_path, encoding="utf-8") as f:
        all_questions = {q["id"]: q for q in (json.loads(line) for line in f if line.strip())}
    with open(answers_path, encoding="utf-8") as f:
        all_answers = {a["id"]: a for a in (json.loads(line) for line in f if line.strip())}

    assert len(all_questions) == 3000, f"Expected 3000 questions, got {len(all_questions)}"
    assert len(all_answers) == 3000, f"Expected 3000 answers, got {len(all_answers)}"

    # Check seen_200 are all in evaluation
    assert seen_200_set.issubset(eval_ids), "All seen 200 must belong to evaluation split"
    assert len(seen_200_set & fit_ids) == 0, "Seen 200 must not overlap with fit"
    assert len(seen_200_set & calib_ids) == 0, "Seen 200 must not overlap with calib"

    # 3. Select 800 unseen held-out questions
    # Part A: all 509 remaining questions from evaluation
    eval_remaining_ids = sorted(list(eval_ids - seen_200_set))
    assert len(eval_remaining_ids) == 509, f"Expected 509 remaining in eval, got {len(eval_remaining_ids)}"

    # Part B: 291 stratified questions across 6 calibration businesses
    target_calib = 291
    N_calib = len(calib_ids)
    calib_biz_counts = Counter(all_questions[qid]["business_id"] for qid in calib_ids)

    base_quotas = {}
    rem_quotas = {}
    for b, cnt in sorted(calib_biz_counts.items()):
        val = target_calib * cnt / N_calib
        base = int(val)
        base_quotas[b] = base
        rem_quotas[b] = val - base

    deficit = target_calib - sum(base_quotas.values())
    sorted_rem = sorted(rem_quotas.items(), key=lambda x: (-x[1], x[0]))
    for b, _ in sorted_rem[:deficit]:
        base_quotas[b] += 1

    assert sum(base_quotas.values()) == target_calib

    rng = random.Random(20261107)
    calib_selected_ids = []
    by_biz_type_calib = defaultdict(Counter)

    for b, target in sorted(base_quotas.items()):
        b_cands = sorted([qid for qid in calib_ids if all_questions[qid]["business_id"] == b])
        by_type = defaultdict(list)
        for qid in b_cands:
            t = all_answers[qid]["type"]
            by_type[t].append(qid)

        avail_types = sorted(by_type.keys())
        quotas = {t: 1 for t in avail_types}
        rem_slots = target - len(avail_types)
        denom = sum(len(by_type[t]) - 1 for t in avail_types)
        remainders = {}
        for t in avail_types:
            exact = rem_slots * (len(by_type[t]) - 1) / denom if denom > 0 else 0
            base = int(exact)
            quotas[t] += base
            remainders[t] = exact - base

        deficit_t = target - sum(quotas.values())
        sorted_rem_t = sorted(remainders.items(), key=lambda x: (-x[1], x[0]))
        for t, _ in sorted_rem_t[:deficit_t]:
            quotas[t] += 1

        assert sum(quotas.values()) == target

        b_selected = []
        for t in avail_types:
            cands_t = sorted(by_type[t])
            rng.shuffle(cands_t)
            chosen = cands_t[: quotas[t]]
            b_selected.extend(chosen)
            by_biz_type_calib[b][t] += len(chosen)

        assert len(b_selected) == target
        calib_selected_ids.extend(b_selected)

    assert len(calib_selected_ids) == 291
    assert len(set(calib_selected_ids)) == 291

    new_800_ids = set(eval_remaining_ids) | set(calib_selected_ids)
    assert len(new_800_ids) == 800, f"Expected 800 new IDs, got {len(new_800_ids)}"
    assert len(new_800_ids & seen_200_set) == 0, "Overlap between new_800 and seen_200!"
    assert len(new_800_ids & fit_ids) == 0, "Overlap between new_800 and fit!"
    assert len(seen_200_set | new_800_ids) == 1000, f"Union must be 1000, got {len(seen_200_set | new_800_ids)}"
    print("[OK] Step 2: Selected 800 disjoint unseen questions (eval: 509, calib: 291).")

    # 4. Group by business and sort deterministically
    # Sort key: business_id, then id
    all_800_questions = [all_questions[qid] for qid in new_800_ids]
    all_800_questions.sort(key=lambda q: (q["business_id"], q["id"]))

    wave1_questions = all_800_questions[:400]
    wave2_questions = all_800_questions[400:]

    assert len(wave1_questions) == 400
    assert len(wave2_questions) == 400

    w1_ids = set(q["id"] for q in wave1_questions)
    w2_ids = set(q["id"] for q in wave2_questions)
    assert len(w1_ids & w2_ids) == 0, "Overlap between Wave 1 and Wave 2"
    assert (w1_ids | w2_ids) == new_800_ids

    # Write wave 1 and wave 2 files
    w1_path = out_dir / "questions_800_wave1.jsonl"
    w2_path = out_dir / "questions_800_wave2.jsonl"

    with open(w1_path, "w", encoding="utf-8") as f:
        for q in wave1_questions:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")

    with open(w2_path, "w", encoding="utf-8") as f:
        for q in wave2_questions:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")

    print(f"[OK] Wrote Wave 1 questions: {w1_path} ({len(wave1_questions)} items)")
    print(f"[OK] Wrote Wave 2 questions: {w2_path} ({len(wave2_questions)} items)")

    # 5. Build and write complete 1,000-question reference files
    # Reference ordering: seen_200 questions, then wave 1, then wave 2
    seen_200_questions = [all_questions[qid] for qid in seen_200_ids]
    questions_1000 = seen_200_questions + wave1_questions + wave2_questions
    assert len(questions_1000) == 1000
    q1000_ids = [q["id"] for q in questions_1000]
    assert len(set(q1000_ids)) == 1000

    gold_1000 = [all_answers[qid] for qid in q1000_ids]
    assert len(gold_1000) == 1000

    q1000_path = out_dir / "questions_1000.jsonl"
    g1000_path = out_dir / "gold_1000.jsonl"

    with open(q1000_path, "w", encoding="utf-8") as f:
        for q in questions_1000:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")

    with open(g1000_path, "w", encoding="utf-8") as f:
        for g in gold_1000:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")

    print(f"[OK] Wrote combined questions: {q1000_path} (1000 items)")
    print(f"[OK] Wrote combined gold answers: {g1000_path} (1000 items)")

    # Also save gold for wave 1 and wave 2 for convenient per-wave inspection
    g_w1_path = out_dir / "gold_800_wave1.jsonl"
    g_w2_path = out_dir / "gold_800_wave2.jsonl"
    with open(g_w1_path, "w", encoding="utf-8") as f:
        for q in wave1_questions:
            f.write(json.dumps(all_answers[q["id"]], ensure_ascii=False) + "\n")
    with open(g_w2_path, "w", encoding="utf-8") as f:
        for q in wave2_questions:
            f.write(json.dumps(all_answers[q["id"]], ensure_ascii=False) + "\n")

    # Also write metadata summary
    meta = {
        "seen_200_ids": seen_200_ids,
        "wave1_ids": [q["id"] for q in wave1_questions],
        "wave2_ids": [q["id"] for q in wave2_questions],
        "eval_remaining_count": len(eval_remaining_ids),
        "calib_selected_count": len(calib_selected_ids),
        "wave1_by_biz": dict(Counter(q["business_id"] for q in wave1_questions)),
        "wave2_by_biz": dict(Counter(q["business_id"] for q in wave2_questions)),
        "total_by_biz": dict(Counter(q["business_id"] for q in questions_1000)),
        "total_by_type": dict(Counter(g["type"] for g in gold_1000)),
    }
    with open(out_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print("[DONE] Successfully generated all 1,000 reference files!")


if __name__ == "__main__":
    main()
