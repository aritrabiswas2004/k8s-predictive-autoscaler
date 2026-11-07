# Predictive Autoscaling for Kubernetes

[![CI](https://github.com/aritrabiswas2004/k8s-predictive-autoscaler/actions/workflows/ci.yml/badge.svg)](https://github.com/aritrabiswas2004/k8s-predictive-autoscaler/actions/workflows/ci.yml)

Scale your Deployments **before** traffic arrives. A small forecaster reads your request
rate from Prometheus, predicts the peak of the next few minutes from the daily pattern, and
exports it as a metric. [KEDA](https://keda.sh/) scales on that forecast and on the current
request rate together, so predictable load gets pods early and unexpected load is still
handled the usual way.

**Get started:** [Amazon EKS](docs/install/eks.md) · [Google GKE](docs/install/gke.md) ·
[Azure AKS](docs/install/aks.md) · [Run it locally](#try-it-locally)

- **Ahead of the load.** New pods are ready when the daily ramp starts, not a minute or two
  after it.
- **Safe by default.** If the forecaster is down, still learning, or cannot reach
  Prometheus, it exports nothing, and scaling continues on the current request rate.
- **Plain Kubernetes.** One small Deployment and one ScaledObject per service. No CRDs or
  operators beyond KEDA.
- **Works with managed Prometheus,** or with Prometheus in your cluster.
- **Observable.** Forecast accuracy, coverage and freshness are exported as metrics, with
  alerts for stale or inaccurate forecasts.

## Get started

### Install on your cluster

Pick your platform. Each guide is a complete, copy-paste walkthrough of about 30 minutes:
push the forecaster image, install KEDA, set up Prometheus, deploy the forecaster, attach the
ScaledObject, and check that it works.

> **[→ Amazon EKS](docs/install/eks.md)**<br>
> ECR, KEDA, and kube-prometheus-stack or Amazon Managed Service for Prometheus. Covers
> Karpenter, Cluster Autoscaler and Auto Mode.

> **[→ Google GKE](docs/install/gke.md)**<br>
> Standard and Autopilot. Artifact Registry, KEDA, and Google Cloud Managed Service for
> Prometheus (on by default).

> **[→ Azure AKS](docs/install/aks.md)**<br>
> ACR, the KEDA add-on, and kube-prometheus-stack or Azure Monitor managed Prometheus.

Another cluster? Follow the [EKS guide](docs/install/eks.md) and skip the AWS-specific steps:
it uses kube-prometheus-stack, which runs anywhere.

**What you need:**

- KEDA 2.x (the guides install it, or use the AKS add-on).
- Prometheus scraping your service every 15 s, with at least 3 days of retention. Managed
  Prometheus on each cloud works.
- A request-rate metric for your service, such as `http_requests_total`.
- Traffic with a daily pattern.
- Your scale-up time: how long a new pod takes to serve traffic, including a new node if
  one is needed ([how to measure it](docs/configuration.md#measuring-your-scale-up-time)).

The forecaster starts exporting once it has 2 days of history. Until then, your service
scales on the request rate alone, like a normal autoscaler.

### Try it locally

No cloud account needed. This runs everything on your laptop in a
[kind](https://kind.sigs.k8s.io/) cluster: Prometheus, KEDA, a demo service, the forecaster
and a load generator. A "day" is compressed to 5 minutes, so you see it forecast within
about half an hour instead of 2 days.

Needs Docker Desktop with at least 8 CPUs and 8 GB of memory, plus `kind`, `kubectl`, `helm`,
`make` and Python 3.12.

```sh
git clone https://github.com/aritrabiswas2004/k8s-predictive-autoscaler.git
cd k8s-predictive-autoscaler

make venv up deploy forecaster-deploy   # cluster, monitoring, KEDA, demo service, forecaster
make arm ARM=d                          # attach the predictive autoscaler
make arm-smoke ARM=d                    # replay 10 minutes of daily traffic: the forecaster learns
make arm-smoke ARM=d                    # replay again: now it adds pods ahead of each ramp
make arm-status                         # replicas, and how many pods each trigger asks for
make down                               # delete the cluster
```

Grafana dashboards, comparing it with other autoscalers, and rerunning the benchmarks:
**[local setup guide](docs/local-testbed.md)**.

## How it works

```
         ┌─────────────────────────── scales ────────────────────────────┐
         ▼                                                               │
  traffic ──▶ your pods ── request count ──▶ Prometheus ──▶ KEDA ──▶ HPA
                                              │      ▲
                                     history  │      │  forecast_rps
                                              ▼      │
                                            forecaster
```

1. Your service exposes a request counter, and Prometheus scrapes it.
2. Every 30 s the forecaster reads the last three days of the request rate, forecasts the
   **peak** over a window that starts as far ahead as a new pod needs to become ready, and
   exports the upper bound of that forecast as `forecast_rps`.
3. A KEDA ScaledObject has two Prometheus triggers, the forecast and the current request
   rate, and the HPA follows whichever asks for more pods:

```yaml
triggers:
  - type: prometheus      # predictive: forecast peak of the coming window
    metadata:
      query: max(forecast_rps{job="checkout-forecaster",model="naive",bound="upper"})
      threshold: "40"     # requests/s per pod
  - type: prometheus      # reactive: the current request rate
    metadata:
      query: sum(rate(http_requests_total{service="checkout"}[1m]))
      threshold: "40"
```

The forecast model is seasonal naive ("same time yesterday") with an upper bound learned
from its own past errors.

## Configuration

The forecaster is configured with environment variables. The ones you set for every service:

| Variable | Typical value | Meaning |
|---|---|---|
| `PROMETHEUS_URL` | depends on the platform | Where to read the request rate |
| `QUERY` | `sum(rate(http_requests_total{service="checkout"}[1m]))` | Your service's request rate, as one series |
| `CYCLE_SECONDS` / `STEP_SECONDS` | `86400` / `60` | A daily pattern, sampled every minute |
| `LEAD_SECONDS` | scale-up time + 30 s | How far ahead the forecast window starts |
| `RPS_PER_POD` / `MAX_REPLICAS` | your threshold / `maxReplicaCount` | Caps the forecast at what your maximum replica count can serve |

How to choose them, every other variable, the exported metrics, alerts and failure behaviour:
**[docs/configuration.md](docs/configuration.md)**.

## Performance

The predictive autoscaler (**D**) was benchmarked against five other setups, 5 runs each, on
two traffic patterns: a synthetic daily pattern whose days vary in height and timing, and a
real week of German Wikipedia traffic. The other setups are the default HPA on CPU (**A**),
KEDA on the current request rate with 0, 20 and 40 % spare capacity (**R100**, **R80**,
**R60**), and the forecast alone (**C**). A minute counts as slow if its p95 latency is over
300 ms or more than 5 % of requests failed.

![Cost against slow minutes](docs/img/frontier.png)

| Setup | Varying daily traffic: slow minutes (of 10) | Cost vs R100 | Wikipedia: slow minutes (of 10) | Cost vs R100 |
|---|---:|---:|---:|---:|
| A: HPA on CPU | 3.2 | −8 % | 2.6 | −18 % |
| R100: request rate | 2.8 | — | 2.0 | — |
| R80: request rate, 20 % spare | 2.2 | +24 % | 1.8 | +23 % |
| R60: request rate, 40 % spare | 2.0 | +51 % | 0.2 | +49 % |
| C: forecast only | 0.6 | +20 % | 2.0 | −2 % |
| **D: forecast + request rate** | **0.2** | +31 % | 2.0 | +9 % |

- **Varying daily traffic:** D cut slow minutes from 2.8 to 0.2, with 14 % fewer
  pod-seconds than adding 40 % spare capacity, which still left 2.0 slow minutes.
- **Smooth daily traffic:** reactive scaling already keeps up, so D performs the same as
  R100 at 9 % more cost. The remaining slow minutes are a sudden spike that no forecast can
  see; only spare capacity absorbs it.
- **The slower your pods start, the more it helps.** With a 30 s or 60 s start-up, D's
  advantage was clear. With pods that start instantly, it was too small to tell apart from
  noise. On a cloud cluster, waiting for a new node adds to the start-up.

- **When the forecaster dies,** D falls back to the request rate. In tests where the
  forecaster was stopped for 3 minutes under load, D cost no more than with its forecast,
  and had 0–2 more slow minutes on varying traffic, about as many as R100 in that cycle. The
  forecast-only setup (C) went to its fallback of maximum pods: no extra slow minutes, but
  40 % more pod-seconds. [Fault test write-up](docs/results.md).

All results, with confidence intervals: [docs/results-tables.md](docs/results-tables.md).
How the benchmarks work: [docs/methodology.md](docs/methodology.md).

## Limitations

- **One repeating pattern.** A daily cycle does not know about weekends or holidays; the
  request-rate trigger covers the difference.
- **Two days of warm-up** before the first forecast.
- **Sudden spikes are not forecast.** Keep spare capacity if you need to absorb them.
- **Managed Prometheus on EKS and AKS** needs an authentication proxy next to the forecaster,
  because the forecaster does not sign requests itself. GKE's managed Prometheus works without
  one.
- **Where it has been tested.** The benchmarks ran on a local cluster with days compressed to
  5 minutes. The cloud install guides follow each provider's documentation and have not yet
  been run end to end on every provider.

## Development

Development happens on the [local setup](docs/local-testbed.md), which also holds the
benchmark harness that produced the results above.

```sh
make venv && make test   # unit tests: Go, forecaster, load generator, manifests
```

| Path | What |
|---|---|
| `forecaster/` | The forecaster (Python): models, service, tests, Dockerfile |
| `deploy/forecaster/` | Forecaster manifests and alerts for the local setup |
| `deploy/arms/` | The benchmarked autoscaler setups; `d-hybrid.yaml` is the predictive one |
| `docs/install/` | Install guides for EKS, GKE and AKS |
| `app/`, `deploy/`, `loadtest/`, `experiments/` | Local test setup: demo service, cluster config, traffic replay, benchmark harness and raw results |
