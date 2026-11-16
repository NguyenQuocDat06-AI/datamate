import json
from pathlib import Path
from collections import defaultdict

def main():
    root = Path(__file__).resolve().parent.parent

    # 5 batches
    batches = {
        "Batch 1 (50)": root / "artifacts" / "live_trammay_eval_50" / "score_report_v2.json",
        "Batch 2 (100)": root / "artifacts" / "live_trammay_eval_100" / "score_report_v2.json",
        "Batch 3 (150)": root / "artifacts" / "live_trammay_eval_150" / "score_report_v2.json",
        "Batch 4 (150)": root / "artifacts" / "live_trammay_eval_450" / "score_report.json",
        "Batch 5 (50)": root / "artifacts" / "live_trammay_eval_500" / "score_report.json",
    }

    for name, p in batches.items():
        if not p.exists():
            print(f"Warning: {p} does not exist yet!")
            return
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        print(f"{name}: n={d['n']}, correct={d['correct']}, wrong={d['wrong']}, abstain={d['abstain']}, PA={d['pa']:.4f}, coverage={d['coverage']:.4f}")

    # Compute cumulative metrics
    total_n = 0
    total_correct = 0
    total_wrong = 0
    total_abstain = 0

    by_business = defaultdict(lambda: {"n": 0, "correct": 0, "wrong": 0, "abstain": 0})
    by_type = defaultdict(lambda: {"n": 0, "correct": 0, "wrong": 0, "abstain": 0})

    for name, p in batches.items():
        with open(p, encoding="utf-8") as f:
            rep = json.load(f)
        total_n += rep["n"]
        total_correct += rep["correct"]
        total_wrong += rep["wrong"]
        total_abstain += rep["abstain"]

        for b, s in rep.get("by_business", {}).items():
            by_business[b]["n"] += s["n"]
            by_business[b]["correct"] += s["correct"]
            by_business[b]["wrong"] += s["wrong"]
            by_business[b]["abstain"] += s["abstain"]

        for t, s in rep.get("by_type", {}).items():
            by_type[t]["n"] += s["n"]
            by_type[t]["correct"] += s["correct"]
            by_type[t]["wrong"] += s["wrong"]
            by_type[t]["abstain"] += s["abstain"]

    cum_pa = total_correct / total_n
    cum_cov = (total_n - total_abstain) / total_n
    cum_acc = total_correct / (total_n - total_abstain) if (total_n - total_abstain) > 0 else 0.0

    # Business-macro PA
    biz_pas = [s["correct"] / s["n"] for s in by_business.values()]
    biz_macro_pa = sum(biz_pas) / len(biz_pas) if biz_pas else 0.0

    print("\n" + "="*50)
    print("=== CUMULATIVE 500 QUESTIONS (50 + 100 + 150 + 150 + 50) ===")
    print("="*50)
    print(f"Total n: {total_n}")
    print(f"Total correct: {total_correct}")
    print(f"Total wrong: {total_wrong}")
    print(f"Total abstain: {total_abstain}")
    print(f"Query-Macro PA: {cum_pa:.4f} ({cum_pa*100:.2f}%)")
    print(f"Business-Macro PA: {biz_macro_pa:.4f} ({biz_macro_pa*100:.2f}%)")
    print(f"Coverage: {cum_cov:.4f} ({cum_cov*100:.2f}%)")
    print(f"Answered Accuracy: {cum_acc:.4f} ({cum_acc*100:.2f}%)")

    print("\n--- Cumulative Breakdown by Business ---")
    print(f"{'Business':<10} {'n':>5} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'Coverage':>10}")
    for b in sorted(by_business):
        s = by_business[b]
        pa = s["correct"] / s["n"]
        cov = (s["n"] - s["abstain"]) / s["n"]
        print(f"{b:<10} {s['n']:>5} {s['correct']:>8} {s['wrong']:>6} {s['abstain']:>8} {pa:>8.4f} {cov:>10.4f}")

    print("\n--- Cumulative Breakdown by Answer Type ---")
    print(f"{'Type':<15} {'n':>5} {'Correct':>8} {'Wrong':>6} {'Abstain':>8} {'PA':>8} {'Coverage':>10}")
    for t in sorted(by_type):
        s = by_type[t]
        pa = s["correct"] / s["n"]
        cov = (s["n"] - s["abstain"]) / s["n"]
        print(f"{t:<15} {s['n']:>5} {s['correct']:>8} {s['wrong']:>6} {s['abstain']:>8} {pa:>8.4f} {cov:>10.4f}")

    # Save summary
    out_file = root / "artifacts" / "live_trammay_eval_500" / "cumulative_500_summary.json"
    summary = {
        "n": total_n,
        "correct": total_correct,
        "wrong": total_wrong,
        "abstain": total_abstain,
        "query_macro_pa": cum_pa,
        "business_macro_pa": biz_macro_pa,
        "coverage": cum_cov,
        "answered_accuracy": cum_acc,
        "by_business": dict(by_business),
        "by_type": dict(by_type),
    }
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\n[OK] Saved cumulative summary to {out_file}")

if __name__ == "__main__":
    main()

