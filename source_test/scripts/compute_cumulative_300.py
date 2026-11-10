import json

paths = {
    "Eval 50 (V2)": "artifacts/live_trammay_eval_50/score_report_v2.json",
    "Eval 100 (V2)": "artifacts/live_trammay_eval_100/score_report_v2.json",
    "Eval 150 (V2)": "artifacts/live_trammay_eval_150/score_report_v2.json",
}

reports = {}
total_n = 0
total_correct = 0
total_wrong = 0
total_abstain = 0

all_by_business = {}
all_by_type = {}

for name, p in paths.items():
    with open(p, "r", encoding="utf-8") as f:
        rep = json.load(f)
    reports[name] = rep
    total_n += rep["n"]
    total_correct += rep["correct"]
    total_wrong += rep["wrong"]
    total_abstain += rep["abstain"]
    print(f"=== {name} ===")
    print(f"  n: {rep['n']}, correct: {rep['correct']}, wrong: {rep['wrong']}, abstain: {rep['abstain']}, PA: {rep['pa']:.4f}, coverage: {rep['coverage']:.4f}")

    for b, s in rep.get("by_business", {}).items():
        if b not in all_by_business:
            all_by_business[b] = {"n": 0, "correct": 0, "wrong": 0, "abstain": 0}
        all_by_business[b]["n"] += s["n"]
        all_by_business[b]["correct"] += s["correct"]
        all_by_business[b]["wrong"] += s["wrong"]
        all_by_business[b]["abstain"] += s["abstain"]

    for t, s in rep.get("by_type", {}).items():
        if t not in all_by_type:
            all_by_type[t] = {"n": 0, "correct": 0, "wrong": 0, "abstain": 0}
        all_by_type[t]["n"] += s["n"]
        all_by_type[t]["correct"] += s["correct"]
        all_by_type[t]["wrong"] += s["wrong"]
        all_by_type[t]["abstain"] += s["abstain"]

print("\n=== CUMULATIVE 300 QUESTIONS (50 + 100 + 150 V2) ===")
cumulative_pa = total_correct / total_n if total_n > 0 else 0.0
cumulative_coverage = (total_n - total_abstain) / total_n if total_n > 0 else 0.0
cumulative_acc = total_correct / (total_n - total_abstain) if (total_n - total_abstain) > 0 else 0.0

print(f"Total n: {total_n}")
print(f"Total correct: {total_correct}")
print(f"Total wrong: {total_wrong}")
print(f"Total abstain: {total_abstain}")
print(f"Cumulative PA: {cumulative_pa:.4f} ({cumulative_pa * 100:.2f}%)")
print(f"Cumulative Coverage: {cumulative_coverage:.4f} ({cumulative_coverage * 100:.2f}%)")
print(f"Answered Accuracy: {cumulative_acc:.4f} ({cumulative_acc * 100:.2f}%)")

print("\nCumulative by Business:")
for b in sorted(all_by_business):
    s = all_by_business[b]
    pa = s["correct"] / s["n"]
    print(f"  {b}: {s['correct']}/{s['n']} (PA: {pa:.4f})")

print("\nCumulative by Type:")
for t in sorted(all_by_type):
    s = all_by_type[t]
    pa = s["correct"] / s["n"]
    print(f"  {t}: {s['correct']}/{s['n']} (PA: {pa:.4f})")
