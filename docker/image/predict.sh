#!/bin/bash
set -euo pipefail
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
export PYTHONHASHSEED=42 TOKENIZERS_PARALLELISM=false
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
export DATAMATE_AUDIT_LOG_DIR=/result/logs
mkdir -p /result/logs
cd /code/src
exec python /code/container/predict.py
