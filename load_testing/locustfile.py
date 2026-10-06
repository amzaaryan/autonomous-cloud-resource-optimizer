"""Locust workload profiles for the target service."""

from __future__ import annotations

import os

from locust import HttpUser, LoadTestShape, between, task


DEFAULT_INTENSITY = os.getenv("LOCUST_WORK_INTENSITY", "medium")
PROFILE = os.getenv("LOCUST_PROFILE", "constant").strip().lower()


class TargetUser(HttpUser):
    wait_time = between(0.05, 0.5)

    @task(10)
    def work(self):
        self.client.get(f"/work?intensity={DEFAULT_INTENSITY}", name="work")

    @task(1)
    def health(self):
        self.client.get("/health", name="health")


class WorkloadShape(LoadTestShape):
    """Reusable profile selector via LOCUST_PROFILE env var."""

    time_limit = 30 * 60

    def tick(self):
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
