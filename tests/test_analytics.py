from optimizer.analytics import update_cost_totals


def test_update_cost_totals_accumulates_cost_and_savings():
    cost, savings = update_cost_totals(
        cumulative_cost=1.0,
        cumulative_savings=0.1,
        elapsed_minutes=2.0,
        replicas=2,
        baseline_replicas=4,
        cost_per_replica_minute=0.5,
    )
    assert cost == 3.0
    assert savings == 2.1
