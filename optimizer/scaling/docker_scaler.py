"""Docker Engine scaling adapter.

Two real scaling paths, both driven through the Docker SDK:

``compose`` (what you get from a plain ``docker compose up``)
    Compose has no in-place scale API, so replicas are sibling containers cloned from the
    running compose container of the service (the "template" = the lowest container-number):
    same image, env, command, CPU/memory limits, compose project/service labels, and the same
    network *with the service name as a network alias*. Docker's embedded DNS therefore returns
    every replica for ``target-service``, which the nginx load balancer and Prometheus
    (``dns_sd_configs``) both use for discovery. No host ports are published on replicas.
    Cloned containers keep the compose labels, so ``docker compose down`` removes them.

``swarm``
    ``service.scale(n)`` on a Swarm service.

``DRY_RUN=true`` (the default) only logs the intended change.
"""

from __future__ import annotations

import logging
import time

import docker
import requests
from docker.errors import DockerException, NotFound


LOGGER = logging.getLogger(__name__)

COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
COMPOSE_NUMBER_LABEL = "com.docker.compose.container-number"
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
MANAGED_LABEL = "optimizer.managed-replica"


class DockerScaler:
    def __init__(
        self,
        service_name: str,
        dry_run: bool = True,
        mode: str = "compose",
        *,
        port: int = 8000,
        health_path: str = "/health",
        ready_timeout_seconds: float = 45.0,
        drain_seconds: float = 6.0,
    ) -> None:
        self.service_name = service_name
        self.dry_run = dry_run
        self.mode = mode
        self.port = port
        self.health_path = health_path
        self.ready_timeout_seconds = ready_timeout_seconds
        self.drain_seconds = drain_seconds
        try:
            self.client = docker.from_env()
        except DockerException:
            self.client = None

    # ---- discovery -------------------------------------------------------------------

    def _running_containers(self) -> list:
        return self.client.containers.list(
            filters={
                "label": f"{COMPOSE_SERVICE_LABEL}={self.service_name}",
                "status": "running",
            }
        )

    @staticmethod
    def _number(container) -> int:
        try:
            return int((container.labels or {}).get(COMPOSE_NUMBER_LABEL, "0"))
        except (TypeError, ValueError):
            return 0

    def _sorted(self, containers: list) -> list:
        return sorted(containers, key=self._number)

    def _compose_replica_count(self) -> int:
        if self.client is None:
            return 1
        return max(1, len(self._running_containers()))

    def _swarm_replica_count(self) -> int:
        if self.client is None:
            return 1
        try:
            service = self.client.services.get(self.service_name)
        except NotFound:
            return 1
        mode = service.attrs.get("Spec", {}).get("Mode", {}).get("Replicated", {})
        replicas = mode.get("Replicas")
        return int(replicas) if replicas is not None else 1

    def get_resource_usage(self) -> tuple[float | None, float | None]:
        """Aggregate CPU (in CORES, summed over replicas) and memory ratio, via ``docker stats``.

        Only used when Prometheus/cAdvisor has no per-service data.
        """
        if self.client is None:
            return None, None
        containers = self._running_containers()
        cpu_total = 0.0
        memory_used = 0
        memory_limit = 0
        for container in containers:
            try:
                stats = container.stats(stream=False)
                cpu = stats.get("cpu_stats", {})
                previous = stats.get("precpu_stats", {})
                cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - previous.get("cpu_usage", {}).get("total_usage", 0)
                system_delta = cpu.get("system_cpu_usage", 0) - previous.get("system_cpu_usage", 0)
                online_cpus = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage", []) or []) or 1
                if system_delta > 0 and cpu_delta >= 0:
                    cpu_total += (cpu_delta / system_delta) * online_cpus
                memory = stats.get("memory_stats", {})
                memory_used += int(memory.get("usage", 0))
                memory_limit += int(memory.get("limit", 0))
            except (DockerException, KeyError, TypeError, ValueError):
                LOGGER.exception("Failed reading resource stats for %s", container.name)
        return cpu_total, (memory_used / memory_limit if memory_limit else None)

    def get_replica_count(self) -> int:
        if self.mode == "swarm":
            return self._swarm_replica_count()
        return self._compose_replica_count()

    # ---- scaling ---------------------------------------------------------------------

    def set_replicas(self, replicas: int) -> bool:
        if replicas < 1:
            raise ValueError("replicas must be >= 1")

        if self.dry_run:
            LOGGER.info("[DRY RUN] Set %s replicas to %d", self.service_name, replicas)
            return True

        if self.client is None:
            LOGGER.warning("Docker client unavailable; cannot scale")
            return False

        if self.mode == "swarm":
            return self._scale_swarm(replicas)
        return self._scale_compose(replicas)

    def _scale_swarm(self, replicas: int) -> bool:
        try:
            service = self.client.services.get(self.service_name)
            service.scale(replicas)
            LOGGER.info("Scaled swarm service %s to %d replicas", self.service_name, replicas)
            return True
        except (NotFound, DockerException):
            LOGGER.exception("Failed to scale service %s", self.service_name)
            return False

    def _scale_compose(self, desired: int) -> bool:
        try:
            running = self._sorted(self._running_containers())
            if not running:
                LOGGER.warning("No running container for compose service %s to clone", self.service_name)
                return False

            current = len(running)
            if desired == current:
                return True

            if desired > current:
                added = 0
                for _ in range(desired - current):
                    if not self._add_replica(running):
                        break
                    added += 1
                    running = self._sorted(self._running_containers())
                LOGGER.info("Scaled %s %d -> %d replicas (%d added)", self.service_name, current, len(running), added)
                return added == desired - current

            removable = list(reversed(running[1:]))  # newest first; never remove the original
            victims = removable[: current - desired]
            self._remove_replicas(victims)
            LOGGER.info("Scaled %s %d -> %d replicas", self.service_name, current, current - len(victims))
            return len(victims) == current - desired
        except DockerException:
            LOGGER.exception("Failed to scale compose service %s", self.service_name)
            return False

    def _add_replica(self, running: list) -> bool:
        template = running[0]
        attrs = template.attrs or {}
        config = attrs.get("Config", {})
        host = attrs.get("HostConfig", {})
        networks = (attrs.get("NetworkSettings", {}) or {}).get("Networks") or {}
        if not networks or not config.get("Image"):
            LOGGER.warning("Template container %s lacks network/image details; cannot clone", template.name)
            return False

        number = max(self._number(c) for c in running) + 1
        labels = dict(config.get("Labels") or {})
        labels[COMPOSE_NUMBER_LABEL] = str(number)
        labels[MANAGED_LABEL] = "true"
        project = labels.get(COMPOSE_PROJECT_LABEL)
        name = f"{project}-{self.service_name}-{number}" if project else f"{self.service_name}-{number}"

        network = next(iter(networks))
        api = self.client.api
        restart = host.get("RestartPolicy") or {}
        host_config = api.create_host_config(
            network_mode=network,
            restart_policy=(
                {"Name": restart["Name"], "MaximumRetryCount": restart.get("MaximumRetryCount", 0)}
                if restart.get("Name")
                else None
            ),
            nano_cpus=host.get("NanoCpus") or None,
            mem_limit=host.get("Memory") or None,
            cpu_shares=host.get("CpuShares") or None,
        )
        networking_config = api.create_networking_config(
            {network: api.create_endpoint_config(aliases=[self.service_name])}
        )

        created = api.create_container(
            image=config["Image"],
            command=config.get("Cmd"),
            entrypoint=config.get("Entrypoint"),
            environment=config.get("Env"),
            working_dir=config.get("WorkingDir") or None,
            user=config.get("User") or None,
            labels=labels,
            name=name,
            host_config=host_config,
            networking_config=networking_config,
        )
        container_id = created["Id"]
        api.start(container_id)
        container = self.client.containers.get(container_id)

        if self._wait_ready(container):
            LOGGER.info("Replica %s is ready", name)
            return True

        LOGGER.warning("Replica %s did not become ready within %ss; discarding it", name, self.ready_timeout_seconds)
        self._discard(container)
        return False

    def _remove_replicas(self, victims: list) -> None:
        # 1) detach from the network so Docker DNS stops returning them,
        # 2) give the load balancer's resolver cache and in-flight requests time to clear,
        # 3) stop and remove.
        for container in victims:
            networks = (container.attrs.get("NetworkSettings", {}) or {}).get("Networks") or {}
            for network_name in networks:
                try:
                    self.client.networks.get(network_name).disconnect(container)
                except DockerException:
                    LOGGER.warning("Could not detach %s from %s", container.name, network_name)
        if victims and self.drain_seconds > 0:
            time.sleep(self.drain_seconds)
        for container in victims:
            self._discard(container)

    @staticmethod
    def _discard(container) -> None:
        try:
            container.stop(timeout=10)
        except DockerException:
            LOGGER.warning("Could not stop %s cleanly", container.name)
        try:
            container.remove(force=True)
        except DockerException:
            LOGGER.warning("Could not remove %s", container.name)

    # ---- readiness -------------------------------------------------------------------

    def _wait_ready(self, container) -> bool:
        deadline = time.monotonic() + self.ready_timeout_seconds
        while time.monotonic() < deadline:
            try:
                container.reload()
            except DockerException:
                return False
            if container.status not in {"running", "created", "restarting"}:
                return False
            address = self._ip_address(container)
            if container.status == "running" and address and self._http_ok(address):
                return True
            time.sleep(1.0)
        return False

    @staticmethod
    def _ip_address(container) -> str | None:
        networks = (container.attrs.get("NetworkSettings", {}) or {}).get("Networks") or {}
        for details in networks.values():
            address = (details or {}).get("IPAddress")
            if address:
                return address
        return None

    def _http_ok(self, address: str) -> bool:
        try:
            response = requests.get(f"http://{address}:{self.port}{self.health_path}", timeout=2)
            return response.status_code < 500
        except requests.RequestException:
            return False
