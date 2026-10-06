# Autonomous Cloud Resource Optimizer

Educational prototype for **predictive container scaling** using Docker Compose, Prometheus, cAdvisor, Locust, FastAPI, and Streamlit.

## What runs locally

- Target API: `http://localhost:8000` (`/health`, `/work`, `/metrics`)
- Prometheus: `http://localhost:9090`
- cAdvisor: `http://localhost:8080` (host support dependent)
- Optimizer health/metrics: `http://localhost:8001/health`, `http://localhost:8001/metrics`
- Locust UI: `http://localhost:8089`
- Dashboard: `http://localhost:8501`

## Quick start

```bash
cp .env.example .env
docker compose up --build
```

If cAdvisor is unsupported on your host, keep the stack running and continue with app/Prometheus metrics; some container-level metrics may be missing.

## Workload generation

Locust profiles are selected by `LOCUST_PROFILE` in `.env`:

- `constant`
- `ramp`
- `spike`
- `burst`
- `traffic-drop`

You can also run custom Locust commands:

```bash
docker compose run --rm -e LOCUST_PROFILE=spike locust
```

Increase target load via `.env` (`WORK_INTENSITY_*_ITERATIONS`) or endpoint override:

```text
GET /work?intensity=high
GET /work?intensity=medium&iterations=2000000
```

## Scaling modes and safety

Default mode is safe local baseline:

- `DRY_RUN=true`
- `SCALING_MODE=compose`

Behavior implemented:

- min/max replicas
- cooldown
- one-step scaling (`SCALE_STEP`)
- SLA-safe scale-down checks (latency + error rate)
- structured scaling event logging (`data/runtime/scaling_events.jsonl`)
- predictive fallback to reactive when forecast confidence/data is weak

> Docker Compose is not a production scheduler. Real in-place scaling is supported only with `SCALING_MODE=swarm` and `DRY_RUN=false`.

## Telemetry + runtime data schema

Optimizer writes line-delimited JSON snapshots:

- `data/runtime/telemetry_snapshots.jsonl`
  - timestamp
  - cpu_usage
  - memory_usage
  - request_rate
  - p95_latency_ms
  - error_rate
  - replica_count
  - forecasts
  - forecast_confidence
  - estimated_cost_total
  - estimated_savings_total

And scaling events:

- `data/runtime/scaling_events.jsonl`
  - timestamp, action, from/to replicas, reason, policy, dry_run, success

## Forecasting workflow

Train Random Forest models (chronological split, horizons 1/3/5 min):

```bash
python -m optimizer.forecasting.train \
  --input data/runtime/telemetry_snapshots.jsonl \
  --model-dir data/models \
  --report data/processed/model_evaluation.csv
```

Outputs:

- `data/models/rf_request_rate_h*.joblib`
- `data/models/metadata.json`
- `data/processed/model_evaluation.csv` (MAE, RMSE, MAPE, R2, spike precision/recall)

Model artifacts/runtime data are gitignored.

## Dashboard capabilities

- live CPU, memory, request rate, p95 latency, error rate, replica count
- historical telemetry chart
- forecast chart (when available)
- scaling event table
- optimizer health/policy state
- estimated cost/savings
- predictive evaluation table from training report

Dashboard gracefully degrades when Prometheus/files are unavailable.

## Developer commands

```bash
make test
make train
make up
make down
```

## Testing

Unit tests cover:

- Prometheus parsing and malformed/empty data handling
- feature engineering
- persistence baseline + evaluation metrics
- reactive/predictive policy decisions
- Docker scaler safety behavior (mocked)
- cost calculations
- FastAPI smoke tests

Tests do not require live Prometheus or Docker daemon.

## Troubleshooting

- Prometheus unavailable: optimizer stays degraded and retries next interval.
- Empty/malformed metric results: handled defensively; predictive policy falls back.
- No forecast models: optimizer uses safe fallback forecasts from current request rate.
- Missing cAdvisor metrics: CPU/memory may be unavailable; loop still runs with fallback behavior.

## Architecture and assumptions

See:

- `docs/architecture.md`
- `docs/implementation-roadmap.md`

PromQL queries assume:

- `target-service` metrics exposed by FastAPI `/metrics`
- cAdvisor compose labels (`container_label_com_docker_compose_service`)
- scrape jobs named `target-service`, `cadvisor`, and `optimizer`

## Important note on costs

Estimated cost/savings in this project are **local approximations for education only** and are **not actual cloud billing**.
