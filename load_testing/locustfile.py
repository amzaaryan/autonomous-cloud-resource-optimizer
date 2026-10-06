"""Locust workload scaffold."""

from locust import HttpUser, between, task


class TargetUser(HttpUser):
    wait_time = between(0.1, 1.0)

    @task(8)
    def work(self):
        self.client.get("/work", name="work")

    @task(1)
    def health(self):
        self.client.get("/health", name="health")


# TODO: Add separate constant, ramp, spike, burst, and traffic-drop profiles.
