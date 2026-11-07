# Install on Azure Kubernetes Service (AKS)

Set up predictive autoscaling for one Deployment on an AKS cluster: push the forecaster image
to Azure Container Registry, turn on KEDA, install Prometheus, deploy the forecaster next to
your service and attach a ScaledObject. About 30 minutes, plus two days before the first
forecast (the forecaster learns from your traffic first).

1. [Before you start](#1-before-you-start)
2. [Push the forecaster image to ACR](#2-push-the-forecaster-image-to-acr)
3. [Turn on KEDA](#3-turn-on-keda)
4. [Install Prometheus](#4-install-prometheus)
5. [Deploy the forecaster](#5-deploy-the-forecaster)
6. [Attach the ScaledObject](#6-attach-the-scaledobject)
7. [Check that it works](#7-check-that-it-works)
8. [Node provisioning: Cluster Autoscaler, Node Auto Provisioning](#8-node-provisioning)

Using Azure Monitor managed service for Prometheus instead? See [Option B](#option-b-azure-monitor-managed-prometheus).
To remove everything: [Uninstall](#uninstall).

## 1. Before you start

You need:

- An AKS cluster and `kubectl` pointed at it:
  `az aks get-credentials --resource-group <group> --name <cluster>`
- The Azure CLI, `helm`, and Docker with `buildx`.
- A Deployment that exposes a request counter to Prometheus (for example
  `http_requests_total`), and traffic with a daily pattern.
- Your service's scale-up time: how long a new pod takes to serve traffic, including a new
  node if one is needed ([how to measure it](../configuration.md#measuring-your-scale-up-time)).

Set these once; every command below uses them:

```sh
export RG=my-resource-group
export CLUSTER=my-cluster
export ACR=myregistry           # globally unique, letters and digits only
export IMAGE=$ACR.azurecr.io/predictive-forecaster:0.1.0

export NS=shop                  # namespace of your Deployment
export APP=checkout             # name of your Deployment
export QUERY='sum(rate(http_requests_total{namespace="shop",service="checkout"}[1m]))'
export RPS_PER_POD=40           # requests/s one pod should handle
export MIN_REPLICAS=2
export MAX_REPLICAS=20
export LEAD_SECONDS=120         # scale-up time + 30 s, rounded up to a multiple of 60
```

How to choose each value: [configuration](../configuration.md#choosing-the-settings).

## 2. Push the forecaster image to ACR

From a clone of this repository:

```sh
az acr create --resource-group $RG --name $ACR --sku Basic
az acr login --name $ACR

# Both architectures, so it runs on x86 and Arm (Cobalt, Ampere) nodes.
docker buildx build --platform linux/amd64,linux/arm64 -t $IMAGE --push forecaster/

# Let the cluster pull from the registry.
az aks update --resource-group $RG --name $CLUSTER --attach-acr $ACR
```

## 3. Turn on KEDA

AKS ships KEDA as a managed add-on. Skip this if KEDA is already installed, and do not
install it with Helm as well.

```sh
az aks update --resource-group $RG --name $CLUSTER --enable-keda
kubectl get pods -n kube-system -l app.kubernetes.io/name=keda-operator
```

The add-on's KEDA version follows your Kubernetes version. The features used here (the
Prometheus scaler with two triggers) are available in every supported version.

## 4. Install Prometheus

**Option A, recommended:** kube-prometheus-stack in the cluster. It works with the
forecaster as is. For Azure Monitor managed Prometheus, see
[Option B](#option-b-azure-monitor-managed-prometheus) instead.

Prometheus must keep 3 days of history across restarts, so give it a managed disk
(`managed-csi` is AKS's default storage class):

```sh
cat > prometheus-values.yaml <<EOF
prometheus:
  prometheusSpec:
    scrapeInterval: 15s
    retention: 7d
    # Pick up ServiceMonitors and rules from any namespace, not only this release's.
    serviceMonitorSelectorNilUsesHelmValues: false
    ruleSelectorNilUsesHelmValues: false
    storageSpec:
      volumeClaimTemplate:
        spec:
          storageClassName: managed-csi
          accessModes: ["ReadWriteOnce"]
          resources:
            requests:
              storage: 50Gi
EOF

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --version 92.17.0 --namespace monitoring --create-namespace \
  -f prometheus-values.yaml --wait --timeout 10m

export PROMETHEUS_URL=http://kube-prometheus-stack-prometheus.monitoring.svc:9090
```

If your service is not scraped yet, add a ServiceMonitor for it. This assumes its Service has
the label `app: $APP` and a port named `http` that serves `/metrics`:

```sh
kubectl apply -f - <<EOF
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
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

Check that `QUERY` returns one series before going on:

```sh
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090 &
curl -s localhost:9090/api/v1/query --data-urlencode "query=$QUERY"
```

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
apiVersion: v1
kind: Service
metadata:
  name: $APP-forecaster
  namespace: $NS
  labels:
    app: $APP-forecaster
spec:
  selector:
    app: $APP-forecaster
  ports:
    - {name: metrics, port: 8000, targetPort: metrics}
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
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

Optional: the stale- and bad-forecast alerts, created in the `monitoring` namespace:
`kubectl apply -f deploy/forecaster/alerts.yaml` ([what they mean](../configuration.md#alerts)).

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

In Prometheus:

- `forecast_history_seconds` grows to 172800 (2 days). Until then no forecast is exported,
  and the service scales on the request rate alone, like a normal autoscaler.
- After that, `forecast_rps{bound="upper"}` runs ahead of your request rate by about
  `LEAD_SECONDS`, and `forecast_coverage_ratio` settles near 0.9.

## 8. Node provisioning

When new pods need a new node (Cluster Autoscaler or Node Auto Provisioning), the node's
start-up adds to your scale-up time. Either include it in `LEAD_SECONDS`, or keep a little
[spare node capacity](../configuration.md#spare-node-capacity) so pods can start at once.

## Option B: Azure Monitor managed Prometheus

If your metrics go to an Azure Monitor workspace, skip step 4 and change steps 5 and 6 as
follows. Queries need a Microsoft Entra ID token, so both the forecaster and KEDA need an
identity with the **Monitoring Data Reader** role on the workspace, through
[workload identity](https://learn.microsoft.com/azure/aks/workload-identity-overview).

1. **Turn on metrics collection** and scrape your service and the forecaster every 15 s:

   ```sh
   az aks update --resource-group $RG --name $CLUSTER --enable-azure-monitor-metrics
   ```

   Use ServiceMonitors as in steps 4 and 5, but with `apiVersion: azmonitoring.coreos.com/v1`,
   the API group that the Azure Monitor agent reads. Note the workspace's **query endpoint**
   from its overview page in the Azure portal.

2. **The forecaster** does not add Entra ID tokens itself, so run Microsoft's
   [authorization proxy](https://learn.microsoft.com/azure/azure-monitor/containers/prometheus-authorization-proxy)
   as a sidecar, pointed at the query endpoint, and set `PROMETHEUS_URL` to the proxy's local
   address.

3. **KEDA** adds the token itself. Set up workload identity for the KEDA operator, then add a
   `TriggerAuthentication` and reference it from both triggers:

   ```yaml
   apiVersion: keda.sh/v1alpha1
   kind: TriggerAuthentication
   metadata:
     name: azure-monitor
     namespace: shop
   spec:
     podIdentity:
       provider: azure-workload
       identityId: <client-id of the identity>
   ```

   In each trigger, set `serverAddress` to the query endpoint and add
   `authenticationRef: {name: azure-monitor}`. See KEDA's
   [Prometheus scaler](https://keda.sh/docs/2.21/scalers/prometheus/) and
   [Azure workload identity](https://keda.sh/docs/2.21/authentication-providers/azure-ad-workload-identity/) docs.

4. **Alerts:** create the [two alerts](../configuration.md#alerts) as an Azure Monitor
   Prometheus rule group.

## Uninstall

```sh
kubectl -n $NS delete scaledobject $APP       # KEDA removes its HPA; replicas stay as they are
kubectl -n $NS delete deployment,service,servicemonitor $APP-forecaster
```

The KEDA add-on and Prometheus can stay for other uses; otherwise
`az aks update --resource-group $RG --name $CLUSTER --disable-keda` and
`helm uninstall kube-prometheus-stack -n monitoring`.
