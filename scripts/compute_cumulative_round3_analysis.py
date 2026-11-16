import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

from bizreason.evaluation.scorer import score_records


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main():
    # 1. Paths
    b1_dir_v4 = root / "artifacts" / "live_trammay_eval_50_gptoss_v4"
    b1_dir_v3 = root / "artifacts" / "live_trammay_eval_50_gptoss_v3"
    b1_gold_p = root / "artifacts" / "live_trammay_eval_50" / "gold_50.jsonl"
    b1_q_p = root / "artifacts" / "live_trammay_eval_50" / "questions_50.jsonl"

    b2_dir_v2 = root / "artifacts" / "live_trammay_eval_next50_gptoss_v2"
    b2_dir_v1 = root / "artifacts" / "live_trammay_eval_next50_gptoss"
    b2_gold_p = b2_dir_v1 / "gold_next50.jsonl"
    b2_q_p = b2_dir_v1 / "questions_next50.jsonl"

    # Load records
    b1_pred_v4 = load_jsonl(b1_dir_v4 / "predictions.jsonl")
    b1_pred_v3 = load_jsonl(b1_dir_v3 / "submission.jsonl")
    b1_gold = load_jsonl(b1_gold_p)
    b1_q = load_jsonl(b1_q_p)

    b2_pred_v2 = load_jsonl(b2_dir_v2 / "predictions.jsonl")
    b2_pred_v1 = load_jsonl(b2_dir_v1 / "submission.jsonl")
    b2_gold = load_jsonl(b2_gold_p)
    b2_q = load_jsonl(b2_q_p)

    # Official scoring
    rep_b1_v4 = score_records(b1_pred_v4, b1_gold, b1_q)
    with open(b1_dir_v3 / "score_report.json", encoding="utf-8") as f:
        rep_b1_v3 = json.load(f)

    rep_b2_v2 = score_records(b2_pred_v2, b2_gold, b2_q)
    with open(b2_dir_v1 / "score_report.json", encoding="utf-8") as f:
        rep_b2_v1 = json.load(f)

    # Combined 100
    all_preds_round3 = b1_pred_v4 + b2_pred_v2
    all_preds_baseline = b1_pred_v3 + b2_pred_v1
    all_golds = b1_gold + b2_gold
    all_qs = b1_q + b2_q

    rep_100_round3 = score_records(all_preds_round3, all_golds, all_qs)
    rep_100_baseline = score_records(all_preds_baseline, all_golds, all_qs)

    # Execution times
    with open(b1_dir_v4 / "execution_time.json", encoding="utf-8") as f:
        t_b1_v4 = json.load(f)["elapsed_seconds"]
    with open(b2_dir_v2 / "execution_time.json", encoding="utf-8") as f:
        t_b2_v2 = json.load(f)["elapsed_seconds"]

    # Fast triage hit counts
    b1_traces_dir = b1_dir_v4 / "traces" / "traces"
    b2_traces_dir = b2_dir_v2 / "traces" / "traces"

    b1_0_calls = 0
    for p in b1_traces_dir.glob("*.json"):
        with open(p, encoding="utf-8") as f:
            if json.load(f).get("model_calls") == 0:
                b1_0_calls += 1

    b2_0_calls = 0
    fast_triage_ids = []
    for p in b2_traces_dir.glob("*.json"):
        with open(p, encoding="utf-8") as f:
            td = json.load(f)
            if td.get("model_calls") == 0:
                b2_0_calls += 1
                fast_triage_ids.append(td.get("question", {}).get("id"))

    # Map question-by-question outcomes
    b1_outcomes_v3 = {d["id"]: d["outcome"] for d in rep_b1_v3["details"]}
    b1_outcomes_v4 = {d["id"]: d["outcome"] for d in rep_b1_v4["details"]}

    b2_outcomes_v1 = {d["id"]: d["outcome"] for d in rep_b2_v1["details"]}
    b2_outcomes_v2 = {d["id"]: d["outcome"] for d in rep_b2_v2["details"]}

    baseline_outcomes = {**b1_outcomes_v3, **b2_outcomes_v1}
    round3_outcomes = {**b1_outcomes_v4, **b2_outcomes_v2}

    assert len(baseline_outcomes) == 100
    assert len(round3_outcomes) == 100

    transitions = {}
    for qid in baseline_outcomes:
        b_out = baseline_outcomes[qid]
        r_out = round3_outcomes[qid]
        trans_key = f"{b_out} -> {r_out}"
        transitions[trans_key] = transitions.get(trans_key, 0) + 1

    previously_non_correct = [qid for qid, out in baseline_outcomes.items() if out != "correct"]
    now_correct = [qid for qid in previously_non_correct if round3_outcomes[qid] == "correct"]
    regressions = [qid for qid, out in baseline_outcomes.items() if out == "correct" and round3_outcomes[qid] != "correct"]

    # Print summary
    print("=" * 70)
    print("SUMMARY COMPARISON: BASELINE vs ROUND 3 (ag/gpt-oss-120b-medium)")
    print("=" * 70)
    print(f"Batch 1 V3 Baseline:  Correct={rep_b1_v3['correct']}, Wrong={rep_b1_v3['wrong']}, Abstain={rep_b1_v3['abstain']}, PA={rep_b1_v3['pa']:.4f}, AnsAcc={rep_b1_v3.get('answered_accuracy', 0):.4f}")
    print(f"Batch 1 V4 (Round 3): Correct={rep_b1_v4['correct']}, Wrong={rep_b1_v4['wrong']}, Abstain={rep_b1_v4['abstain']}, PA={rep_b1_v4['pa']:.4f}, AnsAcc={rep_b1_v4.get('answered_accuracy', 0):.4f}, Time={t_b1_v4:.2f}s, FastTriage={b1_0_calls}")
    print("-" * 70)
    print(f"Batch 2 V1 Baseline:  Correct={rep_b2_v1['correct']}, Wrong={rep_b2_v1['wrong']}, Abstain={rep_b2_v1['abstain']}, PA={rep_b2_v1['pa']:.4f}, AnsAcc={rep_b2_v1.get('answered_accuracy', 0):.4f}")
    print(f"Batch 2 V2 (Round 3): Correct={rep_b2_v2['correct']}, Wrong={rep_b2_v2['wrong']}, Abstain={rep_b2_v2['abstain']}, PA={rep_b2_v2['pa']:.4f}, AnsAcc={rep_b2_v2.get('answered_accuracy', 0):.4f}, Time={t_b2_v2:.2f}s, FastTriage={b2_0_calls}")
    print("-" * 70)
    print(f"Combined Baseline:    Correct={rep_100_baseline['correct']}, Wrong={rep_100_baseline['wrong']}, Abstain={rep_100_baseline['abstain']}, PA={rep_100_baseline['pa']:.4f}, AnsAcc={rep_100_baseline.get('answered_accuracy', 0):.4f}")
    print(f"Combined Round 3:     Correct={rep_100_round3['correct']}, Wrong={rep_100_round3['wrong']}, Abstain={rep_100_round3['abstain']}, PA={rep_100_round3['pa']:.4f}, AnsAcc={rep_100_round3.get('answered_accuracy', 0):.4f}, TotalTime={t_b1_v4+t_b2_v2:.2f}s, FastTriage={b1_0_calls+b2_0_calls}")
    print("=" * 70)

    print("\nTRANSITIONS:")
    for trans, count in sorted(transitions.items(), key=lambda x: -x[1]):
        print(f"  {trans}: {count}")

    print(f"\nPreviously non-correct questions (out of 29): {len(now_correct)} became CORRECT!")
    print(f"Regressions from previously correct: {len(regressions)} ({regressions})")

    # Detailed list of the converted questions
    q_by_id = {q["id"]: q for q in all_qs}
    gold_by_id = {g["id"]: g for g in all_golds}
    round3_preds_by_id = {p["id"]: p for p in all_preds_round3}

    print("\nCONVERTED QUESTIONS (Non-correct -> Correct):")
    for qid in now_correct:
        b = q_by_id[qid]["business_id"]
        qtxt = q_by_id[qid]["question"]
        old_status = baseline_outcomes[qid]
        print(f"  [{old_status} -> correct] ({qid}, {b}): {qtxt}")

    # Residual non-correct cases in Round 3
    remaining_wrong = [qid for qid, out in round3_outcomes.items() if out == "wrong"]
    remaining_abstain = [qid for qid, out in round3_outcomes.items() if out == "abstain"]

    print("\n" + "=" * 70)
    print(f"REMAINING NON-CORRECT IN ROUND 3 ({len(remaining_wrong)} WRONG, {len(remaining_abstain)} ABSTAIN):")
    print("=" * 70)

    # Index all traces by question id
    traces_by_id = {}
    for p in list(b1_traces_dir.glob("*.json")) + list(b2_traces_dir.glob("*.json")):
        with open(p, encoding="utf-8") as f:
            tdata = json.load(f)
            qid = tdata.get("question", {}).get("id")
            if qid:
                traces_by_id[qid] = tdata

    for qid in remaining_wrong:
        b = q_by_id[qid]["business_id"]
        qtxt = q_by_id[qid]["question"]
        g_ans = gold_by_id[qid].get("answer")
        g_type = gold_by_id[qid].get("type")
        p_ans = round3_preds_by_id[qid].get("answer")
        p_type = round3_preds_by_id[qid].get("type")
        tdata = traces_by_id.get(qid, {})

        print(f"\nWRONG: {qid} ({b})")
        print(f"  Question: {qtxt}")
        print(f"  Gold: [{g_type}] {g_ans}")
        print(f"  Pred: [{p_type}] {p_ans}")
        print(f"  Decision reason: {tdata.get('decision', {}).get('reason')}")
        print(f"  Errors: {tdata.get('errors')}")
        print(f"  Model calls: {tdata.get('model_calls')}")
        plans = tdata.get("plans", [])
        if plans:
            print(f"  Plan: {plans[0].get('metric')}, tables: {plans[0].get('tables')}")

    print("\n" + "=" * 70)
    print("CUMULATIVE 100 BREAKDOWN BY BUSINESS")
    print("=" * 70)
    print(f"{'Business':<10} {'N':>4} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'Coverage':>10} {'AnsAcc':>10}")
    for b in sorted(rep_100_round3["by_business"]):
        s = rep_100_round3["by_business"][b]
        acc = s.get("answered_accuracy")
        acc_s = f"{acc:.4f}" if acc is not None else "N/A"
        print(f"{b:<10} {s['n']:>4} {s['correct']:>8} {s['wrong']:>6} {s['abstain']:>8} {s['pa']:>8.4f} {s['coverage']:>10.4f} {acc_s:>10}")

    print("\n" + "=" * 70)
    print("CUMULATIVE 100 BREAKDOWN BY ANSWER TYPE")
    print("=" * 70)
    print(f"{'Type':<15} {'N':>4} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'Coverage':>10} {'AnsAcc':>10}")
    for t in sorted(rep_100_round3["by_type"]):
        s = rep_100_round3["by_type"][t]
        acc = s.get("answered_accuracy")
        acc_s = f"{acc:.4f}" if acc is not None else "N/A"
        print(f"{t:<15} {s['n']:>4} {s['correct']:>8} {s['wrong']:>6} {s['abstain']:>8} {s['pa']:>8.4f} {s['coverage']:>10.4f} {acc_s:>10}")

    # Also save the official cumulative 100 json
    cum_path = b2_dir_v2 / "cumulative_100_gptoss_summary.json"
    with open(cum_path, "w", encoding="utf-8") as f:
        json.dump(rep_100_round3, f, indent=2)
    print(f"\n[OK] Saved official cumulative summary to {cum_path}")


if __name__ == "__main__":
    main()
