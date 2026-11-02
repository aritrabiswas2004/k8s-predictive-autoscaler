"""The interface every model implements."""

from dataclasses import dataclass

import numpy as np


class ModelError(Exception):
    """The model could not produce a usable forecast from this history."""


@dataclass
class Forecast:
    """Forecast for the steps h = 1..H after the last history sample."""
    mean: np.ndarray   # point forecast
    upper: np.ndarray  # one-sided upper prediction bound at the requested quantile

    def check(self, horizon):
        for name in ("mean", "upper"):
            a = np.asarray(getattr(self, name), dtype=float)
            if a.shape != (horizon,) or not np.isfinite(a).all():
                raise ModelError(f"{name} is not {horizon} finite values: {a}")
        return self


class Model:
    name = ""

    def min_history(self, season):
        """Fewest samples the model needs."""
        raise NotImplementedError

    def forecast(self, y, season, horizon, quantile):
        """Forecast the `horizon` samples after y (1-D array, regular spacing)."""
        raise NotImplementedError
