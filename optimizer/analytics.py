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


def per_replica_cpu(cores: float | None, replicas: int, cpu_limit_cores: float) -> float | None:
    """Aggregate cores used -> utilisation of ONE replica's CPU limit (0.0 - 1.0+).

    cAdvisor / ``docker stats`` report CPU summed over all replicas. Policy thresholds such as
    "scale up above 70%" mean per-replica utilisation, so the sum is divided by
    ``replicas * limit``; otherwise the same load would look 3x busier at 3 replicas.
    """
    if cores is None:
        return None
    return cores / (max(1, int(replicas)) * max(cpu_limit_cores, 1e-9))
