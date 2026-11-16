# XNeural — DataMate Docker submission (v2)

Code version: commit `7f1216314437e837214c6ccb4eab475b96049a47` of the team repository (branch `arch/rulebook-ir`, parent
`23ff4817337b6a27fb1856a16389d7ad8525df98`; v3 = v2 plus debt/instalment obligations built from the shop's own ledgers, rulebook-gated
conventions that abstain when the current rulebook does not state them, per-row unit factors, customer-name/phone identity keys,
mixed-date ingestion and a few new measures; no model, weight or training change). The run is pure code reading and compiling of the data plus a local language model for annotating columns and parsing
questions into typed intents; no arbitrary generated SQL runs. No test-question IDs or answers are embedded in the inference logic.
Submission-only additions record per-question observability required by the guide (`submission_audit.py`, the reasoning returned
by the local server) and do not change any answer. Plan fallback, SQL fallback, consensus, reasoning, the answer gate and the
time reader are all **off**. Aliases are off, few-shot k=4 is on, and stored-value linking, column measures, rulebook terms and
rulebook definitions use the defaults of `run_v2`.

## Run

```bash
docker run --rm --gpus '"device=0,1"' --network none --shm-size=16g \
  --entrypoint bash -v /path/to/test:/data:ro -v /path/to/output:/result \
  zaic2026_datamate_xneural:final /code/predict.sh
```

`/code/predict.sh` takes no arguments. Input is `/data/questions.jsonl`, `/data/rulebook.md`, `/data/calendar.csv` and
`/data/data/<business_id>/`. The split's own rulebook and each shop README are read at runtime. The pipeline reads CSV/XLSX/SQLite,
annotates source columns with the packaged model (cached, validated before use), builds canonical SQLite layers, parses each
question into a typed intent, runs deterministic compiler functions and returns a value, `unanswerable` or `abstain`.

`/result/submission.jsonl` is the live answer checkpoint, flushed after each recorded answer and replaced with the complete final
output at the end. Per-question logs are in `/result/logs/questions.jsonl` (`id`, `answer`, `elapsed_ms`, `thinking`, `code`,
`inputs`, `output`). Empty thought channels are null; no reasoning is invented or summarized. `elapsed_ms` includes queue time and
shared preprocessing; shared model loading is recorded in the run timing and counts in the budget.

## Start-up checks (new in v2)

The first trial on the organizers' RTX 5090 machine stopped after 20 s with `model server exited with code 1`; the cause was only in
`server.log`, which the console did not show. v2 therefore:

1. runs `/code/container/gpu_preflight.py` in a child process before loading weights. It records library versions, each GPU's name,
   compute capability and memory, whether the installed torch lists kernels for that architecture, and runs a real bitsandbytes NF4
   layer on every GPU. The report is printed to the console and saved as `/result/logs/environment.json`.
2. if NF4 fails on any GPU, selects the unquantized fp16 weights (about 24 GB per 32 GB GPU) for all workers
   (`DATAMATE_QUANT=fp16`); a worker whose NF4 load fails after the preflight also prints its traceback and falls back to fp16.
   fp16 was never measured for accuracy in this pipeline; the measured configuration is NF4.
3. echoes the last 80 lines of `/result/server.log` to the console whenever the model server exits or does not answer.

**Not verified:** none of this has run on a GPU. The build host has no NVIDIA GPU. Loading on RTX 5090 hardware, the NF4 kernels on
that architecture, two-run determinism and completion within 6 hours are untested; only CPU checks (unit tests, Python 3.11
syntax and import checks, container layout) were run.

## Model and hardware

- Model: `google/gemma-4-12B-it`, fixed revision `707f0a3b8a3c7ad586ed01e27eafbad8a27dd0f7` (20 July 2026); original model
  release 3 June 2026; Apache-2.0. Parameters: 11,959,730,224 according to upstream safetensors metadata. One model is reused for
  annotation and intent parsing; the two GPU replicas load the same weights. No adapters, no auxiliary models.
- All inference weights, configs and tokenizer files are local at `/code/models/gemma-4-12B-it`; sources, byte sizes and SHA-256
  values are in `/code/model_manifest.json`.
- Original BF16 checkpoint loaded as NF4 with fp16 compute (fp16 weights when the preflight selects the fallback). Batch size 1,
  seed 42, greedy decoding, 4 question threads, GPUs 0 and 1, 21000-second overall budget including server start-up.
- PyTorch 2.9.1 CUDA 12.8, bitsandbytes 0.50.2, transformers 5.23.0 on Python 3.11; resolved dependencies in
  `/code/requirements.lock.txt`. The stack is not vLLM and does not use NCCL; the guide's NCCL workaround is not needed.

## Measured results of this code (development measurements, not evidence of private PA)

Public test, end-to-end with the same local model (NF4) on other GPUs: PA 0.837 over 1000 questions (shops used for development
0.916; five shops treated as held-out 0.652). Training split replay: PA 0.974 over 3000. Held-out PA did not improve over the
previous version (0.649 to 0.652), so these numbers do not show generalization to unseen shops.

## Training and verification

No parameter training. See `/code/training_code/README.md` for the 3000-question TRAIN split,
653-example frozen bank, its preparation/export commands and the historical-label provenance limitation.
No organizer public/private gold or public/private test inputs are included.

All inference assets are inside the image, including weights; public model URLs are provenance only
and are never used during inference. Offline Hugging Face flags are fixed in the image and entrypoint.
The final image is linux/amd64. Artifact/source manifests and build verification are packaged under `/code`.
