"""Score a run_v2 submission against gold answers, with a shop-level bootstrap interval.

Usage: gen_score.py <submission.jsonl> <gold.jsonl> [--questions questions.jsonl] [--exclude ids.json] [--out records.jsonl]
Only ids present in both the submission and the gold file are scored, unless --questions is given (then every
question id counts, a missing prediction counts as abstain). Ids in --exclude are dropped without being printed.
"""
import argparse
import collections
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, "/home/ubuntu/agy-worktrees/arch9/source_test")
from bizreason.evaluation.scorer import matches  # noqa: E402


def jl(path):
    return [json.loads(x) for x in Path(path).open(encoding="utf-8") if x.strip()]


def pa(rows):
    return (sum(r["outcome"] == "correct" for r in rows) - sum(r["outcome"] == "wrong" for r in rows)) / max(len(rows), 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("submission")
    ap.add_argument("gold")
    ap.add_argument("--questions")
    ap.add_argument("--exclude")
    ap.add_argument("--out")
    ap.add_argument("--boot", type=int, default=2000)
    a = ap.parse_args()
    pred = {r["id"]: r for r in jl(a.submission)}
    gold = {r["id"]: r for r in jl(a.gold)}
    excluded = set(json.load(open(a.exclude))) if a.exclude else set()
    if a.questions:
        meta = {q["id"]: q for q in jl(a.questions)}
        ids = [i for i in meta if i in gold and i not in excluded]
    else:
        meta = {}
        ids = [i for i in pred if i in gold and i not in excluded]
    rows = []
    for i in ids:
        p = pred.get(i, {"id": i, "type": "abstain", "answer": None})
        if p.get("type") == "abstain":
            outcome = "abstain"
        else:
            outcome = "correct" if matches({k: v for k, v in p.items() if v is not None}, gold[i]) else "wrong"
        rows.append({"id": i, "biz": meta.get(i, {}).get("business_id"), "outcome": outcome,
                     "type": p.get("type"), "answer": p.get("answer"), "expected": gold[i].get("answer"),
                     "reason": p.get("reason")})
    tally = collections.Counter(r["outcome"] for r in rows)
    line = f"== n={len(rows)} {dict(tally)} PA={pa(rows):.3f}"
    by_shop = collections.defaultdict(list)
    for r in rows:
        by_shop[r["biz"]].append(r)
    if len(by_shop) > 1 and a.boot:
        rng = random.Random(0)
        shops = list(by_shop)
        stats = sorted(pa([r for s in (rng.choice(shops) for _ in shops) for r in by_shop[s]]) for _ in range(a.boot))
        line += f" CI95=[{stats[int(0.025 * a.boot)]:.3f}, {stats[int(0.975 * a.boot)]:.3f}] shops={len(shops)}"
    print(line)
    if a.out:
        Path(a.out).write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows), encoding="utf-8")


if __name__ == "__main__":
    main()
