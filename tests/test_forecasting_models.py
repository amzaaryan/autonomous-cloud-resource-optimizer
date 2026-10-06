import numpy as np

from optimizer.forecasting.models import evaluate_regression, persistence_forecast


def test_persistence_forecast_basic():
    actual, predicted = persistence_forecast([1, 2, 3, 4, 5], horizon=1)
    assert np.allclose(actual, [2, 3, 4, 5])
    assert np.allclose(predicted, [1, 2, 3, 4])


def test_evaluate_regression_returns_required_metrics():
    metrics = evaluate_regression([10, 20, 30], [11, 19, 31])
    assert metrics["mae"] is not None
    assert metrics["rmse"] is not None
    assert metrics["r2"] is not None
    assert metrics["spike_precision"] is not None
    assert metrics["spike_recall"] is not None
