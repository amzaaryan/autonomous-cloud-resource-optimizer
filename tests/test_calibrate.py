import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "load_testing"))

from calibrate import percentile, pick_capacity  # noqa: E402


def test_percentile():
    assert percentile(list(range(1, 101)), 0.95) == 95
    assert math.isnan(percentile([], 0.95))


def test_pick_capacity_ignores_steps_that_breach_sla_or_error():
    steps = [
        {"workers": 1, "rps": 40, "p95_ms": 30, "errors": 0},
        {"workers": 4, "rps": 80, "p95_ms": 200, "errors": 0},
        {"workers": 16, "rps": 95, "p95_ms": 2000, "errors": 0},
        {"workers": 32, "rps": 99, "p95_ms": 100, "errors": 3},
    ]
    assert pick_capacity(steps, 500) == 80
    assert pick_capacity(steps, 10) is None
