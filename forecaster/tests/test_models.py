import numpy as np
import pytest

from conftest import SEASON, seasonal
from forecaster import models
from forecaster.models import HoltWinters, ModelError, SeasonalNaive


def test_naive_repeats_the_last_cycle():
    y = seasonal(3 * SEASON)
    f = SeasonalNaive().forecast(y, SEASON, 9, 0.9)
    assert np.array_equal(f.mean, y[SEASON:SEASON + 9])
    assert np.array_equal(f.upper, f.mean)  # a perfectly periodic past has no errors


def test_naive_wraps_beyond_one_cycle():
    y = np.arange(40.0)
    f = SeasonalNaive().forecast(y, SEASON, 25, 0.9)
    assert f.mean[0] == y[20]          # h=1 -> one cycle back
    assert f.mean[19] == y[39]         # h=20 -> the last sample
    assert f.mean[20] == y[20]         # h=21 -> two cycles back


def test_naive_upper_adds_the_error_quantile():
    y = seasonal(3 * SEASON, noise=0.05)
    f = SeasonalNaive().forecast(y, SEASON, 9, 0.9)
    margin = np.quantile(y[SEASON:] - y[:-SEASON], 0.9)
    assert margin > 0
    assert np.allclose(f.upper - f.mean, margin)


def test_naive_never_lowers_the_bound():
    y = seasonal(3 * SEASON) * np.repeat([1.5, 1.2, 1.0], SEASON)  # shrinking cycles
    f = SeasonalNaive().forecast(y, SEASON, 9, 0.9)
    assert np.array_equal(f.upper, f.mean)


def test_holtwinters_follows_the_seasonal_shape():
    y = seasonal(4 * SEASON, noise=0.03)
    hist, future = y[:3 * SEASON], seasonal(4 * SEASON)[3 * SEASON:3 * SEASON + 9]
    f = HoltWinters().forecast(hist, SEASON, 9, 0.9)
    assert np.all(np.abs(f.mean - future) < 0.15 * future)
    assert np.all(f.upper >= f.mean)
    # Intervals widen (or at least do not shrink) with the horizon.
    assert np.all(np.diff(f.upper - f.mean) >= -1e-9)


@pytest.mark.parametrize("model", [SeasonalNaive(), HoltWinters()])
def test_flat_series(model):
    f = model.forecast(np.full(3 * SEASON, 50.0), SEASON, 9, 0.9)
    assert np.allclose(f.mean, 50)


def test_holtwinters_wraps_fit_errors(monkeypatch):
    import forecaster.models.holtwinters as hw

    def boom(*a, **k):
        raise RuntimeError("singular")
    monkeypatch.setattr(hw, "ETSModel", boom)
    with pytest.raises(ModelError):
        HoltWinters().forecast(seasonal(3 * SEASON), SEASON, 9, 0.9)


def test_registry():
    assert set(models.MODELS) == {"naive", "holtwinters"}
    with pytest.raises(ValueError):
        models.get("prophet")
