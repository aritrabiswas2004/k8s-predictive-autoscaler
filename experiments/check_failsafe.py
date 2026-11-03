"""What arms C and D do when the forecast disappears (Phase 5 check, before Phase 8's runs).

    python3 experiments/check_failsafe.py c --out experiments/results/arms/failsafe-c.log

Applies the arm (without waiting for idle), stops the forecaster (scale to 0), and logs every
5 s: the forecast and arrival values, the ScaledObject's Fallback condition and the HPA's
metric values and desired replicas. Then starts the forecaster again and logs until the
forecast is back. No load is needed: the point is which signal the HPA follows.

Expected (deploy/arms/): C, with ignoreNullValues "false", turns the empty result into a
scaler error and after failureThreshold (3) errors goes to fallback.replicas (7). D treats the
empty result as 0 and follows its arrivals trigger, with no fallback.
"""

import argparse
import json
import time

import arms

FORECASTER = ["-n", "monitoring", "deployment/forecaster"]


def so_conditions(name):
    o = arms.get("scaledobject", name)
    return {c["type"]: c["status"] for c in o.get("status", {}).get("conditions", [])}


def hpa_state(name):
    st = arms.get("hpa", name).get("status", {})
    metrics = [m["external"]["current"].get("averageValue") or m["external"]["current"].get("value")
               for m in st.get("currentMetrics") or [] if m.get("type") == "External"]
    return st.get("currentReplicas"), st.get("desiredReplicas"), metrics


def sample(arm, t0, out, note=""):
    a = arms.ARMS[arm]
    cur, want, metrics = hpa_state(a.hpa)
    row = {"t": round(time.time() - t0, 1), "forecast": arms.query(arms.FORECAST),
           "arrivals": arms.query(arms.ARRIVALS),
           "fallback": so_conditions(a.name).get("Fallback"), "hpa_metrics": metrics,
           "replicas": cur, "desired": want, "note": note}
    line = json.dumps(row)
    print(line, flush=True)
    out.write(line + "\n")
    out.flush()
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("arm", choices=["c", "d"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--down-seconds", type=int, default=150)
    a = ap.parse_args()
    arms.apply(a.arm, idle=False)
    with open(a.out, "w") as out:
        t0 = time.time()
        for _ in range(3):
            sample(a.arm, t0, out, "forecaster running")
            time.sleep(5)
        arms.kubectl("scale", *FORECASTER, "--replicas=0")
        arms.wait("forecaster gone", lambda: not arms.kubectl(
            "get", "pods", "-n", "monitoring", "-l", "app=forecaster", "-o", "name").strip(), 120)
        sample(a.arm, t0, out, "forecaster stopped")
        end = time.time() + a.down_seconds
        while time.time() < end:
            time.sleep(5)
            sample(a.arm, t0, out, "forecaster stopped")
        arms.kubectl("scale", *FORECASTER, "--replicas=1")
        sample(a.arm, t0, out, "forecaster started")
        end = time.time() + 180
        while time.time() < end:
            time.sleep(5)
            row = sample(a.arm, t0, out, "forecaster started")
            if row["forecast"] is not None and row["fallback"] != "True":
                break
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
