"""Inference-time forecasting engine (shared by the live optimizer and the simulator)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
from sklearn.exceptions import NotFittedError

from optimizer.feature_engineering import build_features, feature_columns
from optimizer.forecasting.models import load_model


LOGGER = logging.getLogger(__name__)


class ForecastEngine:
    """Predicts request-rate at each horizon (minutes) from recent telemetry history.

    Returns ``(forecasts, confidence)`` where confidence is the fraction of horizons
    served by a trained model. With no model the forecast is the current request rate
    and confidence is 0.0, which makes the predictive policy defer to the reactive one.
    """

    def __init__(
        self,
        model_dir: str | Path,
        horizons_minutes: tuple[int, ...],
        interval_seconds: float,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.horizons = tuple(horizons_minutes)
        self.interval_seconds = float(interval_seconds)
        self.models: dict[int, Any] = {}
        self.feature_order: list[str] | None = None
        self.target_mode = "absolute"
        self._load_models()

    @classmethod
    def from_settings(cls, settings) -> "ForecastEngine":
        return cls(settings.model_dir, settings.forecast_horizons_minutes, settings.telemetry_interval_seconds)

    def _load_metadata(self) -> dict[str, Any]:
        path = self.model_dir / "metadata.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            LOGGER.warning("Could not read model metadata %s", path)
            return {}

    def _load_models(self) -> None:
        metadata = self._load_metadata()
        trained_interval = metadata.get("interval_seconds")
        if trained_interval:
            ratio = float(trained_interval) / self.interval_seconds
            if not 0.5 <= ratio <= 2.0:
                # Models were trained on a different sampling rate: their lag/rolling
                # features and horizon targets would mean something else. Do not use them.
                LOGGER.warning(
                    "Models trained at %.1fs sampling but optimizer samples every %.1fs; "
                    "ignoring models (retrain with the current interval).",
                    float(trained_interval),
                    self.interval_seconds,
                )
                return
        else:
            LOGGER.warning("No interval_seconds in model metadata; horizons may be mis-scaled. Retrain recommended.")

        self.feature_order = metadata.get("feature_columns")
        self.target_mode = metadata.get("target_mode", "absolute")  # old models predate the field

        for horizon in self.horizons:
            path = self.model_dir / f"rf_request_rate_h{horizon}.joblib"
            if not path.exists():
                continue
            try:
                self.models[horizon] = load_model(path)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Failed loading model %s", path)

    def forecast(self, history: list[dict[str, Any]]) -> tuple[dict[int, float], float | None]:
        if not history:
            return {}, None

        current_rps = history[-1].get("request_rate")
        if current_rps is None:
            current_rps = 0.0
        fallback = {h: float(current_rps) for h in self.horizons}

        if not self.models:
            return fallback, 0.0

        frame = pd.DataFrame(history)
        engineered = build_features(frame, with_targets=False, interval_seconds=self.interval_seconds)
        if engineered.empty:
            return fallback, 0.0  # not enough history yet for the lag/rolling features

        latest = engineered.tail(1)
        columns = self.feature_order if self.feature_order else feature_columns(latest)
        columns = [c for c in columns if c in latest.columns]

        result: dict[int, float] = {}
        model_predictions = 0
        for horizon in self.horizons:
            model = self.models.get(horizon)
            if model is not None:
                try:
                    predicted = float(model.predict(latest[columns])[0])
                    if self.target_mode == "delta":
                        predicted += float(current_rps)
                    result[horizon] = max(0.0, predicted)
                    model_predictions += 1
                    continue
                except (ValueError, NotFittedError, KeyError):
                    LOGGER.warning("Model inference failed for horizon %s", horizon)
            result[horizon] = float(current_rps)

        return result, model_predictions / max(1, len(self.horizons))
