from datetime import datetime, timezone

from optimizer.policy.policies import PolicyConfig, PolicyInput, predictive_decision, reactive_decision


def _config() -> PolicyConfig:
    return PolicyConfig(
        min_replicas=1,
        max_replicas=5,
        scale_step=1,
        cooldown_seconds=60,
        cpu_scale_up_threshold=0.7,
        cpu_scale_down_threshold=0.35,
        memory_scale_up_threshold=0.75,
        latency_sla_ms=500,
        error_rate_sla=0.05,
    )


def test_reactive_scales_up_when_cpu_high():
    data = PolicyInput(0.9, 0.2, 120, 0.0, 2, {})
    decision = reactive_decision(data, _config(), datetime.now(timezone.utc), None)
    assert decision.action == "scale_up"
    assert decision.desired_replicas == 3


def test_reactive_refuses_scale_down_when_sla_bad():
    data = PolicyInput(0.2, 0.1, 900, 0.2, 3, {})
    decision = reactive_decision(data, _config(), datetime.now(timezone.utc), None)
    assert decision.action == "no_action"


def test_predictive_scales_up_when_high_forecast():
    data = PolicyInput(0.3, 0.2, 100, 0.0, 2, {1: 2.5, 3: 3.5, 5: 3.1})
    decision = predictive_decision(
        data,
        _config(),
        datetime.now(timezone.utc),
        None,
        forecast_confidence=0.9,
        high_load_rps_threshold=3.0,
        low_load_rps_threshold=0.8,
    )
    assert decision.action == "scale_up"


def test_predictive_scales_down_only_when_sla_healthy():
    data = PolicyInput(0.1, 0.1, 150, 0.0, 3, {1: 0.5, 3: 0.6, 5: 0.7})
    decision = predictive_decision(
        data,
        _config(),
        datetime.now(timezone.utc),
        None,
        forecast_confidence=0.8,
        high_load_rps_threshold=3.0,
        low_load_rps_threshold=0.8,
    )
    assert decision.action == "scale_down"


# ---- capacity-based predictive policy -----------------------------------------------------

from optimizer.policy.combined import combined_decision  # noqa: E402
from optimizer.policy.policies import required_replicas  # noqa: E402

CAP, TARGET, DOWN = 90.0, 0.70, 0.50


def _cap(data, config=None, confidence=0.9, last=None):
    return predictive_decision(
        data,
        config or _config(),
        datetime.now(timezone.utc),
        last,
        forecast_confidence=confidence,
        capacity_rps_per_replica=CAP,
        target_utilization=TARGET,
        scale_down_utilization=DOWN,
    )


def test_required_replicas_rounds_up():
    assert required_replicas(63.0, 90.0, 0.7) == 1
    assert required_replicas(63.1, 90.0, 0.7) == 2
    assert required_replicas(0.0, 90.0, 0.7) == 1


def test_capacity_policy_scales_toward_needed_one_step_by_default():
    data = PolicyInput(0.3, 0.2, 100, 0.0, 1, {1: 200.0, 3: 200.0, 5: 200.0})
    decision = _cap(data)
    assert decision.action == "scale_up"
    assert decision.desired_replicas == 2  # needed 4, but default step is 1


def test_capacity_policy_jumps_when_predictive_max_step_allows():
    cfg = _config()
    cfg.predictive_max_step = 3
    data = PolicyInput(0.3, 0.2, 100, 0.0, 1, {1: 200.0, 3: 200.0, 5: 200.0})
    assert _cap(data, cfg).desired_replicas == 4
    data = PolicyInput(0.3, 0.2, 100, 0.0, 1, {1: 100.0, 3: 100.0, 5: 100.0})
    assert _cap(data, cfg).desired_replicas == 2  # never overshoots the need


def test_capacity_policy_never_exceeds_max_replicas():
    data = PolicyInput(0.3, 0.2, 100, 0.0, 5, {1: 9999.0})
    assert _cap(data).action == "no_action"


def test_capacity_policy_scales_down_with_hysteresis():
    # 2 replicas, forecast 40 rps: one replica at 50% of 90 = 45 rps -> fits
    data = PolicyInput(0.1, 0.1, 100, 0.0, 2, {1: 40.0, 3: 40.0, 5: 40.0})
    decision = _cap(data)
    assert decision.action == "scale_down" and decision.desired_replicas == 1
    # forecast 50 rps: needs 1 replica at 70% (63 rps) but 50 > 45, so DO NOT scale down yet
    data = PolicyInput(0.1, 0.1, 100, 0.0, 2, {1: 50.0, 3: 50.0, 5: 50.0})
    decision = _cap(data)
    assert decision.action == "no_action" and decision.informative


def test_capacity_policy_scales_down_when_idle_and_latency_missing():
    # p95 is undefined with no traffic; that must not block scaling down
    data = PolicyInput(0.0, 0.1, None, None, 3, {1: 0.0, 3: 0.0, 5: 0.0})
    assert _cap(data).action == "scale_down"


def test_capacity_policy_blocks_scale_down_when_sla_breached():
    data = PolicyInput(0.1, 0.1, 900, 0.0, 3, {1: 1.0, 3: 1.0, 5: 1.0})
    assert _cap(data).action == "no_action"


def test_capacity_policy_respects_cooldown_and_low_confidence():
    data = PolicyInput(0.3, 0.2, 100, 0.0, 1, {1: 500.0})
    assert _cap(data, last=datetime.now(timezone.utc)).reason == "cooldown active"
    assert _cap(data, confidence=0.0).reason == "forecast confidence too low"


def test_legacy_thresholds_still_default_without_capacity():
    data = PolicyInput(0.3, 0.2, 100, 0.0, 2, {1: 2.5, 3: 3.5, 5: 3.1})
    decision = predictive_decision(data, _config(), datetime.now(timezone.utc), None, forecast_confidence=0.9)
    assert decision.action == "scale_up"


def _combined(data, confidence=0.9):
    return combined_decision(
        data,
        _config(),
        now=datetime.now(timezone.utc),
        last_scaled_at=None,
        forecast_confidence=confidence,
        capacity_rps_per_replica=CAP,
        target_utilization=TARGET,
        scale_down_utilization=DOWN,
    )


def test_combined_vetoes_reactive_scale_down_against_confident_forecast():
    # CPU is momentarily low (reactive would scale down) but the forecast needs 2 replicas
    data = PolicyInput(0.1, 0.1, 100, 0.0, 2, {1: 100.0, 3: 100.0, 5: 100.0})
    decision = _combined(data)
    assert decision.action == "no_action"
    assert "vetoed" in decision.reason


def test_combined_reactive_scale_up_is_never_vetoed():
    # forecast says all is calm but CPU is hot: safety net must fire
    data = PolicyInput(0.95, 0.2, 100, 0.0, 1, {1: 5.0, 3: 5.0, 5: 5.0})
    assert _combined(data).action == "scale_up"


def test_combined_falls_back_to_reactive_without_forecast_confidence():
    data = PolicyInput(0.95, 0.2, 100, 0.0, 1, {1: 5.0})
    decision = _combined(data, confidence=0.0)
    assert decision.action == "scale_up" and decision.policy == "reactive"
