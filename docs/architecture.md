# Architecture

## System overview

```mermaid
flowchart LR
    subgraph load[Load generation]
        L[Locust<br/>profile = LOCUST_PROFILE]
    end
    subgraph app[Application tier]
        LB[nginx load balancer<br/>host :8000]
        R1[target-service #1<br/>original]
        R2[target-service #2..N<br/>cloned by optimizer]
    end
    subgraph obs[Telemetry]
        P[Prometheus<br/>:9090]
        C[cAdvisor<br/>:8080]
    end
    subgraph ctl[Control plane]
        O[Optimizer loop<br/>:8001]
        F[(ForecastEngine<br/>data/models)]
        D[DockerScaler]
    end
    UI[Streamlit dashboard<br/>:8501]
    DK[(Docker Engine<br/>/var/run/docker.sock)]

    L -->|HTTP| LB
    LB -->|round-robin via Docker DNS| R1
    LB --> R2
    R1 -. /metrics .-> P
    R2 -. /metrics .-> P
    C -. container CPU/mem .-> P
    P -->|PromQL| O
    O --> F
    O --> D
    D -->|create / start / stop / remove| DK
    DK --> R2
    O -->|jsonl + /metrics| P
    P --> UI
    O -. "jsonl files in data/runtime" .-> UI
```

Replicas are not published on host ports. nginx and Prometheus find them through Docker's embedded DNS:
every replica carries the network alias `target-service`, so the name resolves to one address per replica.

## Control loop (every `TELEMETRY_INTERVAL_SECONDS`, default 15 s)

```mermaid
flowchart TD
    A[Collect telemetry from Prometheus] --> B{CPU / memory missing?}
    B -- yes --> B2[Fallback: docker stats for the service]
    B -- no --> C
    B2 --> C[Normalise CPU per replica:<br/>cores / replicas x limit]
    C --> D[ForecastEngine: forecast 1/3/5 min]
    D --> E{Models loaded and<br/>trained at this interval?}
    E -- no --> E2[forecast = current rate<br/>confidence 0.0]
    E -- yes --> E3[forecast from RF<br/>confidence = share of horizons served]
    E2 --> F
    E3 --> F[combined_decision]
    F --> G{Cooldown active?}
    G -- yes --> N[no_action]
    G -- no --> H{Forecast confidence >= 0.30?}
    H -- yes --> I[Predictive: needed = ceil peak / capacity x 70%]
    H -- no --> R[Reactive: per-replica CPU / memory thresholds]
    I --> J{needed > current?}
    J -- yes --> UP[scale_up toward needed<br/>1 step, or PREDICTIVE_MAX_STEP]
    J -- no --> K{Peak fits one fewer replica<br/>under 50% load and SLA healthy?}
    K -- yes --> DOWN[scale_down by 1]
    K -- no --> L2[no_action, forecast is informative]
    L2 --> R
    R --> S{Reactive says scale_down<br/>and forecast was informative?}
    S -- yes --> V[Veto: no_action]
    S -- no --> RES[reactive decision]
    UP --> X
    DOWN --> X
    RES --> X
    V --> X
    N --> X[Log event to scaling_events.jsonl]
    X --> Y{action is scale and DRY_RUN=false?}
    Y -- yes --> Z[DockerScaler.set_replicas]
    Y -- no --> W[Log only]
```

Without trained models the confidence is 0.0, so the predictive branch is skipped and the system is
**reactive only**. Train models (see `docs/setup-and-testing.md`, step 9) to activate predictive scaling.

## How a replica is added and removed (compose mode, `DRY_RUN=false`)

```mermaid
sequenceDiagram
    participant O as Optimizer
    participant D as Docker Engine
    participant N as New replica
    participant L as nginx LB / Prometheus
    O->>D: list containers with label service=target-service
    O->>D: create_container(clone of lowest-numbered one:<br/>same image, env, CPU/mem limits, labels, network alias)
    O->>D: start
    loop until /health answers (max REPLICA_READY_TIMEOUT_SECONDS)
        O->>N: GET /health
    end
    Note over O,N: not ready in time -> stop + remove it, report failure
    N-->>L: Docker DNS now returns it, traffic and scraping begin
    Note over O,L: scale-down, newest replica first, never the original
    O->>D: disconnect from network (DNS stops returning it)
    O->>O: wait SCALE_DOWN_DRAIN_SECONDS
    O->>D: stop + remove
```

## Safety and failure behaviour

- Min/max replica bounds are always enforced.
- Cooldown prevents rapid oscillation.
- Scale-down requires healthy p95 latency and error rate; a missing p95 (no traffic) is not treated as a breach.
- Scale-down hysteresis: scale up at 70% of planned capacity, scale down only if the forecast would sit under 50% with one replica fewer.
- A reactive scale-down is vetoed when a confident forecast says current capacity is needed. Reactive scale-up is never vetoed.
- Missing or malformed telemetry (including NaN/Inf) becomes `None` and yields a safe no-op or reactive fallback.
- New replicas must pass `/health` before they count; a replica that never becomes ready is discarded.
- Models trained at a different sampling interval than the optimizer's are ignored.
- `DRY_RUN=true` (default) never touches Docker.

## Data schema

### telemetry snapshots (`data/runtime/telemetry_snapshots.jsonl`)

- `timestamp`
- `cpu_cores`: aggregate cores used by all replicas
- `cpu_usage`: **per-replica** utilisation = `cpu_cores / (replicas x REPLICA_CPU_LIMIT_CORES)`
- `memory_usage`: aggregate memory used / aggregate limit
- `request_rate` (rps), `p95_latency_ms`, `error_rate`, `replica_count`
- `forecasts` (horizon minutes -> forecast rps), `forecast_confidence`
- `estimated_cost_total`, `estimated_savings_total`

### scaling events (`data/runtime/scaling_events.jsonl`)

`timestamp`, `action`, `from_replicas`, `to_replicas`, `reason`, `policy` (`reactive` / `predictive`),
`dry_run`, `success`, `forecast_confidence`, `forecasts`.

### model metadata (`data/models/metadata.json`)

`interval_seconds`, `horizons`, `target_mode` (`delta` / `absolute`), `feature_columns`, sample counts, `data_source`.

## Evaluation methodology

- Forecasting: Random Forest per horizon vs a persistence baseline ("rate stays as it is now"), reported as MAE, RMSE, MAPE, R2, and `mae_improvement_vs_baseline_pct`. Horizons are minutes, converted to rows from the sampling interval.
- Control: `optimizer/benchmark` runs the same `combined_decision` code against a simulated queueing plant on all five profiles, with the Random Forest evaluated leave-one-profile-out. Results and caveats: `docs/benchmark.md`.

## Scope limitations

- Local Compose is not a production scheduler; compose-mode scaling is container cloning through the Docker socket.
- `RPS_PER_REPLICA_CAPACITY` must be calibrated per machine (`calibrate.py`).
- The benchmark's "rate-based predictive" uses a current-rate forecast at confidence 1.0; the live optimizer without trained models has confidence 0.0 and so behaves reactively.
- The ML forecaster is not shown to beat a "current rate" forecast on unseen traffic shapes (`docs/benchmark.md`).
- cAdvisor behaviour is host-dependent (it is often incomplete on Docker Desktop); the optimizer falls back to `docker stats`.
- Estimated cost/savings are synthetic, not cloud invoices.
