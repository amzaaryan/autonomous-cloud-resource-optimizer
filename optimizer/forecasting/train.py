"""Training script for request-rate forecasting models."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd

from optimizer.feature_engineering import build_features, feature_columns
from optimizer.forecasting.models import evaluate_regression, persistence_forecast, train_random_forest


def chronological_split(frame: pd.DataFrame, train_ratio: float = 0.8) -> tuple[pd.DataFrame, pd.DataFrame]:
    split_idx = max(1, int(len(frame) * train_ratio))
    return frame.iloc[:split_idx].copy(), frame.iloc[split_idx:].copy()


def main() -> None:
    parser = argparse.ArgumentParser(description="Train random-forest forecasting models")
    parser.add_argument("--input", default="data/runtime/telemetry_snapshots.jsonl")
    parser.add_argument("--model-dir", default="data/models")
    parser.add_argument("--report", default="data/processed/model_evaluation.csv")
    parser.add_argument("--metadata", default="data/models/metadata.json")
    parser.add_argument("--horizons", default="1,3,5")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Telemetry input not found: {input_path}")

    raw = pd.read_json(input_path, lines=True)
    if raw.empty:
        raise ValueError("Telemetry dataset is empty")

    horizons = tuple(int(part.strip()) for part in args.horizons.split(",") if part.strip())
    feature_frame = build_features(raw, horizons=horizons, with_targets=True)
    if feature_frame.empty:
        raise ValueError("Not enough telemetry samples for training")

    train_frame, test_frame = chronological_split(feature_frame)
    columns = feature_columns(feature_frame)

    model_dir = Path(args.model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for horizon in horizons:
        target = f"target_request_rate_t_plus_{horizon}"
        X_train = train_frame[columns]
        y_train = train_frame[target]
        X_test = test_frame[columns]
        y_test = test_frame[target]

        model_path = model_dir / f"rf_request_rate_h{horizon}.joblib"
        model = train_random_forest(X_train, y_train, model_path)
        predictions = model.predict(X_test)
        metrics = evaluate_regression(y_test, predictions)

        baseline_actual, baseline_pred = persistence_forecast(feature_frame["request_rate"], horizon=horizon)
        baseline_metrics = evaluate_regression(baseline_actual, baseline_pred)

        rows.append(
            {
                "horizon_min": horizon,
                "model": "random_forest",
                **metrics,
                "baseline_mae": baseline_metrics["mae"],
                "baseline_rmse": baseline_metrics["rmse"],
                "baseline_mape": baseline_metrics["mape"],
                "baseline_r2": baseline_metrics["r2"],
            }
        )

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(report_path, index=False)

    metadata = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "input_path": str(input_path),
        "horizons": list(horizons),
        "feature_columns": columns,
        "train_samples": int(len(train_frame)),
        "test_samples": int(len(test_frame)),
        "report": str(report_path),
    }
    metadata_path = Path(args.metadata)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
