.PHONY: up down test train

up:
	docker compose up --build

down:
	docker compose down

test:
	python -m pytest -q OR docker compose run --rm optimizer python -m pytest -q

train:
	python -m optimizer.forecasting.train --input data/runtime/telemetry_snapshots.jsonl --model-dir data/models --report data/processed/model_evaluation.csv
