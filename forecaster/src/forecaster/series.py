"""Turn Prometheus samples into the regular history the models fit on."""

import numpy as np


def to_grid(samples, end, step, n):
    """Values at end - (n-1)*step, ..., end; NaN where Prometheus returned no sample.

    query_range returns samples exactly on its evaluation grid, so anything else is ignored.
    """
    grid = np.full(n, np.nan)
    start = end - (n - 1) * step
    for t, v in samples:
        i = round((t - start) / step)
        if 0 <= i < n and abs(t - (start + i * step)) < 1e-3:
            grid[i] = v
    return grid


def recent_history(grid, max_gap):
    """The most recent stretch of the grid without a gap longer than max_gap samples.

    Short gaps (a missed scrape, a restarted pod) are filled by linear interpolation; a longer
    gap means the series was absent (no pods, Prometheus down), and history before it is
    dropped rather than bridged. Missing samples at the very end are dropped too: the history
    ends at the last real sample. Returns (values, samples_dropped_at_end).
    """
    ok = ~np.isnan(grid)
    if not ok.any():
        return np.array([]), len(grid)
    last = int(np.flatnonzero(ok)[-1])
    first = last
    gap = 0
    for i in range(last - 1, -1, -1):
        if ok[i]:
            first, gap = i, 0
        else:
            gap += 1
            if gap > max_gap:
                break
    x = np.arange(first, last + 1)
    seg = grid[first:last + 1]
    m = ~np.isnan(seg)
    values = np.interp(x, x[m], seg[m])
    return values, len(grid) - 1 - last
