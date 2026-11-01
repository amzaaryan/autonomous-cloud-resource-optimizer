"""Regression tests for the four problems fixed in this branch + the benchmark harness."""

from datetime import datetime, timedelta, timezone
import json
import math

import numpy as np
import pandas as pd

from optimizer.analytics import per_replica_cpu
from optimizer.feature_engineering import build_features, horizon_steps, infer_interval_seconds
from optimizer.forecasting.engine import ForecastEngine
from optimizer.forecasting.train import train_models
from optimizer.prometheus_client import PrometheusClient


def _telemetry(rows=400, interval=15, with_idle=True, error_rate_none=True):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out = []
    for i in range(rows):
        rps = 0.0 if (with_idle and i < 20) else 40.0 + 30.0 * math.sin(i / 25.0)
        out.append(
            {
                "timestamp": (start + timedelta(seconds=interval * i)).isoformat(),
                "cpu_usage": rps / 90.0,
                "memory_usage": 0.3,
                "request_rate": rps,
                "p95_latency_ms": None if rps == 0 else 90.0,
                "error_rate": None if error_rate_none else 0.0,
                "replica_count": 1,
            }
        )
    return pd.DataFrame(out)


# ---- problem 1: forecaster could not train ------------------------------------------------


def test_features_survive_none_error_rate_and_idle_rows():
    features = build_features(_telemetry(), interval_seconds=15)
    assert len(features) > 100
    assert not features.isna().any().any()


# ---- problem 3: horizons are minutes, not rows ----------------------------------------------


def test_horizon_minutes_convert_using_the_sampling_interval():
    assert [horizon_steps(m, 15) for m in (1, 3, 5)] == [4, 12, 20]
    assert [horizon_steps(m, 5) for m in (1, 3, 5)] == [12, 36, 60]


def test_target_is_really_n_minutes_ahead():
    frame = _telemetry(with_idle=False)
    features = build_features(frame, horizons=(1,), interval_seconds=15)
    # row k of `features` corresponds to frame row k + warm-up; the target is 4 rows later
    first = features.iloc[0]
    idx = frame.index[(frame["request_rate"] - first["request_rate"]).abs() < 1e-9][0]
    assert math.isclose(first["target_request_rate_t_plus_1"], frame["request_rate"].iloc[idx + 4], rel_tol=1e-9)


def test_infer_interval_from_timestamps():
    assert infer_interval_seconds(_telemetry(interval=5)) == 5.0


# ---- problem 4: per-replica CPU --------------------------------------------------------------


def test_per_replica_cpu_is_independent_of_fleet_size_for_equal_load_per_replica():
    # 0.35 cores on each replica with a 0.5-core limit = 70% busy, however many replicas
    assert math.isclose(per_replica_cpu(0.35, 1, 0.5), 0.7)
    assert math.isclose(per_replica_cpu(1.05, 3, 0.5), 0.7)
    assert per_replica_cpu(None, 3, 0.5) is None


# ---- prometheus parsing ------------------------------------------------------------------------


def test_parse_instant_value_maps_nan_and_inf_to_none():
    for raw in ("NaN", "+Inf", "-Inf"):
        payload = {"status": "success", "data": {"result": [{"value": [1, raw]}]}}
        assert PrometheusClient.parse_instant_value(payload) is None


def test_parse_range_values_drops_nan():
    payload = {"status": "success", "data": {"result": [{"values": [[1, "1.0"], [2, "NaN"], [3, "2.0"]]}]}}
    assert PrometheusClient.parse_range_values(payload) == [(1.0, 1.0), (3.0, 2.0)]


# ---- training + engine -------------------------------------------------------------------------


def test_train_and_forecast_end_to_end(tmp_path):
    rows, metadata = train_models(
        [_telemetry(600)],
        horizons=(1, 3),
        model_dir=tmp_path,
        report_path=None,
    )
    assert metadata["interval_seconds"] == 15.0 and metadata["target_mode"] == "delta"
    assert {r["horizon_min"] for r in rows} == {1, 3}
    assert all("baseline_mae" in r and "mae_improvement_vs_baseline_pct" in r for r in rows)

    engine = ForecastEngine(tmp_path, (1, 3), 15.0)
    assert set(engine.models) == {1, 3}
    history = _telemetry(60, with_idle=False).to_dict("records")
    forecasts, confidence = engine.forecast(history)
    assert confidence == 1.0 and set(forecasts) == {1, 3}
    assert all(v >= 0 for v in forecasts.values())


def test_absolute_mode_roundtrips_through_metadata(tmp_path):
    train_models([_telemetry(600)], horizons=(1,), model_dir=tmp_path, report_path=None, target_mode="absolute")
    assert json.loads((tmp_path / "metadata.json").read_text())["target_mode"] == "absolute"
    assert ForecastEngine(tmp_path, (1,), 15.0).target_mode == "absolute"


def test_engine_without_models_falls_back_to_persistence_with_zero_confidence(tmp_path):
    engine = ForecastEngine(tmp_path, (1, 3, 5), 15.0)
    forecasts, confidence = engine.forecast([{"request_rate": 12.5}])
    assert forecasts == {1: 12.5, 3: 12.5, 5: 12.5} and confidence == 0.0


def test_engine_ignores_models_trained_at_a_different_interval(tmp_path):
    train_models([_telemetry(600)], horizons=(1,), model_dir=tmp_path, report_path=None)
    assert ForecastEngine(tmp_path, (1,), 5.0).models == {}  # 15 s vs 5 s: would mis-scale horizons


def test_train_rejects_empty_input(tmp_path):
    try:
        train_models([pd.DataFrame()], model_dir=tmp_path, report_path=None)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


# ---- benchmark harness --------------------------------------------------------------------------


def test_benchmark_is_deterministic_and_predictive_not_worse_than_reactive_on_burst():
    from optimizer.benchmark.simulate import run_benchmark, summarise

    kwargs = dict(profiles=["burst"], seeds=1, with_ml=False)
    runs_a, _, _ = run_benchmark(**kwargs)
    runs_b, _, _ = run_benchmark(**kwargs)
    pd.testing.assert_frame_equal(runs_a, runs_b)

    table = summarise(runs_a).set_index("policy")["sla_violation_seconds_mean"]
    assert table["static-5 (peak)"] == 0.0
    assert table["predictive: rate-based, 1-step"] <= table["reactive"]
    cost = summarise(runs_a).set_index("policy")["est_cost_mean"]
    assert cost["static-5 (peak)"] > cost["reactive"]  # elasticity actually saves money
