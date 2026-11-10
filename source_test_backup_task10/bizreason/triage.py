from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from typing import Any, Protocol, runtime_checkable

from bizreason.contracts import (
    AnswerType,
    BusinessContext,
    Candidate,
    OutputSpec,
    PlanFilter,
    QueryPlan,
    Question,
    TimeWindow,
)
from bizreason.llm import LanguageModel, ModelError
from bizreason.planning import validate_plan
from bizreason.rules import RuleRegistry
from bizreason.temporal import TemporalResolver


@runtime_checkable
class SemanticCritic(Protocol):
    def score(
        self,
        question: Question,
        candidate: Candidate,
        context: BusinessContext,
        rules: RuleRegistry,
        timeout: float | None = None,
    ) -> float:
        ...


CRITIC_SYSTEM_PROMPT = (
    "You are a strict Vietnamese business SQL auditor. Audit whether the candidate SQL "
    "and output type strictly obey the Vietnamese question and runtime rulebook policies.\n"
    "Audit criteria:\n"
    "1. Unit compatibility and currency rounding (e.g. VND vs units, integer vs decimal rounding).\n"
    "2. Status filter policy (e.g. completed/shipping vs cancelled, returns).\n"
    "3. Period and comparison direction (e.g. MoM, YoY, calendar vs lunar dates, correct date bounds).\n"
    "4. Customer/product identity grouping (e.g. by customer_id vs phone, product_id vs name).\n"
    "5. Requested output order and limit (e.g. descending order, top N limit).\n\n"
    "Respond ONLY with a JSON object containing exactly these fields:\n"
    '{"verdict": "aligned" | "policy_violation" | "uncertain", "score": <float between 0.0 and 1.0>, "reason": "<brief explanation>"}'
)


