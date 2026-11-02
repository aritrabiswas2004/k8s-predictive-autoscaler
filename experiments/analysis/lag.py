"""Break down the scale-up lag of one load step (see loadtest/measure-lag.sh).

    python3 experiments/analysis/lag.py --step-at MS --threshold RPS --replicas-before N
        --pods-before NAME,NAME --hpa HPA_NAME --out OUT.json

t0  step         first request of the high phase (k6 STEP_AT)
t1  metric       sum(rate(http_requests_received_total[1m])) first exceeds what the
                 current replicas cover, plus the HPA's 10 % tolerance (evaluated post hoc at
                 1 s resolution, i.e. the earliest moment any scaler could have seen it)
t2  decision     HPA SuccessfulRescale event
t3  scheduled    the new pod's PodScheduled condition
t4  started      the container's startedAt
t5  ready        the app's "startup delay ... done, ready" log line (the Ready condition only
                 keeps its last transition, which overload can move later); the kubelet
                 sees it on the next readiness probe, at most 2 s later
t6  serving      the first /work request logged by the new pod

Stdlib only: talks to Kubernetes through kubectl and to Prometheus through the API-server
service proxy, so no port-forward is needed.
"""

import argparse
import json
import subprocess
import urllib.parse
from datetime import datetime, timezone

PROM = "/api/v1/namespaces/monitoring/services/kube-prometheus-stack-prometheus:9090/proxy"
QUERY = 'sum(rate(http_requests_received_total{app="web"}[1m]))'
TOLERANCE = 0.1  # kube-controller-manager default --horizontal-pod-autoscaler-tolerance


def kubectl_json(*args):
    return json.loads(subprocess.check_output(["kubectl", *args, "-o", "json"]))


def ts(s):
    """RFC 3339 -> unix seconds (float). Log timestamps have nanoseconds; Python takes 6 digits."""
    s = s.replace("Z", "")
    if "." in s:
        whole, frac = s.split(".")
        s = f"{whole}.{frac[:6]}"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")


def metric_crossing(step_at, threshold, replicas):
    q = urllib.parse.urlencode({"query": QUERY, "start": step_at - 30, "end": step_at + 240,
                                "step": "1"})
    raw = subprocess.check_output(["kubectl", "get", "--raw", f"{PROM}/api/v1/query_range?{q}"])
    values = json.loads(raw)["data"]["result"][0]["values"]
    limit = threshold * replicas * (1 + TOLERANCE)
    for t, v in values:
        if t >= step_at and float(v) > limit:
            return float(t), limit
    return None, limit


def first_log(pod, needle):
    out = subprocess.check_output(["kubectl", "logs", "--timestamps", pod], text=True)
    for line in out.splitlines():
        if needle in line:
            return ts(line.split()[0])
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step-at", type=float, required=True, help="unix ms")
    ap.add_argument("--threshold", type=float, required=True)
    ap.add_argument("--replicas-before", type=int, required=True)
    ap.add_argument("--pods-before", required=True)
    ap.add_argument("--hpa", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    t0 = a.step_at / 1000
    t1, limit = metric_crossing(t0, a.threshold, a.replicas_before)

    events = kubectl_json("get", "events", "--field-selector",
                          f"involvedObject.name={a.hpa},reason=SuccessfulRescale")["items"]
    rescales = sorted(
        (ts(e.get("lastTimestamp") or e["eventTime"]), e["message"]) for e in events
    )
    rescales = [r for r in rescales if r[0] >= t0 - 1]
    # The decision is the first scale-UP after the step (a scale-down event must not count).
    ups = [r for r in rescales if "above target" in r[1]]
    t2 = ups[0][0] if ups else None

    before = set(a.pods_before.split(","))
    pods = []
    for p in kubectl_json("get", "pods", "-l", "app=web")["items"]:
        name = p["metadata"]["name"]
        if name in before:
            continue
        cond = {c["type"]: ts(c["lastTransitionTime"]) for c in p["status"].get("conditions", [])}
        state = p["status"]["containerStatuses"][0]["state"]
        pods.append({
            "pod": name,
            "created": ts(p["metadata"]["creationTimestamp"]),
            "scheduled": cond.get("PodScheduled"),
            "started": ts(state["running"]["startedAt"]) if "running" in state else None,
            "ready": first_log(name, "done, ready"),
            "serving": first_log(name, " /work "),
        })
    pods.sort(key=lambda p: p["created"])
    # The first wave: pods created by the first scale-up after the step.
    wave = [p for p in pods if t2 is not None and p["created"] >= t0 and 0 <= p["created"] - t2 < 5]

    def last(key):
        vals = [p[key] for p in wave if p[key] is not None]
        return max(vals) if vals else None

    t3, t4, t5, t6 = last("scheduled"), last("started"), last("ready"), last("serving")
    marks = [("step", t0), ("metric", t1), ("decision", t2), ("scheduled", t3),
             ("started", t4), ("ready", t5), ("serving", t6)]
    breakdown = {}
    for (n0, a0), (n1, a1) in zip(marks, marks[1:]):
        breakdown[f"{n0}->{n1}"] = round(a1 - a0, 1) if a0 is not None and a1 is not None else None
    total = round(t6 - t0, 1) if t6 is not None else None

    result = {
        "threshold_rps": a.threshold, "replicas_before": a.replicas_before,
        "metric_limit_rps": limit,
        "marks": {n: iso(t) if t is not None else None for n, t in marks},
        "breakdown_s": breakdown, "total_s": total,
        "rescale_events": [{"at": iso(t), "message": m} for t, m in rescales],
        "first_wave_pods": [p["pod"] for p in wave],
    }
    with open(a.out, "w") as f:
        json.dump(result, f, indent=2)
    for k, v in breakdown.items():
        print(f"  {k:<22} {v} s")
    print(f"  {'total step->serving':<22} {total} s")
    for t, m in rescales:
        print(f"  rescale {iso(t)}: {m}")


if __name__ == "__main__":
    main()
