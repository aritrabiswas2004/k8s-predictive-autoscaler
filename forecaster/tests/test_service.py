import numpy as np
import pytest

from conftest import SEASON, seasonal
from forecaster.config import Config
from forecaster.models import ModelError
from forecaster.prom import PromError
from forecaster.service import Forecaster

STEP = 15
NOW = 1_800_000_000  # a multiple of 15


class FakeProm:
    """Serves y[i] at time t0 + i*STEP; raises when told to."""

    def __init__(self, y, t0):
        self.y, self.t0, self.fail = np.asarray(y, float), t0, False
        self.calls = []

    def query_range(self, query, start, end, step):
        self.calls.append((start, end, step))
        if self.fail:
            raise PromError("connection refused")
        out = []
        t = start
        while t <= end:
            i = round((t - self.t0) / STEP)
            if 0 <= i < len(self.y):
                out.append((t, float(self.y[i])))
            t += step
        return out


def make(y, cfg=None, end=NOW):
    """A forecaster whose history ends at `end` with the last value of y."""
    prom = FakeProm(y, end - (len(y) - 1) * STEP)
    return Forecaster(cfg or Config(), prom), prom


def exported(f, name, **labels):
    return f.registry.get_sample_value(name, labels)


def test_cold_start_exports_nothing():
    f, _ = make(seasonal(2 * SEASON - 1))
    out = f.step(NOW)
    assert out == {"naive": None, "holtwinters": None}
    for m in ("naive", "holtwinters"):
        assert exported(f, "forecast_rps", model=m, bound="upper") is None
        assert exported(f, "forecast_last_success_timestamp_seconds", model=m) is None
    assert exported(f, "forecast_history_seconds") == (2 * SEASON - 1) * STEP


def test_no_series_at_all_is_a_cold_start():
    f, _ = make([])
    assert f.step(NOW) == {"naive": None, "holtwinters": None}


def test_forecasts_both_models():
    f, prom = make(seasonal(3 * SEASON, noise=0.02))
    out = f.step(NOW + 7)  # mid-interval: the query still ends on the 15 s grid
    start, end, step = prom.calls[0]
    assert end == NOW and step == STEP
    assert (end - start) / STEP + 1 == 3 * SEASON + 8  # fit window + one forecast window
    for m in ("naive", "holtwinters"):
        up = exported(f, "forecast_rps", model=m, bound="upper")
        mean = exported(f, "forecast_rps", model=m, bound="mean")
        assert up == out[m].upper and up >= mean > 0
        assert exported(f, "forecast_last_success_timestamp_seconds", model=m) == NOW + 7
        assert exported(f, "forecast_fit_duration_seconds", model=m) > 0
    # The window [now+60, now+120] of this shape is at the cycle's peak (y peaks at
    # index 10 of each cycle, the history ends at index 19): steps 5..8 -> indices 4..7
    # of the next cycle, before the peak.
    assert out["naive"].mean == pytest.approx(seasonal(SEASON)[4:8].max(), rel=0.05)


def test_uses_only_the_configured_history():
    f, _ = make(seasonal(5 * SEASON, peak=1000)[:2 * SEASON].tolist() + seasonal(3 * SEASON).tolist())
    # Older cycles with a 1000 RPS peak are outside the 3-cycle fit window. (The query also
    # returns 8 older samples for scoring, which the fit must not use.)
    out = f.step(NOW)
    assert out["naive"].upper < 200


def test_exported_forecast_is_clamped():
    f, _ = make(seasonal(3 * SEASON, peak=2000))
    f.step(NOW + 10 * STEP)  # place the window on the peak
    for m in ("naive", "holtwinters"):
        assert exported(f, "forecast_rps", model=m, bound="upper") <= 280
        assert exported(f, "forecast_rps", model=m, bound="mean") <= 280


def test_prometheus_error_keeps_last_forecast_until_its_window_passed():
    f, prom = make(seasonal(3 * SEASON))
    good = f.step(NOW)["holtwinters"].upper
    prom.fail = True

    f.step(NOW + 30)
    assert exported(f, "forecast_rps", model="holtwinters", bound="upper") == good
    assert exported(f, "forecast_last_success_timestamp_seconds", model="holtwinters") == NOW
    assert exported(f, "forecast_errors_total", stage="query", model="") == 1

    f.step(NOW + 120)  # window end, still valid
    assert exported(f, "forecast_rps", model="holtwinters", bound="upper") == good
    f.step(NOW + 121)
    assert exported(f, "forecast_rps", model="holtwinters", bound="upper") is None
    assert exported(f, "forecast_rps", model="naive", bound="upper") is None


def test_series_disappearing_withdraws_immediately():
    f, prom = make(seasonal(3 * SEASON))
    f.step(NOW)
    prom.y = np.array([])
    f.step(NOW + 30)
    assert exported(f, "forecast_rps", model="naive", bound="upper") is None


def test_one_model_failing_does_not_affect_the_other(monkeypatch):
    f, _ = make(seasonal(3 * SEASON))

    def boom(*a, **k):
        raise ModelError("no")
    monkeypatch.setattr(f.models[1], "forecast", boom)
    out = f.step(NOW)
    assert out["holtwinters"] is None and out["naive"] is not None
    assert exported(f, "forecast_errors_total", stage="fit", model="holtwinters") == 1
    assert exported(f, "forecast_rps", model="naive", bound="upper") is not None


def test_rolling_smape_scores_past_forecasts():
    y = seasonal(6 * SEASON)  # perfectly periodic: naive is exact
    f, prom = make(y[:3 * SEASON])
    for k in range(0, 12):  # 3 minutes of 15 s steps, forecasting every 15 s
        prom.y = y[:3 * SEASON + k]
        prom.t0 = NOW - (3 * SEASON - 1) * STEP
        f.step(NOW + k * STEP)
    assert exported(f, "forecast_smape", model="naive") == pytest.approx(0, abs=1e-9)
    assert exported(f, "forecast_coverage_ratio", model="naive") == 1.0
