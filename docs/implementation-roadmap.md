# Implementation roadmap

## Milestone 1: Baseline

- [ ] Run the Docker Compose stack.
- [ ] Verify target service endpoints.
- [ ] Verify Prometheus targets are healthy.
- [ ] Verify cAdvisor metrics are available.
- [ ] Verify Locust can generate traffic.

## Milestone 2: Telemetry

- [ ] Implement typed Prometheus metric helpers.
- [ ] Add periodic telemetry snapshots.
- [ ] Validate metric names and PromQL queries.
- [ ] Add telemetry tests.

## Milestone 3: Reactive scaling

- [ ] Choose Compose or Swarm scaling mode.
- [ ] Implement replica discovery.
- [ ] Implement Docker scaling.
- [ ] Add cooldown, hysteresis, and event logging.
- [ ] Compare behavior under spike traffic.

## Milestone 4: Forecasting

- [ ] Collect historical data.
- [ ] Implement persistence baseline.
- [ ] Implement lag and rolling features.
- [ ] Train Random Forest.
- [ ] Evaluate chronologically using MAE, RMSE, MAPE, R2, and spike recall.
- [ ] Add LSTM only if the baseline requires it.

## Milestone 5: Predictive control

- [ ] Implement online inference.
- [ ] Add forecast Prometheus metrics.
- [ ] Implement SLA-aware predictive decisions.
- [ ] Add cost utility and confidence safeguards.
- [ ] Compare reactive and predictive policies.

## Milestone 6: Dashboard and final evaluation

- [ ] Add live metric charts.
- [ ] Add forecast charts.
- [ ] Add scaling event history.
- [ ] Add cost and savings analysis.
- [ ] Automate experiments and generate report charts.
