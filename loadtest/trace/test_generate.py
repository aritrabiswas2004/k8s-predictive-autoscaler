"""Tests for generate.py: python3 -m unittest discover -s loadtest/trace (make test-trace)."""

import json
import random
import unittest
from pathlib import Path

import generate as g


def make(*args):
    a = g.parse(list(args))
    return a, g.generate(a)


class TraceTest(unittest.TestCase):
    def test_same_seed_same_trace(self):
        for w in ("synthetic", "wikipedia"):
            _, t1 = make(w, "--seed", "3")
            _, t2 = make(w, "--seed", "3")
            self.assertEqual(t1, t2, w)

    def test_different_seeds_differ(self):
        _, t1 = make("synthetic", "--seed", "1")
        _, t2 = make("synthetic", "--seed", "2")
        self.assertNotEqual(t1["rps"], t2["rps"])
        self.assertNotEqual(t1["cycles"], t2["cycles"])

    def test_length_and_step(self):
        a, t = make("synthetic")
        self.assertEqual(len(t["rps"]), a.cycles * a.cycle_seconds // g.STEP + 1)
        self.assertEqual(t["duration_seconds"], 1500)

    def test_bound_holds_outside_spike(self):
        for w in ("synthetic", "wikipedia"):
            for seed in range(1, 21):
                _, t = make(w, "--seed", str(seed))
                self.assertLessEqual(t["stats"]["max_rps_outside_spike"], 168, (w, seed))

    def test_bound_violation_fails(self):
        with self.assertRaises(SystemExit):
            make("synthetic", "--peak", "200")

    def test_spike_in_last_cycle_only(self):
        for seed in range(1, 21):
            a, t = make("synthetic", "--seed", str(seed))
            s = t["spike"]
            last = (a.cycles - 1) * a.cycle_seconds
            self.assertGreaterEqual(s["start_s"], last)
            self.assertLessEqual(s["end_s"], last + a.cycle_seconds)

    def test_spike_same_position_in_both_workloads(self):
        _, syn = make("synthetic", "--seed", "4")
        _, wiki = make("wikipedia", "--seed", "4")
        self.assertEqual(syn["spike"], wiki["spike"])

    def test_no_spike(self):
        a, t = make("synthetic", "--spike-rps", "0")
        self.assertIsNone(t["spike"])
        self.assertEqual(t["stats"]["max_rps"], t["stats"]["max_rps_outside_spike"])

    def test_cycle_boundaries_at_baseline(self):
        # The timing shift must never push load across a cycle boundary: near every boundary
        # the noise-free level is the (trended) baseline.
        a = g.parse(["synthetic"])
        for seed in range(1, 21):
            level, _ = g.synthetic_level(a, random.Random(f"{seed}:cycles"))
            for c in range(1, a.cycles):
                t = c * a.cycle_seconds
                trend = 1 + a.trend * t / (a.cycles * a.cycle_seconds)
                self.assertAlmostEqual(level(t), a.base * trend, places=6)

    def test_amplitude_varies_within_bounds(self):
        _, t = make("synthetic", "--seed", "5")
        amps = [c["amplitude"] for c in t["cycles"]]
        self.assertTrue(all(0.8 <= x <= 1.2 for x in amps))
        self.assertGreater(len(set(amps)), 1)

    def test_wikipedia_maps_window_to_base_and_peak(self):
        a, t = make("wikipedia", "--noise", "0", "--spike-rps", "0")
        self.assertAlmostEqual(min(t["rps"]), a.base, delta=1.5)
        self.assertAlmostEqual(max(t["rps"]), a.peak, delta=1.5)
        self.assertEqual([c["date"] for c in t["cycles"]],
                         ["20250303", "20250304", "20250305", "20250306", "20250307"])

    def test_no_machine_specific_paths(self):
        # Traces are committed and compared in CI, so nothing may depend on where the repo is.
        for w in ("synthetic", "wikipedia"):
            _, t = make(w)
            text = json.dumps(t)
            self.assertNotIn(str(g.REPO), text, w)
            self.assertNotIn(str(Path.home()), text, w)

    def test_committed_traces_are_current(self):
        # loadtest/trace/out/ must match what the generator produces now.
        out = Path(__file__).parent / "out"
        for f in sorted(out.glob("*-s*.json")):
            workload, seed = f.stem.rsplit("-s", 1)
            _, t = make(workload, "--seed", seed, "--out", str(f))
            self.assertEqual(json.loads(f.read_text()), t, f.name)

    def test_committed_smoke_trace_is_current(self):
        # Same arguments as SMOKE_ARGS in the Makefile (make traces).
        f = Path(__file__).parent / "out" / "smoke.json"
        _, t = make("synthetic", "--seed", "1", "--cycles", "2", "--history-cycles", "1",
                    "--spike-rps", "0", "--out", str(f))
        self.assertEqual(json.loads(f.read_text()), t)


if __name__ == "__main__":
    unittest.main()
