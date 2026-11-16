"""Bind a simple whole-business net-profit question to typed source columns.

No question IDs, business IDs, expected answers or generated-SQL rewrites are used.
Unsupported scopes retain the ordinary planner path.
"""

from __future__ import annotations

from dataclasses import dataclass

from bizreason.contracts import BusinessContext, PlanFilter, Program, QueryPlan, Question
from bizreason.data.parsing import ParseStatus, parse_date
from bizreason.data.readers import sqlite_readonly
from bizreason.rules import RuleRegistry
from bizreason.temporal import TemporalResolver
from bizreason.utils import normalize_lookup, quote_identifier


@dataclass(frozen=True)
class ProfitBinding:
    plan: QueryPlan
    program: Program


def bind_net_profit(plan: QueryPlan, question: Question, context: BusinessContext,
                    rules: RuleRegistry) -> ProfitBinding | None:
    text = normalize_lookup(question.question)
    if "lai rong" not in text and "loi nhuan rong" not in text:
        return None
    allowed = set("lai rong loi nhuan cua quan cua hang tiem shop trong thang nam nay truoc tu dau toi den het hom hnay la bao nhieu bnhiu duoc tong cong roi ca".split())
    if any(token not in allowed and not token.isdigit() for token in text.split()):
        return None
    if "%" in question.question or plan.group_by or plan.entity_constraints or plan.joins:
        return None
    if plan.output.type != "number" or plan.output.shape != "scalar" or plan.limit is not None:
        return None
    windows = TemporalResolver().resolve(question.question, question.as_of_date)
    if len(windows) != 1 or len(plan.windows) != 1:
        return None
    window = windows[0]
    if (window.start, window.end_exclusive) != (plan.windows[0].start, plan.windows[0].end_exclusive):
        raise ValueError("NET_PROFIT_WINDOW_DISAGREES_WITH_QUESTION")
    rule_text = normalize_lookup(rules.text)
    if not rules.standard_policy_supported("gross_profit") or not all(
        term in rule_text for term in ["lai rong", "so chi phi", "moi khoan"]
    ):
        return None
    q = quote_identifier
    sales, expenses = [], []
    for name in plan.tables:
        table = context.table(name)
        if table is None:
            return None
        if table.grain == "order_line" and all(table.role(r) for r in (
            "order_id", "ordered_at", "status", "line_amount", "qty_sale", "cost_sale"
        )):
            sales.append(table)
        elif table.role("expense_amount"):
            date_col = table.role("expense_at") or next((c.name for c in table.columns
                if normalize_lookup(c.original_name) in {"ngay mua", "ngay chi", "ngay chi phi"}), None)
            if date_col:
                expenses.append((table, date_col))
    if not sales or not expenses:
        return None
    status_fields = {f"{t.name}.{t.role('status')}" for t in sales}
    date_fields = {f"{t.name}.{t.role('ordered_at')}" for t in sales} | {f"{t.name}.{d}" for t,d in expenses}
    for condition in plan.filters:
        if condition.field in date_fields:
            if condition.operator == "gte" and condition.values == [window.start.isoformat()]:
                continue
            if condition.operator == "lt" and condition.values == [window.end_exclusive.isoformat()]:
                continue
        if condition.field in status_fields and condition.operator in {"in", "not_in"}:
            # Confirm the declared old aliases have exactly the ordinary revenue meaning.
            from bizreason.data.parsing import parse_status
            states = {parse_status(v).value for v in condition.values}
            if (condition.operator == "not_in" and states == {"cancelled", "returned"}
                or condition.operator == "in" and states == {"completed", "shipping"}):
                continue
        return None  # Never discard a subgroup or additional condition.
    required = []
    sales_sql = []
    for table in sales:
        roles = ["order_id", "ordered_at", "status", "line_amount", "qty_sale", "cost_sale"]
        fields = [table.role(role) for role in roles]
        if any(table.column(field).parse_errors or table.column(field).null_count for field in fields):
            raise ValueError("NET_PROFIT_INCOMPLETE_REQUIRED_COLUMN")
        required.extend(f"{table.name}.{field}" for field in fields)
        scope = q(table.role("scope")) if table.role("scope") else "''"
        sales_sql.append(f"SELECT {scope} AS scope_key," + ",".join(
            f"{q(field)} AS {alias}" for field,alias in zip(fields,
            ["order_key","sold_at","state","amount","quantity","unit_cost"])
        ) + f" FROM {q(table.name)}")
    expense_sql = []
    connection = sqlite_readonly(context.database)
    try:
        for table, date_col in expenses:
            amount = table.role("expense_amount")
            if table.column(amount).parse_errors or table.column(amount).null_count:
                raise ValueError("NET_PROFIT_INCOMPLETE_EXPENSE_AMOUNT")
            for (value,) in connection.execute(f"SELECT DISTINCT {q(date_col)} FROM {q(table.name)}"):
                if parse_date(value).status != ParseStatus.OK:
                    raise ValueError("NET_PROFIT_UNRESOLVED_EXPENSE_DATE")
            required.extend([f"{table.name}.{date_col}", f"{table.name}.{amount}"])
            expense_sql.append(f"SELECT iso_date({q(date_col)}) AS spent_at,{q(amount)} AS amount FROM {q(table.name)}")
    finally:
        connection.close()
    sql = f"""WITH sales AS ({' UNION ALL '.join(sales_sql)}),
    eligible AS (
      SELECT s.* FROM sales s WHERE sold_at>=? AND sold_at<?
      AND state IN ('completed','shipping')
      AND NOT EXISTS (SELECT 1 FROM sales b WHERE b.scope_key=s.scope_key
        AND b.order_key=s.order_key AND b.state IN ('cancelled','returned'))
    ), expenses AS ({' UNION ALL '.join(expense_sql)})
    SELECT decimal_sub(decimal_sub(
      COALESCE((SELECT decimal_sum(amount) FROM eligible),'0'),
      COALESCE((SELECT decimal_sum(decimal_mul(quantity,unit_cost)) FROM eligible),'0')),
      COALESCE((SELECT decimal_sum(amount) FROM expenses WHERE spent_at>=? AND spent_at<?),'0'))
    """
    parameters = [window.start.isoformat(), window.end_exclusive.isoformat()] * 2
    refs = list(dict.fromkeys([*plan.rule_refs, *rules.required_refs("gross_profit")]))
    normalized = plan.model_copy(update={
        "sql": sql, "parameters": parameters, "rule_refs": refs, "required_fields": required,
        "filters": [PlanFilter(field=f, operator="in", values=["completed", "shipping"]) for f in sorted(status_fields)],
        "output": plan.output.model_copy(update={"rounding": "money"}),
        "derivation_notes": [*plan.derivation_notes, "net_profit:typed_source_binding:v1"],
    })
    return ProfitBinding(normalized, Program(sql=sql, parameters=parameters, backend="template", policy_refs=refs))
