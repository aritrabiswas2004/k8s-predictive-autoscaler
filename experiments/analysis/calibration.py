"""Find the capacity knee in a calibration run and plot it.

    .venv/bin/python experiments/analysis/calibration.py RUN.json [--plot OUT.png]
        [--overlay OTHER.json:PODS:LABEL ...]

The knee is where client-side p95 first crosses the SLO. Steps are discrete, so it is
reported as the bracketing pair of steps plus a linear interpolation between them.
RPS_PER_POD is 75 % of the knee (the plan says 70-80 %), rounded down.
--overlay adds the p95 of other runs to the plot, with load divided by their pod count.
"""

import argparse
import json
import math

SLO_MS = 300
HEADROOM = 0.75


def knee(steps):
    """Return (last_ok_rps, first_bad_rps, interpolated_rps), or None if p95 never crosses."""
    prev = None
    for s in steps:
        if s["p95_ms"] is not None and s["p95_ms"] >= SLO_MS:
            if prev is None:
                return (None, s["target_rps"], s["target_rps"])
            x0, y0 = prev["target_rps"], prev["p95_ms"]
            x1, y1 = s["target_rps"], s["p95_ms"]
            return (x0, x1, x0 + (SLO_MS - y0) * (x1 - x0) / (y1 - y0))
        prev = s
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--plot")
    ap.add_argument("--label", default="1 pod")
    ap.add_argument("--overlay", action="append", default=[], help="RUN.json:PODS:LABEL")
    args = ap.parse_args()

    with open(args.run) as f:
        run = json.load(f)
    steps = run["steps"]

    print(f"{'target':>7} {'achieved':>9} {'dropped':>8} {'failed':>7} {'p50':>8} {'p95':>8} {'p99':>8}")
    for s in steps:
        print(f"{s['target_rps']:>7} {s['achieved_rps']:>9.1f} {s['dropped']:>8} "
              f"{s['failed_rate']:>7.3f} {s['p50_ms']:>8.1f} {s['p95_ms']:>8.1f} {s['p99_ms']:>8.1f}")

    k = knee(steps)
    if k is None:
        print(f"\np95 never reached {SLO_MS} ms: extend STEPS upward")
    else:
        lo, hi, est = k
        rps_per_pod = math.floor(HEADROOM * est)
        print(f"\nknee: p95 crosses {SLO_MS} ms between {lo} and {hi} RPS (interpolated {est:.1f})")
        print(f"RPS_PER_POD = floor({HEADROOM} x {est:.1f}) = {rps_per_pod}")

    if args.plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        x = [s["target_rps"] for s in steps]
        fig, ax = plt.subplots(figsize=(7, 4.2), dpi=150)
        ax.plot(x, [s["p50_ms"] for s in steps], "o-", color="#8aa", label="p50")
        ax.plot(x, [s["p95_ms"] for s in steps], "o-", color="#1f5fa8", lw=2, label=f"p95, {args.label}")
        ax.plot(x, [s["p99_ms"] for s in steps], "o--", color="#1f5fa8", alpha=0.5, label="p99")
        for i, spec in enumerate(args.overlay):
            path, pods, label = spec.split(":", 2)
            with open(path) as f:
                other = json.load(f)["steps"]
            ax.plot([s["target_rps"] / int(pods) for s in other], [s["p95_ms"] for s in other],
                    "s-", color=["#e67e22", "#8e44ad", "#16a085"][i % 3], alpha=0.8,
                    label=f"p95, {label}")
        ax.axhline(SLO_MS, color="#c0392b", ls=":", label=f"SLO p95 < {SLO_MS} ms")
        if k is not None:
            ax.axvline(k[2], color="#c0392b", alpha=0.4, label=f"knee ≈ {k[2]:.0f} RPS")
            ax.axvline(rps_per_pod, color="#27ae60", alpha=0.7, ls="--",
                       label=f"RPS_PER_POD = {rps_per_pod}")
        ax.set_yscale("log")
        ax.set_xlabel("offered load per pod, requests/s (open model, 500m CPU per pod)")
        ax.set_ylabel("client-side latency, ms (log)")
        ax.set_title("Capacity calibration (client-side latency from k6)")
        ax.grid(True, which="both", alpha=0.25)
        ax.legend(loc="upper left", fontsize=8)
        fig.tight_layout()
        fig.savefig(args.plot)
        print(f"plot: {args.plot}")


if __name__ == "__main__":
    main()
