"""Cluster-free tests of the harness: the grids and the summary metrics (make test-experiments)."""

import collections
import math
import time
import types
import unittest

import metrics
import run_experiment as rx


def trace(rps=40.0, history=1, cycles=2, cycle=300, step=5):
    n = cycles * cycle // step + 1
    return {"rps": [rps] * n, "step_seconds": step, "history_cycles": history,
            "cycle_seconds": cycle, "duration_seconds": cycles * cycle}


def minute(m, offered=2400, requests=2400, p95=20.0, failed=0.0):
    return {"minute": m, "offered": offered, "requests": requests, "p95_ms": p95,
            "failed_rate": failed}


def series(t0, name_values):
    """{name: [{"values": [[t0 + t, v], ...]}]} from {name: [(t, v), ...]}."""
    return {k: [{"metric": {}, "values": [[t0 + t, v] for t, v in vs]}]
            for k, vs in name_values.items()}


class GridTest(unittest.TestCase):
    def test_main_grid(self):
        runs = rx.grid_runs("main")
        self.assertEqual(len(runs), 6 * 2 * 5)
        self.assertEqual(len({r.run_id for r in runs}), len(runs))
        by_trace = collections.defaultdict(list)
        for r in runs:
            by_trace[r.trace].append(r.arm)
            self.assertEqual(r.delay, 30)
            self.assertTrue(r.trace.endswith(f"-s{r.rep}"))
        for t, arms in by_trace.items():
            self.assertEqual(sorted(arms), sorted(rx.ARMS), t)  # every arm, same trace
        # The order rotates: no arm is first in two repetitions of the same workload.
        firsts = [by_trace[f"synthetic-s{rep}"][0] for rep in range(1, 6)]
        self.assertEqual(len(set(firsts)), 5)

    def test_sweep_grid(self):
        runs = rx.grid_runs("sweep")
        self.assertEqual(len(runs), 3 * 2 * 3)
        self.assertEqual({r.delay for r in runs}, {0, 60})
        self.assertEqual({r.arm for r in runs}, {"r100", "r80", "d"})
        # A delay change needs a rollout. Runs with the same delay are consecutive, and the
        # delay order alternates per rep (0,60 | 60,0 | 0,60), so there are only 3 changes.
        changes = sum(a.delay != b.delay for a, b in zip(runs, runs[1:]))
        self.assertEqual(changes, 3)

    def test_fault_grid(self):
        runs = rx.grid_runs("fault")
        self.assertEqual(len(runs), 2 * 2 * 3)
        self.assertEqual({r.arm for r in runs}, {"c", "d"})
        self.assertEqual({r.delay for r in runs}, {30})
        self.assertEqual({r.fault for r in runs}, {"kill"})
        self.assertTrue(all(r.run_id.endswith("-kill") for r in runs))
        # Every fault run has a no-fault twin in the main grid: same trace, arm and delay.
        twins = {r.run_id for r in rx.grid_runs("main")}
        for r in runs:
            self.assertIn(r.run_id.removesuffix("-kill"), twins)
        self.assertEqual(rx.grid_dir("fault").name, "fault")
        self.assertEqual(rx.grid_runs("main")[0].fault, "")  # old run ids are unchanged

    def test_traces_exist(self):
        for g in ("main", "sweep", "smoke", "fault"):
            for r in rx.grid_runs(g):
                self.assertTrue((rx.TRACES / f"{r.trace}.json").exists(), r.trace)

    def test_jitter_is_deterministic_and_bounded(self):
        r = rx.grid_runs("smoke")[0]
        self.assertEqual(rx.jitter(r), rx.jitter(r))
        self.assertTrue(all(0 <= rx.jitter(x) < 15 for x in rx.grid_runs("main")))


class FaultTest(unittest.TestCase):
    """ForecasterKill against a fake cluster: kubectl, the k6 log and Prometheus are stubs."""

    def setUp(self):
        self.calls, self.replicas, self.made = [], 1, 0.0
        self.saved = rx.subprocess.run, rx.arms.kubectl, rx.arms.query, rx.log
        self.t0 = time.time() + 0.3  # the trace starts 0.3 s from now

        def run(cmd, **kw):  # kubectl logs k6
            return types.SimpleNamespace(stdout=f"x\nTRACE_START={int(self.t0 * 1000)}\n")

        def kubectl(*args, stdin=None):
            self.calls.append((round(time.time() - self.t0, 1), args[0], args[-1]))
            if args[0] == "scale":
                self.replicas = int(args[-1].split("=")[1])
            if args[0] == "rollout":
                self.made = time.time() + 0.05  # the restarted forecaster forecasts at once
            if args[0] == "get":
                return "pod/forecaster-1" if self.replicas else ""
            return ""

        rx.subprocess.run, rx.arms.kubectl = run, kubectl
        rx.arms.query = lambda q: self.made or None
        rx.log = lambda msg: None
        rx.ForecasterKill.poll_every = 0.05

    def tearDown(self):
        rx.subprocess.run, rx.arms.kubectl, rx.arms.query, rx.log = self.saved
        rx.ForecasterKill.poll_every = 2.0

    def test_kill_and_restart(self):
        f = rx.ForecasterKill(at=0.2, down=0.3)
        f.start()
        f.join(timeout=15)
        self.assertFalse(f.is_alive())
        self.assertIsNone(f.error)
        e = {k: v - self.t0 for k, v in f.events.items()}
        self.assertEqual(list(e), ["kill", "gone", "restart", "ready", "back"])
        self.assertAlmostEqual(e["kill"], 0.2, delta=0.15)
        self.assertAlmostEqual(e["restart"] - e["kill"], 0.3, delta=0.15)
        scales = [c[2] for c in self.calls if c[1] == "scale"]
        self.assertEqual(scales, ["--replicas=0", "--replicas=1"])

    def test_old_trace_start_is_ignored(self):
        self.t0 = time.time() - 3600  # the previous run's k6 pod
        f = rx.ForecasterKill(at=0.0, down=0.1)
        f.start()
        time.sleep(0.3)
        f.stop.set()
        f.join(timeout=5)
        self.assertEqual(f.events, {})
        self.assertFalse(any(c[1] == "scale" for c in self.calls))


