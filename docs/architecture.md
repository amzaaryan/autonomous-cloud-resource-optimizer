# Architecture

## Control loop

1. Locust generates controlled workload (`constant`, `ramp`, `spike`, `burst`, `traffic-drop`).
2. Target FastAPI service handles traffic and exports application metrics.
3. Prometheus scrapes target service, cAdvisor, and optimizer metrics endpoints.
4. Optimizer loop queries telemetry and writes snapshots to `data/runtime/telemetry_snapshots.jsonl`.
5. Forecast engine loads trained models (if present) and predicts 1/3/5 minute request-rate horizons.
6. Predictive policy evaluates forecast + SLA health; if uncertain/missing, system falls back to reactive policy.
7. Docker scaling adapter executes safe action path (`DRY_RUN=true` default).
8. Streamlit dashboard visualizes metrics, forecasts, events, and estimated economics.

## Safety and failure behavior

- Min/max replica bounds are always enforced.
- Cooldown prevents rapid oscillation.
- Scale-down requires healthy p95 latency and error rate.
- Missing/malformed telemetry yields safe no-op or reactive fallback.
- Compose mode is treated as educational baseline; Swarm mode enables actual SDK scaling.

## Data schema

### telemetry snapshots (`data/runtime/telemetry_snapshots.jsonl`)

Each row includes:

- `timestamp`
- `cpu_usage`
- `memory_usage`
- `request_rate`
- `p95_latency_ms`
- `error_rate`
- `replica_count`
- `forecasts` (horizon -> forecasted rps)
- `forecast_confidence`
- `estimated_cost_total`
- `estimated_savings_total`

### scaling events (`data/runtime/scaling_events.jsonl`)

Each row includes:

- `timestamp`
- `action`
- `from_replicas`
- `to_replicas`
- `reason`
- `policy` (`reactive` / `predictive`)
- `dry_run`
- `success`
- `forecast_confidence`
- `forecasts`

## Evaluation methodology

- Chronological train/test split (no random shuffling)
- Baseline: persistence forecast
- Model: Random Forest regressors per horizon
- Metrics: MAE, RMSE, MAPE (when valid), R2, spike precision, spike recall

## Scope limitations

- Local Compose is not a production scheduler.
- cAdvisor behavior is host-dependent.
- Estimated cost/savings are synthetic and not cloud invoices.
- Optional LSTM is intentionally not required for system operation.
