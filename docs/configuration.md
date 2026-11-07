# Configuration

How to choose the forecaster's settings for your service, and what it exports. The install
guides for [EKS](install/eks.md), [GKE](install/gke.md) and [AKS](install/aks.md) link here.

- [Choosing the settings](#choosing-the-settings)
- [Measuring your scale-up time](#measuring-your-scale-up-time)
- [Spare node capacity](#spare-node-capacity)
- [Environment variables](#environment-variables)
- [Metrics](#metrics)
- [Alerts](#alerts)
- [When things go wrong](#when-things-go-wrong)

## Choosing the settings

| Setting | How to choose it |
|---|---|
| Request-rate query (`QUERY` and the ScaledObject's `arrivals` trigger) | Your service's request rate as **one** series, e.g. `sum(rate(http_requests_total{namespace="shop",service="checkout"}[1m]))`. A counter that counts requests when they arrive is best: counters of completed requests, like CPU usage, stop rising when pods are overloaded, which is exactly when more pods are needed. |
| Rate window | `[1m]` needs a 15 s scrape interval (at least four samples per window). With a 30 s scrape interval use `[2m]`, and add 30 s to `LEAD_SECONDS`, because a longer window trails real traffic by about that much more. |
| Threshold (both triggers) and `RPS_PER_POD` | The request rate one pod handles while still meeting your latency target, with margin. Load-test one pod, find where latency starts to climb, and use about 75 % of that rate. |
| `LEAD_SECONDS` | Your [scale-up time](#measuring-your-scale-up-time) plus 30 s, rounded up to a multiple of `STEP_SECONDS`. |
| `WINDOW_SECONDS` | 60 s is a good default: the forecast is the peak of `[now + LEAD, now + LEAD + WINDOW]`, and a new forecast comes every 30 s. |
| `CYCLE_SECONDS` | The length of the repeating pattern: `86400` for a daily pattern. |
| `STEP_SECONDS` | Keep `HISTORY_CYCLES × CYCLE_SECONDS / STEP_SECONDS` under 11,000, Prometheus' limit of points per query. For a daily cycle and 3 cycles of history, use `60`. |
| `MODELS` | `naive`. Holt-Winters fits one seasonal term per step, which is slow for a daily cycle. |
| `MAX_REPLICAS` | The ScaledObject's `maxReplicaCount`. With `RPS_PER_POD` it caps the forecast at what the maximum replica count can serve. |

The forecaster only exports a forecast once it has `MIN_HISTORY_CYCLES` cycles of history (2
days for a daily cycle), so Prometheus must keep at least `HISTORY_CYCLES` cycles (3 days).
Until then, the ScaledObject scales on the request rate alone.

## Measuring your scale-up time

`LEAD_SECONDS` must cover the time from the moment a new pod is requested to the moment it
serves traffic. Measure it on the cluster you will run on, at a time when no spare node
capacity is free, so that the measurement includes adding a node:

```sh
kubectl -n shop scale deployment checkout --replicas=<current + 1>
kubectl -n shop get pods -l app=checkout -w     # note when the new pod turns Ready
```

The time to Ready includes scheduling, any new node, the image pull and your application's
start-up. If a node has to be added (Cluster Autoscaler, Karpenter, Node Auto-Provisioning or
GKE Autopilot), that often adds a minute or more.

## Spare node capacity

If new pods have to wait for a new node, the forecast has to look further ahead, and its
error grows with the distance. Keeping a little free node capacity removes the node from
the critical path. A common way is a low-priority placeholder that any real pod can evict:

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: placeholder
value: -10
globalDefault: false
description: Placeholder pods that real workloads evict, to keep spare node capacity.
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: capacity-placeholder
  namespace: shop
spec:
  replicas: 2          # how many pods' worth of spare capacity to keep
  selector:
    matchLabels: {app: capacity-placeholder}
  template:
    metadata:
      labels: {app: capacity-placeholder}
    spec:
      priorityClassName: placeholder
      terminationGracePeriodSeconds: 0
      containers:
        - name: pause
          image: registry.k8s.io/pause:3.10
          resources:
            requests: {cpu: 500m, memory: 128Mi}   # same as one of your pods
```

When the forecast scales up, your pods evict the placeholders and start at once; the
autoscaler then adds a node for the placeholders in the background.

## Environment variables

Every setting is an environment variable on the forecaster container.

| Variable | Default | Meaning |
|---|---|---|
| `PROMETHEUS_URL` | `http://kube-prometheus-stack-prometheus.monitoring.svc:9090` | Prometheus to read from (no authentication; see the install guides for managed Prometheus) |
| `QUERY` | `sum(rate(http_requests_received_total{app="web"}[1m]))` | Request rate to forecast; must return one series |
| `MODELS` | `naive,holtwinters` | Models to run: `naive`, `holtwinters` |
| `CYCLE_SECONDS` | `300` | Length of the repeating pattern (the default is the test setup's 5-minute "day") |
| `STEP_SECONDS` | `15` | Sample spacing of the history; cycle, lead and window must be multiples of it |
| `HISTORY_CYCLES` | `3` | Cycles of history each fit uses |
| `MIN_HISTORY_CYCLES` | `2` | Below this, export nothing |
| `MAX_GAP_STEPS` | `2` | Gaps up to this many samples are interpolated; longer gaps cut the history |
| `LEAD_SECONDS` | `60` | The forecast window starts this far ahead |
| `WINDOW_SECONDS` | `60` | Forecast window length |
| `UPPER_QUANTILE` | `0.9` | One-sided upper bound exported as `bound="upper"` |
| `RPS_PER_POD` | `40` | With `MAX_REPLICAS`, caps forecasts at `MAX_REPLICAS × RPS_PER_POD` |
| `MAX_REPLICAS` | `7` | See `RPS_PER_POD` |
| `INTERVAL_SECONDS` | `30` | How often to forecast |
| `SMAPE_SAMPLES` | `10` | Forecasts in the rolling accuracy metric |
| `QUERY_TIMEOUT_SECONDS` | `10` | Prometheus query timeout |
| `PORT` | `8000` | Port of `/metrics` |

## Metrics

| Metric | Labels | Meaning |
|---|---|---|
| `forecast_rps` | `model`, `bound` (`upper`, `mean`) | Forecast peak request rate for the coming window. Scale on `bound="upper"`. |
| `forecast_last_success_timestamp_seconds` | `model` | Time of the last successful forecast |
| `forecast_history_seconds` | | Usable history; nothing is exported below `MIN_HISTORY_CYCLES` |
| `forecast_smape` | `model` | Rolling forecast error (0.1 = 10 %) against the realized peak |
| `forecast_coverage_ratio` | `model` | Share of recent windows whose peak stayed under the upper bound; should be near `UPPER_QUANTILE` |
| `forecast_fit_duration_seconds` | `model` | Duration of the last fit |
| `forecast_errors_total` | `stage`, `model` | Failed forecast attempts |

## Alerts

Two alerts are worth having. [`deploy/forecaster/alerts.yaml`](../deploy/forecaster/alerts.yaml)
has them as a `PrometheusRule` for the Prometheus Operator; on managed Prometheus, create them
in your provider's alerting with the same expressions.

| Alert | Expression | Means |
|---|---|---|
| ForecastStale | `(time() - max(forecast_last_success_timestamp_seconds) > 60) or absent(forecast_last_success_timestamp_seconds)` for 15 s | No fresh forecast; scaling has fallen back to the request rate |
| ForecastInaccurate | `max by (model) (forecast_smape{model="naive"}) > 0.4` for 10 min | Forecasts are far off, for example after the traffic pattern changed |

## When things go wrong

| Situation | What the forecaster does | What scaling does |
|---|---|---|
| Not enough history yet | Exports nothing | Scales on the request rate alone |
| Prometheus query fails | Keeps the last forecast until its window has passed, then exports nothing | Same as above once the forecast is gone |
| One model fails | Stops exporting that model only | Unaffected unless it is the model you scale on |
| Forecaster down | Its series go stale; ForecastStale fires | Scales on the request rate alone |
| Absurd forecast | Capped at `MAX_REPLICAS × RPS_PER_POD` | Never more than `maxReplicaCount` |

KEDA reads a missing forecast as 0, so the request-rate trigger decides alone. KEDA's
`fallback` applies to the whole ScaledObject, so it only takes over when Prometheus itself
is unreachable and both triggers fail.
