#!/usr/bin/env bash
# Create the kind cluster and pin its node containers to disjoint CPUs.
#
# CPU split (Docker VM with N CPUs):
#   app node:              the last APP_CPUS CPUs, used only by the web app
#   control-plane + tools: everything else (Prometheus, Grafana, KEDA, forecaster, k6)
# Isolation is about keeping load generation and monitoring off the app's CPUs;
# the control plane is idle enough to share with tools.
set -euo pipefail

CLUSTER=pa
APP_CPUS=${APP_CPUS:-4}
MIN_CPUS=8
DIR=$(cd "$(dirname "$0")" && pwd)

ncpu=$(docker info --format '{{.NCPU}}')
if (( ncpu < MIN_CPUS )); then
  echo "Docker has $ncpu CPUs; need at least $MIN_CPUS. Raise it in Docker Desktop > Settings > Resources." >&2
  exit 1
fi

app_cpus="$((ncpu - APP_CPUS))-$((ncpu - 1))"
shared_cpus="0-$((ncpu - APP_CPUS - 1))"

if kind get clusters | grep -qx "$CLUSTER"; then
  echo "Cluster '$CLUSTER' already exists, skipping create."
else
  sed -e "s/\${APP_RESERVED}/$((ncpu - APP_CPUS))/" \
      -e "s/\${TOOLS_RESERVED}/$APP_CPUS/" \
      "$DIR/cluster.yaml.tmpl" | kind create cluster --config=- --wait 120s
fi

# Pinning is not persisted by kind, so re-apply it on every `make up`.
docker update --cpuset-cpus="$app_cpus" "$CLUSTER-worker" >/dev/null
docker update --cpuset-cpus="$shared_cpus" "$CLUSTER-worker2" "$CLUSTER-control-plane" >/dev/null

for node in "$CLUSTER-control-plane" "$CLUSTER-worker" "$CLUSTER-worker2"; do
  printf '%-20s cpuset=%s\n' "$node" "$(docker inspect --format '{{.HostConfig.CpusetCpus}}' "$node")"
done
