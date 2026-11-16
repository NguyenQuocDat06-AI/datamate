from __future__ import annotations

import inspect
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from bizreason.compilation import CompilationError, TemplateCompiler
from bizreason.config import Settings
from bizreason.contracts import (
    AnswerType,
    BusinessContext,
    Candidate,
    ExecutionResult,
    ExecutionStatus,
    Prediction,
    QueryPlan,
    Question,
    QuestionTrace,
)
from bizreason.decision import (
    DecisionGate,
    UnsupportedEvidence,
    assess_metric_coverage,
    verify_unsupported,
)
from bizreason.execution import execute_sql
from bizreason.llm import LanguageModel, ModelError, ProviderError
from bizreason.planning import Planner, auto_heal_plan, validate_plan
from bizreason.profit import bind_net_profit
from bizreason.rules import RuleRegistry
from bizreason.temporal import TemporalResolver
from bizreason.triage import SemanticCritic, TriageRouter
from bizreason.utils import digest, normalize_lookup
from bizreason.verification import verify_candidate


def _recover_list_execution(
    execution: ExecutionResult,
    plan: QueryPlan,
    question: Question | None,
) -> ExecutionResult:
    """Recover list predictions from multi-column execution results or concatenated values."""
    if execution.status not in {ExecutionStatus.SUCCESS, ExecutionStatus.EMPTY_SUCCESS}:
        return execution
    if not (plan.output.type == AnswerType.LIST or plan.output.shape == "list"):
        return execution
    rows = execution.rows
    if not rows:
        return execution

    # Case 1: 1 row with a comma-separated string
    if len(rows) == 1 and len(rows[0]) == 1 and isinstance(rows[0][0], str) and "," in rows[0][0]:
        parts = [s.strip() for s in rows[0][0].split(",") if s.strip()]
        if len(parts) > 1:
            return execution.model_copy(
                update={
                    "rows": [[p] for p in parts],
                    "columns": execution.columns[:1] if execution.columns else ["recovered_item"],
                    "row_count": len(parts),
                }
            )

    # Case 2: Multi-column rows (e.g. name + numeric metric or id + name)
    if any(len(row) >= 2 for row in rows):
        if len(rows) == 1 and len(rows[0]) >= 2 and all(
            c is None or (isinstance(c, (int, float, Decimal)) and not isinstance(c, bool))
            for c in rows[0]
        ):
            return execution
        q_raw = getattr(question, "question", "") if question else ""
        q_norm = normalize_lookup(q_raw) if q_raw else ""
        asks_code = bool(re.search(r"\b(?:ma|code|id|ma hang|ma sp|ma kh|ma don)\b", q_norm))
        asks_category_numbers = bool(
            re.search(
                r"\bmoi\s+(?:loai|kenh|nhom|hinh\s+thuc|thang|quy|nam|ngay)\s+(?:la\s+)?(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\b|\b(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\s+moi\s+(?:thang|quy|nam|ngay|loai|kenh|nhom)\b|\blan\s+luot\b|\bla\s+(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\s*.*\bla\s+(?:bao\s+nhieu|bao\s+nhiu|bn|bnhiu)\b",
                q_norm,
            )
        )

        asks_table_metrics = bool(
            re.search(
                r"\b(?:kem|kem\s+theo|moi\s+nguoi\s+(?:con\s+)?no|moi\s+khach\s+(?:con\s+)?no|cho\s+bang)\b",
                q_norm,
            )
        )
        if asks_table_metrics:
            return execution

        extracted_items: list[Any] = []
        for r in rows:
            if len(r) == 0:
                continue
            if len(r) == 1:
                extracted_items.append(r[0])
                continue

            col0, col1 = r[0], r[1]
            col1_is_num = isinstance(col1, (int, float, Decimal)) and not isinstance(col1, bool)
            col0_is_code = isinstance(col0, str) and bool(re.match(r"^[A-Za-z]{2,4}\d{3,6}$", col0.strip()))
            col1_is_str_name = isinstance(col1, str) and not col1.strip().isdigit()

            if col0_is_code and col1_is_str_name and not asks_code:
                extracted_items.append(col1)
            elif (
                (plan.output.element_type == "number" or (plan.output.element_type is None and asks_category_numbers))
                and col1_is_num
            ):
                extracted_items.append(col1)
            elif isinstance(col0, str) and (col1_is_num or len(r) >= 2):
                extracted_items.append(col0)
            elif col1_is_str_name and not asks_code:
                extracted_items.append(col1)
            else:
                extracted_items.append(col0)

        if any(len(r) > 0 and r[0] is None for r in rows):
            return execution

        clean_items = [it for it in extracted_items if it is not None]
        if plan.limit is not None and len(clean_items) < plan.limit:
            return execution

        m_count = re.search(
            r"\b([2-9]|\d{2,})\s*(?:khach|thang|mat\s*hang|san\s*pham|mon|loai|kenh|ngay|quy|nam)\b",
            q_norm,
        )
        if m_count and len(clean_items) != int(m_count.group(1)):
            return execution

        return execution.model_copy(
            update={
                "rows": [[it] for it in clean_items],
                "columns": execution.columns[:1] if execution.columns else ["recovered_item"],
                "row_count": len(clean_items),
            }
        )

    return execution


