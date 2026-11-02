#!/usr/bin/env bash
# Run a k6 script as a pod on the tools node and save its summary.
#
#   loadtest/run-k6.sh <script.js> <out-prefix> [KEY=VALUE ...]
#
# Writes <out-prefix>.json (the script's handleSummary output), <out-prefix>.log (full pod
# log) and <out-prefix>.docker-stats.csv (CPU of the kind node containers, sampled every few
# seconds: the tools node must stay well below saturation, see HANDOVER pitfalls).
# KEY=VALUE pairs are passed to the script as k6 -e variables. K6_FILES="name=path ..." mounts
# extra files next to the script in /scripts (e.g. K6_FILES=trace.json=loadtest/trace/out/x.json).
set -euo pipefail

script=$1; out=$2; shift 2
ns=loadtest
# Pinned by digest; kind load fails for this multi-arch image, so the node pulls it.
image=${K6_IMAGE:-grafana/k6:2.3.0@sha256:2dd4756ec6e14e62778916e1eb850108e4b499304c02df56760fa0bdb0fba0b8}
prom_rw=http://kube-prometheus-stack-prometheus.monitoring.svc:9090/api/v1/write

mkdir -p "$(dirname "$out")"
kubectl get ns "$ns" >/dev/null 2>&1 || kubectl create ns "$ns"
files=(--from-file=script.js="$script")
for f in ${K6_FILES:-}; do files+=(--from-file="$f"); done
kubectl -n "$ns" create configmap k6-script "${files[@]}" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$ns" delete pod k6 --ignore-not-found --wait >/dev/null

args='"run", "--quiet", "--no-usage-report", "-o", "experimental-prometheus-rw"'
for kv in "$@"; do args+=", \"-e\", \"$kv\""; done
args+=', "/scripts/script.js"'

kubectl -n "$ns" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: k6
  labels: {app: k6}
spec:
  restartPolicy: Never
  nodeSelector: {role: tools}
  # Without connection reuse every request leaves a socket in TIME_WAIT for 60 s, all to the
  # same Service IP:port. The default port range (28k) would cap that at ~470 new connections
  # per second; the full range (64k) at ~1000. ip_local_port_range is a namespaced "safe" sysctl.
  securityContext:
    sysctls:
      - {name: net.ipv4.ip_local_port_range, value: "1024 65535"}
  containers:
    - name: k6
      image: $image
      imagePullPolicy: IfNotPresent
      args: [$args]
      env:
        - {name: K6_PROMETHEUS_RW_SERVER_URL, value: "$prom_rw"}
        - {name: K6_PROMETHEUS_RW_TREND_STATS, value: "p(95),p(99)"}
        - {name: K6_PROMETHEUS_RW_PUSH_INTERVAL, value: "5s"}
      resources:
        requests: {cpu: 500m, memory: 256Mi}
        limits: {cpu: "2", memory: 1Gi}
      volumeMounts:
        - {name: script, mountPath: /scripts}
  volumes:
    - name: script
      configMap: {name: k6-script}
EOF

# Sample node-container CPU until k6 finishes.
stats="$out.docker-stats.csv"
echo "time,container,cpu_percent" > "$stats"
(
  while true; do
    docker stats --no-stream --format '{{.Name}},{{.CPUPerc}}' pa-worker pa-worker2 pa-control-plane \
      | sed "s/^/$(date +%s),/; s/%$//" >> "$stats"
  done
) &
sampler=$!
trap 'kill $sampler 2>/dev/null || true' EXIT

echo "k6 started at $(date -u +%FT%TZ): $script $*"
while true; do
  phase=$(kubectl -n "$ns" get pod k6 -o jsonpath='{.status.phase}')
  [[ $phase == Succeeded || $phase == Failed ]] && break
  sleep 5
done
echo "k6 finished at $(date -u +%FT%TZ) with phase $phase"

# Overload runs log thousands of identical "Request Failed" lines; keep 20 and a count.
kubectl -n "$ns" logs k6 | awk '
  /msg="Request Failed"/ { n++; if (n > 20) next }
  { print }
  END { if (n > 20) printf "[run-k6.sh: %d more \"Request Failed\" lines omitted, %d in total]\n", n - 20, n }
' > "$out.log"
sed -n '/===SUMMARY-BEGIN===/,/===SUMMARY-END===/p' "$out.log" | sed '1d;$d' > "$out.json"
[[ -s $out.json ]] || { echo "no summary in $out.log" >&2; exit 1; }
echo "summary: $out.json"
