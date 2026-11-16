import json
from pathlib import Path
from collections import Counter

root = Path(__file__).resolve().parent.parent

# 1. Load the 450 previously evaluated questions
q50_path = root / "artifacts" / "live_trammay_eval_50" / "questions_50.jsonl"
q100_path = root / "artifacts" / "live_trammay_eval_100" / "questions_100.jsonl"
q150_path = root / "artifacts" / "live_trammay_eval_150" / "questions_150.jsonl"
q150_b4_path = root / "artifacts" / "live_trammay_eval_450" / "questions_150_b4.jsonl"

with open(q50_path, encoding="utf-8") as f:
    q50 = [json.loads(line) for line in f if line.strip()]
with open(q100_path, encoding="utf-8") as f:
    q100 = [json.loads(line) for line in f if line.strip()]
with open(q150_path, encoding="utf-8") as f:
    q150 = [json.loads(line) for line in f if line.strip()]
with open(q150_b4_path, encoding="utf-8") as f:
    q150_b4 = [json.loads(line) for line in f if line.strip()]

s50 = set(q["id"] for q in q50)
s100 = set(q["id"] for q in q100)
s150 = set(q["id"] for q in q150)
s150_b4 = set(q["id"] for q in q150_b4)

print(f"Lengths: q50={len(s50)}, q100={len(s100)}, q150={len(s150)}, q150_b4={len(s150_b4)}")
assert len(s50 & s100) == 0
assert len(s50 & s150) == 0
assert len(s50 & s150_b4) == 0
assert len(s100 & s150) == 0
assert len(s100 & s150_b4) == 0
assert len(s150 & s150_b4) == 0

prev_450 = s50 | s100 | s150 | s150_b4
print(f"Union prev_450: {len(prev_450)}")

eval_q_file = root.parent / "data steps challenge" / "data" / "eval" / "questions.jsonl"
eval_gold_file = root.parent / "answer" / "Step_Data.jsonl"
print(f"eval_q_file ({eval_q_file}) exists: {eval_q_file.exists()}")
print(f"eval_gold_file ({eval_gold_file}) exists: {eval_gold_file.exists()}")

if eval_q_file.exists() and eval_gold_file.exists():
    with open(eval_q_file, encoding="utf-8") as f:
        all_questions = {json.loads(line)["id"]: json.loads(line) for line in f if line.strip()}
    with open(eval_gold_file, encoding="utf-8") as f:
        all_gold = {json.loads(line)["id"]: json.loads(line) for line in f if line.strip()}
    candidate_ids = sorted(list(set(all_questions.keys()) - prev_450))
else:
    split_path = root / "artifacts" / "live_trammay_eval" / "split.json"
    with open(split_path, encoding="utf-8") as f:
        split_data = json.load(f)
    eval_ids = set(split_data["splits"]["evaluation"]["question_ids"])

    with open(root / "data" / "train" / "questions.jsonl", encoding="utf-8") as f:
        all_questions = {json.loads(line)["id"]: json.loads(line) for line in f if line.strip()}
    with open(root / "data" / "train" / "reference_answers.jsonl", encoding="utf-8") as f:
        all_gold = {json.loads(line)["id"]: json.loads(line) for line in f if line.strip()}

    candidate_ids = sorted(list((eval_ids & set(all_questions.keys())) - prev_450))

print(f"Total remaining candidate unseen evaluation questions: {len(candidate_ids)}")
businesses = ["biz_104", "biz_111", "biz_117", "biz_121", "biz_128", "biz_130"]
for b in businesses:
    b_cands = [qid for qid in candidate_ids if all_questions[qid]["business_id"] == b]
    b_types = Counter(all_gold[qid]["type"] for qid in b_cands)
    print(f"  {b}: {len(b_cands)} candidates -> {dict(b_types)}")

