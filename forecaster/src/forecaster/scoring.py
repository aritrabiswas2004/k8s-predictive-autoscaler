"""Forecast accuracy: sMAPE, and a rolling score of the service's own past forecasts."""

from collections import deque

import numpy as np


def smape(forecast, actual):
    """Symmetric MAPE as a ratio in [0, 2] (0.1 = 10 %). Pairs where both are 0 count as 0."""
    f = np.asarray(forecast, dtype=float)
    a = np.asarray(actual, dtype=float)
    den = np.abs(f) + np.abs(a)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.where(den == 0, 0.0, 2 * np.abs(f - a) / den)
    return float(r.mean())


def realized_peak(times, values, targets, step):
    """Max of the observed series at the target times, or None if any target is missing
    (not yet observed, or outside the history)."""
    out = []
    for t in targets:
        i = np.flatnonzero(np.abs(times - t) < step / 2)
        if len(i) == 0 or np.isnan(values[i[0]]):
            return None
        out.append(values[i[0]])
    return float(max(out))


class RollingScore:
    """Keeps a model's forecasts until their window has been observed, then scores them.

    sMAPE compares the forecast's mean peak with the realized peak; coverage is the share of
    windows whose realized peak stayed at or below the upper bound (raw, before clamping).
    Both are over the last `n` scored forecasts.
    """

    def __init__(self, n, step):
        self.step = step
        self.pending = deque()
        self.scored = deque(maxlen=n)  # (mean, upper_raw, actual)

    def add(self, pf):
        self.pending.append(pf)

    def update(self, times, values):
        """Score pending forecasts whose whole window is in (times, values)."""
        if len(times) == 0:
            return
        newest = times[-1]
        keep = deque()
        for pf in self.pending:
            if pf.targets[-1] > newest:
                keep.append(pf)
                continue
            actual = realized_peak(times, values, pf.targets, self.step)
            if actual is not None:  # else the window fell into a gap: drop it unscored
                self.scored.append((pf.mean, pf.upper_raw, actual))
        self.pending = keep

    @property
    def smape(self):
        if not self.scored:
            return None
        m, _, a = zip(*self.scored)
        return smape(m, a)

    @property
    def coverage(self):
        if not self.scored:
            return None
        return float(np.mean([a <= u for _, u, a in self.scored]))
