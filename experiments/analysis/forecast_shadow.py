"""Forecast quality of the live forecaster during a trace replay (shadow mode).

    # with Prometheus port-forwarded to localhost:9090:
    .venv/bin/python experiments/analysis/forecast_shadow.py RUN [RUN ...] --plot docs/img/forecast-shadow.png

RUN is a k6 result prefix written by run-k6.sh (e.g. experiments/results/forecaster/
shadow-synthetic-s1). The first time, the arrival rate and forecast_rps series of the run are
fetched from Prometheus and saved as RUN.series.json; later invocations read that file, so
the analysis is reproducible without the cluster.

Scoring matches the backtest (forecaster/backtest/backtest.py) and the service's own
forecast_smape: each forecast made in the scored cycles (identified by
forecast_last_success_timestamp_seconds) is compared with the realized peak of the arrival
rate at exactly the samples its window covered (the forecaster's 15 s grid, from the first
sample at or after made + LEAD). Windows overlapping the spike, or the minute after it, are
reported separately.

Not "the value Prometheus holds at t vs the peak over [t + LEAD, ...]": an exported value
can be up to 45 s old (30 s interval + 15 s scrape), and LEAD already allows for that
(docs/methodology.md#what-it-forecasts-and-how-far-ahead), so that would count it twice.
"""

import argparse
import json
import math
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

STEP = 15
ARRIVALS = 'sum(rate(http_requests_received_total{app="web"}[1m]))'
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "forecaster" / "src"))
from forecaster.window import horizon_steps  # noqa: E402


def trace_start(run):
    log = Path(f"{run}.log").read_text()
    return int(re.search(r"TRACE_START=(\d+)", log).group(1)) / 1000


def summary(run):
    log = Path(f"{run}.log").read_text()
    return json.loads(log.split("===SUMMARY-BEGIN===")[1].split("===SUMMARY-END===")[0])


def query_range(prom, query, start, end):
    q = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": STEP})
    with urllib.request.urlopen(f"{prom}/api/v1/query_range?{q}", timeout=30) as r:
        body = json.load(r)
    return [{"metric": s["metric"], "values": [[float(t), float(v)] for t, v in s["values"]]}
            for s in body["data"]["result"]]


def fetch(run, prom):
    s = summary(run)
    trace = json.loads((REPO / "loadtest/trace/out" / f"{s['config']['trace']}.json").read_text())
    t0 = trace_start(run)
    # Align the grid to the trace start so samples fall on whole 15 s steps of the trace.
    start, end = t0 - 60, t0 + trace["duration_seconds"] + 180
    data = {
        "run": Path(run).name, "trace": trace["name"], "trace_start": t0, "step_seconds": STEP,
        "arrivals": query_range(prom, ARRIVALS, start, end),
        "forecast_rps": query_range(prom, "forecast_rps", start, end),
        "made_at": query_range(prom, "forecast_last_success_timestamp_seconds", start, end),
        # The forecaster's own grid (multiples of 15 s since the epoch), for "own window".
        "arrivals_forecaster_grid": query_range(prom, ARRIVALS, start - start % STEP, end),
        "lead_seconds": query_range(prom, "forecast_lead_seconds", start, end),
        "window_seconds": query_range(prom, "forecast_window_seconds", start, end),
    }
    Path(f"{run}.series.json").write_text(json.dumps(data, separators=(",", ":")) + "\n")
    return data


def last_value(series):
    return series[0]["values"][-1][1]


def pods(rps, rps_per_pod=40, lo=2, hi=7):
    return min(max(math.ceil(rps / rps_per_pod - 1e-9), lo), hi)


def analyse(data):
    trace = json.loads((REPO / "loadtest/trace/out" / f"{data['trace']}.json").read_text())
    t0 = data["trace_start"]
    lead, window = int(last_value(data["lead_seconds"])), int(last_value(data["window_seconds"]))
    arr = np.array(data["arrivals"][0]["values"])
    at, av = arr[:, 0] - t0, arr[:, 1]
    grid = {round(t, 3): v for t, v in data["arrivals_forecaster_grid"][0]["values"]}

    # Each forecast once: the scraped forecast_rps values that share one
    # forecast_last_success_timestamp_seconds (made) value. Times relative to the trace start.
    values = {}
    for s in data["forecast_rps"]:
        values[(s["metric"]["model"], s["metric"]["bound"])] = dict(s["values"])
    forecasts = {}
    for s in data["made_at"]:
        m = s["metric"]["model"]
        seen = {}
        for t, made in s["values"]:
            if made not in seen and t in values.get((m, "upper"), {}):
                seen[made] = {"made": made - t0, "mean": values[(m, "mean")][t],
                              "upper": values[(m, "upper")][t]}
        forecasts[m] = sorted(seen.values(), key=lambda f: f["made"])

    def own_peak(made_abs):
        """Realized peak at exactly the samples the forecast's window covered."""
        last = math.floor(made_abs / STEP) * STEP
        while round(last, 3) not in grid:
            last -= STEP
        steps = horizon_steps(made_abs, last, STEP, lead, window)
        vals = [grid.get(round(last + h * STEP, 3)) for h in steps]
        return None if None in vals else max(vals)

    first = trace["history_cycles"] * trace["cycle_seconds"]
    sp = trace["spike"]
    rows = []
    for m, fs in forecasts.items():
        for f in fs:
            w0 = f["made"] + lead
            if f["made"] < first or w0 + window > trace["duration_seconds"]:
                continue
            actual = own_peak(f["made"] + t0)
            if actual is None:
                continue
            rows.append({**f, "model": m, "actual": actual,
                         "spike": bool(sp and w0 <= sp["end_s"] + 60
                                       and w0 + window >= sp["start_s"])})
    out = {}
    for m in sorted(forecasts):
        for part in (False, True):
            rs = [r for r in rows if r["model"] == m and r["spike"] == part]
            if not rs:
                continue
            mean = np.array([r["mean"] for r in rs])
            up = np.array([r["upper"] for r in rs])
            act = np.array([r["actual"] for r in rs])
            implied = np.array([pods(u) for u in up])
            needed = np.array([pods(a) for a in act])
            den = np.abs(mean) + np.abs(act)
            out[f"{m}/{'spike' if part else 'scored'}"] = {
                "n": len(rs),
                "smape": float(np.mean(np.where(den == 0, 0, 2 * np.abs(mean - act) / den))),
                "coverage": float(np.mean(act <= up)),
                "short": float(np.mean(implied < needed)),
                "excess_pods": float(np.mean(implied - needed)),
            }
    return {"lead": lead, "window": window, "trace": trace, "at": at, "av": av,
            "forecasts": forecasts, "rows": rows, "scores": out}


