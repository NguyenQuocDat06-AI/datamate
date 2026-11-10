from __future__ import annotations

import inspect
import time
from datetime import datetime, timezone
from typing import Any

from bizreason.compilation import CompilationError, TemplateCompiler
from bizreason.config import Settings
from bizreason.contracts import (
    AnswerType,
    BusinessContext,
    Candidate,
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
from bizreason.planning import Planner, validate_plan
from bizreason.profit import bind_net_profit
from bizreason.rules import RuleRegistry
from bizreason.temporal import TemporalResolver
from bizreason.triage import SemanticCritic, TriageRouter
from bizreason.utils import digest
from bizreason.verification import verify_candidate


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
                    violations = validate_plan(
                        fast_plan,
                        context,
                        self.rules,
                        question,
                        fallback_windows=self.temporal.resolve(question.question, question.as_of_date)
                        if not fast_plan.windows
                        else None,
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
                        plans.append(plan)
                        violations = validate_plan(
                            plan,
                            context,
                            self.rules,
                            question,
                            fallback_windows=self.planner.temporal.resolve(question.question, question.as_of_date)
                            if not plan.windows
                            else None,
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
