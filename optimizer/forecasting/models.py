"""Forecasting model implementations."""

from pathlib import Path

import joblib
from sklearn.ensemble import RandomForestRegressor


def train_random_forest(X, y, output_path: str | Path):
    """Train and persist the initial forecasting model."""
    model = RandomForestRegressor(n_estimators=200, random_state=42, n_jobs=-1)
    model.fit(X, y)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, output)
    return model


def load_model(path: str | Path):
    return joblib.load(path)


# TODO: Add persistence baseline, chronological evaluation, and optional LSTM model.
