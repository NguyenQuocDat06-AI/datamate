import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

from bizreason.evaluation.scorer import score_records


def main():
    # Batch 1 files
    b1_dir = root / "artifacts" / "live_trammay_eval_50_gptoss_v3"
    b1_gold_path = root / "artifacts" / "live_trammay_eval_50" / "gold_50.jsonl"
    b1_q_path = root / "artifacts" / "live_trammay_eval_50" / "questions_50.jsonl"
    b1_pred_path = b1_dir / "submission.jsonl"
    b1_score_path = b1_dir / "score_report.json"

    # Batch 2 files
    b2_dir = root / "artifacts" / "live_trammay_eval_next50_gptoss"
    b2_gold_path = b2_dir / "gold_next50.jsonl"
    b2_q_path = b2_dir / "questions_next50.jsonl"
    b2_pred_path = b2_dir / "submission.jsonl"
    b2_score_path = b2_dir / "score_report.json"

    with open(b1_score_path, encoding="utf-8") as f:
        rep1 = json.load(f)
    with open(b2_score_path, encoding="utf-8") as f:
        rep2 = json.load(f)

    # Load 100 predictions, golds, questions
    with open(b1_pred_path, encoding="utf-8") as f:
        p1 = [json.loads(l) for l in f if l.strip()]
    with open(b2_pred_path, encoding="utf-8") as f:
        p2 = [json.loads(l) for l in f if l.strip()]
    with open(b1_gold_path, encoding="utf-8") as f:
        g1 = [json.loads(l) for l in f if l.strip()]
    with open(b2_gold_path, encoding="utf-8") as f:
        g2 = [json.loads(l) for l in f if l.strip()]
    with open(b1_q_path, encoding="utf-8") as f:
        q1 = [json.loads(l) for l in f if l.strip()]
    with open(b2_q_path, encoding="utf-8") as f:
        q2 = [json.loads(l) for l in f if l.strip()]

    all_preds = p1 + p2
    all_golds = g1 + g2
    all_qs = q1 + q2

    assert len(all_preds) == 100
    assert len(all_golds) == 100
    assert len(all_qs) == 100

    # Official score_records run
    cum_report = score_records(all_preds, all_golds, all_qs)

    print("Batch 1 (First 50 - GPT-OSS v3):")
    print(f"  n={rep1['n']}, correct={rep1['correct']}, wrong={rep1['wrong']}, abstain={rep1['abstain']}, PA={rep1['pa']:.4f}, answered_acc={rep1.get('answered_accuracy', 0):.4f}, coverage={rep1['coverage']:.4f}")

    print("Batch 2 (Next 50 - GPT-OSS):")
    print(f"  n={rep2['n']}, correct={rep2['correct']}, wrong={rep2['wrong']}, abstain={rep2['abstain']}, PA={rep2['pa']:.4f}, answered_acc={rep2.get('answered_accuracy', 0):.4f}, coverage={rep2['coverage']:.4f}")

    # Business-macro PA
    biz_pas = [s["pa"] for s in cum_report["by_business"].values()]
    biz_macro_pa = sum(biz_pas) / len(biz_pas) if biz_pas else 0.0

    cum_report["business_macro_pa"] = biz_macro_pa
    cum_report["query_macro_pa"] = cum_report["pa"]

    print("\n" + "=" * 60)
    print("=== CUMULATIVE 100 QUESTIONS ON ag/gpt-oss-120b-medium ===")
    print("=" * 60)
    print(f"Total Questions Evaluated: {cum_report['n']}")
    print(f"Total Correct: {cum_report['correct']} ({cum_report['correct'] / cum_report['n'] * 100:.2f}%)")
    print(f"Total Wrong: {cum_report['wrong']} ({cum_report['wrong'] / cum_report['n'] * 100:.2f}%)")
    print(f"Total Abstain: {cum_report['abstain']} ({cum_report['abstain'] / cum_report['n'] * 100:.2f}%)")
    print(f"Query-Macro PA (Penalized): {cum_report['query_macro_pa']:.4f} ({cum_report['query_macro_pa'] * 100:.2f}%)")
    print(f"Business-Macro PA (Penalized): {cum_report['business_macro_pa']:.4f} ({cum_report['business_macro_pa'] * 100:.2f}%)")
    print(f"Coverage: {cum_report['coverage']:.4f} ({cum_report['coverage'] * 100:.2f}%)")
    print(f"Answered Accuracy: {cum_report['answered_accuracy']:.4f} ({cum_report['answered_accuracy'] * 100:.2f}%)")

    print("\n--- Cumulative Breakdown by Business ---")
    print(f"{'Business':<10} {'n':>5} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'Coverage':>10}")
    for b in sorted(cum_report["by_business"]):
        s = cum_report["by_business"][b]
        print(f"{b:<10} {s['n']:>5} {s['correct']:>8} {s['wrong']:>6} {s['abstain']:>8} {s['pa']:>8.4f} {s['coverage']:>10.4f}")

    print("\n--- Cumulative Breakdown by Answer Type ---")
    print(f"{'Type':<15} {'n':>5} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'Coverage':>10}")
    for t in sorted(cum_report["by_type"]):
        s = cum_report["by_type"][t]
        print(f"{t:<15} {s['n']:>5} {s['correct']:>8} {s['wrong']:>6} {s['abstain']:>8} {s['pa']:>8.4f} {s['coverage']:>10.4f}")

    out_file = b2_dir / "cumulative_100_gptoss_summary.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(cum_report, f, indent=2)
    print(f"\n[OK] Saved official cumulative summary to {out_file}")


if __name__ == "__main__":
    main()
