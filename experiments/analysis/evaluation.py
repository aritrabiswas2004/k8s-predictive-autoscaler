"""Phase 7 numbers: per-run rows, per-arm statistics, the frontier test and forecast scores.

Standard library only, so it is tested in CI without a cluster or numpy (make
test-experiments). analyze.py turns these numbers into the figures and tables in docs/.

Inputs are the raw files the harness saves per run (experiments/run_experiment.py): run.json,
k6.json, series.json and k6.docker-stats.csv. Every number is recomputed from them, with the
same code as the harness's own summary row (experiments/metrics.py).

Statistics. A cell is one (workload, arm, startup delay). Its runs are the repetitions, and
repetition r replays trace seed r, so the spread includes both run-to-run noise and the
difference between traces. Means come with a two-sided 95 % Student-t interval. Comparisons
between arms are paired within a repetition, where every arm saw the identical trace.

The frontier test (docs/methodology.md#phase-7-analysis). In each repetition the three
reactive arms R100, R80 and R60 give three (cost, violations) points, joined in order of cost
into a piecewise-linear frontier. An arm's gap in that repetition is its violations minus the
frontier's value at the arm's cost. Below the cheapest reactive arm the frontier is held flat
at that arm's violations (an arm that is cheaper than R100 must also violate less to count as
better). Above the most expensive one it is not defined: the gap is None, because nothing was
measured there and extending the frontier flat would credit an arm for buying more pods than
R60. The arm is below the frontier when the 95 % interval of its gaps is entirely below 0.
"""

import csv
import json
import math
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "experiments"))
import metrics  # noqa: E402

TRACES = REPO / "loadtest" / "trace" / "out"
REACTIVE = ("r100", "r80", "r60")
ARMS = ("a", "r100", "r80", "r60", "c", "d")
MODELS = ("naive", "holtwinters")

# Reference price: AWS Fargate, Linux/x86, US East (N. Virginia), on-demand, as listed on
# https://aws.amazon.com/fargate/pricing/ on 2026-11-03: $0.000011244 per vCPU-second and
# $0.000001235 per GB-second. A pod requests 0.5 vCPU and 128 MiB; the smallest Fargate size
# with 0.5 vCPU has 1 GB, so one pod-second is priced as 0.5 vCPU + 1 GB.
PRICE_VCPU_SECOND = 0.000011244
PRICE_GB_SECOND = 0.000001235
POD_VCPU, POD_GB = 0.5, 1.0
PRICE_POD_SECOND = POD_VCPU * PRICE_VCPU_SECOND + POD_GB * PRICE_GB_SECOND
PRICE_SOURCE = ("AWS Fargate, Linux/x86, us-east-1, on-demand, listed 2026-11-03: "
                "$0.000011244 per vCPU-s, $0.000001235 per GB-s; a pod = 0.5 vCPU + 1 GB")

# Two-sided 95 % Student-t quantiles by degrees of freedom.
T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
        9: 2.262, 10: 2.228, 15: 2.131, 20: 2.086, 30: 2.042}


def t975(df):
    if df in T975:
        return T975[df]
    below = [d for d in T975 if d <= df]
    return T975[max(below)] if below else T975[1]  # conservative: next lower df


@dataclass(frozen=True)
class Stat:
    n: int
    mean: float | None
    lo: float | None  # 95 % interval; None with fewer than 2 values
    hi: float | None
    values: tuple

    @property
    def half(self):
        return None if self.lo is None else (self.hi - self.lo) / 2


def mean_ci(values):
    xs = [float(v) for v in values if v is not None and not math.isnan(float(v))]
    if not xs:
        return Stat(0, None, None, None, ())
    m = statistics.fmean(xs)
    if len(xs) < 2:
        return Stat(1, m, None, None, tuple(xs))
    h = t975(len(xs) - 1) * statistics.stdev(xs) / math.sqrt(len(xs))
    return Stat(len(xs), m, m - h, m + h, tuple(xs))


def verdict(stat):
    """Below / above 0 when the 95 % interval excludes 0, else within noise."""
    if stat.n < 2:
        return "n < 2"
    if stat.hi < 0:
        return "below"
    if stat.lo > 0:
        return "above"
    return "within noise"


# ---------------------------------------------------------------- per run

def spike_minutes(trace):
    """Minutes of the trace that overlap the spike or the minute after it (queue drain)."""
    sp = trace.get("spike")
    if not sp:
        return set()
    lo, hi = sp["start_s"], sp["end_s"] + 60
    return {m for m in range(math.ceil(trace["duration_seconds"] / 60))
            if m * 60 < hi and m * 60 + 60 > lo}


def series_of(series, name, **labels):
    """{time: value} of the one series of `name` whose labels include `labels`."""
    for s in series.get(name, []):
        if all(s["metric"].get(k) == v for k, v in labels.items()):
            return {float(t): float(v) for t, v in s["values"] if not math.isnan(float(v))}
    return {}


