"""Switch the scaling arm of the web Deployment (Phase 5).

    python3 experiments/arms.py apply r100   # remove the old scaler, reset, wait idle, apply
    python3 experiments/arms.py clear        # remove every scaler of web, reset to 2 replicas
    python3 experiments/arms.py status       # active scaler, replicas, current metric values
    python3 experiments/arms.py check        # the arms differ only where they should

Arms (deploy/arms/): a (HPA on CPU), r100 / r80 / r60 (KEDA on arrivals), c (forecast only),
d (forecast + arrivals). Standard library and kubectl only, so the Phase 6 harness can import
it.

Never two scalers on one Deployment: they would fight over the replica count. apply therefore
removes every HPA and ScaledObject that targets web first, waits until KEDA has deleted the
HPA it owned, scales web back to the minimum and waits until exactly that many pods are
Ready. It then waits until the arrival rate is ~0: the reactive signal is rate(...[1m]), so
load from a previous run lingers for a minute and would make a new scaler scale up at once.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NAMESPACE = "default"
TARGET = "web"
MIN_REPLICAS, MAX_REPLICAS = 2, 7
RPS_PER_POD = 40  # docs/methodology.md, Phase 2
ARRIVALS = 'sum(rate(http_requests_received_total{app="web"}[1m]))'
FORECAST = 'max(forecast_rps{model="naive",bound="upper"})'
PROM_PROXY = ("/api/v1/namespaces/monitoring/services/"
              "kube-prometheus-stack-prometheus:9090/proxy/api/v1/query")


@dataclass(frozen=True)
class Arm:
    file: str
    kind: str  # HorizontalPodAutoscaler or ScaledObject
    name: str
    # ScaledObject arms: (query, threshold) per trigger, in order.
    triggers: tuple = ()

    @property
    def hpa(self):
        """The HPA doing the scaling: the arm itself, or the one KEDA creates for it."""
        return self.name if self.kind == "HorizontalPodAutoscaler" else f"keda-hpa-{self.name}"


ARMS = {
    "a": Arm("deploy/arms/a-hpa-cpu.yaml", "HorizontalPodAutoscaler", "web-a"),
    "r100": Arm("deploy/arms/r100.yaml", "ScaledObject", "web-r100",
                ((ARRIVALS, RPS_PER_POD),)),
    "r80": Arm("deploy/arms/r80.yaml", "ScaledObject", "web-r80",
               ((ARRIVALS, 0.8 * RPS_PER_POD),)),
    "r60": Arm("deploy/arms/r60.yaml", "ScaledObject", "web-r60",
               ((ARRIVALS, 0.6 * RPS_PER_POD),)),
    "c": Arm("deploy/arms/c-forecast.yaml", "ScaledObject", "web-c",
             ((FORECAST, RPS_PER_POD),)),
    "d": Arm("deploy/arms/d-hybrid.yaml", "ScaledObject", "web-d",
             ((FORECAST, RPS_PER_POD), (ARRIVALS, RPS_PER_POD))),
}


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def kubectl(*args, stdin=None):
    r = subprocess.run(["kubectl", *args], input=stdin, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def get(*args):
    return json.loads(kubectl("get", "-n", NAMESPACE, "-o", "json", *args))


def wait(what, ok, timeout, every=2):
    deadline = time.monotonic() + timeout
    while True:
        if ok():
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out after {timeout} s waiting for {what}")
        time.sleep(every)


def scalers():
    """(kind, name) of every ScaledObject and HPA that targets the web Deployment."""
    out = []
    for kind in ("scaledobject", "hpa"):
        for o in get(kind)["items"]:
            if o["spec"]["scaleTargetRef"]["name"] == TARGET:
                out.append((kind, o["metadata"]["name"]))
    return out


def pods():
    return get("pods", "-l", f"app={TARGET}")["items"]


def ready(pod):
    if pod["metadata"].get("deletionTimestamp"):
        return False
    conds = pod.get("status", {}).get("conditions", [])
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conds)


def query(promql):
    """Instant query through the API server's service proxy (no port-forward needed)."""
    body = json.loads(kubectl("get", "--raw", f"{PROM_PROXY}?{urllib.parse.urlencode({'query': promql})}"))
    result = body["data"]["result"]
    return float(result[0]["value"][1]) if result else None


def clear(replicas=MIN_REPLICAS):
    """Remove every scaler of web and leave exactly `replicas` Ready pods."""
    # ScaledObjects first: KEDA then deletes the HPA it owns; anything left is a plain HPA.
    for kind in ("scaledobject", "hpa"):
        for k, name in scalers():
            if k == kind:
                log(f"deleting {kind}/{name}")
                kubectl("delete", "-n", NAMESPACE, kind, name, "--ignore-not-found", "--wait")
    wait("no scaler on web", lambda: not scalers(), 120)
    kubectl("scale", "-n", NAMESPACE, f"deployment/{TARGET}", f"--replicas={replicas}")
    # Terminating pods count too: they may still receive traffic while draining.
    wait(f"exactly {replicas} Ready pods",
         lambda: (lambda ps: len(ps) == replicas and all(map(ready, ps)))(pods()), 300)
    log(f"no scaler, {replicas} pods Ready")


