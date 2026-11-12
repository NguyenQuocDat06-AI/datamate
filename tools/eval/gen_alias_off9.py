"""Full-pipeline measurement with the header alias list disabled: a stand-in for shops with unseen headers."""
import collections
import json
import sqlite3
import sys
from pathlib import Path

WT = Path("/home/ubuntu/agy-worktrees/arch9/source_test")
MAIN = Path("/home/ubuntu/datamate/source_test")
D = Path("/home/ubuntu/datamate_review_data")
OUT = D / "alias_off_run9"
OUT.mkdir(exist_ok=True)
sys.path.insert(0, str(WT))
import bizreason.data.schema as schema  # noqa: E402
import bizreason.utils as utils  # noqa: E402

schema.ROLE_LOOKUP.clear()  # every header name becomes unknown to ingestion
utils.WORKSPACE = OUT

from bizreason.cli import _builder, _registry  # noqa: E402
from bizreason.config import Settings  # noqa: E402
from bizreason.contracts import Question  # noqa: E402
from bizreason.evaluation.scorer import matches  # noqa: E402
from bizreason.llm import TrammayClient, endpoint_file_resolver  # noqa: E402
from bizreason.semantic.annotator import Annotator, load_or_annotate, load_status_map, load_value_groups  # noqa: E402
from bizreason.semantic.compiler import Intent  # noqa: E402
from bizreason.semantic.intent import ParsedIntent  # noqa: E402
from bizreason.semantic.layer import annotations_by_signature, build, effective_roles, to_date, to_number  # noqa: E402
from bizreason.semantic.solver import SemanticSolver  # noqa: E402
from bizreason.utils import normalize_lookup, quote_identifier as q  # noqa: E402

DATA = MAIN / "data/public_test"


class Stored:
    def __init__(self, parsed):
        self.parsed = parsed

    def parse(self, _, choices=None):
        return self.parsed


def jl(path):
    return [json.loads(x) for x in Path(path).open(encoding="utf-8")]


def names(context, records, kinds):
    by_sig = annotations_by_signature(records)
    found = set()
    con = sqlite3.connect(f"file:{context.database}?mode=ro", uri=True)
    for table in context.tables:
        note = by_sig.get(tuple(c.name for c in table.columns))
        if note is None or note.get("kind") not in kinds:
            continue
        column = effective_roles(table, note).get("product_name")
        if column:
            found.update(str(r[0]).strip() for r in con.execute(f"SELECT DISTINCT {q(column)} FROM {q(table.name)} WHERE {q(column)} IS NOT NULL"))
    con.close()
    return sorted(found)


def main():
    cfg = json.load(open(MAIN / "artifacts/kaggle_12h_server/connection_config.json"))
    ep = json.load(open(MAIN / "artifacts/kaggle_12h_server/active_endpoint.json"))
    client = TrammayClient(Settings(base_url=ep["base_url"], model=cfg["model"], api_key=cfg["bearer_token"], max_output_tokens=1500, request_timeout=280), endpoint_resolver=endpoint_file_resolver(MAIN / "artifacts/kaggle_12h_server/active_endpoint.json"), outage_wait_seconds=1800)
    annotator = Annotator(client, workers=4)
    builder, _, temporal = _builder(DATA, OUT / "cache")
    questions = jl(D / "e9_questions.jsonl")
    gold = {r["id"]: r for r in jl(MAIN.parent / "submission.jsonl")}
    intents = {r["id"]: r for r in jl(D / "e2e5/e9_intents.jsonl")}
    connections = {}
    report = {}
    for biz in sorted({x["business_id"] for x in questions}):
        started = __import__("time").time()
        context = builder.prepare(DATA / "data" / biz)
        records = load_or_annotate(annotator, context, OUT / "ann" / f"{biz}.json")
        status = load_status_map(annotator, context, records, OUT / "status" / f"{biz}.json")
        kinds = annotator.annotate_line_kinds(context, records, names(context, records, {"product_catalog"}), names(context, records, {"sales"}))
        target = OUT / "sem" / f"{biz}.sqlite"
        target.parent.mkdir(exist_ok=True)
        groups = load_value_groups(annotator, context, records, OUT / "groups" / f"{biz}.json")
        info = build(context, records, target, status_map=status, line_kinds=kinds, value_groups=groups, model_failures=annotator.take_failures())
        report[biz] = {"grains": dict(collections.Counter(t.grain for t in context.tables)), "kinds": dict(collections.Counter(r.get("kind", "error") for r in records)), "orders": info.get("orders"), "lines": info.get("lines"), "status_null_orders": info.get("status_null_orders"), "model_failures": info.get("model_failures"), "seconds": round(__import__("time").time() - started)}
        print(biz, json.dumps(report[biz], ensure_ascii=False), flush=True)
        con = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        con.create_function("lookup", 1, lambda v: None if v is None else normalize_lookup(v))
        con.create_function("to_number", 1, to_number)
        con.create_function("to_date", 1, to_date)
        connections[biz] = con
    tally, reasons, out = collections.Counter(), collections.Counter(), []
    for question in questions:
        biz = question["business_id"]
        rec = intents[question["id"]]
        intent = Intent(**rec["intent"]) if rec["intent"] else None
        solver = SemanticSolver(Stored(ParsedIntent(intent, rec["reason"], rec["attempts"], None)), temporal, connections[biz])
        pred, trace = solver.solve(Question.model_validate(question))
        ref = gold[question["id"]]
        if pred.type.value == "abstain":
            outcome = "abstain"
            reasons[trace.reason[:40]] += 1
        else:
            outcome = "correct" if matches(pred.model_dump(mode="json", exclude_none=True), ref) else "wrong"
        tally[outcome] += 1
        out.append({"id": question["id"], "biz": biz, "question": question["question"], "expected": ref.get("answer"), "answer": pred.answer, "type": pred.type.value, "outcome": outcome, "reason": trace.reason})
    (OUT / "records.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in out), encoding="utf-8")
    n = len(questions)
    answered = tally["correct"] + tally["wrong"]
    print(f"== alias_off e9: n={n} {dict(tally)} PA={(tally['correct'] - tally['wrong']) / n:.3f} accuracy_answered={tally['correct'] / max(answered, 1):.3f}", flush=True)
    print("abstain reasons", reasons.most_common(12), flush=True)


if __name__ == "__main__":
    main()
