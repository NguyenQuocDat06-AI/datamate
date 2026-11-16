#!/bin/bash
set -euo pipefail
python /code/training_code/data_generation/export_bank.py --out /tmp/reproduced_fewshot_bank.jsonl --reference /code/assets/fewshot_bank.jsonl
echo 'Frozen TRAIN intent bank reproduced byte-for-byte. No model training was performed.'
