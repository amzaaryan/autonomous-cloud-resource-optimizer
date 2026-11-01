"""The decision the optimizer actually takes each loop: predictive first, reactive as safety net.

Kept in one function so the live control loop (``optimizer.main``) and the offline benchmark
(``optimizer.benchmark``) are guaranteed to run identical logic.
"""

from __future__ import annotations

from datetime import datetime

from optimizer.policy.policies import (
    PolicyConfig,
    PolicyInput,
    ScalingDecision,
    predictive_decision,
    reactive_decision,
)


def combined_decision(
    data: PolicyInput,
    config: PolicyConfig,
    *,
    now: datetime,
    last_scaled_at: datetime | None,
    forecast_confidence: float | None,
    capacity_rps_per_replica: float,
    target_utilization: float,
    scale_down_utilization: float,
) -> ScalingDecision:
    """Predictive policy first; fall back to reactive when the forecast has nothing to say.

    A reactive *scale-down* is vetoed when the predictive policy had a usable forecast and
    still concluded the current capacity is needed. Otherwise a momentarily low CPU reading
    right before a forecast spike would remove a replica that the next loop has to re-add.
    Reactive *scale-up* is never vetoed: it is the safety net when a forecast is wrong.
    """
    predictive = predictive_decision(
        data,
        config,
        now=now,
        last_scaled_at=last_scaled_at,
        forecast_confidence=forecast_confidence,
        capacity_rps_per_replica=capacity_rps_per_replica,
        target_utilization=target_utilization,
        scale_down_utilization=scale_down_utilization,
    )
    if predictive.action != "no_action":
        return predictive

    reactive = reactive_decision(data, config, now=now, last_scaled_at=last_scaled_at)
    if reactive.action == "scale_down" and predictive.informative:
        return ScalingDecision(
            "no_action",
            data.current_replicas,
            "reactive scale-down vetoed: forecast needs current capacity",
            "reactive",
        )
    return reactive
