"""Traffic presets for the demo control panel (pure logic, no Locust import).

The dashboard writes ``{"preset": <key>, "started_at": <epoch>}`` to the control file; the Locust
load shape calls :func:`target` every second with the elapsed time to learn how many users to run.
Keeping this free of Locust makes it unit-testable and importable by the dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass

SPAWN_RATE = 20  # users/s: step changes show up within a few seconds


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    description: str
    steps: tuple[tuple[float, int], ...]  # (start_second, users), sorted by start
    cycle_seconds: float | None = None  # repeat the steps with this period
    paused: bool = False  # keep one idle user, send no requests


@dataclass(frozen=True)
class Target:
    users: int
    spawn_rate: int
    paused: bool


_RAMP = tuple((i * 15.0, min(120, 10 + 7 * i)) for i in range(17))  # 10 -> 120 users over 4 minutes

# 25 minutes of deliberately varied traffic (up, down, jumps) so the forecaster sees many shapes.
_WARMUP = (
    (0, 10), (60, 40), (150, 90), (240, 20), (330, 120), (420, 60), (510, 10), (570, 100),
    (660, 30), (750, 80), (840, 15), (930, 110), (1020, 50), (1110, 25), (1200, 95), (1290, 10), (1380, 70),
)

PRESETS: dict[str, Preset] = {
    p.key: p
    for p in (
        Preset("idle", "Idle", "No traffic", ((0, 1),), paused=True),
        Preset("low", "Low", "Light steady load (8 users)", ((0, 8),)),
        Preset("medium", "Medium", "Moderate steady load (30 users)", ((0, 30),)),
        Preset("high", "High", "Heavy steady load (100 users)", ((0, 100),)),
        Preset("ramp", "Ramp", "Load grows from 10 to 120 users over 4 minutes", _RAMP),
        Preset("spike", "Spike", "10 users, a sudden jump to 120 after 30 s, back to 10 after 150 s", ((0, 10), (30, 120), (150, 10))),
        Preset("burst", "Burst", "Repeating 100-user bursts (40 s) every 100 s on a 25-user base", ((0, 25), (60, 100)), cycle_seconds=100),
        Preset("drop", "Traffic drop", "100 users for 90 s, then drops to 8", ((0, 100), (90, 8))),
        Preset(
            "demo",
            "Demo script",
            "7.5 min scripted story: quiet, big spike, quiet, second spike, quiet",
            ((0, 10), (90, 100), (240, 15), (360, 100), (450, 10)),
        ),
        Preset("warmup", "Training traffic", "25 min of varied traffic, repeats; use it so the model has data to learn from", _WARMUP, cycle_seconds=1500),
    )
}
DEFAULT_PRESET = "low"


def target(preset_key: str, elapsed_seconds: float) -> Target:
    preset = PRESETS.get(preset_key) or PRESETS[DEFAULT_PRESET]
    elapsed = max(0.0, float(elapsed_seconds))
    if preset.cycle_seconds:
        elapsed %= preset.cycle_seconds
    users = preset.steps[0][1]
    for start, value in preset.steps:
        if elapsed >= start:
            users = value
        else:
            break
    return Target(users=int(users), spawn_rate=SPAWN_RATE, paused=preset.paused)
