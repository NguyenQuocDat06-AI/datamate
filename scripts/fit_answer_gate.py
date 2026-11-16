"""Fit the answer gate offline and report leave-shop-out gains. Usage: fit_answer_gate.py OUT.json"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bizreason.evaluation.scorer import matches  # noqa: E402
from bizreason.semantic.answer_gate import AnswerGate, build_table, coarse_key, path_key  # noqa: E402

DR = Path("/home/ubuntu/datamate_review_data")
TRAIN_PRED = Path("/home/ubuntu/.claude/jobs/095e2a71/tmp/h/train/m1_predictions.jsonl")
TRAIN_TRACE = DR / "train_full_4/traces.jsonl"
TRAIN_REF = Path("/home/ubuntu/datamate/source_test/data/train/reference_answers.jsonl")
DEV_TRACES = [DR / f"pub_m1_{x}/traces.jsonl" for x in "ABC"]
DEV_PRED = DR / "pub_m1_all.jsonl"
DEV_GOLD = Path("/home/ubuntu/datamate/submission.jsonl")  # offline fitting only, dev ids only
HELD = {"dev_008", "dev_009", "dev_010", "dev_013", "dev_014"}


def read(path):
    return [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]


def last_by_id(paths):
    out = {}
    for path in paths:
        if path.exists():
            for row in read(path):
                out[row["id"]] = row
    return out


def samples(pred_path, trace_paths, gold_path, keep):
    """Rows (shop, fine, coarse, correct, is_answer) for every id in the prediction file."""
    traces, gold = last_by_id(trace_paths), {r["id"]: r for r in read(gold_path)}
    rows = []
    for pred in read(pred_path):
        trace = traces.get(pred["id"])
        ref = gold.get(pred["id"])
        if trace is None or ref is None:
            continue
        shop = trace.get("business_id") or pred.get("biz")
        if not keep(shop):
            continue
        kind = pred.get("type")
        intent = trace.get("intent")
        rows.append((shop, path_key(intent, trace, kind), coarse_key(intent, kind),
                     kind not in {"abstain", "unanswerable"} and matches(pred, ref), kind, ref.get("type")))
    return rows


def leave_shop_out(rows):
    answered = [r for r in rows if r[4] not in {"abstain", "unanswerable"}]
    dc = dw = 0
    blocked = {}
    for shop in sorted({r[0] for r in answered}):
        gate = AnswerGate(build_table([(f, c, ok) for s, f, c, ok, _, _ in answered if s != shop]))
        for s, f, c, ok, _, _ in answered:
            if s == shop and not gate.decide(f, c):
                blocked[f] = blocked.get(f, 0) + 1
                if ok:
                    dc += 1
                else:
                    dw += 1
    return dc, dw, blocked, len(rows)


def report(name, rows):
    dc, dw, blocked, total = leave_shop_out(rows)
    base_c = sum(1 for r in rows if r[3])
    base_w = sum(1 for r in rows if r[4] not in {"abstain", "unanswerable"} and not r[3])
    print(f"{name}: total={total} base C={base_c} W={base_w}; gate blocks dC=-{dc} dW=-{dw}; "
          f"dPA={(dw - dc) / total:+.4f}")
    for key, n in sorted(blocked.items(), key=lambda kv: -kv[1])[:8]:
        print(f"   blocked {n:3d}  {key}")


def main():
    out = Path(sys.argv[1])
    train = samples(TRAIN_PRED, [TRAIN_TRACE], TRAIN_REF, lambda s: True)
    dev = samples(DEV_PRED, DEV_TRACES, DEV_GOLD, lambda s: s not in HELD and str(s).startswith("dev_"))
    report("train", train)
    report("dev", dev)
    report("train+dev", train + dev)
    both = [(f, c, ok) for _, f, c, ok, k, _ in train + dev if k not in {"abstain", "unanswerable"}]
    table = build_table(both)
    out.write_text(json.dumps(table, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print(f"wrote {out}: {len(table)} keys")


if __name__ == "__main__":
    main()