def wait_idle(below=1.0, timeout=600):
    """Wait until the arrival rate the reactive arms read has decayed to ~0."""
    def idle():
        v = query(ARRIVALS)
        return v is None or v < below
    log(f"waiting for arrivals < {below} RPS")
    wait("arrivals to decay", idle, timeout, every=5)


def hpa_active(name):
    try:
        h = get("hpa", name)
    except RuntimeError:
        return False
    conds = {c["type"]: c["status"] for c in h.get("status", {}).get("conditions", [])}
    return conds.get("ScalingActive") == "True"


def apply(arm, idle=True):
    a = ARMS[arm]
    clear()
    if idle:
        wait_idle()
    kubectl("apply", "-n", NAMESPACE, "-f", str(REPO / a.file))
    # ScalingActive: the HPA exists and has fetched its metrics at least once.
    wait(f"hpa/{a.hpa} to be active", lambda: hpa_active(a.hpa), 180)
    log(f"arm {arm} active ({a.kind.lower()}/{a.name}, hpa/{a.hpa})")


def status():
    active = [n for n in scalers()]
    print("scalers:", ", ".join(f"{k}/{n}" for k, n in active) or "none")
    for k, n in active:
        if k != "hpa":
            continue
        h = get("hpa", n)
        st = h.get("status", {})
        print(f"hpa/{n}: replicas {st.get('currentReplicas')} -> {st.get('desiredReplicas')}")
        for m in st.get("currentMetrics") or []:
            print("  ", json.dumps(m))
        for c in st.get("conditions", []):
            print(f"   {c['type']}={c['status']} {c.get('reason', '')}: {c.get('message', '')}")
    print(f"pods ready: {sum(map(ready, pods()))}")
    print(f"arrivals: {query(ARRIVALS)}  forecast: {query(FORECAST)}")


def check():
    """The arms may differ only in their signal and threshold. Parses the manifests with a
    client-side dry run (needs a cluster with the KEDA CRDs, changes nothing)."""
    problems = []
    behaviors = {}
    for arm, a in ARMS.items():
        o = json.loads(kubectl("apply", "--dry-run=client", "-o", "json", "-f", str(REPO / a.file)))
        s = o["spec"]
        if (o["kind"], o["metadata"]["name"]) != (a.kind, a.name):
            problems.append(f"{arm}: is {o['kind']}/{o['metadata']['name']}")
        if s["scaleTargetRef"]["name"] != TARGET:
            problems.append(f"{arm}: targets {s['scaleTargetRef']['name']}")
        if a.kind == "HorizontalPodAutoscaler":
            limits = (s["minReplicas"], s["maxReplicas"])
            behaviors[arm] = s.get("behavior")
        else:
            limits = (s["minReplicaCount"], s["maxReplicaCount"])
            behaviors[arm] = s.get("advanced", {}).get("horizontalPodAutoscalerConfig", {}) \
                .get("behavior")
            got = tuple((t["metadata"]["query"], float(t["metadata"]["threshold"]))
                        for t in s["triggers"])
            if got != a.triggers:
                problems.append(f"{arm}: triggers {got}, expected {a.triggers}")
            if any(t.get("metricType") != "AverageValue" for t in s["triggers"]):
                problems.append(f"{arm}: every trigger must be metricType AverageValue")
            if "pollingInterval" in s:
                problems.append(f"{arm}: pollingInterval is set (only matters for 0<->1)")
        if limits != (MIN_REPLICAS, MAX_REPLICAS):
            problems.append(f"{arm}: min/max {limits}, expected {(MIN_REPLICAS, MAX_REPLICAS)}")
    ref = behaviors["a"]
    for arm, b in behaviors.items():
        if b != ref:
            problems.append(f"{arm}: behavior differs from arm a: {b}")
    if ref is None or ref.get("scaleDown", {}).get("stabilizationWindowSeconds") != 120:
        problems.append("scale-down stabilization must be 120 s in every arm")
    for p in problems:
        print("FAIL", p)
    if not problems:
        print(f"ok: {len(ARMS)} arms, same min/max {MIN_REPLICAS}/{MAX_REPLICAS} and behavior; "
              "triggers as expected")
    return not problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("apply")
    p.add_argument("arm", choices=ARMS)
    p.add_argument("--no-wait-idle", action="store_true",
                   help="apply even while arrivals from earlier load are still decaying")
    sub.add_parser("clear")
    sub.add_parser("status")
    sub.add_parser("check")
    a = ap.parse_args()
    if a.cmd == "apply":
        apply(a.arm, idle=not a.no_wait_idle)
    elif a.cmd == "clear":
        clear()
    elif a.cmd == "status":
        status()
    elif a.cmd == "check":
        sys.exit(0 if check() else 1)


if __name__ == "__main__":
    main()
