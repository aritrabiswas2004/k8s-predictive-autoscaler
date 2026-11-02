import numpy as np

from forecaster.series import recent_history, to_grid


def test_to_grid_places_samples_and_leaves_gaps():
    g = to_grid([(100, 1.0), (115, 2.0), (145, 4.0), (138, 9.0), (160, 9.0)], end=145, step=15, n=4)
    # 130 has no sample; 138 is off the grid and 160 after the end.
    assert np.array_equal(g[:2], [1.0, 2.0])
    assert np.isnan(g[2])
    assert g[3] == 4.0


def test_short_gaps_are_interpolated():
    v, dropped = recent_history(np.array([1, np.nan, np.nan, 4, 5.0]), max_gap=2)
    assert np.allclose(v, [1, 2, 3, 4, 5])
    assert dropped == 0


def test_long_gap_cuts_history():
    v, _ = recent_history(np.array([9, 9, np.nan, np.nan, np.nan, 1, 2.0]), max_gap=2)
    assert np.array_equal(v, [1, 2])


def test_trailing_missing_samples_are_dropped():
    v, dropped = recent_history(np.array([1, 2, 3, np.nan, np.nan]), max_gap=2)
    assert np.array_equal(v, [1, 2, 3])
    assert dropped == 2


def test_no_samples():
    v, dropped = recent_history(np.full(5, np.nan), max_gap=2)
    assert len(v) == 0 and dropped == 5
