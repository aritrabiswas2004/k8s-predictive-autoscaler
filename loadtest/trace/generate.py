"""Generate the load traces that k6 replays (loadtest/k6/trace.js).

    python3 loadtest/trace/generate.py synthetic --seed 1 --out loadtest/trace/out/synthetic-s1.json
    python3 loadtest/trace/generate.py wikipedia --seed 1 --out loadtest/trace/out/wikipedia-s1.json

A trace is the offered request rate (RPS) every STEP seconds. k6 ramps linearly between
points. One run is NUM_CYCLES compressed "days" of CYCLE_SECONDS each: the first
HISTORY_CYCLES are history (the forecaster learns from them, not scored) and the rest are
scored. An unforecastable spike is added to the last scored cycle.

Two workloads:

synthetic   A fixed daily shape (baseline, ramp up, peak, dip, second peak, ramp down) with
            per-cycle variation, so that a perfect copy of yesterday is never the right
            forecast (plan revision R2):
              amplitude   the peak's height above baseline is scaled by U(0.8, 1.2)
              timing      the whole shape is shifted by U(-0.1, 0.1) x cycle
              trend       the level grows by TREND over the run (default +10 %)
wikipedia   Hourly pageviews of German Wikipedia (raw/), one real day per cycle, starting at
            03:00 UTC (the daily trough) so cycle boundaries fall at the baseline like in the
            synthetic shape. Hours are placed at their midpoints and interpolated linearly;
            the 5-day window's minimum maps to BASE and its maximum to PEAK (affine).

Both get the same seeded noise (multiplicative, Gaussian, sigma NOISE, clipped at 2 sigma)
and the same spike for a given seed. Random streams are separate per purpose, so the
spike's position for seed N is identical in both workloads.

Bounds: every point outside the spike must stay at or below MAX_RPS, the most the R60 arm
can serve with maxReplicas (7 pods x 0.6 x RPS_PER_POD 40 = 168, docs/methodology.md). The
generator fails rather than clipping, so a trace never silently changes shape.

Stdlib only.
"""

import argparse
import json
import random
from pathlib import Path

STEP = 5  # seconds between trace points

# One compressed day: (fraction of the cycle, fraction of the base -> peak range).
# Baseline covers both ends of the cycle with at least 0.1 to spare, so the timing shift
# (up to +-0.1 of a cycle) never moves load across a cycle boundary.
SHAPE = [
    (0.00, 0.0), (0.25, 0.0),  # night
    (0.40, 1.0),               # morning ramp, 0.15 of a cycle
    (0.48, 1.0),               # peak
    (0.54, 0.6), (0.60, 0.6),  # midday dip
    (0.66, 0.9), (0.70, 0.9),  # second peak
    (0.82, 0.0),               # evening ramp down
    (1.00, 0.0),
]

DEFAULT_RAW = Path(__file__).parent / "raw" / "dewiki-user-hourly-2025-03-03_09.json"


