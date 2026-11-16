# Training and data preparation

No model parameter training or fine-tuning was performed for this submission. Inference uses
the original Google Gemma 4 12B instruction model at the fixed revision in `/code/model_manifest.json`.
There is no adapter, embedding model, reranker, optimizer checkpoint or additional teacher at inference.

The organizer TRAIN split (30 businesses, 3000 questions) is packaged in `data/train/` for verification.
No public/private test inputs, gold answers, acceptance sets, inference caches or credentials are packaged.
The retrieval bank contains 653 frozen structured intent examples from TRAIN. Every bank ID, business ID
and question is checked against the packaged TRAIN questions. The inference retriever excludes the
current business and question according to the packaged `bizreason.semantic.fewshot` implementation.

The structured labels are cached model-generated intents from development. The original raw provider
HTTP responses and exact historical teacher prompts were not retained; we do not claim to reproduce
those historical provider generations. The final frozen labels themselves, their checksums, preparation
code and the exact inference prompt templates are packaged for inspection. This provenance limitation
must be resolved or accepted by the organizers before treating this trial as a verified final submission.
No new API-generated training data was created during Docker packaging.

Reproduce the bank export offline:

```bash
bash /code/training_code/train.sh
```

This validates and exports the frozen intent dataset; it does not train model parameters or call an API.
The output is byte-identical to `/code/assets/fewshot_bank.jsonl`. Dependencies are pinned in
`/code/requirements.lock.txt`. Data paths, sizes and SHA256 values are in `data_manifest.json`.
Inference source and prompt templates are in `/code/src/bizreason/`.
