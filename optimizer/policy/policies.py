"""Reactive and predictive policy placeholders."""

from dataclasses import dataclass


@dataclass
class ScalingDecision:
    action: str
    desired_replicas: int
    reason: str


def reactive_decision(cpu: float, current_replicas: int, min_replicas: int, max_replicas: int) -> ScalingDecision:
    """Simple baseline policy.

    TODO: Add memory, latency, hysteresis, cooldown, and structured decision metadata.
    """
    if cpu > 0.70 and current_replicas < max_replicas:
        return ScalingDecision("scale_up", current_replicas + 1, "current CPU above threshold")
    if cpu < 0.35 and current_replicas > min_replicas:
        return ScalingDecision("scale_down", current_replicas - 1, "current CPU below threshold")
    return ScalingDecision("no_action", current_replicas, "thresholds not met")


# TODO: Implement predictive_decision() using forecast values and SLA constraints.
