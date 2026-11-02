"""Rolling-origin backtest of the forecasting models, using the service's own code path
(forecaster.window.predict) and configuration.

    .venv/bin/python forecaster/backtest/backtest.py [--out-dir DIR] [--plot PNG]

Series:
  simulated  The arrival series Prometheus would record for each trace in loadtest/trace/out/
             (seeds 1-5, both workloads): Poisson arrivals at the trace's rate, counted by 7
             pod counters scraped every 15 s, read as sum(rate(...[1m])) on a 15 s grid.
             Checked against the recorded series below (simulation_vs_recorded in the output).
  recorded   The real series from the Phase 3 trace-check runs
             (experiments/results/traces/check-*-s1.arrivals.json).

Origins: every 30 s (the service's interval) from the end of the history cycles until the last
window that fits in the run. At each origin every model is fitted on the preceding 3 cycles
and forecasts the peak over [origin + LEAD, origin + LEAD + WINDOW]. Windows that overlap the
unforecastable spike (or the minute after it, while the 1-minute rate still contains it) are
reported separately.

Metrics per model (peak = max of the series over the forecast window):
  sMAPE     of the mean peak vs the realized peak
  MASE      mean absolute error of the mean peak, scaled by the mean absolute seasonal
            difference of each origin's history (< 1: better than the in-sample naive error)
  coverage  share of windows whose realized peak is at or below the upper bound
  short     share of windows where the upper bound implies fewer pods than the realized peak
            needs (pods = ceil(rps / RPS_PER_POD), clamped to [2, 7]): under-provisioning
  excess    mean pods implied minus pods needed: the cost of the bound
"current" is a reference, not a model: the latest observed value used as the forecast, i.e.
what a reactive scaler acts on (without its own detection lag).
"""

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE.parent / "src"))

from forecaster import models  # noqa: E402
from forecaster.config import Config  # noqa: E402
from forecaster.scoring import smape  # noqa: E402
from forecaster.window import predict  # noqa: E402

TRACES = REPO / "loadtest" / "trace" / "out"
RECORDED = REPO / "experiments" / "results" / "traces"
MIN_REPLICAS = 2


def simulate(trace, step=15, seed=0, pods=7, pre=300):
    """(times relative to the trace start, sum(rate(counter[1m])) values) on a `step` grid
    from -pre to the end of the trace.

    Arrivals are Poisson at the trace's rate, spread evenly over `pods` pods. Each pod's
    counter is scraped every `step` seconds at its own random phase, and rate() over 1 minute
    is the increase between the first and last scrape in the window divided by the time
    between them. The sum over pods averages the scrape phases, like the real series (the
    recorded runs had 7 pods).
    """
    rng = np.random.default_rng(seed)
    rps = np.asarray(trace["rps"], float)
    dur = trace["duration_seconds"]
    sec = np.arange(dur)
    lam = np.interp(sec + 0.5, np.arange(len(rps)) * trace["step_seconds"], rps)
    total = pre + dur + step + 60
    times = np.arange(-pre + 60, dur + step / 2, step, dtype=float)
    values = np.zeros(len(times))
    for _ in range(pods):
        counts = np.zeros(total)
        counts[pre:pre + dur] = rng.poisson(lam / pods)
        cum = np.concatenate([[0.0], np.cumsum(counts)])  # cum[k]: arrivals before second k-pre
        phase = rng.uniform(0, step)
        for j, T in enumerate(times):
            # Scrapes at phase + k*step inside (T-60, T].
            k0 = math.floor((T - 60 - phase) / step) + 1
            k1 = math.floor((T - phase) / step)
            s0, s1 = phase + k0 * step, phase + k1 * step
            c0 = cum[min(int(s0) + pre, len(cum) - 1)]
            c1 = cum[min(int(s1) + pre, len(cum) - 1)]
            values[j] += (c1 - c0) / (s1 - s0)
    return times, values


def load_recorded(name):
    d = json.loads((RECORDED / f"check-{name}.arrivals.json").read_text())
    t0 = d["trace_start_ms"] / 1000
    v = np.array(d["values"], float)
    return v[:, 0] - t0, v[:, 1]


class Current:
    name = "current"

    def min_history(self, season):
        return 1

    def forecast(self, y, season, horizon, quantile):
        v = np.full(horizon, float(y[-1]))
        return models.Forecast(v, v)


def pods(rps, cfg):
    return int(min(max(math.ceil(rps / cfg.rps_per_pod - 1e-9), MIN_REPLICAS), cfg.max_replicas))


