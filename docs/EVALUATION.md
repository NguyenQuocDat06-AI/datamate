# Evaluation protocol and results

Score: correct `+1`, wrong `−1`, abstain `0`; `PA = (correct − wrong) / total`. Always report correct, wrong and
abstain separately, and count `unanswerable` answers separately.

## Data splits

| Split | Size | Use |
|---|---|---|
| Train | 30 shops, 3,000 questions | Allowed for development and few-shot examples (same shop/source question excluded) |
| Public | 14 shops, 1,000 questions | Gold available for evaluation. Dev = `dev_001`–`dev_007`, `dev_011`, `dev_012`; held = `dev_008`–`dev_010`, `dev_013`, `dev_014` |
| Private | ≈ 1,000 questions | Unseen; released by the organizers on 2026-11-20 |

Since 2026-11-16 held shops were inspected question by question, so **held is no longer an independent estimate**.
Gold is never given to the solver or the prompts.

## Measurement rules

1. Freeze a code snapshot and configuration before a run; record the commit and uncommitted diff, model, split, data
   paths (absolute), reused caches, flags, worker count, API errors, time and correct/wrong/abstain.
2. An ablation is valid only on the same snapshot, split and conditions, changing exactly one component.
3. Separate replay results (reusing old intents/annotations) from end-to-end results with new model calls.
4. Infrastructure errors (model call failed, timeouts) are rerun, not scored as wrong answers.
5. Do not rerun full 1,000/3,000 questions after small changes; finish independent fixes and CPU checks first.

## Measured results (public split, optimistic)

| Run | Correct / Wrong / Abstain | PA | Note |
|---|---|---|---|
| Public, all 1,000 (run "m2", 2026-11-15, hosted Gemma 4 12B) | 876 / 39 / 85 | 0.837 | Latest full run |
| – dev shops (9) | 658 / 16 / 27 | 0.916 | Used for development |
| – held shops (5) | 218 / 23 / 58 | 0.652 | Now inspected |
| Public, m2 with newer code on 399 questions replaced | 890 / 41 / 69 | 0.849 | Mixed code versions, not one run |
| Train replay (no model call) | 2938 / 16 / 46 | 0.974 | Not independent |
| Layout-perturbed public shops (earlier snapshot) | – | 0.627 | Exposed subtotal rows and column order |

Later fixes (service tickets, ledger obligations, identity keys, ingestion) improved one held shop from PA 0.206 to
0.429 and left the sampled dev questions essentially unchanged.

## What these numbers do and do not say

* They are not an estimate of private accuracy. Shops, industries, layouts and rulebook variants in the private set
  are unseen; the gap between dev (0.916) and held (0.652) before held-targeted fixes is the more honest guide.
* The target was PA ≥ 0.90; held did not reach it.
* No packaged-image run on the full public set has been completed; an end-to-end GPU run of `predict.sh` was still
  being verified when this document was written.

## Next measurements

1. Real GPU `predict.sh` run on a small set, then on the public set with the image.
2. Layout-perturbation set (operators that preserve data meaning) to test ingestion robustness.
3. Pseudo-private set with rulebook variants.
4. Only then consider enabling the plan fallback, with a same-snapshot ablation.
