"""Cumulative Round 4b analysis across all milestones."""

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
    for p in traces_dir.glob("*.json"):
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
    # Directories
    b1_dir_v6 = root / "artifacts" / "live_trammay_eval_50_gptoss_v6"
    b1_dir_v5 = root / "artifacts" / "live_trammay_eval_50_gptoss_v5"
    b1_dir_v4 = root / "artifacts" / "live_trammay_eval_50_gptoss_v4"
    b1_dir_v3 = root / "artifacts" / "live_trammay_eval_50_gptoss_v3"
    b1_gold_p = root / "artifacts" / "live_trammay_eval_50" / "gold_50.jsonl"
    b1_q_p = root / "artifacts" / "live_trammay_eval_50" / "questions_50.jsonl"

    b2_dir_v4 = root / "artifacts" / "live_trammay_eval_next50_gptoss_v4"
    b2_dir_v3 = root / "artifacts" / "live_trammay_eval_next50_gptoss_v3"
    b2_dir_v2 = root / "artifacts" / "live_trammay_eval_next50_gptoss_v2"
    b2_dir_v1 = root / "artifacts" / "live_trammay_eval_next50_gptoss"
    b2_gold_p = b2_dir_v1 / "gold_next50.jsonl"
    b2_q_p = b2_dir_v1 / "questions_next50.jsonl"

    # Load datasets
    b1_gold = load_jsonl(b1_gold_p)
    b1_q = load_jsonl(b1_q_p)
    b2_gold = load_jsonl(b2_gold_p)
    b2_q = load_jsonl(b2_q_p)

    all_golds = b1_gold + b2_gold
    all_qs = b1_q + b2_q

    # Load predictions
    b1_pred_v3 = load_jsonl(b1_dir_v3 / "submission.jsonl")
    b2_pred_v1 = load_jsonl(b2_dir_v1 / "submission.jsonl")

    b1_pred_v4 = load_jsonl(b1_dir_v4 / "predictions.jsonl")
    b2_pred_v2 = load_jsonl(b2_dir_v2 / "predictions.jsonl")

    b1_pred_v5 = load_jsonl(b1_dir_v5 / "predictions.jsonl")
    b2_pred_v3 = load_jsonl(b2_dir_v3 / "predictions.jsonl")

    b1_pred_v6 = load_jsonl(b1_dir_v6 / "predictions.jsonl")
    b2_pred_v4 = load_jsonl(b2_dir_v4 / "predictions.jsonl")

    # Score Batch 1
    rep_b1_v3 = score_records(b1_pred_v3, b1_gold, b1_q)
    rep_b1_v4 = score_records(b1_pred_v4, b1_gold, b1_q)
    rep_b1_v5 = score_records(b1_pred_v5, b1_gold, b1_q)
    rep_b1_v6 = score_records(b1_pred_v6, b1_gold, b1_q)

    # Score Batch 2
    rep_b2_v1 = score_records(b2_pred_v1, b2_gold, b2_q)
    rep_b2_v2 = score_records(b2_pred_v2, b2_gold, b2_q)
    rep_b2_v3 = score_records(b2_pred_v3, b2_gold, b2_q)
    rep_b2_v4 = score_records(b2_pred_v4, b2_gold, b2_q)

    # Combined sets
    rep_100_base = score_records(b1_pred_v3 + b2_pred_v1, all_golds, all_qs)
    rep_100_r3 = score_records(b1_pred_v4 + b2_pred_v2, all_golds, all_qs)
    rep_100_r4a = score_records(b1_pred_v5 + b2_pred_v3, all_golds, all_qs)
    rep_100_r4b = score_records(b1_pred_v6 + b2_pred_v4, all_golds, all_qs)

    # Timings
    t_b1_v6 = 0.0
    t_b2_v4 = 0.0
    if (b1_dir_v6 / "execution_time.json").exists():
        with open(b1_dir_v6 / "execution_time.json", encoding="utf-8") as f:
            t_b1_v6 = json.load(f).get("elapsed_seconds", 0.0)
    if (b2_dir_v4 / "execution_time.json").exists():
        with open(b2_dir_v4 / "execution_time.json", encoding="utf-8") as f:
            t_b2_v4 = json.load(f).get("elapsed_seconds", 0.0)

    # Fast triage counts
    # Trace dir could be traces or traces/traces
    t1_p = b1_dir_v6 / "traces"
    if (t1_p / "traces").exists():
        t1_p = t1_p / "traces"
    b1_ft_c, b1_ft_ids = count_fast_triage(t1_p)

    t2_p = b2_dir_v4 / "traces"
    if (t2_p / "traces").exists():
        t2_p = t2_p / "traces"
    b2_ft_c, b2_ft_ids = count_fast_triage(t2_p)
    total_ft_c = b1_ft_c + b2_ft_c

    # Macro PAs
    macro_b1_v6 = macro_pa(rep_b1_v6["by_business"])
    macro_b2_v4 = macro_pa(rep_b2_v4["by_business"])
    macro_100_base = macro_pa(rep_100_base["by_business"])
    macro_100_r3 = macro_pa(rep_100_r3["by_business"])
    macro_100_r4a = macro_pa(rep_100_r4a["by_business"])
    macro_100_r4b = macro_pa(rep_100_r4b["by_business"])

    print("\n" + "=" * 90)
    print("ROUND 4B EVALUATION: 100-QUESTION PROGRESSION ON ag/gpt-oss-120b-medium")
    print("=" * 90)
    print(
        f"{'Milestone':<22} {'N':>4} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'MacroPA':>8} {'Coverage':>9} {'AnsAcc':>8} {'Time':>8}"
    )
    print("-" * 90)
    print(
        f"{'Batch 1 Base (v3)':<22} {50:>4} {rep_b1_v3['correct']:>8} {rep_b1_v3['wrong']:>6} {rep_b1_v3['abstain']:>8} {rep_b1_v3['pa']:>8.4f} {'-':>8} {rep_b1_v3['coverage']:>9.4f} {rep_b1_v3.get('answered_accuracy', 0):>8.4f} {'-':>8}"
    )
    print(
        f"{'Batch 1 R3 (v4)':<22} {50:>4} {rep_b1_v4['correct']:>8} {rep_b1_v4['wrong']:>6} {rep_b1_v4['abstain']:>8} {rep_b1_v4['pa']:>8.4f} {'-':>8} {rep_b1_v4['coverage']:>9.4f} {rep_b1_v4.get('answered_accuracy', 0):>8.4f} {'100.86s':>8}"
    )
    print(
        f"{'Batch 1 R4a (v5)':<22} {50:>4} {rep_b1_v5['correct']:>8} {rep_b1_v5['wrong']:>6} {rep_b1_v5['abstain']:>8} {rep_b1_v5['pa']:>8.4f} {'-':>8} {rep_b1_v5['coverage']:>9.4f} {rep_b1_v5.get('answered_accuracy', 0):>8.4f} {'108.66s':>8}"
    )
    print(
        f"{'Batch 1 R4b (v6)':<22} {50:>4} {rep_b1_v6['correct']:>8} {rep_b1_v6['wrong']:>6} {rep_b1_v6['abstain']:>8} {rep_b1_v6['pa']:>8.4f} {macro_b1_v6:>8.4f} {rep_b1_v6['coverage']:>9.4f} {rep_b1_v6.get('answered_accuracy', 0):>8.4f} {f'{t_b1_v6:.2f}s':>8}"
    )
    print("-" * 90)
    print(
        f"{'Batch 2 Base (v1)':<22} {50:>4} {rep_b2_v1['correct']:>8} {rep_b2_v1['wrong']:>6} {rep_b2_v1['abstain']:>8} {rep_b2_v1['pa']:>8.4f} {'-':>8} {rep_b2_v1['coverage']:>9.4f} {rep_b2_v1.get('answered_accuracy', 0):>8.4f} {'-':>8}"
    )
    print(
        f"{'Batch 2 R3 (v2)':<22} {50:>4} {rep_b2_v2['correct']:>8} {rep_b2_v2['wrong']:>6} {rep_b2_v2['abstain']:>8} {rep_b2_v2['pa']:>8.4f} {'-':>8} {rep_b2_v2['coverage']:>9.4f} {rep_b2_v2.get('answered_accuracy', 0):>8.4f} {'113.88s':>8}"
    )
    print(
        f"{'Batch 2 R4a (v3)':<22} {50:>4} {rep_b2_v3['correct']:>8} {rep_b2_v3['wrong']:>6} {rep_b2_v3['abstain']:>8} {rep_b2_v3['pa']:>8.4f} {'-':>8} {rep_b2_v3['coverage']:>9.4f} {rep_b2_v3.get('answered_accuracy', 0):>8.4f} {'97.10s':>8}"
    )
    print(
        f"{'Batch 2 R4b (v4)':<22} {50:>4} {rep_b2_v4['correct']:>8} {rep_b2_v4['wrong']:>6} {rep_b2_v4['abstain']:>8} {rep_b2_v4['pa']:>8.4f} {macro_b2_v4:>8.4f} {rep_b2_v4['coverage']:>9.4f} {rep_b2_v4.get('answered_accuracy', 0):>8.4f} {f'{t_b2_v4:.2f}s':>8}"
    )
    print("-" * 90)
    print(
        f"{'Combined Base':<22} {100:>4} {rep_100_base['correct']:>8} {rep_100_base['wrong']:>6} {rep_100_base['abstain']:>8} {rep_100_base['pa']:>8.4f} {macro_100_base:>8.4f} {rep_100_base['coverage']:>9.4f} {rep_100_base.get('answered_accuracy', 0):>8.4f} {'-':>8}"
    )
    print(
        f"{'Combined Round 3':<22} {100:>4} {rep_100_r3['correct']:>8} {rep_100_r3['wrong']:>6} {rep_100_r3['abstain']:>8} {rep_100_r3['pa']:>8.4f} {macro_100_r3:>8.4f} {rep_100_r3['coverage']:>9.4f} {rep_100_r3.get('answered_accuracy', 0):>8.4f} {'214.74s':>8}"
    )
    print(
        f"{'Combined Round 4a':<22} {100:>4} {rep_100_r4a['correct']:>8} {rep_100_r4a['wrong']:>6} {rep_100_r4a['abstain']:>8} {rep_100_r4a['pa']:>8.4f} {macro_100_r4a:>8.4f} {rep_100_r4a['coverage']:>9.4f} {rep_100_r4a.get('answered_accuracy', 0):>8.4f} {'205.76s':>8}"
    )
    print(
        f"{'Combined Round 4b':<22} {100:>4} {rep_100_r4b['correct']:>8} {rep_100_r4b['wrong']:>6} {rep_100_r4b['abstain']:>8} {rep_100_r4b['pa']:>8.4f} {macro_100_r4b:>8.4f} {rep_100_r4b['coverage']:>9.4f} {rep_100_r4b.get('answered_accuracy', 0):>8.4f} {f'{t_b1_v6+t_b2_v4:.2f}s':>8}"
    )
    print("=" * 90)
    print(
        f"Fast Triage (0 LLM calls): Batch 1={b1_ft_c}/50, Batch 2={b2_ft_c}/50, Total={total_ft_c}/100"
    )
    if total_ft_c > 0:
        print(f"Fast Triage Question IDs: {b1_ft_ids + b2_ft_ids}")

    # Per-Business Breakdown Table
    print("\n" + "=" * 70)
    print("PER-BUSINESS BREAKDOWN (ROUND 4B FINAL - 100 QUESTIONS)")
    print("=" * 70)
    print(
        f"{'Business':<15} {'N':>4} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'Coverage':>9} {'AnsAcc':>8}"
    )
    print("-" * 70)
    for biz, stats in rep_100_r4b["by_business"].items():
        ans_acc = stats.get("answered_accuracy")
        ans_acc_str = f"{ans_acc:.4f}" if ans_acc is not None else "N/A"
        print(
            f"{biz:<15} {stats['n']:>4} {stats['correct']:>8} {stats['wrong']:>6} {stats['abstain']:>8} {stats['pa']:>8.4f} {stats['coverage']:>9.4f} {ans_acc_str:>8}"
        )
    print("-" * 70)
    print(
        f"{'OVERALL':<15} {rep_100_r4b['n']:>4} {rep_100_r4b['correct']:>8} {rep_100_r4b['wrong']:>6} {rep_100_r4b['abstain']:>8} {rep_100_r4b['pa']:>8.4f} {rep_100_r4b['coverage']:>9.4f} {rep_100_r4b['answered_accuracy']:>8.4f}"
    )
    print(f"Business-Macro PA: {macro_100_r4b:.4f}")
    print("=" * 70)

    # Per-Answer-Type Breakdown Table
    print("\n" + "=" * 70)
    print("PER-ANSWER-TYPE BREAKDOWN (ROUND 4B FINAL - 100 QUESTIONS)")
    print("=" * 70)
    print(
        f"{'Type':<15} {'N':>4} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'Coverage':>9} {'AnsAcc':>8}"
    )
    print("-" * 70)
    for qtype, stats in rep_100_r4b["by_type"].items():
        ans_acc = stats.get("answered_accuracy")
        ans_acc_str = f"{ans_acc:.4f}" if ans_acc is not None else "N/A"
        print(
            f"{qtype:<15} {stats['n']:>4} {stats['correct']:>8} {stats['wrong']:>6} {stats['abstain']:>8} {stats['pa']:>8.4f} {stats['coverage']:>9.4f} {ans_acc_str:>8}"
        )
    print("-" * 70)
    print(
        f"{'OVERALL':<15} {rep_100_r4b['n']:>4} {rep_100_r4b['correct']:>8} {rep_100_r4b['wrong']:>6} {rep_100_r4b['abstain']:>8} {rep_100_r4b['pa']:>8.4f} {rep_100_r4b['coverage']:>9.4f} {rep_100_r4b['answered_accuracy']:>8.4f}"
    )
    print("=" * 70)

    # Transitions from R4a and R3
    out_100_r3 = {
        d["id"]: d["outcome"]
        for d in (rep_b1_v4["details"] + rep_b2_v2["details"])
    }
    out_100_r4a = {
        d["id"]: d["outcome"]
        for d in (rep_b1_v5["details"] + rep_b2_v3["details"])
    }
    out_100_r4b = {d["id"]: d["outcome"] for d in rep_100_r4b["details"]}

    trans_r4a_r4b = {}
    for qid, o_a in out_100_r4a.items():
        o_b = out_100_r4b[qid]
        k = f"{o_a} -> {o_b}"
        trans_r4a_r4b[k] = trans_r4a_r4b.get(k, 0) + 1

    print("\nTRANSITIONS (Round 4a -> Round 4b):")
    for trans, count in sorted(trans_r4a_r4b.items(), key=lambda x: -x[1]):
        print(f"  {trans}: {count}")

    q_by_id = {q["id"]: q for q in all_qs}
    gold_by_id = {g["id"]: g for g in all_golds}
    r4b_pred_by_id = {p["id"]: p for p in (b1_pred_v6 + b2_pred_v4)}

    # Index traces
    traces_by_id = {}
    for p in list(t1_p.glob("*.json")) + list(t2_p.glob("*.json")):
        with open(p, encoding="utf-8") as f:
            try:
                td = json.load(f)
                qid = td.get("question", {}).get("id")
                if qid:
                    traces_by_id[qid] = td
            except Exception:
                pass

    # Questions fixed in 4b (non-correct in 4a -> correct in 4b)
    fixed_in_4b = [
        qid
        for qid, o_a in out_100_r4a.items()
        if o_a != "correct" and out_100_r4b[qid] == "correct"
    ]
    print(f"\nFixed in Round 4b (from Round 4a non-correct): {len(fixed_in_4b)}")
    for qid in fixed_in_4b:
        b = q_by_id[qid]["business_id"]
        qtxt = q_by_id[qid]["question"]
        old_o = out_100_r4a[qid]
        g_ans = gold_by_id[qid].get("answer")
        p_ans = r4b_pred_by_id[qid].get("answer")
        print(f"  [{old_o} -> correct] ({qid}, {b}): {qtxt}")
        print(f"    Gold: {g_ans} | Pred: {p_ans}")

    # Regressions from 4a to 4b
    reg_4a_4b = [
        qid
        for qid, o_a in out_100_r4a.items()
        if o_a == "correct" and out_100_r4b[qid] != "correct"
    ]
    print(f"\nRegressions from Round 4a (correct -> non-correct): {len(reg_4a_4b)}")
    for qid in reg_4a_4b:
        b = q_by_id[qid]["business_id"]
        qtxt = q_by_id[qid]["question"]
        new_o = out_100_r4b[qid]
        print(f"  [correct -> {new_o}] ({qid}, {b}): {qtxt}")

    # Regressions from Round 3 to Round 4b
    reg_r3_r4b = [
        qid
        for qid, o_3 in out_100_r3.items()
        if o_3 == "correct" and out_100_r4b[qid] != "correct"
    ]
    print(f"\nRegressions from Round 3 (correct -> non-correct): {len(reg_r3_r4b)}")
    for qid in reg_r3_r4b:
        b = q_by_id[qid]["business_id"]
        qtxt = q_by_id[qid]["question"]
        new_o = out_100_r4b[qid]
        print(f"  [correct -> {new_o}] ({qid}, {b}): {qtxt}")

    # Remaining Wrong and Abstain in Round 4b
    rem_wrong = [qid for qid, out in out_100_r4b.items() if out == "wrong"]
    rem_abstain = [qid for qid, out in out_100_r4b.items() if out == "abstain"]

    print("\n" + "=" * 90)
    print(
        f"REMAINING NON-CORRECT IN ROUND 4B ({len(rem_wrong)} WRONG, {len(rem_abstain)} ABSTAIN):"
    )
    print("=" * 90)
    for qid in rem_wrong:
        b = q_by_id[qid]["business_id"]
        qtxt = q_by_id[qid]["question"]
        g_ans = gold_by_id[qid].get("answer")
        p_ans = r4b_pred_by_id[qid].get("answer")
        tdata = traces_by_id.get(qid, {})
        print(f"\nWRONG: {qid} ({b})")
        print(f"  Question: {qtxt}")
        print(f"  Gold: {g_ans}")
        print(f"  Pred: {p_ans}")
        print(f"  Decision reason: {tdata.get('decision', {}).get('reason')}")
        print(f"  Errors: {tdata.get('errors')}")

    for qid in rem_abstain:
        b = q_by_id[qid]["business_id"]
        qtxt = q_by_id[qid]["question"]
        g_ans = gold_by_id[qid].get("answer")
        p_ans = r4b_pred_by_id[qid].get("answer")
        tdata = traces_by_id.get(qid, {})
        print(f"\nABSTAIN: {qid} ({b})")
        print(f"  Question: {qtxt}")
        print(f"  Gold: {g_ans}")
        print(f"  Pred: {p_ans}")
        print(f"  Decision reason: {tdata.get('decision', {}).get('reason')}")
        print(f"  Errors: {tdata.get('errors')}")

    # Save summary
    res_path = b2_dir_v4 / "cumulative_100_round4b_summary.json"
    rep_100_r4b["business_macro_pa"] = macro_100_r4b
    rep_100_r4b["wall_clock_time"] = t_b1_v6 + t_b2_v4
    rep_100_r4b["fast_triage_count"] = total_ft_c
    with open(res_path, "w", encoding="utf-8") as f:
        json.dump(rep_100_r4b, f, indent=2)
    print(f"\n[OK] Saved Round 4b cumulative summary to {res_path}")


if __name__ == "__main__":
    main()
