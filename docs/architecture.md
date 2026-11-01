# Architecture

## Control loop

1. Locust generates controlled workload (`constant`, `ramp`, `spike`, `burst`, `traffic-drop`) against the nginx load balancer.
2. nginx spreads requests across every `target-service` replica (Docker DNS, re-resolved every 2 s).
3. Prometheus discovers each replica through DNS service discovery and scrapes them, cAdvisor and the optimizer.
4. Optimizer loop queries telemetry and writes snapshots to `data/runtime/telemetry_snapshots.jsonl`.
5. Forecast engine loads trained models (if present) and predicts 1/3/5 *minute* request-rate horizons.
6. `combined_decision`: the predictive policy sizes the fleet from the forecast and the measured per-replica capacity;
   if it has nothing to say the reactive policy decides, and a reactive scale-down is vetoed when a confident
   forecast needs the current capacity.
7. Docker scaling adapter executes the decision. `DRY_RUN=true` (default) only logs; otherwise it clones/removes
   `target-service` containers and waits for `/health` before a replica counts.
8. Streamlit dashboard visualizes metrics, forecasts, events, and estimated economics.

## Safety and failure behavior

- Min/max replica bounds are always enforced.
- Cooldown prevents rapid oscillation.
- Scale-down requires healthy p95 latency and error rate.
- Missing/malformed telemetry yields safe no-op or reactive fallback.
- New replicas must pass `/health` before they count; a replica that never becomes ready is discarded.
- Scale-down detaches the newest replica from the network, drains, then removes it. The original is never removed.
- CPU is normalised per replica, so thresholds mean the same at 1 or 5 replicas.
- The benchmark (`optimizer/benchmark`) runs the identical `combined_decision` code against a simulated plant.

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

- Local Compose is not a production scheduler; compose-mode scaling is container cloning through the Docker socket.
- `RPS_PER_REPLICA_CAPACITY` must be calibrated per machine (`make calibrate`).
- The ML forecaster is not shown to beat a "current rate" forecast on unseen traffic shapes (see `docs/benchmark.md`).
- cAdvisor behavior is host-dependent.
- Estimated cost/savings are synthetic and not cloud invoices.
- Optional LSTM is intentionally not required for system operation.
