"""Reactive and predictive scaling policies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class ScalingDecision:
    action: str
    desired_replicas: int
    reason: str
    policy: str = "reactive"


@dataclass
class PolicyInput:
    cpu: float | None
    memory: float | None
    p95_latency_ms: float | None
    error_rate: float | None
    current_replicas: int
    predicted_request_rate: dict[int, float]


@dataclass
class PolicyConfig:
    min_replicas: int
    max_replicas: int
    scale_step: int
    cooldown_seconds: int
    cpu_scale_up_threshold: float
    cpu_scale_down_threshold: float
    memory_scale_up_threshold: float
    latency_sla_ms: float
    error_rate_sla: float


def _clamp_replicas(replicas: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, replicas))


def in_cooldown(last_scaled_at: datetime | None, cooldown_seconds: int, now: datetime) -> bool:
    if last_scaled_at is None:
        return False
    return now - last_scaled_at < timedelta(seconds=cooldown_seconds)


def reactive_decision(
    data: PolicyInput,
    config: PolicyConfig,
    now: datetime,
    last_scaled_at: datetime | None,
) -> ScalingDecision:
    if in_cooldown(last_scaled_at, config.cooldown_seconds, now):
        return ScalingDecision("no_action", data.current_replicas, "cooldown active", "reactive")

    if data.cpu is None and data.memory is None:
        return ScalingDecision("no_action", data.current_replicas, "missing CPU and memory telemetry", "reactive")

    up_signal = (
        (data.cpu is not None and data.cpu >= config.cpu_scale_up_threshold)
        or (data.memory is not None and data.memory >= config.memory_scale_up_threshold)
    )

    scale_down_safe = (
        data.p95_latency_ms is None
        or data.p95_latency_ms <= config.latency_sla_ms
    ) and (
        data.error_rate is None
        or data.error_rate <= config.error_rate_sla
    )

    down_signal = (
        data.cpu is not None
        and data.cpu <= config.cpu_scale_down_threshold
        and scale_down_safe
    )

    if up_signal and data.current_replicas < config.max_replicas:
        desired = _clamp_replicas(data.current_replicas + config.scale_step, config.min_replicas, config.max_replicas)
        return ScalingDecision("scale_up", desired, "reactive threshold crossed", "reactive")

    if down_signal and data.current_replicas > config.min_replicas:
        desired = _clamp_replicas(data.current_replicas - config.scale_step, config.min_replicas, config.max_replicas)
        return ScalingDecision("scale_down", desired, "reactive low-utilization threshold crossed", "reactive")

    return ScalingDecision("no_action", data.current_replicas, "reactive thresholds not met", "reactive")


def predictive_decision(
    data: PolicyInput,
    config: PolicyConfig,
    now: datetime,
    last_scaled_at: datetime | None,
    forecast_confidence: float | None,
    high_load_rps_threshold: float,
    low_load_rps_threshold: float,
) -> ScalingDecision:
    if in_cooldown(last_scaled_at, config.cooldown_seconds, now):
        return ScalingDecision("no_action", data.current_replicas, "cooldown active", "predictive")

    if not data.predicted_request_rate:
        return ScalingDecision("no_action", data.current_replicas, "missing forecast data", "predictive")

    if forecast_confidence is not None and forecast_confidence < 0.30:
        return ScalingDecision("no_action", data.current_replicas, "forecast confidence too low", "predictive")

    peak_forecast = max(data.predicted_request_rate.values())
    min_forecast = min(data.predicted_request_rate.values())

    if peak_forecast >= high_load_rps_threshold and data.current_replicas < config.max_replicas:
        desired = _clamp_replicas(data.current_replicas + config.scale_step, config.min_replicas, config.max_replicas)
        return ScalingDecision("scale_up", desired, f"predicted load high ({peak_forecast:.2f} rps)", "predictive")

    scale_down_safe = (
        data.p95_latency_ms is not None
        and data.p95_latency_ms <= config.latency_sla_ms
        and data.error_rate is not None
        and data.error_rate <= config.error_rate_sla
    )
    if min_forecast <= low_load_rps_threshold and scale_down_safe and data.current_replicas > config.min_replicas:
        desired = _clamp_replicas(data.current_replicas - config.scale_step, config.min_replicas, config.max_replicas)
        return ScalingDecision("scale_down", desired, f"predicted load low ({min_forecast:.2f} rps)", "predictive")

    return ScalingDecision("no_action", data.current_replicas, "predictive thresholds not met", "predictive")
