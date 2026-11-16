import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from bizreason.utils import digest

rep = json.loads((root / "artifacts/live_trammay_eval_50_gptoss/score_report.json").read_text(encoding="utf-8"))
qs = {json.loads(line)["id"]: json.loads(line) for line in (root / "artifacts/live_trammay_eval_50/questions_50.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()}
gold = {json.loads(line)["id"]: json.loads(line) for line in (root / "artifacts/live_trammay_eval_50/gold_50.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()}
traces_dir = root / "artifacts/live_trammay_eval_50_gptoss/run_artifacts/traces"

abstains = [item for item in rep["details"] if item["outcome"] == "abstain"]
for a in abstains:
    qid = a["id"]
    t_file = traces_dir / f"{digest(qid)[:24]}.json"
    t_data = json.loads(t_file.read_text(encoding="utf-8")) if t_file.exists() else {}
    print(f"=== ABSTAIN {qid} ({a['business_id']}, {a['type']}) ===")
    print("Q:", qs[qid]["question"])
    print("Gold:", gold[qid]["answer"])
    print("Decision reason:", t_data.get("decision", {}).get("reason"))
    print("Errors:", t_data.get("errors"))
    print("Invalid plans count:", len(t_data.get("invalid_plans", [])))
    for ip in t_data.get("invalid_plans", []):
        print("  IP:", str(ip)[:160])
    cands = t_data.get("candidates", [])
    print("Candidates count:", len(cands))
    for idx, c in enumerate(cands):
        print(f"  Cand {idx+1}: answer={c.get('answer')}, verified={c.get('verified')}, audit_notes={c.get('audit_notes')}, reasons={c.get('reasons')}")
    # Also inspect repair notes if any
    probes = t_data.get("probes", [])
    for idx, pr in enumerate(probes):
        print(f"  Probe {idx+1}: action={pr.get('action')}, status={pr.get('status')}, error={pr.get('error')}")
    print()

