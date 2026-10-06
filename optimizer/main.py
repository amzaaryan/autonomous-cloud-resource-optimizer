"""Optimizer worker entry point with telemetry, forecasting and scaling control loop."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import signal
import threading
import time
from typing import Any

import pandas as pd
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from sklearn.exceptions import NotFittedError

from optimizer.analytics import update_cost_totals
from optimizer.config import Settings
from optimizer.feature_engineering import build_features, feature_columns
from optimizer.forecasting.models import load_model
from optimizer.policy.policies import PolicyConfig, PolicyInput, predictive_decision, reactive_decision
from optimizer.prometheus_client import PrometheusClient, PrometheusQueryError, TelemetrySnapshot
from optimizer.scaling.docker_scaler import DockerScaler

from http.server import BaseHTTPRequestHandler, HTTPServer


LOGGER = logging.getLogger(__name__)

LOOP_SUCCESS = Counter("optimizer_loop_success_total", "Successful optimizer loops")
LOOP_FAILURE = Counter("optimizer_loop_failure_total", "Failed optimizer loops")
SCALING_ACTIONS = Counter("optimizer_scaling_actions_total", "Scaling actions", ["action", "policy"]) 
FORECAST_GAUGE = Gauge("optimizer_forecast_rps", "Forecasted request rate", ["horizon_minute"])
POLICY_STATE = Gauge("optimizer_policy_state", "1 predictive, 0 reactive")
ESTIMATED_COST = Gauge("optimizer_estimated_cost_total", "Estimated cumulative cost")
ESTIMATED_SAVINGS = Gauge("optimizer_estimated_savings_total", "Estimated cumulative savings")
CURRENT_REPLICAS = Gauge("optimizer_current_replicas", "Current known replica count")


class RuntimeStatus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.last_loop_at: str | None = None
        self.last_error: str | None = None
        self.policy: str = "reactive"

    def update(self, *, loop_at: str | None = None, error: str | None = None, policy: str | None = None) -> None:
        with self._lock:
            if loop_at is not None:
                self.last_loop_at = loop_at
            self.last_error = error
            if policy is not None:
                self.policy = policy

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "ok" if self.last_error is None else "degraded",
                "last_loop_at": self.last_loop_at,
                "last_error": self.last_error,
                "policy": self.policy,
            }


class OptimizerHttpHandler(BaseHTTPRequestHandler):
    status_provider: RuntimeStatus | None = None

    def _send_json(self, payload: dict[str, Any], status_code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            status = self.status_provider.snapshot() if self.status_provider else {"status": "unknown"}
            self._send_json(status, 200)
            return

        if self.path == "/metrics":
            payload = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self._send_json({"detail": "not found"}, 404)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


class ForecastEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.models: dict[int, Any] = {}
        self._load_models()

    def _load_models(self) -> None:
        model_dir = Path(self.settings.model_dir)
        for horizon in self.settings.forecast_horizons_minutes:
            path = model_dir / f"rf_request_rate_h{horizon}.joblib"
            if path.exists():
                try:
                    self.models[horizon] = load_model(path)
                except Exception:  # noqa: BLE001
                    LOGGER.exception("Failed loading model %s", path)

    def forecast(self, history: list[dict[str, Any]]) -> tuple[dict[int, float], float | None]:
        if not history:
            return {}, None

        current_rps = history[-1].get("request_rate")
        if current_rps is None:
            return {}, None

        frame = pd.DataFrame(history)
        engineered = build_features(frame, with_targets=False)
        if engineered.empty:
            return {h: float(current_rps) for h in self.settings.forecast_horizons_minutes}, 0.5

        latest = engineered.tail(1)
        cols = feature_columns(latest)

        result: dict[int, float] = {}
        model_predictions = 0
        for horizon in self.settings.forecast_horizons_minutes:
            model = self.models.get(horizon)
            if model is not None:
                try:
                    prediction = float(model.predict(latest[cols])[0])
                    result[horizon] = max(0.0, prediction)
                    model_predictions += 1
                    continue
                except (ValueError, NotFittedError):
                    LOGGER.warning("Model inference failed for horizon %s", horizon)
            result[horizon] = float(current_rps)

        confidence = model_predictions / max(1, len(self.settings.forecast_horizons_minutes))
        return result, confidence


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def append_jsonl(path: str, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def start_http_server(host: str, port: int, status: RuntimeStatus) -> HTTPServer:
    OptimizerHttpHandler.status_provider = status
    server = HTTPServer((host, port), OptimizerHttpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def build_policy_config(settings: Settings) -> PolicyConfig:
    return PolicyConfig(
        min_replicas=settings.min_replicas,
        max_replicas=settings.max_replicas,
        scale_step=settings.scale_step,
        cooldown_seconds=settings.cooldown_seconds,
        cpu_scale_up_threshold=settings.cpu_scale_up_threshold,
        cpu_scale_down_threshold=settings.cpu_scale_down_threshold,
        memory_scale_up_threshold=settings.memory_scale_up_threshold,
        latency_sla_ms=settings.latency_sla_ms,
        error_rate_sla=settings.error_rate_sla,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    status = RuntimeStatus()
    server = start_http_server(settings.optimizer_host, settings.optimizer_port, status)

    stop_event = threading.Event()

    def _stop_handler(*_args: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)

    telemetry_client = PrometheusClient(settings.prometheus_url, service_name=settings.target_service_name)
    scaler = DockerScaler(settings.target_service_name, dry_run=settings.dry_run, mode=settings.scaling_mode)
    forecaster = ForecastEngine(settings)
    policy_config = build_policy_config(settings)

    history: deque[dict[str, Any]] = deque(maxlen=512)
    last_scaled_at: datetime | None = None
    last_tick = time.time()
    cumulative_cost = 0.0
    cumulative_savings = 0.0

    LOGGER.info("Optimizer started. dry_run=%s scaling_mode=%s", settings.dry_run, settings.scaling_mode)

    try:
        while not stop_event.is_set():
            loop_time = datetime.now(timezone.utc)
            loop_iso = loop_time.isoformat()

            try:
                snapshot: TelemetrySnapshot = telemetry_client.collect_snapshot()
                data = asdict(snapshot)

                replicas = data.get("replica_count")
                if replicas is None:
                    replicas = scaler.get_replica_count()
                    data["replica_count"] = replicas
                CURRENT_REPLICAS.set(float(replicas))

                forecasts, confidence = forecaster.forecast(list(history) + [data])
                for horizon, value in forecasts.items():
                    FORECAST_GAUGE.labels(horizon_minute=str(horizon)).set(value)

                now_tick = time.time()
                elapsed_minutes = max(0.0, (now_tick - last_tick) / 60.0)
                last_tick = now_tick

                current_replicas = int(replicas)
                baseline_replicas = max(settings.savings_baseline_replicas, settings.min_replicas)
                cumulative_cost, cumulative_savings = update_cost_totals(
                    cumulative_cost=cumulative_cost,
                    cumulative_savings=cumulative_savings,
                    elapsed_minutes=elapsed_minutes,
                    replicas=current_replicas,
                    baseline_replicas=baseline_replicas,
                    cost_per_replica_minute=settings.cost_per_replica_minute,
                )
                ESTIMATED_COST.set(cumulative_cost)
                ESTIMATED_SAVINGS.set(cumulative_savings)
                data["forecasts"] = forecasts
                data["forecast_confidence"] = confidence
                data["estimated_cost_total"] = cumulative_cost
                data["estimated_savings_total"] = cumulative_savings
                history.append(data)
                append_jsonl(settings.telemetry_path, data)

                policy_input = PolicyInput(
                    cpu=data.get("cpu_usage"),
                    memory=data.get("memory_usage"),
                    p95_latency_ms=data.get("p95_latency_ms"),
                    error_rate=data.get("error_rate"),
                    current_replicas=current_replicas,
                    predicted_request_rate=forecasts,
                )

                predictive = predictive_decision(
                    policy_input,
                    policy_config,
                    now=loop_time,
                    last_scaled_at=last_scaled_at,
                    forecast_confidence=confidence,
                    high_load_rps_threshold=3.0,
                    low_load_rps_threshold=0.8,
                )

                if predictive.action == "no_action":
                    decision = reactive_decision(policy_input, policy_config, now=loop_time, last_scaled_at=last_scaled_at)
                    status_policy = "reactive"
                    POLICY_STATE.set(0)
                else:
                    decision = predictive
                    status_policy = "predictive"
                    POLICY_STATE.set(1)

                status.update(loop_at=loop_iso, error=None, policy=status_policy)

                if decision.action in {"scale_up", "scale_down"} and decision.desired_replicas != current_replicas:
                    scaled = scaler.set_replicas(decision.desired_replicas)
                    event = {
                        "timestamp": loop_iso,
                        "action": decision.action,
                        "from_replicas": current_replicas,
                        "to_replicas": decision.desired_replicas,
                        "reason": decision.reason,
                        "policy": decision.policy,
                        "dry_run": settings.dry_run,
                        "success": scaled,
                        "forecast_confidence": confidence,
                        "forecasts": forecasts,
                    }
                    append_jsonl(settings.scaling_events_path, event)
                    SCALING_ACTIONS.labels(action=decision.action, policy=decision.policy).inc()
                    if scaled:
                        last_scaled_at = loop_time

                LOOP_SUCCESS.inc()
            except (PrometheusQueryError, ValueError) as exc:
                LOOP_FAILURE.inc()
                status.update(loop_at=loop_iso, error=str(exc))
                LOGGER.warning("Telemetry unavailable or malformed: %s", exc)
            except Exception as exc:  # noqa: BLE001
                LOOP_FAILURE.inc()
                status.update(loop_at=loop_iso, error=str(exc))
                LOGGER.exception("Optimizer loop failure: %s", exc)

            stop_event.wait(settings.telemetry_interval_seconds)
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
