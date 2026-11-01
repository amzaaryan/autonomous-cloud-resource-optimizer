"""Optimizer worker entry point with telemetry, forecasting and scaling control loop."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
from pathlib import Path
import signal
import threading
import time
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from optimizer.analytics import per_replica_cpu, update_cost_totals
from optimizer.config import Settings
from optimizer.control import read_control
from optimizer.forecasting.autotrain import AutoTrainer
from optimizer.forecasting.engine import ForecastEngine
from optimizer.policy.combined import effective_forecast, select_decision
from optimizer.policy.policies import PolicyConfig, PolicyInput
from optimizer.prometheus_client import PrometheusClient, PrometheusQueryError, TelemetrySnapshot
from optimizer.scaling.docker_scaler import DockerScaler


LOGGER = logging.getLogger(__name__)

LOOP_SUCCESS = Counter("optimizer_loop_success_total", "Successful optimizer loops")
LOOP_FAILURE = Counter("optimizer_loop_failure_total", "Failed optimizer loops")
SCALING_ACTIONS = Counter("optimizer_scaling_actions_total", "Scaling actions", ["action", "policy"])
FORECAST_GAUGE = Gauge("optimizer_forecast_rps", "Forecasted request rate", ["horizon_minute"])
POLICY_STATE = Gauge("optimizer_policy_state", "1 predictive, 0 reactive")
ESTIMATED_COST = Gauge("optimizer_estimated_cost_total", "Estimated cumulative cost")
ESTIMATED_SAVINGS = Gauge("optimizer_estimated_savings_total", "Estimated cumulative savings")
CURRENT_REPLICAS = Gauge("optimizer_current_replicas", "Current known replica count")
RESOURCE_CPU = Gauge(
    "optimizer_target_cpu_usage",
    "Per-replica CPU utilisation of target-service: aggregate cores / (replicas * per-replica CPU limit)",
)
RESOURCE_MEMORY = Gauge("optimizer_target_memory_usage", "Aggregate target-service memory used / memory limit")


class RuntimeStatus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.last_loop_at: str | None = None
        self.last_error: str | None = None
        self.policy: str = "reactive"
        self.policy_mode: str = "auto"

    def update(
        self,
        *,
        loop_at: str | None = None,
        error: str | None = None,
        policy: str | None = None,
        policy_mode: str | None = None,
    ) -> None:
        with self._lock:
            if loop_at is not None:
                self.last_loop_at = loop_at
            self.last_error = error
            if policy is not None:
                self.policy = policy
            if policy_mode is not None:
                self.policy_mode = policy_mode

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "ok" if self.last_error is None else "degraded",
                "last_loop_at": self.last_loop_at,
                "last_error": self.last_error,
                "policy": self.policy,
                "policy_mode": self.policy_mode,
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
        predictive_max_step=settings.predictive_max_step,
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
    scaler = DockerScaler(
        settings.target_service_name,
        dry_run=settings.dry_run,
        mode=settings.scaling_mode,
        port=settings.target_service_port,
        health_path=settings.target_health_path,
        ready_timeout_seconds=settings.replica_ready_timeout_seconds,
        drain_seconds=settings.scale_down_drain_seconds,
    )
    forecaster = ForecastEngine.from_settings(settings)
    policy_config = build_policy_config(settings)

    if settings.auto_train:
        AutoTrainer(
            telemetry_path=settings.telemetry_path,
            model_dir=settings.model_dir,
            horizons=settings.forecast_horizons_minutes,
            interval_seconds=settings.telemetry_interval_seconds,
            status_path=settings.model_status_path,
            report_path=settings.model_report_path,
            min_rows=settings.auto_train_min_rows,
            retrain_seconds=settings.auto_train_interval_minutes * 60.0,
            require_improvement=settings.auto_train_require_improvement,
            engine=forecaster,
        ).start(stop_event)

    history: deque[dict[str, Any]] = deque(maxlen=512)
    last_scaled_at: datetime | None = None
    last_tick = time.time()
    cumulative_cost = 0.0
    cumulative_savings = 0.0

    LOGGER.info(
        "Optimizer started. dry_run=%s scaling_mode=%s models_loaded=%s capacity=%.1f rps/replica",
        settings.dry_run,
        settings.scaling_mode,
        sorted(forecaster.models),
        settings.rps_per_replica_capacity,
    )

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
                current_replicas = int(replicas)
                CURRENT_REPLICAS.set(float(current_replicas))

                # Prometheus/cAdvisor first; `docker stats` only when it has nothing for the service.
                cores = data.get("cpu_usage")
                memory = data.get("memory_usage")
                if cores is None or memory is None:
                    docker_cores, docker_memory = scaler.get_resource_usage()
                    cores = docker_cores if cores is None else cores
                    memory = docker_memory if memory is None else memory
                data["cpu_cores"] = cores
                data["cpu_usage"] = per_replica_cpu(cores, current_replicas, settings.replica_cpu_limit_cores)
                data["memory_usage"] = memory
                if data["cpu_usage"] is not None:
                    RESOURCE_CPU.set(float(data["cpu_usage"]))
                if memory is not None:
                    RESOURCE_MEMORY.set(float(memory))

                policy_mode = read_control(settings.control_path)["policy_mode"]
                forecasts, confidence = forecaster.forecast(list(history) + [data])
                forecasts, confidence, forecast_source = effective_forecast(
                    policy_mode, forecasts, confidence, data.get("request_rate")
                )
                for horizon, value in forecasts.items():
                    FORECAST_GAUGE.labels(horizon_minute=str(horizon)).set(value)

                now_tick = time.time()
                elapsed_minutes = max(0.0, (now_tick - last_tick) / 60.0)
                last_tick = now_tick

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
                data["forecast_source"] = forecast_source
                data["policy_mode"] = policy_mode
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
                decision = select_decision(
                    policy_mode,
                    policy_input,
                    policy_config,
                    now=loop_time,
                    last_scaled_at=last_scaled_at,
                    forecast_confidence=confidence,
                    capacity_rps_per_replica=settings.rps_per_replica_capacity,
                    target_utilization=settings.target_utilization,
                    scale_down_utilization=settings.scale_down_utilization,
                )
                POLICY_STATE.set(1 if decision.policy == "predictive" else 0)
                status.update(loop_at=loop_iso, error=None, policy=decision.policy, policy_mode=policy_mode)

                if decision.action in {"scale_up", "scale_down"} and decision.desired_replicas != current_replicas:
                    scaled = scaler.set_replicas(decision.desired_replicas)
                    event = {
                        "timestamp": loop_iso,
                        "action": decision.action,
                        "from_replicas": current_replicas,
                        "to_replicas": decision.desired_replicas,
                        "reason": decision.reason,
                        "policy": decision.policy,
                        "policy_mode": policy_mode,
                        "forecast_source": forecast_source,
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
