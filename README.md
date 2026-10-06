# Autonomous Cloud Resource Optimizer

An educational prototype for predictive scaling of containerized services.

## Planned control loop

```text
Locust -> target service -> Prometheus/cAdvisor -> forecasting worker
       -> policy engine -> Docker scaler -> target replicas
       -> Streamlit dashboard
```

## Status

This repository contains the initial development scaffold. The implementation should be completed incrementally in the order described below.

## Quick start

```bash
docker compose up --build
```

Expected local services:

- Target API: http://localhost:8000
- Prometheus: http://localhost:9090
- cAdvisor: http://localhost:8080
- Locust: http://localhost:8089
- Streamlit dashboard: http://localhost:8501

## Development roadmap

1. Verify the target service and Prometheus metrics.
2. Add Locust workload profiles.
3. Implement the Prometheus telemetry client.
4. Implement the reactive scaling baseline.
5. Collect historical telemetry.
6. Train and evaluate forecasting models.
7. Connect predictive decisions to Docker scaling.
8. Complete the dashboard and experiments.

See `docs/architecture.md` and `docs/implementation-roadmap.md` for details.
