"""Plot load traces: the whole run (left) and its cycles overlaid (right), one row per trace.

    .venv/bin/python loadtest/trace/plot.py [--traces NAME ...] --out docs/img/traces.png
"""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
# Categorical slots in fixed order (one per cycle) and recessive inks.
CYCLE_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
LINE = "#2a78d6"
INK = "#0b0b0b"
MUTED = "#52514e"
SHADE = "#efeeeb"
SPIKE = "#e34948"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", nargs="+", default=["synthetic-s1", "wikipedia-s1"])
    ap.add_argument("--max-rps", type=float, default=168)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    traces = [json.loads((OUT_DIR / f"{n}.json").read_text()) for n in a.traces]

    plt.rcParams.update({"font.size": 8, "axes.edgecolor": MUTED, "axes.labelcolor": INK,
                         "xtick.color": MUTED, "ytick.color": MUTED,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(len(traces), 2, figsize=(10, 3.1 * len(traces)), dpi=150,
                             gridspec_kw={"width_ratios": [2.2, 1]}, squeeze=False)
    ymax = max(max(t["rps"]) for t in traces) * 1.08

    for row, t in zip(axes, traces):
        step, cyc = t["step_seconds"], t["cycle_seconds"]
        xs = [i * step for i in range(len(t["rps"]))]
        title = {"synthetic": "Synthetic", "wikipedia": "German Wikipedia, 3-7 Mar 2025"}[t["workload"]]

        ax = row[0]
        hist_end = t["history_cycles"] * cyc
        ax.axvspan(0, hist_end, color=SHADE, lw=0)
        for c in range(1, t["num_cycles"]):
            ax.axvline(c * cyc, color=MUTED, lw=0.5, alpha=0.4)
        ax.text(hist_end / 2, ymax * 0.97, "history (not scored)", ha="center", va="top",
                color=MUTED)
        ax.text(hist_end + 10, ymax * 0.97, "scored", ha="left", va="top", color=MUTED)
        ax.axhline(a.max_rps, color=MUTED, lw=1, ls="--")
        ax.text(5, a.max_rps + 3, f"R60 limit at 7 pods ({a.max_rps:g} RPS)", color=MUTED,
                va="bottom")
        ax.plot(xs, t["rps"], color=LINE, lw=1.2)
        if t["spike"]:
            s = t["spike"]
            ax.annotate(f"unforecastable spike\n+{s['extra_rps']:g} RPS for {s['end_s'] - s['start_s']} s",
                        xy=(s["end_s"], max(t["rps"]) * 0.92), xytext=(14, 0),
                        textcoords="offset points", color=INK, va="center", ha="left",
                        arrowprops={"arrowstyle": "-", "color": MUTED, "lw": 0.8})
        ax.set_xlim(0, t["duration_seconds"])
        ax.set_ylim(0, ymax)
        ax.set_xticks([c * cyc for c in range(t["num_cycles"] + 1)])
        ax.set_ylabel("offered RPS")
        ax.set_title(f"{title} ({t['name']}): {t['num_cycles']} cycles of {cyc} s",
                     loc="left", color=INK)
        ax.grid(axis="y", color=MUTED, alpha=0.15, lw=0.5)

        ax = row[1]
        per = cyc // step
        for c in range(t["num_cycles"]):
            seg = t["rps"][c * per:(c + 1) * per + 1]
            ax.plot([i * step for i in range(len(seg))], seg, color=CYCLE_COLORS[c], lw=1.1,
                    label=f"cycle {c + 1}" + (" (spike)" if t["spike"] and c == t["num_cycles"] - 1 else ""))
        ax.set_xlim(0, cyc)
        ax.set_ylim(0, ymax)
        ax.set_xticks(range(0, cyc + 1, 60))
        ax.set_title("Cycles overlaid", loc="left", color=INK)
        ax.legend(frameon=False, fontsize=7, loc="upper left")
        ax.grid(axis="y", color=MUTED, alpha=0.15, lw=0.5)

    axes[-1][0].set_xlabel("seconds since start of run")
    axes[-1][1].set_xlabel("seconds into cycle")
    fig.tight_layout()
    fig.savefig(a.out)
    print(a.out)


if __name__ == "__main__":
    main()
