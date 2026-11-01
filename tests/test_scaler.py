from unittest.mock import MagicMock, patch

from optimizer.scaling.docker_scaler import DockerScaler


def test_set_replicas_dry_run_true_returns_success():
    scaler = DockerScaler("target-service", dry_run=True)
    assert scaler.set_replicas(2) is True




def test_get_replica_count_compose_uses_label_filter():
    with patch("optimizer.scaling.docker_scaler.docker.from_env") as from_env:
        client = MagicMock()
        client.containers.list.return_value = [object(), object()]
        from_env.return_value = client
        scaler = DockerScaler("target-service", dry_run=True, mode="compose")
        assert scaler.get_replica_count() == 2


def _container(number, *, name=None, ip="10.0.0.%d"):
    c = MagicMock()
    c.name = name or f"proj-target-service-{number}"
    c.labels = {
        "com.docker.compose.service": "target-service",
        "com.docker.compose.container-number": str(number),
        "com.docker.compose.project": "proj",
    }
    c.status = "running"
    c.attrs = {
        "Config": {"Image": "img:1", "Cmd": ["run"], "Env": ["A=1"], "Labels": dict(c.labels)},
        "HostConfig": {"NanoCpus": 500_000_000, "Memory": 268_435_456, "RestartPolicy": {"Name": "unless-stopped"}},
        "NetworkSettings": {"Networks": {"proj_default": {"IPAddress": ip % number if "%d" in ip else ip}}},
    }
    return c


def _scaler(containers, **kw):
    with patch("optimizer.scaling.docker_scaler.docker.from_env") as from_env:
        client = MagicMock()
        client.containers.list.return_value = containers
        from_env.return_value = client
        scaler = DockerScaler("target-service", dry_run=False, mode="compose", drain_seconds=0, **kw)
    return scaler, client


def test_scale_up_clones_template_with_alias_and_labels():
    template = _container(1)
    scaler, client = _scaler([template])
    client.api.create_container.return_value = {"Id": "abc"}
    new = _container(2)
    client.containers.get.return_value = new
    # after the first add, the list includes the new replica
    client.containers.list.side_effect = [[template], [template, new]]
    scaler._wait_ready = MagicMock(return_value=True)

    assert scaler.set_replicas(2) is True

    kwargs = client.api.create_container.call_args.kwargs
    assert kwargs["image"] == "img:1"
    assert kwargs["labels"]["com.docker.compose.container-number"] == "2"
    assert kwargs["labels"]["optimizer.managed-replica"] == "true"
    assert kwargs["name"] == "proj-target-service-2"
    client.api.create_endpoint_config.assert_called_once_with(aliases=["target-service"])
    host_kwargs = client.api.create_host_config.call_args.kwargs
    assert host_kwargs["nano_cpus"] == 500_000_000
    assert host_kwargs["mem_limit"] == 268_435_456
    client.api.start.assert_called_once_with("abc")


def test_scale_up_discards_replica_that_never_becomes_ready():
    template = _container(1)
    scaler, client = _scaler([template])
    client.api.create_container.return_value = {"Id": "abc"}
    broken = _container(2)
    client.containers.get.return_value = broken
    scaler._wait_ready = MagicMock(return_value=False)

    assert scaler.set_replicas(2) is False
    broken.stop.assert_called_once()
    broken.remove.assert_called_once_with(force=True)


def test_scale_down_removes_newest_and_never_the_original():
    c1, c2, c3 = _container(1), _container(2), _container(3)
    scaler, client = _scaler([c3, c1, c2])  # deliberately unsorted

    assert scaler.set_replicas(2) is True
    c3.remove.assert_called_once_with(force=True)
    c1.remove.assert_not_called()
    c2.remove.assert_not_called()
    client.networks.get.return_value.disconnect.assert_called_with(c3)


def test_scale_down_to_one_keeps_original():
    c1, c2 = _container(1), _container(2)
    scaler, _ = _scaler([c1, c2])
    assert scaler.set_replicas(1) is True
    c1.remove.assert_not_called()
    c2.remove.assert_called_once()


def test_scale_with_no_running_template_returns_false():
    scaler, _ = _scaler([])
    assert scaler.set_replicas(2) is False


def test_set_replicas_noop_when_already_at_target():
    c1 = _container(1)
    scaler, client = _scaler([c1])
    assert scaler.set_replicas(1) is True
    client.api.create_container.assert_not_called()


def test_dry_run_never_touches_docker():
    with patch("optimizer.scaling.docker_scaler.docker.from_env") as from_env:
        client = MagicMock()
        from_env.return_value = client
        scaler = DockerScaler("target-service", dry_run=True)
        assert scaler.set_replicas(4) is True
        client.api.create_container.assert_not_called()


def test_rejects_zero_replicas():
    scaler = DockerScaler("target-service", dry_run=True)
    try:
        scaler.set_replicas(0)
    except ValueError:
        return
    raise AssertionError("expected ValueError")
