# Setup, run and test guide

Follow the steps in order the first time. Each command is followed by what it does. Steps 5 and 6 need
no Docker, so you can check the code before starting the stack.

> **Status of what has been verified.** Unit tests and the benchmark simulation run without Docker and
> were run during development. The Docker pieces (real scaling, nginx, Prometheus discovery, cAdvisor)
> were only tested with mocks. Step 11 is how you verify them on your machine.

## 1. Prerequisites

| Need | Why | Check |
|---|---|---|
| Docker Engine + Compose v2 | runs the whole stack | `docker --version` and `docker compose version` |
| Python 3.10+ | tests, training, benchmark, calibration (not needed just to run the stack) | `python3 --version` |
| ~4 GB free RAM, 4+ CPU cores | up to 5 replicas plus Prometheus, cAdvisor, Locust | |
| Linux, macOS (Docker Desktop) or Windows with WSL2 | the optimizer talks to `/var/run/docker.sock` | |

On Docker Desktop (macOS/Windows) cAdvisor often reports incomplete per-container CPU. The optimizer then
falls back to `docker stats` automatically, so scaling still works.

## 2. Get the code

```bash
git clone https://github.com/amzaaryan/autonomous-cloud-resource-optimizer.git
cd autonomous-cloud-resource-optimizer
git checkout fix/real-scaling-and-forecasting   # or main, once merged
```

Clones the repo and selects the branch that contains real scaling, the fixed forecaster and the benchmark.

## 3. Configure

```bash
cp .env.example .env
```

Creates your local settings (`.env` is git-ignored). Docker Compose reads it automatically. The defaults work as they are.
The ones you will touch:

| Variable | Default | Meaning |
|---|---|---|
| `DRY_RUN` | `true` | `true` only logs decisions. `false` really starts/stops containers |
| `LOCUST_PROFILE` | `constant` | traffic shape: `constant`, `ramp`, `spike`, `burst`, `traffic-drop` |
| `LOCUST_WORK_INTENSITY` | `low` | CPU cost per request (`low`/`medium`/`high`) |
| `RPS_PER_REPLICA_CAPACITY` | `90` | requests/s one replica sustains. **Measure it (step 10)** |
| `MIN_REPLICAS` / `MAX_REPLICAS` | `1` / `5` | hard bounds |
| `COOLDOWN_SECONDS` | `60` | minimum gap between scaling actions |
| `PREDICTIVE_MAX_STEP` | `0` | `0` = add one replica per decision; `3` = may add up to three at once |
| `TELEMETRY_INTERVAL_SECONDS` | `15` | optimizer loop period; models are tied to this value |

Shell variables override `.env` for a single run, for example `DRY_RUN=false docker compose up`.

## 4. Python environment (for tests, training, benchmark)

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Creates an isolated environment and installs pandas, scikit-learn, FastAPI, pytest, matplotlib and the rest.
Not needed if you only run the Docker stack.

## 5. Run the unit tests (no Docker)

```bash
make test            # same as: python -m pytest -q
```

Runs every test in `tests/`: policy decisions (reactive, capacity-based predictive, hysteresis, combined veto),
feature engineering and minute-based horizons, NaN handling, forecasting train/inference, the Docker scaler with a
mocked Docker client (scale up, scale down, never remove the original, readiness failure, dry run), the
smoke-test tool, the calibration helper, the target FastAPI service and the benchmark's determinism.
Expect all green. One test file is skipped automatically if FastAPI is not installed.

Run a subset: `python -m pytest -q tests/test_policy.py` or `python -m pytest -q -k scaler`.

## 6. Run the benchmark (no Docker, optional)

```bash
make benchmark       # about 5 minutes; --seeds 5, all profiles, with the Random Forest
```

Simulates 30 minutes of each traffic profile for static, reactive and predictive controllers using the same decision
code as the live optimizer. Writes `docs/results/*.csv` and plots in `docs/images/`. Quick version without ML (seconds):

```bash
python -m optimizer.benchmark.simulate --seeds 3 --no-ml --out /tmp/bench --plot-dir /tmp/bench
```

How to read it: [`benchmark.md`](benchmark.md). It is a simulation, not a Docker measurement.

## 7. Start the stack (safe mode: DRY_RUN=true)

```bash
docker compose up --build
```

Builds the images and starts everything in the foreground (add `-d` to run in the background):
`target-service`, the nginx load balancer `lb`, Prometheus, cAdvisor, the `optimizer`, the Streamlit dashboard and Locust.
With `DRY_RUN=true` the optimizer decides and logs but never touches Docker. The first start takes a few minutes.

Check that everything is healthy (second terminal):

```bash
docker compose ps                              # all services "running" / "healthy"
curl -s localhost:8000/health                  # target-service through the load balancer
curl -s localhost:8001/health                  # optimizer: {"status":"ok", ...}
```

