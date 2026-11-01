.PHONY: up down test train benchmark calibrate demo-live

up:
	docker compose up --build

down:
	docker compose down

test:
	python -m pytest -q

train:
	python -m optimizer.forecasting.train --input data/runtime/telemetry_snapshots.jsonl --model-dir data/models --report data/processed/model_evaluation.csv

# Reactive-vs-predictive comparison on the simulated plant (no Docker needed, ~5 min with ML)
benchmark:
	python -m optimizer.benchmark.simulate --seeds 5 --out docs/results --plot-dir docs/images

# Measure one replica's capacity, then put the result in .env as RPS_PER_REPLICA_CAPACITY
calibrate:
	python load_testing/calibrate.py --url http://localhost:8000/work?intensity=low

# REAL scaling: the optimizer starts/stops target-service containers. Needs the Docker socket.
demo-live:
	DRY_RUN=false docker compose up --build
