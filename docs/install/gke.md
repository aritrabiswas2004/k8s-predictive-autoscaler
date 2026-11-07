# Install on Google Kubernetes Engine (GKE)

Set up predictive autoscaling for one Deployment on a GKE Standard or Autopilot cluster:
push the forecaster image to Artifact Registry, install KEDA, query Google Cloud Managed
Service for Prometheus, deploy the forecaster next to your service and attach a
ScaledObject. About 30 minutes, plus two days before the first forecast (the forecaster
learns from your traffic first).

1. [Before you start](#1-before-you-start)
2. [Push the forecaster image to Artifact Registry](#2-push-the-forecaster-image-to-artifact-registry)
3. [Install KEDA](#3-install-keda)
4. [Set up Managed Service for Prometheus](#4-set-up-managed-service-for-prometheus)
5. [Deploy the forecaster](#5-deploy-the-forecaster)
6. [Attach the ScaledObject](#6-attach-the-scaledobject)
7. [Check that it works](#7-check-that-it-works)
8. [Node provisioning: Cluster Autoscaler, node auto-provisioning, Autopilot](#8-node-provisioning)

Already running kube-prometheus-stack in the cluster? See [Option B](#option-b-prometheus-in-the-cluster).
To remove everything: [Uninstall](#uninstall).

## 1. Before you start

You need:

- A GKE cluster with Workload Identity Federation enabled (always on in Autopilot), and
  `kubectl` pointed at it:
  `gcloud container clusters get-credentials <cluster> --location <location>`
- The `gcloud` CLI, `helm`, and Docker with `buildx`.
- A Deployment that exposes a request counter in Prometheus format (for example
  `http_requests_total`), and traffic with a daily pattern.
- Your service's scale-up time: how long a new pod takes to serve traffic, including a new
  node if one is needed ([how to measure it](../configuration.md#measuring-your-scale-up-time)).

Set these once; every command below uses them:

```sh
export PROJECT_ID=$(gcloud config get-value project)
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
export CLUSTER=my-cluster
export LOCATION=europe-west4    # the cluster's region or zone
export REGION=europe-west4      # Artifact Registry region
export IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/predictive/forecaster:0.1.0

export NS=shop                  # namespace of your Deployment
export APP=checkout             # name of your Deployment
export QUERY='sum(rate(http_requests_total{namespace="shop",job="checkout"}[1m]))'
export RPS_PER_POD=40           # requests/s one pod should handle
export MIN_REPLICAS=2
export MAX_REPLICAS=20
export LEAD_SECONDS=120         # scale-up time + 30 s, rounded up to a multiple of 60
```

How to choose each value: [configuration](../configuration.md#choosing-the-settings).

## 2. Push the forecaster image to Artifact Registry

From a clone of this repository:

```sh
gcloud artifacts repositories create predictive \
  --repository-format=docker --location=$REGION
gcloud auth configure-docker $REGION-docker.pkg.dev

# Both architectures, so it runs on x86 and Arm (Tau T2A, Axion) nodes.
docker buildx build --platform linux/amd64,linux/arm64 -t $IMAGE --push forecaster/
```

Nodes can pull from Artifact Registry in the same project if their service account has
`roles/artifactregistry.reader`, which the default node service account usually has.

## 3. Install KEDA

Skip this if KEDA is already installed.

```sh
helm repo add kedacore https://kedacore.github.io/charts
helm upgrade --install keda kedacore/keda --version 2.21.0 \
  --namespace keda --create-namespace --wait
```

**Private clusters:** the control plane must reach KEDA's metrics server and webhook on the
nodes. Add a firewall rule that allows TCP 6443 and 9443 from the control plane's address
range to the nodes, or `kubectl get hpa` will show the metrics as unknown.

## 4. Set up Managed Service for Prometheus

Managed collection is on by default on new clusters. Check, and turn it on if needed:

```sh
gcloud container clusters describe $CLUSTER --location $LOCATION \
  --format='value(monitoringConfig.managedPrometheusConfig.enabled)'
gcloud container clusters update $CLUSTER --location $LOCATION --enable-managed-prometheus
```

**Scrape your service every 15 s.** The default `PodMonitoring` interval is 60 s, which is too
coarse for a one-minute rate. This assumes your pods have the label `app: $APP` and a
container port named `http` that serves `/metrics`:

```sh
kubectl apply -f - <<EOF
apiVersion: monitoring.googleapis.com/v1
kind: PodMonitoring
metadata:
  name: $APP
  namespace: $NS
spec:
  selector:
    matchLabels:
      app: $APP
  endpoints:
    - port: http
      path: /metrics
      interval: 15s
EOF
```

The series get the label `job="$APP"` (the PodMonitoring's name), which is what `QUERY` above
selects on.

**Deploy the query frontend.** The forecaster and KEDA query Prometheus over plain HTTP. Google's
frontend serves the Prometheus HTTP API inside the cluster and adds the Google Cloud
credentials, so neither needs changes. It runs as the `default` service account in
`monitoring` and needs permission to read metrics:

```sh
kubectl create namespace monitoring

gcloud projects add-iam-policy-binding $PROJECT_ID --role=roles/monitoring.viewer \
  --member=principal://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$PROJECT_ID.svc.id.goog/subject/ns/monitoring/sa/default

# <version>: the latest release of github.com/GoogleCloudPlatform/prometheus-engine
curl -s https://raw.githubusercontent.com/GoogleCloudPlatform/prometheus-engine/<version>/examples/frontend.yaml \
  | sed "s/\$PROJECT_ID/$PROJECT_ID/" \
  | kubectl apply -n monitoring -f -

export PROMETHEUS_URL=http://frontend.monitoring.svc:9090
```

See Google's guide to [querying with the Prometheus API](https://cloud.google.com/stackdriver/docs/managed-prometheus/query-api-ui)
for details. Check that `QUERY` returns one series before going on:

```sh
kubectl -n monitoring port-forward svc/frontend 9090 &
curl -s localhost:9090/api/v1/query --data-urlencode "query=$QUERY"
```

Managed Service for Prometheus keeps data for 24 months, so the forecaster's history survives
restarts.

## 5. Deploy the forecaster

One forecaster per scaled Deployment, in the same namespace:

```sh
kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $APP-forecaster
  namespace: $NS
  labels:
    app: $APP-forecaster
spec:
  replicas: 1
  strategy:
    type: Recreate          # two instances would export conflicting forecasts
  selector:
    matchLabels:
      app: $APP-forecaster
  template:
    metadata:
      labels:
        app: $APP-forecaster
    spec:
      containers:
        - name: forecaster
          image: $IMAGE
          ports:
            - name: metrics
              containerPort: 8000
          env:
            - {name: PROMETHEUS_URL, value: "$PROMETHEUS_URL"}
            - {name: QUERY, value: '$QUERY'}
            - {name: MODELS, value: naive}
            - {name: CYCLE_SECONDS, value: "86400"}
            - {name: STEP_SECONDS, value: "60"}
            - {name: LEAD_SECONDS, value: "$LEAD_SECONDS"}
            - {name: WINDOW_SECONDS, value: "60"}
            - {name: RPS_PER_POD, value: "$RPS_PER_POD"}
            - {name: MAX_REPLICAS, value: "$MAX_REPLICAS"}
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits: {memory: 512Mi}
          livenessProbe:
            httpGet: {path: /metrics, port: metrics}
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 6
          securityContext:
            runAsNonRoot: true
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: ["ALL"]}
---
apiVersion: monitoring.googleapis.com/v1
kind: PodMonitoring
metadata:
  name: $APP-forecaster
  namespace: $NS
spec:
  selector:
    matchLabels:
      app: $APP-forecaster
  endpoints:
    - port: metrics
      interval: 15s
EOF
```

Optional: the stale- and bad-forecast [alerts](../configuration.md#alerts). Create them as a
`Rules` resource (`apiVersion: monitoring.googleapis.com/v1`), which takes the same `groups`
as [`deploy/forecaster/alerts.yaml`](../../deploy/forecaster/alerts.yaml), and route them with
[managed alerting](https://cloud.google.com/stackdriver/docs/managed-prometheus/rules-managed).

## 6. Attach the ScaledObject

Delete any existing HPA on the Deployment first: two autoscalers would fight over its
replica count.

```sh
kubectl apply -f - <<EOF
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: $APP
  namespace: $NS
spec:
  scaleTargetRef:
    name: $APP
  minReplicaCount: $MIN_REPLICAS
  maxReplicaCount: $MAX_REPLICAS
  triggers:
    # Predictive: the forecast peak of the coming window.
    - type: prometheus
      name: forecast
      metricType: AverageValue
      metadata:
        serverAddress: $PROMETHEUS_URL
        query: max(forecast_rps{job="$APP-forecaster",model="naive",bound="upper"})
        threshold: "$RPS_PER_POD"
    # Reactive: the current request rate. Decides alone when there is no forecast.
    - type: prometheus
      name: arrivals
      metricType: AverageValue
      metadata:
        serverAddress: $PROMETHEUS_URL
        query: '$QUERY'
        threshold: "$RPS_PER_POD"
EOF
```

The HPA that KEDA creates follows whichever trigger asks for more pods.

## 7. Check that it works

```sh
kubectl -n $NS get scaledobject $APP     # READY True
kubectl -n $NS get hpa                   # keda-hpa-$APP, with two metrics
kubectl -n $NS logs deploy/$APP-forecaster
```

In Cloud Monitoring's Metrics Explorer (PromQL), or through the frontend:

- `forecast_history_seconds` grows to 172800 (2 days). Until then no forecast is exported,
  and the service scales on the request rate alone, like a normal autoscaler.
- After that, `forecast_rps{bound="upper"}` runs ahead of your request rate by about
  `LEAD_SECONDS`, and `forecast_coverage_ratio` settles near 0.9.

## 8. Node provisioning

When new pods need a new node (Cluster Autoscaler, node auto-provisioning, or Autopilot,
which adds capacity per pod), the node's start-up adds to your scale-up time. Either include
it in `LEAD_SECONDS`, or keep a little [spare node capacity](../configuration.md#spare-node-capacity)
with low-priority placeholder pods, which Autopilot also supports.

## Option B: Prometheus in the cluster

On GKE Standard you can run kube-prometheus-stack instead of the managed service. Follow
[step 4 of the EKS guide](eks.md#4-install-prometheus) with `storageClassName: standard-rwo`
and without the StorageClass, then use the EKS guide's step 5 (ServiceMonitor instead of
PodMonitoring) and `PROMETHEUS_URL=http://kube-prometheus-stack-prometheus.monitoring.svc:9090`.
On Autopilot, use the managed service: kube-prometheus-stack's node exporter needs host
access that Autopilot does not allow.

## Uninstall

```sh
kubectl -n $NS delete scaledobject $APP       # KEDA removes its HPA; replicas stay as they are
kubectl -n $NS delete deployment,podmonitoring $APP-forecaster
```

KEDA and the frontend can stay for other uses; otherwise `helm uninstall keda -n keda` and
delete the frontend from `monitoring`.
