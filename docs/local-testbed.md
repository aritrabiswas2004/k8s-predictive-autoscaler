# Local test setup

Run the whole stack on your laptop: a [kind](https://kind.sigs.k8s.io/) cluster with
Prometheus, KEDA, a demo service, the forecaster and a load generator. Use it to try the
autoscaler without a cloud account, to develop the forecaster, or to rerun the benchmarks
behind the [performance results](../README.md#performance).

In this setup a "day" is compressed to 5 minutes, so the forecaster learns in 10 minutes
instead of 2 days.

## Requirements

- Docker Desktop with at least 8 CPUs (10 recommended) and about 8 GB of memory.
- `kind`, `kubectl`, `helm`, `make` and Python 3.12. The load generator (k6) runs inside the
  cluster.
- For the unit tests also Go 1.26.

## Try it

```sh
make venv                # Python virtualenv for the helper scripts
make up                  # kind cluster, Prometheus, KEDA, metrics-server (pinned versions)
make deploy              # build the demo service and deploy it
make forecaster-deploy   # build and deploy the forecaster
make arm ARM=d           # attach the predictive autoscaler to the demo service
make arm-smoke ARM=d     # replay 10 minutes of daily-pattern traffic against it
make arm-status          # replicas, and how many pods each trigger asks for
```

The forecaster needs 10 minutes of history before it exports anything, and it predicts from
the traffic it has seen. Run `make arm-smoke ARM=d` a second time to watch it add pods ahead
of each ramp.

To watch it in Grafana:

```sh
kubectl -n monitoring get secret kube-prometheus-stack-grafana \
  -o jsonpath='{.data.admin-password}' | base64 -d; echo     # password for user "admin"
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80
```

Open http://localhost:3000 and the dashboard **Predictive autoscaling: forecast**.

`make down` deletes the cluster.

## The cluster

- Three nodes: control plane, an app node that runs only the demo service, and a tools node
  for Prometheus, KEDA, the forecaster and k6.
- The app node is pinned to its own CPUs, so load generation and monitoring do not slow the
  service under test.
- The demo service ([`app/`](../app/)) burns a fixed amount of CPU per request, waits a
  configurable start-up delay (30 s by default) before it reports ready, and counts requests
  as they arrive.

Details: [methodology](methodology.md#setup).

## Compare autoscalers

The setups from the [performance results](../README.md#performance) are in
[`deploy/arms/`](../deploy/arms/). Only one is attached at a time; switching removes the old
one and resets the replicas first.

```sh
make arm ARM=r100        # a, r100, r80, r60, c or d
make arm-smoke ARM=r100  # 10-minute smoke test against it
make arm-check           # the setups differ only in signal and threshold
```

## Run the benchmarks

```sh
make experiment-smoke              # every setup once (~1.5 h)
make experiment GRID=main          # the full comparison: 6 setups x 2 workloads x 5 (~27 h)
make experiment GRID=sweep         # pod start-up delay 0 / 30 / 60 s (~8 h)
make experiment GRID=fault         # kill the forecaster mid-run
make experiment-plan GRID=main     # which runs of a grid are done
make experiment GRID=main ONLY=wikipedia LIMIT=6   # a batch
```

Grids are resumable: rerunning a command skips finished runs.

```sh
make summarize GRID=main           # recompute every run's summary from its raw files
make analyze                       # every table and chart from the saved runs
```

Neither needs a cluster. On a fresh clone, `make analyze` rebuilds every published result
from the raw data in [`experiments/results/runs/`](../experiments/results/runs/).

## Calibration and other tools

```sh
make calibrate                        # stepped load against one pod: capacity per pod
make lag                              # one traffic step: scale-up time, step by step
make traces                           # regenerate the traffic patterns (deterministic per seed)
make trace-check TRACE=synthetic-s1   # replay a pattern at 7 fixed replicas, no autoscaler
make backtest                         # forecast accuracy on every pattern
make status                           # nodes, allocatable CPU, pods
```

`make calibrate` and `make lag` write to `experiments/results/calibration/` with a timestamp.

## Tests

```sh
make venv && make test   # Go, forecaster, load-generator and manifest tests
```
