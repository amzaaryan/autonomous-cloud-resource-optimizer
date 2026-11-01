"""Streamlit dashboard for optimizer telemetry and decisions."""

from __future__ import annotations

from pathlib import Path
import json
import sys
import time

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

from optimizer.control import INTENSITIES, MODES, read_control, update_control

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "load_testing"))
import presets  # noqa: E402


st.set_page_config(page_title="Cloud Resource Optimizer", layout="wide")
st.title("Autonomous Cloud Resource Optimizer")

PROMETHEUS_URL = st.sidebar.text_input("Prometheus URL", value="http://prometheus:9090")
TELEMETRY_PATH = st.sidebar.text_input("Telemetry file", value="data/runtime/telemetry_snapshots.jsonl")
EVENTS_PATH = st.sidebar.text_input("Scaling events file", value="data/runtime/scaling_events.jsonl")
REFRESH_SECONDS = st.sidebar.slider("Auto refresh (seconds)", min_value=5, max_value=60, value=10)
CONTROL_PATH = "data/runtime/control.json"
MODEL_STATUS_PATH = "data/runtime/model_status.json"

POLICY_MODE_LABELS = {
    "auto": "Auto (predictive when a model is trained, else reactive)",
    "reactive": "Reactive only (CPU thresholds)",
    "predictive": "Predictive (forecast-driven; rate-based until a model is trained)",
}


def render_control_panel() -> None:
    st.header("Demo control")
    control = read_control(CONTROL_PATH)
    traffic = control["traffic"]

    st.markdown("**Traffic generator**: pick what Locust sends to the app")
    keys = list(presets.PRESETS)
    columns = st.columns(len(keys))
    for column, key in zip(columns, keys):
        preset = presets.PRESETS[key]
        if column.button(preset.label, key=f"preset_{key}", help=preset.description, width="stretch"):
            update_control(CONTROL_PATH, preset=key)
            st.rerun()

    current = presets.PRESETS.get(traffic["preset"], presets.PRESETS[presets.DEFAULT_PRESET])
    started = traffic["started_at"]
    running_for = f"{max(0, time.time() - started):.0f}s" if started else "since start"
    now_target = presets.target(current.key, time.time() - started if started else 0.0)
    st.info(
        f"Traffic: **{current.label}** ({current.description}). Running {running_for}. "
        f"Right now: **{'paused' if now_target.paused else str(now_target.users) + ' users'}**."
    )

    left, right = st.columns(2)
    intensity = left.radio(
        "Work per request",
        INTENSITIES,
        index=INTENSITIES.index(traffic["intensity"]),
        horizontal=True,
        help="Heavier requests use more CPU per request, so one replica serves fewer requests per second.",
    )
    if intensity != traffic["intensity"]:
        update_control(CONTROL_PATH, intensity=intensity)
        st.rerun()

    mode = right.radio(
        "Scaling policy",
        MODES,
        index=MODES.index(control["policy_mode"]),
        format_func=lambda m: POLICY_MODE_LABELS[m],
        help="Switch live to compare reactive and predictive on the same traffic.",
    )
    if mode != control["policy_mode"]:
        update_control(CONTROL_PATH, policy_mode=mode)
        st.rerun()

    render_model_status()


