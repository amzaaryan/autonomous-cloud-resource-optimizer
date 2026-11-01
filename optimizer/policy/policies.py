"""Reactive and predictive scaling policies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math


@dataclass
class ScalingDecision:
    action: str
    desired_replicas: int
    reason: str
    policy: str = "reactive"
    # True when a forecast was actually evaluated against capacity (so a "no_action" means
    # "the forecast says current capacity is right", not "no data"). Used to stop the
    # reactive fallback from scaling down against a confident forecast.
    informative: bool = False


@dataclass
class PolicyInput:
    # ``cpu`` is the utilisation of ONE replica's CPU limit (aggregate cores used divided
    # by replicas * per-replica core limit), so thresholds keep their meaning as the fleet
    # grows. ``memory`` is aggregate memory used / aggregate memory limit.
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
    # Max replicas the PREDICTIVE policy may add in one decision. 0 means "same as
    # scale_step" (one-step scaling, the conservative default). Because a forecast already
    # names the capacity that will be needed, a larger value lets it provision ahead of a
    # spike instead of climbing one replica per cooldown.
    predictive_max_step: int = 0


def _clamp_replicas(replicas: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, replicas))


def in_cooldown(last_scaled_at: datetime | None, cooldown_seconds: int, now: datetime) -> bool:
    if last_scaled_at is None:
        return False
    return now - last_scaled_at < timedelta(seconds=cooldown_seconds)


def _sla_healthy(data: PolicyInput, config: PolicyConfig) -> bool:
    """True when there is no evidence of an SLA breach.

    A missing latency / error-rate sample is NOT evidence of a breach: p95 latency is
    undefined whenever there is no traffic, which is exactly when scaling down is safest.
    """
    latency_ok = data.p95_latency_ms is None or data.p95_latency_ms <= config.latency_sla_ms
    errors_ok = data.error_rate is None or data.error_rate <= config.error_rate_sla
    return latency_ok and errors_ok


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

    down_signal = (
        data.cpu is not None
        and data.cpu <= config.cpu_scale_down_threshold
        and _sla_healthy(data, config)
    )

    if up_signal and data.current_replicas < config.max_replicas:
        desired = _clamp_replicas(data.current_replicas + config.scale_step, config.min_replicas, config.max_replicas)
        return ScalingDecision("scale_up", desired, "reactive threshold crossed", "reactive")

    if down_signal and data.current_replicas > config.min_replicas:
        desired = _clamp_replicas(data.current_replicas - config.scale_step, config.min_replicas, config.max_replicas)
        return ScalingDecision("scale_down", desired, "reactive low-utilization threshold crossed", "reactive")

    return ScalingDecision("no_action", data.current_replicas, "reactive thresholds not met", "reactive")


def required_replicas(peak_rps: float, capacity_rps_per_replica: float, utilization: float) -> int:
    """Replicas needed so that ``peak_rps`` keeps each replica at or below ``utilization``."""
    usable = max(capacity_rps_per_replica * utilization, 1e-9)
    # the epsilon stops float noise (90 * 0.7 == 62.99999999999999) from costing an extra replica
    return max(1, math.ceil(peak_rps / usable - 1e-9))


def predictive_decision(
    data: PolicyInput,
    config: PolicyConfig,
    now: datetime,
    last_scaled_at: datetime | None,
    forecast_confidence: float | None,
    high_load_rps_threshold: float = 3.0,
    low_load_rps_threshold: float = 0.8,
    *,
    capacity_rps_per_replica: float | None = None,
    target_utilization: float = 0.70,
    scale_down_utilization: float = 0.50,
) -> ScalingDecision:
    """Forecast-driven scaling.

    With ``capacity_rps_per_replica`` the policy sizes the fleet from the forecast:
    it scales up toward ``ceil(peak_forecast / (capacity * target_utilization))`` and
    scales down only when, after removing one replica, the forecast peak would still sit
    under ``scale_down_utilization`` (< ``target_utilization``) — the gap is hysteresis
    that stops a scale-down from immediately triggering a scale-up.

    Without it, the legacy absolute thresholds (``high``/``low_load_rps_threshold``) apply.
    """
    if in_cooldown(last_scaled_at, config.cooldown_seconds, now):
        return ScalingDecision("no_action", data.current_replicas, "cooldown active", "predictive")

    if not data.predicted_request_rate:
        return ScalingDecision("no_action", data.current_replicas, "missing forecast data", "predictive")

    if forecast_confidence is not None and forecast_confidence < 0.30:
        return ScalingDecision("no_action", data.current_replicas, "forecast confidence too low", "predictive")

    peak_forecast = max(data.predicted_request_rate.values())
    min_forecast = min(data.predicted_request_rate.values())
    healthy = _sla_healthy(data, config)

    if capacity_rps_per_replica is not None:
        needed = _clamp_replicas(
            required_replicas(peak_forecast, capacity_rps_per_replica, target_utilization),
            config.min_replicas,
            config.max_replicas,
        )
        if needed > data.current_replicas:
            step = config.predictive_max_step or config.scale_step
            desired = min(data.current_replicas + step, needed)
            return ScalingDecision(
                "scale_up",
                desired,
                f"forecast peak {peak_forecast:.1f} rps needs {needed} replicas (capacity {capacity_rps_per_replica:.0f} rps each)",
                "predictive",
            )

        if data.current_replicas > config.min_replicas and healthy:
            after_removal = data.current_replicas - config.scale_step
            after_removal = _clamp_replicas(after_removal, config.min_replicas, config.max_replicas)
            if peak_forecast <= capacity_rps_per_replica * after_removal * scale_down_utilization:
                return ScalingDecision(
                    "scale_down",
                    after_removal,
                    f"forecast peak {peak_forecast:.1f} rps fits {after_removal} replicas under {scale_down_utilization:.0%} load",
                    "predictive",
                )

        return ScalingDecision(
            "no_action", data.current_replicas, "forecast within current capacity", "predictive", informative=True
        )

    # ---- legacy absolute-threshold behaviour -------------------------------------------
    if peak_forecast >= high_load_rps_threshold and data.current_replicas < config.max_replicas:
        desired = _clamp_replicas(data.current_replicas + config.scale_step, config.min_replicas, config.max_replicas)
        return ScalingDecision("scale_up", desired, f"predicted load high ({peak_forecast:.2f} rps)", "predictive")

    if min_forecast <= low_load_rps_threshold and healthy and data.current_replicas > config.min_replicas:
        desired = _clamp_replicas(data.current_replicas - config.scale_step, config.min_replicas, config.max_replicas)
        return ScalingDecision("scale_down", desired, f"predicted load low ({min_forecast:.2f} rps)", "predictive")

    return ScalingDecision("no_action", data.current_replicas, "predictive thresholds not met", "predictive")
