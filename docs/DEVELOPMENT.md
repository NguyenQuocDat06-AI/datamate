# Development guide

## Environment

* Python 3.11+. The unit tests need no GPU, model or network.
* `cd source_test && pip install -e ".[dev]"`.
* Datasets live in `source_test/data/` (git-ignored): `train/`, `public_test/`. A test root contains
  `questions.jsonl`, `rulebook.md`, `calendar.csv` and `data/<business_id>/` with the shop's files and `README.md`.

## Tests

```bash
cd source_test
python -m pytest -o addopts="" -q                         # everything
python -m pytest -o addopts="" -q tests/test_semantic_intent.py
```

* `tests/test_no_dataset_literals.py` forbids dataset-specific literals in the source. It supplements, and does not
  replace, review.
* `tests/test_semantic_intent.py` pins the length and SHA-256 of the default intent prompt. When you intentionally
  change the prompt, update `DEFAULT_SYSTEM_SHA256` and the length and say why in the commit message.
* Add synthetic tests for logic changes. Never edit or weaken a test, add `skip`/`xfail`, or loosen an assertion to
  make a fix look right. Test fixtures must not contain real customer names, phones or other personal data.

## Working on the pipeline

1. Find the cause structurally (types, table relations, arithmetic, rulebook/README definitions) using traces and
   caches. Replay offline before calling a model again.
2. Fix it generally. Do not add IDs, hashes, product or customer names, dates or question fragments to conditions.
   Business vocabulary goes in `bizreason/semantic/vocabulary.py` or `bizreason/data/vocabulary.py`.
3. Rulebook-dependent behaviour must go through the convention gates; a rule seen in one industry is not a universal
   truth.
4. For ingestion/layout changes, compare source and result: rows/orders, date range, money and quantity totals; check
   continuation rows of an order are not removed as subtotals; run regression on the original shops.
5. Run the focused tests, then the full suite.

## Running against a model

```bash
cd source_test
python -m bizreason.run_v2 --help
```

The measured defaults: `--aliases off`, `--conventions on`, `--column-measures on`, `--value-linking on`,
`--rule-terms on`, `--rulebook-definitions on`, `--fewshot-k 4`; `--plan-fallback`, `--sql-fallback`, `--consensus`,
`--time-reader` and reasoning off. Check the actual defaults in `run_v2.py` before a measurement.

Model endpoints, tokens and `.env` files are never committed or printed. `serve/model_api.py` is a development helper
that hosts the model on Kaggle GPU sessions; it reads credentials from the environment or its state directory.

## Git workflow

* Develop on a feature branch; keep commits focused; stage explicit files, not `git add .`.
* Before pushing, scan the diff for secrets and make sure no data, gold, traces, caches, SQLite or `.env` is included.
* Merging to `main` requires the full suite to pass.

## Repository layout

```
source_test/bizreason/      package (data/, semantic/, evaluation/, top-level helpers)
source_test/serve/          local_server.py (submission), model_api.py (dev)
source_test/container/      predict.py, gpu_preflight.py
source_test/tests/          unit and regression tests
source_test/scripts/        measurement and analysis scripts (development only; see header of each)
docker/                     image build context templates, tools, packaging docs
docs/                       this documentation
```

`source_test/scripts/` contains historical measurement scripts. Some are tied to the local development machine and
datasets; treat them as examples, not as a supported CLI.
