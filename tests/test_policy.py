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
