"""Background retraining on the telemetry the optimizer itself collects.

Every ``retrain_seconds`` it rebuilds the Random Forests from ``telemetry_path`` and hot-reloads them
into the running :class:`ForecastEngine`. A model is **deployed only if it beats the persistence
baseline ("rate stays the same") on held-out rows**; otherwise that horizon is left without a model,
so a worse-than-baseline forest can never drive scaling. The outcome is written to
``status_path`` for the dashboard.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Any

import pandas as pd

from optimizer.forecasting.train import train_models


LOGGER = logging.getLogger(__name__)

MIN_SEGMENT_ROWS = 40  # shorter stretches of continuous telemetry are too small to build lag features from


def split_segments(frame: pd.DataFrame, interval_seconds: float, gap_factor: float = 3.0) -> list[pd.DataFrame]:
    """Split telemetry wherever the optimizer was off (a time gap), so lags never span a restart."""
    if frame.empty:
        return []
    if "timestamp" not in frame.columns:
        return [frame]
    stamps = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame = frame.assign(timestamp=stamps).dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    gaps = frame["timestamp"].diff().dt.total_seconds().fillna(0) > interval_seconds * gap_factor
    group = gaps.cumsum()
    return [part.reset_index(drop=True) for _, part in frame.groupby(group, sort=True)]


class AutoTrainer:
    def __init__(
        self,
        *,
        telemetry_path: str | Path,
        model_dir: str | Path,
        horizons: tuple[int, ...],
        interval_seconds: float,
        status_path: str | Path,
        report_path: str | Path | None = None,
        min_rows: int = 80,
        retrain_seconds: float = 600.0,
        require_improvement: bool = True,
        max_rows: int = 6000,
        engine: Any = None,
    ) -> None:
        self.telemetry_path = Path(telemetry_path)
        self.model_dir = Path(model_dir)
        self.horizons = tuple(horizons)
        self.interval_seconds = float(interval_seconds)
        self.status_path = Path(status_path)
        self.report_path = Path(report_path) if report_path else None
        self.min_rows = min_rows
        self.retrain_seconds = retrain_seconds
        self.require_improvement = require_improvement
        self.max_rows = max_rows
        self.engine = engine

    # ---- status ------------------------------------------------------------------------

    def _write_status(self, **fields: Any) -> dict[str, Any]:
        status = {"updated_at": datetime.now(timezone.utc).isoformat(), "min_rows": self.min_rows, **fields}
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.status_path.with_name(self.status_path.name + ".tmp")
            tmp.write_text(json.dumps(status, default=str), encoding="utf-8")
            os.replace(tmp, self.status_path)
        except OSError:
            LOGGER.warning("Could not write model status to %s", self.status_path)
        return status

    # ---- one training attempt ----------------------------------------------------------

    def run_once(self) -> dict[str, Any]:
        try:
            frame = pd.read_json(self.telemetry_path, lines=True) if self.telemetry_path.exists() else pd.DataFrame()
        except ValueError:
            frame = pd.DataFrame()
        frame = frame.tail(self.max_rows)
        segments = [s for s in split_segments(frame, self.interval_seconds) if len(s) >= MIN_SEGMENT_ROWS]
        usable = sum(len(s) for s in segments)

        if usable < self.min_rows:
            return self._write_status(
                state="collecting",
                rows=usable,
                message=f"Collecting telemetry: {usable}/{self.min_rows} rows "
                f"(~{max(0, self.min_rows - usable) * self.interval_seconds / 60:.0f} more minutes of traffic needed)",
            )

        staging = self.model_dir / ".staging"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            rows, metadata = train_models(
                segments,
                horizons=self.horizons,
                interval_seconds=self.interval_seconds,
                model_dir=staging,
                report_path=self.report_path,
                data_source="live telemetry (auto-train)",
            )
        except ValueError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            return self._write_status(state="collecting", rows=usable, message=f"Not enough usable data yet: {exc}")
        except Exception as exc:  # noqa: BLE001 - never let training take the optimizer down
            shutil.rmtree(staging, ignore_errors=True)
            LOGGER.exception("Auto-train failed")
            return self._write_status(state="error", rows=usable, message=f"Training failed: {exc}")

        results, deployed = [], []
        self.model_dir.mkdir(parents=True, exist_ok=True)
        for row in rows:
            horizon = int(row["horizon_min"])
            improvement = row.get("mae_improvement_vs_baseline_pct")
            ok = (improvement is not None and improvement > 0) or not self.require_improvement
            target = self.model_dir / f"rf_request_rate_h{horizon}.joblib"
            if ok:
                shutil.copyfile(staging / target.name, target)
                deployed.append(horizon)
            elif target.exists():
                target.unlink()  # a stale model that no longer passes must not keep driving decisions
            results.append(
                {
                    "horizon_min": horizon,
                    "mae": row.get("mae"),
                    "baseline_mae": row.get("baseline_mae"),
                    "improvement_pct": improvement,
                    "deployed": ok,
                }
            )
        if deployed:
            (self.model_dir / "metadata.json").write_text(
                json.dumps({**metadata, "deployed_horizons": deployed}, indent=2), encoding="utf-8"
            )
        shutil.rmtree(staging, ignore_errors=True)
        if self.engine is not None:
            self.engine.reload()

        if deployed:
            message = f"Random forest deployed for horizons {deployed} min (beats current-rate baseline on held-out rows)"
        else:
            message = "Random forest NOT deployed: it did not beat the current-rate baseline on held-out rows"
        LOGGER.info("Auto-train: %s", message)
        return self._write_status(
            state="deployed" if deployed else "rejected",
            rows=usable,
            message=message,
            deployed_horizons=deployed,
            results=results,
            trained_at=datetime.now(timezone.utc).isoformat(),
            next_attempt_at=(datetime.now(timezone.utc) + timedelta(seconds=self.retrain_seconds)).isoformat(),
        )

    # ---- scheduling --------------------------------------------------------------------

    def run_forever(self, stop_event: threading.Event, poll_seconds: float = 60.0) -> None:
        """Check every ``poll_seconds``; retrain when enough data exists and the interval has elapsed."""
        last_trained = 0.0

        while not stop_event.is_set():
            now = time.monotonic()
            try:
                if last_trained == 0.0 or now - last_trained >= self.retrain_seconds or self._collecting():
                    status = self.run_once()
                    if status.get("state") in {"deployed", "rejected", "error"}:
                        last_trained = time.monotonic()
            except Exception:  # noqa: BLE001
                LOGGER.exception("Auto-train scheduler error")
            stop_event.wait(poll_seconds)

    def _collecting(self) -> bool:
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8")).get("state") == "collecting"
        except (OSError, ValueError):
            return True

    def start(self, stop_event: threading.Event) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, args=(stop_event,), name="auto-trainer", daemon=True)
        thread.start()
        return thread
