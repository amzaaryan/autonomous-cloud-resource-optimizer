from optimizer.prometheus_client import PrometheusClient


def test_parse_instant_value_success():
    payload = {"status": "success", "data": {"result": [{"value": [1720000000, "1.25"]}]}}
    assert PrometheusClient.parse_instant_value(payload) == 1.25


def test_parse_instant_value_handles_empty_result():
    payload = {"status": "success", "data": {"result": []}}
    assert PrometheusClient.parse_instant_value(payload) is None


def test_parse_instant_value_handles_malformed_sample():
    payload = {"status": "success", "data": {"result": [{"value": [1720000000]}]}}
    assert PrometheusClient.parse_instant_value(payload) is None


def test_parse_range_values_filters_bad_samples():
    payload = {
        "status": "success",
        "data": {"result": [{"values": [[1720, "1.0"], [1721, "bad"], [1722, "2.0"]]}]},
    }
    assert PrometheusClient.parse_range_values(payload) == [(1720.0, 1.0), (1722.0, 2.0)]
