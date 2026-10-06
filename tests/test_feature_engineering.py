import pandas as pd

from optimizer.feature_engineering import build_features


def test_build_features_adds_lags_rolling_and_targets():
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=20, freq="min"),
            "cpu_usage": [0.1 + i * 0.01 for i in range(20)],
            "memory_usage": [0.3 + i * 0.005 for i in range(20)],
            "request_rate": [1 + i for i in range(20)],
            "p95_latency_ms": [100 + i for i in range(20)],
            "error_rate": [0.01 for _ in range(20)],
            "replica_count": [1 for _ in range(20)],
        }
    )
    result = build_features(frame, horizons=(1, 3, 5), with_targets=True)
    assert not result.empty
    assert "cpu_usage_lag_1" in result.columns
    assert "request_rate_roll_mean_3" in result.columns
    assert "request_rate_roc_1" in result.columns
    assert "target_request_rate_t_plus_5" in result.columns
