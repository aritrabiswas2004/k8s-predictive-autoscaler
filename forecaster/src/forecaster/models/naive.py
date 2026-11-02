"""Seasonal naive: the value one cycle earlier."""

import numpy as np

from .base import Forecast, Model


class SeasonalNaive(Model):
    """Point forecast: the same moment one cycle ago. Upper bound: that plus the
    `quantile`-quantile of the history's seasonal differences y[t] - y[t - season], i.e. of
    the errors this model would have made in the past. Only an upward adjustment is applied,
    so the bound is never below the point forecast."""

    name = "naive"

    def min_history(self, season):
        return 2 * season  # one cycle to copy, one more for the error distribution

    def forecast(self, y, season, horizon, quantile):
        y = np.asarray(y, dtype=float)
        n = len(y)
        # Step h lands at index n-1+h; go back whole cycles until it is inside the history.
        idx = np.array([n - 1 + h - season * -(-h // season) for h in range(1, horizon + 1)])
        mean = y[idx]
        errors = y[season:] - y[:-season]
        margin = max(0.0, float(np.quantile(errors, quantile)))
        return Forecast(mean, mean + margin).check(horizon)
