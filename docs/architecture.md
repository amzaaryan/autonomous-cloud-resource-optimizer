# Architecture

## Control loop

1. Locust generates controlled workload.
2. The target service handles requests and exposes application metrics.
3. Prometheus scrapes application and cAdvisor metrics.
4. The optimizer queries recent telemetry.
5. A forecasting model predicts near-future workload.
6. A policy engine selects `scale_up`, `scale_down`, or `no_action`.
7. The Docker scaling adapter applies the desired replica count.
8. The dashboard visualizes current state, predictions, events, and cost estimates.

## Safety requirements

- Enforce minimum and maximum replicas.
- Use scale-up and scale-down hysteresis.
- Use a cooldown period.
- Do not scale down while latency or errors violate the SLA.
- Fall back to the reactive policy if predictions are missing or invalid.
- Keep the initial Docker adapter in dry-run mode.
