"""Replica timeline of one or more arm runs: offered load, arrival rate, forecast, replicas.

    .venv/bin/python experiments/analysis/arm_timeline.py RUN [RUN ...] --plot docs/img/arms-smoke.png

RUN is a k6 result prefix written by run-k6.sh with loadtest/k6/trace.js (e.g.
experiments/results/arms/smoke-r100); the arm is taken from the RUN_ID (smoke-<arm>). The
first time, the series are fetched from Prometheus through the API server's service proxy (no
port-forward needed) and saved as RUN.series.json; later invocations read that file.

Replicas are kube-state-metrics' view of the Deployment: spec (what the scaler asked for) and
ready (serving). The offered load is the trace itself, the ground truth k6 was told to send.
"""

import argparse
import json
import math
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import numpy as np

STEP = 15
REPO = Path(__file__).resolve().parents[2]
PROXY = "/api/v1/namespaces/monitoring/services/kube-prometheus-stack-prometheus:9090/proxy"
QUERIES = {
    "arrivals": 'sum(rate(http_requests_received_total{app="web"}[1m]))',
    "forecast": 'max(forecast_rps{model="naive",bound="upper"})',
    "spec_replicas": 'max(kube_deployment_spec_replicas{namespace="default",deployment="web"})',
    "ready_replicas": 'max(kube_deployment_status_replicas_ready{namespace="default",'
                      'deployment="web"})',
}
LABELS = {"a": "A: HPA on CPU (70 %)", "r100": "R100: KEDA on arrivals, 40 RPS/pod",
          "r80": "R80: KEDA on arrivals, 32 RPS/pod", "r60": "R60: KEDA on arrivals, 24 RPS/pod",
          "c": "C: forecast only", "d": "D: forecast + arrivals"}


def log_text(run):
    return Path(f"{run}.log").read_text()


def summary(run):
    return json.loads(log_text(run).split("===SUMMARY-BEGIN===")[1].split("===SUMMARY-END===")[0])


def query_range(query, start, end):
    q = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": STEP})
    out = subprocess.run(["kubectl", "get", "--raw", f"{PROXY}/api/v1/query_range?{q}"],
                         capture_output=True, text=True, check=True).stdout
    result = json.loads(out)["data"]["result"]
    return [[float(t), float(v)] for t, v in result[0]["values"]] if result else []


def fetch(run):
    s = summary(run)
    t0 = int(re.search(r"TRACE_START=(\d+)", log_text(run)).group(1)) / 1000
    trace = json.loads((REPO / "loadtest/trace/out" / f"{s['config']['trace']}.json").read_text())
    start, end = t0 - 60, t0 + trace["duration_seconds"] + 180
    if end > time.time():
        raise SystemExit(f"{run}: the series end {end - time.time():.0f} s from now; "
                         "fetch after that, or the saved file would be cut short")
    data = {"run": s["config"]["run"], "trace": trace["name"], "trace_start": t0,
            "step_seconds": STEP, "queries": QUERIES,
            "series": {k: query_range(q, start, end) for k, q in QUERIES.items()}}
    Path(f"{run}.series.json").write_text(json.dumps(data, separators=(",", ":")) + "\n")
    return data


def load(run):
    p = Path(f"{run}.series.json")
    return json.loads(p.read_text()) if p.exists() else fetch(run)


def arm_of(data):
    return data["run"].rsplit("-", 1)[-1]


def describe(run, data):
    """Numbers for the acceptance check: did the replica count follow the load?"""
    s = summary(run)
    t0 = data["trace_start"]
    trace = json.loads((REPO / "loadtest/trace/out" / f"{data['trace']}.json").read_text())
    dur = trace["duration_seconds"]

    def during(name):
        v = np.array(data["series"][name] or [[t0, np.nan]])
        m = (v[:, 0] >= t0) & (v[:, 0] <= t0 + dur)
        return v[m, 0] - t0, v[m, 1]

    _, spec = during("spec_replicas")
    rt, ready = during("ready_replicas")
    changes = int(np.sum(np.diff(spec[~np.isnan(spec)]) != 0))
    # Replica-seconds while the trace ran (ready pods, 15 s samples).
    replica_seconds = float(np.nansum(ready) * STEP)
    # Seconds where ready pods could not carry the offered load at RPS_PER_POD = 40.
    offered = np.interp(rt, np.arange(len(trace["rps"])) * trace["step_seconds"], trace["rps"])
    needed = np.array([max(2, min(7, math.ceil(o / 40 - 1e-9))) for o in offered])
    short = float(np.sum(ready < needed) * STEP)
    return {"arm": arm_of(data), "requests": s["requests"], "offered": s["offered"],
            "dropped": s["dropped"], "failed_rate": s["failed_rate"], "p95_ms": s["p95_ms"],
            "p99_ms": s["p99_ms"], "replicas_min": float(np.nanmin(ready)),
            "replicas_max": float(np.nanmax(ready)), "replica_changes": changes,
            "replica_seconds": replica_seconds, "short_seconds": short}


