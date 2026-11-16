"""Collect disjoint prediction batches, preserving values and recording provenance."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from bizreason.contracts import Prediction
from bizreason.formatting import SubmissionWriter
from bizreason.utils import atomic_json, file_hash, load_jsonl, writable_path


def collect(manifest: Path, batches: list[Path], output: Path, report: Path) -> dict:
    expected_ids = json.loads(manifest.read_text(encoding="utf-8"))["question_ids"]
    if len(expected_ids) != len(set(expected_ids)):
        raise ValueError("Expected manifest contains duplicate IDs")

    output, report = writable_path(output), writable_path(report)
    if output == report or output in [path.resolve() for path in batches]:
        raise ValueError("Collection outputs must differ from their inputs and each other")
    if manifest.resolve() in {output, report} or report in [path.resolve() for path in batches]:
        raise ValueError("Collection outputs must differ from their inputs")

    predictions: dict[str, Prediction] = {}
    origins: dict[str, str] = {}
    sources = []
    for path in batches:
        rows = load_jsonl(path)
        for row in rows:
            prediction = Prediction.model_validate(row)
            if prediction.id in predictions:
                raise ValueError(f"Overlapping batch ID: {prediction.id}")
            predictions[prediction.id] = prediction
            origins[prediction.id] = str(path)
        sources.append({"path": str(path), "sha256": file_hash(path), "count": len(rows)})

    missing = set(expected_ids) - predictions.keys()
    extra = predictions.keys() - set(expected_ids)
    if missing or extra:
        raise ValueError(f"Collection is incomplete or out of scope: missing={sorted(missing)}, extra={sorted(extra)}")

    temporary = output.with_suffix(output.suffix + ".writing")
    with SubmissionWriter(temporary) as writer:
        for identity in expected_ids:
            writer.write(predictions[identity])
    temporary.replace(output)
    result = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "count": len(expected_ids),
        "expected_manifest": str(manifest),
        "expected_manifest_sha256": file_hash(manifest),
        "batches": sources,
        "per_question_source": [{"id": identity, "batch": origins[identity]} for identity in expected_ids],
        "output": str(output),
        "output_sha256": file_hash(output),
        "ground_truth_available": False,
        "pa": None,
        "note": "Collection preserves each input prediction. It does not rerun, correct or grade answers, and does not assert a common solver version across batches.",
    }
    atomic_json(report, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--batch", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = collect(args.manifest, args.batch, args.output, args.report)
    print(json.dumps({"count": result["count"], "output": result["output"], "report": str(args.report)}))


if __name__ == "__main__":
    main()
