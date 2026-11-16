"""Evidence-backed executable metrics and question contracts.

Evidence validity proves where a definition came from, not that its model-generated
interpretation is correct. Reviewed/oracle definitions must evaluate that separately.
Question contracts select registered expressions; they never replace their formula.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Mapping, Sequence

from bizreason.contracts import AnswerType
from bizreason.semantic import knowledge
from bizreason.semantic.evidence import EvidenceDocument, extract_definitions
from bizreason.semantic.definition_checks import DefinitionPopulationError, validate_population
from bizreason.semantic.plan import (
    AGGS, ARITH, CMP, OPS, OUTPUTS, PERIODS, SCOPES, PlanCompiler, PlanError, question_check,
)
from bizreason.utils import normalize_lookup

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")
_METRIC_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
UNITS = ("number", "count", "money", "ratio", "percent", "date")


class CatalogError(ValueError):
    """A definition, contract or selection that cannot be trusted."""


def _object(raw: Any, allowed: set[str], required: set[str], context: str) -> dict:
    if not isinstance(raw, dict):
        raise CatalogError(f"{context} must be an object")
    extra, missing = set(raw) - allowed, required - set(raw)
    if extra or missing:
        raise CatalogError(f"{context} fields: unknown={sorted(extra)}, missing={sorted(missing)}")
    return raw


def _text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{context} must be nonempty text")
    return value.strip()


def _strings(value: Any, context: str, *, empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not empty):
        raise CatalogError(f"{context} must be a list")
    out = tuple(_text(v, context) for v in value)
    if len(set(out)) != len(out):
        raise CatalogError(f"duplicate {context}")
    return out


def _column(value: Any, source: str) -> str:
    value = _text(value, "column")
    parts = value.split(".")
    if len(parts) > 2 or not all(_IDENTIFIER.fullmatch(p) for p in parts):
        raise CatalogError("invalid column reference")
    return value if len(parts) == 2 else f"{source}.{value}"


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _expression(expr: Any, source: str, refs: set[str], depth: int = 0) -> None:
    if depth > 8:
        raise CatalogError("expression too deep")
    if isinstance(expr, str):
        refs.add(_column(expr, source))
    elif _finite(expr):
        return
    elif isinstance(expr, dict) and "days" in expr:
        _object(expr, {"days"}, {"days"}, "date expression")
        if not isinstance(expr["days"], list) or len(expr["days"]) != 2:
            raise CatalogError("days needs two references")
        for ref in expr["days"]:
            if ref not in ("today", "period_start", "period_end"):
                refs.add(_column(ref, source))
    elif isinstance(expr, dict):
        _object(expr, {"op", "left", "right"}, {"op", "left", "right"}, "expression")
        if expr["op"] not in ARITH:
            raise CatalogError("invalid expression operator")
        for side in ("left", "right"):
            _expression(expr[side], source, refs, depth + 1)
    else:
        raise CatalogError("invalid expression")


def _filters(filters: Any, source: str, refs: set[str]) -> None:
    if not isinstance(filters, list):
        raise CatalogError("filters must be a list")
    for item in filters:
        _object(item, {"column", "expr", "op", "value", "column2"}, {"op"}, "filter")
        if item["op"] not in OPS or ("column" in item) == ("expr" in item):
            raise CatalogError("invalid filter")
        unary = item["op"] in ("is_empty", "not_empty") or item["op"].endswith(
            ("_today", "_period", "_period_end"))
        if unary and ("value" in item or "column2" in item):
            raise CatalogError("unused filter operand")
        if not unary and (("value" in item) == ("column2" in item)):
            raise CatalogError("filter needs exactly one operand")
        if "expr" in item and (item["op"] not in CMP or not _finite(item.get("value"))):
            raise CatalogError("expression filter needs numeric comparison")
        if "column" in item:
            refs.add(_column(item["column"], source))
        else:
            _expression(item["expr"], source, refs)
        if "column2" in item:
            refs.add(_column(item["column2"], source))


def _measure(raw: Any, source: str, refs: set[str], depth: int = 0) -> None:
    if depth > 3:
        raise CatalogError("measure too deep")
    if _finite(raw):
        return
    if not isinstance(raw, dict):
        raise CatalogError("invalid measure")
    if "agg" in raw:
        _object(raw, {"agg", "expr", "filters", "scope", "period"}, {"agg"}, "aggregate")
        if raw["agg"] not in AGGS:
            raise CatalogError("invalid aggregate")
        if raw.get("expr") is not None:
            _expression(raw["expr"], source, refs)
        if "filters" in raw:
            _filters(raw["filters"], source, refs)
        if "scope" in raw and raw["scope"] not in SCOPES:
            raise CatalogError("invalid aggregate scope")
        if "period" in raw and raw["period"] not in PERIODS:
            raise CatalogError("invalid aggregate period")
    else:
        _object(raw, {"op", "left", "right"}, {"op", "left", "right"}, "measure")
        if raw["op"] not in ARITH:
            raise CatalogError("invalid measure operator")
        for side in ("left", "right"):
            _measure(raw[side], source, refs, depth + 1)


def _plan_refs(plan: dict) -> set[str]:
    allowed = {"source", "scope", "period", "filters", "measure", "per", "having", "as_percent"}
    _object(plan, allowed, {"source", "measure"}, "metric plan")
    source = _text(plan["source"], "source")
    refs: set[str] = set()
    _measure(plan["measure"], source, refs)
    _filters(plan.get("filters", []), source, refs)
    per = plan.get("per")
    if per is not None:
        _object(per, {"by", "agg", "having"}, {"by", "agg"}, "per")
        refs.add(_column(per["by"], source))
        if per["agg"] not in ("count", "avg", "sum", "min", "max", "share"):
            raise CatalogError("invalid per aggregation")
        having = per.get("having")
        if having is not None:
            _object(having, {"agg", "expr", "filters", "scope", "period", "op", "value"},
                    {"op", "value"}, "having")
            if having["op"] not in CMP or not _finite(having["value"]):
                raise CatalogError("invalid having")
            if "agg" in having:
                _measure({k: v for k, v in having.items() if k not in ("op", "value")}, source, refs)
            elif set(having) - {"op", "value"}:
                raise CatalogError("having has unused aggregate fields")
    if plan.get("having") is not None:
        raise CatalogError("having belongs inside per in a metric template")
    if "as_percent" in plan and not isinstance(plan["as_percent"], bool):
        raise CatalogError("as_percent must be boolean")
    return refs


def with_derived_dependencies(reply: Any) -> Any:
    """Fill an omitted dependency list from a closed AST, without changing its meaning.

    The runtime's compact model protocol omits this redundant metadata. Explicit
    lists from older clients are still validated strictly, never silently replaced.
    This derives consumed references, not business dependencies entailed by prose.
    """
    if not isinstance(reply, dict) or not isinstance(reply.get('metrics'), list):
        return reply
    metrics = []
    for raw in reply['metrics']:
        if isinstance(raw, dict) and 'dependencies' not in raw:
            try:
                raw = {**raw, 'dependencies': sorted(_plan_refs(raw.get('plan')))}
            except (CatalogError, TypeError, ValueError, KeyError):
                pass  # the normal validator reports the malformed proposal
        metrics.append(raw)
    return {**reply, 'metrics': metrics}


@dataclass(frozen=True)
class MetricSpec:
    metric_id: str
    terms: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    source: str
    grain: str
    entity: str
    date_axis: str | None
    population: str
    unit: str
    dependencies: tuple[str, ...]
    plan_json: str

    @property
    def plan(self) -> dict:
        return json.loads(self.plan_json)

    @classmethod
    def from_json(cls, raw: Any) -> MetricSpec:
        keys = {"metric_id", "terms", "evidence_ids", "source", "grain", "entity", "date_axis",
                "population", "unit", "dependencies", "plan"}
        _object(raw, keys, keys, "metric")
        metric_id = _text(raw["metric_id"], "metric_id")
        if not _METRIC_ID.fullmatch(metric_id):
            raise CatalogError("invalid metric_id")
        source = _text(raw["source"], "source")
        if not _IDENTIFIER.fullmatch(source):
            raise CatalogError("invalid source")
        plan = raw["plan"]
        consumed = _plan_refs(plan)
        dependencies = tuple(sorted(_column(v, source) for v in _strings(
            raw["dependencies"], "dependencies", empty=True)))
        if len(set(dependencies)) != len(dependencies) or set(dependencies) != consumed:
            raise CatalogError("declared dependencies differ from consumed references")
        if raw["population"] not in SCOPES or raw["unit"] not in UNITS:
            raise CatalogError("invalid population or unit")
        if plan["source"] != source or plan.get("scope", raw["population"]) != raw["population"]:
            raise CatalogError("metric source/population differs from plan")
        if plan.get("period", "question") not in PERIODS:
            raise CatalogError("invalid metric period")
        if bool(plan.get("as_percent", False)) != (raw["unit"] == "percent"):
            raise CatalogError("percent unit requires explicit as_percent")
        axis = raw["date_axis"]
        if axis is not None:
            axis = _column(axis, source)
        return cls(metric_id, _strings(raw["terms"], "terms"),
                   _strings(raw["evidence_ids"], "evidence_ids"), source,
                   _text(raw["grain"], "grain"), _text(raw["entity"], "entity"), axis,
                   raw["population"], raw["unit"], dependencies,
                   json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False))


@dataclass(frozen=True)
class QuestionContract:
    metric_id: str
    grain: str
    entity: str
    date_axis: str | None
    population: str
    period: str
    output: str
    filters_json: str = "[]"
    group_by: str | None = None
    limit: int | None = None
    order: str = "desc"

    @classmethod
    def from_json(cls, raw: Any) -> QuestionContract:
        required = {"metric_id", "grain", "entity", "date_axis", "population", "period", "output"}
        _object(raw, required | {"filters", "group_by", "limit", "order"}, required, "contract")
        if raw["population"] not in SCOPES or raw["period"] not in PERIODS or raw["output"] not in OUTPUTS:
            raise CatalogError("invalid contract population, period or output")
        # These outputs require additional semantic definitions, not question-side formula overrides.
        if raw["output"] in ("share", "yesno"):
            raise CatalogError("share/yesno contract output is not yet supported")
        limit = raw.get("limit")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 50):
            raise CatalogError("invalid contract limit")
        order = raw.get("order", "desc")
        if order is None and raw["output"] == "scalar" and raw.get("group_by") is None:
            order = "desc"  # scalar results have no ordering; normalize a transport-only null
        if order not in ("asc", "desc"):
            raise CatalogError("invalid contract order")
        group_by = raw.get("group_by")
        if group_by is not None:
            group_by = _text(group_by, "group_by")
        filters = raw.get("filters", [])
        if not isinstance(filters, list):
            raise CatalogError("contract filters must be list")
        return cls(*(_text(raw[k], k) for k in ("metric_id", "grain", "entity")),
                   None if raw["date_axis"] is None else _text(raw["date_axis"], "date_axis"),
                   raw["population"], raw["period"], raw["output"],
                   json.dumps(filters, allow_nan=False), group_by, limit, order)


@dataclass(frozen=True)
class CatalogIssue:
    metric_id: str | None
    code: str
    message: str


@dataclass(frozen=True)
class MetricResult:
    type: AnswerType
    answer: Any
    plan: dict
    metric: MetricSpec
    evidence: tuple[Any, ...]


class _UnitCompiler(PlanCompiler):
    def __init__(self, *args, unit: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.unit = unit

    def _final(self, value, kind, as_percent, plan, output):
        if self.unit == "count" and value is not None and float(value) != int(float(value)):
            raise PlanError("fractional count")
        kind = {"percent": "ratio"}.get(self.unit, self.unit)
        return super()._final(value, kind, as_percent, plan, output)

    def _groups(self, rows, plan, output, kind, as_percent, gcol):
        if self.unit == "count" and any(value is not None and float(value) != int(float(value))
                                        for _, value in rows):
            raise PlanError("fractional count")
        kind = {"percent": "ratio"}.get(self.unit, self.unit)
        return super()._groups(rows, plan, output, kind, as_percent, gcol)


class Catalog:
    def __init__(self, metrics: Mapping[str, MetricSpec], evidence: Mapping[str, Any],
                 errors: Sequence[CatalogIssue] = ()):
        self.metrics = dict(metrics)
        self.evidence = dict(evidence)
        self.errors = tuple(errors)

    @classmethod
    def from_reply(cls, reply: Any, documents: Mapping[str, str], connection: sqlite3.Connection,
                   evidence_docs: Sequence[EvidenceDocument] | None = None) -> Catalog:
        _object(reply, {"metrics"}, {"metrics"}, "catalog reply")
        if not isinstance(reply["metrics"], list) or len(reply["metrics"]) > 100:
            raise CatalogError("metrics must be a bounded list")
        docs = list(evidence_docs) if evidence_docs is not None else [
            extract_definitions(text, source_id=key) for key, text in documents.items()]
        originals = {key: {d.definition_id: d for d in extract_definitions(text, source_id=key).definitions}
                     for key, text in documents.items()}
        evidence, conflicts = {}, set()
        # Compute conflicts from the source documents, not caller-supplied annotations.
        bodies: dict[str, set[str]] = {}
        for definitions in originals.values():
            for definition in definitions.values():
                bodies.setdefault(normalize_lookup(definition.term), set()).add(definition.definition_id)
        conflicts.update(term for term, ids in bodies.items() if len(ids) > 1)
        for doc in docs:
            text = documents.get(doc.source_id)
            if text is None or hashlib.sha256(text.encode()).hexdigest() != doc.document_sha256:
                raise CatalogError("evidence document hash mismatch")
            conflicts.update(doc.conflicts)
            for definition in doc.definitions:
                original = originals[doc.source_id].get(definition.definition_id)
                if original is None or (definition.term, definition.body) != (original.term, original.body):
                    raise CatalogError("definition does not match its source document")
                if not definition.evidence:
                    raise CatalogError("definition has no evidence span")
                for span in definition.evidence:
                    if (span not in original.evidence or span.source_id not in documents
                            or not span.validate(documents[span.source_id])):
                        raise CatalogError("forged evidence span")
                if definition.definition_id in evidence:
                    old = evidence[definition.definition_id]
                    evidence[definition.definition_id] = replace(
                        old, evidence=tuple(dict.fromkeys((*old.evidence, *definition.evidence))))
                    continue  # each source definition was independently re-extracted and verified
                evidence[definition.definition_id] = definition
        metrics, errors, blocked = {}, [], set()
        card = knowledge.build_card(connection)
        for raw in reply["metrics"]:
            ident = raw.get("metric_id") if isinstance(raw, dict) else None
            try:
                spec = MetricSpec.from_json(raw)
                if spec.metric_id in metrics or spec.metric_id in blocked:
                    blocked.add(spec.metric_id)
                    metrics.pop(spec.metric_id, None)
                    raise CatalogError("duplicate metric_id")
                selected = [evidence[i] for i in spec.evidence_ids if i in evidence]
                if len(selected) != len(spec.evidence_ids):
                    raise CatalogError("unknown definition evidence")
                terms = {normalize_lookup(d.term) for d in selected}
                if terms & conflicts or any(normalize_lookup(t) not in terms for t in spec.terms):
                    raise CatalogError("conflicting or ungrounded metric term")
                compiler = PlanCompiler(connection, date.today(), None, card=card)
                compiler._setup(spec.plan)
                compiler._status(spec.population)
                compiler._measure(spec.plan["measure"])
                compiler._filters(spec.plan.get("filters", []))
                validate_population(spec, selected, compiler)
                for ref in spec.dependencies:
                    compiler._ref(ref, "filter" if ref not in _measure_refs(spec) else "value")
                if spec.date_axis:
                    column = compiler._ref(spec.date_axis, "filter")
                    if not compiler._is_date(column):
                        raise CatalogError("date_axis is not a date column")
                metrics[spec.metric_id] = spec
            except DefinitionPopulationError as exc:
                errors.append(CatalogIssue(ident, "definition_population", str(exc)))
            except (CatalogError, PlanError, TypeError, ValueError, KeyError) as exc:
                errors.append(CatalogIssue(ident, "invalid_metric", str(exc)))
        return cls(metrics, evidence, errors)

    def select(self, question: str) -> MetricSpec | None:
        words = set(normalize_lookup(question).split())
        hits = [m for m in self.metrics.values()
                if any(set(normalize_lookup(term).split()) <= words for term in m.terms)]
        if len(hits) > 1:
            raise CatalogError("ambiguous metric selection")
        return hits[0] if hits else None

    def prompt(self) -> str:
        return "\n".join(json.dumps({k: getattr(m, k) for k in (
            "metric_id", "terms", "source", "grain", "entity", "date_axis", "population", "unit",
            "evidence_ids")}, ensure_ascii=False) for m in self.metrics.values())

    def execute(self, contract: QuestionContract | dict, connection: sqlite3.Connection, as_of: date,
                period: tuple[date, date] | None = None, comparison: tuple[date, date] | None = None,
                question: str = "", named: Sequence[str] = ()) -> MetricResult:
        if isinstance(contract, dict):
            contract = QuestionContract.from_json(contract)
        spec = self.metrics.get(contract.metric_id)
        if spec is None:
            raise CatalogError("unknown metric_id")
        for key in ("grain", "entity", "population"):
            if getattr(contract, key) != getattr(spec, key):
                raise CatalogError(f"contract {key} does not match metric")
        axis = _column(contract.date_axis, spec.source) if contract.date_axis is not None else None
        if axis != spec.date_axis:
            raise CatalogError("contract date_axis does not match metric")
        if question:
            selected = self.select(question)
            if selected is None or selected.metric_id != spec.metric_id:
                raise CatalogError("contract metric does not match named definition")
        filters = json.loads(contract.filters_json)
        _filters(filters, spec.source, set())
        if question:
            from bizreason.semantic.intent import question_numbers
            stated = question_numbers(question)
            for item in filters:
                values = item.get("value")
                for value in values if isinstance(values, list) else [values]:
                    if _finite(value) and not any(math.isclose(value, number) for number in stated):
                        raise CatalogError("contract numeric bound is not stated in the question")
        plan = spec.plan
        plan.update(scope=spec.population, output=contract.output, group_by=contract.group_by,
                    limit=contract.limit, order=contract.order)
        plan["filters"] = [*plan.get("filters", []), *filters]
        if axis is None:
            if contract.period != "none":
                raise CatalogError("metric has no date axis")
            plan["period"] = "none"
        elif contract.period in ("question", "until_today"):
            plan["period"] = "none"
            plan["filters"].append({"column": axis, "op": "in_period" if contract.period == "question"
                                    else "on_or_before_today"})
        elif contract.period == "none":
            plan["period"] = "none"
        else:
            raise CatalogError("comparison-only contract is not yet supported")
        problem = question_check(plan, period, comparison, list(named), as_of)
        if problem:
            raise CatalogError(problem)
        kind, value = _UnitCompiler(connection, as_of, period, comparison=comparison,
                                    unit=spec.unit).run(plan)
        return MetricResult(kind, value, plan, spec, tuple(self.evidence[i] for i in spec.evidence_ids))


def _measure_refs(spec: MetricSpec) -> set[str]:
    refs: set[str] = set()
    _measure(spec.plan["measure"], spec.source, refs)
    return refs