def plot(datas, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ink, muted, shade = "#0b0b0b", "#52514e", "#efeeeb"
    blue, orange, green = "#2a78d6", "#eb6834", "#1f9d6b"
    plt.rcParams.update({"font.size": 8, "axes.edgecolor": muted, "axes.labelcolor": ink,
                         "xtick.color": muted, "ytick.color": muted,
                         "axes.spines.top": False, "axes.spines.right": False})
    n = len(datas)
    cols = 2 if n > 1 else 1
    rows = math.ceil(n / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(5.2 * cols, 2.5 * rows + 0.5), dpi=150,
                             squeeze=False, sharex=True)
    for ax in axes.flat[n:]:
        ax.set_visible(False)
    for ax, data in zip(axes.flat, datas):
        t0 = data["trace_start"]
        trace = json.loads((REPO / "loadtest/trace/out" / f"{data['trace']}.json").read_text())
        dur = trace["duration_seconds"]
        # Replicas are drawn as capacity (x 40 RPS) on the RPS axis, so "capacity below the
        # load" is visible directly; the right axis reads the same line in pods.
        tt = np.arange(len(trace["rps"])) * trace["step_seconds"]
        ax.plot(tt, trace["rps"], color=muted, lw=1, ls=(0, (2, 2)), label="offered (trace)")
        for name, color, label in (("arrivals", ink, "arrivals, rate[1m]"),
                                   ("forecast", blue, "forecast upper bound, peak 60-120 s ahead")):
            v = np.array(data["series"][name] or [[t0, np.nan]])
            ax.plot(v[:, 0] - t0, v[:, 1], color=color, lw=1.3 if name == "arrivals" else 1.1,
                    label=label)
        r = np.array(data["series"]["ready_replicas"] or [[t0, np.nan]])
        ax.step(r[:, 0] - t0, r[:, 1] * 40, where="post", color=orange, lw=1.6,
                label="capacity: ready pods x 40 RPS")
        ax.set_xlim(-30, dur + 150)
        ax.set_ylim(0, 300)
        ax.axvspan(dur, dur + 150, color=shade, lw=0)
        ax.set_title(LABELS.get(arm_of(data), data["run"]), loc="left", color=ink)
        ax.grid(axis="y", color=shade, lw=0.6)
        sec = ax.secondary_yaxis("right", functions=(lambda x: x / 40, lambda x: x * 40))
        sec.set_ylabel("pods", color=muted)
        ax.set_ylabel("RPS")
    for ax in axes[-1]:
        ax.set_xlabel("seconds since trace start (grey: after the trace)")
    h, lab = axes.flat[0].get_legend_handles_labels()
    top = 1 - 0.45 / fig.get_figheight()  # room for the legend above the panels
    fig.tight_layout(rect=(0, 0, 1, top))
    fig.legend(h, lab, frameon=False, loc="upper center", ncol=4, bbox_to_anchor=(0.5, 1))
    fig.savefig(out)
    print(f"wrote {out}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--plot")
    a = ap.parse_args()
    datas = []
    print("| arm | requests / offered | dropped | failed | p95 | p99 | ready pods | changes "
          "| replica-s | short of pods |")
    print("|---|---:|---:|---:|---:|---:|---|---:|---:|---:|")
    for run in a.runs:
        data = load(run)
        datas.append(data)
        d = describe(run, data)
        Path(f"{run}.timeline.json").write_text(json.dumps(d, indent=1) + "\n")
        print(f"| {d['arm']} | {d['requests']} / {d['offered']} | {d['dropped']} | "
              f"{d['failed_rate'] * 100:.2f} % | {d['p95_ms']:.0f} ms | {d['p99_ms']:.0f} ms | "
              f"{d['replicas_min']:.0f}-{d['replicas_max']:.0f} | {d['replica_changes']} | "
              f"{d['replica_seconds']:.0f} | {d['short_seconds']:.0f} s |")
    if a.plot:
        plot(datas, a.plot)


if __name__ == "__main__":
    main()
