"""Docker Engine scaling adapter.

The initial implementation is intentionally conservative. Set DRY_RUN=false only
when the Docker scaling path has been tested safely.
"""

from __future__ import annotations

import logging

import docker
from docker.errors import DockerException, NotFound


LOGGER = logging.getLogger(__name__)


class DockerScaler:
    def __init__(self, service_name: str, dry_run: bool = True, mode: str = "compose") -> None:
        self.service_name = service_name
        self.dry_run = dry_run
        self.mode = mode
        try:
            self.client = docker.from_env()
        except DockerException:
            self.client = None

    def _compose_replica_count(self) -> int:
        if self.client is None:
            return 1
        containers = self.client.containers.list(
            filters={
                "label": f"com.docker.compose.service={self.service_name}",
                "status": "running",
            }
        )
        return max(1, len(containers))

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

    def get_replica_count(self) -> int:
        if self.mode == "swarm":
            return self._swarm_replica_count()
        return self._compose_replica_count()

    def set_replicas(self, replicas: int) -> bool:
        if replicas < 1:
            raise ValueError("replicas must be >= 1")

        if self.dry_run:
            LOGGER.info("[DRY RUN] Set %s replicas to %d", self.service_name, replicas)
            return True

        if self.client is None:
            LOGGER.warning("Docker client unavailable; cannot scale")
            return False

        if self.mode != "swarm":
            LOGGER.warning(
                "Compose mode does not provide reliable in-place scaling through SDK. "
                "Use DRY_RUN=true or SCALING_MODE=swarm."
            )
            return False

        try:
            service = self.client.services.get(self.service_name)
            service.scale(replicas)
            LOGGER.info("Scaled swarm service %s to %d replicas", self.service_name, replicas)
            return True
        except (NotFound, DockerException):
            LOGGER.exception("Failed to scale service %s", self.service_name)
            return False
