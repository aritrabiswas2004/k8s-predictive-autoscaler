# Predictive Autoscaling for Kubernetes

Kubernetes adds pods only after traffic has already gone up, and a new pod can take a minute
or more to start serving. Until it does, requests queue and users wait. This project starts
pods **before** the traffic arrives: it forecasts the next peak from the daily pattern and
scales on that forecast with [KEDA](https://keda.sh/), with normal reactive scaling kept as
a backup.

It also tests whether the forecast is worth it, under identical load, against the simplest
alternative: running more spare pods.

> **Result:** on traffic whose daily ramps vary, forecasting removed almost every slow
> minute, and it cost less than buying the same effect with spare pods. On smooth real-world
> traffic it gained nothing. [Details](#what-i-found)
>
> **Status:** evaluation done (78 runs). In progress: killing the forecaster mid-run under
> load to test the fallback.

## The problem

On this setup, a new pod starts serving **60–75 s** after traffic jumps
([measured](docs/methodology.md#scale-up-lag)):

| Step | Time |
|---|---:|
| The request-rate metric catches up with the new traffic | ~27 s |
| Wait for the autoscaler's next check (every 15 s) | 0–15 s |
| Schedule and start the container | ~1 s |
| App start-up (simulated warm-up: caches, connections, JIT) | 30 s |
| Pod marked ready and added to the Service | ~1 s |

No autoscaler can skip the start-up. The first step, waiting for the metric to show that
load has risen, is the part a forecast removes.

![Scale-up after a 25 → 100 RPS step](docs/img/scale-up-lag.png)

## How it works

```
         ┌─────────────────────────── scales ────────────────────────────┐
         ▼                                                               │
  k6 ──▶ web pods ── arrivals ──▶ Prometheus ──▶ KEDA ScaledObject ──▶ HPA
                                   │      ▲
                          history  │      │  forecast_rps
                                   ▼      │
                                 forecaster
```

1. **Count requests when they arrive.** Each pod counts incoming requests
   (`http_requests_received_total`) before they wait for CPU. CPU usage and completed-request
   counts stop rising when a pod is overloaded, which is exactly when more pods are needed.
   Arrivals keep rising.
2. **Forecast the next peak.** Every 30 s, a small Python service reads the last three
   "days" of traffic from Prometheus and predicts the highest load 60–120 s ahead, the time a
   new pod needs to be ready ([why that window](docs/methodology.md#what-it-forecasts-and-how-far-ahead)).
   It exports the upper bound of the forecast, because too few pods costs latency and too
   many only costs money. The model is "same as this time yesterday". Holt-Winters was also
   tried and was not better.
3. **Scale on the forecast and on current traffic.** One KEDA ScaledObject has two
   triggers, the forecast and the current request rate, and the HPA follows whichever asks
   for more pods ([manifest](deploy/arms/d-hybrid.yaml)). The forecast handles the daily
   pattern; the request-rate trigger catches what the forecast misses.
4. **Fail safe.** If the forecaster dies or has no history yet, it exports nothing. KEDA
   reads the missing forecast as 0, and the request-rate trigger scales on its own, like a
   normal autoscaler ([details](docs/methodology.md#when-the-forecast-disappears)).

## How it was tested

Six autoscaler setups ran one at a time, against the same load and from the same starting
state:

| Setup | What scales the pods |
|---|---|
| A | The default: HPA on CPU, 70 % target |
| R100 / R80 / R60 | KEDA on the current request rate, with 0 / 20 / 40 % spare capacity per pod |
| C | The forecast only |
| **D** | **The forecast plus the current request rate (this project)** |

All six have the same pod size, the same 2–7 replica range and the same scale-down
behaviour ([manifests](deploy/arms/)).

- **Traffic.** Two workloads: a synthetic daily pattern where every day differs in height,
  timing and trend, and a real week of German Wikipedia traffic. Each "day" is compressed to
  5 minutes, and the last day has a sudden spike that no forecast can see coming.
- **Score.** The number of slow minutes out of 10. A minute counts as slow if the
  client-side p95 latency is over 300 ms, or more than 5 % of requests failed.
- **Cost.** Pod-seconds: the number of pods multiplied by how long they exist.
- **The fair comparison is spare capacity, not the default.** Any autoscaler looks better
  if it simply runs more pods. R100 → R80 → R60 shows what extra pods buy, and the forecast
  only counts as a win if it does better than that for the same cost.
- **Repetitions.** Every setup ran 5 times per workload (60 runs), plus 18 runs that vary
  the app's start-up delay. Everything ran on a local [kind](https://kind.sigs.k8s.io/)
  cluster, with the app on its own pinned CPUs.

![The two workloads](docs/img/traces.png)

## What I found

| Setup | Synthetic: slow minutes (of 10) | Cost vs R100 | Wikipedia: slow minutes (of 10) | Cost vs R100 |
|---|---:|---:|---:|---:|
| A (HPA on CPU) | 3.2 | −8 % | 2.6 | −18 % |
| R100 | 2.8 | — | 2.0 | — |
| R80 | 2.2 | +24 % | 1.8 | +23 % |
| R60 | 2.0 | +51 % | 0.2 | +49 % |
| C (forecast only) | 0.6 | +20 % | 2.0 | −2 % |
| **D (forecast + request rate)** | **0.2** | +31 % | 2.0 | +9 % |

Averages over 5 runs. Confidence intervals and every other table:
[docs/results-tables.md](docs/results-tables.md), generated by `make analyze` from the raw
data in [`experiments/results/runs/`](experiments/results/runs/).

- **Varying daily traffic: forecasting wins.** D was slow in 1 of 50 minutes across its 5
  runs. Reactive scaling lost about a minute per run on the ramps of the daily pattern, even
  with 40 % spare capacity (R60), and D used 13 % fewer pod-seconds than R60.
- **Smooth real traffic: no gain.** Reactive scaling already kept up with Wikipedia's daily
  curve. The only slow minutes were in the spike, which no forecast can predict, and only
  spare capacity absorbed it, at 49 % more cost.
- **Slow start-up is what makes forecasting pay.** With pods that start instantly, D's
  advantage was too small to tell apart from noise. With 30 s and 60 s start-up it was
  clear (3 runs per setting).
- **Forecast accuracy was not what decided it.** The Wikipedia forecasts were six times
  more accurate, yet only helped on the synthetic traffic. "Same as yesterday" was as good
  as the more complex Holt-Winters model.
- **CPU-based scaling under-reacts.** The default HPA was cheapest, but on Wikipedia it
  failed 7.6 % of requests, against none for R100. CPU usage cannot go
  above 100 %, so it cannot show how far behind the scaler is.

![Cost against slow minutes](docs/img/frontier.png)

*Top: cost against slow minutes; bottom: cost against seconds with too few pods. Large
markers are each setup's average, small ones single runs. The blue line joins R100, R80 and
R60 and shows what spare capacity buys; a setup below it does better than spare capacity at
the same cost.*

## Should you do this?

These results come from one CPU-bound, stateless service on a laptop, with days compressed
to 5 minutes. Measure on your own system. With that caveat:

- **Measure your scale-up lag first:** the time from a traffic jump to a new pod serving.
  If it is a few seconds, forecasting has little to gain.
- **Forecast when pods start slowly and traffic ramps faster than they can follow,** and
  the pattern repeats daily.
- **Keep spare capacity for spikes.** A forecast only helps with load that repeats.
- **Always keep a reactive trigger next to the forecast.** In KEDA, put both triggers on
  one ScaledObject: the HPA takes the higher of the two, and a missing forecast reads as 0.
  KEDA's `fallback` applies to the whole ScaledObject, not to one trigger, so it cannot
  replace this.
- **Scale on incoming requests, not on CPU or completed requests,** which both stop
  rising when pods are overloaded.
- **Start with the simplest forecast.** "Same as this time yesterday" was enough here.
- **Judge it against spare capacity at the same cost,** not against the default HPA.

## Gotchas found along the way

Each of these would have quietly skewed the results
([more](docs/methodology.md#overload-behaviour-two-fixes)):

- **Default probes restart overloaded pods.** The liveness probe timed out under load, the
  pod restarted, and the Service had no endpoints for the whole start-up delay.
- **Overload can blind the scaler.** Without a concurrency limit, hundreds of CPU-bound
  goroutines on 0.5 CPU starved the accept loop and `/metrics`. The request rate the scaler
  saw *fell* while load quadrupled, and KEDA never scaled up.
- **Keep-alive pins load to old pods.** kube-proxy balances connections, not requests, so
  with reused connections a new pod got only part of its share.
- **A one-minute rate remembers the previous traffic.** A scaler applied right after a
  load test scales up immediately.
- **`replicas:` in a Deployment fights the autoscaler.** Every `kubectl apply` resets the
  count.

## Try it

**Requirements:** Docker Desktop with at least 8 CPUs (10 recommended) and about 8 GB of
memory; `kind`, `kubectl`, `helm`, Go 1.26 and Python 3.12. k6 runs inside the cluster.

```sh
make up                  # kind cluster, Prometheus, KEDA, metrics-server (pinned versions)
make deploy              # build the demo app, load it into kind, deploy it
make forecaster-deploy   # build and deploy the forecaster
make arm ARM=d           # attach the predictive autoscaler
make arm-smoke ARM=d     # replay a 10-minute traffic pattern against it
make arm-status          # current replicas and what each trigger asks for
make down                # delete the cluster
```

The forecaster needs 10 minutes of history before it exports anything, and it predicts from
what it has seen, so run `make arm-smoke ARM=d` twice to see it scale ahead of the load.

<details>
<summary>All commands (calibration, backtest, the full experiment)</summary>

```sh
make status      # nodes, CPU allocatable, pods
make test        # Go, trace generator, forecaster and arm-manifest tests (needs make venv)
make calibrate   # stepped load against one pod -> knee and RPS_PER_POD
make lag         # one 25 -> 100 RPS step with reactive KEDA -> scale-up lag breakdown

make traces      # regenerate loadtest/trace/out/ (deterministic per seed)
make trace-check TRACE=synthetic-s1   # replay a trace at 7 fixed replicas, no scaler
make backtest    # forecast accuracy on every trace -> experiments/results/backtest/

make arm ARM=r100        # switch the scaler: a, r100, r80, r60, c or d (one at a time)
make arm-check           # the setups differ only in signal and threshold

make experiment-smoke              # every setup once on the smoke trace (~1.5 h)
make experiment-plan GRID=main     # the runs of a grid and which are done
make experiment GRID=main          # run what is not done yet (main ~27 h, sweep ~8 h)
make experiment GRID=main ONLY=wikipedia LIMIT=6   # a batch
make summarize GRID=main           # recompute summary.csv from the raw files
make analyze                       # every table and figure from the saved runs -> docs/
```

`make calibrate` and `make lag` write to `experiments/results/calibration/` with a
timestamp. The analysis scripts use the virtualenv that `make venv` creates.

</details>

## Repository layout

| Path | What |
|---|---|
| `app/` | Demo service in Go: CPU-bound `/work` endpoint, concurrency limit, start-up delay, Prometheus metrics |
| `forecaster/` | The forecaster in Python: models, service, tests, backtest |
| `deploy/arms/` | One autoscaler per setup: the CPU HPA (A) and the KEDA ScaledObjects (R100, R80, R60, C, D) |
| `deploy/` | Everything else the cluster runs: kind config with CPU pinning, monitoring, KEDA, the app, the forecaster |
| `loadtest/` | Traffic generator (synthetic and Wikipedia) and the k6 scripts that replay it |
| `experiments/` | The harness that runs the experiments, the analysis, and the raw results of every run |
| `docs/` | [Methodology](docs/methodology.md), [results tables](docs/results-tables.md) and figures |

## More detail

- [docs/methodology.md](docs/methodology.md): how every constant was measured (pod
  capacity, scale-up lag, forecast window), the forecast backtest, and how runs are scored.
- [docs/results-tables.md](docs/results-tables.md): every result with confidence intervals.
