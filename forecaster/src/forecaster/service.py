"""The forecaster service: every interval, read the arrival history from Prometheus, fit each
model, and export the forecast peak as Prometheus metrics.

Failure rules (plan section "Phase 4"):
- Cold start (less than min_history_cycles of history, or no series at all): export no
  forecast_rps for that model. The reactive trigger (arm D) or KEDA's fallback (arm C) takes
  over. A made-up number is never exported.
- Prometheus or fit error: log it, keep exporting the last good forecast while its window has
  not yet passed, then withdraw it. forecast_last_success_timestamp_seconds stops advancing,
  so its age shows the staleness.
"""

import logging
import math
import signal
import threading
import time

import numpy as np
from prometheus_client import CollectorRegistry, Counter, Gauge, start_http_server

from . import models
from .config import Config
from .prom import Prometheus, PromError
from .scoring import RollingScore
from .series import recent_history, to_grid
from .window import clamp, predict

log = logging.getLogger("forecaster")


class Metrics:
    def __init__(self, registry, cfg):
        g = lambda name, doc, labels=(): Gauge(name, doc, labels, registry=registry)  # noqa: E731
        self.rps = g("forecast_rps",
                     "Forecast peak arrival rate over [now+lead, now+lead+window]; "
                     "bound=upper is the one KEDA scales on", ["model", "bound"])
        self.last_success = g("forecast_last_success_timestamp_seconds",
                              "Unix time of the last successful forecast", ["model"])
        self.fit_duration = g("forecast_fit_duration_seconds",
                              "Duration of the last model fit", ["model"])
        self.smape = g("forecast_smape",
                       "Rolling sMAPE (ratio, 0.1 = 10 %) of the mean peak vs the realized "
                       f"peak, last {cfg.smape_samples} scored forecasts", ["model"])
        self.coverage = g("forecast_coverage_ratio",
                          "Share of recent forecast windows whose realized peak stayed at or "
                          "below the upper bound", ["model"])
        self.history = g("forecast_history_seconds", "Length of usable arrival history")
        self.errors = Counter("forecast_errors", "Failed forecast attempts", ["stage", "model"],
                              registry=registry)
        g("forecast_lead_seconds", "Window start, seconds ahead").set(cfg.lead_seconds)
        g("forecast_window_seconds", "Window length").set(cfg.window_seconds)
        g("forecast_upper_quantile", "Quantile of the upper bound").set(cfg.upper_quantile)


class Forecaster:
    def __init__(self, cfg: Config, prom, registry=None):
        self.cfg = cfg
        self.prom = prom
        self.models = [models.get(n) for n in cfg.models]
        self.registry = registry or CollectorRegistry()
        self.m = Metrics(self.registry, cfg)
        self.last_good = {}  # model name -> PeakForecast currently exported
        self.scores = {mod.name: RollingScore(cfg.smape_samples, cfg.step_seconds)
                       for mod in self.models}

    def history_samples(self):
        """Grid points fetched: the fit window plus one forecast window, so the rolling score
        can still see the realized values of forecasts made a window ago."""
        c = self.cfg
        return c.history_cycles * c.season + (c.lead_seconds + c.window_seconds) // c.step_seconds

    def step(self, now=None):
        """One iteration. Returns {model: PeakForecast or None}."""
        now = time.time() if now is None else now
        c = self.cfg
        end = math.floor(now / c.step_seconds) * c.step_seconds
        n = self.history_samples()
        try:
            samples = self.prom.query_range(c.query, end - (n - 1) * c.step_seconds, end,
                                            c.step_seconds)
        except PromError as e:
            log.warning("prometheus: %s", e)
            self.m.errors.labels("query", "").inc()
            self.expire(now)
            return {mod.name: None for mod in self.models}

        grid = to_grid(samples, end, c.step_seconds, n)
        times = end - (n - 1 - np.arange(n)) * c.step_seconds
        for mod in self.models:
            self.scores[mod.name].update(times, grid)
            self.export_score(mod.name)

        values, dropped = recent_history(grid, c.max_gap_steps)
        values = values[-c.history_cycles * c.season:]
        last_sample = end - dropped * c.step_seconds
        self.m.history.set(len(values) * c.step_seconds)

        out = {}
        for mod in self.models:
            need = max(mod.min_history(c.season), c.min_history_cycles * c.season)
            if len(values) < need:
                log.info("%s: cold start, %d of %d samples", mod.name, len(values), need)
                self.withdraw(mod.name)
                out[mod.name] = None
                continue
            t0 = time.perf_counter()
            try:
                pf = predict(mod, values, now=now, last_sample=last_sample, cfg=c)
            except models.ModelError as e:
                log.warning("%s: %s", mod.name, e)
                self.m.errors.labels("fit", mod.name).inc()
                self.expire(now, only=mod.name)
                out[mod.name] = None
                continue
            self.m.fit_duration.labels(mod.name).set(time.perf_counter() - t0)
            self.publish(pf)
            out[mod.name] = pf
        return out

    def publish(self, pf):
        self.last_good[pf.model] = pf
        self.m.rps.labels(pf.model, "upper").set(pf.upper)
        self.m.rps.labels(pf.model, "mean").set(clamp(pf.mean, self.cfg.clamp_rps))
        self.m.last_success.labels(pf.model).set(pf.made_at)
        self.scores[pf.model].add(pf)
        log.info("%s: peak %.1f (upper %.1f) RPS over +%ds..+%ds", pf.model, pf.mean, pf.upper,
                 self.cfg.lead_seconds, self.cfg.lead_seconds + self.cfg.window_seconds)

    def withdraw(self, name):
        """Stop exporting a model's forecast (the series disappears from the next scrape)."""
        self.last_good.pop(name, None)
        for bound in ("upper", "mean"):
            try:
                self.m.rps.remove(name, bound)
            except KeyError:
                pass

    def expire(self, now, only=None):
        """After a failure: keep the last good forecast until its window has passed."""
        for name, pf in list(self.last_good.items()):
            if only not in (None, name):
                continue
            if now > pf.made_at + self.cfg.lead_seconds + self.cfg.window_seconds:
                log.warning("%s: last good forecast is past its window, withdrawn", name)
                self.withdraw(name)

    def export_score(self, name):
        s = self.scores[name]
        if s.smape is not None:
            self.m.smape.labels(name).set(s.smape)
            self.m.coverage.labels(name).set(s.coverage)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    log.info("config: %s", cfg)
    f = Forecaster(cfg, Prometheus(cfg.prometheus_url, cfg.query_timeout_seconds))
    start_http_server(cfg.port, registry=f.registry)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        t0 = time.time()
        try:
            f.step(t0)
        except Exception:  # keep serving metrics; the staleness metric shows the failure
            log.exception("forecast iteration failed")
            f.m.errors.labels("internal", "").inc()
            f.expire(time.time())
        stop.wait(max(0.0, cfg.interval_seconds - (time.time() - t0)))
