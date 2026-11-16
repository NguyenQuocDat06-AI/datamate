import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

from bizreason.evaluation.scorer import score_records


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def count_fast_triage(traces_dir):
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


def main():
    # Directories
    b1_dir_v5 = root / "artifacts" / "live_trammay_eval_50_gptoss_v5"
    b1_dir_v4 = root / "artifacts" / "live_trammay_eval_50_gptoss_v4"
    b1_dir_v3 = root / "artifacts" / "live_trammay_eval_50_gptoss_v3"
    b1_gold_p = root / "artifacts" / "live_trammay_eval_50" / "gold_50.jsonl"
    b1_q_p = root / "artifacts" / "live_trammay_eval_50" / "questions_50.jsonl"

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

    # Score Batch 1
    rep_b1_v3 = score_records(b1_pred_v3, b1_gold, b1_q)
    rep_b1_v4 = score_records(b1_pred_v4, b1_gold, b1_q)
    rep_b1_v5 = score_records(b1_pred_v5, b1_gold, b1_q)

    # Score Batch 2
    rep_b2_v1 = score_records(b2_pred_v1, b2_gold, b2_q)
    rep_b2_v2 = score_records(b2_pred_v2, b2_gold, b2_q)
    rep_b2_v3 = score_records(b2_pred_v3, b2_gold, b2_q)

    # Combined sets
    rep_100_base = score_records(b1_pred_v3 + b2_pred_v1, all_golds, all_qs)
    rep_100_r3 = score_records(b1_pred_v4 + b2_pred_v2, all_golds, all_qs)
    rep_100_r4 = score_records(b1_pred_v5 + b2_pred_v3, all_golds, all_qs)

    # Timings
    t_b1_v5 = 0.0
    t_b2_v3 = 0.0
    if (b1_dir_v5 / "execution_time.json").exists():
        with open(b1_dir_v5 / "execution_time.json", encoding="utf-8") as f:
            t_b1_v5 = json.load(f).get("elapsed_seconds", 0.0)
    if (b2_dir_v3 / "execution_time.json").exists():
        with open(b2_dir_v3 / "execution_time.json", encoding="utf-8") as f:
            t_b2_v3 = json.load(f).get("elapsed_seconds", 0.0)

    # Fast triage counts
    b1_ft_c, b1_ft_ids = count_fast_triage(b1_dir_v5 / "traces" / "traces")
    b2_ft_c, b2_ft_ids = count_fast_triage(b2_dir_v3 / "traces" / "traces")
    total_ft_c = b1_ft_c + b2_ft_c

    # Outcomes mapping
    b1_out_r3 = {d["id"]: d["outcome"] for d in rep_b1_v4["details"]}
    b2_out_r3 = {d["id"]: d["outcome"] for d in rep_b2_v2["details"]}
    out_100_r3 = {**b1_out_r3, **b2_out_r3}

    b1_out_r4 = {d["id"]: d["outcome"] for d in rep_b1_v5["details"]}
    b2_out_r4 = {d["id"]: d["outcome"] for d in rep_b2_v3["details"]}
    out_100_r4 = {**b1_out_r4, **b2_out_r4}


    transitions_r3_r4 = {}
    for qid in out_100_r3:
        r3_o = out_100_r3[qid]
        r4_o = out_100_r4[qid]
        k = f"{r3_o} -> {r4_o}"
        transitions_r3_r4[k] = transitions_r3_r4.get(k, 0) + 1

    r3_non_correct = [qid for qid, out in out_100_r3.items() if out != "correct"]
    r4_now_correct = [qid for qid in r3_non_correct if out_100_r4[qid] == "correct"]
    regressions_r3_r4 = [qid for qid, out in out_100_r3.items() if out == "correct" and out_100_r4[qid] != "correct"]

    # Index questions and traces
    q_by_id = {q["id"]: q for q in all_qs}
    gold_by_id = {g["id"]: g for g in all_golds}
    r4_pred_by_id = {p["id"]: p for p in (b1_pred_v5 + b2_pred_v3)}

    traces_by_id = {}
    for p in list((b1_dir_v5 / "traces" / "traces").glob("*.json")) + list((b2_dir_v3 / "traces" / "traces").glob("*.json")):
        with open(p, encoding="utf-8") as f:
            try:
                td = json.load(f)
                qid = td.get("question", {}).get("id")
                if qid:
                    traces_by_id[qid] = td
            except Exception:
                pass

    print("\n" + "=" * 78)
    print("PROGRESSION SUMMARY: BASELINE -> ROUND 3 -> ROUND 4 (ag/gpt-oss-120b-medium)")
    print("=" * 78)
    print(f"{'Milestone':<22} {'N':>4} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'Coverage':>9} {'AnsAcc':>8} {'Time':>8}")
    print("-" * 78)
    print(f"{'Batch 1 Base (v3)':<22} {50:>4} {rep_b1_v3['correct']:>8} {rep_b1_v3['wrong']:>6} {rep_b1_v3['abstain']:>8} {rep_b1_v3['pa']:>8.4f} {rep_b1_v3['coverage']:>9.4f} {rep_b1_v3.get('answered_accuracy', 0):>8.4f} {'-':>8}")
    print(f"{'Batch 1 R3 (v4)':<22} {50:>4} {rep_b1_v4['correct']:>8} {rep_b1_v4['wrong']:>6} {rep_b1_v4['abstain']:>8} {rep_b1_v4['pa']:>8.4f} {rep_b1_v4['coverage']:>9.4f} {rep_b1_v4.get('answered_accuracy', 0):>8.4f} {'100.86s':>8}")
    print(f"{'Batch 1 R4 (v5)':<22} {50:>4} {rep_b1_v5['correct']:>8} {rep_b1_v5['wrong']:>6} {rep_b1_v5['abstain']:>8} {rep_b1_v5['pa']:>8.4f} {rep_b1_v5['coverage']:>9.4f} {rep_b1_v5.get('answered_accuracy', 0):>8.4f} {f'{t_b1_v5:.2f}s':>8}")
    print("-" * 78)
    print(f"{'Batch 2 Base (v1)':<22} {50:>4} {rep_b2_v1['correct']:>8} {rep_b2_v1['wrong']:>6} {rep_b2_v1['abstain']:>8} {rep_b2_v1['pa']:>8.4f} {rep_b2_v1['coverage']:>9.4f} {rep_b2_v1.get('answered_accuracy', 0):>8.4f} {'-':>8}")
    print(f"{'Batch 2 R3 (v2)':<22} {50:>4} {rep_b2_v2['correct']:>8} {rep_b2_v2['wrong']:>6} {rep_b2_v2['abstain']:>8} {rep_b2_v2['pa']:>8.4f} {rep_b2_v2['coverage']:>9.4f} {rep_b2_v2.get('answered_accuracy', 0):>8.4f} {'113.88s':>8}")
    print(f"{'Batch 2 R4 (v3)':<22} {50:>4} {rep_b2_v3['correct']:>8} {rep_b2_v3['wrong']:>6} {rep_b2_v3['abstain']:>8} {rep_b2_v3['pa']:>8.4f} {rep_b2_v3['coverage']:>9.4f} {rep_b2_v3.get('answered_accuracy', 0):>8.4f} {f'{t_b2_v3:.2f}s':>8}")
    print("-" * 78)
    print(f"{'Combined Base':<22} {100:>4} {rep_100_base['correct']:>8} {rep_100_base['wrong']:>6} {rep_100_base['abstain']:>8} {rep_100_base['pa']:>8.4f} {rep_100_base['coverage']:>9.4f} {rep_100_base.get('answered_accuracy', 0):>8.4f} {'-':>8}")
    print(f"{'Combined Round 3':<22} {100:>4} {rep_100_r3['correct']:>8} {rep_100_r3['wrong']:>6} {rep_100_r3['abstain']:>8} {rep_100_r3['pa']:>8.4f} {rep_100_r3['coverage']:>9.4f} {rep_100_r3.get('answered_accuracy', 0):>8.4f} {'214.74s':>8}")
    print(f"{'Combined Round 4':<22} {100:>4} {rep_100_r4['correct']:>8} {rep_100_r4['wrong']:>6} {rep_100_r4['abstain']:>8} {rep_100_r4['pa']:>8.4f} {rep_100_r4['coverage']:>9.4f} {rep_100_r4.get('answered_accuracy', 0):>8.4f} {f'{t_b1_v5+t_b2_v3:.2f}s':>8}")
    print("=" * 78)
    print(f"Fast Triage (0 LLM calls): Batch 1={b1_ft_c}/50, Batch 2={b2_ft_c}/50, Total={total_ft_c}/100")
    if total_ft_c > 0:
        print(f"Fast Triage Question IDs: {b1_ft_ids + b2_ft_ids}")

    print("\nTRANSITIONS (Round 3 -> Round 4):")
    for trans, count in sorted(transitions_r3_r4.items(), key=lambda x: -x[1]):
        print(f"  {trans}: {count}")

    print(f"\nRound 3 non-correct questions resolved to CORRECT in Round 4: {len(r4_now_correct)}")
    for qid in r4_now_correct:
        b = q_by_id[qid]["business_id"]
        qtxt = q_by_id[qid]["question"]
        old_s = out_100_r3[qid]
        g_ans = gold_by_id[qid].get("answer")
        p_ans = r4_pred_by_id[qid].get("answer")
        print(f"  [{old_s} -> correct] ({qid}, {b}): {qtxt}")
        print(f"    Gold: {g_ans} | Pred: {p_ans}")

    print(f"\nRegressions from Round 3 (correct -> non-correct): {len(regressions_r3_r4)}")
    for qid in regressions_r3_r4:
        b = q_by_id[qid]["business_id"]
        qtxt = q_by_id[qid]["question"]
        new_s = out_100_r4[qid]
        print(f"  [correct -> {new_s}] ({qid}, {b}): {qtxt}")

    # Remaining Wrong and Abstain in Round 4
    rem_wrong = [qid for qid, out in out_100_r4.items() if out == "wrong"]
    rem_abstain = [qid for qid, out in out_100_r4.items() if out == "abstain"]

    print("\n" + "=" * 78)
    print(f"REMAINING NON-CORRECT IN ROUND 4 ({len(rem_wrong)} WRONG, {len(rem_abstain)} ABSTAIN):")
    print("=" * 78)
    for qid in rem_wrong:
        b = q_by_id[qid]["business_id"]
        qtxt = q_by_id[qid]["question"]
        g_ans = gold_by_id[qid].get("answer")
        p_ans = r4_pred_by_id[qid].get("answer")
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
        p_ans = r4_pred_by_id[qid].get("answer")
        tdata = traces_by_id.get(qid, {})
        print(f"\nABSTAIN: {qid} ({b})")
        print(f"  Question: {qtxt}")
        print(f"  Gold: {g_ans}")
        print(f"  Pred: {p_ans}")
        print(f"  Decision reason: {tdata.get('decision', {}).get('reason')}")
        print(f"  Errors: {tdata.get('errors')}")

    # Save summary
    res_path = b2_dir_v3 / "cumulative_100_round4_summary.json"
    with open(res_path, "w", encoding="utf-8") as f:
        json.dump(rep_100_r4, f, indent=2)
    print(f"\n[OK] Saved Round 4 cumulative summary to {res_path}")


if __name__ == "__main__":
    main()
