import sys, json, os, glob, time, multiprocessing
from pathlib import Path

# Insert source_test into path
ROOT_DIR = Path("/home/ubuntu/datamate/source_test")
if not ROOT_DIR.exists():
    ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from bizreason.contracts import Question, QueryPlan, BusinessContext, AnswerType
from bizreason.temporal import TemporalResolver, LunarCalendar
from bizreason.rules import RuleRegistry
from bizreason.cli import _builder
from bizreason.pipeline import Solver
from bizreason.triage import TriageRouter
from bizreason.config import Settings
from bizreason.llm import Usage, ModelError
from bizreason.evaluation.scorer import score_records, matches
from bizreason.utils import load_jsonl, json_default

class RecordedPlanModel:
    def __init__(self, plan_dict: dict | None):
        self.usage = Usage()
        self.plan_dict = plan_dict

    def generate_json(self, system: str, prompt: str, *, timeout: float = 300) -> dict:
        self.usage.calls += 1
        if self.plan_dict is None:
            raise ModelError("Empty plan in trace")
        return self.plan_dict

def main():
    t_start = time.time()
    data_dir = ROOT_DIR / "data" / "train"
    cache_dir = ROOT_DIR / "artifacts" / "cache"
    builder, rules, temporal = _builder(data_dir, cache_dir)
    settings = Settings()
    triage = TriageRouter()

    baseline_pred_file = Path("/home/ubuntu/datamate/results/predictions.jsonl")
    if not baseline_pred_file.exists():
        baseline_pred_file = ROOT_DIR / "artifacts" / "kaggle_12h_eval_1000" / "predictions.jsonl"
    preds_baseline = load_jsonl(baseline_pred_file)
    pred_map = {p["id"]: p for p in preds_baseline}
    ordered_qids = [p["id"] for p in preds_baseline]

    refs = load_jsonl(data_dir / "reference_answers.jsonl")
    refs_map = {r["id"]: r for r in refs if r["id"] in pred_map}
    qs = load_jsonl(data_dir / "questions.jsonl")
    qs_map = {q["id"]: q for q in qs if q["id"] in pred_map}

    print(f"Scoring baseline across {len(ordered_qids)} questions...")
    base_summary = score_records(preds_baseline, [refs_map[qid] for qid in ordered_qids], [qs_map[qid] for qid in ordered_qids])
    base_details = {row["id"]: row["outcome"] for row in base_summary.get("details", [])}

    print(f"Baseline: n={base_summary['n']}, correct={base_summary['correct']}, wrong={base_summary['wrong']}, abstain={base_summary['abstain']}, PA={base_summary['pa']:.4f}")

    trace_dir = ROOT_DIR / "artifacts" / "run" / "traces"
    traces = {}
    for p in trace_dir.glob("*.json"):
        with p.open(encoding="utf-8") as f:
            t = json.load(f)
        traces[t["question"]["id"]] = t

    print(f"Loaded {len(traces)} traces from {trace_dir}")

    # Preload business contexts
    contexts = {}
    for biz_dir in (data_dir / "data").iterdir():
        if biz_dir.is_dir() and not biz_dir.name.startswith("."):
            contexts[biz_dir.name] = builder.prepare(biz_dir)
    print(f"Pre-loaded {len(contexts)} business contexts.")

    new_predictions = []
    transitions = []
    regressions = []
    improvements = []

    print(f"Starting replay of {len(ordered_qids)} questions...")
    for idx, qid in enumerate(ordered_qids, 1):
        t = traces.get(qid)
        if not t:
            # Fallback to baseline prediction if trace missing
            new_predictions.append(pred_map[qid])
            continue

        q = Question.model_validate(t["question"])
        ctx = contexts[q.business_id]
        
        raw_plan = None
        if t.get("invalid_plans"):
            raw_plan = t["invalid_plans"][0]
        elif t.get("plans"):
            raw_plan = t["plans"][0]

        model = RecordedPlanModel(raw_plan)
        solver = Solver(model, rules, temporal, settings, triage_router=triage)

        pred, new_trace = solver.solve(q, ctx)
        pred_dict = pred.model_dump(mode="json", exclude_none=True)
        # Match official format: if answer is None, remove it or set type abstain
        if pred.type == AnswerType.ABSTAIN or pred.answer is None:
            pred_dict = {"id": q.id, "type": "abstain", "reason": pred.reason or "No eligible candidate"}
        new_predictions.append(pred_dict)

        old_outcome = base_details.get(qid, "unknown")
        ref = refs_map.get(qid)
        new_outcome = "abstain" if pred_dict.get("type") == "abstain" else ("correct" if matches(pred_dict, ref) else "wrong")

        if old_outcome != new_outcome:
            trans_info = {
                "id": qid,
                "business_id": q.business_id,
                "question": q.question,
                "old_outcome": old_outcome,
                "new_outcome": new_outcome,
                "old_answer": pred_map.get(qid, {}).get("answer"),
                "new_answer": pred_dict.get("answer"),
                "expected": ref.get("answer") if ref else None,
                "trace_errors": new_trace.errors,
            }
            transitions.append(trans_info)
            if old_outcome == "correct":
                regressions.append(trans_info)
                print(f"[REGRESSION!] {qid} ({q.business_id}): correct -> {new_outcome} | old={trans_info['old_answer']} new={trans_info['new_answer']} exp={trans_info['expected']}")
            elif new_outcome == "correct":
                improvements.append(trans_info)
            elif old_outcome == "wrong" and new_outcome == "abstain":
                improvements.append(trans_info)

        if idx % 100 == 0 or idx == len(ordered_qids):
            print(f"Processed {idx}/{len(ordered_qids)} questions ({len(transitions)} transitions, {len(regressions)} regressions)...")

    # Save replayed predictions
    results_dir = Path("/home/ubuntu/datamate/results")
    results_dir.mkdir(parents=True, exist_ok=True)
    replayed_pred_file = results_dir / "replayed_predictions.jsonl"
    with replayed_pred_file.open("w", encoding="utf-8") as f:
        for item in new_predictions:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"Saved replayed predictions to {replayed_pred_file}")

    # Official scoring
    replayed_summary = score_records(new_predictions, [refs_map[qid] for qid in ordered_qids], [qs_map[qid] for qid in ordered_qids])

    # Save summary report
    replayed_score_file = results_dir / "replayed_score_report.json"
    with replayed_score_file.open("w", encoding="utf-8") as f:
        json.dump(replayed_summary, f, indent=2, ensure_ascii=False, default=json_default)
    print(f"Saved score report to {replayed_score_file}")

    # Full audit report
    audit_report = {
        "elapsed_seconds": time.time() - t_start,
        "baseline": {
            "n": base_summary["n"],
            "correct": base_summary["correct"],
            "wrong": base_summary["wrong"],
            "abstain": base_summary["abstain"],
            "answered_accuracy": base_summary["answered_accuracy"],
            "coverage": base_summary["coverage"],
            "pa": base_summary["pa"],
        },
        "replayed": {
            "n": replayed_summary["n"],
            "correct": replayed_summary["correct"],
            "wrong": replayed_summary["wrong"],
            "abstain": replayed_summary["abstain"],
            "answered_accuracy": replayed_summary["answered_accuracy"],
            "coverage": replayed_summary["coverage"],
            "pa": replayed_summary["pa"],
        },
        "target_businesses": {},
        "regressions_count": len(regressions),
        "regressions": regressions,
        "improvements_count": len(improvements),
        "improvements": improvements,
    }

    all_bizzes = sorted(set(base_summary.get("by_business", {}).keys()) | set(replayed_summary.get("by_business", {}).keys()))
    for b in all_bizzes:
        audit_report["target_businesses"][b] = {
            "before": base_summary.get("by_business", {}).get(b, {}),
            "after": replayed_summary.get("by_business", {}).get(b, {}),
        }

    audit_file = results_dir / "replay_1000_audit.json"
    with audit_file.open("w", encoding="utf-8") as f:
        json.dump(audit_report, f, indent=2, ensure_ascii=False, default=json_default)
    print(f"Saved full audit report to {audit_file}")

    print("\n" + "="*80)
    print("                      1,000-QUESTION REPLAY BENCHMARK RESULT                     ")
    print("="*80)
    print("Metric                 Before (Overnight)     After (Replay)        Delta")
    print("-" * 80)
    print(f"Correct                {base_summary['correct']:<22} {replayed_summary['correct']:<21} {replayed_summary['correct'] - base_summary['correct']:+d}")
    print(f"Wrong                  {base_summary['wrong']:<22} {replayed_summary['wrong']:<21} {replayed_summary['wrong'] - base_summary['wrong']:+d}")
    print(f"Abstain                {base_summary['abstain']:<22} {replayed_summary['abstain']:<21} {replayed_summary['abstain'] - base_summary['abstain']:+d}")
    print(f"Answered Accuracy      {base_summary['answered_accuracy']*100:.2f}%{' '*16} {replayed_summary['answered_accuracy']*100:.2f}%{' '*15} {(replayed_summary['answered_accuracy'] - base_summary['answered_accuracy'])*100:+.2f}%")
    print(f"Coverage               {base_summary['coverage']*100:.2f}%{' '*16} {replayed_summary['coverage']*100:.2f}%{' '*15} {(replayed_summary['coverage'] - base_summary['coverage'])*100:+.2f}%")
    print(f"PA Score               {base_summary['pa']*100:.2f}%{' '*16} {replayed_summary['pa']*100:.2f}%{' '*15} {(replayed_summary['pa'] - base_summary['pa'])*100:+.2f}%")
    print("-" * 80)
    print(f"Total Regressions:     {len(regressions)}")
    print(f"Total Improvements:    {len(improvements)}")
    print("="*80 + "\n")

    print("Businesses Breakdown (with changes):")
    for b in all_bizzes:
        bef = base_summary.get("by_business", {}).get(b, {})
        aft = replayed_summary.get("by_business", {}).get(b, {})
        if bef != aft:
            print(f"--- {b} ---")
            print(f"  Before: Correct={bef.get('correct')}, Wrong={bef.get('wrong')}, Abstain={bef.get('abstain')}, PA={bef.get('pa', 0)*100:.2f}%")
            print(f"  After:  Correct={aft.get('correct')}, Wrong={aft.get('wrong')}, Abstain={aft.get('abstain')}, PA={aft.get('pa', 0)*100:.2f}%")

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
