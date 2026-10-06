"""Smoke tests for the target FastAPI service."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_work():
    response = client.get("/work?intensity=low")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_work_accepts_iteration_override():
    response = client.get("/work?intensity=low&iterations=100")
    assert response.status_code == 200
    assert response.json()["iterations"] == 100


def test_metrics():
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "http_requests_total" in response.text
