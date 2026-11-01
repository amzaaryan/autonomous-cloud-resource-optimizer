.PHONY: up down test train benchmark calibrate demo-live smoke-scale reset

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
	python load_testing/calibrate.py --url 'http://localhost:8000/work?intensity=low'

# REAL scaling: the optimizer starts/stops target-service containers. Needs the Docker socket.
demo-live:
	DRY_RUN=false docker compose up --build

# Proves real scaling + load balancing WITHOUT waiting for load: scales to 3, sends requests via the
# LB, prints how many each replica served (PASS only if every replica served some), restores.
smoke-scale:
	docker compose stop optimizer; \
	docker compose run --rm --no-deps optimizer python -m optimizer.scaling.smoke_test --replicas 3; \
	rc=$$?; docker compose start optimizer; exit $$rc

# Stop everything (including optimizer-created replicas) and delete the Prometheus volume
reset:
	docker compose down -v
