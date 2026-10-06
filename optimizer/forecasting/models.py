"""Forecasting model implementations."""

from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score


def train_random_forest(X, y, output_path: str | Path):
    """Train and persist the initial forecasting model."""
    model = RandomForestRegressor(n_estimators=200, random_state=42, n_jobs=-1)
    model.fit(X, y)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, output)
    return model


def persistence_forecast(series, horizon: int = 1):
    values = np.asarray(series, dtype=float)
    if len(values) <= horizon:
        return np.array([]), np.array([])
    actual = values[horizon:]
    predicted = values[:-horizon]
    return actual, predicted


def evaluate_regression(actual, predicted) -> dict[str, float | None]:
    actual_arr = np.asarray(actual, dtype=float)
    pred_arr = np.asarray(predicted, dtype=float)
    if len(actual_arr) == 0:
        return {
            "mae": None,
            "rmse": None,
            "mape": None,
            "r2": None,
            "spike_precision": None,
            "spike_recall": None,
        }

    mae = float(mean_absolute_error(actual_arr, pred_arr))
    rmse = float(np.sqrt(mean_squared_error(actual_arr, pred_arr)))
    mape = None
    non_zero = np.abs(actual_arr) > 1e-9
    if np.any(non_zero):
        mape = float(np.mean(np.abs((actual_arr[non_zero] - pred_arr[non_zero]) / actual_arr[non_zero])) * 100)

    r2 = float(r2_score(actual_arr, pred_arr)) if len(actual_arr) > 1 else None

    threshold = np.percentile(actual_arr, 90) if len(actual_arr) > 1 else actual_arr[0]
    actual_spike = actual_arr >= threshold
    pred_spike = pred_arr >= threshold
    tp = int(np.sum(actual_spike & pred_spike))
    fp = int(np.sum(~actual_spike & pred_spike))
    fn = int(np.sum(actual_spike & ~pred_spike))
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0

    return {
        "mae": mae,
        "rmse": rmse,
        "mape": mape,
        "r2": r2,
        "spike_precision": float(precision),
        "spike_recall": float(recall),
    }


def load_model(path: str | Path):
    return joblib.load(path)
