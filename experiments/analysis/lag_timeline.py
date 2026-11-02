"""Save and plot the Prometheus timeline around one lag measurement.

    .venv/bin/python experiments/analysis/lag_timeline.py RUN.lag.json OUT_PREFIX

Writes OUT_PREFIX.timeline.json (raw series, so the plot survives Prometheus retention)
and OUT_PREFIX.timeline.png.
"""

import json
import subprocess
import sys
import urllib.parse
from datetime import datetime

PROM = "/api/v1/namespaces/monitoring/services/kube-prometheus-stack-prometheus:9090/proxy"
SERIES = {
    "arrivals_rate_1m": 'sum(rate(http_requests_received_total{app="web"}[1m]))',
    "k6_completions_rate_1m": "sum(rate(k6_http_reqs_total[1m]))",
    "queued": 'sum(work_requests_queued{app="web"})',
    "desired_replicas": 'max(kube_horizontalpodautoscaler_status_desired_replicas{horizontalpodautoscaler="keda-hpa-web-lag"})',
    "ready_pods": 'sum(kube_pod_status_ready{condition="true",pod=~"web-.*"})',
}


def main():
    lag_path, out = sys.argv[1], sys.argv[2]
    with open(lag_path) as f:
        lag = json.load(f)
    t0 = datetime.fromisoformat(lag["marks"]["step"]).timestamp()

    data = {"t0": t0, "marks": lag["marks"], "series": {}}
    for name, expr in SERIES.items():
        q = urllib.parse.urlencode({"query": expr, "start": t0 - 60, "end": t0 + 150, "step": "5"})
        raw = subprocess.check_output(["kubectl", "get", "--raw", f"{PROM}/api/v1/query_range?{q}"])
        res = json.loads(raw)["data"]["result"]
        data["series"][name] = [[t - t0, float(v)] for t, v in res[0]["values"]] if res else []
    with open(f"{out}.timeline.json", "w") as f:
        json.dump(data, f)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    s = data["series"]
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(7, 5.2), dpi=150, sharex=True,
                                  gridspec_kw={"height_ratios": [3, 2]})
    offered = [[-60, 25], [0, 25], [0, 100], [150, 100]]
    ax.plot(*zip(*offered), color="#555", ls="--", label="offered (k6 schedule)")
    ax.plot(*zip(*s["arrivals_rate_1m"]), color="#1f5fa8", lw=2,
            label="arrivals rate[1m] (scaler input)")
    ax.plot(*zip(*s["k6_completions_rate_1m"]), color="#e67e22", label="completions rate[1m] (k6)")
    ax.set_ylabel("requests/s")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.25)

    ax2.step(*zip(*s["desired_replicas"]), where="post", color="#8e44ad", label="desired replicas (HPA)")
    ax2.step(*zip(*s["ready_pods"]), where="post", color="#27ae60", lw=2, label="ready pods")
    ax2.set_ylabel("pods")
    ax2.set_xlabel("seconds since load step")
    ax2.grid(alpha=0.25)
    ax2.legend(loc="upper left", fontsize=8)

    for name, color in [("metric", "#1f5fa8"), ("decision", "#8e44ad"), ("serving", "#27ae60")]:
        t = lag["marks"][name]
        if t:
            x = datetime.fromisoformat(t).timestamp() - t0
            for a in (ax, ax2):
                a.axvline(x, color=color, alpha=0.4, ls=":")
            ax.annotate(name, (x, ax.get_ylim()[1] * 0.92), fontsize=7, color=color,
                        rotation=90, ha="right", va="top")
    ax.set_title(f"Scale-up after a 25 → 100 RPS step (step → new pod serving: {lag['total_s']} s)")
    fig.tight_layout()
    fig.savefig(f"{out}.timeline.png")
    print(f"{out}.timeline.json, {out}.timeline.png")


if __name__ == "__main__":
    main()
