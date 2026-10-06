"""Prometheus access layer.

TODO: Implement query(), query_range(), and typed metric helpers.
"""

from typing import Any
import requests


class PrometheusClient:
    def __init__(self, base_url: str, timeout: int = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def query(self, promql: str) -> dict[str, Any]:
        response = requests.get(
            f"{self.base_url}/api/v1/query",
            params={"query": promql},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def query_range(self, promql: str, start: str, end: str, step: str) -> dict[str, Any]:
        response = requests.get(
            f"{self.base_url}/api/v1/query_range",
            params={"query": promql, "start": start, "end": end, "step": step},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    # TODO: Add get_cpu_usage(), get_memory_usage(), get_request_rate(), and get_p95_latency().
