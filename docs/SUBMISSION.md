# Running the submission on the exam machine

One command starts the local model server, runs the V2 pipeline, and writes the submission file. Nothing is
downloaded and no network is used.

```
python run_submission.py \
  --data <test_root> \
  --model-dir <model_dir> \
  --out <work_dir> \
  --submission <work_dir>/submission.jsonl \
  --fewshot-bank <fewshot_bank.jsonl> \
  --total-seconds 21000 \
  --workers 12
```

All paths above are placeholders. Replace them with the real locations on the machine.

## Before the exam: what goes where

| Item | Location | Notes |
|---|---|---|
| Model weights | `<model_dir>` | A local checkpoint directory: `config.json`, tokenizer files, `*.safetensors`. Full-precision weights; the code loads them in 4-bit NF4 at start-up. |
| Few-shot bank | any path, passed with `--fewshot-bank` | Optional. A jsonl file of solved questions with intents. Omit the flag to run without it. |
| Test set | `<test_root>` | Must contain `questions.jsonl`, `rulebook.md`, and `data/<business_id>/`. Read-only. |
| Work directory | `<work_dir>` | Caches, checkpoints, `server.log`, `timing.jsonl`. Must not be inside `<test_root>`. |
| Submission | `--submission` path | Copied from `<work_dir>/submission.final.jsonl` at the end. |

The model directory name becomes the model id reported by the server. It is not used by any other part of the run.

## What the command does

1. Starts `serve/local_server.py` with `--gpus 0,1`: one worker process per GPU, each loading the model in 4-bit.
   A random bearer token is generated for this run and passed in the environment (never on a command line).
   Output goes to `<work_dir>/server.log`.
2. Waits until `/v1/models` answers with the token, then waits for each worker's warm-up request.
   Timeout is `--server-timeout` (default 1800 s).
3. Writes `<work_dir>/connection.json` with mode 600 (model id and token). This file is deleted after the run.
4. Runs `python -m bizreason.run_v2` against `http://127.0.0.1:8000/v1` with `--aliases off`,
   `--workers <N>`, `--max-model-concurrency <N>`, and `--total-seconds` set to
   `total - server start - 300`. run_v2 applies its own 300 s margin on top of that, so questions stop being
   started about 600 s before the total budget ends.
5. Stops the server (SIGTERM, then SIGKILL after 60 s) in every case, including Ctrl-C and SIGTERM.
6. Copies `<work_dir>/submission.final.jsonl` to `--submission`. If the pipeline did not finish cleanly, the final
   file is rebuilt from the checkpoint `<work_dir>/submission.jsonl`. Every question that has no answer becomes
   `{"id": ..., "type": "abstain", "reason": "not answered"}`.

The exit status is non-zero only when no submission file could be written.

## Expected time

- Server start: several minutes, dominated by loading the weights into two GPUs (the exact figure is unknown
  until measured on the T4s; it is logged as `server_start_seconds`).
- Pipeline: runs until its budget or until all questions are answered, whichever is first.
  The budget defaults to 21000 s (5 h 50 min), leaving 10 minutes under the 6 hour limit.
- Finish: the final TIMING line gives `server_start_seconds`, `pipeline_seconds` and `total_seconds`.
  `total_seconds` should stay below 21000 s plus a few minutes for shutdown.

## How to verify the output

1. The last lines of the console log should be:
   ```
   SUBMISSION WRITTEN: <submission path> (<N> lines; source: run_v2; types: {...})
   TIMING server_start_seconds=... pipeline_seconds=... total_seconds=...
   ```
   A `WARNING: the pipeline did not finish cleanly` line means some questions were not answered.
   `ERROR: no submission could be written` means exit status 1.
2. In `<work_dir>/server.log`, each worker should print `startup self-test passed`,
   `prefix cache self-check passed`, and `listening on 127.0.0.1:<port>`. A `prefix cache disabled` line is not an
   error, but it makes the run slower.
3. Check the file itself: one line per question, each valid JSON, and no duplicate ids.
   ```
   python -c "import json,sys; q=[json.loads(l)['id'] for l in open(sys.argv[1],encoding='utf-8') if l.strip()]; \
   s=[json.loads(l) for l in open(sys.argv[2],encoding='utf-8') if l.strip()]; \
   print('questions', len(set(q)), 'submission lines', len(s), 'unique ids', len({r['id'] for r in s}), \
   'types', __import__('collections').Counter(r['type'] for r in s))" \
   <test_root>/questions.jsonl <submission path>
   ```
   The counts of questions and submission lines should match, and so should the unique id counts.

## Testing without a GPU

The unit tests do not need torch, transformers, or a GPU. They use a fake server subprocess and a stub for run_v2:

```
python -m pytest -o addopts="" -q tests/test_run_submission.py
```

