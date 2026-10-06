"""Cost and decision analytics helpers."""


def update_cost_totals(
    *,
    cumulative_cost: float,
    cumulative_savings: float,
    elapsed_minutes: float,
    replicas: int,
    baseline_replicas: int,
    cost_per_replica_minute: float,
) -> tuple[float, float]:
    current_cost = replicas * elapsed_minutes * cost_per_replica_minute
    baseline_cost = baseline_replicas * elapsed_minutes * cost_per_replica_minute
    next_cost = cumulative_cost + current_cost
    next_savings = cumulative_savings + max(0.0, baseline_cost - current_cost)
    return next_cost, next_savings
