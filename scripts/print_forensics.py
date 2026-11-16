import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
with open(root / "artifacts" / "live_trammay_eval_next50_gptoss" / "forensics.json", encoding="utf-8") as f:
    cases = json.load(f)

print("=== 8 WRONG CASES ===")
for c in cases:
    if c["outcome"] == "wrong":
        print(f"\nID: {c['id']} | Biz: {c['business_id']} | GoldType: {c['gold_type']} -> PredType: {c['pred_type']}")
        print(f"Question: {c['question']}")
        print(f"Gold: {c['gold_answer']}")
        print(f"Pred: {c['pred_answer']}")
        print(f"Reason: {c['pred_reason']}")

print("\n" + "="*50)
print("=== 11 ABSTAIN CASES ===")
for c in cases:
    if c["outcome"] == "abstain":
        print(f"\nID: {c['id']} | Biz: {c['business_id']} | GoldType: {c['gold_type']} -> PredType: {c['pred_type']}")
        print(f"Question: {c['question']}")
        print(f"Gold: {c['gold_answer']}")
        print(f"Reason: {c['pred_reason']}")