These tests check the server lifecycle, the connection file permissions, the budget arithmetic, the submission
copy, the fallback when run_v2 fails, and the gateway. They do not check model loading, quantization, or
generation quality, which need the real GPUs.

## Assumptions to confirm with the organizers

These are assumptions, not facts. Each one needs confirming before the exam.

- **Offline run.** No network during the exam. Nothing is downloaded at run time, and `pip install` is not run.
  The Python environment must already contain torch (CUDA build), transformers, bitsandbytes, accelerate,
  httpx, pydantic, openpyxl and python-dotenv. fastapi and uvicorn are not needed.
- **Hardware.** Two NVIDIA T4 GPUs, 16 GB each, visible as CUDA devices 0 and 1. The server pins one worker to
  each GPU with `CUDA_VISIBLE_DEVICES`.
- **No bf16 on T4.** The weights are cast to fp16, and bitsandbytes NF4 uses fp16 compute. If a different GPU
  is provided, this path still works but has not been measured.
- **Model size.** At most 15 B parameters. Each GPU holds a full 4-bit copy (data parallel, not sharded). For a
  14 B model this was estimated at about 9.5 GB per GPU, which is an estimate from the earlier 12 h server
  script, not a measurement on this hardware.
- **Time limit.** 6 hours covers everything, including model load. The default budget leaves 10 minutes spare.
  The true limit and whether it counts the exam set-up time need confirming.
- **Failure policy.** If the model server does not start, the run still writes a submission (abstains plus any
  checkpointed answers) and exits 0, following the rule that exit status is non-zero only when no file is
  written. Confirm that a degraded run with exit 0 is acceptable, or that a degraded run should be treated as a
  failure. The log line `WARNING` is the only marker.
- **Input layout.** `--data` contains `questions.jsonl`, `rulebook.md` and `data/<business_id>/` in the same
  layout as the development test set. Confirm this with the organizers.
- **Writable paths.** `--out` and `--submission` are writable and are not inside the test data directory.
- **Prompt profile and few-shot.** The run uses `--prompt-profile default` and the server's chat template from
  the model directory. Confirm which prompt profile matches the final model, and whether the few-shot bank is
  part of the submission.
- **Model identity.** The model directory is the one the team tuned on. The server does not check the weights
  against any hash.
- **Server behaviour.** Greedy decoding for temperature 0, micro-batching, shared-prefix KV cache (enabled, with
  a self-check that disables it on a mismatch), eos handling and think-block stripping follow the earlier
  12 h server. Streaming requests are answered with one complete SSE event sequence; the pipeline does not
  stream.
- **Token.** The bearer token is random per run and only lives in the process environment and in
  `connection.json` (mode 600, deleted at the end). The server never prints it.
- **Proxy.** The run sets `NO_PROXY=127.0.0.1,localhost` for its children. If the exam machine has an HTTP proxy
  configured for other purposes, this is needed for the loopback calls to work.

## Final configuration (13/11)

`run_submission.py` with `--fewshot-bank <fewshot_bank_v5.jsonl>` and the defaults of `run_v2`:

| Option | Value | Why |
|---|---|---|
| `--rule-terms` | on | ranking terms are translated from the test set's own rulebook at start (one model call, cached) |
| `--rulebook-definitions` | on | rulebook rows for ranking and industry terms the question uses are shown to the parser |
| `--fewshot-k` | 4 | retrieved examples; held-out public +4 points over none |
| `--consensus` | off | measured: fewer wrong answers but more lost correct ones, and 1.5x slower |
| `--sql-fallback` | off | measured: two SQL readings agreed on wrong answers |
| `--reasoning` | off | measured: slower and lower |

Measured with code 0ad37de: train 3000 PA 0.959 (c23d215), public 1000 PA 0.752
(dev shops 0.809, held-out shops 0.619). Offline package on Kaggle T4x2: 3000 questions in 4.4 h, 1000 in 1.6 h.

## Final configuration (14/11)

Defaults of `run_v2` now give the submitted configuration; `run_submission.py` adds the few-shot bank.

| Option | Value | Measured on public (dev / held-out shops) |
|---|---|---|
| `--value-linking` | on | 0.809 / 0.619 -> 0.819 / 0.642 |
| `--column-measures` | on | with kept columns, README conventions and rule gating: 0.842 / 0.659 |
| `--rule-terms`, `--rulebook-definitions` | on | |
| `--consensus`, `--sql-fallback`, `--reasoning` | off | each measured lower |

Data stage per shop (one model, cached): column annotation, statuses, line kinds, value groups, README conventions
(checked on the data before use). Every source column is kept (`x_` columns, `src_` tables, `column_catalog`).
Industry rules apply only when the test set's rulebook (or the shop README) states them.
Public 1000 questions: PA 0.787 (code 623b9a2). Run time on 2 servers: 1.1 h; one T4x2 machine ~2 h.
