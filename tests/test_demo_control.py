"""Demo control panel, policy modes and background training."""

from datetime import datetime, timedelta, timezone
import json
import math
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "load_testing"))
import presets  # noqa: E402

from optimizer.control import default_control, read_control, update_control  # noqa: E402
from optimizer.forecasting.autotrain import AutoTrainer, split_segments  # noqa: E402
from optimizer.forecasting.engine import ForecastEngine  # noqa: E402
from optimizer.policy.combined import effective_forecast, select_decision  # noqa: E402
from optimizer.policy.policies import PolicyConfig, PolicyInput  # noqa: E402


# ---- traffic presets ------------------------------------------------------------------------


def test_every_preset_returns_valid_targets_over_time():
    for key in presets.PRESETS:
        for t in (0, 10, 61, 200, 1000, 5000):
            tgt = presets.target(key, t)
            assert tgt.users >= 1 and tgt.spawn_rate > 0


def test_idle_is_paused_and_unknown_falls_back():
    assert presets.target("idle", 5).paused is True
    assert presets.target("nope", 5).users == presets.target("low", 5).users


def test_spike_steps_up_then_back_down():
    assert [presets.target("spike", t).users for t in (0, 29, 30, 149, 150)] == [10, 10, 120, 120, 10]


def test_burst_and_warmup_repeat():
    assert presets.target("burst", 10).users == presets.target("burst", 110).users == 25
    assert presets.target("burst", 70).users == presets.target("burst", 170).users == 100
    assert presets.target("warmup", 335).users == presets.target("warmup", 335 + 1500).users == 120


def test_ramp_is_monotonic_and_capped():
    users = [presets.target("ramp", t).users for t in range(0, 600, 15)]
    assert users == sorted(users) and users[0] == 10 and users[-1] == 120


# ---- control file -----------------------------------------------------------------------------


def test_control_defaults_when_missing_or_corrupt(tmp_path):
    assert read_control(tmp_path / "none.json") == default_control()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert read_control(bad) == default_control()
    bad.write_text(json.dumps({"policy_mode": "bogus", "traffic": {"intensity": "huge", "preset": 5}}))
    assert read_control(bad) == default_control()


def test_update_control_roundtrip_and_restart_clock(tmp_path):
    path = tmp_path / "sub" / "control.json"
    update_control(path, preset="spike", now=1000.0)
    update_control(path, policy_mode="reactive")
    update_control(path, intensity="medium")  # must not restart the preset clock
    control = read_control(path)
    assert control["traffic"] == {"preset": "spike", "started_at": 1000.0, "intensity": "medium"}
    assert control["policy_mode"] == "reactive"
    assert not list(path.parent.glob("*.tmp"))


def test_update_control_rejects_bad_values(tmp_path):
    for kwargs in ({"intensity": "x"}, {"policy_mode": "x"}):
        try:
            update_control(tmp_path / "c.json", **kwargs)
        except ValueError:
            continue
        raise AssertionError("expected ValueError")


# ---- policy modes -------------------------------------------------------------------------------


def _cfg():
    return PolicyConfig(1, 5, 1, 60, 0.7, 0.35, 0.75, 500, 0.05)


def test_effective_forecast_predictive_falls_back_to_rate_based():
    f, c, src = effective_forecast("predictive", {1: 5.0, 3: 5.0}, 0.0, 120.0)
    assert f == {1: 120.0, 3: 120.0} and c == 1.0 and src == "rate-based"


def test_effective_forecast_auto_never_fakes_confidence():
    f, c, src = effective_forecast("auto", {1: 5.0}, 0.0, 120.0)
    assert f == {1: 5.0} and c == 0.0 and src == "none"
    assert effective_forecast("auto", {1: 5.0}, 1.0, 120.0)[2] == "random-forest"


def _decide(mode, cpu, rps, confidence):
    return select_decision(
        mode,
        PolicyInput(cpu, 0.2, 100, 0.0, 1, {1: rps, 3: rps}),
        _cfg(),
        now=datetime.now(timezone.utc),
        last_scaled_at=None,
        forecast_confidence=confidence,
        capacity_rps_per_replica=90.0,
        target_utilization=0.7,
        scale_down_utilization=0.5,
    )


def test_reactive_mode_ignores_the_forecast():
    d = _decide("reactive", cpu=0.2, rps=500.0, confidence=1.0)  # huge forecast, idle CPU
    assert d.action == "no_action" and d.policy == "reactive"


