"""Comprehensive analysis of Standalone New-800 and Cumulative 1,000-Question Benchmark.

Evaluated on ag/gpt-oss-120b-medium.
"""

import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

from bizreason.evaluation.scorer import score_records  # noqa: E402


def load_jsonl(path: Path) -> list:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def count_fast_triage(traces_dir: Path) -> tuple[int, list]:
    c = 0
    ids = []
    if not traces_dir.exists():
        return c, ids
    search_dir = traces_dir / "traces" if (traces_dir / "traces").exists() else traces_dir
    for p in search_dir.glob("*.json"):
        with open(p, encoding="utf-8") as f:
            try:
                td = json.load(f)
                if td.get("model_calls") == 0:
                    c += 1
                    ids.append(td.get("question", {}).get("id"))
            except Exception:
                pass
    return c, ids


def macro_pa(by_business: dict) -> float:
    pas = [b["pa"] for b in by_business.values() if b["n"] > 0]
    return sum(pas) / len(pas) if pas else 0.0


def main() -> None:
    os.chdir(root)
    eval_1000_dir = root / "artifacts" / "live_trammay_eval_1000_gptoss"

    # 1. Define paths for prior 200 questions
    b1_dir = root / "artifacts" / "live_trammay_eval_50_gptoss_v6"
    b1_q_path = root / "artifacts" / "live_trammay_eval_50" / "questions_50.jsonl"
    b1_g_path = root / "artifacts" / "live_trammay_eval_50" / "gold_50.jsonl"
    b1_p_path = b1_dir / "predictions.jsonl"

    b2_dir = root / "artifacts" / "live_trammay_eval_next50_gptoss_v4"
    b2_q_path = root / "artifacts" / "live_trammay_eval_next50_gptoss" / "questions_next50.jsonl"
    b2_g_path = root / "artifacts" / "live_trammay_eval_next50_gptoss" / "gold_next50.jsonl"
    b2_p_path = b2_dir / "predictions.jsonl"

    n100_dir = root / "artifacts" / "live_trammay_eval_next100_gptoss"
    n100_q_path = n100_dir / "questions_next100.jsonl"
    n100_g_path = n100_dir / "gold_next100.jsonl"
    n100_p_path = n100_dir / "predictions.jsonl"

    # 2. Define paths for wave 1 and wave 2
    w1_q_path = eval_1000_dir / "questions_800_wave1.jsonl"
    w1_g_path = eval_1000_dir / "gold_800_wave1.jsonl"
    w1_p_path = eval_1000_dir / "predictions_wave1.jsonl"

    w2_q_path = eval_1000_dir / "questions_800_wave2.jsonl"
    w2_g_path = eval_1000_dir / "gold_800_wave2.jsonl"
    w2_p_path = eval_1000_dir / "predictions_wave2.jsonl"

    # Reference paths for combined 1000
    q1000_path = eval_1000_dir / "questions_1000.jsonl"
    g1000_path = eval_1000_dir / "gold_1000.jsonl"
    p1000_path = eval_1000_dir / "predictions_1000.jsonl"

    # Verify all prediction files exist
    assert b1_p_path.exists(), f"Batch 1 V6 predictions missing: {b1_p_path}"
    assert b2_p_path.exists(), f"Batch 2 V4 predictions missing: {b2_p_path}"
    assert n100_p_path.exists(), f"Next-100 predictions missing: {n100_p_path}"
    assert w1_p_path.exists(), f"Wave 1 predictions missing: {w1_p_path}"
    assert w2_p_path.exists(), f"Wave 2 predictions missing: {w2_p_path}"

    # Load datasets
    b1_q = load_jsonl(b1_q_path)
    b1_g = load_jsonl(b1_g_path)
    b1_p = load_jsonl(b1_p_path)

    b2_q = load_jsonl(b2_q_path)
    b2_g = load_jsonl(b2_g_path)
    b2_p = load_jsonl(b2_p_path)

    n100_q = load_jsonl(n100_q_path)
    n100_g = load_jsonl(n100_g_path)
    n100_p = load_jsonl(n100_p_path)

    w1_q = load_jsonl(w1_q_path)
    w1_g = load_jsonl(w1_g_path)
    w1_p = load_jsonl(w1_p_path)

    w2_q = load_jsonl(w2_q_path)
    w2_g = load_jsonl(w2_g_path)
    w2_p = load_jsonl(w2_p_path)

    q1000 = load_jsonl(q1000_path)
    g1000 = load_jsonl(g1000_path)

    assert len(b1_p) == 50
    assert len(b2_p) == 50
    assert len(n100_p) == 100
    assert len(w1_p) == 400
    assert len(w2_p) == 400
    assert len(q1000) == 1000
    assert len(g1000) == 1000

    # Map all predictions by id
    all_preds_by_id = {}
    for p in b1_p + b2_p + n100_p + w1_p + w2_p:
        all_preds_by_id[p["id"]] = p

    assert len(all_preds_by_id) == 1000, f"Expected 1000 unique predictions, got {len(all_preds_by_id)}"

    # Build predictions_1000 matching questions_1000 order
    p1000 = []
    for q in q1000:
        qid = q["id"]
        assert qid in all_preds_by_id, f"Missing prediction for question ID {qid}"
        p1000.append(all_preds_by_id[qid])

    with open(p1000_path, "w", encoding="utf-8") as f:
        for p in p1000:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    print(f"[OK] Wrote merged predictions_1000.jsonl: {p1000_path} (1,000 items)")

    # 3. Official CLI Scorer on 1000 questions
    official_score_cmd = [
        sys.executable,
        "-m",
        "bizreason.cli",
        "score",
        "--pred",
        str(p1000_path),
        "--gold",
        str(g1000_path),
        "--questions",
        str(q1000_path),
        "--out",
        str(eval_1000_dir / "score_report_1000.json"),
    ]
    cli_proc = subprocess.run(official_score_cmd)
    assert cli_proc.returncode == 0, f"Official CLI scoring failed with code {cli_proc.returncode}"
    print("[OK] Official CLI scoring completed successfully into score_report_1000.json")

    # 4. Standalone scoring on New-800
    new800_p = w1_p + w2_p
    new800_g = w1_g + w2_g
    new800_q = w1_q + w2_q

    rep_new800 = score_records(new800_p, new800_g, new800_q)
    with open(eval_1000_dir / "score_report_new800.json", "w", encoding="utf-8") as f:
        json.dump(rep_new800, f, indent=2, ensure_ascii=False)
    print("[OK] Computed standalone score report for new 800 into score_report_new800.json")

    # 5. Milestone scoring for breakdown
    rep_b1 = score_records(b1_p, b1_g, b1_q)
    rep_b2 = score_records(b2_p, b2_g, b2_q)
    rep_n100 = score_records(n100_p, n100_g, n100_q)
    rep_prior200 = score_records(b1_p + b2_p + n100_p, b1_g + b2_g + n100_q, b1_q + b2_q + n100_q)
    rep_w1 = score_records(w1_p, w1_g, w1_q)
    rep_w2 = score_records(w2_p, w2_g, w2_q)
    rep_cum1000 = score_records(p1000, g1000, q1000)

    # Fast-Triage counts
    c_ft_b1, _ = count_fast_triage(b1_dir / "traces")
    c_ft_b2, _ = count_fast_triage(b2_dir / "traces")
    c_ft_n100, _ = count_fast_triage(n100_dir / "traces")
    c_ft_w1, ids_ft_w1 = count_fast_triage(eval_1000_dir / "traces_wave1")
    c_ft_w2, ids_ft_w2 = count_fast_triage(eval_1000_dir / "traces_wave2")

    total_ft_new800 = c_ft_w1 + c_ft_w2
    total_ft_1000 = c_ft_b1 + c_ft_b2 + c_ft_n100 + total_ft_new800

    # Macro PAs
    macro_prior200 = macro_pa(rep_prior200["by_business"])
    macro_w1 = macro_pa(rep_w1["by_business"])
    macro_w2 = macro_pa(rep_w2["by_business"])
    macro_new800 = macro_pa(rep_new800["by_business"])
    macro_1000 = macro_pa(rep_cum1000["by_business"])

    # Timings
    def get_time(p: Path) -> float:
        if p.exists():
            with open(p, encoding="utf-8") as tf:
                return json.load(tf).get("elapsed_seconds", 0.0)
        return 0.0

    t_b1 = get_time(b1_dir / "execution_time.json")
    t_b2 = get_time(b2_dir / "execution_time.json")
    t_n100 = get_time(n100_dir / "execution_time.json")
    t_w1 = get_time(eval_1000_dir / "execution_time_wave1.json")
    t_w2 = get_time(eval_1000_dir / "execution_time_wave2.json")

    # =========================================================================
    # DISPLAY PART 1: PROGRESSION & OVERVIEW
    # =========================================================================
    print("\n" + "=" * 95)
    print("LIVE EVALUATION RESULTS: ag/gpt-oss-120b-medium (BENCHMARK PROGRESSION)")
    print("=" * 95)
    print(
        f"{'Milestone':<24} {'N':>4} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'MacroPA':>8} {'Coverage':>9} {'AnsAcc':>8} {'WallTime':>9}"
    )
    print("-" * 95)
    print(
        f"{'Batch 1 V6 (q50)':<24} {50:>4} {rep_b1['correct']:>8} {rep_b1['wrong']:>6} {rep_b1['abstain']:>8} {rep_b1['pa']:>8.4f} {macro_pa(rep_b1['by_business']):>8.4f} {rep_b1['coverage']:>9.4f} {rep_b1['answered_accuracy']:>8.4f} {f'{t_b1:.2f}s':>9}"
    )
    print(
        f"{'Batch 2 V4 (qn50)':<24} {50:>4} {rep_b2['correct']:>8} {rep_b2['wrong']:>6} {rep_b2['abstain']:>8} {rep_b2['pa']:>8.4f} {macro_pa(rep_b2['by_business']):>8.4f} {rep_b2['coverage']:>9.4f} {rep_b2['answered_accuracy']:>8.4f} {f'{t_b2:.2f}s':>9}"
    )
    print(
        f"{'Next-100 (qn100)':<24} {100:>4} {rep_n100['correct']:>8} {rep_n100['wrong']:>6} {rep_n100['abstain']:>8} {rep_n100['pa']:>8.4f} {macro_pa(rep_n100['by_business']):>8.4f} {rep_n100['coverage']:>9.4f} {rep_n100['answered_accuracy']:>8.4f} {f'{t_n100:.2f}s':>9}"
    )
    print(
        f"{'Prior 200 (B1+B2+N100)':<24} {200:>4} {rep_prior200['correct']:>8} {rep_prior200['wrong']:>6} {rep_prior200['abstain']:>8} {rep_prior200['pa']:>8.4f} {macro_prior200:>8.4f} {rep_prior200['coverage']:>9.4f} {rep_prior200['answered_accuracy']:>8.4f} {f'{t_b1+t_b2+t_n100:.2f}s':>9}"
    )
    print("-" * 95)
    print(
        f"{'Wave 1 (400 Qs)':<24} {400:>4} {rep_w1['correct']:>8} {rep_w1['wrong']:>6} {rep_w1['abstain']:>8} {rep_w1['pa']:>8.4f} {macro_w1:>8.4f} {rep_w1['coverage']:>9.4f} {rep_w1['answered_accuracy']:>8.4f} {f'{t_w1:.2f}s':>9}"
    )
    print(
        f"{'Wave 2 (400 Qs)':<24} {400:>4} {rep_w2['correct']:>8} {rep_w2['wrong']:>6} {rep_w2['abstain']:>8} {rep_w2['pa']:>8.4f} {macro_w2:>8.4f} {rep_w2['coverage']:>9.4f} {rep_w2['answered_accuracy']:>8.4f} {f'{t_w2:.2f}s':>9}"
    )
    print(
        f"{'New 800 (W1+W2)':<24} {800:>4} {rep_new800['correct']:>8} {rep_new800['wrong']:>6} {rep_new800['abstain']:>8} {rep_new800['pa']:>8.4f} {macro_new800:>8.4f} {rep_new800['coverage']:>9.4f} {rep_new800['answered_accuracy']:>8.4f} {f'{t_w1+t_w2:.2f}s':>9}"
    )
    print("=" * 95)
    print(
        f"{'CUMULATIVE 1,000':<24} {1000:>4} {rep_cum1000['correct']:>8} {rep_cum1000['wrong']:>6} {rep_cum1000['abstain']:>8} {rep_cum1000['pa']:>8.4f} {macro_1000:>8.4f} {rep_cum1000['coverage']:>9.4f} {rep_cum1000['answered_accuracy']:>8.4f} {f'{t_b1+t_b2+t_n100+t_w1+t_w2:.2f}s':>9}"
    )
    print("=" * 95)
    print(f"Fast-Triage (0 LLM calls): New 800 = {total_ft_new800}/800, Cumulative 1,000 = {total_ft_1000}/1000")

    # =========================================================================
    # DISPLAY PART 2: STANDALONE NEW-800 BREAKDOWNS
    # =========================================================================
    print("\n" + "=" * 80)
    print("STANDALONE NEW-800: PER-BUSINESS BREAKDOWN (N=800)")
    print("=" * 80)
    print(
        f"{'Business':<15} {'N':>4} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'Coverage':>9} {'AnsAcc':>8}"
    )
    print("-" * 80)
    for biz, stats in sorted(rep_new800["by_business"].items()):
        ans_acc = stats.get("answered_accuracy")
        ans_acc_str = f"{ans_acc:.4f}" if ans_acc is not None else "N/A"
        print(
            f"{biz:<15} {stats['n']:>4} {stats['correct']:>8} {stats['wrong']:>6} {stats['abstain']:>8} {stats['pa']:>8.4f} {stats['coverage']:>9.4f} {ans_acc_str:>8}"
        )
    print("-" * 80)
    print(
        f"{'OVERALL':<15} {rep_new800['n']:>4} {rep_new800['correct']:>8} {rep_new800['wrong']:>6} {rep_new800['abstain']:>8} {rep_new800['pa']:>8.4f} {rep_new800['coverage']:>9.4f} {rep_new800['answered_accuracy']:>8.4f}"
    )
    print(f"Business-Macro PA: {macro_new800:.4f}")
    print("=" * 80)

    print("\n" + "=" * 80)
    print("STANDALONE NEW-800: PER-ANSWER-TYPE BREAKDOWN (N=800)")
    print("=" * 80)
    print(
        f"{'Type':<15} {'N':>4} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'Coverage':>9} {'AnsAcc':>8}"
    )
    print("-" * 80)
    for qtype, stats in sorted(rep_new800["by_type"].items()):
        ans_acc = stats.get("answered_accuracy")
        ans_acc_str = f"{ans_acc:.4f}" if ans_acc is not None else "N/A"
        print(
            f"{qtype:<15} {stats['n']:>4} {stats['correct']:>8} {stats['wrong']:>6} {stats['abstain']:>8} {stats['pa']:>8.4f} {stats['coverage']:>9.4f} {ans_acc_str:>8}"
        )
    print("-" * 80)
    print(
        f"{'OVERALL':<15} {rep_new800['n']:>4} {rep_new800['correct']:>8} {rep_new800['wrong']:>6} {rep_new800['abstain']:>8} {rep_new800['pa']:>8.4f} {rep_new800['coverage']:>9.4f} {rep_new800['answered_accuracy']:>8.4f}"
    )
    print("=" * 80)

    # =========================================================================
    # DISPLAY PART 3: FULL 1,000-QUESTION BREAKDOWNS
    # =========================================================================
    print("\n" + "=" * 80)
    print("FULL 1,000 BENCHMARK: PER-BUSINESS BREAKDOWN (N=1,000 across 12 businesses)")
    print("=" * 80)
    print(
        f"{'Business':<15} {'N':>4} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'Coverage':>9} {'AnsAcc':>8}"
    )
    print("-" * 80)
    for biz, stats in sorted(rep_cum1000["by_business"].items()):
        ans_acc = stats.get("answered_accuracy")
        ans_acc_str = f"{ans_acc:.4f}" if ans_acc is not None else "N/A"
        print(
            f"{biz:<15} {stats['n']:>4} {stats['correct']:>8} {stats['wrong']:>6} {stats['abstain']:>8} {stats['pa']:>8.4f} {stats['coverage']:>9.4f} {ans_acc_str:>8}"
        )
    print("-" * 80)
    print(
        f"{'OVERALL':<15} {rep_cum1000['n']:>4} {rep_cum1000['correct']:>8} {rep_cum1000['wrong']:>6} {rep_cum1000['abstain']:>8} {rep_cum1000['pa']:>8.4f} {rep_cum1000['coverage']:>9.4f} {rep_cum1000['answered_accuracy']:>8.4f}"
    )
    print(f"Business-Macro PA: {macro_1000:.4f}")
    print("=" * 80)

    print("\n" + "=" * 80)
    print("FULL 1,000 BENCHMARK: PER-ANSWER-TYPE BREAKDOWN (N=1,000 across 8 types)")
    print("=" * 80)
    print(
        f"{'Type':<15} {'N':>4} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'Coverage':>9} {'AnsAcc':>8}"
    )
    print("-" * 80)
    for qtype, stats in sorted(rep_cum1000["by_type"].items()):
        ans_acc = stats.get("answered_accuracy")
        ans_acc_str = f"{ans_acc:.4f}" if ans_acc is not None else "N/A"
        print(
            f"{qtype:<15} {stats['n']:>4} {stats['correct']:>8} {stats['wrong']:>6} {stats['abstain']:>8} {stats['pa']:>8.4f} {stats['coverage']:>9.4f} {ans_acc_str:>8}"
        )
    print("-" * 80)
    print(
        f"{'OVERALL':<15} {rep_cum1000['n']:>4} {rep_cum1000['correct']:>8} {rep_cum1000['wrong']:>6} {rep_cum1000['abstain']:>8} {rep_cum1000['pa']:>8.4f} {rep_cum1000['coverage']:>9.4f} {rep_cum1000['answered_accuracy']:>8.4f}"
    )
    print("=" * 80)

    # =========================================================================
    # DISPLAY PART 4: ROOT-CAUSE FORENSIC ANALYSIS OF ANY NON-CORRECT CASES
    # =========================================================================
    non_correct_1000 = [d for d in rep_cum1000["details"] if d["outcome"] != "correct"]
    print(f"\nFULL 1,000 BENCHMARK NON-CORRECT CASES ({len(non_correct_1000)} total):")

    q_map = {q["id"]: q for q in q1000}
    g_map = {g["id"]: g for g in g1000}
    p_map = {p["id"]: p for p in p1000}

    # Gather trace info if available
    trace_dirs = [
        b1_dir / "traces",
        b2_dir / "traces",
        n100_dir / "traces",
        eval_1000_dir / "traces_wave1",
        eval_1000_dir / "traces_wave2",
    ]
    all_traces = {}
    for td in trace_dirs:
        sdir = td / "traces" if (td / "traces").exists() else td
        if sdir.exists():
            for p in sdir.glob("*.json"):
                try:
                    with open(p, encoding="utf-8") as f:
                        data = json.load(f)
                        qid = data.get("question", {}).get("id")
                        if qid:
                            all_traces[qid] = data
                except Exception:
                    pass

    failure_categories = defaultdict(list)
    for d in non_correct_1000:
        qid = d["id"]
        q_item = q_map.get(qid, {})
        g_item = g_map.get(qid, {})
        p_item = p_map.get(qid, {})
        tr = all_traces.get(qid, {})

        outcome = d["outcome"]
        q_text = q_item.get("question", "")
        g_val = g_item.get("value")
        p_val = p_item.get("value")
        g_type = g_item.get("type")
        biz = q_item.get("business_id", "")

        # Categorize
        if outcome == "abstain":
            cat = "Safety Abstain / Fallback"
        elif g_type == "percent":
            cat = "Percentage Scaling / Rounding"
        elif g_type == "date":
            cat = "Date Formatting / ISO Format"
        elif g_type == "list":
            cat = "List Order / Missing Element"
        elif g_type == "table":
            cat = "Table Column Alignment"
        elif g_type == "unanswerable":
            cat = "Unanswerable Gate Misclassification"
        elif g_type == "number":
            cat = "Numerical Calculation / Filtering"
        else:
            cat = "Other Semantic Discrepancy"

        failure_categories[cat].append({
            "id": qid,
            "business": biz,
            "outcome": outcome,
            "type": g_type,
            "question": q_text[:80] + "..." if len(q_text) > 80 else q_text,
            "gold": str(g_val)[:60],
            "pred": str(p_val)[:60],
            "trace_gate": tr.get("gate"),
            "trace_calls": tr.get("model_calls"),
        })

    print("-" * 80)
    print("FAILURE TAXONOMY BREAKDOWN:")
    for cat, items in sorted(failure_categories.items(), key=lambda x: -len(x[1])):
        print(f"  * {cat}: {len(items)} cases ({len(items)/len(p1000)*100:.2f}%)")
        for item in items[:3]:
            print(f"      - [{item['id']}][{item['business']}][{item['type']}] Gold: {item['gold']} | Pred: {item['pred']}")
        if len(items) > 3:
            print(f"      - ... and {len(items)-3} more")

    # Save complete detailed analysis report
    summary_data = {
        "benchmark": "1000-question held-out evaluation",
        "model": "ag/gpt-oss-120b-medium",
        "total_questions": 1000,
        "new_questions": 800,
        "prior_questions": 200,
        "cumulative_1000": {
            "correct": rep_cum1000["correct"],
            "wrong": rep_cum1000["wrong"],
            "abstain": rep_cum1000["abstain"],
            "pa": rep_cum1000["pa"],
            "macro_pa": macro_1000,
            "coverage": rep_cum1000["coverage"],
            "answered_accuracy": rep_cum1000["answered_accuracy"],
            "total_fast_triage": total_ft_1000,
            "by_business": rep_cum1000["by_business"],
            "by_type": rep_cum1000["by_type"],
        },
        "standalone_new800": {
            "correct": rep_new800["correct"],
            "wrong": rep_new800["wrong"],
            "abstain": rep_new800["abstain"],
            "pa": rep_new800["pa"],
            "macro_pa": macro_new800,
            "coverage": rep_new800["coverage"],
            "answered_accuracy": rep_new800["answered_accuracy"],
            "total_fast_triage": total_ft_new800,
            "by_business": rep_new800["by_business"],
            "by_type": rep_new800["by_type"],
        },
        "wave1": {
            "correct": rep_w1["correct"],
            "wrong": rep_w1["wrong"],
            "abstain": rep_w1["abstain"],
            "pa": rep_w1["pa"],
            "macro_pa": macro_w1,
            "wall_time": t_w1,
        },
        "wave2": {
            "correct": rep_w2["correct"],
            "wrong": rep_w2["wrong"],
            "abstain": rep_w2["abstain"],
            "pa": rep_w2["pa"],
            "macro_pa": macro_w2,
            "wall_time": t_w2,
        },
        "failure_categories": {k: len(v) for k, v in failure_categories.items()},
        "non_correct_cases": failure_categories,
    }

    with open(eval_1000_dir / "comprehensive_analysis_1000.json", "w", encoding="utf-8") as f:
        json.dump(summary_data, f, indent=2, ensure_ascii=False)

    print(f"\n[DONE] Saved full analysis into {eval_1000_dir / 'comprehensive_analysis_1000.json'}")


if __name__ == "__main__":
    main()