def _recover_scalar_execution(
    execution: ExecutionResult,
    plan: QueryPlan,
    question: Question | None,
) -> ExecutionResult:
    """Recover scalar predictions when a UNION ALL query returns multiple single-column numeric rows."""
    if execution.status not in {ExecutionStatus.SUCCESS, ExecutionStatus.EMPTY_SUCCESS}:
        return execution
    if plan.output.type not in {AnswerType.NUMBER, AnswerType.PERCENT} or plan.output.shape != "scalar":
        return execution
    rows = execution.rows
    if not rows or len(rows) <= 1:
        return execution
    if all(len(r) == 1 and isinstance(r[0], (int, float, Decimal)) and not isinstance(r[0], bool) for r in rows):
        total_sum = sum(float(r[0]) for r in rows)
        if all(isinstance(r[0], int) for r in rows):
            total_sum = int(total_sum)
        return execution.model_copy(
            update={
                "rows": [[total_sum]],
                "columns": execution.columns[:1] if execution.columns else ["recovered_scalar"],
                "row_count": 1,
            }
        )
    return execution


def _execute_zero_row_fallback(
    candidate: Candidate,
    plan: QueryPlan,
    context: BusinessContext,
    question: Question,
    settings: Settings,
    deadline: float,
) -> Candidate:
    """Deterministic, fast fallback re-execution for queries yielding 0 rows, NULL, or 0."""
    raw_sql = candidate.program.sql
    if not raw_sql or not getattr(context, "database", None):
        return candidate

    variations: list[str] = []

    # 1. Loosen cancellation / return status filter if present
    if re.search(r"'(?:huỷ|hủy|hủy đơn|huỷ đơn|đã huỷ|đã hủy)'", raw_sql, re.IGNORECASE):
        sql_st = re.sub(
            r"\b((?:[A-Za-z_][A-Za-z0-9_]*\.)?(?:trang_thai|tinh_trang|status))\s*=\s*'(?:huỷ|hủy|hủy đơn|huỷ đơn|đã huỷ|đã hủy)'",
            r"lookup(\1) LIKE '%huy%'",
            raw_sql,
            flags=re.IGNORECASE,
        )
        if sql_st != raw_sql:
            variations.append(sql_st)

    # 2. String equality loosening for non-ID, non-date literals
    def _repl_str_eq(m: re.Match) -> str:
        col = m.group(1)
        val = m.group(2)
        if any(k in col.lower() for k in ("id", "ma_", "code", "date", "ngay", "time")):
            return m.group(0)
        clean_val = val.strip("%' ")
        if len(clean_val) < 2:
            return m.group(0)
        return f"lookup({col}) LIKE '%' || lookup('{clean_val}') || '%'"

    sql_str_loose = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*'([^']+)'",
        _repl_str_eq,
        raw_sql,
        flags=re.IGNORECASE,
    )
    if sql_str_loose != raw_sql:
        variations.append(sql_str_loose)

    # 3. Drop overly restrictive status = 'completed' if initial query was empty
    sql_drop_st = re.sub(
        r"\s+AND\s+(?:[A-Za-z_][A-Za-z0-9_.]*\.)?(?:trang_thai|tinh_trang|status)\s*=\s*'(?:completed|hoàn thành|xong|Hoàn thành)'",
        "",
        raw_sql,
        flags=re.IGNORECASE,
    )
    if sql_drop_st != raw_sql:
        variations.append(sql_drop_st)

    for v_sql in variations:
        rem_time = max(0.5, min(settings.sql_timeout, deadline - time.monotonic()))
        v_exec = execute_sql(
            context.database,
            v_sql,
            candidate.program.parameters,
            plan.tables,
            timeout=rem_time,
            max_rows=settings.max_result_rows,
            use_pool=settings.use_sql_worker_pool,
        )
        v_exec = _recover_list_execution(v_exec, plan, question)
        v_exec = _recover_scalar_execution(v_exec, plan, question)
        if v_exec.status == ExecutionStatus.SUCCESS and v_exec.rows:
            first_val = v_exec.rows[0][0] if v_exec.rows[0] else None
            if first_val not in (None, 0, 0.0):
                v_prog = candidate.program.model_copy(update={"sql": v_sql})
                v_cand = candidate.model_copy(update={"program": v_prog, "execution": v_exec})
                v_cand = verify_candidate(v_cand, plan, context=context, question=question)
                if v_cand.verified:
                    return v_cand

    return candidate