def plot(results, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"naive": "#2a78d6", "holtwinters": "#eb6834"}
    labels = {"naive": "seasonal naive", "holtwinters": "Holt-Winters"}
    ink, muted, shade = "#0b0b0b", "#52514e", "#efeeeb"
    plt.rcParams.update({"font.size": 8, "axes.edgecolor": muted, "axes.labelcolor": ink,
                         "xtick.color": muted, "ytick.color": muted,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(len(results), 1, figsize=(10, 3.1 * len(results)), dpi=150,
                             squeeze=False)
    for ax, r in zip(axes[:, 0], results):
        tr, lead, window = r["trace"], r["lead"], r["window"]
        end = tr["duration_seconds"]
        hist = tr["history_cycles"] * tr["cycle_seconds"]
        ax.axvspan(0, hist, color=shade, lw=0)
        ax.text(hist / 2, 1, "history: the forecaster's window still holds what came before "
                "the run", transform=ax.get_xaxis_transform(), ha="center", va="top",
                color=muted)
        m = (r["at"] >= 0) & (r["at"] <= end)
        ax.plot(r["at"][m], r["av"][m], color=ink, lw=1.4, label="arrivals (1-min rate)")
        for model in ("naive", "holtwinters"):
            fs = [f for f in r["forecasts"].get(model, []) if 0 <= f["made"] + lead <= end]
            if not fs:
                continue
            # Plotted at its window start: the value at x is the forecast for [x, x + window].
            ax.step([f["made"] + lead for f in fs], [f["upper"] for f in fs], where="post",
                    color=colors[model], lw=1.1,
                    label=f"{labels[model]}: upper bound for [x, x+{window} s], made {lead} s "
                          "before x")
        if tr["spike"]:
            s = tr["spike"]
            ax.axvspan(s["start_s"], s["end_s"] + 60, color="#e34948", alpha=0.08, lw=0)
        title = {"synthetic": "Synthetic", "wikipedia": "German Wikipedia"}[tr["workload"]]
        ax.set_title(f"{title} ({tr['name']}), live forecaster in shadow mode, 7 fixed replicas",
                     loc="left", color=ink)
        ax.set_ylabel("RPS")
        ax.set_xlabel("seconds since trace start (grey: history cycles, red: spike)")
        ax.set_ylim(0, 300)
        ax.set_xlim(0, end)
        ax.grid(axis="y", color=shade, lw=0.6)
        ax.legend(frameon=False, loc="upper left", ncol=2, bbox_to_anchor=(0, -0.22))
    fig.tight_layout()
    fig.savefig(out)
    print(f"wrote {out}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--prometheus", default="http://localhost:9090")
    ap.add_argument("--plot")
    a = ap.parse_args()
    results = []
    for run in a.runs:
        p = Path(f"{run}.series.json")
        data = json.loads(p.read_text()) if p.exists() else fetch(run, a.prometheus)
        r = analyse(data)
        results.append(r)
        Path(f"{run}.forecast.json").write_text(json.dumps(
            {"lead": r["lead"], "window": r["window"], "scores": r["scores"]}, indent=1) + "\n")
        print(f"\n{data['run']} (lead {r['lead']} s, window {r['window']} s)")
        print("| model | windows | n | sMAPE | coverage | short | excess pods |")
        print("|---|---|---:|---:|---:|---:|---:|")
        for k, s in r["scores"].items():
            model, part = k.split("/")
            print(f"| {model} | {part} | {s['n']} | {s['smape'] * 100:.1f} % | "
                  f"{s['coverage'] * 100:.0f} % | {s['short'] * 100:.0f} % | "
                  f"{s['excess_pods']:+.2f} |")
    if a.plot:
        plot(results, a.plot)


if __name__ == "__main__":
    main()
