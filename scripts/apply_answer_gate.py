"""Apply a fitted gate offline. Usage: apply_answer_gate.py TRACES.jsonl [TRACES2...] SUBMISSION.jsonl GATE.json OUT.jsonl"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bizreason.semantic.answer_gate import AnswerGate  # noqa: E402


def main():
    *trace_paths, sub, gate_path, out = sys.argv[1:]
    traces = {}
    for path in trace_paths:
        for line in open(path, encoding="utf-8"):
            if line.strip():
                row = json.loads(line)
                traces[row["id"]] = row
    gate = AnswerGate(json.load(open(gate_path, encoding="utf-8")))
    blocked = 0
    with open(sub, encoding="utf-8") as src, open(out, "w", encoding="utf-8") as dst:
        for line in src:
            if not line.strip():
                continue
            row = json.loads(line)
            trace = traces.get(row["id"])
            if trace is not None:
                ok, key = gate.allows(trace.get("intent"), trace, row.get("type"))
                if not ok:
                    blocked += 1
                    row = {"id": row["id"], "type": "abstain", "reason": f"answer gate: {key}",
                           "gated_answer": row.get("answer")}
            dst.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"blocked {blocked} answers -> {out}")


if __name__ == "__main__":
    main()