def interp(points, x):
    """Piecewise-linear interpolation through sorted (x, y) points, flat beyond the ends."""
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def synthetic_level(a, rng):
    """Noise-free RPS over the run, plus the per-cycle parameters that were drawn."""
    cycles = []
    for c in range(a.cycles):
        cycles.append({
            "index": c,
            "amplitude": round(rng.uniform(1 - a.amplitude_var, 1 + a.amplitude_var), 4),
            "shift_s": round(rng.uniform(-a.timing_var, a.timing_var) * a.cycle_seconds, 1),
        })
    total = a.cycles * a.cycle_seconds

    def level(t):
        c = min(int(t // a.cycle_seconds), a.cycles - 1)
        p = cycles[c]
        x = (t - c * a.cycle_seconds - p["shift_s"]) / a.cycle_seconds
        shape = interp(SHAPE, x)
        trend = 1 + a.trend * t / total
        return (a.base + (a.peak - a.base) * p["amplitude"] * shape) * trend

    return level, cycles


def wikipedia_level(a):
    items = json.loads(Path(a.raw).read_text())["items"]
    stamps = [i["timestamp"][:10] for i in items]
    first = stamps.index(a.start_hour)
    hours = a.cycles * 24 + 1  # one extra so the last half hour has a point to ramp to
    views = [items[first + h]["views"] for h in range(hours)]
    if len(views) < hours:
        raise SystemExit(f"{a.raw}: need {hours} hours from {a.start_hour}, have {len(views)}")
    lo, hi = min(views), max(views)
    per_hour = a.cycle_seconds / 24
    points = [((h + 0.5) * per_hour, v) for h, v in enumerate(views)]

    def level(t):
        v = interp(points, t)
        return a.base + (v - lo) / (hi - lo) * (a.peak - a.base)

    days = [{"index": c, "date": items[first + 24 * c]["timestamp"][:8],
             "views_min": min(views[24 * c:24 * c + 24]),
             "views_max": max(views[24 * c:24 * c + 24])} for c in range(a.cycles)]
    return level, days, {"views_min": lo, "views_max": hi, "project": items[0]["project"]}


def spike(a, rng):
    """Additive spike somewhere in the last cycle: (start_s, end_s, rps(t))."""
    if a.spike_rps <= 0:
        return None
    rise, hold, fall = a.spike_rise, a.spike_hold, a.spike_fall
    last = (a.cycles - 1) * a.cycle_seconds
    earliest = last + 0.05 * a.cycle_seconds
    latest = last + a.cycle_seconds - (rise + hold + fall) - 0.05 * a.cycle_seconds
    start = round(rng.uniform(earliest, latest) / STEP) * STEP
    pts = [(start, 0.0), (start + rise, a.spike_rps), (start + rise + hold, a.spike_rps),
           (start + rise + hold + fall, 0.0)]
    return start, pts[-1][0], lambda t: interp(pts, t)


def generate(a):
    seed = a.seed
    total = a.cycles * a.cycle_seconds
    meta = {}
    if a.workload == "synthetic":
        level, cycles = synthetic_level(a, random.Random(f"{seed}:cycles"))
    else:
        level, cycles, meta = wikipedia_level(a)

    noise_rng = random.Random(f"{seed}:noise")
    sp = spike(a, random.Random(f"{seed}:spike"))

    rps, clean = [], []
    for i in range(total // STEP + 1):
        t = i * STEP
        base = level(t)
        z = max(-2.0, min(2.0, noise_rng.gauss(0, 1)))
        v = base * (1 + a.noise * z)
        clean.append(base)
        if sp and sp[0] <= t <= sp[1]:
            v += sp[2](t)
        rps.append(round(v, 1))

    in_spike = [bool(sp and sp[0] <= i * STEP <= sp[1]) for i in range(len(rps))]
    outside = [v for v, s in zip(rps, in_spike) if not s]
    if max(outside) > a.max_rps:
        raise SystemExit(f"peak {max(outside)} RPS outside the spike exceeds --max-rps "
                         f"{a.max_rps}; lower --peak or --noise")
    if min(rps) < 1:
        raise SystemExit(f"rate drops to {min(rps)} RPS; raise --base")

    # Steepest sustained rise of the noise-free level, over 60 s (about one scale-up lag).
    w = 60 // STEP
    ramp = max((clean[i + w] - clean[i]) / 60 for i in range(len(clean) - w))

    return {
        "name": a.out.stem if a.out else f"{a.workload}-s{seed}",
        "workload": a.workload,
        "seed": seed,
        "step_seconds": STEP,
        "cycle_seconds": a.cycle_seconds,
        "num_cycles": a.cycles,
        "history_cycles": a.history_cycles,
        "duration_seconds": total,
        "params": {k: v for k, v in vars(a).items() if k not in ("out", "workload")
                   and not (a.workload == "wikipedia" and k in ("amplitude_var", "timing_var", "trend"))
                   and not (a.workload == "synthetic" and k in ("raw", "start_hour"))},
        "source": meta or None,
        "cycles": cycles,
        "spike": {"start_s": sp[0], "end_s": sp[1], "extra_rps": a.spike_rps} if sp else None,
        "stats": {
            "max_rps": max(rps),
            "max_rps_outside_spike": max(outside),
            "min_rps": min(rps),
            "max_ramp_rps_per_s_60s": round(ramp, 2),
            "offered_requests": round(sum((x + y) / 2 * STEP for x, y in zip(rps, rps[1:]))),
        },
        "rps": rps,
    }


def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workload", choices=["synthetic", "wikipedia"])
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--cycles", type=int, default=5)
    ap.add_argument("--history-cycles", type=int, default=3)
    ap.add_argument("--cycle-seconds", type=int, default=300)
    ap.add_argument("--base", type=float, default=30, help="night-time RPS")
    ap.add_argument("--peak", type=float, default=None,
                    help="nominal peak RPS (default: 120 synthetic, 150 wikipedia)")
    ap.add_argument("--noise", type=float, default=0.04, help="sigma of multiplicative noise")
    ap.add_argument("--amplitude-var", type=float, default=0.2)
    ap.add_argument("--timing-var", type=float, default=0.1)
    ap.add_argument("--trend", type=float, default=0.1, help="level growth over the run")
    ap.add_argument("--raw", default=str(DEFAULT_RAW))
    ap.add_argument("--start-hour", default="2025030303", help="YYYYMMDDHH (UTC)")
    ap.add_argument("--spike-rps", type=float, default=120, help="0 disables the spike")
    ap.add_argument("--spike-rise", type=int, default=5)
    ap.add_argument("--spike-hold", type=int, default=60)
    ap.add_argument("--spike-fall", type=int, default=15)
    ap.add_argument("--max-rps", type=float, default=168)
    a = ap.parse_args(argv)
    if a.peak is None:
        a.peak = 120 if a.workload == "synthetic" else 150
    if a.cycle_seconds % STEP:
        raise SystemExit(f"--cycle-seconds must be a multiple of {STEP}")
    if not 0 <= a.history_cycles < a.cycles:
        raise SystemExit("--history-cycles must be less than --cycles")
    return a


def main(argv=None):
    a = parse(argv)
    trace = generate(a)
    text = json.dumps(trace, separators=(",", ":"))
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text + "\n")
        s = trace["stats"]
        print(f"{a.out}: {len(trace['rps'])} points, {s['min_rps']}-{s['max_rps']} RPS "
              f"(max {s['max_rps_outside_spike']} outside the spike), steepest ramp "
              f"{s['max_ramp_rps_per_s_60s']} RPS/s")
    else:
        print(text)


if __name__ == "__main__":
    main()
