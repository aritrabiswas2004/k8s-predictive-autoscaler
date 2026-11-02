"""Holt-Winters as an ETS(A,N,A) state space model (statsmodels)."""

import warnings

import numpy as np
import pandas as pd
from statsmodels.tsa.exponential_smoothing.ets import ETSModel

from .base import Forecast, Model, ModelError


class HoltWinters(Model):
    """Additive error, no trend, additive seasonality with period = one cycle.

    No trend: three cycles are too short to estimate one reliably, and over the forecast
    horizon (about two minutes) the level update follows any slow drift anyway. The state
    space form gives analytic prediction intervals; the upper bound is the one-sided
    `quantile` of the predictive distribution at each step.

    Fitted on ~3 cycles of the 1-minute arrival rate, the optimiser puts the level smoothing
    at its upper bound and the seasonal smoothing at its lower bound (alpha ~ 1, gamma ~ 0):
    the forecast becomes "the latest value plus the change the seasonal profile expects".
    docs/methodology.md#forecaster discusses this.
    """

    name = "holtwinters"

    def min_history(self, season):
        return 2 * season

    def forecast(self, y, season, horizon, quantile):
        # A Series, not an ndarray: statsmodels 0.15 get_prediction() needs an index.
        y = pd.Series(np.asarray(y, dtype=float))
        n = len(y)
        try:
            with warnings.catch_warnings():
                # Convergence warnings are common on short series; the forecast is checked
                # for sanity below instead.
                warnings.simplefilter("ignore")
                fit = ETSModel(y, error="add", trend=None, seasonal="add",
                               seasonal_periods=season).fit(disp=False, maxiter=1000)
                frame = fit.get_prediction(start=n, end=n + horizon - 1).summary_frame(
                    alpha=2 * (1 - quantile))
        except Exception as e:  # statsmodels raises many types; none may kill the loop
            raise ModelError(f"ETS fit failed: {e}") from e
        return Forecast(frame["mean"].to_numpy(), frame["pi_upper"].to_numpy()).check(horizon)
