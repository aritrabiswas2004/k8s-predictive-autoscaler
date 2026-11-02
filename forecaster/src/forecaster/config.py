"""Configuration from environment variables. Defaults are the experiment's values
(docs/methodology.md#forecaster)."""

import os
from dataclasses import dataclass, fields

ARRIVALS = 'sum(rate(http_requests_received_total{app="web"}[1m]))'


@dataclass(frozen=True)
class Config:
    prometheus_url: str = "http://kube-prometheus-stack-prometheus.monitoring.svc:9090"
    query: str = ARRIVALS
    models: tuple = ("naive", "holtwinters")
    step_seconds: int = 15          # sample spacing of the history (= scrape interval)
    cycle_seconds: int = 300        # one compressed "day": the seasonal period
    history_cycles: int = 3         # how much history to fit on
    min_history_cycles: int = 2     # less than this: cold start, export nothing
    max_gap_steps: int = 2          # longer gaps cut the history (shorter ones are interpolated)
    lead_seconds: int = 60          # forecast window starts this far ahead ...
    window_seconds: int = 60        # ... and is this long
    upper_quantile: float = 0.9     # one-sided upper prediction bound
    rps_per_pod: float = 40.0
    max_replicas: int = 7           # forecasts are clamped to max_replicas x rps_per_pod
    interval_seconds: float = 30.0   # how often to forecast
    smape_samples: int = 10         # rolling sMAPE over this many evaluated forecasts
    query_timeout_seconds: float = 10.0
    port: int = 8000

    def __post_init__(self):
        for name in ("cycle_seconds", "lead_seconds", "window_seconds"):
            if getattr(self, name) % self.step_seconds:
                raise ValueError(f"{name} must be a multiple of step_seconds")
        if not 1 <= self.min_history_cycles <= self.history_cycles:
            raise ValueError("need 1 <= min_history_cycles <= history_cycles")
        if not 0.5 < self.upper_quantile < 1:
            raise ValueError("upper_quantile must be in (0.5, 1)")

    @property
    def season(self):
        """Seasonal period in samples (20 at 300 s / 15 s)."""
        return self.cycle_seconds // self.step_seconds

    @property
    def clamp_rps(self):
        return self.max_replicas * self.rps_per_pod

    @classmethod
    def from_env(cls, env=None):
        """Every field can be set as an upper-case env var, e.g. LEAD_SECONDS=90,
        MODELS=naive,holtwinters."""
        env = os.environ if env is None else env
        kw = {}
        for f in fields(cls):
            raw = env.get(f.name.upper())
            if raw is None:
                continue
            default = f.default
            if isinstance(default, tuple):
                kw[f.name] = tuple(s.strip() for s in raw.split(",") if s.strip())
            else:
                kw[f.name] = type(default)(raw)
        return cls(**kw)