class MetricsTest(unittest.TestCase):
    def test_minute_slo(self):
        self.assertEqual(metrics.minute_slo(minute(0))[0], False)
        self.assertEqual(metrics.minute_slo(minute(0, p95=301))[0], True)
        # 6 % failed: violated even though the p95 of what came back is fine.
        self.assertEqual(metrics.minute_slo(minute(0, failed=0.06))[0], True)
        # 4 % failed + 2 % never sent: 6 % bad.
        self.assertEqual(metrics.minute_slo(minute(0, requests=2352, failed=0.04))[0], True)
        self.assertEqual(metrics.minute_slo(minute(0, requests=0, p95=None))[0], True)
        self.assertEqual(metrics.minute_slo(minute(0, offered=0, requests=0, p95=None))[0],
                         False)

    def test_slo_minutes_counts_scored_only(self):
        tr = trace()  # history 0-300 s, scored 300-600 s = minutes 5-9
        k6 = {"minutes": [minute(m, p95=900 if m in (1, 6, 9) else 20) for m in range(10)]}
        self.assertEqual(metrics.slo_minutes(k6, tr), (2, 5, [6, 9]))

    def test_offered_at_interpolates(self):
        tr = trace()
        tr["rps"][1] = 50.0
        self.assertAlmostEqual(metrics.offered_at(tr, 2.5), 45.0)
        self.assertEqual(metrics.offered_at(tr, 10_000), 40.0)

    def test_series_metrics(self):
        t0 = 1000.0
        tr = trace(rps=100.0)  # needs ceil(100/40) = 3 pods; scored 300-600 s, 20 samples
        ts = range(0, 600, 15)
        s = series(t0, {
            "replicas": [(t, 4.0 if t >= 450 else 2.0) for t in ts],
            "replicas_ready": [(t, 4.0 if t >= 465 else 2.0) for t in ts],
            "replicas_spec": [(t, 4.0 if t >= 450 else 2.0) for t in ts],
            "cpu_used": [(t, 0.5) for t in ts],
        })
        m = metrics.series_metrics(s, tr, t0)
        self.assertEqual(m["samples"], 20)
        self.assertEqual(m["replica_seconds"], (10 * 2 + 10 * 4) * 15)
        self.assertEqual(m["cpu_core_seconds_requested"], m["replica_seconds"] * 0.5)
        self.assertEqual(m["scale_events"], 1)
        self.assertEqual(m["underprovisioned_seconds"], 11 * 15)  # ready 2 < 3 until 465 s
        self.assertAlmostEqual(m["overprovisioning_mean"], (11 * -1 + 9 * 1) / 20)
        self.assertAlmostEqual(m["cpu_utilization_mean"], 20 * 0.5 / ((10 * 2 + 10 * 4) * 0.5))
        self.assertEqual((m["replicas_min"], m["replicas_max"]), (2.0, 4.0))

    def test_missing_samples_are_not_invented(self):
        t0 = 0.0
        s = series(t0, {"replicas": [(300, 2.0), (315, math.nan)], "replicas_ready": [],
                        "replicas_spec": [], "cpu_used": []})
        m = metrics.series_metrics(s, trace(), t0)
        self.assertEqual(m["samples"], 1)
        self.assertIsNone(m["cpu_utilization_mean"])
        self.assertIsNone(m["overprovisioning_mean"])

    def test_summarize_row(self):
        tr = trace()
        t0 = 1000.0
        k6 = {"minutes": [minute(m) for m in range(10)], "offered": 24000, "requests": 24000,
              "dropped": 0, "failed_rate": 0.0, "vus_max": 200,
              "scored": {"p95_ms": 20.0, "p99_ms": 30.0, "failed_rate": 0.0},
              "spike": {"requests": 0, "failed_rate": 0, "p95_ms": 0, "p99_ms": 0}}
        meta = {"run_id": "x", "grid": "smoke", "arm": "d", "workload": "synthetic",
                "trace": "smoke", "seed": 1, "startup_delay_seconds": 30, "lead_seconds": 60,
                "rep": 1, "order": 0, "git_sha": "abc", "git_dirty": False,
                "started_at": "s", "finished_at": "f", "trace_start": t0}
        s = series(t0, {"replicas": [(t, 2.0) for t in range(0, 600, 15)]})
        row = metrics.summarize(meta, k6, s, tr)
        self.assertEqual(row["slo_violation_minutes"], 0)
        self.assertEqual(row["scored_minutes"], 5)
        self.assertIsNone(row["spike_p95_ms"])  # no spike in the trace: no data, not 0 ms
        self.assertEqual(row["replica_seconds"], 20 * 2 * 15)


if __name__ == "__main__":
    unittest.main()
