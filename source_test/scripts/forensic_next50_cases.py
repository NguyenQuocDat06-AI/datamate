import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent

rep_path = root / "artifacts" / "live_trammay_eval_next50_gptoss" / "score_report.json"
sub_path = root / "artifacts" / "live_trammay_eval_next50_gptoss" / "submission.jsonl"
gold_path = root / "artifacts" / "live_trammay_eval_next50_gptoss" / "gold_next50.jsonl"
q_path = root / "artifacts" / "live_trammay_eval_next50_gptoss" / "questions_next50.jsonl"
trace_dir = root / "artifacts" / "live_trammay_eval_next50_gptoss" / "run_artifacts" / "traces"

with open(rep_path, encoding="utf-8") as f:
    rep = json.load(f)

with open(sub_path, encoding="utf-8") as f:
    subs = {l["id"]: l for l in [json.loads(line) for line in f if line.strip()]}

with open(gold_path, encoding="utf-8") as f:
    golds = {l["id"]: l for l in [json.loads(line) for line in f if line.strip()]}

with open(q_path, encoding="utf-8") as f:
    qs = {l["id"]: l for l in [json.loads(line) for line in f if line.strip()]}

details = rep["details"]
wrong_cases = [d for d in details if d["outcome"] == "wrong"]
abstain_cases = [d for d in details if d["outcome"] == "abstain"]

print(f"Total wrong: {len(wrong_cases)}")
print(f"Total abstain: {len(abstain_cases)}")

forensics = []

for item in details:
    if item["outcome"] == "correct":
        continue
    qid = item["id"]
    outcome = item["outcome"]
    q_info = qs.get(qid, {})
    g_info = golds.get(qid, {})
    s_info = subs.get(qid, {})

    trace_file = trace_dir / f"{qid}.json"
    trace_data = {}
    if trace_file.exists():
        with open(trace_file, encoding="utf-8") as tf:
            trace_data = json.load(tf)

    forensic_entry = {
        "id": qid,
        "outcome": outcome,
        "business_id": q_info.get("business_id"),
        "question": q_info.get("question"),
        "as_of_date": q_info.get("as_of_date"),
        "gold_type": g_info.get("type"),
        "gold_answer": g_info.get("answer"),
        "pred_type": s_info.get("type"),
        "pred_answer": s_info.get("answer"),
        "pred_reason": s_info.get("reason"),
        "trace_stages": [s.get("stage") for s in trace_data.get("history", []) if isinstance(s, dict)],
        "error": trace_data.get("error"),
        "trace_keys": list(trace_data.keys()),
    }
    forensics.append(forensic_entry)

out_forensics = root / "artifacts" / "live_trammay_eval_next50_gptoss" / "forensics.json"
with open(out_forensics, "w", encoding="utf-8") as f:
    json.dump(forensics, f, indent=2, ensure_ascii=False)

print(f"Wrote {len(forensics)} forensic cases to {out_forensics}")

