from fastapi import FastAPI, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
import time

app = FastAPI(title="Target Workload Service")
REQUESTS = Counter("http_requests_total", "Total HTTP requests", ["method", "path", "status"])
LATENCY = Histogram("http_request_duration_seconds", "Request duration", ["method", "path"])


@app.get("/health")
def health():
    REQUESTS.labels("GET", "/health", "200").inc()
    return {"status": "ok"}


@app.get("/work")
def work(intensity: str = "medium"):
    start = time.perf_counter()
    iterations = {"low": 50_000, "medium": 250_000, "high": 1_000_000}.get(intensity, 250_000)
    value = 0
    for number in range(iterations):
        value = (value + number * number) % 1_000_003
    duration = time.perf_counter() - start
    LATENCY.labels("GET", "/work").observe(duration)
    REQUESTS.labels("GET", "/work", "200").inc()
    return {"status": "ok", "intensity": intensity, "result": value, "duration_seconds": duration}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
