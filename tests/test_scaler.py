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


# ---- smoke-test tool ---------------------------------------------------------------------------


def test_smoke_test_parses_work_counter_and_ignores_other_paths():
    from optimizer.scaling.smoke_test import work_count

    text = (
        '# HELP http_requests_total Total HTTP requests\n'
        'http_requests_total{method="GET",path="/work",status="200"} 12.0\n'
        'http_requests_total{method="GET",path="/health",status="200"} 99.0\n'
        'http_requests_total_created{method="GET",path="/work",status="200"} 1.7e9\n'
    )
    assert work_count(text) == 12.0


def test_smoke_test_passes_only_when_every_replica_serves_traffic():
    from optimizer.scaling import smoke_test

    scaler = MagicMock()
    scaler.get_replica_count.return_value = 2
    scaler.set_replicas.return_value = True
    scaler._sorted.side_effect = lambda c: c
    scaler._running_containers.return_value = [_container(1), _container(2)]
    scaler._ip_address.side_effect = lambda c: f"10.0.0.{c.labels['com.docker.compose.container-number']}"

    class Resp:
        status_code = 200
        text = ""

    served = {"10.0.0.1": 0, "10.0.0.2": 0}

    def fake_get(url, timeout=0):
        resp = Resp()
        if url.endswith("/metrics"):
            ip = url.split("//")[1].split(":")[0]
            resp.text = f'http_requests_total{{method="GET",path="/work",status="200"}} {served[ip]}\n'
        else:  # a request via the LB: alternate between the two replicas
            served["10.0.0.1" if sum(served.values()) % 2 == 0 else "10.0.0.2"] += 1
        return resp

    lines = []
    with patch.object(smoke_test.requests, "get", side_effect=fake_get):
        ok = smoke_test.run(
            scaler, replicas=2, request_count=10, lb_url="http://lb", port=8000, restore=True,
            settle_seconds=0, out=lines.append,
        )
    assert ok and any("PASS" in line for line in lines)
    scaler.set_replicas.assert_called_with(2)  # restore to the original count

    served.update({"10.0.0.1": 0, "10.0.0.2": 0})
    def one_sided(url, timeout=0):
        resp = Resp()
        if url.endswith("/metrics"):
            ip = url.split("//")[1].split(":")[0]  # replica 2 never serves anything
            resp.text = f'http_requests_total{{method="GET",path="/work",status="200"}} {served[ip]}\n'
        else:
            served["10.0.0.1"] += 1
        return resp
    with patch.object(smoke_test.requests, "get", side_effect=one_sided):
        assert not smoke_test.run(
            scaler, replicas=2, request_count=10, lb_url="http://lb", port=8000, restore=False,
            settle_seconds=0, out=lambda *_: None,
        )
