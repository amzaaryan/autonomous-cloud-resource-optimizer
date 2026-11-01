"""Feature engineering for forecasting models."""

from __future__ import annotations

import numpy as np
import pandas as pd


BASE_COLUMNS = ("cpu_usage", "memory_usage", "request_rate", "p95_latency_ms", "error_rate", "replica_count")

DEFAULT_INTERVAL_SECONDS = 15.0

# Metrics where "no sample" has a natural value: no traffic means 0 rps, no errors,
# and an undefined (NaN) p95 latency. Treating these as 0 keeps idle periods usable
# for training instead of silently discarding every row that contains them.
_ZERO_FILL = ("request_rate", "error_rate", "p95_latency_ms")
# Gauges that persist between samples: carry the last known value forward.
_FORWARD_FILL = {"cpu_usage": 0.0, "memory_usage": 0.0, "replica_count": 1.0}


def infer_interval_seconds(frame: pd.DataFrame, default: float = DEFAULT_INTERVAL_SECONDS) -> float:
    """Median spacing between telemetry rows, falling back to ``default``."""
    if "timestamp" not in frame.columns or len(frame) < 3:
        return float(default)
    stamps = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce").dropna()
    if len(stamps) < 3:
        return float(default)
    deltas = stamps.diff().dropna().dt.total_seconds()
    deltas = deltas[deltas > 0]
    if deltas.empty:
        return float(default)
    return float(deltas.median())


def horizon_steps(horizon_minutes: int, interval_seconds: float) -> int:
    """Number of telemetry rows that correspond to ``horizon_minutes`` of wall-clock time."""
    return max(1, int(round(horizon_minutes * 60.0 / max(interval_seconds, 1e-9))))


def _clean_base_columns(result: pd.DataFrame) -> pd.DataFrame:
    for column in BASE_COLUMNS:
        if column not in result:
            result[column] = np.nan
        result[column] = pd.to_numeric(result[column], errors="coerce").replace([np.inf, -np.inf], np.nan)

    for column in _ZERO_FILL:
        result[column] = result[column].fillna(0.0)
    for column, fallback in _FORWARD_FILL.items():
        result[column] = result[column].ffill().fillna(fallback)
    return result


def build_features(
    frame: pd.DataFrame,
    lags: tuple[int, ...] = (1, 2, 4),
    rolling_windows: tuple[int, ...] = (3, 5),
    horizons: tuple[int, ...] = (1, 3, 5),
    with_targets: bool = True,
    interval_seconds: float | None = None,
) -> pd.DataFrame:
    """Build lagged, rolling, and rate-of-change features.

    ``horizons`` are in MINUTES. Targets are shifted by ``horizon * 60 / interval_seconds``
    rows so that, at a 15 s sampling interval, the 1/3/5 minute targets are 4/12/20 rows
    ahead. ``interval_seconds`` defaults to the median spacing of the ``timestamp`` column.
    Lags and rolling windows are expressed in rows (samples).
    """
    keep_cols = []
    if "timestamp" in frame.columns:
        keep_cols.append("timestamp")
    keep_cols.extend(column for column in BASE_COLUMNS if column in frame.columns)
    result = _clean_base_columns(frame[keep_cols].copy())

    if interval_seconds is None:
        interval_seconds = infer_interval_seconds(frame)

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
            steps = horizon_steps(horizon, interval_seconds)
            result[f"target_request_rate_t_plus_{horizon}"] = result["request_rate"].shift(-steps)

    # Remaining NaNs are only the lag/rolling warm-up at the start and, when targets are
    # requested, the unknown future at the end of the series.
    return result.dropna().reset_index(drop=True)


def feature_columns(frame: pd.DataFrame) -> list[str]:
    return [
        col
        for col in frame.columns
        if not col.startswith("target_request_rate_t_plus_") and col != "timestamp"
    ]
