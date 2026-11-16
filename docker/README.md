# docker/

Templates and tools for building the submission image. The full procedure and lessons learned are in
[`../docs/DOCKER.md`](../docs/DOCKER.md).

| Path | Purpose |
|---|---|
| `Dockerfile` | Two-stage build: `deps` (Python 3.11, torch cu128, pinned libraries) and `submit` (code and entry point) |
| `Dockerfile.build` | The `submit` stage on top of an existing `datamate-runtime-deps:*` image (avoids rebuilding deps under emulation) |
| `requirements.txt` | Pinned Python dependencies of the runtime |
| `image/` | Files copied to `/code`: `predict.sh`, `README.md`, `model_manifest.json`, `requirements.txt` |
| `overlay/submission_audit.py` | Per-question log writer injected into the image only; it never changes an answer |
| `training_code/` | Description and export script of the frozen few-shot bank (no model training) |
| `tools/make_context.py` | Builds `docker/build/context` from the committed source plus `--extra` data |
| `tools/make_manifest.py` | Writes `artifact_manifest.json` (SHA-256 of every packaged file) |
| `tools/export_image.py` | Streams a docker-save archive with the verified weights layer to a file or `rclone` remote |
| `Makefile` | Shortcuts: `make context EXTRA=...`, `make image TAG=...` |

`docker/build/` is generated and git-ignored. Never commit weights, datasets, gold files or credentials here.
