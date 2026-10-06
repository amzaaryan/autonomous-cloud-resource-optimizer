"""Streamlit dashboard for optimizer telemetry and decisions."""

from __future__ import annotations

from pathlib import Path
import json

import pandas as pd
import plotly.express as px
import requests
import streamlit as st


st.set_page_config(page_title="Cloud Resource Optimizer", layout="wide")
st.title("Autonomous Cloud Resource Optimizer")

PROMETHEUS_URL = st.sidebar.text_input("Prometheus URL", value="http://prometheus:9090")
TELEMETRY_PATH = st.sidebar.text_input("Telemetry file", value="data/runtime/telemetry_snapshots.jsonl")
EVENTS_PATH = st.sidebar.text_input("Scaling events file", value="data/runtime/scaling_events.jsonl")
REFRESH_SECONDS = st.sidebar.slider("Auto refresh (seconds)", min_value=5, max_value=60, value=15)


def read_jsonl(path: str) -> pd.DataFrame:
    target = Path(path)
    if not target.exists():
        return pd.DataFrame()
    return pd.read_json(target, lines=True)


def prom_query(promql: str) -> float | None:
    try:
        response = requests.get(
            f"{PROMETHEUS_URL.rstrip('/')}/api/v1/query",
            params={"query": promql},
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("data", {}).get("result", [])
        if not result:
            return None
        value = result[0].get("value", [None, None])[1]
        return float(value)
    except Exception:  # noqa: BLE001
        return None


def get_live_metrics() -> dict[str, float | None]:
    return {
        "cpu_usage": prom_query('sum(rate(container_cpu_usage_seconds_total{container_label_com_docker_compose_service="target-service"}[1m]))'),
        "memory_usage": prom_query('sum(container_memory_usage_bytes{container_label_com_docker_compose_service="target-service"}) / clamp_min(sum(container_spec_memory_limit_bytes{container_label_com_docker_compose_service="target-service"}), 1)'),
        "request_rate": prom_query('sum(rate(http_requests_total{path="/work"}[1m]))'),
        "p95_latency_ms": prom_query('histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{path="/work"}[5m]))) * 1000'),
        "error_rate": prom_query('sum(rate(http_requests_total{status=~"5.."}[5m])) / clamp_min(sum(rate(http_requests_total[5m])), 1)'),
        "replica_count": prom_query('count(up{job="target-service"} == 1)'),
    }


def parse_forecasts(telemetry: pd.DataFrame) -> pd.DataFrame:
    if telemetry.empty:
        return pd.DataFrame()
    forecast_rows = []
    for _, row in telemetry.tail(200).iterrows():
        forecasts = row.get("forecasts")
        if isinstance(forecasts, dict):
            for horizon, value in forecasts.items():
                forecast_rows.append(
                    {
                        "timestamp": row.get("timestamp"),
                        "horizon": f"{horizon}m",
                        "forecast_rps": value,
                    }
                )
    return pd.DataFrame(forecast_rows)


def load_events(path: str) -> pd.DataFrame:
    frame = read_jsonl(path)
    if frame.empty:
        return frame
    columns = [
        "timestamp",
        "action",
        "from_replicas",
        "to_replicas",
        "policy",
        "reason",
        "success",
        "dry_run",
    ]
    available = [col for col in columns if col in frame.columns]
    return frame[available].sort_values(by="timestamp", ascending=False)


telemetry = read_jsonl(TELEMETRY_PATH)
live = get_live_metrics()
events = load_events(EVENTS_PATH)

col1, col2, col3, col4, col5, col6 = st.columns(6)
col1.metric("Replicas", "--" if live["replica_count"] is None else f"{live['replica_count']:.0f}")
col2.metric("CPU", "--" if live["cpu_usage"] is None else f"{live['cpu_usage']:.3f}")
col3.metric("Memory", "--" if live["memory_usage"] is None else f"{live['memory_usage']:.3f}")
col4.metric("RPS", "--" if live["request_rate"] is None else f"{live['request_rate']:.3f}")
col5.metric("p95 latency", "--" if live["p95_latency_ms"] is None else f"{live['p95_latency_ms']:.1f} ms")
col6.metric("Error rate", "--" if live["error_rate"] is None else f"{live['error_rate']:.3f}")

if telemetry.empty:
    st.warning("No telemetry snapshots yet. Start optimizer and generate traffic in Locust.")
else:
    telemetry = telemetry.tail(500).copy()
    if "timestamp" in telemetry:
        telemetry["timestamp"] = pd.to_datetime(telemetry["timestamp"], errors="coerce")

    st.subheader("Historical telemetry")
    available_metrics = [
        metric
        for metric in ["cpu_usage", "memory_usage", "request_rate", "p95_latency_ms", "error_rate", "replica_count"]
        if metric in telemetry.columns
    ]
    if available_metrics and "timestamp" in telemetry:
        long_df = telemetry.melt(id_vars=["timestamp"], value_vars=available_metrics, var_name="metric", value_name="value")
        st.plotly_chart(px.line(long_df, x="timestamp", y="value", color="metric"), use_container_width=True)

    st.subheader("Forecasts")
    forecast_df = parse_forecasts(telemetry)
    if forecast_df.empty:
        st.info("Forecast output will appear after optimizer inference runs.")
    else:
        forecast_df["timestamp"] = pd.to_datetime(forecast_df["timestamp"], errors="coerce")
        st.plotly_chart(px.line(forecast_df, x="timestamp", y="forecast_rps", color="horizon"), use_container_width=True)

st.subheader("Scaling events")
if events.empty:
    st.info("No scaling events logged yet.")
else:
    st.dataframe(events, use_container_width=True)

st.subheader("Policy state and optimizer health")
status_col1, status_col2 = st.columns(2)
try:
    health = requests.get("http://optimizer:8001/health", timeout=3).json()
except Exception:  # noqa: BLE001
    health = {"status": "unavailable", "detail": "optimizer health endpoint not reachable"}
status_col1.json(health)

if telemetry.empty:
    status_col2.info("Estimated cost/savings available after telemetry is generated.")
else:
    latest = telemetry.tail(1).to_dict(orient="records")[0]
    cost = latest.get("estimated_cost_total")
    savings = latest.get("estimated_savings_total")
    status_col2.write(
        {
            "estimated_cost_total": cost,
            "estimated_savings_total": savings,
            "note": "Estimated values, not actual cloud billing.",
        }
    )

st.subheader("Reactive vs predictive comparison")
comparison_path = Path("data/processed/model_evaluation.csv")
if comparison_path.exists():
    comparison = pd.read_csv(comparison_path)
    st.dataframe(comparison, use_container_width=True)
else:
    st.info("Run model training to generate predictive evaluation comparison (data/processed/model_evaluation.csv).")

st.caption(f"Refresh this page every {REFRESH_SECONDS}s or use Streamlit auto-rerun during demos.")
