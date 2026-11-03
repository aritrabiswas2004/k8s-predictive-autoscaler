"""One summary row per run, from the raw files the harness saves (Phase 6, plan section 9.3).

Pure functions on the saved data, standard library only, so every row can be recomputed
offline (python3 experiments/run_experiment.py summarize) and tested without a cluster.

Scored window: the trace's scored cycles, [history_cycles x cycle_seconds, duration). All
metrics below are over that window unless the name says otherwise.

SLO: client-side p95 < 300 ms. A minute of the trace violates it when its p95 is over
300 ms, or when more than 5 % of the requests offered in it failed or were never sent
(dropped by k6). A failed or unsent request is an infinitely slow one, so with more than
5 % of them the minute's true p95 is over any limit, whatever the latency of the rest.
"""

import csv
import math
import statistics

SLO_MS = 300.0
SLO_BAD_SHARE = 0.05
RPS_PER_POD = 40.0
POD_CPU = 0.5  # cores requested per pod (deploy/app/deployment.yaml)
STEP = 15  # seconds between samples of the saved Prometheus series


def scored_window(trace):
    return trace["history_cycles"] * trace["cycle_seconds"], trace["duration_seconds"]


def offered_at(trace, t):
    """Offered RPS at trace time t (the trace is linear between its points)."""
    rps, step = trace["rps"], trace["step_seconds"]
    if t <= 0:
        return rps[0]
    i = int(t // step)
    if i >= len(rps) - 1:
        return rps[-1]
    return rps[i] + (rps[i + 1] - rps[i]) * (t - i * step) / step


def minute_slo(minute):
    """(violated, bad_share) for one entry of the k6 summary's per-minute list."""
    sent, offered = minute["requests"], minute["offered"]
    failed = (minute["failed_rate"] or 0) * sent
    dropped = max(0, offered - sent)
    bad = (failed + dropped) / offered if offered else 0.0
    p95 = minute["p95_ms"]
    violated = (sent > 0 and p95 is not None and p95 > SLO_MS) or bad > SLO_BAD_SHARE \
        or (sent == 0 and offered > 0)
    return violated, bad


def slo_minutes(k6, trace):
    lo, hi = scored_window(trace)
    scored = [m for m in k6["minutes"] if lo <= m["minute"] * 60 < hi]
    flags = [minute_slo(m)[0] for m in scored]
    return sum(flags), len(flags), [m["minute"] for m, f in zip(scored, flags) if f]


def values(series, name, t0, lo, hi):
    """[(trace time, value)] of a single-series query within [lo, hi) seconds of the trace."""
    out = []
    for s in series.get(name, []):
        for t, v in s["values"]:
            tt = t - t0
            if lo <= tt < hi and not math.isnan(v):
                out.append((tt, v))
    return sorted(out)


def changes(vs):
    return sum(1 for (_, a), (_, b) in zip(vs, vs[1:]) if a != b)


def series_metrics(series, trace, t0):
    lo, hi = scored_window(trace)
    expected = (hi - lo) // STEP
    pods = values(series, "replicas", t0, lo, hi)
    ready = values(series, "replicas_ready", t0, lo, hi)
    spec = values(series, "replicas_spec", t0, lo, hi)
    cpu = dict(values(series, "cpu_used", t0, lo, hi))
    out = {
        "samples": len(pods), "samples_expected": expected,
        # Pods exist (and cost) from creation, Ready or not.
        "replica_seconds": sum(v for _, v in pods) * STEP,
        "ready_replica_seconds": sum(v for _, v in ready) * STEP,
        "scale_events": changes(spec),
        "scale_events_run": changes(values(series, "replicas_spec", t0, 0,
                                           trace["duration_seconds"])),
        "replicas_min": min((v for _, v in ready), default=None),
        "replicas_max": max((v for _, v in ready), default=None),
    }
    out["cpu_core_seconds_requested"] = out["replica_seconds"] * POD_CPU
    used = sum(cpu.get(t, 0.0) for t, _ in pods)
    requested = sum(v * POD_CPU for t, v in pods if t in cpu)
    out["cpu_utilization_mean"] = used / requested if requested else None
    # Over-provisioning: Ready pods minus the pods the offered load needs at RPS_PER_POD.
    diffs, short = [], 0
    for t, v in ready:
        need = math.ceil(offered_at(trace, t) / RPS_PER_POD - 1e-9)
        diffs.append(v - need)
        short += v < need
    out["overprovisioning_mean"] = statistics.fmean(diffs) if diffs else None
    out["underprovisioned_seconds"] = short * STEP
    return out


def docker_stats(path):
    """Median and max CPU % (100 = one core) per kind node container during the run."""
    per = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                per.setdefault(r["container"], []).append(float(r["cpu_percent"]))
            except ValueError:
                continue
    return {c: {"median": statistics.median(v), "max": max(v)} for c, v in per.items() if v}


def summarize(meta, k6, series, trace, stats=None):
    """The summary row: run metadata + k6 + series metrics, flat (one CSV row)."""
    t0 = meta["trace_start"]
    n, total, which = slo_minutes(k6, trace)
    row = {k: meta[k] for k in ("run_id", "grid", "arm", "workload", "trace", "seed",
                                "startup_delay_seconds", "lead_seconds", "rep", "order",
                                "git_sha", "git_dirty", "started_at", "finished_at")}
    row.update({
        "slo_violation_minutes": n, "scored_minutes": total,
        "violating_minutes": " ".join(map(str, which)),
        "offered": k6["offered"], "requests": k6["requests"], "dropped": k6["dropped"],
        "failed_rate": k6["failed_rate"], "vus_max": k6["vus_max"],
        "scored_p95_ms": k6["scored"]["p95_ms"], "scored_p99_ms": k6["scored"]["p99_ms"],
        "scored_failed_rate": k6["scored"]["failed_rate"],
    })
    # k6 reports an empty group (no spike in the trace) as zeros; that is no data, not 0 ms.
    spike = k6["spike"] if k6["spike"]["requests"] else {}
    row.update({"spike_requests": k6["spike"]["requests"], "spike_p95_ms": spike.get("p95_ms"),
                "spike_failed_rate": spike.get("failed_rate")})
    row.update(series_metrics(series, trace, t0))
    if stats:
        for node, key in (("pa-worker", "app_node"), ("pa-worker2", "tools_node")):
            if node in stats:
                row[f"{key}_cpu_median_pct"] = stats[node]["median"]
                row[f"{key}_cpu_max_pct"] = stats[node]["max"]
    return row
