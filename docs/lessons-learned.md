# Lessons learned

Things this project found the hard way. Each one would have skewed the comparison, or
quietly broken a run, if it had been left in place. They are grouped by where they bite. The
evidence for each is in [methodology.md](methodology.md) and the [results](results.md).

## Measuring overload honestly

- **Overload can blind the scaler.**
  - Without a concurrency limit, an overloaded pod ran hundreds of CPU-bound goroutines on a
    0.5-CPU quota. That starved its accept loop and its metrics endpoint.
  - In one run, the arrival rate Prometheus saw *fell* from 25 to 10 RPS when the offered
    load jumped to 100, and KEDA never scaled up.
  - The fix: count arrivals before they queue, and bound the work in progress
    (`WORK_CONCURRENCY`).
- **Scale on arrivals, never on completions, CPU or replicas.**
  - Completions and CPU flatten exactly when the service falls behind.
  - Replicas feed the scaler's own decisions back into its input.
- **Default probes punish under-provisioned arms twice.**
  - At 70 RPS on one pod, the default 1 s liveness probe timed out and the container
    restarted.
  - The Service then had no endpoints for the whole start-up delay.
  - The probes are now overload-tolerant.
- **An open-model generator can hide overload by not sending.**
  - When responses slow down, k6 must start virtual users mid-run, and it drops the requests
    it can't start on time.
  - The p95 of the requests that *were* sent then understates what users saw. So a minute
    also counts as violated when more than 5 % of its offered requests failed or were never
    sent.
- **Cumulative quantiles are not windowed latency.** k6's remote-written p95 and p99 are
  quantiles since the start of the run, so a spike's p99 decays slowly instead of ending with
  the spike. SLO minutes come from k6's own summary instead, which tags every request
  with its minute of the trace and computes each minute's p95 from all of its samples.
- **An empty group is not a fast one.** k6 reports a tagged sub-metric that got no requests as
  a p95 of 0 ms. Stored as a number, it would average in as perfect latency; the harness
  stores it as missing.
- **Request timeouts cap latency.** With k6's 10 s timeout, p95 and p99 saturate under heavy
  overload and stop separating arms. Violation minutes and the share of failed requests keep
  separating them.

## Load balancing and timing

- **Keep-alive pins load to old pods.** kube-proxy balances *connections*. With reused
  connections, a new pod got only part of its share, which slowed relief after a scale-up.
  The load generator opens a new connection per request.
- **One connection per request makes arrivals bursty.** A pod sometimes gets several requests
  at once and exhausts its CPU quota for a 100 ms CFS period. That fattens the tail before the
  knee: p99 is noisier than p95.
- **A one-minute rate remembers the previous run.** A scaler created within a minute of
  earlier load scales up at once. Every run now waits for the arrival rate to reach zero.
- **Aligned start times hide the HPA sync wait.** Runs started a multiple of 15 s apart all
  hit the same phase of the HPA's 15 s sync. Starts are now jittered by 0–15 s.
- **KEDA's `pollingInterval` doesn't set the scaling cadence.** It only matters when scaling
  to or from zero. From 1 to N replicas, the HPA's 15 s sync drives the queries.

## Forecasting

- **Forecast the peak of a window, not a point.** Pods must be ready before the load arrives
  and stay up through it. So the forecaster predicts the peak over
  [now + lead, now + lead + window], with the lead set to the scale-up time plus a margin.
- **Use the upper bound.** Under-provisioning costs more than over-provisioning, so the
  scalers read a one-sided 90 % bound, not the mean.
- **Simple was enough.** Seasonal naive ("same time yesterday") matched or beat Holt-Winters
  on both workloads, in the backtest and live.
- **Accuracy is not the benefit.** The forecasts were six times more accurate on Wikipedia,
  yet prediction only paid on the synthetic workload, where the reactive lag actually cost
  minutes.
- **Score forecasts on the forecaster's own time grid.** Scoring against a series offset by
  5 s made seasonal naive look twice as bad on Wikipedia's steep evening drop.
- **Query the forecast through an aggregation.** `forecast_rps` carries pod labels, so a
  restarted forecaster is a new series. Always use `max(forecast_rps{...})`.

## Failure behaviour

- **A missing forecast must not read as zero for a forecast-only scaler.**
  - By default, KEDA's Prometheus scaler treats an empty result as 0. A dead forecaster would
    then hold the minimum replica count through any load.
  - Arm C turns an empty result into an error, which triggers its fallback.
- **KEDA's fallback is per object, not per trigger.**
  - In KEDA 2.21, a fallback replaces the whole ScaledObject's output.
  - So the hybrid arm relies on the max rule instead: a missing forecast reads as 0, and the
    request-rate trigger decides alone.
  - Measured with the forecaster killed under load: the hybrid becomes the reactive
    autoscaler it contains. The forecast-only arm stays safe at 40 % more pod-seconds.
- **The restarted forecaster needs no warm-up.** Its history lives in Prometheus, so it
  forecasts again within seconds of a restart. Only a fresh Prometheus means a cold start.
- **Re-applying a Deployment can fight the scaler.** A manifest that sets `replicas` resets
  the count every time it's applied, even under an active autoscaler. The field was removed.

## Running long experiments on a laptop

- **CPU pinning isolates scheduling, not hardware.**
  - Saturating the tools node slowed the pinned app by about 40 %: the cores share the
    memory system, the thermal budget and the host's mix of performance and efficiency cores.
  - Record node CPU in every run, and keep the load generator far from saturation.
- **A suspended run can look like a successful one.**
  - When the laptop slept mid-run, every process froze together. On waking, the run simply
    finished and was recorded as successful, with nine hours missing from its middle.
  - Check that each run's duration equals the trace plus its tail, and that its series have
    no gaps.
  - On battery, closing the lid sleeps the Mac even under `caffeinate`.
- **A Docker Desktop restart can wipe the kind cluster.** Images survive, the cluster
  doesn't. Rebuilding is one command, but the forecaster then needs two cycles of history
  before arm C stops sitting in its fallback.
- **A dirty-tree flag needs evidence to be useful.**
  - Each run records whether the working tree differed from its commit. During the fault
    runs, a second session edited documentation in the same checkout, and two runs were
    flagged.
  - Recording the list of changed files at each run's start showed that nothing a run reads
    had changed, so the runs could be kept.
  - Do concurrent work in a separate `git worktree`.
- **Make every run resumable and self-describing.** The harness skips finished runs, marks
  interrupted ones failed so they're redone, and saves the raw data of each run with its
  commit, versions and timestamps. That made pausing, resuming across four cluster rebuilds,
  and regenerating every table byte for byte from a fresh clone routine.
