import numpy as np
import pytest

from conftest import SEASON, seasonal
from forecaster.models import SeasonalNaive
from forecaster.window import clamp, horizon_steps, predict


def test_window_relative_to_last_sample():
    # now == last sample: [now+60, now+120] = steps 4..8 at 15 s
    assert horizon_steps(1000, 1000, 15, 60, 60) == [4, 5, 6, 7, 8]


def test_window_relative_to_now_when_sample_is_older():
    # last sample 10 s old: targets at last+75 .. last+120 lie in [now+60, now+120]
    assert horizon_steps(1010, 1000, 15, 60, 60) == [5, 6, 7, 8]


def test_window_must_contain_a_sample():
    with pytest.raises(ValueError):
        horizon_steps(1010, 1000, 15, 60, 0)


def test_clamp():
    assert clamp(-3, 280) == 0
    assert clamp(500, 280) == 280
    assert clamp(123.4, 280) == 123.4


def test_predict_takes_the_peak_over_the_window(cfg):
    y = seasonal(3 * SEASON)
    pf = predict(SeasonalNaive(), y, now=3000, last_sample=3000, cfg=cfg)
    window = y[SEASON + 3:SEASON + 8]  # steps 4..8, one cycle ago
    assert pf.mean == window.max()
    assert list(pf.targets) == [3060, 3075, 3090, 3105, 3120]


def test_predict_clamps_only_the_exported_value(cfg):
    y = seasonal(3 * SEASON, peak=1000)
    pf = predict(SeasonalNaive(), y, now=3000 + 7 * 15, last_sample=3000 + 7 * 15, cfg=cfg)
    assert pf.upper_raw > cfg.clamp_rps
    assert pf.upper == cfg.clamp_rps
