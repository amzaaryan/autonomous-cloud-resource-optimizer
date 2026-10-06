"""Feature engineering for forecasting models."""

import pandas as pd


def build_features(frame: pd.DataFrame, lags: tuple[int, ...] = (1, 2, 4)) -> pd.DataFrame:
    """Build lagged and rolling features.

    TODO:
    - Add lags for CPU, memory, request rate, and latency.
    - Add rolling mean, max, standard deviation, and slope features.
    - Add future target columns for 1, 3, and 5 minute horizons.
    - Keep transformations reproducible for online inference.
    """
    result = frame.copy()
    for lag in lags:
        for column in ("cpu_usage", "memory_usage", "request_rate"):
            if column in result:
                result[f"{column}_lag_{lag}"] = result[column].shift(lag)
    return result.dropna()