def render_model_status() -> None:
    st.markdown("**Forecasting model (retrained automatically in the background)**")
    try:
        status = json.loads(Path(MODEL_STATUS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st.info("Waiting for the optimizer's first training check...")
        return
    state = status.get("state")
    message = status.get("message", "")
    if state == "deployed":
        st.success(message)
    elif state == "collecting":
        rows, needed = status.get("rows", 0), status.get("min_rows", 0)
        st.progress(min(1.0, rows / needed) if needed else 0.0, text=message)
    elif state == "rejected":
        st.warning(message + ". Scaling stays rate-based / reactive.")
    else:
        st.error(message)
    results = status.get("results")
    if results:
        st.dataframe(pd.DataFrame(results), width="stretch")


render_control_panel()


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


def first_present(*values: float | None) -> float | None:
    """First value that is not None. (`a or b` would wrongly discard a real 0.0 reading.)"""
    for value in values:
        if value is not None:
            return value
    return None


def get_live_metrics() -> dict[str, float | None]:
    return {
        "cpu_usage": first_present(prom_query('optimizer_target_cpu_usage'), prom_query('sum(rate(container_cpu_usage_seconds_total{job="cadvisor",id!="/",id!~"/docker.*|/system.slice.*|/user.slice.*"}[1m]))')),
        "memory_usage": first_present(prom_query('optimizer_target_memory_usage'), prom_query('sum(container_memory_usage_bytes{job="cadvisor",id!="/",id!~"/docker.*|/system.slice.*|/user.slice.*"}) / scalar(sum(machine_memory_bytes))')),
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
        "policy_mode",
        "forecast_source",
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
col2.metric("CPU", "--" if live["cpu_usage"] is None else f"{live['cpu_usage'] * 100:.1f}%")
col3.metric("Memory", "--" if live["memory_usage"] is None else f"{live['memory_usage'] * 100:.1f}%")
col4.metric("RPS", "--" if live["request_rate"] is None else f"{live['request_rate']:.3f}")
col5.metric("p95 latency", "--" if live["p95_latency_ms"] is None else f"{live['p95_latency_ms']:.1f} ms")
col6.metric("Error rate", "--" if live["error_rate"] is None else f"{live['error_rate']:.3f}")

if not telemetry.empty and "policy_mode" in telemetry.columns:
    latest_row = telemetry.iloc[-1]
    st.caption(
        f"Optimizer is using: policy mode **{latest_row.get('policy_mode')}**, forecast source "
        f"**{latest_row.get('forecast_source')}**, forecast confidence {latest_row.get('forecast_confidence')}"
    )

if telemetry.empty:
    st.warning("No telemetry snapshots yet. Start optimizer and generate traffic in Locust.")
else:
    telemetry = telemetry.tail(500).copy()
    if "timestamp" in telemetry:
        telemetry["timestamp"] = pd.to_datetime(telemetry["timestamp"], utc=True, errors="coerce")

    st.subheader("Historical telemetry")
    available_metrics = [
        metric
        for metric in ["cpu_usage", "memory_usage", "request_rate", "p95_latency_ms", "error_rate", "replica_count"]
        if metric in telemetry.columns
    ]
    if available_metrics and "timestamp" in telemetry:
        long_df = telemetry.melt(id_vars=["timestamp"], value_vars=available_metrics, var_name="metric", value_name="value")
        st.plotly_chart(px.line(long_df, x="timestamp", y="value", color="metric"), width="stretch")

    st.subheader("Forecasts")
    forecast_df = parse_forecasts(telemetry)
    if forecast_df.empty:
        st.info("Forecast output will appear after optimizer inference runs.")
    else:
        forecast_df["timestamp"] = pd.to_datetime(forecast_df["timestamp"], utc=True, errors="coerce")
        st.plotly_chart(px.line(forecast_df, x="timestamp", y="forecast_rps", color="horizon"), width="stretch")

st.subheader("Scaling events")
if events.empty:
    st.info("No scaling events logged yet.")
else:
    st.dataframe(events, width="stretch")

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
    st.dataframe(comparison, width="stretch")
else:
    st.info("Run model training to generate predictive evaluation comparison (data/processed/model_evaluation.csv).")

st.subheader("Reactive vs predictive benchmark (simulation)")
benchmark_path = Path("docs/results/benchmark_summary.csv")
if benchmark_path.exists():
    bench = pd.read_csv(benchmark_path)
    shown = bench[
        ["profile", "policy", "sla_violation_seconds_mean", "requests_over_sla_pct_mean", "avg_replicas_mean", "est_cost_mean"]
    ].rename(
        columns={
            "sla_violation_seconds_mean": "SLA-violation seconds",
            "requests_over_sla_pct_mean": "% requests over SLA",
            "avg_replicas_mean": "avg replicas",
            "est_cost_mean": "est. cost",
        }
    )
    profile = st.selectbox("Traffic profile", list(shown["profile"].unique()))
    st.dataframe(shown[shown["profile"] == profile].drop(columns="profile"), width="stretch")
    st.caption("Closed-loop simulation of the same policy code; see docs/benchmark.md for assumptions.")
else:
    st.info("Run `make benchmark` to generate docs/results/benchmark_summary.csv.")

st.caption(f"Auto-refreshing every {REFRESH_SECONDS}s.")
time.sleep(REFRESH_SECONDS)
st.rerun()
