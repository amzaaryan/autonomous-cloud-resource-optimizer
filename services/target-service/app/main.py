from fastapi import FastAPI, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
import os
import time

app = FastAPI(title="Target Workload Service")
REQUESTS = Counter("http_requests_total", "Total HTTP requests", ["method", "path", "status"])
LATENCY = Histogram("http_request_duration_seconds", "Request duration", ["method", "path"])

BASE_ITERATIONS = {
    "low": int(os.getenv("WORK_INTENSITY_LOW_ITERATIONS", "50000")),
    "medium": int(os.getenv("WORK_INTENSITY_MEDIUM_ITERATIONS", "250000")),
    "high": int(os.getenv("WORK_INTENSITY_HIGH_ITERATIONS", "1000000")),
}


@app.get("/health")
def health():
    REQUESTS.labels("GET", "/health", "200").inc()
    return {"status": "ok"}


@app.get("/work")
def work(intensity: str = "medium", iterations: int | None = None):
    start = time.perf_counter()
    default_iterations = BASE_ITERATIONS.get(intensity, BASE_ITERATIONS["medium"])
    total_iterations = max(1, iterations or default_iterations)

    value = 0
    for number in range(total_iterations):
        value = (value + number * number) % 1_000_003

    duration = time.perf_counter() - start
    LATENCY.labels("GET", "/work").observe(duration)
    REQUESTS.labels("GET", "/work", "200").inc()
    return {
        "status": "ok",
        "intensity": intensity,
        "iterations": total_iterations,
        "result": value,
        "duration_seconds": duration,
    }


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