def forecasts(series, model):
    """Each forecast once, [(made_at, mean, upper)] in unix time.

    The forecaster exports forecast_rps and forecast_last_success_timestamp_seconds in the
    same scrape, so the samples that share one made_at value hold one forecast; the first of
    them is used."""
    made = series_of(series, "forecast_made_at", model=model)
    mean = series_of(series, "forecast_rps", model=model, bound="mean")
    upper = series_of(series, "forecast_rps", model=model, bound="upper")
    seen = {}
    for t in sorted(made):
        if made[t] not in seen and t in mean and t in upper:
            seen[made[t]] = (made[t], mean[t], upper[t])
    return sorted(seen.values())


def window_targets(made, lead, window, step=metrics.STEP):
    """Epoch-grid sample times in [made + lead, made + lead + window].

    The same samples as forecaster.window.horizon_steps counted from the newest sample (the
    test checks this): steps after the last sample are on the same epoch grid, and the
    window starts after it."""
    first = math.ceil((made + lead) / step - 1e-9)
    last = math.floor((made + lead + window) / step + 1e-9)
    return [k * step for k in range(first, last + 1)]


def pods_for(rps):
    return min(max(math.ceil(rps / metrics.RPS_PER_POD - 1e-9), 2), 7)


def forecast_scores(series, trace, t0):
    """Peak sMAPE, 90 % coverage and short-of-pods share per model, over the forecasts made in
    the scored cycles whose window ends within the trace; windows touching the spike (or the
    minute after it) are counted separately, as in the backtest."""
    arrivals = series_of(series, "arrivals")
    leads = series_of(series, "forecast_lead")
    windows = series_of(series, "forecast_window")
    if not leads or not windows:
        return {}
    lo, hi = metrics.scored_window(trace)
    sp = trace.get("spike")
    out = {}
    for model in MODELS:
        rows = {"scored": [], "spike": []}
        for made, mean, upper in forecasts(series, model):
            # The lead and window in force when the forecast was made.
            at = max((t for t in leads if t <= made + metrics.STEP), default=min(leads))
            lead, window = leads[at], windows.get(at, statistics.median(windows.values()))
            w0 = made - t0 + lead
            if made - t0 < lo or w0 + window > hi:
                continue
            vals = [arrivals.get(t) for t in window_targets(made, lead, window)]
            if not vals or None in vals:
                continue
            actual = max(vals)
            part = "spike" if sp and w0 <= sp["end_s"] + 60 and w0 + window >= sp["start_s"] \
                else "scored"
            rows[part].append((mean, upper, actual))
        for part, rs in rows.items():
            if not rs:
                continue
            sm = [0.0 if m + a == 0 else 2 * abs(m - a) / (abs(m) + abs(a)) for m, _, a in rs]
            out[f"{model}_{part}"] = {
                "n": len(rs), "smape": statistics.fmean(sm),
                "coverage": statistics.fmean(a <= u for _, u, a in rs),
                "short": statistics.fmean(pods_for(u) < pods_for(a) for _, u, a in rs),
            }
    return out


def load_run(d):
    """Everything about one finished run, or None if it did not finish."""
    d = Path(d)
    try:
        meta = json.loads((d / "run.json").read_text())
    except FileNotFoundError:
        return None
    if meta.get("status") != "ok":
        return None
    k6 = json.loads((d / "k6.json").read_text())
    series = json.loads((d / "series.json").read_text())["series"]
    trace = json.loads((TRACES / f"{meta['trace']}.json").read_text())
    stats_path = d / "k6.docker-stats.csv"
    stats = metrics.docker_stats(stats_path) if stats_path.exists() else None
    return {"dir": d, "meta": meta, "k6": k6, "series": series, "trace": trace, "stats": stats}


def run_row(run):
    """The harness's summary row plus the Phase 7 columns."""
    meta, k6, trace = run["meta"], run["k6"], run["trace"]
    row = metrics.summarize(meta, k6, run["series"], trace, run["stats"])
    row["cost_usd"] = row["replica_seconds"] * PRICE_POD_SECOND
    spiky = spike_minutes(trace)
    lo, hi = metrics.scored_window(trace)
    viol = {m["minute"] for m in k6["minutes"]
            if lo <= m["minute"] * 60 < hi and metrics.minute_slo(m)[0]}
    row["spike_violation_minutes"] = len(viol & spiky) if spiky else None
    row["nonspike_violation_minutes"] = len(viol - spiky)
    # Requests that failed or were never sent, as a share of what the scored cycles offered.
    scored = [m for m in k6["minutes"] if lo <= m["minute"] * 60 < hi]
    offered = sum(m["offered"] for m in scored)
    bad = sum(metrics.minute_slo(m)[1] * m["offered"] for m in scored)
    row["scored_bad_share"] = bad / offered if offered else None
    for key, s in forecast_scores(run["series"], trace, meta["trace_start"]).items():
        for k in ("n", "smape", "coverage", "short"):
            row[f"fc_{key}_{k}"] = s[k]
    return row


def load_rows(dirs):
    rows = []
    for base in dirs:
        for d in sorted(Path(base).glob("*/")):
            run = load_run(d)
            if run:
                rows.append(run_row(run))
    return rows


def write_csv(rows, path):
    keys = []
    for r in rows:
        keys += [k for k in r if k not in keys]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- across runs

