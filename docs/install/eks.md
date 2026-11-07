# Install on Amazon EKS

Set up predictive autoscaling for one Deployment on an EKS cluster: push the forecaster image
to ECR, install KEDA and Prometheus, deploy the forecaster next to your service and attach a
ScaledObject. About 30 minutes, plus two days before the first forecast (the forecaster
learns from your traffic first).

1. [Before you start](#1-before-you-start)
2. [Push the forecaster image to ECR](#2-push-the-forecaster-image-to-ecr)
3. [Install KEDA](#3-install-keda)
4. [Install Prometheus](#4-install-prometheus)
5. [Deploy the forecaster](#5-deploy-the-forecaster)
6. [Attach the ScaledObject](#6-attach-the-scaledobject)
7. [Check that it works](#7-check-that-it-works)
8. [Node provisioning: Karpenter, Cluster Autoscaler, Auto Mode](#8-node-provisioning)

Using Amazon Managed Service for Prometheus instead? See [Option B](#option-b-amazon-managed-service-for-prometheus).
To remove everything: [Uninstall](#uninstall).

## 1. Before you start

You need:

- An EKS cluster and `kubectl` pointed at it:
  `aws eks update-kubeconfig --region <region> --name <cluster>`
- The AWS CLI, `helm`, and Docker with `buildx`.
- A Deployment that exposes a request counter to Prometheus (for example
  `http_requests_total`), and traffic with a daily pattern.
- Your service's scale-up time: how long a new pod takes to serve traffic, including a new
  node if one is needed ([how to measure it](../configuration.md#measuring-your-scale-up-time)).

Set these once; every command below uses them:

```sh
export AWS_REGION=eu-central-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export IMAGE=$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/predictive-forecaster:0.1.0

export NS=shop                  # namespace of your Deployment
export APP=checkout             # name of your Deployment
export QUERY='sum(rate(http_requests_total{namespace="shop",service="checkout"}[1m]))'
export RPS_PER_POD=40           # requests/s one pod should handle
export MIN_REPLICAS=2
export MAX_REPLICAS=20
export LEAD_SECONDS=120         # scale-up time + 30 s, rounded up to a multiple of 60
```

How to choose each value: [configuration](../configuration.md#choosing-the-settings).

## 2. Push the forecaster image to ECR

From a clone of this repository:

```sh
aws ecr create-repository --repository-name predictive-forecaster --region $AWS_REGION
aws ecr get-login-password --region $AWS_REGION \
  | docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com

# Both architectures, so it runs on x86 and Graviton nodes.
docker buildx build --platform linux/amd64,linux/arm64 -t $IMAGE --push forecaster/
```

Nodes pull from ECR in the same account with the standard node IAM role
(`AmazonEC2ContainerRegistryReadOnly`).

## 3. Install KEDA

Skip this if KEDA is already installed.

```sh
helm repo add kedacore https://kedacore.github.io/charts
helm upgrade --install keda kedacore/keda --version 2.21.0 \
  --namespace keda --create-namespace --wait
```

## 4. Install Prometheus

**Option A, recommended:** kube-prometheus-stack in the cluster. It works with the
forecaster as is. For Amazon Managed Service for Prometheus, see
[Option B](#option-b-amazon-managed-service-for-prometheus) instead.

Prometheus must keep 3 days of history across restarts, so give it an EBS volume. This needs
the [Amazon EBS CSI driver](https://docs.aws.amazon.com/eks/latest/userguide/ebs-csi.html)
(built in on EKS Auto Mode). If your cluster has no gp3 storage class, create one:

```sh
kubectl apply -f - <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
provisioner: ebs.csi.aws.com      # EKS Auto Mode: ebs.csi.eks.amazonaws.com
parameters:
  type: gp3
volumeBindingMode: WaitForFirstConsumer
EOF
```

Then install Prometheus:

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
          storageClassName: gp3
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

When new pods need a new node (Karpenter, Cluster Autoscaler or EKS Auto Mode), the node's
start-up adds to your scale-up time. Either include it in `LEAD_SECONDS`, or keep a little
[spare node capacity](../configuration.md#spare-node-capacity) so pods can start at once.

## Option B: Amazon Managed Service for Prometheus

If your metrics go to an Amazon Managed Service for Prometheus (AMP) workspace, skip step 4
and change steps 5 and 6 as follows. Both the forecaster and KEDA need IAM permission to
query the workspace.

1. **Get your metrics into AMP** with a 15 s scrape interval, including the forecaster's
   `/metrics` endpoint, using the AMP managed collector or a Prometheus/ADOT agent with
   remote write. See [ingesting metrics](https://docs.aws.amazon.com/prometheus/latest/userguide/AMP-onboard-ingest-metrics.html).

2. **The forecaster** does not sign requests itself, so run AWS's SigV4 proxy as a sidecar
   and point the forecaster at it. Add a service account with the AWS managed policy
   `AmazonPrometheusQueryAccess`, through
   [EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html)
   or IRSA, and add this container to the Deployment in step 5:

   ```yaml
   - name: sigv4-proxy
     image: public.ecr.aws/aws-observability/aws-sigv4-proxy:<tag>
     args: ["--name", "aps", "--region", "<region>",
            "--host", "aps-workspaces.<region>.amazonaws.com", "--port", ":8005"]
   ```

   Set `PROMETHEUS_URL` to `http://localhost:8005/workspaces/<workspace-id>`.

3. **KEDA** signs requests itself. Give the `keda-operator` service account the same
   policy, then add a `TriggerAuthentication` and reference it from both triggers:

   ```yaml
   apiVersion: keda.sh/v1alpha1
   kind: TriggerAuthentication
   metadata:
     name: amp
     namespace: shop
   spec:
     podIdentity:
       provider: aws
   ```

   In each trigger's `metadata`, set
   `serverAddress: https://aps-workspaces.<region>.amazonaws.com/workspaces/<workspace-id>`
   and `awsRegion: <region>`, and add `authenticationRef: {name: amp}` to the trigger. See
   KEDA's [Prometheus scaler](https://keda.sh/docs/2.21/scalers/prometheus/) and
   [AWS authentication](https://keda.sh/docs/2.21/authentication-providers/aws/) docs.

4. **Alerts:** create the [two alerts](../configuration.md#alerts) as AMP alerting rules.

## Uninstall

```sh
kubectl -n $NS delete scaledobject $APP       # KEDA removes its HPA; replicas stay as they are
kubectl -n $NS delete deployment,service,servicemonitor $APP-forecaster
```

KEDA and Prometheus can stay for other uses; otherwise `helm uninstall` them.
