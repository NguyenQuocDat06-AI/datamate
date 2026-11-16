import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from bizreason.utils import digest

rep_path = root / "artifacts" / "live_trammay_eval_500" / "score_report.json"
qs_path = root / "artifacts" / "live_trammay_eval_500" / "questions_50_b5.jsonl"
gold_path = root / "artifacts" / "live_trammay_eval_500" / "gold_50_b5.jsonl"
sub_path = root / "artifacts" / "live_trammay_eval_500" / "submission.jsonl"

with open(rep_path, encoding="utf-8") as f:
    rep = json.load(f)

with open(qs_path, encoding="utf-8") as f:
    qs = {json.loads(l)["id"]: json.loads(l) for l in f if l.strip()}

with open(gold_path, encoding="utf-8") as f:
    golds = {json.loads(l)["id"]: json.loads(l) for l in f if l.strip()}

with open(sub_path, encoding="utf-8") as f:
    preds = {json.loads(l)["id"]: json.loads(l) for l in f if l.strip()}

print("=== Summary ===")
print(f"Total: {rep['n']}, Correct: {rep['correct']}, Wrong: {rep['wrong']}, Abstain: {rep['abstain']}, PA: {rep['pa']}")

# Model call stats
traces_dir = root / "artifacts" / "live_trammay_eval_500" / "run_artifacts" / "traces"
traces = [json.load(open(p, encoding="utf-8")) for p in traces_dir.glob("*.json")]
mcs = [t.get("model_calls", 0) for t in traces]
zero_call_hits = sum(1 for x in mcs if x == 0)
call_dist = sorted([(c, mcs.count(c)) for c in set(mcs)])
print(f"\nExecution stats across {len(traces)} traces:")
print(f"Total model calls: {sum(mcs)}")
print(f"Zero model call (fast-triage) hits: {zero_call_hits}")
print(f"Model calls distribution: {call_dist}")

non_correct = [item for item in rep["details"] if item["outcome"] != "correct"]
print(f"\nNon-correct count: {len(non_correct)}")

for item in non_correct:
    qid = item["id"]
    trace_rel = f"artifacts/live_trammay_eval_500/run_artifacts/traces/{digest(qid)[:24]}.json"
    trace_file = root / trace_rel
    
    trace_data = {}
    if trace_file.exists():
        with open(trace_file, encoding="utf-8") as tf:
            trace_data = json.load(tf)

    print("\n--------------------------------------------------")
    print(f"ID: {qid}")
    print(f"Outcome: {item['outcome']}")
    print(f"Business: {qs[qid]['business_id']}")
    print(f"Question: {qs[qid]['question']}")
    print(f"Gold: {json.dumps(golds[qid], ensure_ascii=False)}")
    print(f"Prediction: {json.dumps(preds[qid], ensure_ascii=False)}")
    print(f"Trace File: {trace_rel}")
    if trace_data:
        print(f"Elapsed ms: {trace_data.get('elapsed_ms')}")
        print(f"Model calls: {trace_data.get('model_calls')}")
        print(f"Decision: {trace_data.get('decision')}")
        print(f"Errors: {trace_data.get('errors')}")
        print(f"Steps count: {len(trace_data.get('steps', []))}")
        for s_idx, step in enumerate(trace_data.get("steps", [])):
            print(f"  Step {s_idx+1}: action={step.get('action')}, tool={step.get('tool')}, status={step.get('status')}")
            if step.get("notes"):
                print(f"    notes={step.get('notes')}")
            if step.get("error"):
                print(f"    error={step.get('error')}")
