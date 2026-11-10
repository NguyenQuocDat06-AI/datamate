import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from bizreason.utils import digest

rep = json.loads((root / "artifacts/live_trammay_eval_50_gptoss/score_report.json").read_text(encoding="utf-8"))
traces_dir = root / "artifacts/live_trammay_eval_50_gptoss/run_artifacts/traces"

abstains = [item for item in rep["details"] if item["outcome"] == "abstain"]
for a in abstains:
    qid = a["id"]
    tf = traces_dir / f"{digest(qid)[:24]}.json"
    d = json.loads(tf.read_text(encoding="utf-8")) if tf.exists() else {}
    print(f"=== {qid} ({a['business_id']}, {a['type']}) ===")
    print("Decision reason:", d.get("decision", {}).get("reason"))
    print("Errors:", d.get("errors"))
    for idx, c in enumerate(d.get("candidates", [])):
        print(f"  Cand {idx+1} checks: {c.get('checks')}")
        print(f"  Cand {idx+1} exec status: {c.get('execution', {}).get('status')}, error: {c.get('execution', {}).get('error_code')}, rows: {c.get('execution', {}).get('rows')}")
        if c.get('execution', {}).get('diagnostics'):
            print(f"  Cand {idx+1} diagnostics: {c.get('execution', {}).get('diagnostics')}")
    print()