| URL | What |
|---|---|
| http://localhost:8000 | app via the load balancer (`/health`, `/work?intensity=low`) |
| http://localhost:9090 | Prometheus (Status, Targets must show `target-service` UP) |
| http://localhost:8080 | cAdvisor |
| http://localhost:8001/health, `/metrics` | optimizer status and its own Prometheus metrics |
| http://localhost:8089 | Locust UI |
| http://localhost:8501 | dashboard |

## 8. Generate load and watch decisions

1. Open http://localhost:8089 and click **Start**. The traffic shape comes from `LOCUST_PROFILE`, not from the form fields.
2. Watch the optimizer decide:

```bash
docker compose logs -f optimizer                      # quiet unless something happens: "[DRY RUN] Set target-service replicas to N" when it would scale, warnings on errors
tail -f data/runtime/scaling_events.jsonl             # structured events: action, from/to, reason, policy
tail -n 1 data/runtime/telemetry_snapshots.jsonl      # latest metrics + forecasts
```

3. Open the dashboard (http://localhost:8501) for live CPU, p95 latency, request rate, replicas, forecasts and events.

What you should see: `cpu_usage` rising with traffic, and `scale_up` events with a `reason` such as
`reactive threshold crossed` once per-replica CPU passes 70%. In dry run the replica count stays at 1; only the events show intent.

If nothing ever scales, the load is too light for one replica. Use a heavier profile or intensity:
`LOCUST_PROFILE=ramp LOCUST_WORK_INTENSITY=medium docker compose up -d locust`, then Start again in the UI.

Switch profile later: edit `.env` (or prefix the variable) and recreate Locust:
`LOCUST_PROFILE=burst docker compose up -d locust`.

## 9. Train the forecasting models (activates predictive scaling)

Until models exist, forecast confidence is 0.0 and the system is **reactive only**. Let step 8 run for at least
**30 minutes** (more is better, ideally across two or three profiles), then:

```bash
make train
```

Runs `python -m optimizer.forecasting.train` on `data/runtime/telemetry_snapshots.jsonl`: builds lag/rolling features,
trains one Random Forest per 1/3/5-minute horizon (target = change in request rate, `--target-mode delta`), saves
`data/models/rf_request_rate_h*.joblib` and `metadata.json`, and writes `data/processed/model_evaluation.csv` comparing
each model with the "rate stays the same" baseline (`mae_improvement_vs_baseline_pct`; negative means the model is worse).

Train on several profile files and hold one out:

```bash
python -m optimizer.forecasting.train --input data/runtime/run_constant.jsonl data/runtime/run_ramp.jsonl \
  --test-input data/runtime/run_burst.jsonl --model-dir data/models
```

(Copy `telemetry_snapshots.jsonl` to a named file after each profile run to build these.)
Load the new models:

```bash
docker compose restart optimizer
docker compose logs optimizer | grep "models_loaded"     # should list [1, 3, 5]
```

Models trained at a different `TELEMETRY_INTERVAL_SECONDS` than the optimizer uses are ignored, with a warning in the log.
Be honest in the evaluation: on unseen traffic shapes the forest did not beat a plain current-rate forecast in the simulation
([`benchmark.md`](benchmark.md)). Check `mae_improvement_vs_baseline_pct` on your own data before claiming a benefit.

## 10. Calibrate the capacity of one replica

```bash
make calibrate
```

Runs `load_testing/calibrate.py` against `localhost:8000`: ramps closed-loop concurrency (1 to 32 workers), prints
requests/s and p95 for each step, and reports the highest throughput within the 500 ms SLA. Do it while exactly one replica
is running and nothing else is sending load (stop Locust first). Put the suggested number in `.env`:

```bash
RPS_PER_REPLICA_CAPACITY=<number printed>
```

then `docker compose up -d optimizer` to apply it. The predictive policy sizes the fleet from this number, so a wrong value
means too many or too few replicas.

## 11. Real scaling

**11a. Prove it works without waiting for load**

```bash
make smoke-scale
```

Stops the optimizer (so it does not fight you), scales `target-service` to 3 replicas through the Docker SDK, sends
60 requests through the load balancer, reads each replica's `/metrics` and prints how many each served, then restores the
original count and restarts the optimizer. `RESULT: PASS` means all of these work: container cloning, readiness check,
DNS discovery by nginx, and load spreading. In another terminal, `docker ps` shows the extra `...-target-service-2/3` containers while it runs.

**11b. Let the optimizer scale by itself**

```bash
make demo-live                    # = DRY_RUN=false docker compose up --build
```

Same stack, but decisions are executed. Start Locust (step 8) with `ramp`, `spike` or `burst` and watch:

```bash
watch -n 2 'docker ps --filter label=com.docker.compose.service=target-service --format "{{.Names}}  {{.Status}}"'
tail -f data/runtime/scaling_events.jsonl        # "dry_run": false, "success": true
```

Replicas appear and disappear as load changes, never below `MIN_REPLICAS` or above `MAX_REPLICAS`, and the original is never removed.
After a scale-up, Prometheus Targets (http://localhost:9090/targets) shows the new replica within about 5 seconds.
`make down` removes the optimizer-created replicas too, since they carry the compose labels.

## 12. Scenarios: what to run and what to expect

| Condition | How to set it up | Expected behaviour |
|---|---|---|
| Safe baseline | default `.env` (`DRY_RUN=true`) | decisions logged, replica count unchanged |
| Reactive only | no files in `data/models/` | `forecast_confidence` 0.0 in telemetry, events have `"policy": "reactive"` |
| Predictive | after step 9 and an optimizer restart | events with `"policy": "predictive"` and a reason like `forecast peak X rps needs N replicas` |
| Predictive that provisions ahead | `PREDICTIVE_MAX_STEP=3` | jumps several replicas at once instead of one per cooldown |
| Steady load | `LOCUST_PROFILE=constant` | scales to a stable count, few events |
| Gradual growth | `ramp` | replicas follow the ramp |
| Sudden spike | `spike` | latency breaches for roughly a minute whatever the policy (see benchmark); replicas rise after |
| Repeating bursts | `burst` | after the first burst, predictive keeps capacity warm; fewer SLA breaches than reactive |
| Load drops | `traffic-drop` | scale-down only after the forecast fits fewer replicas under 50% load and p95 is healthy |
| Heavier requests | `LOCUST_WORK_INTENSITY=medium` | lower capacity per replica, more replicas; recalibrate (step 10) |
| Tight cap | `MAX_REPLICAS=2` | never exceeds 2, even when overloaded |
| Faster demo | `COOLDOWN_SECONDS=30` | actions can follow each other every 30 s |
| Prometheus down | `docker compose stop prometheus` | optimizer `/health` shows `degraded`, loop retries each interval, no scaling action is taken |
| Optimizer cannot reach Docker | `DRY_RUN=false` but no Docker socket mounted | scale attempts fail and are logged as warnings/`"success": false`; the optimizer keeps running (not rehearsed, check the log) |
| cAdvisor missing | `docker compose stop cadvisor` | CPU falls back to `docker stats`; scaling continues |
| Replica fails to start | break the image, then force a scale up | not healthy within `REPLICA_READY_TIMEOUT_SECONDS`: container removed, event logged with `"success": false` |
| Simulation only | `make benchmark` | no Docker; see `benchmark.md` |

Run several in sequence from a clean state with `make reset` between them.

## 13. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `target-service` target is DOWN or missing in Prometheus | check it runs: `docker compose ps`; Prometheus finds replicas via DNS, so it must be on the same network (`optimizer-network`) |
| `curl localhost:8000` gives 502/504 right after start | load balancer started before the app was ready; wait 15 s and retry |
| Optimizer `/health` is `degraded` | read `last_error` in the JSON; usually Prometheus not up yet or no data for a metric |
| CPU stays empty or 0 | cAdvisor unsupported on this host; check the log for the `docker stats` fallback; otherwise lower expectations to request-rate-based scaling |
| Scale-up fails with `did not become ready` | the replica cannot start (memory limit too low, port, image); `docker logs <container>` |
| Never scales although load is on | load too light, `RPS_PER_REPLICA_CAPACITY` far too high, or still in cooldown; check the `reason` in the last events |
| Scales to the max immediately | capacity set too low, or `LOCUST_WORK_INTENSITY` too heavy for the CPU limit (`REPLICA_CPU_LIMIT_CORES`) |
| `make train` says not enough samples | collect more telemetry (30+ minutes) |
| Models ignored after restart | sampling interval mismatch, see the warning in `docker compose logs optimizer`; retrain |
| `permission denied` on `/var/run/docker.sock` | add your user to the `docker` group, or run with sudo (Linux) |

## 14. Stop and reset

```bash
docker compose down        # stop everything, including optimizer-created replicas
make reset                 # same, plus delete the Prometheus data volume
rm -f data/runtime/*.jsonl # forget collected telemetry and events
rm -f data/models/*        # forget trained models (back to reactive-only)
```

## Command reference

| Command | What it does |
|---|---|
| `make up` / `docker compose up --build` | build and start the stack in safe mode |
| `make demo-live` | start the stack with real scaling (`DRY_RUN=false`) |
| `make smoke-scale` | scale to 3, verify load spreads over every replica, restore |
| `make test` | all unit tests, no Docker |
| `make train` | train forecasting models from collected telemetry |
| `make benchmark` | simulated reactive-vs-predictive comparison |
| `make calibrate` | measure one replica's capacity |
| `make down` / `make reset` | stop / stop and delete Prometheus data |
