#!/usr/bin/env bash
# Measure end-to-end scale-up lag for one load step, with a reactive KEDA ScaledObject on
# arrivals (the R100 configuration).
#
#   loadtest/measure-lag.sh <out-prefix> <rps-per-pod> <low-rps> <high-rps>
#
# Resets web to 1 replica, applies deploy/calibration/scaledobject-lag.yaml with the given
# threshold, runs loadtest/k6/step.js, then writes the lag breakdown to <out-prefix>.lag.json.
set -euo pipefail

out=$1; threshold=$2; low=$3; high=$4
here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")

cleanup() {
  kubectl delete scaledobject web-lag --ignore-not-found >/dev/null
  kubectl scale deployment web --replicas=1 >/dev/null
}
trap cleanup EXIT

cleanup
kubectl rollout status deployment/web --timeout=120s >/dev/null
# Wait until exactly one pod is left (terminating ones included) and it is Ready.
until [[ $(kubectl get pods -l app=web --no-headers | wc -l) -eq 1 ]]; do sleep 2; done
kubectl wait --for=condition=Ready pod -l app=web --timeout=120s >/dev/null
pods_before=$(kubectl get pods -l app=web -o jsonpath='{.items[*].metadata.name}' | tr ' ' ',')

# The scaler reads rate(...[1m]), so load from a previous run lingers for a minute. Creating
# the ScaledObject before it has gone makes the HPA scale up before the step (seen in the
# first attempt). Wait until the arrival rate is back to ~0.
prom=/api/v1/namespaces/monitoring/services/kube-prometheus-stack-prometheus:9090/proxy/api/v1/query
arrivals() {
  kubectl get --raw "$prom?query=sum(rate(http_requests_received_total%7Bapp%3D%22web%22%7D%5B1m%5D))" \
    | python3 -c 'import json,sys; r=json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else 0)'
}
until python3 -c "import sys; sys.exit(float('$(arrivals)') >= 1)"; do sleep 5; done

sed "s/__THRESHOLD__/$threshold/" "$root/deploy/calibration/scaledobject-lag.yaml" | kubectl apply -f - >/dev/null
until kubectl get hpa keda-hpa-web-lag >/dev/null 2>&1; do sleep 2; done
# Let the HPA settle on the low load before the step.
"$here/run-k6.sh" "$here/k6/step.js" "$out" \
  LOW_RPS="$low" HIGH_RPS="$high" LOW_SECONDS=90 HIGH_SECONDS=150

step_at=$(grep -o 'STEP_AT=[0-9]*' "$out.log" | head -1 | cut -d= -f2)
python3 "$root/experiments/analysis/lag.py" --step-at "$step_at" --threshold "$threshold" \
  --replicas-before 1 --pods-before "$pods_before" --hpa keda-hpa-web-lag --out "$out.lag.json"
kubectl get hpa keda-hpa-web-lag -o jsonpath='{.status.currentReplicas} replicas at end{"\n"}'
