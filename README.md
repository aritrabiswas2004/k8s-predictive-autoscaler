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
> calibration and the load traces. The forecaster (the core of the autoscaler) and the
> evaluation are next. There are no results yet, so nothing here claims the prediction
> helps; the evaluation will show whether, and when, it does.

## How it works (design; the forecaster is being built)

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
2. **The forecaster predicts the coming peak.** Every 30 s it reads the recent arrival
   history and fits a seasonal model (Holt-Winters, with "same as one cycle ago" as the
   baseline). It then exports `forecast_rps`: the upper bound of the **peak** load in a
   window that starts about 75 s ahead. Pods must be ready before the load and stay up
   through it, so it forecasts the peak, not the load at one moment.
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

- **SLO:** client-side p95 latency < 300 ms, measured by k6 (it includes queueing, which
  server-side metrics miss). The score is the number of minutes in violation.
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

## Repository layout

| Path | What |
|---|---|
| `app/` | Demo app in Go: CPU-bound `/work` handler with a concurrency limit, a startup delay, `/ready`, `/healthz` and Prometheus `/metrics` |
| `deploy/kind/` | kind cluster config, plus `up.sh`, which pins the node containers to CPU sets |
| `deploy/monitoring/`, `deploy/keda/` | Helm values for kube-prometheus-stack and KEDA |
| `deploy/app/` | Deployment, Service, PodDisruptionBudget, ServiceMonitor |
| `deploy/calibration/` | ScaledObject used by the lag measurement |
| `loadtest/trace/` | Trace generator (synthetic and Wikipedia), its tests, the raw Wikipedia data and the generated traces |
| `loadtest/k6/`, `loadtest/*.sh` | k6 scripts (trace player, calibration, load step) and the scripts that run k6 inside the cluster |
| `experiments/analysis/` | Python analysis and plots |
| `experiments/results/` | Raw results of every run that the docs cite |
| `docs/` | Methodology and figures |

## Running it

**Requirements**
- Docker Desktop with at least 8 CPUs (10 recommended) and about 8 GB of memory.
- `kind`, `kubectl`, `helm`, Go 1.26 and Python 3.12.
- k6 runs inside the cluster, so it does not need to be installed locally.

```sh
make up          # kind cluster + kube-prometheus-stack + KEDA (pinned chart versions)
make deploy      # build the app image, load it into kind, deploy it
make status      # nodes, CPU allocatable, pods

make test        # Go unit tests + trace generator tests
make calibrate   # stepped load against one pod  -> knee and RPS_PER_POD
make lag         # one 25 -> 100 RPS step with reactive KEDA -> scale-up lag breakdown

make traces      # regenerate loadtest/trace/out/ (deterministic per seed)
make trace-check TRACE=synthetic-s1   # replay a trace at 7 fixed replicas, no scaler

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
- [ ] **Phase 4**: forecaster (Python, reads Prometheus, exports a forecast metric)
- [ ] **Phase 5**: scaling arms (HPA, reactive KEDA at three headroom levels, forecast, hybrid)
- [ ] **Phase 6**: experiment harness (repeated runs, arm order rotated between repetitions)
- [ ] **Phase 7**: analysis: cost vs. SLO-violation frontier, startup-delay sweep
- [ ] **Phase 8**: fault injection (kill the forecaster mid-run; the fallback should keep the service healthy)
