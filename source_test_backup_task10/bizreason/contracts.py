"""Versioned objects shared between pipeline stages; no reference-answer fields."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AnswerType(StrEnum):
    NUMBER = "number"
    PERCENT = "percent"
    STRING = "string"
    DATE = "date"
    BOOLEAN = "boolean"
    LIST = "list"
    TABLE = "table"
    UNANSWERABLE = "unanswerable"
    ABSTAIN = "abstain"


class Question(Contract):
    id: str
    business_id: str
    question: str
    as_of_date: date


class SourceRef(Contract):
    path: str
    table: str = ""
    row: int | None = None
    sha256: str = ""


class Diagnostic(Contract):
    code: str
    message: str
    source: SourceRef | None = None
    severity: Literal["info", "warning", "error"] = "warning"


class ColumnMeta(Contract):
    name: str
    original_name: str
    dtype: str
    semantic_role: str | None = None
    unit: str | None = None
    examples: list[Any] = Field(default_factory=list)
    null_count: int = 0
    parse_errors: int = 0
    evidence: list[str] = Field(default_factory=list)


class TableMeta(Contract):
    name: str
    sources: list[SourceRef]
    columns: list[ColumnMeta]
    grain: str = "unknown"
    row_count: int
    raw_row_count: int
    excluded_rows: int = 0
    duplicate_rows: int = 0
    header_row: int = 1
    coverage: dict[str, tuple[str, str]] = Field(default_factory=dict)

    def column(self, name: str) -> ColumnMeta | None:
        return next((column for column in self.columns if column.name == name), None)

    def role(self, role: str) -> str | None:
        matches = [column.name for column in self.columns if column.semantic_role == role]
        return matches[0] if len(matches) == 1 else None


class JoinEdge(Contract):
    left: str
    right: str
    keys: list[tuple[str, str]]
    cardinality: Literal["many_to_one", "one_to_one"]
    orphan_count: int
    evidence: list[str]


class BusinessContext(Contract):
    version: str = "1"
    business_id: str
    revision: str
    database: Path
    readme: str
    tables: list[TableMeta]
    joins: list[JoinEdge] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    declared_end: date | None = None
    declared_start: date | None = None
    manifest: dict[str, str]
    schema_complete: bool = False

    def table(self, name: str) -> TableMeta | None:
        return next((table for table in self.tables if table.name == name), None)


class TimeWindow(Contract):
    role: str = "target"
    mode: Literal["period", "cumulative"] = "period"
    start: date
    end_exclusive: date
    field: str | None = None
    evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def ordered(self) -> TimeWindow:
        if self.start >= self.end_exclusive:
            raise ValueError("Time window must be nonempty and half-open")
        return self


class OutputSpec(Contract):
    type: AnswerType
    shape: Literal["scalar", "list", "table", "ratio"] = "scalar"
    rounding: Literal["none", "money", "two_decimals", "integer"] = "none"
    columns: list[str] = Field(default_factory=list)
    element_type: Literal["string", "number"] | None = None
    column_types: list[Literal["string", "number"]] = Field(default_factory=list)


class UnsupportedClaim(Contract):
    kind: Literal["missing_field", "outside_coverage", "missing_history"]
    required: list[str]
    explanation: str
    source_refs: list[str]


class ProbeRequest(Contract):
    purpose: str
    sql: str
    parameters: list[str | int | float | None] = Field(default_factory=list)


class PlanFilter(Contract):
    field: str
    operator: Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "contains"]
    values: list[str | int | float | None]


class QueryPlan(Contract):
    version: str = "1"
    metric: Literal[
        "sales_revenue",
        "cancellation_rate",
        "top_products",
        "debt_total",
        "gross_profit",
        "generic_sql",
        "unsupported",
    ]
    tables: list[str]
    windows: list[TimeWindow] = Field(default_factory=list)
    rule_refs: list[str]
    required_fields: list[str] = Field(default_factory=list)
    joins: list[JoinEdge] = Field(default_factory=list)
    entity_constraints: list[dict[str, str]] = Field(default_factory=list)
    filters: list[PlanFilter] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    template_eligible: bool = False
    status_policy: str = ""
    units: dict[str, str] = Field(default_factory=dict)
    output: OutputSpec
    limit: int | None = Field(default=None, ge=1, le=10000)
    sql: str | None = None
    parameters: list[str | int | float | None] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    unsupported: UnsupportedClaim | None = None
    probe_requests: list[ProbeRequest] = Field(default_factory=list, max_length=2)
    derivation_notes: list[str] = Field(default_factory=list)


class ExecutionStatus(StrEnum):
    SUCCESS = "success"
    EMPTY_SUCCESS = "empty_success"
    TIMEOUT = "timeout"
    EXECUTION_ERROR = "execution_error"
    POLICY_DENIED = "policy_denied"
    RESULT_LIMIT = "result_limit"


class ExecutionResult(Contract):
    status: ExecutionStatus
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    result_hash: str = ""
    elapsed_ms: float = 0
    error_code: str | None = None
    diagnostics: list[str] = Field(default_factory=list)
    accessed_tables: list[str] = Field(default_factory=list)


class Program(Contract):
    sql: str
    parameters: list[str | int | float | None]
    backend: Literal["template", "generated_sql"]
    policy_refs: list[str]


class Candidate(Contract):
    id: str
    parent_id: str | None = None
    plan_hash: str
    code_hash: str
    source_revision: str
    program: Program
    execution: ExecutionResult
    output: OutputSpec
    value: Any = None
    verified: bool = False
    checks: list[str] = Field(default_factory=list)
    signature: str = ""


class Decision(Contract):
    action: Literal["value", "unanswerable", "abstain"]
    reason: str
    p_value: float | None = None
    p_unanswerable: float | None = None
    policy_version: str


class Prediction(Contract):
    id: str
    type: AnswerType
    answer: Any = None
    reason: str | None = None
    rounding: Literal["none", "money", "two_decimals", "integer"] = "none"


class QuestionTrace(Contract):
    question: Question
    source_revision: str
    plans: list[QueryPlan] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    probes: list[dict[str, Any]] = Field(default_factory=list)
    repair_count: int = 0
    unsupported_verified: bool = False
    invalid_plans: list[dict[str, Any]] = Field(default_factory=list)
    decision: Decision
    elapsed_ms: float
    model_calls: int
    model_tokens: int
    finished_at: datetime
