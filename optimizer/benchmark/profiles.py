"""Traffic profiles for the benchmark.

``PROFILES`` mirrors ``LOCUST_PROFILE`` in ``load_testing/locustfile.py`` exactly (target
users, spawn rate) as a function of seconds since the test started. Keep the two in sync.
"""

from __future__ import annotations

from typing import Callable

import numpy as np


DURATION_SECONDS = 30 * 60  # Locust's WorkloadShape.time_limit

Profile = Callable[[int], "tuple[int, int]"]


def _constant(t: int) -> tuple[int, int]:
    return 25, 3


def _ramp(t: int) -> tuple[int, int]:
    return min(120, 10 + t // 10), 5


def _spike(t: int) -> tuple[int, int]:
    if t < 60:
        return 20, 5
    if t < 120:
        return 150, 30
    return 30, 10


def _burst(t: int) -> tuple[int, int]:
    return (100, 20) if t % 180 < 30 else (25, 5)


def _traffic_drop(t: int) -> tuple[int, int]:
    return (80, 15) if t < 120 else (5, 2)


PROFILES: dict[str, Profile] = {
    "constant": _constant,
    "ramp": _ramp,
    "spike": _spike,
    "burst": _burst,
    "traffic-drop": _traffic_drop,
}


def jittered(name: str, rng: np.random.Generator) -> Profile:
    """A varied copy of a profile (different height, speed, phase) used ONLY to build training data."""
    base = PROFILES[name]
    amplitude = float(rng.uniform(0.7, 1.3))
    stretch = float(rng.uniform(0.8, 1.25))
    shift = int(rng.integers(0, 60))

    def profile(t: int) -> tuple[int, int]:
        users, spawn = base(int((t + shift) / stretch))
        return max(1, round(users * amplitude)), max(1, round(spawn * amplitude))

    return profile
