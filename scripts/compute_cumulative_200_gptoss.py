"""Comprehensive analysis of Standalone Next-100 and Cumulative 200-Question Benchmark.

Evaluated on ag/gpt-oss-120b-medium.
"""

import json
import sys
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
    # 1. Define paths
    b1_dir = root / "artifacts" / "live_trammay_eval_50_gptoss_v6"
    b1_q_path = root / "artifacts" / "live_trammay_eval_50" / "questions_50.jsonl"
    b1_g_path = root / "artifacts" / "live_trammay_eval_50" / "gold_50.jsonl"
    b1_p_path = b1_dir / "predictions.jsonl"

    b2_dir = root / "artifacts" / "live_trammay_eval_next50_gptoss_v4"
    b2_q_path = (
        root
        / "artifacts"
        / "live_trammay_eval_next50_gptoss"
        / "questions_next50.jsonl"
    )
    b2_g_path = (
        root
        / "artifacts"
        / "live_trammay_eval_next50_gptoss"
        / "gold_next50.jsonl"
    )
    b2_p_path = b2_dir / "predictions.jsonl"

    n100_dir = root / "artifacts" / "live_trammay_eval_next100_gptoss"
    n100_q_path = n100_dir / "questions_next100.jsonl"
    n100_g_path = n100_dir / "gold_next100.jsonl"
    n100_p_path = n100_dir / "predictions.jsonl"

    # Verify predictions exist
    assert n100_p_path.exists(), f"Next-100 predictions missing: {n100_p_path}"
    assert b1_p_path.exists(), f"Batch 1 V6 predictions missing: {b1_p_path}"
    assert b2_p_path.exists(), f"Batch 2 V4 predictions missing: {b2_p_path}"

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

    # Assert lengths
    assert len(b1_q) == 50 and len(b1_p) == 50
    assert len(b2_q) == 50 and len(b2_p) == 50
    assert len(n100_q) == 100 and len(n100_p) == 100

    # Disjointness checks
    id_b1 = set(q["id"] for q in b1_q)
    id_b2 = set(q["id"] for q in b2_q)
    id_n100 = set(q["id"] for q in n100_q)

    assert len(id_b1 & id_b2) == 0, "Overlap between Batch 1 and Batch 2"
    assert len(id_n100 & id_b1) == 0, "Overlap between Next-100 and Batch 1"
    assert len(id_n100 & id_b2) == 0, "Overlap between Next-100 and Batch 2"
    cum_ids = id_b1 | id_b2 | id_n100
    assert len(cum_ids) == 200, f"Expected 200 cumulative unique IDs, got {len(cum_ids)}"

    # Score standalone milestones
    rep_b1 = score_records(b1_p, b1_g, b1_q)
    rep_b2 = score_records(b2_p, b2_g, b2_q)
    rep_n100 = score_records(n100_p, n100_g, n100_q)

    # Score Prior 100 (B1 + B2)
    rep_prior100 = score_records(b1_p + b2_p, b1_g + b2_g, b1_q + b2_q)

    # Score Cumulative 200
    all_p = b1_p + b2_p + n100_p
    all_g = b1_g + b2_g + n100_g
    all_q = b1_q + b2_q + n100_q
    rep_cum200 = score_records(all_p, all_g, all_q)

    # Timings
    t_b1 = 0.0
    t_b2 = 0.0
    t_n100 = 0.0
    if (b1_dir / "execution_time.json").exists():
        with open(b1_dir / "execution_time.json", encoding="utf-8") as f:
            t_b1 = json.load(f).get("elapsed_seconds", 0.0)
    if (b2_dir / "execution_time.json").exists():
        with open(b2_dir / "execution_time.json", encoding="utf-8") as f:
            t_b2 = json.load(f).get("elapsed_seconds", 0.0)
    if (n100_dir / "execution_time.json").exists():
        with open(n100_dir / "execution_time.json", encoding="utf-8") as f:
            t_n100 = json.load(f).get("elapsed_seconds", 0.0)

    # Fast Triage counts
    c_ft_b1, ids_ft_b1 = count_fast_triage(b1_dir / "traces")
    c_ft_b2, ids_ft_b2 = count_fast_triage(b2_dir / "traces")
    c_ft_n100, ids_ft_n100 = count_fast_triage(n100_dir / "traces")
    total_ft_200 = c_ft_b1 + c_ft_b2 + c_ft_n100

    # Macro PAs
    macro_b1 = macro_pa(rep_b1["by_business"])
    macro_b2 = macro_pa(rep_b2["by_business"])
    macro_prior100 = macro_pa(rep_prior100["by_business"])
    macro_n100 = macro_pa(rep_n100["by_business"])
    macro_cum200 = macro_pa(rep_cum200["by_business"])

    # Load traces for Next-100 analysis
    n100_trace_dir = n100_dir / "traces"
    if (n100_trace_dir / "traces").exists():
        n100_trace_dir = n100_trace_dir / "traces"

    traces_n100 = {}
    if n100_trace_dir.exists():
        for tp in n100_trace_dir.glob("*.json"):
            with open(tp, encoding="utf-8") as tf:
                try:
                    td = json.load(tf)
                    qid = td.get("question", {}).get("id")
                    if qid:
                        traces_n100[qid] = td
                except Exception:
                    pass

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
        f"{'Batch 1 V6 (q50)':<24} {50:>4} {rep_b1['correct']:>8} {rep_b1['wrong']:>6} {rep_b1['abstain']:>8} {rep_b1['pa']:>8.4f} {macro_b1:>8.4f} {rep_b1['coverage']:>9.4f} {rep_b1['answered_accuracy']:>8.4f} {f'{t_b1:.2f}s':>9}"
    )
    print(
        f"{'Batch 2 V4 (q_next50)':<24} {50:>4} {rep_b2['correct']:>8} {rep_b2['wrong']:>6} {rep_b2['abstain']:>8} {rep_b2['pa']:>8.4f} {macro_b2:>8.4f} {rep_b2['coverage']:>9.4f} {rep_b2['answered_accuracy']:>8.4f} {f'{t_b2:.2f}s':>9}"
    )
    print(
        f"{'Prior 100 (B1+B2)':<24} {100:>4} {rep_prior100['correct']:>8} {rep_prior100['wrong']:>6} {rep_prior100['abstain']:>8} {rep_prior100['pa']:>8.4f} {macro_prior100:>8.4f} {rep_prior100['coverage']:>9.4f} {rep_prior100['answered_accuracy']:>8.4f} {f'{t_b1+t_b2:.2f}s':>9}"
    )
    print("-" * 95)
    print(
        f"{'Next-100 (Unseen Held-out)':<24} {100:>4} {rep_n100['correct']:>8} {rep_n100['wrong']:>6} {rep_n100['abstain']:>8} {rep_n100['pa']:>8.4f} {macro_n100:>8.4f} {rep_n100['coverage']:>9.4f} {rep_n100['answered_accuracy']:>8.4f} {f'{t_n100:.2f}s':>9}"
    )
    print("=" * 95)
    print(
        f"{'CUMULATIVE 200 (B1+B2+N100)':<24} {200:>4} {rep_cum200['correct']:>8} {rep_cum200['wrong']:>6} {rep_cum200['abstain']:>8} {rep_cum200['pa']:>8.4f} {macro_cum200:>8.4f} {rep_cum200['coverage']:>9.4f} {rep_cum200['answered_accuracy']:>8.4f} {f'{t_b1+t_b2+t_n100:.2f}s':>9}"
    )
    print("=" * 95)
    print(
        f"Fast-Triage (0 LLM calls): Next-100 = {c_ft_n100}/100, Cumulative 200 = {total_ft_200}/200"
    )
    if ids_ft_n100:
        print(f"Next-100 Fast-Triage IDs: {ids_ft_n100}")

    # =========================================================================
    # DISPLAY PART 2: STANDALONE NEXT-100 BREAKDOWNS
    # =========================================================================
    print("\n" + "=" * 80)
    print("STANDALONE NEXT-100: PER-BUSINESS BREAKDOWN (N=100)")
    print("=" * 80)
    print(
        f"{'Business':<15} {'N':>4} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'Coverage':>9} {'AnsAcc':>8}"
    )
    print("-" * 80)
    for biz, stats in rep_n100["by_business"].items():
        ans_acc = stats.get("answered_accuracy")
        ans_acc_str = f"{ans_acc:.4f}" if ans_acc is not None else "N/A"
        print(
            f"{biz:<15} {stats['n']:>4} {stats['correct']:>8} {stats['wrong']:>6} {stats['abstain']:>8} {stats['pa']:>8.4f} {stats['coverage']:>9.4f} {ans_acc_str:>8}"
        )
    print("-" * 80)
    print(
        f"{'OVERALL':<15} {rep_n100['n']:>4} {rep_n100['correct']:>8} {rep_n100['wrong']:>6} {rep_n100['abstain']:>8} {rep_n100['pa']:>8.4f} {rep_n100['coverage']:>9.4f} {rep_n100['answered_accuracy']:>8.4f}"
    )
    print(f"Business-Macro PA: {macro_n100:.4f}")
    print("=" * 80)

    print("\n" + "=" * 80)
    print("STANDALONE NEXT-100: PER-ANSWER-TYPE BREAKDOWN (N=100)")
    print("=" * 80)
    print(
        f"{'Type':<15} {'N':>4} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'Coverage':>9} {'AnsAcc':>8}"
    )
    print("-" * 80)
    for qtype, stats in rep_n100["by_type"].items():
        ans_acc = stats.get("answered_accuracy")
        ans_acc_str = f"{ans_acc:.4f}" if ans_acc is not None else "N/A"
        print(
            f"{qtype:<15} {stats['n']:>4} {stats['correct']:>8} {stats['wrong']:>6} {stats['abstain']:>8} {stats['pa']:>8.4f} {stats['coverage']:>9.4f} {ans_acc_str:>8}"
        )
    print("-" * 80)
    print(
        f"{'OVERALL':<15} {rep_n100['n']:>4} {rep_n100['correct']:>8} {rep_n100['wrong']:>6} {rep_n100['abstain']:>8} {rep_n100['pa']:>8.4f} {rep_n100['coverage']:>9.4f} {rep_n100['answered_accuracy']:>8.4f}"
    )
    print("=" * 80)

    # Next-100 Non-Correct Case Summaries
    q_map = {q["id"]: q for q in n100_q}
    g_map = {g["id"]: g for g in n100_g}
    p_map = {p["id"]: p for p in n100_p}

    n100_non_correct = [d for d in rep_n100["details"] if d["outcome"] != "correct"]
    print(
        f"\nSTANDALONE NEXT-100 NON-CORRECT CASES ({len(n100_non_correct)} total):"
    )
    case_summaries = []
    if not n100_non_correct:
        print("  None! (100% Perfect Accuracy)")
    else:
        for idx, item in enumerate(n100_non_correct, 1):
            qid = item["id"]
            tr = traces_n100.get(qid, {})
            biz = item.get("business_id", tr.get("question", {}).get("business_id", "unknown"))
            qtext = q_map.get(qid, {}).get("question", "")
            g_ans = g_map.get(qid, {}).get("answer")
            p_ans = p_map.get(qid, {}).get("answer")
            p_abstain = p_map.get(qid, {}).get("abstain", False)
            route = tr.get("route", "N/A")
            calls = tr.get("model_calls", 0)

            reason = tr.get("answer_summary", "") or tr.get("reasoning", "") or tr.get("error", "")
            if isinstance(reason, list):
                reason = " | ".join(str(r) for r in reason)
            reason_str = str(reason).strip().replace("\n", " ")
            if len(reason_str) > 140:
                reason_str = reason_str[:137] + "..."

            print(f"[{idx:>2}] ID: {qid} | Biz: {biz} | Outcome: {item['outcome'].upper()} | Type: {item.get('type')}")
            print(f"     Q: {qtext[:100]}...")
            print(f"     Gold: {g_ans} | Pred: {p_ans} (Abstain={p_abstain}) | Route: {route} | Calls: {calls}")
            if reason_str:
                print(f"     Trace Note: {reason_str}")

            case_summaries.append({
                "id": qid,
                "business_id": biz,
                "outcome": item["outcome"],
                "type": item.get("type"),
                "question": qtext,
                "gold": g_ans,
                "prediction": p_ans,
                "abstain": p_abstain,
                "route": route,
                "model_calls": calls,
                "trace_note": reason_str,
            })

    # =========================================================================
    # DISPLAY PART 3: CUMULATIVE 200-QUESTION BREAKDOWNS
    # =========================================================================
    print("\n" + "=" * 80)
    print("CUMULATIVE 200-QUESTION BENCHMARK: PER-BUSINESS BREAKDOWN (N=200)")
    print("=" * 80)
    print(
        f"{'Business':<15} {'N':>4} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'Coverage':>9} {'AnsAcc':>8}"
    )
    print("-" * 80)
    for biz, stats in rep_cum200["by_business"].items():
        ans_acc = stats.get("answered_accuracy")
        ans_acc_str = f"{ans_acc:.4f}" if ans_acc is not None else "N/A"
        print(
            f"{biz:<15} {stats['n']:>4} {stats['correct']:>8} {stats['wrong']:>6} {stats['abstain']:>8} {stats['pa']:>8.4f} {stats['coverage']:>9.4f} {ans_acc_str:>8}"
        )
    print("-" * 80)
    print(
        f"{'OVERALL':<15} {rep_cum200['n']:>4} {rep_cum200['correct']:>8} {rep_cum200['wrong']:>6} {rep_cum200['abstain']:>8} {rep_cum200['pa']:>8.4f} {rep_cum200['coverage']:>9.4f} {rep_cum200['answered_accuracy']:>8.4f}"
    )
    print(f"Business-Macro PA: {macro_cum200:.4f}")
    print("=" * 80)

    print("\n" + "=" * 80)
    print("CUMULATIVE 200-QUESTION BENCHMARK: PER-ANSWER-TYPE BREAKDOWN (N=200)")
    print("=" * 80)
    print(
        f"{'Type':<15} {'N':>4} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'Coverage':>9} {'AnsAcc':>8}"
    )
    print("-" * 80)
    for qtype, stats in rep_cum200["by_type"].items():
        ans_acc = stats.get("answered_accuracy")
        ans_acc_str = f"{ans_acc:.4f}" if ans_acc is not None else "N/A"
        print(
            f"{qtype:<15} {stats['n']:>4} {stats['correct']:>8} {stats['wrong']:>6} {stats['abstain']:>8} {stats['pa']:>8.4f} {stats['coverage']:>9.4f} {ans_acc_str:>8}"
        )
    print("-" * 80)
    print(
        f"{'OVERALL':<15} {rep_cum200['n']:>4} {rep_cum200['correct']:>8} {rep_cum200['wrong']:>6} {rep_cum200['abstain']:>8} {rep_cum200['pa']:>8.4f} {rep_cum200['coverage']:>9.4f} {rep_cum200['answered_accuracy']:>8.4f}"
    )
    print("=" * 80)

    # Save complete report to JSON
    summary_report = {
        "standalone_next100": {
            "n": rep_n100["n"],
            "correct": rep_n100["correct"],
            "wrong": rep_n100["wrong"],
            "abstain": rep_n100["abstain"],
            "pa": rep_n100["pa"],
            "business_macro_pa": macro_n100,
            "coverage": rep_n100["coverage"],
            "answered_accuracy": rep_n100["answered_accuracy"],
            "wall_clock_seconds": t_n100,
            "fast_triage_zero_llm_hits": c_ft_n100,
            "fast_triage_ids": ids_ft_n100,
            "by_business": rep_n100["by_business"],
            "by_type": rep_n100["by_type"],
            "non_correct_cases": case_summaries,
        },
        "cumulative_200": {
            "n": rep_cum200["n"],
            "correct": rep_cum200["correct"],
            "wrong": rep_cum200["wrong"],
            "abstain": rep_cum200["abstain"],
            "pa": rep_cum200["pa"],
            "business_macro_pa": macro_cum200,
            "coverage": rep_cum200["coverage"],
            "answered_accuracy": rep_cum200["answered_accuracy"],
            "wall_clock_seconds": t_b1 + t_b2 + t_n100,
            "fast_triage_zero_llm_hits": total_ft_200,
            "by_business": rep_cum200["by_business"],
            "by_type": rep_cum200["by_type"],
        },
        "progression": {
            "batch_1_v6": {
                "n": rep_b1["n"],
                "correct": rep_b1["correct"],
                "wrong": rep_b1["wrong"],
                "abstain": rep_b1["abstain"],
                "pa": rep_b1["pa"],
                "business_macro_pa": macro_b1,
                "coverage": rep_b1["coverage"],
                "answered_accuracy": rep_b1["answered_accuracy"],
                "wall_clock_seconds": t_b1,
            },
            "batch_2_v4": {
                "n": rep_b2["n"],
                "correct": rep_b2["correct"],
                "wrong": rep_b2["wrong"],
                "abstain": rep_b2["abstain"],
                "pa": rep_b2["pa"],
                "business_macro_pa": macro_b2,
                "coverage": rep_b2["coverage"],
                "answered_accuracy": rep_b2["answered_accuracy"],
                "wall_clock_seconds": t_b2,
            },
            "prior_100": {
                "n": rep_prior100["n"],
                "correct": rep_prior100["correct"],
                "wrong": rep_prior100["wrong"],
                "abstain": rep_prior100["abstain"],
                "pa": rep_prior100["pa"],
                "business_macro_pa": macro_prior100,
                "coverage": rep_prior100["coverage"],
                "answered_accuracy": rep_prior100["answered_accuracy"],
                "wall_clock_seconds": t_b1 + t_b2,
            },
            "next_100": {
                "n": rep_n100["n"],
                "correct": rep_n100["correct"],
                "wrong": rep_n100["wrong"],
                "abstain": rep_n100["abstain"],
                "pa": rep_n100["pa"],
                "business_macro_pa": macro_n100,
                "coverage": rep_n100["coverage"],
                "answered_accuracy": rep_n100["answered_accuracy"],
                "wall_clock_seconds": t_n100,
            },
            "cumulative_200": {
                "n": rep_cum200["n"],
                "correct": rep_cum200["correct"],
                "wrong": rep_cum200["wrong"],
                "abstain": rep_cum200["abstain"],
                "pa": rep_cum200["pa"],
                "business_macro_pa": macro_cum200,
                "coverage": rep_cum200["coverage"],
                "answered_accuracy": rep_cum200["answered_accuracy"],
                "wall_clock_seconds": t_b1 + t_b2 + t_n100,
            },
        },
    }

    report_out = n100_dir / "cumulative_200_report.json"
    with open(report_out, "w", encoding="utf-8") as rf:
        json.dump(summary_report, rf, indent=2)
    print(f"\n[OK] Successfully saved comprehensive report to {report_out}")


if __name__ == "__main__":
    main()
