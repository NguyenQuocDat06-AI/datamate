"""End-to-end check of the semantic pipeline. Intents are cached so compiler changes re-score without model calls."""
import collections
import glob
import json
import random
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WT = Path("/home/ubuntu/agy-worktrees/arch9/source_test")
MAIN = Path("/home/ubuntu/datamate/source_test")
D = Path("/home/ubuntu/datamate_review_data")
SEM, RUN = D / "sem8", D / "e2e9"
SEM.mkdir(exist_ok=True)
RUN.mkdir(exist_ok=True)
sys.path.insert(0, str(WT))
from bizreason.cli import _registry  # noqa: E402
from bizreason.config import Settings  # noqa: E402
from bizreason.contracts import BusinessContext, Question  # noqa: E402
from bizreason.evaluation.scorer import matches  # noqa: E402
from bizreason.llm import TrammayClient, endpoint_file_resolver  # noqa: E402
from bizreason.semantic.compiler import Intent  # noqa: E402
from bizreason.semantic.intent import IntentParser, ParsedIntent  # noqa: E402
from bizreason.semantic.layer import build, to_date, to_number  # noqa: E402
from bizreason.semantic.solver import SemanticSolver, filter_choices  # noqa: E402
from bizreason.utils import normalize_lookup  # noqa: E402

notes = collections.defaultdict(list)
for f in [D / "spike_a1b/annotations.jsonl", D / "ann_rest/annotations.jsonl", D / "ann_fix/annotations.jsonl", D / "ann_fix2/annotations.jsonl"]:
    for line in f.open(encoding="utf-8"):
        r = json.loads(line)
        notes[r["biz"]].append(r)


def jl(path):
    return [json.loads(x) for x in Path(path).open(encoding="utf-8")]


def samples():
    used = {r["id"] for r in jl(D / "spike_a2_questions.jsonl")}
    tax = {x.split("\t", 1)[1].strip() for x in (D / "taxonomy_questions.txt").open(encoding="utf-8")}
    train = jl(MAIN / "data/train/questions.jsonl")
    pool = [q for q in train if q["id"] not in used and q["question"].strip() not in tax]
    rng = random.Random(20261111)
    return {"train300": rng.sample(pool, 300), "e9": jl(D / "e9_questions.jsonl")}


def connect(biz):
    target = SEM / f"{biz}.sqlite"
    if not target.exists():
        path = Path(sorted(glob.glob(str(MAIN / f"artifacts/cache/{biz}/*/catalog.json")))[-1])
        data = json.load(open(path))
        data["database"] = str(path.with_name("business.sqlite"))
        partial = target.with_suffix(".building")  # never reuse a half-built file
        smap = D / "status_maps" / f"{biz}.json"
        status_map = json.loads(smap.read_text(encoding="utf-8")) if smap.exists() else None
        print("build", biz, build(BusinessContext.model_validate(data), notes[biz], partial, status_map=status_map, value_groups=(json.loads((D / "value_groups" / f"{biz}.json").read_text(encoding="utf-8")) if (D / "value_groups" / f"{biz}.json").exists() else None), line_kinds=(json.loads((D / "line_kinds" / f"{biz}.json").read_text(encoding="utf-8")) if (D / "line_kinds" / f"{biz}.json").exists() else None)), flush=True)
        partial.replace(target)
    con = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    con.create_function("lookup", 1, lambda v: None if v is None else normalize_lookup(v))
    con.create_function("to_number", 1, to_number)
    con.create_function("to_date", 1, to_date)
    return con


class Stored:
    def __init__(self, parsed):
        self.parsed = parsed

    def parse(self, _, choices=None):
        return self.parsed


def intents(name, rows):
    path = RUN / f"{name}_intents.jsonl"
    done = {r["id"]: r for r in jl(path)} if path.exists() else {}
    todo = [q for q in rows if q["id"] not in done]
    if todo:
        cfg = json.load(open(MAIN / "artifacts/kaggle_12h_server/connection_config.json"))
        ep = json.load(open(MAIN / "artifacts/kaggle_12h_server/active_endpoint.json"))
        client = TrammayClient(Settings(base_url=ep["base_url"], model=cfg["model"], api_key=cfg["bearer_token"], max_output_tokens=400, request_timeout=200), endpoint_resolver=endpoint_file_resolver(MAIN / "artifacts/kaggle_12h_server/active_endpoint.json"), outage_wait_seconds=1800)
        parser = IntentParser(client)
        hints = {}
        for biz in {q["business_id"] for q in todo}:
            con = connect(biz)
            hints[biz] = filter_choices(con)
            con.close()

        def run(q):
            p = parser.parse(q["question"], choices=hints[q["business_id"]])
            return {"id": q["id"], "intent": p.intent.__dict__ if p.intent else None, "reason": p.reason, "attempts": p.attempts}

        with ThreadPoolExecutor(4) as pool, path.open("a", encoding="utf-8") as fh:
            for i, rec in enumerate(pool.map(run, todo), 1):
                done[rec["id"]] = rec
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                if i % 25 == 0:
                    print(name, i, "/", len(todo), flush=True)
    return done


def main():
    gold = {r["id"]: r for r in jl(MAIN / "data/train/reference_answers.jsonl")}
    gold.update({r["id"]: r for r in jl(MAIN.parent / "submission.jsonl")})
    temporal = {"biz": _registry(MAIN / "data/train")[1], "dev": _registry(MAIN / "data/public_test")[1]}
    t0 = time.time()
    for name, rows in samples().items():
        parsed = intents(name, rows)
        cons = {}
        out, tally, reasons = [], collections.Counter(), collections.Counter()
        by_measure = collections.defaultdict(collections.Counter)
        for q in rows:
            biz = q["business_id"]
            cons.setdefault(biz, connect(biz))
            rec = parsed[q["id"]]
            intent = Intent(**rec["intent"]) if rec["intent"] else None
            solver = SemanticSolver(Stored(ParsedIntent(intent, rec["reason"], rec["attempts"], None)), temporal[biz[:3]], cons[biz])
            pred, trace = solver.solve(Question.model_validate(q))
            ref = gold[q["id"]]
            if pred.type.value == "abstain":
                o = "abstain"
                reasons[trace.reason[:38]] += 1
            else:
                o = "correct" if matches(pred.model_dump(mode="json", exclude_none=True), ref) else "wrong"
            tally[o] += 1
            key = intent.measure if intent else "none"
            by_measure[key][o] += 1
            out.append({"id": q["id"], "biz": biz, "question": q["question"], "as_of": q["as_of_date"], "ref_type": ref["type"], "expected": ref.get("answer"), "type": pred.type.value, "answer": pred.answer, "outcome": o, "reason": trace.reason, "intent": rec["intent"], "windows": [(w.role, str(w.start), str(w.end_exclusive)) for w in trace.windows]})
        with (RUN / f"{name}_records.jsonl").open("w", encoding="utf-8") as fh:
            for r in out:
                fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        n = len(rows)
        answered = tally["correct"] + tally["wrong"]
        print(f"== {name}: n={n} {dict(tally)} PA={(tally['correct'] - tally['wrong']) / n:.3f} accuracy_answered={tally['correct'] / max(answered, 1):.3f}", flush=True)
        print("by measure", {k: dict(v) for k, v in sorted(by_measure.items(), key=lambda x: -sum(x[1].values()))}, flush=True)
        print("abstain reasons", reasons.most_common(14), flush=True)
    print("elapsed", round(time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
