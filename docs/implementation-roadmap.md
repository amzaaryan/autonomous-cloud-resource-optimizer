# Implementation roadmap

## Milestone 1: Baseline

- [x] Run Docker Compose stack with pinned core image tags.
- [x] Verify target service endpoints (`/health`, `/work`, `/metrics`).
- [x] Configure Prometheus scrape jobs for target, cAdvisor, optimizer.
- [x] Add optimizer and dashboard health checks.
- [x] Keep cAdvisor included for supported hosts.

## Milestone 2: Telemetry

- [x] Implement Prometheus client instant/range query support.
- [x] Add typed telemetry helpers (CPU, memory, request rate, p95, errors, replicas).
- [x] Add defensive parsing for unavailable/empty/malformed metric data.
- [x] Persist periodic telemetry snapshots.
- [x] Expose optimizer health/metrics on port 8001.

## Milestone 3: Reactive scaling

- [x] Implement compose/swarm-aware Docker scaling adapter.
- [x] Keep default safe path (`DRY_RUN=true`).
- [x] Enforce min/max, cooldown, one-step adjustments.
- [x] Add SLA-safe scale-down logic and structured scaling event logging.

## Milestone 4: Forecasting

- [x] Implement lag, rolling, and rate-of-change features.
- [x] Add persistence baseline and regression evaluation metrics.
- [x] Train Random Forest models with chronological split.
- [x] Export model artifacts and metadata under ignored paths.

## Milestone 5: Predictive control

- [x] Load trained models when available.
- [x] Produce 1/3/5-minute request-rate forecasts.
- [x] Add forecast metrics and confidence safeguards.
- [x] Use predictive policy first; fallback to reactive policy when needed.

## Milestone 6: Dashboard and evaluation

- [x] Display live metrics and historical telemetry.
- [x] Display forecast trends and scaling event history.
- [x] Show policy/health state and estimated cost/savings.
- [x] Display predictive experiment report table when available.

## Milestone 7: Autonomous scaling execution (CE2)

- [x] Real compose scaling through the Docker SDK (clone, readiness, drain, remove) behind `DRY_RUN`.
- [x] nginx load balancer + DNS service discovery so replicas receive traffic and are scraped.
- [x] Capacity-based predictive policy with hysteresis and reactive veto; per-replica CPU.
- [x] Forecaster fixes: trainable on idle/None telemetry; horizons in minutes; delta target; interval check.
- [x] Reactive-vs-predictive benchmark (`docs/benchmark.md`) and capacity calibration (`make calibrate`).
- [ ] Rehearse `make demo-live` against a live Docker daemon and record the real replica timeline.
- [ ] Evaluate the forecaster in-distribution on real captured telemetry.
