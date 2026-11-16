import json
import os

target_ids = ['q_e9455734edd3', 'q_f895c5de92bd', 'q_91f79e9456a1', 'q_3fc34d6baada']

old_trace_dir = 'artifacts/live_trammay_eval_100/run_artifacts/traces'
new_trace_dir = 'artifacts/live_trammay_eval_100/run_artifacts_retest_4/traces'

with open('artifacts/live_trammay_eval_100/gold_100.jsonl', encoding='utf-8') as f:
    gold_map = {json.loads(line)['id']: json.loads(line) for line in f if line.strip()}

with open('artifacts/live_trammay_eval_100/questions_100.jsonl', encoding='utf-8') as f:
    q_map = {json.loads(line)['id']: json.loads(line) for line in f if line.strip()}

with open('artifacts/live_trammay_eval_100/submission.jsonl', encoding='utf-8') as f:
    sub_old = {json.loads(line)['id']: json.loads(line) for line in f if line.strip()}

with open('artifacts/live_trammay_eval_100/submission_retest_4.jsonl', encoding='utf-8') as f:
    sub_new = {json.loads(line)['id']: json.loads(line) for line in f if line.strip()}

def load_traces_by_qid(tdir):
    m = {}
    for fname in os.listdir(tdir):
        if fname.endswith('.json'):
            with open(os.path.join(tdir, fname), encoding='utf-8') as f:
                t = json.load(f)
                m[t['question']['id']] = (fname, t)
    return m

old_traces = load_traces_by_qid(old_trace_dir)
new_traces = load_traces_by_qid(new_trace_dir)

for qid in target_ids:
    print("=" * 80)
    print(f"QUESTION ID: {qid} | Business: {q_map[qid]['business_id']}")
    print(f"Question text: {q_map[qid]['question']}")
    print(f"Gold:     type={gold_map[qid]['type']}, answer={gold_map[qid].get('answer')}")
    print(f"Old Pred: type={sub_old[qid]['type']}, answer={sub_old[qid].get('answer')}")
    print(f"New Pred: type={sub_new[qid]['type']}, answer={sub_new[qid].get('answer')}")
    
    if qid in old_traces:
        ofname, ot = old_traces[qid]
        print(f"\n[OLD TRACE ({ofname})]")
        print(f"  decision: {ot.get('decision')}, model_calls: {ot.get('model_calls')}, elapsed_ms: {ot.get('elapsed_ms')}")
        print(f"  errors: {ot.get('errors')}")
        for i, p in enumerate(ot.get('plans', [])):
            print(f"  Plan {i+1}: type={p.get('answer_type')}, val={p.get('value')}")
            print(f"    SQL: {p.get('sql')}")
            print(f"    reasoning: {p.get('reasoning')}")
        if ot.get('invalid_plans'):
            print(f"  invalid_plans: {ot.get('invalid_plans')}")
    
    if qid in new_traces:
        nfname, nt = new_traces[qid]
        print(f"\n[NEW TRACE ({nfname})]")
        print(f"  decision: {nt.get('decision')}, model_calls: {nt.get('model_calls')}, elapsed_ms: {nt.get('elapsed_ms')}")
        print(f"  errors: {nt.get('errors')}")
        for i, p in enumerate(nt.get('plans', [])):
            print(f"  Plan {i+1}: type={p.get('answer_type')}, val={p.get('value')}")
            print(f"    SQL: {p.get('sql')}")
            print(f"    reasoning: {p.get('reasoning')}")
        if nt.get('invalid_plans'):
            print(f"  invalid_plans: {nt.get('invalid_plans')}")
        print("  candidates:")
        for c in nt.get('candidates', []):
            print(f"    backend={c.get('program', {}).get('backend')}, verified={c.get('verified')}, val={c.get('value')}, reasons={c.get('reasons')}")