def cell(rows, workload, arm, delay=30, seeds=None):
    return [r for r in rows if r["workload"] == workload and r["arm"] == arm
            and r["startup_delay_seconds"] == delay and (seeds is None or r["seed"] in seeds)]


def by_seed(rows, workload, arm, delay=30):
    return {r["seed"]: r for r in cell(rows, workload, arm, delay)}


def frontier_at(points, x):
    """Value of the piecewise-linear frontier through `points` [(cost, y)] at cost x: flat
    below the cheapest point, None above the most expensive one."""
    pts = sorted(points)
    if x <= pts[0][0]:
        return pts[0][1]
    if x > pts[-1][0]:
        return None
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return None


def frontier_gaps(rows, workload, arm, y="slo_violation_minutes", x="replica_seconds",
                  delay=30):
    """{seed: gap} for the seeds where the arm and all three reactive arms finished. The gap is
    None where the arm cost more than every reactive arm."""
    mine = by_seed(rows, workload, arm, delay)
    reactive = [by_seed(rows, workload, r, delay) for r in REACTIVE]
    out = {}
    for seed, r in sorted(mine.items()):
        if all(seed in rs for rs in reactive):
            f = frontier_at([(rs[seed][x], rs[seed][y]) for rs in reactive], r[x])
            out[seed] = None if f is None else r[y] - f
    return out


def paired(rows, workload, arm, other, key, delay=30):
    """Stat of arm minus other, per seed where both finished."""
    a, b = by_seed(rows, workload, arm, delay), by_seed(rows, workload, other, delay)
    return mean_ci([a[s][key] - b[s][key] for s in sorted(a) if s in b
                    and a[s][key] is not None and b[s][key] is not None])


# ---------------------------------------------------------------- Phase 8: forecaster killed

FALLBACK_REPLICAS = 7  # arm C's KEDA fallback.replicas (deploy/arms/c-forecast.yaml) = max


def twin_id(run_id):
    """The main-grid run of the same trace, arm and delay as a fault run."""
    return run_id.removesuffix("-kill")


def cycle_numbers(run, cycle):
    """(SLO-violation minutes, pod-seconds) of one 0-based cycle of the trace, counted like the
    scored window's: minutes by their start, pods from the 15 s 'replicas' samples."""
    tr = run["trace"]
    lo = cycle * tr["cycle_seconds"]
    hi = lo + tr["cycle_seconds"]
    viol = sum(1 for m in run["k6"]["minutes"]
               if lo <= m["minute"] * 60 < hi and metrics.minute_slo(m)[0])
    pods = metrics.values(run["series"], "replicas", run["meta"]["trace_start"], lo, hi)
    return viol, sum(v for _, v in pods) * metrics.STEP


def fault_row(run, twin, row, twin_row):
    """One row per fault run: when the fault happened (trace seconds, from run.json), what the
    alert and the scaler did (from the saved series), and the differences to the twin overall
    and in the fault's cycle and the next one. Saved series are 15 s samples, so the alert and
    replica times are rounded up to the next sample."""
    meta, s, t0 = run["meta"], run["series"], run["meta"]["trace_start"]
    e = meta["fault_events"]
    out = {k: meta[k] for k in ("run_id", "arm", "workload", "seed", "git_sha", "git_dirty")}
    out.update({f"{k}_s": e.get(k) for k in ("kill", "gone", "restart", "back")})
    kill, back = e["kill"], e["back"]
    out["down_s"] = round(back - kill, 1)
    out["back_after_restart_s"] = round(back - e["restart"], 1)
    firing = sorted(t - t0 for t in series_of(s, "forecast_stale_alert", alertstate="firing"))
    firing = [t for t in firing if kill <= t <= back + 300]
    out["alert_firing_after_kill_s"] = firing[0] - kill if firing else None
    # The alert's last firing sample, relative to the first new forecast: it clears within
    # one sample after that.
    out["alert_last_firing_after_back_s"] = firing[-1] - back if firing else None
    spec = metrics.values(s, "replicas_spec", t0, kill, back + 300)
    at_max = [t for t, v in spec if v >= FALLBACK_REPLICAS]
    out["max_replicas_after_kill_s"] = at_max[0] - kill if at_max else None
    tw = twin["meta"]
    out["twin_run_id"], out["twin_git_sha"] = tw["run_id"], tw["git_sha"]
    for k in ("slo_violation_minutes", "replica_seconds", "underprovisioned_seconds"):
        out[k], out[f"twin_{k}"] = row[k], twin_row[k]
        out[f"d_{k}"] = row[k] - twin_row[k]
    fault_cycle = int(kill // run["trace"]["cycle_seconds"])
    for name, c in (("fault_cycle", fault_cycle), ("next_cycle", fault_cycle + 1)):
        (v, p), (tv, tp) = cycle_numbers(run, c), cycle_numbers(twin, c)
        out[f"{name}_violation_minutes"], out[f"{name}_pod_s"] = v, p
        out[f"d_{name}_violation_minutes"], out[f"d_{name}_pod_s"] = v - tv, p - tp
    return out
