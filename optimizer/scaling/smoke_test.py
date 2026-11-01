"""Manual check of REAL scaling, without waiting for load to trigger it.

Scales ``target-service`` to N replicas, sends requests through the load balancer, and prints how
many ``/work`` requests each replica actually served. If the load balancer and DNS discovery work,
every replica gets a share. Optionally scales back afterwards.

Run it INSIDE the compose network, with the optimizer service stopped (so it does not fight you):

    docker compose stop optimizer
    docker compose run --rm --no-deps optimizer python -m optimizer.scaling.smoke_test --replicas 3
    docker compose start optimizer
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import requests

from optimizer.config import Settings
from optimizer.scaling.docker_scaler import DockerScaler


_WORK_COUNTER = re.compile(r'^http_requests_total\{[^}]*path="/work"[^}]*\}\s+([0-9.eE+-]+)', re.MULTILINE)


def work_count(metrics_text: str) -> float:
    """Total ``/work`` requests a replica reports in its Prometheus text exposition."""
    return float(sum(float(value) for value in _WORK_COUNTER.findall(metrics_text)))


def replica_addresses(scaler: DockerScaler) -> dict[str, str]:
    result = {}
    for container in scaler._sorted(scaler._running_containers()):  # noqa: SLF001
        address = scaler._ip_address(container)  # noqa: SLF001
        if address:
            result[container.name] = address
    return result


def snapshot(scaler: DockerScaler, port: int) -> dict[str, float]:
    counts = {}
    for name, address in replica_addresses(scaler).items():
        try:
            counts[name] = work_count(requests.get(f"http://{address}:{port}/metrics", timeout=3).text)
        except requests.RequestException:
            counts[name] = float("nan")
    return counts


def run(
    scaler: DockerScaler,
    *,
    replicas: int,
    request_count: int,
    lb_url: str,
    port: int,
    restore: bool,
    settle_seconds: float = 4.0,
    out=print,
) -> bool:
    before = scaler.get_replica_count()
    out(f"replicas before: {before}")
    if not scaler.set_replicas(replicas):
        out("FAILED: scaler could not reach the requested replica count")
        return False
    out(f"replicas after scaling: {scaler.get_replica_count()}")
    for name, address in replica_addresses(scaler).items():
        out(f"  {name}  {address}")

    time.sleep(settle_seconds)  # the load balancer re-resolves DNS every ~2 s
    start = snapshot(scaler, port)
    errors = 0
    for _ in range(request_count):
        try:
            if requests.get(f"{lb_url}/work?intensity=low", timeout=10).status_code >= 500:
                errors += 1
        except requests.RequestException:
            errors += 1
    end = snapshot(scaler, port)

    served = {name: end[name] - start.get(name, 0.0) for name in end}
    out(f"sent {request_count} requests through {lb_url} ({errors} errors); served per replica:")
    for name, count in served.items():
        out(f"  {name}: {count:.0f}")
    balanced = len([c for c in served.values() if c > 0]) == len(served) and errors == 0

    if restore:
        scaler.set_replicas(before)
        out(f"restored to {scaler.get_replica_count()} replica(s)")
    out("RESULT: PASS (every replica served traffic)" if balanced else "RESULT: FAIL (see counts above)")
    return balanced


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--replicas", type=int, default=3)
    parser.add_argument("--requests", type=int, default=60)
    parser.add_argument("--lb-url", default="http://lb")
    parser.add_argument("--no-restore", action="store_true", help="leave the new replicas running")
    args = parser.parse_args()

    settings = Settings()
    scaler = DockerScaler(
        settings.target_service_name,
        dry_run=False,
        mode="compose",
        port=settings.target_service_port,
        health_path=settings.target_health_path,
        ready_timeout_seconds=settings.replica_ready_timeout_seconds,
        drain_seconds=settings.scale_down_drain_seconds,
    )
    ok = run(
        scaler,
        replicas=args.replicas,
        request_count=args.requests,
        lb_url=args.lb_url.rstrip("/"),
        port=settings.target_service_port,
        restore=not args.no_restore,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
