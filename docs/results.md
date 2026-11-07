# Results

The evaluation of the six scaling setups (60 runs on two workloads, plus 18 runs that vary
the pods' start-up time) is summarised in the [README](../README.md#performance). Every
table with its confidence intervals is in [results-tables.md](results-tables.md), and the
design is in [methodology.md](methodology.md).

This file is the write-up of the fault scenarios (Phase 8). The one scenario run so far is
the forecaster dying under load.

## Fault: the forecaster dies under load

**Question.** If the forecaster disappears in the middle of a day, does the service stay
healthy, and what does it cost?

### Setup

- **Fault.** The forecaster Deployment is scaled to 0 at trace second 900, the start of the
  first scored cycle, before that cycle's ramp. It is started again 180 s later. Its pod was
  gone 2 s after the kill in every run. The restarted forecaster reads its history back from
  Prometheus, so it forecast again 3–17 s after the restart, with no warm-up.
- **Arms.** The two that scale on the forecast:
  - **C (forecast only).** With the forecast missing, KEDA's query fails. After three
    failures in a row, KEDA's `fallback` sets 7 pods, the maximum.
  - **D (forecast + request rate).** A missing forecast reads as 0, so the request-rate
    trigger decides alone. No fallback is configured.
- **Runs.** C and D × synthetic and German Wikipedia × seeds 1–3, so 12 runs at a start-up
  delay of 30 s. Each fault run is paired with its *twin*: the main-grid run of the same
  trace and arm, without the fault. The numbers below are fault run minus twin: the mean
  ± the half-width of the 95 % Student-t interval, n = 3 per workload and 6 for both.
- **The guard.** The `ForecastStale` alert in
  [`deploy/forecaster/alerts.yaml`](../deploy/forecaster/alerts.yaml) fires when there has
  been no successful forecast for 60 s, or no forecaster metrics at all, for 15 s. Every run
  saves the alert's state.
- **Reproduce.** Run `make experiment GRID=fault`, then `make analyze`. The raw data is in
  `experiments/results/fault/`, and the generated tables are in
  [results-tables.md](results-tables.md#fault-forecaster-stopped-phase-8).

### What happened

![Arms C and D with the forecaster stopped, against their twins](img/fault-timeline.png)

| Arm | Workload | Δ SLO-violation minutes | Δ pod-seconds | Δ pod-seconds, fault cycle | Δ pod-seconds, next cycle |
|---|---|---:|---:|---:|---:|
| C | synthetic | 0.00 ± 2.48 | +775 ± 168 (+32 %) | +720 ± 162 | +55 ± 78 |
| C | Wikipedia | 0.00 ± 0.00 | +970 ± 57 (+48 %) | +930 ± 99 | +40 ± 57 |
| C | both | 0.00 ± 0.66 | **+872 ± 122 (+40 %)** | +825 ± 131 | +48 ± 27 |
| D | synthetic | +1.00 ± 2.48 | **−275 ± 43 (−11 %)** | −200 ± 176 | −75 ± 134 |
| D | Wikipedia | +0.33 ± 1.43 | −5 ± 120 (0 %) | +10 ± 86 | −15 ± 37 |
| D | both | +0.67 ± 0.86 | −140 ± 159 (−5 %) | −95 ± 132 | −45 ± 51 |

**C fails safe and expensive.**
- `ForecastStale` fired 34–56 s after the kill.
- KEDA's fallback raised C to 7 pods 64–87 s after the kill.
- C kept the 7 pods until 122–134 s after the forecast was back. That is the scale-down
  stabilization window of 120 s, the same for every arm.
- C lost no more SLO minutes than its twins, in any pair.
- C paid 40 ± 10 % more pod-seconds per run. Nearly all of the extra pods fall in the cycle
  where the forecaster died: a 3-minute outage cost about 14 extra pod-minutes.

**D falls back to reactive scaling.**
- Without the forecast, D is R100. In the fault cycle, D with the fault and the main grid's
  R100 on the same trace had the same number of SLO-violation minutes, within noise
  (synthetic −0.33 ± 1.43, Wikipedia 0.00 ± 0.00).
- On synthetic, losing the forecast saved 11 % of pod-seconds and cost 0, 1 and 2 extra
  SLO minutes in the three seeds. The direction is consistent, but n = 3 can't separate it
  from noise.
- The figure shows the mechanism in seed 3. Once the forecast was gone, the higher
  recommendation it had made aged out of the 120 s scale-down window, and D followed the
  request rate down to 2 pods just before the ramp. The ramp then caught it with too few
  pods for two minutes (960–1080 s), which is the reactive lag the forecast exists to hide.
  Its twin held 3–4 pods there and lost nothing.
- On Wikipedia, where the forecast didn't help in the main grid either, nothing changed.

**Recovery was complete before the next cycle.** In the cycle after the fault, which holds
the spike, both arms are within noise of their twins in SLO minutes (C 0.00 ± 0.66, D
+0.33 ± 0.54). C's extra pods there are the tail of its scale-down: +48 ± 27 pod-seconds.

**The alert.** In all 12 runs, `ForecastStale` started firing 34–56 s after the kill: about
one scrape and one rule evaluation (15 s each) for the series to go stale and the rule to
see it, the `for: 15s`, and up to one 15 s sample of rounding. It fired without a gap until
0–13 s before the first new forecast, so it cleared with the recovery and never flapped. `ForecastInaccurate` didn't fire, as expected: the
forecasts that were made stayed accurate.

### What it means

- **The hybrid's worst case is the reactive autoscaler.** When D loses its forecast it
  becomes R100: never more expensive, and slower only by the reactive lag. That is the
  failure behaviour the README promises ("scaling continues on the current request rate"),
  now measured.
- **Forecast-only scaling needs a fallback, and the fallback is a cost decision.** Set to
  the maximum, C stayed safe and spent 40 % more. A lower fallback would cost less but could
  under-provision a ramp, and C has no request-rate trigger to catch it. That wasn't tested.
- **Alert on staleness.** The alert caught every outage within a minute, but scaling
  doesn't need it: D degrades on its own, and C's fallback acts about 30 s after the
  alert. The alert is for the operator, who should know that the predictive advantage
  is gone.

### Caveats

- **Few repetitions.** n = 3 per arm and workload. The cost differences are clear, but the
  SLO differences are all within noise.
- **One fault shape.** The forecaster was killed once per run, at one point in the cycle
  (before the ramp), for 3 minutes. A kill during a ramp or a spike, a longer outage, a
  Prometheus outage and a permanent pattern shift weren't run.
- **Pairs across clusters.** The fault runs ran on a fresh cluster (2026-11-07) and their
  twins in the main grid (2026-11-03 to 11-05). Cluster-to-cluster drift adds noise to every
  pair. In the main grid, cluster effects couldn't be told apart from trace effects,
  because repetition 1 ran on a cluster of its own.
- **Two runs with a modified tree.** `wikipedia-s1-d30-d-kill` and
  `wikipedia-s2-d30-d-kill` started while another session had uncommitted documentation in
  the checkout (README and docs only; nothing a run reads). The changed files are recorded
  per run in `experiments/results/fault/dirty-evidence-*.txt`.
