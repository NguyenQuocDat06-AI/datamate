"""Generate and score paired shop/layout/rulebook experiments without test gold.

The oracle uses Python records, not the production compiler/SQL. Generated gold,
fold metadata and pair definitions live OUTSIDE the inference input directories.
This author has read the pipeline: this is a diagnostic suite, not an independent
or private benchmark. The ingestion probe supplies schema bindings from the
generator and does not measure model annotation or question parsing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

POLICIES = {
    "base": {"statuses": ["completed"], "gross": False, "date": "ordered_at"},
    "shipping": {"statuses": ["completed", "shipping"], "gross": False, "date": "ordered_at"},
    "tax": {"statuses": ["completed"], "gross": True, "date": "ordered_at"},
    "delivery": {"statuses": ["completed"], "gross": False, "date": "delivered_at"},
}
LAYOUTS = ("plain", "permuted", "continuation", "split_sheets", "prose")
STATUS = {"completed": "Đã xong", "shipping": "Chờ giao", "cancelled": "Hủy", "returned": "Trả lại"}
FIELDS = {
    "order_id": "mã giao dịch", "ordered_at": "ngày đặt", "delivered_at": "ngày giao",
    "status": "trạng thái", "customer": "mã khách", "product": "mặt hàng",
    "qty": "số lượng", "amount": "thành tiền chưa thuế, đơn vị đồng",
    "tax": "tiền thuế, đơn vị đồng", "rate": "tỷ lệ thưởng ở dạng 0 đến 1",
}
INDUSTRIES = ("thuê thiết bị", "giặt đồ", "sửa dụng cụ", "đặt lịch lớp học")
QUESTIONS = {
    "revenue": "Doanh thu", "commission": "Tiền thưởng", "orders": "Số giao dịch hợp lệ",
    "quantity": "Tổng số lượng hợp lệ", "cancellation": "Tỷ lệ giao dịch hủy",
}


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def load_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def reference(orders: list[dict], policy: dict, start: date, end: date) -> dict[str, dict]:
    """Independent record-level arithmetic; no production imports, SQL or parser."""
    period = [order for order in orders if start <= date.fromisoformat(order[policy["date"]]) < end]
    valid = [order for order in period if order["status"] in policy["statuses"]]
    revenue, commission, quantity = Decimal(0), Decimal(0), 0
    for order in valid:
        for line in order["lines"]:
            value = Decimal(line["amount"]) + (Decimal(line["tax"]) if policy["gross"] else 0)
            revenue += value
            commission += value * Decimal(line["rate"])
            quantity += line["qty"]
    if not period:
        raise ValueError("generator produced an empty denominator")
    cancellation = Decimal(sum(order["status"] == "cancelled" for order in period)) * 100 / len(period)
    integer = lambda value: int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return {
        "revenue": {"type": "number", "answer": integer(revenue)},
        "commission": {"type": "number", "answer": integer(commission)},
        "orders": {"type": "number", "answer": len(valid)},
        "quantity": {"type": "number", "answer": quantity},
        "cancellation": {"type": "percent", "answer": float(cancellation.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))},
    }


def rulebook(policy: dict, prose: bool) -> str:
    names = ", ".join(STATUS[value] for value in policy["statuses"])
    axis = FIELDS[policy["date"]]
    basis = "thành tiền chưa thuế cộng tiền thuế" if policy["gross"] else "thành tiền chưa thuế"
    definitions = [
        ("Giao dịch hợp lệ", f"Chỉ các trạng thái {names}; không lấy các trạng thái còn lại."),
        ("Trục thời gian", f"Mọi câu hỏi theo kỳ dùng {axis}; không dùng cột ngày khác."),
        ("Doanh thu", f"Tổng {basis} của từng dòng thuộc giao dịch hợp lệ trong kỳ."),
        ("Tiền thưởng", f"Cộng ({basis}) nhân tỷ lệ thưởng của từng dòng thuộc giao dịch hợp lệ trong kỳ."),
        ("Số giao dịch hợp lệ", "Đếm mã giao dịch khác nhau trong tập hợp lệ trong kỳ, không đếm dòng hàng."),
        ("Tổng số lượng hợp lệ", "Cộng số lượng từng dòng thuộc giao dịch hợp lệ trong kỳ."),
        ("Tỷ lệ giao dịch hủy", "Số mã giao dịch Hủy chia số mã giao dịch mọi trạng thái trong kỳ, nhân 100. Tử và mẫu có tập trạng thái riêng."),
    ]
    if prose:
        body = "\n\n".join(f"{name} được định nghĩa như sau: {text}" for name, text in definitions)
    else:
        body = "| Khái niệm | Quy định |\n|---|---|\n" + "\n".join(f"| {name} | {text} |" for name, text in definitions)
    return "# Quy tắc đang áp dụng\n\n" + body + "\n\nTiền làm tròn nguyên đồng; phần trăm hai số thập phân.\nÔ trống cùng một nhóm lấy dòng đầu nhóm.\n"


def make_orders(rng: random.Random, start: date) -> list[dict]:
    result = []
    for index, offset in enumerate((-4, -1, 1, 4, 8, 12, 17, 21, 25, 28, 32, 36)):
        ordered = start + timedelta(days=offset)
        lines = []
        for item in range(2):
            qty, price = rng.randint(1, 7), rng.randrange(10, 50) * 100
            amount = qty * price
            lines.append({"product": f"Item-{index}-{item}", "qty": qty,
                          "amount": amount, "tax": amount // 10,
                          "rate": str(Decimal(rng.choice((5, 10, 20, 30))) / 100)})
        result.append({"order_id": f"T-{index}", "ordered_at": ordered.isoformat(),
                       "delivered_at": (ordered + timedelta(days=7)).isoformat(),
                       "status": tuple(STATUS)[index % len(STATUS)],
                       "customer": f"C-{index % 3}", "lines": lines})
    return result


def profile(orders: list[dict]) -> dict:
    return {"rows": sum(len(order["lines"]) for order in orders), "orders": len(orders),
            "amount": sum(line["amount"] for order in orders for line in order["lines"]),
            "quantity": sum(line["qty"] for order in orders for line in order["lines"]),
            "date_min": min(order["ordered_at"] for order in orders),
            "date_max": max(order["ordered_at"] for order in orders)}


def write_shop(path: Path, orders: list[dict], bindings: dict[str, str], layout: str, industry: str,
               rng: random.Random) -> None:
    path.mkdir(parents=True)
    if layout == "prose":
        glossary = "\n\n".join(f"Cột {label} lưu {FIELDS[field]}." for label, field in bindings.items())
    else:
        glossary = "| Cột | Ý nghĩa |\n|---|---|\n" + "\n".join(
            f"| {label} | {FIELDS[field]} |" for label, field in bindings.items())
    (path / "README.md").write_text(
        f"Ngành: {industry}. Mỗi dòng là một mặt hàng. Mã giao dịch có thể lặp trên nhiều dòng.\n"
        "Dòng tiếp theo có thể để trống thông tin giao dịch và kế thừa dòng đầu nhóm.\n"
        + glossary + "\nTrạng thái: " + "; ".join(f"{raw} = {canonical}" for canonical, raw in STATUS.items()) + "\n",
        encoding="utf-8")
    fields = list(bindings.values())
    labels = dict((field, label) for label, field in bindings.items())
    if layout == "permuted":
        rng.shuffle(fields)
    buckets: dict[str, list[list]] = defaultdict(list)
    for order in orders:
        for index, line in enumerate(order["lines"]):
            record = {**order, **line, "status": STATUS[order["status"]]}
            if layout == "continuation" and index:
                record.update({field: "" for field in ("order_id", "ordered_at", "delivered_at", "status", "customer")})
            key = order["ordered_at"][:7] if layout == "split_sheets" else "ledger"
            buckets[key].append([record[field] for field in fields])
    for key, rows in buckets.items():
        headers = [labels[field] for field in fields]
        if layout == "permuted":
            rows = [["Báo cáo giao dịch"], [], headers, *rows]
        else:
            rows = [headers, *rows]
        with (path / f"{key}.csv").open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows)


def generate(out: Path, shops: int, seed: int, folds: int = 3) -> dict:
    if shops < 1 or folds < 2:
        raise ValueError("shops must be positive and folds at least two")
    out.mkdir(parents=True, exist_ok=False)
    rng = random.Random(seed)
    year, month = rng.randrange(2020, 2031), rng.randrange(2, 11)
    start, end = date(year, month, 1), date(year, month+1, 1)
    originals, bindings_by_shop, groups, assignments = {}, {}, {}, {}
    shop_ids = [f"synthetic_shop_{index:03}" for index in range(shops)]
    shuffled = list(shop_ids); rng.shuffle(shuffled)
    for index, shop in enumerate(shuffled):
        assignments[shop] = index % folds
    for index, shop in enumerate(shop_ids):
        originals[shop] = make_orders(rng, start)
        labels = rng.sample(range(100, 999), len(FIELDS))
        bindings_by_shop[shop] = {f"Field-{label}": field for label, field in zip(labels, FIELDS)}
        groups[shop] = [f"industry:{index % len(INDUSTRIES)}", f"source:{shop}"]
    cases, scenarios = [], []
    for policy_name, policy in POLICIES.items():
        for layout in LAYOUTS:
            scenario = f"{policy_name}__{layout}"
            root = out / "inputs" / scenario
            root.mkdir(parents=True)
            (root / "rulebook.md").write_text(rulebook(policy, layout == "prose"), encoding="utf-8")
            questions = []
            for index, shop in enumerate(shop_ids):
                write_shop(root / "data" / shop, originals[shop], bindings_by_shop[shop], layout,
                           INDUSTRIES[index % len(INDUSTRIES)], random.Random(f"{seed}:{shop}:{layout}"))
                expected = reference(originals[shop], policy, start, end)
                for metric, text in QUESTIONS.items():
                    qid = f"s{seed}_{shop}_{metric}__{scenario}"
                    question = f"{text} trong tháng {month}/{year} là bao nhiêu?"
                    questions.append({"id": qid, "business_id": shop, "question": question,
                                      "as_of_date": end.isoformat()})
                    cases.append({"id": qid, "business_id": shop, "metric": metric,
                                  "policy": policy_name, "layout": layout, "scenario": scenario,
                                  "fold": assignments[shop], "industry": groups[shop][0],
                                  **expected[metric]})
            jsonl(root / "questions.jsonl", questions)
            scenarios.append({"name": scenario, "input": root.relative_to(out).as_posix()})
    jsonl(out / "references.jsonl", cases)
    dump(out / "original_records.json", originals)
    dump(out / "business_groups.json", groups)
    dump(out / "folds.json", {"source_shop_fold": assignments, "business_groups": groups})
    hashes = {path.relative_to(out).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(out.rglob("*")) if path.is_file()}
    manifest = {"version": 1, "seed": seed, "shops": shops, "questions": len(cases),
                "start": start.isoformat(), "end": end.isoformat(), "scenarios": scenarios,
                "bindings": bindings_by_shop, "profiles": {shop: profile(orders) for shop, orders in originals.items()},
                "sha256": hashes, "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "limitations": ["generator author has read pipeline", "synthetic diagnostics only",
                                "references and fold metadata are outside inference inputs"]}
    dump(out / "suite.json", manifest)
    return manifest


def verify_suite(suite: Path) -> dict:
    manifest = json.loads((suite / "suite.json").read_text(encoding="utf-8"))
    for name, digest in manifest["sha256"].items():
        if hashlib.sha256((suite / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"suite file changed: {name}")
    return manifest


def answered(pred: dict) -> bool:
    return pred.get("type") not in (None, "abstain", "unanswerable")


def correct(expected: dict, predicted: dict) -> bool:
    if expected["type"] != predicted.get("type") or type(predicted.get("answer")) not in (int, float):
        return False
    try:
        value = Decimal(str(predicted["answer"]))
        return value.is_finite() and value == Decimal(str(expected["answer"]))
    except (KeyError, ArithmeticError, ValueError):
        return False


def score(cases: list[dict], predictions: list[dict]) -> dict:
    if not cases:
        raise ValueError("cannot score an empty suite")
    expected = {row["id"]: row for row in cases}
    if len(expected) != len(cases):
        raise ValueError("duplicate reference id")
    actual = {}
    for row in predictions:
        qid = row.get("id")
        if qid not in expected:
            raise ValueError("prediction has unknown id")
        if qid in actual:
            raise ValueError("duplicate prediction id")
        actual[qid] = row
    counts = defaultdict(lambda: {"correct": 0, "wrong": 0, "abstain": 0, "unanswerable": 0, "missing": 0})
    indexed = {}
    for case in cases:
        pred = actual.get(case["id"], {"type": "abstain"})
        outcome = "abstain" if pred.get("type") == "abstain" else ("correct" if correct(case, pred) else "wrong")
        for key in ("all", f"shop:{case['business_id']}", f"fold:{case['fold']}",
                    f"industry:{case['industry']}", f"policy:{case['policy']}", f"layout:{case['layout']}"):
            counts[key][outcome] += 1
            counts[key]["missing"] += case["id"] not in actual
            counts[key]["unanswerable"] += pred.get("type") == "unanswerable"
        indexed[case["business_id"], case["metric"], case["policy"], case["layout"]] = case
    for values in counts.values():
        total = values["correct"] + values["wrong"] + values["abstain"]
        values.update(total=total, pa=(values["correct"]-values["wrong"])/total if total else None)
    shops = [values for key, values in counts.items() if key.startswith("shop:")]
    bootstrap = []
    if len(shops) >= 2:
        rng = random.Random(17)
        for _ in range(1000):
            sample = rng.choices(shops, k=len(shops))
            bootstrap.append(sum(row["correct"]-row["wrong"] for row in sample) / sum(row["total"] for row in sample))
        bootstrap.sort()
    pairs = defaultdict(lambda: {"total": 0, "both_answered": 0, "both_correct": 0, "prediction_changed": 0})
    def pair(kind, left, right):
        a, b = actual.get(left["id"], {}), actual.get(right["id"], {})
        values = pairs[kind]
        values["total"] += 1
        values["both_answered"] += answered(a) and answered(b)
        values["both_correct"] += correct(left, a) and correct(right, b)
        values["prediction_changed"] += answered(a) and answered(b) and (a.get("type"), a.get("answer")) != (b.get("type"), b.get("answer"))
    for (shop, metric, policy_name, layout), case in indexed.items():
        if layout != "plain":
            base = indexed[shop, metric, policy_name, "plain"]
            if (base["type"], base["answer"]) != (case["type"], case["answer"]):
                raise ValueError("layout transformation changed the oracle")
            pair("layout_invariance", base, case)
        if policy_name != "base":
            base = indexed[shop, metric, "base", layout]
            changed = (base["type"], base["answer"]) != (case["type"], case["answer"])
            pair("rule_change" if changed else "rule_irrelevant", base, case)
    return {"counts": dict(counts), "pairs": dict(pairs), "prediction_count": len(actual),
            "pa_cluster_bootstrap_95": [bootstrap[25], bootstrap[974]] if bootstrap else None,
            "note": "Both-correct pairs require reference correctness; agreement or two abstains do not pass."}


def select_scenarios(cases: list[dict], names: list[str] | None) -> list[dict]:
    """Allow short paired pilots while requiring their unchanged-rule/layout anchors."""
    if names is None:
        return cases
    chosen = set(names)
    if not chosen or chosen - {case["scenario"] for case in cases}:
        raise ValueError("unknown or empty scenario selection")
    selected = [case for case in cases if case["scenario"] in chosen]
    for case in selected:
        required = {f"{case['policy']}__plain", f"base__{case['layout']}"}
        if not required <= chosen:
            raise ValueError("scenario selection needs the matching base-rule and plain-layout anchors")
    return selected


def probe_ingestion(suite: Path, out: Path) -> dict:
    """Oracle-bound ingestion measurement, deliberately excluding model semantics."""
    from bizreason.data import schema
    from bizreason.data.ingestion import BusinessBuilder
    from bizreason.utils import PathPolicy, quote_identifier
    manifest = verify_suite(suite)
    out.mkdir(parents=True, exist_ok=False)
    saved = dict(schema.ROLE_LOOKUP)
    observations = []
    try:
        schema.ROLE_LOOKUP.clear()
        with PathPolicy(mode="competition", allowed_roots=[out], forbidden_roots=[suite]):
            for scenario in manifest["scenarios"]:
                root = suite / scenario["input"]
                rule_hash = hashlib.sha256((root / "rulebook.md").read_bytes()).hexdigest()
                builder = BusinessBuilder(out / "cache", rule_hash=rule_hash)
                for shop, expected in manifest["profiles"].items():
                    context = builder.prepare(root / "data" / shop)
                    bindings = manifest["bindings"][shop]
                    quantity = Decimal(0); amount = Decimal(0); ids = set(); dates = []; count = 0
                    issues = []
                    with sqlite3.connect(context.database) as db:
                        for table in context.tables:
                            columns = {bindings[col.original_name]: col.name for col in table.columns if col.original_name in bindings}
                            needed = ("order_id", "ordered_at", "qty", "amount")
                            if not set(needed) <= columns.keys():
                                issues.append("unresolved source header")
                                continue
                            sql = "SELECT " + ",".join(quote_identifier(columns[field]) for field in needed)
                            sql += " FROM " + quote_identifier(table.name)
                            for oid, day, qty, money in db.execute(sql):
                                count += 1
                                quantity += Decimal(str(qty)); amount += Decimal(str(money))
                                if oid: ids.add(str(oid))
                                if day: dates.append(str(day)[:10])
                    observed = {"rows": count, "orders": len(ids), "quantity": int(quantity), "amount": int(amount),
                                "date_min": min(dates) if dates else None, "date_max": max(dates) if dates else None}
                    issues += [key for key in expected if observed[key] != expected[key]]
                    observations.append({"scenario": scenario["name"], "business_id": shop,
                                         "passed": not issues, "issues": issues, "expected": expected,
                                         "observed": observed, "schema_complete": context.schema_complete})
    finally:
        schema.ROLE_LOOKUP.clear(); schema.ROLE_LOOKUP.update(saved)
    import bizreason
    package = Path(bizreason.__file__).parent
    report = {"mode": "ingestion_with_oracle_schema_bindings", "model_calls": 0,
              "passed": sum(row["passed"] for row in observations), "total": len(observations),
              "observations": observations, "source_sha256": {
                  p.relative_to(package).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted((package / "data").glob("*.py"))}}
    dump(out / "summary.json", report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate")
    gen.add_argument("--out", type=Path, required=True); gen.add_argument("--shops", type=int, default=4)
    gen.add_argument("--seed", type=int, default=71); gen.add_argument("--folds", type=int, default=3)
    probe = commands.add_parser("probe-ingestion")
    probe.add_argument("--suite", type=Path, required=True); probe.add_argument("--out", type=Path, required=True)
    scorer = commands.add_parser("score")
    scorer.add_argument("--suite", type=Path, required=True)
    scorer.add_argument("--predictions", nargs="+", type=Path, required=True)
    scorer.add_argument("--out", type=Path, required=True)
    scorer.add_argument("--scenarios", nargs="+", help="score a short paired pilot including its base/plain anchors")
    args = parser.parse_args(argv)
    if args.command == "generate":
        result = generate(args.out.resolve(), args.shops, args.seed, args.folds)
        print(json.dumps({"shops": result["shops"], "questions": result["questions"], "scenarios": len(result["scenarios"])}))
    elif args.command == "probe-ingestion":
        result = probe_ingestion(args.suite.resolve(), args.out.resolve())
        print(json.dumps({key: result[key] for key in ("mode", "model_calls", "passed", "total")}))
        return int(result["passed"] != result["total"])
    else:
        verify_suite(args.suite)
        all_cases = load_lines(args.suite / "references.jsonl")
        predictions = list(itertools.chain.from_iterable(load_lines(p) for p in args.predictions))
        known = {case["id"] for case in all_cases}
        seen = set()
        for pred in predictions:
            if pred.get("id") not in known or pred["id"] in seen:
                raise ValueError("unknown or duplicate prediction id")
            seen.add(pred["id"])
        selected = select_scenarios(all_cases, args.scenarios)
        wanted = {case["id"] for case in selected}
        result = score(selected, [pred for pred in predictions if pred["id"] in wanted])
        result["selected_scenarios"] = sorted({case["scenario"] for case in selected})
        result["prediction_sha256"] = {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in args.predictions}
        result["mode"] = "external_predictions_provenance_not_independently_verified"
        dump(args.out, result)
        print(json.dumps(result["counts"]["all"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
