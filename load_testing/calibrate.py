"""Measure how many requests/second ONE replica sustains, to set RPS_PER_REPLICA_CAPACITY.

The predictive policy sizes the fleet as ``ceil(forecast_rps / (capacity * target_utilization))``,
so that number must come from this machine and this ``LOCUST_WORK_INTENSITY``, not a guess.

Run against a SINGLE replica (``docker compose up -d`` without scaling; the load balancer in
front of one replica is fine, or point ``--url`` at the service directly):

    python load_testing/calibrate.py --url http://localhost:8000/work?intensity=low

It ramps closed-loop concurrency (each worker sends its next request when the last returns),
records throughput and p95 latency per step, and reports the highest throughput whose p95 stays
under ``--sla-ms``. Capacity is that throughput; the script prints the env line to use.
Standard library only.
"""

from __future__ import annotations

import argparse
import math
import threading
import time
import urllib.error
import urllib.request


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def run_step(url: str, workers: int, seconds: float, timeout: float) -> dict[str, float]:
    latencies: list[float] = []
    errors = 0
    lock = threading.Lock()
    deadline = time.monotonic() + seconds

    def worker() -> None:
        nonlocal errors
        while time.monotonic() < deadline:
            started = time.monotonic()
            try:
                with urllib.request.urlopen(url, timeout=timeout) as response:
                    response.read()
                    ok = response.status < 500
            except (urllib.error.URLError, OSError):
                ok = False
            elapsed = time.monotonic() - started
            with lock:
                if ok:
                    latencies.append(elapsed * 1000.0)
                else:
                    errors += 1

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    begin = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall = max(time.monotonic() - begin, 1e-9)
    return {
        "workers": workers,
        "rps": len(latencies) / wall,
        "p95_ms": percentile(latencies, 0.95),
        "errors": errors,
    }


def pick_capacity(steps: list[dict[str, float]], sla_ms: float) -> float | None:
    """Highest throughput among steps that met the latency SLA with no errors."""
    good = [s["rps"] for s in steps if s["errors"] == 0 and not math.isnan(s["p95_ms"]) and s["p95_ms"] <= sla_ms]
    return max(good) if good else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8000/work?intensity=low")
    parser.add_argument("--workers", default="1,2,4,8,16,32", help="comma-separated concurrency steps")
    parser.add_argument("--seconds", type=float, default=15.0, help="duration of each step")
    parser.add_argument("--sla-ms", type=float, default=500.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    steps = []
    print(f"{'workers':>8} {'rps':>9} {'p95 ms':>9} {'errors':>7}")
    for workers in (int(part) for part in args.workers.split(",")):
        step = run_step(args.url, workers, args.seconds, args.timeout)
        steps.append(step)
        print(f"{workers:>8} {step['rps']:>9.1f} {step['p95_ms']:>9.1f} {step['errors']:>7}")

    capacity = pick_capacity(steps, args.sla_ms)
    if capacity is None:
        print("\nNo step met the SLA. Lower the work intensity or raise --sla-ms.")
        return
    print(f"\nMax throughput within p95 <= {args.sla_ms:.0f} ms: {capacity:.0f} rps")
    print(f"Suggested:  RPS_PER_REPLICA_CAPACITY={int(capacity)}   (policy then targets 70% of this per replica)")
    print("Note: this is for ONE replica at this intensity; recalibrate if you change either.")


if __name__ == "__main__":
    main()
