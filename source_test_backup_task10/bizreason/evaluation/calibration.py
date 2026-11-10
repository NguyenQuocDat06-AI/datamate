"""Fit on separate/OOF prediction records; persist numbers as JSON, never pickle."""

from __future__ import annotations

from typing import Any

from bizreason.decision import FEATURE_NAMES


def fit_gate(records: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        raise ValueError("Install the optional calibration dependencies") from None
    if len({record["business_id"] for record in records}) < 2:
        raise ValueError("Gate fitting requires predictions from at least two businesses")
    if any(record.get("prediction_origin") != "held_out" for record in records):
        raise ValueError("Gate records must explicitly declare held_out prediction origin")
    features = [record["features"] for record in records]
    if any(len(row) != len(FEATURE_NAMES) for row in features):
        raise ValueError("Invalid calibration feature dimensions")
    scaler = StandardScaler().fit(features)
    scaled = scaler.transform(features)

    def fit(label: str) -> dict[str, Any]:
        labels = [int(record[label]) for record in records]
        if set(labels) != {0, 1}:
            raise ValueError(f"Both positive and negative labels are required for {label}")
        classifier = LogisticRegression(C=1.0, max_iter=1000, random_state=2026).fit(scaled, labels)
        return {
            "mean": scaler.mean_.tolist(),
            "scale": scaler.scale_.tolist(),
            "coefficients": classifier.coef_[0].tolist(),
            "intercept": float(classifier.intercept_[0]),
        }

    return {
        "version": "1",
        "feature_names": FEATURE_NAMES,
        "n": len(records),
        "fit_businesses": sorted({record["business_id"] for record in records}),
        "source_revisions": sorted({str(r.get("source_revision", "")) for r in records if r.get("source_revision")}),
        "models": sorted({str(r.get("model", "")) for r in records if r.get("model")}),
        "backends": sorted({str(r.get("backend", "")) for r in records if r.get("backend")}),
        "value_model": fit("correct_value"),
        "unanswerable_model": fit("correct_unanswerable"),
        "note": "Evaluate on disjoint businesses; logistic outputs are estimates, not proven correctness.",
    }
