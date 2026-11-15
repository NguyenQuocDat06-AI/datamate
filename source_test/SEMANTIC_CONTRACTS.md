# Evidence-backed semantic contracts: experimental implementation

This implements the first gated stages of `reports/PA_09_SEMANTIC_RESEARCH_20261114.md`.
It does not establish PA0.9. The normal pipeline and plan fallback defaults remain unchanged.

## Components

- `bizreason.semantic.evidence.extract_definitions(text, source_id=...)`: definition candidates from
  two-column Markdown tables (with or without outer pipes), labelled bullets and prose connectors.
  Candidates carry immutable source ID, document SHA256, exact character offsets and quotation.
  Equivalent duplicates retain all source spans. Conflicting definitions are retained and marked.
- `definitions.provider(text, include_all=True)`: exposes all extracted candidates to a prompt.
  The default preserves legacy eligibility rules for existing callers.
- `metric_catalog.Catalog.from_reply(reply, documents=..., connection=..., evidence_docs=...)`:
  validates a closed metric AST, dependencies, schema, evidence and conflicts across documents.
  It does not certify that a proposed interpretation matches the business meaning of its quotation.
- `QuestionContract`: a question can select a registered metric, add grounded filters, choose grouping
  and output, and bind its resolved period. It cannot replace the registered formula or population.
- `contract_runtime.ContractRuntime`: proposes a catalog once per shop, caches only structurally valid
  replies, and proposes question-specific constraints. Fixed metric fields are supplied by code.
  Cache identity includes DB/document hashes, model identity, implementation and extraction prompt.
  An invalid matched definition abstains rather than escaping to legacy intent/SQL fallback.
- `SemanticSolver` dispatches registered-definition questions through this path before legacy parsing.
  Questions without a literal term match retain legacy routing. This is not complete replacement
  of the primary parser and does not yet solve paraphrased metric selection.

Trace/cache records explicitly label validation as `schema_and_provenance_only`.
Evidence existence, AST validity and agreement between metadata fields do not prove business correctness.
In particular, a model can cite a correct definition and choose an incorrect population or formula.

## Opt-in development command

Run from `source_test/`, with a fresh output directory separate from test data:

```bash
python -m bizreason.run_v2 \
  --data <test_root> --out <fresh_output> --connection <connection_json> \
  --endpoint-file <endpoint_json> --aliases off --semantic-contracts on \
  --workers 4 --max-model-concurrency 4 --traces
```

This uses the existing model, not an additional model. The feature is OFF by default.
`run_submission.py` keeps its normal configuration; do not describe the experimental path as the submitted default.
The global run deadline also bounds catalog-lock waits, model-gate waits and request timeouts.
Failed extraction is not persisted as a successful catalog and is not repeatedly retried per question in one run.

Current limitations include literal term routing, conservative shop-quality/model-failure gates,
unsupported comparison-only contracts, share/yes-no contract outputs, calendar-expression grouping,
and incomplete automatic semantic validation of model-generated definitions.
Some unsupported constructs deliberately abstain. No calibration or training claim follows from unit tests.

## Staged evidence, 14 November

Artifacts reside under `/home/ubuntu/datamate_review_data/semantic_contract_study/`.
The study scripts refuse to overwrite their initial selections and snapshots.

- E0: historical train3000 and public-dev701 outcomes and abstain gates were aggregated.
  Root causes of executable wrong values remain undetermined unless separately traced.
  Historical performance is not a fresh evaluation of this implementation.
- E1 synthetic: 140 checks passed, including explicit safety/support-gap checks; 20 executable
  wrong semantic counterexamples were distinguished by independent Python references.
  A calendar-month grouping rejection is recorded as missing support, not an answered task.
- E1 real train: 10 manually supplied oracle plans over six shops were selected and saved before
  a separate train-only gold scorer ran. 10/10 answers matched. This small sample is biased toward
  supported families and does not estimate the oracle ceiling or test PA.
- E2: format/heading invariance, Unicode/CRLF offsets, duplicates, conflicts and forged evidence
  have synthetic regression tests. Runtime and CLI tests use fake models and no network.
- E3 live synthetic pilot: early versions rejected all four cases due schema/format issues.
  After interface improvements, `runtime_pilot_v5` yielded 1 correct, 1 wrong, 2 abstains on four cases.
  The wrong result selected a population inconsistent with the synthetic definition.
  These four development cases have been used to refine the prompt, so they are not independent validation.

E3 acceptance has therefore FAILED. Keep `--semantic-contracts off` and `--plan-fallback off`.
Do not proceed to a large test run, training or calibration by assuming compilation implies semantic truth.
Before E4, expand reviewed oracle coverage, separate extractor errors from question-parser errors,
and create fresh synthetic/shop/industry splits. Fine-tuning is conditional, not an automatic follow-up
to a few schema-format errors. E5 needs an actual fresh run on the target offline GPUs within six hours;
it has not been performed for this experimental path.

## Verification

```bash
/home/ubuntu/datamate/venv/bin/python -m pytest -o addopts="" -q
```

Focused tests live in `test_evidence.py`, `test_definition_formats.py`, `test_metric_catalog.py`
and `test_contract_runtime.py`. Existing tests are retained. Integration checks cover off-by-default CLI,
answer/trace output, client shutdown, model-gate deadline and refusal to fall through after contract rejection.

Final coordinator run: **1570 passed, 40 warnings in 55.91s**, exit code 0.
The saved log is `/home/ubuntu/datamate_review_data/semantic_contract_study/final_pytest.log`.
This includes compiler/runtime regressions and the dataset-literal guard; it is not a model accuracy result.

## Debug follow-up, 14 November

The four-case result above is the initial prototype, retained as history. A subsequent debug run
reproduced both JSON failures: the model finished normally but omitted an inner closing object
delimiter before the metrics array closed. The shared JSON decoder now inserts the missing inner
delimiter at that existing outer close; it does not invent values or complete a generic truncated EOF.
Typed model failures distinguish invalid JSON, incomplete generation, empty generation and provider errors.

The runtime now checks a narrow set of explicit population clauses directly against compiler-generated
status predicates, including canonical statuses absent from the current rows. `valid` is not assumed
to mean one named status. This checker covers only its explicit grammar, not arbitrary business prose.
A catalog has at most one bounded regeneration for a classified format failure or an explicit error from
the closed schema/evidence validator; provider failures are not treated as semantic repair opportunities.

The compact extraction protocol omits `dependencies`; code derives consumed references from its closed AST.
Explicit dependency lists from older callers remain strictly validated. This removes redundant output work
without certifying that the AST implements the prose definition. Filter operators are specified explicitly.

Final debug CPU run: **1609 passed, 40 warnings in 61.50s**, exit code 0.
Log: `/home/ubuntu/datamate_review_data/semantic_contract_study/debug_full_pytest_v3_final.log`.
Paired synthetic evaluations and limitations are recorded in
`../reports/PA_09_SEMANTIC_DEBUG_20261114.md`. Both experimental flags remain off by default.