class LLMSemanticCritic:
    """Deterministic LLM-backed semantic critic auditing candidates against Vietnamese business rules.

    Produces bounded heuristic audit scores in [0.0, 1.0] for Solver and DecisionGate.
    In verified mode, scores >= 0.5 denote alignment while scores < 0.5 denote potential policy violations.
    On model errors or timeouts, fails safe (returning 0.5) so transient critic glitches never crash the run.
    Note: DecisionGate accepts semantic_score as an evaluation parameter; when running in heuristic
    or verified modes, this bounded score provides a quantitative audit of candidate validity.
    """

    def __init__(self, model: LanguageModel, *, timeout: float = 30.0):
        self.model = model
        self.timeout = timeout

    def audit(
        self,
        question: Question,
        candidate: Candidate,
        context: BusinessContext,
        rules: RuleRegistry,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Audit candidate against rules and return the structured verdict dict."""
        effective_timeout = (
            self.timeout
            if timeout is None
            else max(0.1, min(self.timeout, timeout))
        )
        rule_summary = rules.text[:12000] if hasattr(rules, "text") and rules.text else ""
        readme_summary = context.readme[:4000] if getattr(context, "readme", None) else ""

        tables_desc = []
        for tname in candidate.execution.accessed_tables:
            tbl = context.table(tname)
            if tbl:
                col_descs = [f"{c.original_name} (role: {c.semantic_role or 'none'})" for c in tbl.columns]
                tables_desc.append(f"- Table '{tname}': {', '.join(col_descs)}")
        tables_str = "\n".join(tables_desc) if tables_desc else "None"

        user_prompt = (
            f"Question: {question.question}\n"
            f"As-of Date: {question.as_of_date.isoformat()}\n"
            f"Candidate Output Type: {candidate.output.type.value}\n"
            f"Candidate Output Rounding: {candidate.output.rounding}\n"
            f"Candidate Value: {candidate.value}\n"
            f"Candidate Bound Parameters: {candidate.program.parameters}\n"
            f"Candidate SQL:\n{candidate.program.sql}\n\n"
            f"Accessed Tables & Columns:\n{tables_str}\n\n"
            f"Business README Excerpt:\n{readme_summary}\n\n"
            f"Rulebook Excerpt:\n{rule_summary}\n\n"
            "Audit the SQL and output against the question and rules. "
            "Return JSON with keys 'verdict', 'score', and 'reason'."
        )
        try:
            payload = self.model.generate_json(
                CRITIC_SYSTEM_PROMPT,
                user_prompt,
                timeout=effective_timeout,
            )
            verdict = payload.get("verdict")
            if verdict not in {"aligned", "policy_violation", "uncertain"}:
                verdict = "uncertain"
            raw_score = payload.get("score")
            if raw_score is not None:
                try:
                    score = max(0.0, min(1.0, float(raw_score)))
                except (ValueError, TypeError):
                    score = 0.5
            else:
                score = (
                    1.0
                    if verdict == "aligned"
                    else (0.0 if verdict == "policy_violation" else 0.5)
                )
            reason = str(payload.get("reason", ""))
            return {"verdict": verdict, "score": score, "reason": reason}
        except (ModelError, Exception) as exc:
            return {
                "verdict": "uncertain",
                "score": 0.5,
                "reason": f"Semantic critic failed safe: {type(exc).__name__}",
            }

    def score(
        self,
        question: Question,
        candidate: Candidate,
        context: BusinessContext,
        rules: RuleRegistry,
        timeout: float | None = None,
    ) -> float:
        """Conform to SemanticCritic protocol returning bounded heuristic audit score in [0.0, 1.0]."""
        result = self.audit(question, candidate, context, rules, timeout=timeout)
        return float(result.get("score", 0.5))


@runtime_checkable
class ClassifierBackend(Protocol):
    """Optional System-1 / JEV-style choice scorer interface for ablation experiments."""

    def classify(
        self,
        question: Question,
        context: BusinessContext,
        labels: list[str],
    ) -> dict[str, float]:
        ...


RESIDUAL_MODIFIER_PATTERNS = [
    r"\bkhong\b",
    r"\btru\b",
    r"\bngoai\b",
    r"\bnhom\b",
    r"\bchi\s+nhanh\b",
    r"\bkenh\b",
    r"\bca\b",
    r"\bnhan\s+vien\b",
    r"\bkhach\b",
    r"\bloai\b",
    r"\bhang\b",
    r"\bdanh\s+muc\b",
    r"\bso\s+voi\b",
    r"\bcung\s+ky\b",
    r"\btang\b",
    r"\bgiam\b",
    r"\btrung\s+binh\b",
    r"\bty\s+trong\b",
]


FAST_PATH_METRIC_PATTERNS = {
    "sales_revenue": re.compile(r"\b(?:tong\s+)?(?:doanh\s+thu|doanh\s+so|tong\s+thu)\b"),
    "cancellation_rate": re.compile(
        r"\b(?:(?:ty|ti)\s+le|phan\s+tram)?\s*(?:don\s+(?:hang\s+)?)?(?:bi\s+|da\s+)?huy(?:\s+(?:don|don\s+hang))?\b"
    ),
    "top_products": re.compile(
        r"\b(?:top\s+\d+\s+)?(?:san\s+pham|mat\s+hang|mon)\s+(?:ban\s+chay|ban\s+nhieu)(?:\s+nhat)?\b"
        r"|\btop\s+(?:\d+\s+)?(?:san\s+pham|mat\s+hang|mon)(?:\s+(?:ban\s+chay|ban\s+nhieu)(?:\s+nhat)?)?\b"
        r"|\btop\s+(?:\d+\s+)?(?:ban\s+chay|ban\s+nhieu)(?:\s+nhat)?\b"
        r"|\b(?:ban\s+chay|ban\s+nhieu)\s+nhat\b"
    ),
    "gross_profit": re.compile(r"\b(?:tong\s+)?(?:loi\s+nhuan\s+gop|lai\s+gop|loi\s+nhuan)\b"),
    "debt_total": re.compile(r"\b(?:tong\s+)?(?:cong\s+no|tong\s+no|du\s+no|no\s+con\s+lai)\b"),
}

FAST_PATH_TIME_RE = re.compile(
    r"\b(?:trong\s+|vao\s+|dip\s+|ky\s+|tinh\s+den\s+|den\s+)?"
    r"(?:thang\s+\d{1,2}(?:\s*[/\-]\s*|\s+)\d{4}|thang\s+\d{1,2}\s+nam\s+\d{4}|nam\s+\d{4}|"
    r"quy\s+[1-4](?:\s*[/\-]\s*|\s+)\d{4}|quy\s+[1-4]\s+nam\s+\d{4}|"
    r"(?:ngay\s+)?\d{1,2}(?:\s*[/\-]\s*|\s+)\d{1,2}(?:\s*[/\-]\s*|\s+)\d{4}|"
    r"\d{4}(?:\s*-\s*|\s+)\d{2}(?:\s*-\s*|\s+)\d{2}|"
    r"(?:ngay\s+)?hom\s+qua|(?:ngay\s+)?hom\s+nay|tuan\s+truoc|tuan\s+nay|thang\s+truoc|thang\s+nay|"
    r"nam\s+ngoai|nam\s+truoc|nam\s+nay|tet\s+nguyen\s+dan\s+\d{4}|"
    r"dip\s+tet\s+\d{4}|tet\s+\d{4}|hien\s+tai|den\s+nay|tinh\s+den\s+nay)\b"
)

FAST_PATH_FRAMING_BUSINESS_RE = re.compile(
    r"\b(?:cua\s+)?(?:cua\s+hang|shop|doanh\s+nghiep|ho\s+kinh\s+doanh)\b"
)

FAST_PATH_FRAMING_RE = re.compile(
    r"\b(?:cho\s+biet|cho\s+hoi|hay\s+tinh|kiem\s+tra|tinh|xem|coi|check|giup|"
    r"la\s+bao\s+nhieu|co\s+bao\s+nhieu|bao\s+nhieu|bnhiu|bn|"
    r"la\s+gi|la\s+sp\s+nao|la\s+san\s+pham\s+nao|la\s+mat\s+hang\s+nao|"
    r"nha\s+minh|cua\s+hang|tiem|quan|shop|minh|em|anh|chi|cho|"
    r"la|co|trong|vao|dip|ky|cua|tong|cong|tat\s+ca|toan\s+bo|don\s+vi|vnd|dong|"
    r"a|nhi|vay|nhe|nha|ha|ta|voi|du\s+lieu|theo\s+so|so\s+lieu)\b"
)


def normalize_triage_text(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text).lower().replace("đ", "d"))
    folded = "".join(char for char in t if not unicodedata.combining(char))
    s = re.sub(r"[^a-z0-9%]+", " ", folded)
    s = " ".join(s.split())
    s = re.sub(r"\b(?:hqua|hom\s+qa|bua\s+qua)\b", "hom qua", s)
    s = re.sub(r"\bdthu\b", "doanh thu", s)
    s = re.sub(r"\bdso\b", "doanh so", s)
    s = re.sub(r"\btoongr\b", "tong", s)
    s = re.sub(r"\b(?:giumf|gium)\b", "giup", s)
    s = re.sub(r"\b(?:bnhiu|bnhieu|bao\s+nhiu|bn)\b", "bao nhieu", s)
    # If "bao nhieu %" occurs with "huy", it asks for cancellation rate ("ty le don bi huy")
    s = re.sub(r"\bbao\s+nhieu\s*%\s*(?:don\s+(?:hang\s+)?)?(?:bi\s+|da\s+)?huy", "ty le don bi huy", s)
    s = re.sub(r"((?:bao\s+nhieu|(?:ty|ti)\s+le|phan\s+tram)(?:\s+(?:la|cua))?)\s*%", r"\1", s)
    s = re.sub(r"%\s*(?=\bbao\s+nhieu\b)", "", s)
    s = re.sub(r"\bbao\s+nhieu\s*%\b", "bao nhieu", s)
    s = s.replace("%", " ")
    return " ".join(s.split())


def parse_fast_path_slots(question_text: str) -> dict[str, Any] | None:
    """Enforce a positive grammar with 0 unconsumed tokens for deterministic triage routing."""
    normalized = normalize_triage_text(question_text)
    metric = detect_template_metric(normalized)
    if metric is None:
        return None

    top_k = 1
    if metric == "top_products":
        match = re.search(r"\btop\s+(\d+)\b", normalized)
        if match:
            top_k = int(match.group(1))
            if top_k < 1 or top_k > 100:
                return None

    remaining = FAST_PATH_METRIC_PATTERNS[metric].sub(" ", normalized)
    remaining = FAST_PATH_TIME_RE.sub(" ", remaining)
    remaining = FAST_PATH_FRAMING_BUSINESS_RE.sub(" ", remaining)

    while True:
        stripped = FAST_PATH_FRAMING_RE.sub(" ", remaining)
        if stripped == remaining:
            break
        remaining = stripped

    unconsumed = [t for t in remaining.split() if t]
    if unconsumed:
        return None

    return {"metric": metric, "top_k": top_k}


def explain_route(question_text: str) -> dict[str, Any] | None:
    return parse_fast_path_slots(question_text)


def detect_template_metric(normalized: str) -> str | None:
    has_cancel_kw = bool(re.search(r"\bhuy\b", normalized))
    has_rate_kw = bool(
        re.search(r"\b(?:ty|ti)\s+le\b", normalized)
        or re.search(r"\bphan\s+tram\b", normalized)
        or "%" in normalized
    )
    is_cancellation = has_cancel_kw and has_rate_kw
    is_top_products = bool(
        re.search(
            r"\b(?:top\s+\d+\s+)?(?:san\s+pham|mat\s+hang|mon)\s+(?:ban\s+chay|ban\s+nhieu)(?:\s+nhat)?\b"
            r"|\btop\s+(?:\d+\s+)?(?:san\s+pham|mat\s+hang|mon)(?:\s+(?:ban\s+chay|ban\s+nhieu)(?:\s+nhat)?)?\b"
            r"|\btop\s+(?:\d+\s+)?(?:ban\s+chay|ban\s+nhieu)(?:\s+nhat)?\b"
            r"|\b(?:ban\s+chay|ban\s+nhieu)\s+nhat\b",
            normalized,
        )
    )
    is_gross_profit = bool(
        re.search(r"\bloi\s+nhuan\s+gop\b", normalized)
        or re.search(r"\blai\s+gop\b", normalized)
        or (
            re.search(r"\bloi\s+nhuan\b", normalized)
            and not re.search(r"\b(rong|thuan)\b", normalized)
        )
    )
    is_debt = bool(
        re.search(r"\bcong\s+no\b", normalized)
        or re.search(r"\btong\s+no\b", normalized)
        or re.search(r"\bdu\s+no\b", normalized)
        or re.search(r"\bno\s+con\s+lai\b", normalized)
    )
    is_revenue = bool(
        (
            re.search(r"\bdoanh\s+thu\b", normalized)
            or re.search(r"\bdoanh\s+so\b", normalized)
            or re.search(r"\btong\s+thu\b", normalized)
        )
        and not (
            is_gross_profit
            or is_debt
            or is_cancellation
            or is_top_products
            or re.search(r"\b(chi\s+phi|gia\s+von|lai)\b", normalized)
        )
    )

    matches = []
    if is_cancellation:
        matches.append("cancellation_rate")
    if is_top_products:
        matches.append("top_products")
    if is_gross_profit:
        matches.append("gross_profit")
    if is_debt:
        matches.append("debt_total")
    if is_revenue:
        matches.append("sales_revenue")

    if len(matches) == 1:
        return matches[0]
    return None


class DeterministicSlotGuard:
    """Pre-planner triage router with strict deterministic guards."""

    def route(
        self,
        question: Question,
        context: BusinessContext,
        rules: RuleRegistry,
        temporal: TemporalResolver,
    ) -> QueryPlan | None:
        slots = parse_fast_path_slots(question.question)
        if slots is None:
            return None

        metric = slots["metric"]
        top_k = slots.get("top_k", 1)

        # Condition 1: Map unambiguously to a standard whole-business metric
        if not rules.standard_policy_supported(metric):
            return None

        # Condition 2: Zero residual modifiers, filters, subgroups, comparisons
        normalized = normalize_triage_text(question.question)
        for pattern in RESIDUAL_MODIFIER_PATTERNS:
            if re.search(pattern, normalized):
                return None

        if metric != "cancellation_rate":
            if re.search(r"\b(phan\s+tram|ty\s+le|ti\s+le)\b", normalized):
                return None

        # Condition 3: Exactly 1 target time window
        try:
            windows = temporal.resolve(question.question, question.as_of_date)
        except Exception:
            return None

        if metric == "debt_total":
            if len(windows) == 0:
                # Asking current cumulative debt up to as_of cutoff
                window = TimeWindow(
                    role="target",
                    mode="cumulative",
                    start=date(2000, 1, 1),
                    end_exclusive=question.as_of_date + timedelta(days=1),
                )
                windows = [window]
            elif len(windows) == 1:
                window = windows[0]
            else:
                return None
        else:
            if len(windows) != 1 or windows[0].mode != "period":
                return None
            window = windows[0]

        # Condition 4: Context has exactly 1 eligible physical table, 0 parse errors, 0 nulls on roles, no multi-scope
        plan = self._build_plan(metric, window, context, rules, question, top_k=top_k)
        if plan is None:
            return None

        violations = validate_plan(plan, context, rules, question)
        if violations:
            return None

        return plan

    def _build_plan(
        self,
        metric: str,
        window: TimeWindow,
        context: BusinessContext,
        rules: RuleRegistry,
        question: Question,
        top_k: int = 1,
    ) -> QueryPlan | None:
        if metric == "debt_total":
            return self._build_debt_plan(window, context, rules, question)

        grain_by_metric = {
            "sales_revenue": "order_line",
            "cancellation_rate": "order_header",
            "top_products": "order_line",
            "gross_profit": "order_line",
        }
        required_roles_by_metric = {
            "sales_revenue": {"order_id", "ordered_at", "status", "line_amount"},
            "cancellation_rate": {"order_id", "ordered_at", "status"},
            "top_products": {"order_id", "ordered_at", "status", "qty_base", "product_name"},
            "gross_profit": {
                "order_id",
                "ordered_at",
                "status",
                "line_amount",
                "qty_sale",
                "cost_sale",
            },
        }

        target_grain = grain_by_metric[metric]
        required_roles = required_roles_by_metric[metric]

        candidates = [
            table
            for table in context.tables
            if table.grain == target_grain and all(table.role(role) for role in required_roles)
        ]
        if metric == "top_products" and not candidates:
            has_any_qty_base = any(col.semantic_role == "qty_base" for t in context.tables for col in t.columns)
            has_any_conversion = any(col.semantic_role == "conversion_factor" for t in context.tables for col in t.columns)
            if not has_any_qty_base and not has_any_conversion:
                required_roles = {"order_id", "ordered_at", "status", "qty_sale", "product_name"}
                candidates = [
                    table
                    for table in context.tables
                    if table.grain == target_grain and all(table.role(role) for role in required_roles)
                ]
        if metric == "cancellation_rate" and not candidates:
            # Fall back to order_line grain for cancellation_rate if order_header absent
            target_grain = "order_line"
            candidates = [
                table
                for table in context.tables
                if table.grain == target_grain and all(table.role(role) for role in required_roles)
            ]

        from bizreason.planning import partition_is_needed

        temp_plan = type("TempPlan", (), {"metric": metric, "windows": [window]})()
        if len(candidates) > 1:
            candidates = [
                t for t in candidates if partition_is_needed(t, temp_plan, context)
            ]

        if len(candidates) != 1:
            return None

        table = candidates[0]
        if not table.row_count:
            return None

        # Check multi-scope ambiguity
        scope = next((c for c in table.columns if c.semantic_role == "scope"), None)
        if scope and len(scope.examples) > 1:
            return None

        # Check parse errors and null counts on required roles
        for role in required_roles:
            col = table.column(table.role(role))
            if col is None or col.parse_errors > 0 or col.null_count > 0:
                return None

        # Check whether other partitions exist with same signature
        signature = (
            table.grain,
            tuple((c.original_name, c.semantic_role) for c in table.columns),
        )
        for other in context.tables:
            if other.name != table.name and (
                other.grain,
                tuple((c.original_name, c.semantic_role) for c in other.columns),
            ) == signature:
                if partition_is_needed(other, temp_plan, context):
                    return None

        policies = {
            "sales_revenue": "revenue_eligible_recorded_status",
            "cancellation_rate": "all_orders",
            "top_products": "revenue_eligible_recorded_status",
            "gross_profit": "revenue_eligible_recorded_status",
        }
        outputs = {
            "sales_revenue": OutputSpec(type=AnswerType.NUMBER, shape="scalar", rounding="money"),
            "cancellation_rate": OutputSpec(
                type=AnswerType.PERCENT, shape="scalar", rounding="two_decimals"
            ),
            "top_products": (
                OutputSpec(type=AnswerType.STRING, shape="scalar", rounding="none")
                if top_k == 1
                else OutputSpec(
                    type=AnswerType.LIST,
                    shape="list",
                    rounding="none",
                    element_type="string",
                )
            ),
            "gross_profit": OutputSpec(type=AnswerType.NUMBER, shape="scalar", rounding="money"),
        }

        ordered_col = table.role("ordered_at")
        filters = [
            PlanFilter(
                field=f"{table.name}.{ordered_col}",
                operator="gte",
                values=[window.start.isoformat()],
            ),
            PlanFilter(
                field=f"{table.name}.{ordered_col}",
                operator="lt",
                values=[window.end_exclusive.isoformat()],
            ),
        ]
        if metric in {"sales_revenue", "top_products", "gross_profit"}:
            status_col = table.role("status")
            filters.append(
                PlanFilter(
                    field=f"{table.name}.{status_col}",
                    operator="in",
                    values=["completed", "shipping"],
                )
            )

        group_by = []
        if metric == "top_products":
            group_by = [
                f"{table.name}.{table.role(role)}"
                for role in ("product_id", "product_name")
                if table.role(role)
            ]

        req_fields = [f"{table.name}.{table.role(role)}" for role in required_roles]
        if metric == "top_products" and table.role("product_id"):
            req_fields.append(f"{table.name}.{table.role('product_id')}")

        return QueryPlan(
            metric=metric,
            tables=[table.name],
            windows=[window],
            template_eligible=True,
            status_policy=policies[metric],
            rule_refs=rules.required_refs(metric),
            output=outputs[metric],
            filters=filters,
            group_by=group_by,
            required_fields=sorted(set(req_fields)),
            limit=top_k if metric == "top_products" else None,
        )

    def _build_debt_plan(
        self,
        window: TimeWindow,
        context: BusinessContext,
        rules: RuleRegistry,
        question: Question,
    ) -> QueryPlan | None:
        required_order_roles = {"order_id", "ordered_at", "status", "total_due", "payment_method"}
        required_payment_roles = {"order_id", "paid_at", "paid_amount"}

        order_candidates = [
            t
            for t in context.tables
            if t.grain in {"order_header", "order_line"}
            and all(t.role(r) for r in required_order_roles)
        ]
        payment_candidates = [
            t
            for t in context.tables
            if t.grain == "payment_event" and all(t.role(r) for r in required_payment_roles)
        ]

        if len(order_candidates) != 1 or len(payment_candidates) != 1:
            return None

        orders, payments = order_candidates[0], payment_candidates[0]
        if not orders.row_count or not payments.row_count:
            return None

        # Check parse errors and null counts
        for r in required_order_roles:
            c = orders.column(orders.role(r))
            if c is None or c.parse_errors > 0 or c.null_count > 0:
                return None
        for r in required_payment_roles:
            c = payments.column(payments.role(r))
            if c is None or c.parse_errors > 0 or c.null_count > 0:
                return None

        # Check for verified join between orders and payments
        join_match = next(
            (
                j
                for j in context.joins
                if (j.left == orders.name and j.right == payments.name)
                or (j.left == payments.name and j.right == orders.name)
            ),
            None,
        )
        if join_match is None:
            return None

        req_fields = [f"{orders.name}.{orders.role(r)}" for r in required_order_roles] + [
            f"{payments.name}.{payments.role(r)}" for r in required_payment_roles
        ]

        return QueryPlan(
            metric="debt_total",
            tables=[orders.name, payments.name],
            windows=[window],
            template_eligible=True,
            status_policy="debt_eligible_recorded_status",
            rule_refs=rules.required_refs("debt_total"),
            output=OutputSpec(type=AnswerType.NUMBER, shape="scalar", rounding="money"),
            joins=[join_match],
            required_fields=sorted(set(req_fields)),
        )


TriageRouter = DeterministicSlotGuard
