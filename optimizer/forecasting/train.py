"""Training script for request-rate forecasting models."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from optimizer.feature_engineering import build_features, feature_columns, infer_interval_seconds
from optimizer.forecasting.models import evaluate_regression, train_random_forest


def chronological_split(frame: pd.DataFrame, train_ratio: float = 0.8) -> tuple[pd.DataFrame, pd.DataFrame]:
    split_idx = max(1, int(len(frame) * train_ratio))
    return frame.iloc[:split_idx].copy(), frame.iloc[split_idx:].copy()


def _build_dataset(
    frames: Sequence[pd.DataFrame],
    horizons: tuple[int, ...],
    interval_seconds: float,
) -> pd.DataFrame:
    """Engineer features per trace (so lags never straddle two traces), then concatenate."""
    parts = [
        build_features(frame, horizons=horizons, with_targets=True, interval_seconds=interval_seconds)
        for frame in frames
        if not frame.empty
    ]
    parts = [part for part in parts if not part.empty]
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def train_models(
    frames: Sequence[pd.DataFrame],
    *,
    horizons: tuple[int, ...] = (1, 3, 5),
    interval_seconds: float | None = None,
    test_frames: Sequence[pd.DataFrame] | None = None,
    model_dir: str | Path = "data/models",
    report_path: str | Path | None = "data/processed/model_evaluation.csv",
    metadata_path: str | Path | None = None,
    data_source: str = "runtime telemetry",
    target_mode: str = "delta",
) -> tuple[list[dict], dict]:
    """Train one Random Forest per horizon and evaluate against a persistence baseline.

    If ``test_frames`` is given it is used as the held-out set (e.g. traffic profiles the
    model never saw); otherwise the last 20% of the concatenated, chronologically ordered
    training data is held out. ``horizons`` are minutes.

    ``target_mode`` is ``"delta"`` (learn the CHANGE in request rate over the horizon and add
    it to the current rate at inference; bounded and shape-independent, so it generalises to
    traffic shapes it never saw) or ``"absolute"`` (learn the future rate directly; a tree
    ensemble cannot predict outside the range it was trained on).
    """
    if target_mode not in {"delta", "absolute"}:
        raise ValueError("target_mode must be 'delta' or 'absolute'")
    if not frames or all(frame.empty for frame in frames):
        raise ValueError("Telemetry dataset is empty")

    if interval_seconds is None:
        interval_seconds = float(np.median([infer_interval_seconds(f) for f in frames if not f.empty]))

    train_set = _build_dataset(frames, horizons, interval_seconds)
    if train_set.empty:
        raise ValueError("Not enough telemetry samples for training")

    if test_frames:
        test_set = _build_dataset(test_frames, horizons, interval_seconds)
        if test_set.empty:
            raise ValueError("Held-out telemetry is too short for evaluation")
        train_frame = train_set
        test_frame = test_set
    else:
        train_frame, test_frame = chronological_split(train_set)
        if test_frame.empty:
            raise ValueError("Not enough telemetry samples to hold out a test split")

    columns = feature_columns(train_set)
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for horizon in horizons:
        target = f"target_request_rate_t_plus_{horizon}"
        model_path = model_dir / f"rf_request_rate_h{horizon}.joblib"
        if target_mode == "delta":
            y_train = train_frame[target] - train_frame["request_rate"]
            offset = test_frame["request_rate"].to_numpy()
        else:
            y_train = train_frame[target]
            offset = 0.0
        model = train_random_forest(train_frame[columns], y_train, model_path)
        predictions = np.maximum(model.predict(test_frame[columns]) + offset, 0.0)
        metrics = evaluate_regression(test_frame[target], predictions)

        # Persistence baseline on the SAME held-out rows: "rps stays what it is now".
        baseline = evaluate_regression(test_frame[target], test_frame["request_rate"])

        mae, base_mae = metrics["mae"], baseline["mae"]
        improvement = (1.0 - mae / base_mae) * 100.0 if mae is not None and base_mae else None
        rows.append(
            {
                "horizon_min": horizon,
                "model": "random_forest",
                "target_mode": target_mode,
                **metrics,
                "baseline_mae": baseline["mae"],
                "baseline_rmse": baseline["rmse"],
                "baseline_mape": baseline["mape"],
                "baseline_r2": baseline["r2"],
                "mae_improvement_vs_baseline_pct": improvement,
                "train_samples": int(len(train_frame)),
                "test_samples": int(len(test_frame)),
                "data_source": data_source,
            }
        )

    if report_path is not None:
        report_path = Path(report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_csv(report_path, index=False)

    metadata = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "data_source": data_source,
        "horizons": list(horizons),
        "target_mode": target_mode,
        "interval_seconds": interval_seconds,
        "feature_columns": columns,
        "train_samples": int(len(train_frame)),
        "test_samples": int(len(test_frame)),
        "report": str(report_path) if report_path is not None else None,
    }
    metadata_file = Path(metadata_path) if metadata_path else model_dir / "metadata.json"
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    metadata_file.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return rows, metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="Train random-forest forecasting models")
    parser.add_argument(
        "--input", nargs="+", default=["data/runtime/telemetry_snapshots.jsonl"], help="one or more telemetry JSONL files"
    )
    parser.add_argument("--test-input", nargs="*", default=None, help="optional held-out telemetry JSONL files")
    parser.add_argument("--model-dir", default="data/models")
    parser.add_argument("--report", default="data/processed/model_evaluation.csv")
    parser.add_argument("--metadata", default=None, help="defaults to <model-dir>/metadata.json")
    parser.add_argument("--horizons", default="1,3,5", help="forecast horizons in MINUTES")
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=None,
        help="telemetry sampling interval; inferred from timestamps when omitted",
    )
    parser.add_argument("--data-source", default="runtime telemetry")
    parser.add_argument("--target-mode", choices=["delta", "absolute"], default="delta")
    args = parser.parse_args()

    def _read(paths: Sequence[str]) -> list[pd.DataFrame]:
        frames = []
        for raw_path in paths:
            path = Path(raw_path)
            if not path.exists():
                raise FileNotFoundError(f"Telemetry input not found: {path}")
            frames.append(pd.read_json(path, lines=True))
        return frames

    horizons = tuple(int(part.strip()) for part in args.horizons.split(",") if part.strip())
    rows, metadata = train_models(
        _read(args.input),
        horizons=horizons,
        interval_seconds=args.interval_seconds,
        test_frames=_read(args.test_input) if args.test_input else None,
        model_dir=args.model_dir,
        report_path=args.report,
        metadata_path=args.metadata,
        data_source=args.data_source,
        target_mode=args.target_mode,
    )
    print(pd.DataFrame(rows)[["horizon_min", "mae", "r2", "baseline_mae", "baseline_r2", "mae_improvement_vs_baseline_pct"]])
    print(f"Sampling interval used: {metadata['interval_seconds']:.1f}s | models in {args.model_dir}")


if __name__ == "__main__":
    main()
