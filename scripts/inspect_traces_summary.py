import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
trace_dir = root / "artifacts" / "live_trammay_eval_next50_gptoss" / "run_artifacts" / "traces"

fast_triage_hits = 0
total_traces = 0
durations = []
stages_counts = {}

for f in trace_dir.glob("*.json"):
    total_traces += 1
    data = json.load(open(f, encoding="utf-8"))
    history = data.get("history", [])
    stages = [h.get("stage") for h in history if isinstance(h, dict)]
    for s in stages:
        stages_counts[s] = stages_counts.get(s, 0) + 1
    if "fast_triage" in stages or data.get("fast_triage_hit"):
        fast_triage_hits += 1

print(f"Total traces: {total_traces}")
print(f"Fast triage stages recorded: {fast_triage_hits}")
print(f"Stage breakdown: {stages_counts}")