def backtest(times, values, trace, cfg, model_list):
    """One row per (origin, model)."""
    n_hist = cfg.history_cycles * cfg.season
    first = trace["history_cycles"] * trace["cycle_seconds"]
    end = trace["duration_seconds"]
    sp = trace["spike"]
    rows = []
    for origin in np.arange(first, end - cfg.lead_seconds - cfg.window_seconds + 1,
                            cfg.interval_seconds):
        i = int(np.flatnonzero(np.abs(times - origin) < 1e-6)[0])
        hist = values[i - n_hist + 1:i + 1]
        season_err = float(np.mean(np.abs(hist[cfg.season:] - hist[:-cfg.season])))
        for m in model_list:
            t0 = time.perf_counter()
            pf = predict(m, hist, now=float(origin), last_sample=float(origin), cfg=cfg)
            fit_s = time.perf_counter() - t0
            idx = [int(np.flatnonzero(np.abs(times - t) < 1e-6)[0]) for t in pf.targets]
            actual = float(values[idx].max())
            in_spike = bool(sp and pf.targets[0] <= sp["end_s"] + 60
                            and pf.targets[-1] >= sp["start_s"])
            rows.append({
                "origin": float(origin), "model": m.name, "mean": pf.mean,
                "upper": pf.upper_raw, "actual": actual, "scale": season_err,
                "spike": in_spike, "fit_s": fit_s,
            })
    return rows


def summarize(rows, cfg):
    def agg(rs):
        if not rs:
            return None
        mean = np.array([r["mean"] for r in rs])
        up = np.array([r["upper"] for r in rs])
        act = np.array([r["actual"] for r in rs])
        implied = np.array([pods(u, cfg) for u in up])
        needed = np.array([pods(a, cfg) for a in act])
        return {
            "n": len(rs),
            "smape": smape(mean, act),
            "mase": float(np.mean(np.abs(mean - act)) / np.mean([r["scale"] for r in rs])),
            "coverage": float(np.mean(act <= up)),
            "short": float(np.mean(implied < needed)),
            "excess_pods": float(np.mean(implied - needed)),
            "fit_ms_median": float(np.median([r["fit_s"] for r in rs]) * 1000),
        }
    return {"scored": agg([r for r in rows if not r["spike"]]),
            "spike": agg([r for r in rows if r["spike"]])}


def check_simulation(cfg):
    """Mean absolute relative difference between simulated and recorded series."""
    out = {}
    for name in ("synthetic-s1", "wikipedia-s1"):
        trace = json.loads((TRACES / f"{name}.json").read_text())
        rt, rv = load_recorded(name)
        st, sv = simulate(trace, cfg.step_seconds, seed=1)
        # Recorded samples are not on the simulated grid (the run started mid-scrape):
        # compare at the recorded times, interpolating the simulation.
        m = (rt >= 60) & (rt <= trace["duration_seconds"])
        sim = np.interp(rt[m], st, sv)
        out[name] = float(np.mean(np.abs(sim - rv[m]) / rv[m]))
    return out


def regrid(times, values, step):
    """Shift a recorded series onto a grid aligned with the trace start (linear
    interpolation; the offset is under one step) so origins fall on samples."""
    grid = np.arange(math.ceil(times[0] / step) * step, times[-1] + 1e-6, step)
    return grid, np.interp(grid, times, values)


def run(cfg, seeds):
    model_list = [models.get(n) for n in cfg.models] + [Current()]
    series = []
    for workload in ("synthetic", "wikipedia"):
        for s in seeds:
            name = f"{workload}-s{s}"
            trace = json.loads((TRACES / f"{name}.json").read_text())
            t, v = simulate(trace, cfg.step_seconds, seed=s)
            series.append(("simulated", workload, name, trace, t, v))
        name = f"{workload}-s1"
        trace = json.loads((TRACES / f"{name}.json").read_text())
        t, v = regrid(*load_recorded(name), cfg.step_seconds)
        series.append(("recorded", workload, name, trace, t, v))

    rows = []
    for source, workload, name, trace, t, v in series:
        for r in backtest(t, v, trace, cfg, model_list):
            rows.append({"source": source, "workload": workload, "trace": name, **r})
        print(f"{source:9} {name}", file=sys.stderr)

    summary = {}
    for source in ("simulated", "recorded"):
        for workload in ("synthetic", "wikipedia"):
            for m in model_list:
                rs = [r for r in rows if (r["source"], r["workload"], r["model"]) ==
                      (source, workload, m.name)]
                summary[f"{source}/{workload}/{m.name}"] = summarize(rs, cfg)
    return rows, summary, series


