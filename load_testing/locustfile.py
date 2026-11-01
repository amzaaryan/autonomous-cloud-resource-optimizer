"""Locust workload profiles for the target service."""

from __future__ import annotations

import json
import os
import sys
import time

from locust import HttpUser, LoadTestShape, between, task

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import presets  # noqa: E402  (pure logic shared with the dashboard)


DEFAULT_INTENSITY = os.getenv("LOCUST_WORK_INTENSITY", "medium")
# "panel" (default in docker-compose): traffic is driven live from the dashboard's control file.
# constant / ramp / spike / burst / traffic-drop: the original fixed profiles.
PROFILE = os.getenv("LOCUST_PROFILE", "constant").strip().lower()
CONTROL_PATH = os.getenv("CONTROL_PATH", "/mnt/control/control.json")
PANEL = PROFILE == "panel"

_control_cache: dict = {"read_at": 0.0, "value": {}}


def control() -> dict:
    """Control file, re-read at most once a second; a missing/bad file means defaults."""
    now = time.time()
    if now - _control_cache["read_at"] > 1.0:
        try:
            with open(CONTROL_PATH, encoding="utf-8") as handle:
                _control_cache["value"] = json.load(handle).get("traffic", {})
        except (OSError, ValueError, AttributeError):
            _control_cache["value"] = {}
        _control_cache["read_at"] = now
    return _control_cache["value"]


def panel_target() -> presets.Target:
    traffic = control()
    started = float(traffic.get("started_at") or 0.0)
    elapsed = time.time() - started if started else 0.0
    return presets.target(traffic.get("preset", presets.DEFAULT_PRESET), elapsed)


class TargetUser(HttpUser):
    wait_time = between(0.05, 0.5)

    @task(10)
    def work(self):
        intensity = DEFAULT_INTENSITY
        if PANEL:
            if panel_target().paused:
                time.sleep(1)  # "Idle": keep the user alive but send nothing
                return
            intensity = control().get("intensity", DEFAULT_INTENSITY)
        self.client.get(f"/work?intensity={intensity}", name="work")

    @task(1)
    def health(self):
        self.client.get("/health", name="health")


class WorkloadShape(LoadTestShape):
    """Reusable profile selector via LOCUST_PROFILE env var."""

    time_limit = None if PANEL else 30 * 60

    def tick(self):
        if PANEL:
            wanted = panel_target()
            return wanted.users, wanted.spawn_rate

        run_time = int(self.get_run_time())
        if PROFILE == "constant":
            return 25, 3

        if PROFILE == "ramp":
            users = min(120, 10 + run_time // 10)
            return users, 5

        if PROFILE == "spike":
            if run_time < 60:
                return 20, 5
            if run_time < 120:
                return 150, 30
            return 30, 10

        if PROFILE == "burst":
            cycle = run_time % 180
            if cycle < 30:
                return 100, 20
            return 25, 5

        if PROFILE == "traffic-drop":
            if run_time < 120:
                return 80, 15
            return 5, 2

        return 25, 3
