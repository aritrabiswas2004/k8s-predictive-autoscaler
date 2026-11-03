"""The experiment harness (Phase 6): runs every arm against the same traces, one at a time.

    python3 experiments/run_experiment.py plan main        # the runs of a grid and their status
    python3 experiments/run_experiment.py run main         # run everything not done yet
    python3 experiments/run_experiment.py run main --limit 10 --only wikipedia
    python3 experiments/run_experiment.py summarize main   # rebuild summary.csv from raw files

Grids:
  main   arms A, R100, R80, R60, C, D x traces synthetic/wikipedia seeds 1-5, startup delay 30 s
  sweep  arms R100, R80, D x synthetic seeds 1-3 x startup delay 0 and 60 s (the 30 s runs
         are the main grid's)
  smoke  every arm once on the 10-minute smoke trace (make experiment-smoke)

Repetition r uses trace seed r, so within a repetition every arm gets the identical trace and
spike, and the repetitions differ in both the run and the trace. The arm order is rotated by
one position per repetition, and every k6 start is delayed by 0-15 s (deterministic per run)
so runs don't all hit the same phase of the HPA's 15 s sync.

One run: set the startup delay (and the forecaster's LEAD = delay + 30 s) if it changed ->
wait for the forecaster to have 2 cycles of history -> experiments/arms.py apply (remove the
old scaler, 2 Ready pods, arrivals ~0, new scaler) -> jitter -> k6 replays the trace ->
keep the arm for TAIL more seconds -> save the Prometheus series -> remove the scaler.
Resumable: a run whose run.json says "ok" is skipped. Raw files per run in
experiments/results/<runs|smoke>/<run_id>/; the summary row is metrics.summarize().
"""

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path

import arms
import metrics

REPO = arms.REPO
TRACES = REPO / "loadtest/trace/out"
TAIL = 120  # seconds the arm stays active after the trace: scale-down shows in the series
PRE = 60  # seconds of series saved before the trace starts
ARMS = ["a", "r100", "r80", "r60", "c", "d"]
LEAD_MARGIN = 30  # forecaster LEAD = startup delay + 30 s (docs/methodology.md#forecaster)
MIN_HISTORY = 600  # seconds: 2 cycles, the forecaster's min_history_cycles
PROXY = "/api/v1/namespaces/monitoring/services/kube-prometheus-stack-prometheus:9090/proxy"

# Saved for every run (15 s steps on the epoch grid, like the forecaster's own queries).
SERIES = {
    "arrivals": arms.ARRIVALS,
    "served": 'sum(rate(http_requests_total{app="web"}[1m]))',
    "replicas": 'max(kube_deployment_status_replicas{namespace="default",deployment="web"})',
    "replicas_ready":
        'max(kube_deployment_status_replicas_ready{namespace="default",deployment="web"})',
    "replicas_spec": 'max(kube_deployment_spec_replicas{namespace="default",deployment="web"})',
    "hpa_desired":
        'max(kube_horizontalpodautoscaler_status_desired_replicas{namespace="default"})',
    "cpu_used": 'sum(rate(container_cpu_usage_seconds_total{namespace="default",'
                'pod=~"web-.*",container="app"}[1m]))',
    "cpu_throttled_ratio":
        'sum(rate(container_cpu_cfs_throttled_periods_total{namespace="default",'
        'pod=~"web-.*",container="app"}[1m])) / sum(rate(container_cpu_cfs_periods_total'
        '{namespace="default",pod=~"web-.*",container="app"}[1m]))',
    "queued": 'sum(work_requests_queued{app="web"})',
    "server_p95": 'histogram_quantile(0.95, sum by (le) '
                  '(rate(http_request_duration_seconds_bucket{app="web",route="GET /work"}[1m])))',
    "forecast_rps": "max by (model, bound) (forecast_rps)",
    "forecast_made_at": "max by (model) (forecast_last_success_timestamp_seconds)",
    "forecast_lead": "max(forecast_lead_seconds)",
    "forecast_window": "max(forecast_window_seconds)",
}


@dataclass(frozen=True)
class Run:
    grid: str
    arm: str
    trace: str
    delay: int
    rep: int
    order: int  # position in the grid

    @property
    def run_id(self):
        return f"{self.trace}-d{self.delay}-{self.arm}"


def rotate(xs, k):
    k %= len(xs)
    return xs[k:] + xs[:k]


def grid_runs(grid):
    runs = []
    if grid == "main":
        for rep in range(1, 6):
            workloads = ["synthetic", "wikipedia"] if rep % 2 else ["wikipedia", "synthetic"]
            for w in workloads:
                for arm in rotate(ARMS, rep - 1):
                    runs.append((arm, f"{w}-s{rep}", 30, rep))
    elif grid == "sweep":
        for rep in range(1, 4):
            for delay in ([0, 60] if rep % 2 else [60, 0]):
                for arm in rotate(["r100", "r80", "d"], rep - 1):
                    runs.append((arm, f"synthetic-s{rep}", delay, rep))
    elif grid == "smoke":
        runs = [(arm, "smoke", 30, 1) for arm in ARMS]
    else:
        raise SystemExit(f"unknown grid {grid}")
    return [Run(grid, a, t, d, r, i) for i, (a, t, d, r) in enumerate(runs)]


