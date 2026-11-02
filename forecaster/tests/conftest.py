import numpy as np
import pytest

from forecaster.config import Config

SEASON = 20  # 300 s cycle at 15 s steps


def seasonal(n, season=SEASON, base=40.0, peak=120.0, noise=0.0, seed=0):
    """A daily-like shape: base at night, one peak mid-cycle."""
    rng = np.random.default_rng(seed)
    phase = (np.arange(n) % season) / season
    y = base + (peak - base) * np.clip(1 - np.abs(phase - 0.5) * 4, 0, 1)
    return y * (1 + noise * rng.standard_normal(n))


@pytest.fixture
def cfg():
    return Config()