def test_predictive_mode_scales_ahead_of_cpu():
    d = _decide("predictive", cpu=0.2, rps=200.0, confidence=1.0)
    assert d.action == "scale_up" and d.policy == "predictive"


def test_auto_without_model_is_reactive():
    assert _decide("auto", cpu=0.9, rps=0.0, confidence=0.0).policy == "reactive"


# ---- background training -----------------------------------------------------------------------


def _telemetry_file(path, rows, interval=15, gap_after=None):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out = []
    for i in range(rows):
        shift = 3600 if gap_after is not None and i >= gap_after else 0
        rps = 50.0 + 40.0 * math.sin(i / 12.0) + (i % 7)
        out.append(
            {
                "timestamp": (start + timedelta(seconds=interval * i + shift)).isoformat(),
                "cpu_usage": rps / 90.0, "memory_usage": 0.3, "request_rate": rps,
                "p95_latency_ms": 80.0, "error_rate": 0.0, "replica_count": 1,
            }
        )
    path.write_text("\n".join(json.dumps(r) for r in out), encoding="utf-8")


def _trainer(tmp_path, **kw):
    return AutoTrainer(
        telemetry_path=tmp_path / "t.jsonl", model_dir=tmp_path / "models", horizons=(1, 3),
        interval_seconds=15.0, status_path=tmp_path / "status.json", min_rows=80, **kw,
    )


def test_split_segments_breaks_on_gaps():
    path = Path("/tmp/_seg.jsonl")
    _telemetry_file(path, 100, gap_after=60)
    parts = split_segments(pd.read_json(path, lines=True), 15.0)
    assert [len(p) for p in parts] == [60, 40]


def test_autotrain_collecting_until_enough_rows(tmp_path):
    _telemetry_file(tmp_path / "t.jsonl", 50)
    status = _trainer(tmp_path).run_once()
    assert status["state"] == "collecting" and status["rows"] == 50
    assert json.loads((tmp_path / "status.json").read_text())["state"] == "collecting"
    assert not list((tmp_path / "models").glob("*.joblib")) if (tmp_path / "models").exists() else True


def test_autotrain_without_gate_deploys_and_reloads_engine(tmp_path):
    _telemetry_file(tmp_path / "t.jsonl", 400)
    engine = ForecastEngine(tmp_path / "models", (1, 3), 15.0)
    assert engine.models == {}
    status = _trainer(tmp_path, require_improvement=False, engine=engine).run_once()
    assert status["state"] == "deployed" and status["deployed_horizons"] == [1, 3]
    assert set(engine.models) == {1, 3}  # hot-reloaded, no restart
    assert not (tmp_path / "models" / ".staging").exists()


def test_autotrain_gate_never_deploys_a_model_worse_than_baseline(tmp_path):
    _telemetry_file(tmp_path / "t.jsonl", 400)
    trainer = _trainer(tmp_path, require_improvement=True)
    status = trainer.run_once()
    for row in status["results"]:
        # whatever the data says, deployment must match the gate exactly
        assert row["deployed"] == (row["improvement_pct"] is not None and row["improvement_pct"] > 0)
    deployed_files = {p.name for p in (tmp_path / "models").glob("rf_*.joblib")}
    assert deployed_files == {f"rf_request_rate_h{h}.joblib" for h in status["deployed_horizons"]}


def test_autotrain_removes_stale_model_that_stops_passing(tmp_path):
    _telemetry_file(tmp_path / "t.jsonl", 400)
    trainer = _trainer(tmp_path, require_improvement=False)
    trainer.run_once()
    assert (tmp_path / "models" / "rf_request_rate_h1.joblib").exists()
    trainer.require_improvement = True
    # force every horizon to fail the gate by making improvement impossible
    import optimizer.forecasting.autotrain as mod
    real = mod.train_models

    def worse(*a, **k):
        rows, meta = real(*a, **k)
        for r in rows:
            r["mae_improvement_vs_baseline_pct"] = -5.0
        return rows, meta

    mod.train_models = worse
    try:
        status = trainer.run_once()
    finally:
        mod.train_models = real
    assert status["state"] == "rejected"
    assert not list((tmp_path / "models").glob("rf_*.joblib"))


def test_engine_reload_picks_up_new_models(tmp_path):
    _telemetry_file(tmp_path / "t.jsonl", 400)
    engine = ForecastEngine(tmp_path / "models", (1,), 15.0)
    _trainer(tmp_path, require_improvement=False).run_once()
    assert engine.models == {}
    engine.reload()
    assert set(engine.models) == {1}
