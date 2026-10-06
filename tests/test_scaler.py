from unittest.mock import MagicMock, patch

from optimizer.scaling.docker_scaler import DockerScaler


def test_set_replicas_dry_run_true_returns_success():
    scaler = DockerScaler("target-service", dry_run=True)
    assert scaler.set_replicas(2) is True


def test_set_replicas_compose_non_dry_run_returns_false():
    with patch("optimizer.scaling.docker_scaler.docker.from_env") as from_env:
        client = MagicMock()
        from_env.return_value = client
        scaler = DockerScaler("target-service", dry_run=False, mode="compose")
        assert scaler.set_replicas(2) is False


def test_get_replica_count_compose_uses_label_filter():
    with patch("optimizer.scaling.docker_scaler.docker.from_env") as from_env:
        client = MagicMock()
        client.containers.list.return_value = [object(), object()]
        from_env.return_value = client
        scaler = DockerScaler("target-service", dry_run=True, mode="compose")
        assert scaler.get_replica_count() == 2
