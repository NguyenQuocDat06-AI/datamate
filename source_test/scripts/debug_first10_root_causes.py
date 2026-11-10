"""Reproduce four reviewed failures locally using frozen plans, without an API.

Production code, cached databases and original predictions are read-only here.
Counterfactual changes exist only in memory or in a separate fixture directory.
"""

from __future__ import annotations

import copy
import csv
import json
import sqlite3
from decimal import Decimal
from itertools import islice
from pathlib import Path
import sys

_workspace = Path(__file__).resolve().parents[1]
_legacy = _workspace / "artifacts/public_50_trammay/runtime_snapshot"
if _legacy.exists():
    sys.path.insert(0, str(_legacy))
    import bizreason.utils as _legacy_utils
    _legacy_utils.WORKSPACE = _workspace

from bizreason.compilation import CompilationError, TemplateCompiler
from bizreason.config import Settings
from bizreason.contracts import BusinessContext, Candidate, QueryPlan, Question
from bizreason.data.ingestion import BusinessBuilder, infer_locale
from bizreason.data.layout import is_summary
from bizreason.data.parsing import parse_number
from bizreason.data.readers import read_sheets
from bizreason.decision import DecisionGate, UnsupportedEvidence, verify_unsupported
from bizreason.execution import execute_sql
from bizreason.llm import Usage
from bizreason.pipeline import Solver
from bizreason.planning import validate_plan
from bizreason.rules import RuleRegistry
from bizreason.temporal import TemporalResolver
from bizreason.utils import digest, file_hash, quote_identifier
from bizreason.verification import verify_candidate
from scripts.review_public_first10 import number as independent_money

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/public_first10/debug"
IDS = {
    "revenue": "q_00bf8df3ea0b",
    "ranking": "q_01ba91ff94c6",
    "unsupported": "q_008fdebb6ec9",
    "average": "q_01ac0fc9f2a2",
}


class RecordedModel:
    """Return previously generated public plans; never performs inference."""

    def __init__(self, responses):
        self.responses = iter(responses)
        self.usage = Usage()

    def generate_json(self, system, prompt, *, timeout):
        self.usage.calls += 1
        return copy.deepcopy(next(self.responses))


def read_context(business_id, manifest):
    revision = manifest["business_revisions"][business_id]
    path = ROOT / "artifacts/cache" / business_id / revision[:20] / "catalog.json"
    return BusinessContext.model_validate_json(path.read_text(encoding="utf-8"))


