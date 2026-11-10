from __future__ import annotations

from bizreason.contracts import BusinessContext, Program, QueryPlan, TableMeta
from bizreason.rules import RuleRegistry
from bizreason.utils import quote_identifier
from bizreason.sql_parameters import bind_lookup_parameters


class CompilationError(ValueError):
    pass


def _table(context: BusinessContext, plan: QueryPlan, grain: str, required: set[str]) -> TableMeta:
    candidates = [
        table
        for table in context.tables
        if table.name in plan.tables
        and table.grain == grain
        and all(table.role(role) for role in required)
    ]
    if len(candidates) != 1:
        raise CompilationError(
            f"Template needs exactly one {grain} source with {sorted(required)}; use SQL fallback for partitions/joins"
        )
    table = candidates[0]
    if not table.row_count:
        raise CompilationError("Empty source does not establish temporal coverage")
    scope = next((column for column in table.columns if column.semantic_role == "scope"), None)
    if scope and len(scope.examples) > 1:
        raise CompilationError("Multiple scopes require an explicit composite-key SQL plan")
    for column in table.columns:
        if column.semantic_role in required and (column.parse_errors or column.null_count):
            raise CompilationError(
                f"Required template field has missing/invalid cells: {table.name}.{column.name}"
            )
    return table


class TemplateCompiler:
    def __init__(self, rules: RuleRegistry):
        self.rules = rules

    def compile(self, plan: QueryPlan, context: BusinessContext) -> Program:
        if plan.metric == "generic_sql":
            return self._fallback(plan)
        if plan.metric == "unsupported":
            raise CompilationError("Unsupported claims are not executable")
        if not plan.template_eligible:
            return self._fallback(plan)
        if not self.rules.standard_policy_supported(plan.metric):
            return self._fallback(plan)
        if plan.entity_constraints or not self._builtin_conditions_only(plan, context):
            return self._fallback(plan)  # A plain metric template must not drop extra filters.
        expected = {
            "sales_revenue": {"number"},
            "cancellation_rate": {"percent"},
            "top_products": {"string", "list"},
            "debt_total": {"number"},
            "gross_profit": {"number"},
        }
        if plan.output.type.value not in expected[plan.metric]:
            return self._fallback(plan)
        if len(plan.windows) != 1:
            return self._fallback(plan)
        if plan.metric != "debt_total" and any(window.mode == "cumulative" for window in plan.windows):
            return self._fallback(plan)  # Period templates must not discard earlier history.
        try:
            sql, parameters = self._compile_metric(plan, context)
        except CompilationError:
            if plan.sql:
                return self._fallback(plan)
            raise
        return Program(
            sql=sql, parameters=parameters, backend="template", policy_refs=plan.rule_refs
        )

    @staticmethod
    def _builtin_conditions_only(plan: QueryPlan, context: BusinessContext) -> bool:
        if len(plan.windows) != 1 or plan.joins and plan.metric != "debt_total":
            return False
        window = plan.windows[0]
        allowed_grouping = set()
        if plan.metric == "top_products":
            for name in plan.tables:
                table = context.table(name)
                if table:
                    allowed_grouping.update(f"{name}.{table.role(role)}" for role in ("product_id", "product_name") if table.role(role))
        if not set(plan.group_by) <= allowed_grouping:
            return False
        for item in plan.filters:
            name, _, column = item.field.rpartition(".")
            table = context.table(name)
            field = table.column(column) if table else None
            if field is None:
                return False
            if field.semantic_role == "ordered_at":
                if item.operator == "gte" and item.values == [window.start.isoformat()]:
                    continue
                if item.operator == "lt" and item.values == [window.end_exclusive.isoformat()]:
                    continue
            if field.semantic_role == "status" and plan.metric in {"sales_revenue", "top_products", "gross_profit"}:
                if item.operator == "not_in" and set(item.values) == {"cancelled", "returned"}:
                    continue
                if item.operator == "in" and set(item.values) == {"completed", "shipping"}:
                    continue
            return False
        return True

    @staticmethod
    def _fallback(plan: QueryPlan) -> Program:
        if not plan.sql or not plan.sql.strip():
            raise CompilationError("No compatible template and no generated SQL supplied")
        return Program(
            sql=plan.sql,
            parameters=bind_lookup_parameters(plan.sql, plan.parameters),
            backend="generated_sql",
            policy_refs=plan.rule_refs,
        )

    def _compile_metric(self, plan: QueryPlan, context: BusinessContext) -> tuple[str, list]:
        q = quote_identifier
        start, end = plan.windows[0].start.isoformat(), plan.windows[0].end_exclusive.isoformat()
        base = {"order_id", "ordered_at", "status"}
        if plan.metric == "debt_total":
            return self._debt(plan, context, end)
        top_qty_role = "qty_base"
        if plan.metric == "top_products":
            try:
                table = _table(context, plan, "order_line", base | {"qty_base"})
            except CompilationError:
                has_any_qty_base = any(col.semantic_role == "qty_base" for t in context.tables for col in t.columns)
                has_any_conversion = any(col.semantic_role == "conversion_factor" for t in context.tables for col in t.columns)
                if not has_any_qty_base and not has_any_conversion:
                    table = _table(context, plan, "order_line", base | {"qty_sale"})
                    top_qty_role = "qty_sale"
                else:
                    raise
        else:
            extra = {
                "sales_revenue": {"line_amount"},
                "cancellation_rate": set(),
                "gross_profit": {"line_amount", "qty_sale", "cost_sale"},
            }[plan.metric]
            grain = (
                "order_header"
                if plan.metric == "cancellation_rate"
                and any(
                    table.name in plan.tables and table.grain == "order_header"
                    for table in context.tables
                )
                else "order_line"
            )
            table = _table(context, plan, grain, base | extra)
        column = lambda role: q(table.role(role))
        source = q(table.name)
        period = f"{column('ordered_at')}>=? AND {column('ordered_at')}<?"
        valid = f"{column('status')} IN ('completed','shipping')"
        if plan.metric == "sales_revenue":
            return (
                f"SELECT COALESCE(decimal_sum({column('line_amount')}),'0') AS value FROM {source} WHERE {period} AND {valid}",
                [start, end],
            )
        if plan.metric == "cancellation_rate":
            qty_col = table.role("qty_sale")
            placed_filter = (
                f" AND CAST({q(qty_col)} AS REAL) > 0"
                if grain == "order_line" and qty_col
                else ""
            )
            return (
                f"SELECT COUNT(DISTINCT CASE WHEN {column('status')}='cancelled' THEN {column('order_id')} END), "
                f"COUNT(DISTINCT {column('order_id')}) FROM {source} WHERE {period}{placed_filter}"
            ), [start, end]
        if plan.metric == "gross_profit":
            # Use exact per-line multiplication, not base quantity or current catalog cost.
            return (
                f"SELECT COALESCE(decimal_sum(CAST({column('line_amount')} AS TEXT)), '0'), "
                f"COALESCE(decimal_sum(decimal_mul({column('qty_sale')},{column('cost_sale')})), '0') "
                f"FROM {source} WHERE {period} AND {valid}"
            ), [start, end]
        product = table.role("product_id") or table.role("product_name")
        name = table.role("product_name") or product
        if not product or not name:
            raise CompilationError("Product identity is unavailable")
        if not table.role("product_name"):
            raise CompilationError("Service-fee exclusion requires a verified product-name lookup")
        if not table.role("product_id") and any(
            other.grain == "product_dimension" and other.role("product_id")
            for other in context.tables
        ):
            raise CompilationError("Code tie-break requires joining the supplied product catalog")
        # Product codes are returned as accepted identities when a name lookup is unavailable.
        service = f"lookup({q(name)}) <> 'phi phuc vu'"
        return (
            f"SELECT MIN({q(name)}) FROM {source} WHERE {period} AND {valid} AND {service} "
            f"GROUP BY {q(product)} ORDER BY CAST(decimal_sum({column(top_qty_role)}) AS REAL) DESC, {q(product)} ASC LIMIT ?"
        ), [start, end, plan.limit or 1]

    def _debt(self, plan: QueryPlan, context: BusinessContext, end: str) -> tuple[str, list]:
        q = quote_identifier
        required = {"order_id", "ordered_at", "status", "total_due", "payment_method"}
        try:
            orders = _table(context, plan, "order_header", required)
        except CompilationError:
            orders = _table(context, plan, "order_line", required)
        payments = _table(context, plan, "payment_event", {"order_id", "paid_at", "paid_amount"})
        oc = lambda role: q(orders.role(role))
        pc = lambda role: q(payments.role(role))
        # Aggregate payment events before joining; recorded returns remain eligible debt.
        channel = orders.role("sales_channel")
        channel_filter = (
            f"AND lookup({q(channel)}) NOT IN ('shopee','lazada','tiki','san','san tmdt','marketplace','tmdt')"
            if channel
            else ""
        )
        sql = f"""WITH o AS (
            SELECT {oc('order_id')} AS id, MAX({oc('total_due')}) AS due
            FROM {q(orders.name)} WHERE {oc('ordered_at')} < ?
            AND {oc('status')} <> 'cancelled'
            AND lookup({oc('payment_method')}) NOT IN ('cod','giao hang thu tien','san','san tmdt','marketplace','tmdt')
            {channel_filter}
            GROUP BY {oc('order_id')}
        ), p AS (
            SELECT {pc('order_id')} AS id, decimal_sum({pc('paid_amount')}) AS paid
            FROM {q(payments.name)} WHERE {pc('paid_at')} < ? GROUP BY {pc('order_id')}
        ) SELECT COALESCE(decimal_sum(decimal_positive(decimal_sub(o.due,COALESCE(p.paid,'0')))),'0')
          FROM o LEFT JOIN p ON o.id=p.id"""
        return sql, [end, end]
