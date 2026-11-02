import numpy as np

from forecaster.scoring import RollingScore, smape
from forecaster.window import PeakForecast


def test_smape():
    assert smape([100], [100]) == 0
    assert smape([0], [0]) == 0
    assert smape([150], [50]) == 1.0
    assert smape([100, 150], [100, 50]) == 0.5


def pf(targets, mean, upper):
    return PeakForecast("naive", 0, 0, np.array(targets, float), mean, upper, upper)


def test_rolling_score_waits_for_the_window():
    s = RollingScore(n=10, step=15)
    s.add(pf([60, 75], mean=100, upper=110))
    times = np.array([30.0, 45, 60])
    s.update(times, np.array([1.0, 1, 1]))
    assert s.smape is None and len(s.pending) == 1

    times = np.array([45.0, 60, 75])
    s.update(times, np.array([1.0, 90, 120]))
    assert not s.pending
    assert s.smape == smape([100], [120])
    assert s.coverage == 0.0  # realized 120 > upper 110


def test_rolling_score_keeps_the_last_n():
    s = RollingScore(n=2, step=15)
    for m in (100, 200, 300):
        s.add(pf([0], m, m))
    s.update(np.array([0.0]), np.array([100.0]))
    assert len(s.scored) == 2
    assert s.coverage == 1.0
