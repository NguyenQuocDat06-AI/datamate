# Docker submission guide

The organizers run a single image offline: no network, `/data` mounted read-only, `/result` writable, 2 × T4 (16 GB)
or comparable, at most 6 hours including model load. The entry point is `/code/predict.sh`.

## What is in the image

```
/code/predict.sh            entry point (sets offline env, runs container/predict.py)
/code/container/            predict.py, gpu_preflight.py
/code/src/                  bizreason/, serve/local_server.py, run_submission.py
/code/models/               google/gemma-4-12B-it weights (revision pinned in model_manifest.json)
/code/assets/               frozen few-shot bank (from the training split only)
/code/training_code/        data preparation description and export script (no parameter training)
/code/model_manifest.json   model id, revision, license, parameter count, file SHA-256
/code/artifact_manifest.json  SHA-256 of every packaged file
```

Environment: Python 3.11, torch 2.9.1 (cu128), transformers 5.23.0, bitsandbytes 0.50.2 (pins in
`docker/requirements.txt`). `HF_HUB_OFFLINE=1`, `PYTHONPATH=/code/src`, audit logs in `/result/logs`.

## Runtime behaviour

1. `predict.py` runs `gpu_preflight.py` in a child process (no CUDA context in the parent). It checks library versions,
   each GPU's compute capability and memory, whether torch has kernels for that architecture, and a real NF4 forward
   pass. If NF4 works it loads the 4-bit model; otherwise the fp16 weights. bf16 is never assumed (T4 lacks it).
2. One worker per GPU is started and warmed up.
3. `run_submission.py` runs the pipeline under the time budget and rewrites `submission.jsonl` after every answer.
4. Per-question logs are written to `/result/logs/questions.jsonl` (`id`, `answer`, `elapsed_ms`, generated thinking).

## Build

Requirements: Docker with `linux/amd64` support (QEMU is enough to build, not to run on GPU), the repository at the
commit to ship, and a directory (`EXTRA`) holding the data that is deliberately not in git: the training split under
`training_code/data/`, `assets/fewshot_bank.jsonl`, and verification evidence.

```bash
# 1. assemble the build context from the committed source
python docker/tools/make_context.py --out docker/build --extra "$EXTRA"
python docker/tools/make_manifest.py --out docker/build

# 2. dependency image (slow under emulation; reuse it between code-only rebuilds)
docker build --platform linux/amd64 --target deps -t datamate-runtime-deps:TAG \
  -f docker/build/context/Dockerfile docker/build/context

# 3. runtime image on top of the dependency image
docker build --platform linux/amd64 -t datamate-runtime:TAG \
  -f docker/build/context/Dockerfile.build docker/build/context
```

`Dockerfile.build` must `FROM` the dependency tag used in step 2. `docker/Makefile` wraps these steps
(`make -C docker context image`).

The image is built **without weights**. They are added at export time so the build host never needs a second 24 GB copy.

## Export (docker-save compatible archive with weights)

```bash
python docker/tools/export_image.py \
  --source datamate-runtime:TAG --tag zaic2026_datamate_xneural:final \
  --model-manifest docker/build/context/code/model_manifest.json \
  --model-files /path/to/small_model_files \        # config, tokenizer, ... (not the big safetensors)
  --remote "<rclone-remote>:<path>/zaic2026_datamate_xneural.tar.gz" \
  --receipt export_receipt.json
```

The exporter streams `docker save`, inserts one extra layer containing `/code/models/...`, downloads
`model.safetensors` from Hugging Face while hashing it (the SHA-256 must match the manifest), compresses with `pigz`
and pipes to `rclone rcat`. Afterwards compare the remote MD5 with the receipt. Use `DATAMATE_DOCKER_SOCKET` and
`DATAMATE_DOCKER_PREFIX` (for example `sudo`) to select the Docker daemon. Hugging Face credentials, if any, come from
the environment and are never written to logs or the receipt.

Submit exactly: the share link, the MD5 of the `.tar.gz`, the compressed size, and keep the file name
`zaic2026_datamate_xneural.tar.gz` unchanged. Download it once from a different machine and run `md5sum -c`.

## Verification checklist

Do all of these before submitting; a green unit-test run is not enough.

| Check | How |
|---|---|
| Imports work as a script | `docker run --rm --network none --entrypoint "" IMAGE python -c "import sys; sys.path[:0]=['/code/src']; import bizreason.submission_audit"` and run the server module as a script |
| Offline pipeline | Run a few questions inside the image with `--network none` against a live model; check `submission.jsonl` and `/result/logs/questions.jsonl` |
| Real GPU run | Run `predict.sh` on a T4 × 2 host (for example a Kaggle kernel with the image's contents) on a small question set; check pre-flight, server "listening", no `ModuleNotFoundError`, answers other than abstain |
| Different GPU | If the organizers' hardware may differ (Ampere/Blackwell), confirm the pre-flight chooses a path that works |
| Integrity | MD5 on remote equals the receipt; `artifact_manifest.json` SHA-256 values match |

## Lessons from the first submissions

* **Script import bug.** `local_server.py` runs as a script, so `sys.path[0]` is `serve/`, not `src/`. The audit hook
  `import bizreason.submission_audit` then raised `ModuleNotFoundError` on every request and every answer became
  `abstain`. Fix: `make_context.py` inserts `/code/src` into `sys.path` before the import, and the Dockerfile sets
  `PYTHONPATH=/code/src`. The two earlier exports contain the bug. **Always test the container entry point end to end.**
* Free disk space before building: the dependency layers, QEMU and the save stream need tens of GB.
* `torchaudio` may conflict with the pinned torch in some test hosts; it is not needed by the pipeline.
* Keep the weights revision pinned and verify the SHA-256; do not download from the network at inference.

## Compliance recap

Total parameters of every model used: 11,959,730,224 (≤ 15 B); a single model; no fine-tuning, adapter, embedding
model or reranker; weights released 2026-06-03 (before 2026-10-01); Apache-2.0. See [`COMPLIANCE.md`](COMPLIANCE.md).