class Solver:
    def __init__(
        self,
        model: LanguageModel | None,
        rules: RuleRegistry,
        temporal: TemporalResolver,
        settings: Settings,
        gate: DecisionGate | None = None,
        triage_router: TriageRouter | None = None,
        semantic_critic: SemanticCritic | None = None,
    ):
        self.model, self.rules, self.settings = model, rules, settings
        self.temporal = temporal
        self.planner = Planner(model, rules, temporal, settings) if model else None
        self.compiler = TemplateCompiler(rules)
        self.gate = gate or DecisionGate(settings.gate_mode)
        self.triage_router = triage_router
        self.semantic_critic = semantic_critic

    def solve(
        self, question: Question, context: BusinessContext
    ) -> tuple[Prediction, QuestionTrace]:
        started = time.monotonic()
        deadline = started + self.settings.question_timeout
        initial_calls = self.model.usage.calls if self.model else 0
        initial_tokens = self.model.usage.tokens if self.model else 0
        plans: list[QueryPlan] = []
        candidates: list[Candidate] = []
        errors: list[str] = []
        invalid_plans: list[dict[str, Any]] = []
        unsupported = UnsupportedEvidence(False, "Not established")
        feedback: dict[str, Any] | None = None
        probe_results: list[dict[str, Any]] = []
        probe_calls = 0
        repairs = 0
        if self.triage_router is not None and question.business_id == context.business_id:
            fast_plan = self.triage_router.route(question, context, self.rules, self.temporal)
            if fast_plan is not None:
                try:
                    fallback_w = (
                        self.temporal.resolve(question.question, question.as_of_date)
                        if not fast_plan.windows
                        else None
                    )
                    fast_plan = auto_heal_plan(
                        fast_plan, context, self.rules, question, fallback_windows=fallback_w
                    )
                    violations = validate_plan(
                        fast_plan,
                        context,
                        self.rules,
                        question,
                        fallback_windows=fallback_w,
                    )
                    if not violations:
                        program = self.compiler.compile(fast_plan, context)
                        execution = execute_sql(
                            context.database,
                            program.sql,
                            program.parameters,
                            fast_plan.tables,
                            timeout=min(self.settings.sql_timeout, max(0, deadline - time.monotonic())),
                            max_rows=self.settings.max_result_rows,
                            use_pool=self.settings.use_sql_worker_pool,
                        )
                        execution = _recover_list_execution(execution, fast_plan, question)
                        code_hash = digest(program)
                        candidate = Candidate(
                            id=digest([code_hash, 0, context.revision])[:20],
                            parent_id=None,
                            plan_hash=digest(fast_plan),
                            code_hash=code_hash,
                            source_revision=context.revision,
                            program=program,
                            execution=execution,
                            output=fast_plan.output,
                        )
                        candidate = verify_candidate(candidate, fast_plan, context=context, question=question)
                        if not candidate.verified and (fast_plan.output.type == AnswerType.LIST or fast_plan.output.shape == "list"):
                            alt_exec = _recover_list_execution(candidate.execution, fast_plan, question)
                            if alt_exec.rows != candidate.execution.rows:
                                alt_cand = candidate.model_copy(update={"execution": alt_exec})
                                alt_cand = verify_candidate(alt_cand, fast_plan, context=context, question=question)
                                if alt_cand.verified:
                                    candidate = alt_cand
                        if candidate.verified:
                            plans.append(fast_plan)
                            candidates.append(candidate)
                except Exception as exc:
                    errors.append(f"TRIAGE_ERROR: {str(exc)[:500]}")
        if not candidates:
            if self.planner and question.business_id == context.business_id:
                for attempt in range(self.settings.max_model_calls):
                    if (
                        time.monotonic() >= deadline
                        or self.model.usage.calls - initial_calls >= self.settings.max_model_calls
                    ):
                        errors.append("QUESTION_BUDGET_EXHAUSTED")
                        break
                    try:
                        plan = self.planner.make_plan(
                            question, context, deadline, feedback=feedback, probe_results=probe_results,
                            controller_budget={
                                "model_calls_remaining_including_this_call": self.settings.max_model_calls - (self.model.usage.calls - initial_calls),
                                "probe_calls_remaining": max(0, self.settings.max_probe_calls - probe_calls),
                                "must_finalize": self.model.usage.calls - initial_calls + 1 >= self.settings.max_model_calls,
                            },
                        )
                        binding = bind_net_profit(plan, question, context, self.rules)
                        if binding:
                            plan = binding.plan
                        fallback_w = (
                            self.planner.temporal.resolve(question.question, question.as_of_date)
                            if not plan.windows
                            else None
                        )
                        plan = auto_heal_plan(
                            plan, context, self.rules, question, fallback_windows=fallback_w
                        )
                        plans.append(plan)
                        violations = validate_plan(
                            plan,
                            context,
                            self.rules,
                            question,
                            fallback_windows=fallback_w,
                        )
                        can_observe_then_finalize = (
                            probe_calls < self.settings.max_probe_calls
                            and self.model.usage.calls - initial_calls < self.settings.max_model_calls
                        )
                        if plan.probe_requests and can_observe_then_finalize and not any(
                            item.startswith(("UNKNOWN_TABLE", "NO_DATA_SOURCE")) for item in violations
                        ):
                            for request in plan.probe_requests:
                                if probe_calls >= self.settings.max_probe_calls:
                                    break
                                probe = execute_sql(
                                    context.database,
                                    request.sql,
                                    request.parameters,
                                    [t.name for t in context.tables],
                                    timeout=min(
                                        self.settings.sql_timeout,
                                        max(
                                            min(5.0, self.settings.sql_timeout),
                                            deadline - time.monotonic(),
                                        ),
                                    ),
                                    max_rows=self.settings.max_result_rows,
                                    use_pool=self.settings.use_sql_worker_pool,
                                )
                                probe_calls += 1
                                probe_results.append(
                                    {
                                        "purpose": request.purpose,
                                        "status": probe.status.value,
                                        "columns": probe.columns,
                                        "preview": probe.rows[:20],
                                        "row_count": probe.row_count,
                                        "full_result_hash": probe.result_hash,
                                        "errors": probe.diagnostics,
                                    }
                                )
                            feedback = {
                                "latest_plan": plan.model_dump(mode="json"),
                                "instruction": "Use the completed probes; remove fulfilled probe requests.",
                            }
                            continue
                        if plan.probe_requests:
                            # No observation is useful if no model turn can consume it.
                            # A valid complete SQL may still execute; unresolved plans below
                            # remain rejected rather than being treated as proven answers.
                            errors.append("PROBE_BUDGET_RESERVED_FOR_FINALIZATION")
                        if violations:
                            healed_plan = auto_heal_plan(
                                plan, context, self.rules, question, fallback_windows=fallback_w
                            )
                            if healed_plan != plan:
                                new_violations = validate_plan(
                                    healed_plan,
                                    context,
                                    self.rules,
                                    question,
                                    fallback_windows=fallback_w,
                                )
                                if not new_violations:
                                    plan = healed_plan
                                    plans[-1] = plan
                                    violations = []
                        if violations:
                            feedback = {
                                "latest_plan": plan.model_dump(mode="json"),
                                "validation_errors": violations,
                            }
                            errors.extend(violations)
                            repairs += 1
                            if repairs > self.settings.max_repairs:
                                break
                            continue
                        if plan.metric == "unsupported":
                            unsupported = verify_unsupported(plan, context)
                            if not unsupported.verified:
                                feedback = {
                                    "latest_plan": plan.model_dump(mode="json"),
                                    "unsupported_rejected": unsupported.reason,
                                }
                                errors.append("UNSUPPORTED_NOT_ESTABLISHED")
                                repairs += 1
                                if repairs > self.settings.max_repairs:
                                    break
                                continue
                            break
                        coverage = assess_metric_coverage(plan, context, self.rules.text)
                        if coverage.verified:
                            unsupported = coverage
                            break
                        program = binding.program if binding else self.compiler.compile(plan, context)
                        execution = execute_sql(
                            context.database,
                            program.sql,
                            program.parameters,
                            plan.tables,
                            timeout=min(
                                self.settings.sql_timeout,
                                max(min(5.0, self.settings.sql_timeout), deadline - time.monotonic()),
                            ),
                            max_rows=self.settings.max_result_rows,
                            use_pool=self.settings.use_sql_worker_pool,
                        )
                        execution = _recover_list_execution(execution, plan, question)
                        execution = _recover_scalar_execution(execution, plan, question)
                        code_hash = digest(program)
                        candidate = Candidate(
                            id=digest([code_hash, attempt, context.revision])[:20],
                            parent_id=candidates[-1].id if candidates else None,
                            plan_hash=digest(plan),
                            code_hash=code_hash,
                            source_revision=context.revision,
                            program=program,
                            execution=execution,
                            output=plan.output,
                        )
                        candidate = verify_candidate(candidate, plan, context=context, question=question)
                        if not candidate.verified and (plan.output.type == AnswerType.LIST or plan.output.shape == "list"):
                            alt_exec = _recover_list_execution(candidate.execution, plan, question)
                            if alt_exec.rows != candidate.execution.rows:
                                alt_cand = candidate.model_copy(update={"execution": alt_exec})
                                alt_cand = verify_candidate(alt_cand, plan, context=context, question=question)
                                if alt_cand.verified:
                                    candidate = alt_cand
                        if not candidate.verified or candidate.value in (0, 0.0, None):
                            fb_cand = _execute_zero_row_fallback(
                                candidate, plan, context, question, self.settings, deadline
                            )
                            if fb_cand.verified and fb_cand.value not in (0, 0.0, None):
                                candidate = fb_cand
                        candidates.append(candidate)
                        if candidate.verified:
                            break
                        feedback = {
                            "latest_plan": plan.model_dump(mode="json"),
                            "latest_candidate": candidate.model_dump(mode="json"),
                        }
                        repairs += 1
                        if repairs > self.settings.max_repairs:
                            break
                    except ProviderError as error:
                        if (
                            time.monotonic() + 15.0 < deadline
                            and (self.model.usage.calls - initial_calls) < self.settings.max_model_calls
                        ):
                            errors.append(str(error))
                            continue
                        else:
                            errors.append(str(error))
                            break
                    except (ModelError, CompilationError, ValueError) as error:
                        if self.planner.last_output is not None:
                            invalid_plans.append(self.planner.last_output)
                        errors.append(f"{type(error).__name__}: {str(error)[:2000]}")
                        feedback = {
                            "latest_plan": plans[-1].model_dump(mode="json") if plans else None,
                            "error": errors[-1],
                            "latest_invalid_output": self.planner.last_output,
                        }
                        repairs += 1
                        if repairs > self.settings.max_repairs:
                            break
            else:
                errors.append("NO_MODEL_BACKEND")
        verified = [candidate for candidate in candidates if candidate.verified]
        # Keep distinct plan/code candidates even if execution signatures match.
        selected = next(
            (candidate for candidate in verified if candidate.program.backend == "template"),
            verified[0] if verified else None,
        )
        critic_score: float | None = None
        if self.semantic_critic is not None and selected is not None:
            calls_used = (self.model.usage.calls - initial_calls) if self.model else 0
            remaining_seconds = deadline - time.monotonic()
            if self.model is not None and calls_used >= self.settings.max_model_calls:
                errors.append("CRITIC_SKIPPED_BUDGET_EXHAUSTED")
            elif remaining_seconds <= 0:
                errors.append("CRITIC_SKIPPED_DEADLINE_EXHAUSTED")
            else:
                try:
                    sig = inspect.signature(self.semantic_critic.score)
                    if "timeout" in sig.parameters:
                        critic_score = self.semantic_critic.score(
                            question, selected, context, self.rules, timeout=remaining_seconds
                        )
                    else:
                        critic_score = self.semantic_critic.score(
                            question, selected, context, self.rules
                        )
                except TypeError:
                    critic_score = self.semantic_critic.score(
                        question, selected, context, self.rules
                    )
        decision = self.gate.choose(
            selected, unsupported, repairs, semantic_score=critic_score
        )
        if decision.action == "value" and selected:
            prediction = Prediction(
                id=question.id,
                type=selected.output.type,
                answer=selected.value,
                rounding=selected.output.rounding,
            )
        elif decision.action == "unanswerable":
            prediction = Prediction(
                id=question.id, type=AnswerType.UNANSWERABLE, reason=decision.reason
            )
        else:
            prediction = Prediction(id=question.id, type=AnswerType.ABSTAIN, reason=decision.reason)
        trace = QuestionTrace(
            question=question,
            source_revision=context.revision,
            plans=plans,
            candidates=candidates,
            errors=errors,
            decision=decision,
            probes=probe_results,
            repair_count=repairs,
            unsupported_verified=unsupported.verified,
            invalid_plans=invalid_plans,
            elapsed_ms=(time.monotonic() - started) * 1000,
            model_calls=(self.model.usage.calls - initial_calls) if self.model else 0,
            model_tokens=(self.model.usage.tokens - initial_tokens) if self.model else 0,
            finished_at=datetime.now(timezone.utc),
        )
        return prediction, trace