def grid_dir(grid):
    return REPO / "experiments/results" / ("smoke" if grid == "smoke" else "runs")


def run_dir(run):
    return grid_dir(run.grid) / run.run_id


def status(run):
    p = run_dir(run) / "run.json"
    return json.loads(p.read_text()).get("status") if p.exists() else None


def log(msg):
    arms.log(msg)


# --- cluster state -------------------------------------------------------------------------

def deployment_env(ns, name, var):
    d = json.loads(arms.kubectl("get", "deployment", name, "-n", ns, "-o", "json"))
    for c in d["spec"]["template"]["spec"]["containers"]:
        for e in c.get("env", []):
            if e["name"] == var:
                return e.get("value")
    return None


def set_delay(delay):
    """STARTUP_DELAY_SECONDS on web and LEAD_SECONDS = delay + 30 on the forecaster."""
    lead = delay + LEAD_MARGIN
    changed = False
    for ns, name, var, val in (("default", "web", "STARTUP_DELAY_SECONDS", delay),
                               ("monitoring", "forecaster", "LEAD_SECONDS", lead)):
        if deployment_env(ns, name, var) != str(val):
            log(f"setting {var}={val} on {ns}/{name}")
            arms.kubectl("set", "env", "-n", ns, f"deployment/{name}", f"{var}={val}")
            changed = True
        arms.kubectl("rollout", "status", "-n", ns, f"deployment/{name}", "--timeout=300s")
    if changed:
        # The forecaster restarts with its history in Prometheus; wait for its new config.
        arms.wait(f"forecast_lead_seconds = {lead}",
                  lambda: arms.query("max(forecast_lead_seconds)") == lead, 300, every=5)
    return lead


def preflight():
    nodes = json.loads(arms.kubectl("get", "nodes", "-o", "json"))["items"]
    for n in nodes:
        ready = {c["type"]: c["status"] for c in n["status"]["conditions"]}.get("Ready")
        if ready != "True":
            raise RuntimeError(f"node {n['metadata']['name']} not Ready")
    phase = subprocess.run(["kubectl", "-n", "loadtest", "get", "pod", "k6", "-o",
                            "jsonpath={.status.phase}"], capture_output=True, text=True).stdout
    if phase in ("Pending", "Running"):
        raise RuntimeError("a k6 pod is already running")
    arms.wait("forecaster history >= 2 cycles",
              lambda: (arms.query("max(forecast_history_seconds)") or 0) >= MIN_HISTORY,
              1200, every=15)


def query_range(promql, start, end, step=metrics.STEP):
    q = urllib.parse.urlencode({"query": promql, "start": start, "end": end, "step": step})
    body = json.loads(arms.kubectl("get", "--raw", f"{PROXY}/api/v1/query_range?{q}"))
    return [{"metric": s["metric"], "values": [[float(t), float(v)] for t, v in s["values"]]}
            for s in body["data"]["result"]]


def fetch_series(t0, duration):
    start = math.floor((t0 - PRE) / metrics.STEP) * metrics.STEP
    end = min(t0 + duration + TAIL, time.time())
    end = start + math.floor((end - start) / metrics.STEP) * metrics.STEP
    return {"start": start, "end": end, "step": metrics.STEP, "queries": SERIES,
            "series": {k: query_range(q, start, end) for k, q in SERIES.items()}}


def environment():
    def out(*cmd):
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)
        return r.stdout.strip()
    dirty = out("git", "status", "--porcelain", "--", ".", ":!experiments/results")
    return {
        "git_sha": out("git", "rev-parse", "HEAD"),
        "git_dirty": bool(dirty),
        "helm_releases": json.loads(out("helm", "list", "-A", "-o", "json") or "[]"),
        "kubernetes": json.loads(out("kubectl", "version", "-o", "json") or "{}")
        .get("serverVersion", {}).get("gitVersion"),
        "k6_image": os.environ.get("K6_IMAGE", "default of loadtest/run-k6.sh"),
    }


def jitter(run):
    return int(hashlib.sha256(run.run_id.encode()).hexdigest(), 16) % 1500 / 100  # 0-15 s


# --- one run -------------------------------------------------------------------------------

