"""Feature engineering for forecasting models."""

from __future__ import annotations

import pandas as pd


BASE_COLUMNS = ("cpu_usage", "memory_usage", "request_rate", "p95_latency_ms", "error_rate", "replica_count")


def build_features(
    frame: pd.DataFrame,
    lags: tuple[int, ...] = (1, 2, 4),
    rolling_windows: tuple[int, ...] = (3, 5),
    horizons: tuple[int, ...] = (1, 3, 5),
    with_targets: bool = True,
) -> pd.DataFrame:
    """Build lagged, rolling, and rate-of-change features."""
    keep_cols = []
    if "timestamp" in frame.columns:
        keep_cols.append("timestamp")
    keep_cols.extend(column for column in BASE_COLUMNS if column in frame.columns)
    result = frame[keep_cols].copy()

    for column in BASE_COLUMNS:
        if column not in result:
            result[column] = 0.0
        result[column] = pd.to_numeric(result[column], errors="coerce")

    for lag in lags:
        for column in BASE_COLUMNS:
            result[f"{column}_lag_{lag}"] = result[column].shift(lag)

    for window in rolling_windows:
        for column in ("cpu_usage", "memory_usage", "request_rate", "p95_latency_ms"):
            result[f"{column}_roll_mean_{window}"] = result[column].rolling(window).mean()
            result[f"{column}_roll_std_{window}"] = result[column].rolling(window).std()
            result[f"{column}_roll_max_{window}"] = result[column].rolling(window).max()

    for column in ("cpu_usage", "memory_usage", "request_rate"):
        result[f"{column}_roc_1"] = result[column].diff(1)

    if with_targets:
        for horizon in horizons:
            result[f"target_request_rate_t_plus_{horizon}"] = result["request_rate"].shift(-horizon)

    return result.dropna().reset_index(drop=True)


def feature_columns(frame: pd.DataFrame) -> list[str]:
    return [
        col
        for col in frame.columns
        if not col.startswith("target_request_rate_t_plus_") and col != "timestamp"
    ]