def table(summary):
    lines = ["| series | workload | model | n | sMAPE | MASE | coverage | short | excess pods "
             "| spike coverage |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for key, s in summary.items():
        source, workload, model = key.split("/")
        a, sp = s["scored"], s["spike"]
        spike = f"{sp['coverage'] * 100:.0f} % of {sp['n']}" if sp else "–"
        lines.append(f"| {source} | {workload} | {model} | {a['n']} | {a['smape'] * 100:.1f} % "
                     f"| {a['mase']:.2f} | {a['coverage'] * 100:.0f} % | {a['short'] * 100:.0f} % "
                     f"| {a['excess_pods']:+.2f} | {spike} |")
    return "\n".join(lines)


def plot(series, rows, cfg, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"naive": "#2a78d6", "holtwinters": "#eb6834"}
    labels = {"naive": "seasonal naive", "holtwinters": "Holt-Winters"}
    ink, muted, shade = "#0b0b0b", "#52514e", "#efeeeb"
    plt.rcParams.update({"font.size": 8, "axes.edgecolor": muted, "axes.labelcolor": ink,
                         "xtick.color": muted, "ytick.color": muted,
                         "axes.spines.top": False, "axes.spines.right": False})
    rec = [s for s in series if s[0] == "recorded"]
    fig, axes = plt.subplots(len(rec), 1, figsize=(10, 3.1 * len(rec)), dpi=150, squeeze=False)
    for ax, (_, workload, name, trace, t, v) in zip(axes[:, 0], rec):
        lo = (trace["history_cycles"] - 1) * trace["cycle_seconds"]
        m = (t >= lo) & (t <= trace["duration_seconds"])
        ax.axvspan(lo, trace["history_cycles"] * trace["cycle_seconds"], color=shade, lw=0)
        ax.plot(t[m], v[m], color=muted, lw=0.8, label="arrivals (1-min rate)")
        rs = [r for r in rows if r["source"] == "recorded" and r["trace"] == name]
        x = sorted({r["origin"] for r in rs})
        act = [next(r["actual"] for r in rs if r["origin"] == o) for o in x]
        xs = np.array(x) + cfg.lead_seconds
        ax.step(xs, act, where="post", color=ink, lw=1.6, label="realized peak of the window")
        for mname in ("naive", "holtwinters"):
            up = [next(r["upper"] for r in rs if r["origin"] == o and r["model"] == mname)
                  for o in x]
            ax.step(xs, up, where="post", color=colors[mname], lw=1.2,
                    label=f"{labels[mname]}: upper bound of the peak")
        if trace["spike"]:
            s = trace["spike"]
            # Where the spike is in the 1-minute rate: from its start until a minute after.
            ax.axvspan(s["start_s"], s["end_s"] + 60, color="#e34948", alpha=0.08, lw=0)
            ax.text(s["start_s"], ax.get_ylim()[1] * 0.97, " spike (unforecastable)",
                    color=muted, va="top")
        title = {"synthetic": "Synthetic", "wikipedia": "German Wikipedia"}[workload]
        ax.set_title(f"{title} ({name}, recorded run). Each step: the peak over the next "
                     f"{cfg.window_seconds} s, forecast {cfg.lead_seconds} s before the step "
                     "begins", loc="left", color=ink)
        ax.set_ylabel("RPS")
        ax.set_xlabel("start of the forecast window, seconds since trace start (grey: last history cycle)")
        ax.set_ylim(bottom=0)
        ax.grid(axis="y", color=shade, lw=0.6)
        ax.legend(frameon=False, loc="upper left", ncol=2, bbox_to_anchor=(0, -0.22))
    fig.tight_layout()
    fig.savefig(out)
    print(f"wrote {out}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    ap.add_argument("--out-dir", type=Path, default=REPO / "experiments" / "results" / "backtest")
    ap.add_argument("--plot", type=Path)
    a = ap.parse_args()
    cfg = Config.from_env()

    check = check_simulation(cfg)
    rows, summary, series = run(cfg, a.seeds)
    a.out_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "config": {k: (list(v) if isinstance(v, tuple) else v)
                   for k, v in vars(cfg).items() if k not in ("prometheus_url",)},
        "simulation_vs_recorded_mean_abs_rel_diff": check,
        "summary": summary,
    }
    (a.out_dir / "backtest.json").write_text(json.dumps(result, indent=1) + "\n")
    with open(a.out_dir / "backtest-rows.csv", "w") as f:
        # Without fit times, so the file only changes when the forecasts do.
        keys = [k for k in rows[0] if k != "fit_s"]
        f.write(",".join(keys) + "\n")
        for r in rows:
            f.write(",".join(f"{r[k]:.4f}" if isinstance(r[k], float) else str(r[k])
                             for k in keys) + "\n")
    md = table(summary)
    (a.out_dir / "backtest.md").write_text(md + "\n")
    print(f"simulated vs recorded: {check}")
    print(md)
    if a.plot:
        plot(series, rows, cfg, a.plot)


if __name__ == "__main__":
    main()
