"""Docker Engine scaling adapter.

The initial implementation is intentionally conservative. Set DRY_RUN=false only
when the Docker scaling path has been tested safely.
"""

import docker


class DockerScaler:
    def __init__(self, service_name: str, dry_run: bool = True) -> None:
        self.service_name = service_name
        self.dry_run = dry_run
        self.client = docker.from_env()

    def get_replica_count(self) -> int:
        # TODO: Implement container/service discovery for the selected orchestration mode.
        return 1

    def set_replicas(self, replicas: int) -> None:
        if self.dry_run:
            print(f"[DRY RUN] Set {self.service_name} replicas to {replicas}")
            return
        # TODO: Implement Docker Compose or Docker Swarm scaling.
        raise NotImplementedError("Docker scaling adapter is not implemented yet")
