The CPU and memory issue is fixed. The optimizer now reads resource statistics directly from the Docker Engine API, exposes them through Prometheus gauges, and the dashboard displays percentages.

Current verified values:

- CPU: approximately `103.1%`
- Memory: approximately `1.3%`
- Dashboard: healthy on `http://localhost:8501`
- Optimizer: healthy on `http://localhost:8001`
- Locust: healthy on `http://localhost:8089`

CPU above `100%` means the target container is using slightly more than one CPU core. Memory is the target container’s usage divided by its Docker memory limit.

## Demo preparation

Run the full stack with:

```powershell
docker compose up --pull always --build -d
```

Confirm the services:

```powershell
docker compose ps
```

Open these pages:

- Dashboard: `http://localhost:8501`
- Locust: `http://localhost:8089`
- Prometheus: `http://localhost:9090`
- Target API: `http://localhost:8000`
- Optimizer health: `http://localhost:8001/health`

Use the dashboard’s refresh control at around 15 seconds.

## Step 1: Explain the architecture

Present the flow:

```text
Locust
   |
   v
target-service
   |
   +--> Prometheus application metrics
   |
   +--> Docker Engine CPU and memory statistics
             |
             v
          Optimizer
             |
             v
          Dashboard
```

Explain the roles:

1. **Target service** processes `/work` requests.
2. **Locust** generates configurable traffic.
3. **Prometheus** stores application and optimizer metrics.
4. **Optimizer** collects telemetry and evaluates scaling policy.
5. **Dashboard** displays live metrics, history, forecasts, policy state, and events.
6. **cAdvisor** remains available for container metrics, but Docker Engine statistics are used as the reliable fallback on this Docker Desktop environment.

## Step 2: Start with a baseline

Open the Locust page and start a constant profile.

The current default profile produces:

- 25 users
- Spawn rate of 3 users per second
- Medium work intensity
- A mixture of `/work` and `/health` requests

On the dashboard, point out:

- **CPU** shows the target container’s CPU ratio.
- **Memory** shows target container memory usage.
- **RPS** shows request throughput.
- **p95 latency** shows the slowest 5% request boundary.
- **Error rate** should remain near zero.
- **Replicas** shows the currently observed replica count.
- **Policy state** shows whether the optimizer selected reactive or predictive logic.

Wait at least 30 seconds so Prometheus and the optimizer collect several samples.

## Step 3: Demonstrate a workload spike

Edit `.env` and set:

```env
LOCUST_PROFILE=spike
LOCUST_WORK_INTENSITY=high
```

Recreate only Locust:

```powershell
docker compose up -d --force-recreate locust
```

The spike profile behaves as follows:

- First minute: 20 users
- Second minute: 150 users
- After two minutes: 30 users

Watch the dashboard during the high-load period.

Expected observations:

- RPS increases.
- CPU rises.
- Memory may rise gradually.
- p95 latency may increase.
- Forecast values increase.
- The optimizer policy may change.
- Scaling events may appear in the event table.
- Since `DRY_RUN=true`, scaling is simulated and does not actually create replicas.

For a shorter evaluator demonstration, temporarily change the spike profile timings in `load_testing/locustfile.py`, for example:

```python
if run_time < 30:
    return 20, 5
if run_time < 60:
    return 150, 30
return 30, 10
```

Then rebuild or recreate the Locust service:

```powershell
docker compose up -d --build --force-recreate locust
```

## Step 4: Demonstrate a burst workload

Set:

```env
LOCUST_PROFILE=burst
LOCUST_WORK_INTENSITY=high
```

Restart Locust:

```powershell
docker compose up -d --force-recreate locust
```

The burst profile alternates between:

- 100 users for 30 seconds
- 25 users for the rest of a three-minute cycle

Use this profile to explain how the optimizer responds to changing load rather than a constant request rate.

## Step 5: Demonstrate traffic drop

Set:

```env
LOCUST_PROFILE=traffic-drop
LOCUST_WORK_INTENSITY=medium
```

Restart Locust:

```powershell
docker compose up -d --force-recreate locust
```

The traffic-drop profile changes from:

- 80 users initially
- 5 users after two minutes

Observe:

- RPS decreases.
- CPU trends downward.
- Memory may decrease more slowly.
- The forecast follows the lower request rate.
- The optimizer can evaluate scale-down conditions.
- Latency and error-rate safeguards remain visible in the policy reasoning.

## Step 6: Explain dashboard sections

### Top metric cards

Use these as the live system summary:

- **Replicas**: target-service replica count.
- **CPU**: Docker Engine CPU usage percentage.
- **Memory**: Docker memory usage percentage.
- **RPS**: current request rate.
- **p95 latency**: latency SLA indicator.
- **Error rate**: failed request ratio.

### Historical telemetry

Explain that the chart is built from optimizer snapshots written to:

```text
data/runtime/telemetry_snapshots.jsonl
```

The chart lets you correlate:

```text
load increase -> CPU/memory increase -> latency change -> scaling decision
```

### Forecasts

The optimizer produces request-rate forecasts for:

- 1 minute
- 3 minutes
- 5 minutes

Explain that the forecasts are used by the predictive policy when sufficient data or trained models are available.

### Scaling events

The event table records:

- Current replica count
- Desired replica count
- Scaling action
- Policy type
- Reason
- Dry-run status
- Success status

Because the project defaults to `DRY_RUN=true`, explain that the demo safely shows decisions without modifying the deployment.

### Policy state and optimizer health

Show:

- Optimizer status
- Last loop time
- Last error
- Reactive or predictive policy
- Estimated cost
- Estimated savings

Clarify that cost and savings are educational local estimates, not cloud billing.

## Step 7: Verify metrics during the presentation

If the evaluator asks how the dashboard receives CPU and memory data, run:

```powershell
Invoke-WebRequest http://localhost:8001/metrics -UseBasicParsing |
  Select-Object -ExpandProperty Content |
  Select-String "optimizer_target_(cpu|memory)_usage"
```

You should see:

```text
optimizer_target_cpu_usage
optimizer_target_memory_usage
```

You can also show recent telemetry:

```powershell
Get-Content data/runtime/telemetry_snapshots.jsonl -Tail 5
```

The latest records should contain numeric values for:

```json
"cpu_usage": ...
"memory_usage": ...
```

## Recommended presentation sequence

1. Show the architecture.
2. Show the baseline dashboard.
3. Start the `spike` profile.
4. Point out rising RPS, CPU, memory, and latency.
5. Explain forecast horizons.
6. Show the scaling event and dry-run behavior.
7. Switch to `traffic-drop`.
8. Show the reduced load and policy response.
9. Explain Docker Engine metrics and Prometheus integration.
10. End by highlighting safe scaling controls and predictive fallback behavior.

Next: shorten the spike profile timings for a faster presentation, pre-generate a clean baseline before evaluation, and capture the dashboard plus Locust windows side by side.