"""Prometheus access layer with defensive parsing and typed metric helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any

import requests


@dataclass
class TelemetrySnapshot:
    timestamp: str
    cpu_usage: float | None
    memory_usage: float | None
    request_rate: float | None
    p95_latency_ms: float | None
    error_rate: float | None
    replica_count: int | None


class PrometheusQueryError(RuntimeError):
    """Raised when Prometheus query request fails."""


class PrometheusClient:
    def __init__(self, base_url: str, timeout: int = 10, service_name: str = "target-service") -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.service_name = service_name

    def query(self, promql: str) -> dict[str, Any]:
        try:
            response = requests.get(
                f"{self.base_url}/api/v1/query",
                params={"query": promql},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise PrometheusQueryError(str(exc)) from exc

        if payload.get("status") != "success":
            raise PrometheusQueryError(f"Prometheus query failed: {payload}")
        return payload

    def query_range(self, promql: str, start: str, end: str, step: str) -> dict[str, Any]:
        try:
            response = requests.get(
                f"{self.base_url}/api/v1/query_range",
                params={"query": promql, "start": start, "end": end, "step": step},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise PrometheusQueryError(str(exc)) from exc

        if payload.get("status") != "success":
            raise PrometheusQueryError(f"Prometheus range query failed: {payload}")
        return payload

    @staticmethod
    def _first_result(payload: dict[str, Any]) -> dict[str, Any] | None:
        data = payload.get("data", {})
        result = data.get("result")
        if not isinstance(result, list) or not result:
            return None
        first = result[0]
        return first if isinstance(first, dict) else None

    @classmethod
    def parse_instant_value(cls, payload: dict[str, Any]) -> float | None:
        first = cls._first_result(payload)
        if not first:
            return None

        raw = first.get("value")
        if not isinstance(raw, list) or len(raw) != 2:
            return None

        try:
            value = float(raw[1])
        except (TypeError, ValueError):
            return None
        # Prometheus returns the literal strings "NaN" / "+Inf" for undefined results
        # (e.g. histogram_quantile with zero traffic). Treat them as "no data".
        return value if math.isfinite(value) else None

    @staticmethod
    def parse_range_values(payload: dict[str, Any]) -> list[tuple[float, float]]:
        data = payload.get("data", {})
        result = data.get("result")
        if not isinstance(result, list) or not result:
            return []

        series = result[0] if isinstance(result[0], dict) else {}
        values = series.get("values", [])
        if not isinstance(values, list):
            return []

        parsed: list[tuple[float, float]] = []
        for sample in values:
            if not isinstance(sample, list) or len(sample) != 2:
                continue
            try:
                ts, value = float(sample[0]), float(sample[1])
            except (TypeError, ValueError):
                continue
            if math.isfinite(ts) and math.isfinite(value):
                parsed.append((ts, value))
        return parsed

    def _safe_query_value(self, promql: str) -> float | None:
        try:
            payload = self.query(promql)
        except PrometheusQueryError:
            return None
        return self.parse_instant_value(payload)

    def _service_selector(self) -> str:
        return f'{{container_label_com_docker_compose_service="{self.service_name}"}}'

    def get_cpu_usage(self) -> float | None:
        """Aggregate CPU of the service in CORES (sum over all replicas).

        Returns None when cAdvisor has no per-service data. The optimizer then falls back to
        `docker stats` for the same service. There is deliberately no host-wide fallback: a
        number that includes Prometheus, cAdvisor and Locust must not drive scaling.
        """
        selector = self._service_selector()
        return self._safe_query_value(f"sum(rate(container_cpu_usage_seconds_total{selector}[1m]))")

    def get_memory_usage(self) -> float | None:
        """Aggregate memory used / aggregate memory limit for the service's replicas.

        Containers without a limit report 0 (cgroup v2) or ~9.2e18 (cgroup v1) for
        `container_spec_memory_limit_bytes`; dividing by that gave nonsense ratios, so only
        real limits are used. Without any limit the ratio falls back to share of host memory.
        """
        selector = self._service_selector()
        limited = (
            f"sum(container_memory_usage_bytes{selector}) / "
            f"sum((container_spec_memory_limit_bytes{selector} > 0) < 1e15)"
        )
        value = self._safe_query_value(limited)
        if value is not None:
            return value
        host_share = f"sum(container_memory_usage_bytes{selector}) / scalar(sum(machine_memory_bytes))"
        return self._safe_query_value(host_share)

    def get_request_rate(self) -> float | None:
        promql = 'sum(rate(http_requests_total{path="/work"}[1m]))'
        return self._safe_query_value(promql)

    def get_p95_latency_ms(self) -> float | None:
        promql = (
            'histogram_quantile(0.95, '
            'sum by (le) (rate(http_request_duration_seconds_bucket{path="/work"}[5m]))) * 1000'
        )
        return self._safe_query_value(promql)

    def get_error_rate(self) -> float | None:
        # `sum()` over zero matching series is an EMPTY vector, not 0. The target service
        # only creates a 5xx series once an error happens, so without `or vector(0)` this
        # query returned no data on every healthy system.
        promql = (
            '(sum(rate(http_requests_total{status=~"5.."}[5m])) or vector(0)) '
            '/ clamp_min(sum(rate(http_requests_total[5m])), 1)'
        )
        return self._safe_query_value(promql)

    def get_replica_count(self) -> int | None:
        promql = f'count(up{{job="{self.service_name}"}} == 1)'
        value = self._safe_query_value(promql)
        return int(value) if value is not None else None

    def collect_snapshot(self) -> TelemetrySnapshot:
        return TelemetrySnapshot(
            timestamp=datetime.now(timezone.utc).isoformat(),
            cpu_usage=self.get_cpu_usage(),
            memory_usage=self.get_memory_usage(),
            request_rate=self.get_request_rate(),
            p95_latency_ms=self.get_p95_latency_ms(),
            error_rate=self.get_error_rate(),
            replica_count=self.get_replica_count(),
        )
