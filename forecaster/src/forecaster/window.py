"""From a model's step-by-step forecast to the one number the scaler uses: the peak over the
window [now + LEAD, now + LEAD + WINDOW].

Shared by the service and the backtest, so both compute exactly the same thing.
"""

import math
from dataclasses import dataclass

import numpy as np

from .models import Model


@dataclass
class PeakForecast:
    model: str
    made_at: float        # unix time of the forecast (now)
    last_sample: float    # unix time of the newest history sample
    targets: np.ndarray   # unix times of the samples the window covers
    mean: float           # peak of the point forecast over the window
    upper: float          # peak of the upper bound over the window, clamped (what is exported)
    upper_raw: float      # same before clamping


def horizon_steps(now, last_sample, step, lead, window):
    """Steps after the last sample (1-based) whose times fall in [now+lead, now+lead+window].

    The newest sample can be up to a scrape interval older than now, so the window is placed
    relative to now, not to the sample.
    """
    first = max(1, math.ceil((now + lead - last_sample) / step - 1e-9))
    last = math.floor((now + lead + window - last_sample) / step + 1e-9)
    if last < first:
        raise ValueError("window contains no sample; window must be at least one step")
    return list(range(first, last + 1))


def clamp(rps, upper):
    """Keep a forecast within [0, upper]. upper = max_replicas x RPS_PER_POD: more than that
    cannot change the replica count, and an exploding fit must not reach the scaler as an
    absurd number. No lower clamp: KEDA's minReplicaCount already sets the floor, and a
    floor here would hide the forecast's shape."""
    return min(max(rps, 0.0), upper)


def predict(model: Model, history, *, now, last_sample, cfg):
    """Fit `model` on `history` (ending at `last_sample`) and return the window peak."""
    steps = horizon_steps(now, last_sample, cfg.step_seconds, cfg.lead_seconds,
                          cfg.window_seconds)
    fc = model.forecast(history, cfg.season, steps[-1], cfg.upper_quantile)
    sel = np.array(steps) - 1
    upper_raw = float(fc.upper[sel].max())
    return PeakForecast(
        model=model.name,
        made_at=now,
        last_sample=last_sample,
        targets=last_sample + np.array(steps, dtype=float) * cfg.step_seconds,
        mean=float(fc.mean[sel].max()),
        upper=clamp(upper_raw, cfg.clamp_rps),
        upper_raw=upper_raw,
    )
