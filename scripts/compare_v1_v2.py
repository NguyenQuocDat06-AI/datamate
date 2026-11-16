import json

with open("artifacts/live_trammay_eval_100/score_report_v2.json", encoding="utf-8") as f:
    v2 = json.load(f)

with open("artifacts/live_trammay_eval_100/score_report.json", encoding="utf-8") as f:
    v1 = json.load(f)

print("=== V1 VS V2 OVERALL ===")
print("N:", v1["n"], "->", v2["n"])
print("Correct:", v1["correct"], "->", v2["correct"])
print("Wrong:", v1["wrong"], "->", v2["wrong"])
print("Abstain:", v1["abstain"], "->", v2["abstain"])
print("PA (Query-Macro):", v1["pa"], "->", v2["pa"])
print("Answered Accuracy:", v1["answered_accuracy"], "->", v2["answered_accuracy"])
print("Coverage:", v1["coverage"], "->", v2["coverage"])

b_pas_v1 = [b["pa"] for b in v1["by_business"].values()]
b_macro_v1 = sum(b_pas_v1) / len(b_pas_v1)
b_pas_v2 = [b["pa"] for b in v2["by_business"].values()]
b_macro_v2 = sum(b_pas_v2) / len(b_pas_v2)
print("Business-Macro PA:", b_macro_v1, "->", b_macro_v2)

print("\n=== BY BUSINESS V2 ===")
for bid, binfo in sorted(v2["by_business"].items()):
    print(f"{bid}: n={binfo['n']}, correct={binfo['correct']}, wrong={binfo['wrong']}, abstain={binfo['abstain']}, pa={binfo['pa']:.4f}")

print("\n=== BY TYPE V2 ===")
for tid, tinfo in sorted(v2["by_type"].items()):
    print(f"{tid}: n={tinfo['n']}, correct={tinfo['correct']}, wrong={tinfo['wrong']}, abstain={tinfo['abstain']}, pa={tinfo['pa']:.4f}")