def read_db(context):
    db = sqlite3.connect(context.database.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def run_program(context, plan, program):
    execution = execute_sql(context.database, program.sql, program.parameters, plan.tables, timeout=20)
    candidate = Candidate(
        id="debug", plan_hash=digest(plan), code_hash=digest(program),
        source_revision=context.revision, program=program, execution=execution, output=plan.output,
    )
    candidate = verify_candidate(candidate, plan)
    decision = DecisionGate().choose(candidate, UnsupportedEvidence(False, "not established"))
    return {"status": execution.status.value, "diagnostics": execution.diagnostics,
            "rows": execution.rows, "value": candidate.value, "verified": candidate.verified,
            "checks": candidate.checks, "gate_action": decision.action}


def fixture_summary(rules):
    directory = OUT / "summary_fixture"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "sales.csv"
    header = ["order_id", "ordered_at", "status", "customer_name", "product_name", "qty_sale", "line_amount"]
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows([
            header,
            ["A", "2025-03-04", "completed", "Khách lẻ", "Món A", 1, 100],
            [None, None, None, "TỔNG NGÀY 04/03", None, None, 100],
            ["B", "2025-03-05", "completed", "Khách lẻ", "Món B", 1, 200],
        ])
    context = BusinessBuilder(OUT / "fixture_cache", rule_hash=rules.revision).prepare(directory)
    with read_db(context) as db:
        rows = [dict(row) for row in db.execute(f'SELECT * FROM "{context.tables[0].name}"')]
    return {"expected_transaction_rows": 2, "actual_rows": len(rows), "rows": rows,
            "expected_total": 300, "actual_total": sum(row["line_amount"] for row in rows)}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ROOT / "artifacts/public_50_trammay/run_manifest.json").read_text(encoding="utf-8"))
    review = json.loads((ROOT / "artifacts/public_first10/review.json").read_text(encoding="utf-8"))
    details = {row["question"]["id"]: row for row in review["details"]}
    cases = {}
    contexts = {}
    for name, identity in IDS.items():
        detail = details[identity]
        context = read_context(detail["question"]["business_id"], manifest)
        trace = json.loads((ROOT / detail["trace"]).read_text(encoding="utf-8"))
        contexts[context.business_id] = context
        cases[name] = context, trace
    before = {bid: file_hash(c.database) for bid, c in contexts.items()}
    rules = RuleRegistry.load(ROOT / "data/public_test/rulebook.md")
    compiler = TemplateCompiler(rules)
    result = {"api_calls": 0, "uses_frozen_plans": True}

    context, trace = cases["revenue"]
    plan = QueryPlan.model_validate(trace["plans"][0])
    table = context.table(plan.tables[0])
    amount_column = table.column("tong_tien")
    source = table.sources[0]
    reader = read_sheets(ROOT / "data/public_test/data" / context.business_id / source.path)
    sheet = next(reader)
    raw_sample = list(islice(sheet.rows, table.header_row, table.header_row + 2000))
    reader.close()
    money_roles = {"line_amount", "unit_price", "cost_sale", "total_due", "paid_amount"}
    locale_profiles = {
        column.name: infer_locale([row[i] for row in raw_sample if len(row) > i], context.readme, currency=True)
        for i, column in enumerate(table.columns)
        if column.semantic_role in money_roles
    }
    locale = next(item.split(":")[1] for item in amount_column.evidence if item.startswith("numeric_locale:"))
    parsing = [
        {"raw": value, "column_locale": locale, "status": parse_number(value, locale=locale, unit="VND").status.value,
         "parsed": str(parse_number(value, locale=locale, unit="VND").value),
         "parsed_without_column_locale": str(parse_number(value, unit="VND").value)}
        for value in ["7.000đ", "105.000đ", "45k", "50,500"]
    ]
    roles = [column.semantic_role for column in table.columns if not column.name.startswith("derived_")]
    with read_db(context) as db:
        footer = dict(db.execute(f'SELECT * FROM "{table.name}" WHERE __source_row=813').fetchone())
    raw_footer = json.loads(footer["__raw_json"])
    with read_db(context) as db:
        valid_rows = [dict(row) for row in db.execute(
            f'SELECT tong_tien,__source_row,__raw_json FROM "{table.name}" '
            "WHERE ngay_dat_hang>=? AND ngay_dat_hang<? "
            "AND trang_thai_don_hang NOT IN ('cancelled','returned') "
            f'AND ma_don_hang NOT IN (SELECT ma_don_hang FROM "{table.name}" '
            "WHERE trang_thai_don_hang IN ('cancelled','returned'))",
            ["2025-03-04", "2025-03-05"],
        )]
    ablation = {
        "current": sum(Decimal(str(row["tong_tien"])) for row in valid_rows),
        "fix_money_only": sum(independent_money(json.loads(row["__raw_json"])[7]) for row in valid_rows),
        "fix_summary_only": sum(Decimal(str(row["tong_tien"])) for row in valid_rows if row["__source_row"] != 813),
        "fix_both": sum(independent_money(json.loads(row["__raw_json"])[7]) for row in valid_rows if row["__source_row"] != 813),
    }
    ablation = {name: int(value) for name, value in ablation.items()}
    assert ablation["current"] == 7893391 and ablation["fix_both"] == 4533000
    result["revenue"] = {
        "raw_sample_rows": len(raw_sample),
        "financial_locales_before_sharing": locale_profiles,
        "effective_line_amount_locale_after_sharing": locale,
        "recorded_locale_evidence": amount_column.evidence,
        "parsing_reproduction": parsing,
        "footer_before_inheritance": raw_footer,
        "footer_after_inheritance": {k: v for k, v in footer.items() if not k.startswith("__")},
        "is_summary_middle_of_sheet": is_summary(raw_footer, roles, after_last_record=False),
        "is_summary_even_if_last": is_summary(raw_footer, roles, after_last_record=True),
        "minimal_ingestion_reproduction": fixture_summary(rules),
        "ablation": ablation,
        "current_candidate": run_program(context, plan, compiler.compile(plan, context)),
    }

    context, trace = cases["ranking"]
    plan = QueryPlan.model_validate(trace["plans"][0])
    program = compiler.compile(plan, context)
    corrected = program.model_copy(update={"sql": program.sql.replace("GROUP BY ma_hang, ten_hang", "GROUP BY ma_hang")})
    assert corrected.sql != program.sql
    with read_db(context) as db:
        aliases = [dict(row) for row in db.execute(
            f'SELECT ten_hang, SUM(sl_le) qty FROM "{plan.tables[0]}" '
            "WHERE ngay_xuat>=? AND ngay_xuat<? AND tt_don NOT IN ('cancelled','returned') "
            "AND ma_hang='SP0003' GROUP BY ten_hang ORDER BY qty DESC", program.parameters)]
    result["ranking"] = {
        "plan_validation_errors": validate_plan(plan, context, rules, Question.model_validate(trace["question"])),
        "template_eligible": plan.template_eligible, "group_by": plan.group_by,
        "actual_backend": program.backend,
        "original": run_program(context, plan, program),
        "group_by_code_only": run_program(context, plan, corrected),
        "aliases_of_same_product": aliases,
    }
    try:
        compiler._compile_metric(plan, context)
        result["ranking"]["direct_template_blocker"] = None
    except CompilationError as error:
        result["ranking"]["direct_template_blocker"] = str(error)

    context, trace = cases["unsupported"]
    plans = [QueryPlan.model_validate(p) for p in trace["plans"]]
    result["unsupported"] = {
        "validation_errors_by_attempt": [validate_plan(p, context, rules, Question.model_validate(trace["question"])) for p in plans],
        "missing_role_present_in_catalog": any(col.semantic_role == "product_category" for tb in context.tables for col in tb.columns),
        "claim_result": verify_unsupported(plans[-1], context).__dict__,
        "counterfactual_explicit_readme_sentence": verify_unsupported(
            plans[-1], context.model_copy(update={"readme": context.readme + "\nKhông có nhóm hàng.\n"})
        ).__dict__,
        "counterfactual_note": "In-memory diagnostic only; no statement was added to the actual README.",
    }

    context, trace = cases["average"]
    plans = [QueryPlan.model_validate(p) for p in trace["plans"]]
    question = Question.model_validate(trace["question"])
    plan = plans[-1]
    program = compiler.compile(plan, context)
    sql = program.sql
    for name in plan.tables:
        public = ", ".join(quote_identifier(c.name) for c in context.table(name).columns)
        sql = sql.replace(f"SELECT *\n    FROM {name}", f"SELECT {public}\n    FROM {name}")
    assert sql != program.sql
    patched_tables = []
    with read_db(context) as db:
        for table in context.tables:
            if table.name in plan.tables:
                low, high = db.execute(f'SELECT MIN(gio_tao_don),MAX(gio_tao_don) FROM "{table.name}"').fetchone()
                cols = [c.model_copy(update={"semantic_role": "ordered_at"}) if c.name == "gio_tao_don" else c for c in table.columns]
                table = table.model_copy(update={"columns": cols, "coverage": {"gio_tao_don": (low, high)}})
            patched_tables.append(table)
    patched_context = context.model_copy(update={"tables": patched_tables})
    result["average"] = {
        "validation_errors_by_attempt": [validate_plan(p, context, rules, question) for p in plans],
        "coverage_in_original_catalog": {name: context.table(name).coverage for name in plan.tables},
        "core_role_mapping": {c.name: c.semantic_role for c in context.table(plans[0].tables[0]).columns
                              if c.name in {"don_so", "gio_tao_don", "trang_thai_giao", "thanh_tien_d", "barcode", "hang_hoa"}},
        "source_grain": context.table(plans[0].tables[0]).grain,
        "first_plan_errors_with_time_role_and_coverage_in_memory": validate_plan(plans[0], patched_context, rules, question),
        "original": run_program(context, plan, program),
        "expand_source_stars_only": run_program(context, plan, program.model_copy(update={"sql": sql})),
        "repaired_program": sql,
        "execution_candidate_belongs_to_plan": 1 + next(i for i, p in enumerate(plans) if digest(p) == trace["candidates"][0]["plan_hash"]),
    }
    for name in ("unsupported", "average"):
        context, trace = cases[name]
        model = RecordedModel(trace["plans"])
        prediction, replay = Solver(model, rules, TemporalResolver(), Settings(question_timeout=60, sql_timeout=20)).solve(
            Question.model_validate(trace["question"]), context)
        result[name]["controller_replay"] = {
            "prediction": prediction.model_dump(mode="json", exclude_none=True),
            "model_calls_replayed": replay.model_calls, "repair_count": replay.repair_count,
            "errors": replay.errors, "candidate_checks": [c.checks for c in replay.candidates],
        }
    result["all_database_hashes_unchanged"] = all(before[bid] == file_hash(c.database) for bid, c in contexts.items())
    assert result["all_database_hashes_unchanged"]
    assert result["revenue"]["minimal_ingestion_reproduction"]["actual_total"] == 400
    assert result["ranking"]["original"]["value"] != result["ranking"]["group_by_code_only"]["value"]
    assert result["average"]["expand_source_stars_only"]["value"] == 71724
    assert all(result[n]["controller_replay"]["prediction"]["type"] == "abstain" for n in ("unsupported", "average"))
    (OUT / "root_causes.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"api_calls": 0, "causes_reproduced": list(IDS),
                      "all_database_hashes_unchanged": result["all_database_hashes_unchanged"],
                      "explicit_columns_average": result["average"]["expand_source_stars_only"]["value"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
