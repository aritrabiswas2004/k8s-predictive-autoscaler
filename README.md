# Predictive Autoscaling for Kubernetes

A predictive autoscaler for Kubernetes. It forecasts request load and scales a Deployment
**before** the load arrives, using [KEDA](https://keda.sh/), with reactive scaling kept as a
safety net. It comes with an evaluation that measures what the prediction buys over
well-tuned reactive scaling, run under identical load. Everything runs locally on a
[kind](https://kind.sigs.k8s.io/) cluster.

**Why predict?** A reactive autoscaler only adds pods after load has already risen. On this
setup a new pod starts serving **60–75 s after a load step**
([measured](docs/methodology.md#scale-up-lag)), and requests queue the whole time. The
answer is to start the pods before the load arrives.

> **Status:** work in progress. Done so far: the cluster, the demo app, the capacity
> calibration, the load traces, the forecaster (with a backtest of its accuracy), the six
> scaling arms and the experiment harness that runs them. The experiment runs and their
> analysis are next. There are no evaluation results yet, so nothing here claims the
> prediction helps; the evaluation will show whether, and when, it does.

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

1. **The app counts arrivals.** Each pod counts requests as they arrive
   (`http_requests_received_total`), and Prometheus scrapes the counts. Arrivals show demand
   even when pods are overloaded: completions and CPU usage flatten out exactly when more
   pods are needed.
2. **The forecaster predicts the coming peak.** Every 30 s it reads the last three cycles
   of arrivals and fits two seasonal models: "same as one cycle ago" (seasonal naive) and
   Holt-Winters. For each it exports `forecast_rps`: the upper bound of the **peak** load
   60–120 s ahead, which is the time a new pod takes to start serving plus a margin
   ([why](docs/methodology.md#what-it-forecasts-and-how-far-ahead)). Pods must be ready
   before the load and stay up through it, so it forecasts the peak, not the load at one
   moment. The scalers read the seasonal naive forecast, which did as well or better in the
   [backtest](#forecast-accuracy-backtest); Holt-Winters is still exported and scored.
3. **KEDA scales on the forecast and on current load.** One ScaledObject has two triggers,
   the forecast and the current arrival rate, and the HPA follows whichever asks for more
   pods. The forecast scales ahead of predictable load. The reactive trigger catches what
   the forecast misses, such as a sudden spike.
4. **It fails safe.** If the forecaster stops or has no history yet, it exports nothing
   rather than a made-up number, and the reactive trigger keeps scaling. The evaluation
   kills the forecaster mid-run to check this.

## How it is evaluated

The autoscaler (arm D) and the baselines are run against the same load traces in
sequence, from the same starting state:

| Arm | Scaling |
|---|---|
| A | Kubernetes HPA on CPU, 70 % of the request (the common default) |
| R100 / R80 / R60 | Reactive KEDA on arrivals, targeting 100 / 80 / 60 % of a pod's capacity |
| C | Forecast only, no reactive trigger |
| **D** | **The predictive autoscaler: forecast + reactive** |

Every arm has the same replica bounds (2–7) and the same scaling behaviour (scale-down
stabilization 120 s), and only one scaler is ever attached to the app
([manifests](deploy/arms/), [details](docs/methodology.md#scaling-arms)).

- **SLO:** client-side p95 latency < 300 ms, measured by k6 (it includes queueing, which
  server-side metrics miss). The score is the number of scored minutes in violation: a
  minute violates it if its p95 is over 300 ms or more than 5 % of its requests failed or
  were never sent ([details](docs/methodology.md#experiment-harness)).
- **Cost:** replica-seconds, priced at a stated reference instance price.
- **The fair competitor is over-provisioning.** Any autoscaler violates the SLO less if it
  runs more pods. The reactive arms at three headroom levels trace a cost vs. SLO-violation
  curve, and the predictive autoscaler is only worth it if it lands **below that curve**:
  fewer violations for the same money.
- **Two workloads.** A synthetic daily pattern whose cycles vary in height, timing and
  trend, and real German Wikipedia traffic. Each run is five compressed days of 5 minutes:
  three of history to learn from, then two scored, the last with a spike the forecaster
  cannot know about. Load is open-model (k6 keeps sending at the trace's rate however slow
  the app gets), and every request opens a new connection so it can land on any pod.

![The two workloads](docs/img/traces.png)
- The app runs on its own CPU-pinned node, so load generation and monitoring do not
  steal CPU from it.

## Calibration results

Details, raw data and how to reproduce: **[docs/methodology.md](docs/methodology.md)**.

| Constant | Value |
|---|---|
| CPU per request | ≈ 8.5 ms (`WORK_ITERATIONS=150000`) |
| Knee of one 500m pod | ≈ 54 RPS (saturates at 58–60) |
| `RPS_PER_POD` | 40 (75 % of the knee) |
| Scale-up lag, step → new pod serving | 60.3–60.9 s measured, 60–75 s expected |
| Replicas | min 2, max 7 |

![Capacity of one pod and of seven](docs/img/calibration.png)

![Scale-up after a 25 → 100 RPS step](docs/img/scale-up-lag.png)

## Forecast accuracy (backtest)

Rolling-origin backtest on the scored cycles of every trace, at the forecaster's actual
lead (details: [methodology](docs/methodology.md#backtest)):

| Workload | Model | Peak sMAPE | 90 % bound covers | Short of pods |
|---|---|---:|---:|---:|
| Synthetic | seasonal naive | 21.9 % | 85 % | 4 % |
| Synthetic | Holt-Winters | 21.1 % | 75 % | 5 % |
| Synthetic | *current rate (what reactive scaling sees)* | 66.5 % | 40 % | 42 % |
| Wikipedia | seasonal naive | 2.8 % | 78 % | 5 % |
| Wikipedia | Holt-Winters | 5.9 % | 89 % | 0 % |
| Wikipedia | *current rate (what reactive scaling sees)* | 45.9 % | 24 % | 64 % |

Forecasting a minute or two ahead is far better than using the current rate. The simple
seasonal copy is as good as Holt-Winters on the varied synthetic load and clearly better on
real Wikipedia traffic.

![Backtest](docs/img/backtest.png)

## Repository layout

| Path | What |
|---|---|
| `app/` | Demo app in Go: CPU-bound `/work` handler with a concurrency limit, a startup delay, `/ready`, `/healthz` and Prometheus `/metrics` |
| `deploy/kind/` | kind cluster config, plus `up.sh`, which pins the node containers to CPU sets |
| `deploy/monitoring/`, `deploy/keda/` | Helm values for kube-prometheus-stack and KEDA |
| `deploy/app/` | Deployment, Service, PodDisruptionBudget, ServiceMonitor |
| `deploy/calibration/` | ScaledObject used by the lag measurement |
| `deploy/arms/` | One scaler per arm: the CPU HPA (A) and the KEDA ScaledObjects (R100, R80, R60, C, D) |
| `deploy/metrics-server/` | Helm values for metrics-server (CPU metrics for arm A) |
| `forecaster/` | Forecaster in Python: models (seasonal naive, Holt-Winters), service, tests, backtest, Dockerfile |
| `deploy/forecaster/` | Forecaster Deployment, Service, ServiceMonitor, Grafana dashboard |
| `loadtest/trace/` | Trace generator (synthetic and Wikipedia), its tests, the raw Wikipedia data and the generated traces |
| `loadtest/k6/`, `loadtest/*.sh` | k6 scripts (trace player, calibration, load step) and the scripts that run k6 inside the cluster |
| `experiments/run_experiment.py`, `experiments/metrics.py` | The experiment harness (grids, runs, raw data per run) and the per-run summary metrics |
| `experiments/arms.py` | Switches arms: removes the old scaler, resets the replicas, waits for idle, applies the new one; also checks that the arms differ only where they should |
| `experiments/analysis/` | Python analysis and plots |
| `experiments/results/` | Raw results of every run that the docs cite |
| `docs/` | Methodology and figures |

## Running it

**Requirements**
- Docker Desktop with at least 8 CPUs (10 recommended) and about 8 GB of memory.
- `kind`, `kubectl`, `helm`, Go 1.26 and Python 3.12.
- k6 runs inside the cluster, so it does not need to be installed locally.

```sh
make up          # kind cluster + kube-prometheus-stack + KEDA + metrics-server (pinned charts)
make deploy      # build the app image, load it into kind, deploy it
make status      # nodes, CPU allocatable, pods

make test        # Go, trace generator, forecaster and arm-manifest tests (needs make venv)
make calibrate   # stepped load against one pod  -> knee and RPS_PER_POD
make lag         # one 25 -> 100 RPS step with reactive KEDA -> scale-up lag breakdown

make traces      # regenerate loadtest/trace/out/ (deterministic per seed)
make trace-check TRACE=synthetic-s1   # replay a trace at 7 fixed replicas, no scaler

make forecaster-deploy   # build the forecaster image, load it into kind, deploy it
make backtest            # forecast accuracy on every trace -> experiments/results/backtest/

make arm ARM=r100        # switch the scaler: a, r100, r80, r60, c or d (one at a time)
make arm-status          # active scaler, replicas and its current metric values
make arm-check           # the arms differ only in signal and threshold
make arm-smoke ARM=d     # apply an arm and replay the 10-minute smoke trace against it

make experiment-smoke              # every arm once on the smoke trace (~1.5 h)
make experiment-plan GRID=main     # the runs of a grid and which are done
make experiment GRID=main          # run what is not done yet (main ~29 h, sweep ~9 h)
make experiment GRID=main ONLY=wikipedia LIMIT=6   # a batch
make summarize GRID=main           # recompute summary.csv from the raw files
make analyze                       # every table and figure from the saved runs -> docs/

make down        # delete the cluster
```

`make calibrate` and `make lag` write to `experiments/results/calibration/` with a timestamp.
The analysis scripts use the virtualenv that `make venv` creates.

## Roadmap

- [x] **Phase 0**: kind cluster with CPU pinning, Prometheus, KEDA
- [x] **Phase 1**: demo app
- [x] **Phase 2**: capacity calibration and scale-up lag
- [x] **Phase 3**: load traces (synthetic with varying cycles, German Wikipedia) and the
  k6 trace player
- [x] **Phase 4**: forecaster (seasonal naive and Holt-Winters, exports the forecast peak
  for KEDA) and its backtest
- [x] **Phase 5**: scaling arms (HPA, reactive KEDA at three headroom levels, forecast, hybrid)
- [x] **Phase 6**: experiment harness (repeated runs, arm order rotated between repetitions)
- [ ] Experiment runs: main grid (60 runs) and startup-delay sweep (18 runs)
- [ ] **Phase 7**: analysis: cost vs. SLO-violation frontier, startup-delay sweep
- [ ] **Phase 8**: fault injection (kill the forecaster mid-run; the fallback should keep the service healthy)