def execute(run):
    d = run_dir(run)
    d.mkdir(parents=True, exist_ok=True)
    trace_file = TRACES / f"{run.trace}.json"
    trace = json.loads(trace_file.read_text())
    meta = {"run_id": run.run_id, **asdict(run), "startup_delay_seconds": run.delay,
            "status": "running", "started_at": dt.datetime.now(dt.UTC).isoformat(),
            "rps_per_pod": metrics.RPS_PER_POD, "tail_seconds": TAIL, **environment()}
    meta["workload"] = trace["workload"]
    meta["seed"] = trace["seed"]
    (d / "run.json").write_text(json.dumps(meta, indent=1) + "\n")
    try:
        preflight()
        meta["lead_seconds"] = set_delay(run.delay)
        arms.apply(run.arm)
        meta["jitter_seconds"] = jitter(run)
        time.sleep(meta["jitter_seconds"])
        env = {**os.environ, "K6_FILES": f"trace.json={trace_file}"}
        subprocess.run([str(REPO / "loadtest/run-k6.sh"), str(REPO / "loadtest/k6/trace.js"),
                        str(d / "k6"), f"RUN_ID={run.run_id}"], env=env, check=True, cwd=REPO)
        t0 = int(re.search(r"TRACE_START=(\d+)", (d / "k6.log").read_text()).group(1)) / 1000
        meta["trace_start"] = t0
        remaining = t0 + trace["duration_seconds"] + TAIL - time.time()
        if remaining > 0:
            log(f"keeping arm {run.arm} for {remaining:.0f} s after the trace")
            time.sleep(remaining)
        (d / "series.json").write_text(
            json.dumps(fetch_series(t0, trace["duration_seconds"]), separators=(",", ":")) + "\n")
        meta["finished_at"] = dt.datetime.now(dt.UTC).isoformat()
        meta["summary"] = summarize_dir(d, meta, trace)
        meta["status"] = "ok"
    except BaseException as e:  # includes Ctrl-C: record it, then stop
        meta["status"] = "failed"
        meta["error"] = "".join(traceback.format_exception_only(e)).strip()
        meta["traceback"] = traceback.format_exc()
        raise
    finally:
        (d / "run.json").write_text(json.dumps(meta, indent=1) + "\n")
        try:
            arms.clear()
        except Exception as e:  # noqa: BLE001 - best effort, the next apply clears again
            log(f"clear after run failed: {e}")
    return meta


def summarize_dir(d, meta, trace):
    k6 = json.loads((d / "k6.json").read_text())
    series = json.loads((d / "series.json").read_text())["series"]
    stats = d / "k6.docker-stats.csv"
    return metrics.summarize(meta, k6, series, trace,
                             metrics.docker_stats(stats) if stats.exists() else None)


def write_summary(grid):
    rows = []
    for p in sorted(grid_dir(grid).glob("*/run.json")):
        meta = json.loads(p.read_text())
        if meta.get("status") == "ok":
            rows.append(meta["summary"])
    if not rows:
        return None
    out = grid_dir(grid) / "summary.csv"
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return out


# --- commands ------------------------------------------------------------------------------

def cmd_plan(a):
    runs = select(a)
    for r in runs:
        print(f"{r.order:3d}  {status(r) or 'pending':8s} {r.run_id:28s} rep {r.rep}")
    done = sum(status(r) == "ok" for r in runs)
    per = (json.loads((TRACES / f"{runs[0].trace}.json").read_text())["duration_seconds"]
           + TAIL + 120) if runs else 0
    print(f"{done}/{len(runs)} done; about {(len(runs) - done) * per / 3600:.1f} h to go")


def select(a):
    runs = grid_runs(a.grid)
    if getattr(a, "only", None):
        runs = [r for r in runs if re.search(a.only, r.run_id)]
    return runs


def cmd_run(a):
    pending = [r for r in select(a) if status(r) != "ok"]
    if a.limit:
        pending = pending[:a.limit]
    log(f"{len(pending)} runs to do in grid {a.grid}")
    failures = 0
    for i, r in enumerate(pending, 1):
        log(f"=== run {i}/{len(pending)}: {r.run_id} (order {r.order}, rep {r.rep})")
        try:
            meta = execute(r)
            s = meta["summary"]
            log(f"=== {r.run_id} ok: {s['slo_violation_minutes']}/{s['scored_minutes']} "
                f"SLO-violation minutes, {s['replica_seconds']:.0f} replica-s")
            failures = 0
        except KeyboardInterrupt:
            log("interrupted; the run is marked failed and will be redone")
            raise SystemExit(130)
        except Exception as e:  # noqa: BLE001 - keep going, but not forever
            failures += 1
            log(f"=== {r.run_id} FAILED: {e}")
            if failures >= 2:
                raise SystemExit("two runs in a row failed; stopping")
        write_summary(a.grid)
    out = write_summary(a.grid)
    if out:
        log(f"summary: {out.relative_to(REPO)}")


def cmd_summarize(a):
    for r in select(a):
        d = run_dir(r)
        if status(r) != "ok":
            continue
        meta = json.loads((d / "run.json").read_text())
        meta["summary"] = summarize_dir(d, meta, json.loads(
            (TRACES / f"{r.trace}.json").read_text()))
        (d / "run.json").write_text(json.dumps(meta, indent=1) + "\n")
    out = write_summary(a.grid)
    print(out.relative_to(REPO) if out else "no finished runs")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("plan", cmd_plan), ("run", cmd_run), ("summarize", cmd_summarize)):
        p = sub.add_parser(name)
        p.add_argument("grid", choices=["main", "sweep", "smoke"])
        p.add_argument("--only", help="regex on the run id, e.g. 'wikipedia' or '-d60-'")
        if name == "run":
            p.add_argument("--limit", type=int, help="stop after this many runs")
        p.set_defaults(fn=fn)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
