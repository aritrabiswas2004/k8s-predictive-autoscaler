"""Cluster-free tests of the Phase 7 numbers (make test-experiments)."""

import csv
import importlib.util
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "analysis"))
import evaluation as ev  # noqa: E402

HERE = Path(__file__).resolve().parent
SMOKE = HERE / "results" / "smoke"


def row(seed, arm, cost, viol, workload="synthetic", delay=30, **kw):
    return {"seed": seed, "arm": arm, "workload": workload, "startup_delay_seconds": delay,
            "replica_seconds": cost, "slo_violation_minutes": viol, **kw}


class StatsTest(unittest.TestCase):
    def test_mean_ci(self):
        s = ev.mean_ci([1, 2, 3, 4, 5])
        self.assertEqual((s.n, s.mean), (5, 3.0))
        self.assertAlmostEqual(s.half, 2.776 * math.sqrt(2.5) / math.sqrt(5), places=6)
        one = ev.mean_ci([7, None])
        self.assertEqual((one.n, one.mean, one.lo), (1, 7.0, None))
        self.assertEqual(ev.mean_ci([]).n, 0)

    def test_t_quantile_falls_back_conservatively(self):
        self.assertEqual(ev.t975(4), 2.776)
        self.assertEqual(ev.t975(12), ev.T975[10])  # larger quantile than the true one

    def test_verdict(self):
        self.assertEqual(ev.verdict(ev.mean_ci([-3, -2, -2.5])), "below")
        self.assertEqual(ev.verdict(ev.mean_ci([3, 2, 2.5])), "above")
        self.assertEqual(ev.verdict(ev.mean_ci([-1, 1, 0.5])), "within noise")
        self.assertEqual(ev.verdict(ev.mean_ci([-1])), "n < 2")


class FrontierTest(unittest.TestCase):
    PTS = [(1000, 6), (1200, 4), (1500, 1)]  # R100, R80, R60

    def test_frontier_at(self):
        self.assertEqual(ev.frontier_at(self.PTS, 900), 6)  # flat below the cheapest
        self.assertEqual(ev.frontier_at(self.PTS, 1100), 5)
        self.assertEqual(ev.frontier_at(self.PTS, 1350), 2.5)
        self.assertEqual(ev.frontier_at(self.PTS, 1500), 1)
        self.assertIsNone(ev.frontier_at(self.PTS, 1600))  # nothing measured there
        self.assertEqual(ev.frontier_at(list(reversed(self.PTS)), 1100), 5)  # order-free

    def test_gaps_are_paired_by_seed(self):
        rows = []
        for seed in (1, 2, 3):
            rows += [row(seed, "r100", 1000, 6), row(seed, "r80", 1200, 4),
                     row(seed, "r60", 1500, 1)]
        rows += [row(1, "d", 1100, 2), row(2, "d", 1350, 2.5), row(3, "d", 1600, 0),
                 row(4, "d", 1100, 0)]  # seed 4 has no reactive runs: not compared
        gaps = ev.frontier_gaps(rows, "synthetic", "d")
        self.assertEqual(gaps, {1: -3, 2: 0, 3: None})
        rows.append(row(1, "d", 1100, 2, workload="wikipedia"))
        self.assertEqual(ev.frontier_gaps(rows, "wikipedia", "d"), {})

    def test_paired_difference(self):
        rows = [row(1, "d", 1100, 2), row(2, "d", 1100, 1), row(1, "r100", 1000, 5),
                row(2, "r100", 1000, 3), row(3, "r100", 1000, 9)]
        s = ev.paired(rows, "synthetic", "d", "r100", "slo_violation_minutes")
        self.assertEqual((s.n, s.values), (2, (-3.0, -2.0)))

    def test_price_is_per_pod_second(self):
        # 0.5 vCPU x $0.000011244 + 1 GB x $0.000001235 = $0.000006857 per pod-second.
        self.assertAlmostEqual(ev.PRICE_POD_SECOND, 0.000006857, places=12)


class ForecastTest(unittest.TestCase):
    def test_spike_minutes(self):
        tr = {"duration_seconds": 1500, "spike": {"start_s": 1260, "end_s": 1340}}
        self.assertEqual(ev.spike_minutes(tr), {21, 22, 23})
        self.assertEqual(ev.spike_minutes({"duration_seconds": 600, "spike": None}), set())

    def test_window_targets(self):
        self.assertEqual(ev.window_targets(1000.0, 60, 60), [1065, 1080, 1095, 1110])
        self.assertEqual(ev.window_targets(990.0, 60, 60), [1050, 1065, 1080, 1095, 1110])

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "needs numpy (the forecaster's)")
    def test_window_targets_match_the_forecaster(self):
        sys.path.insert(0, str(HERE.parent / "forecaster" / "src"))
        from forecaster.window import horizon_steps
        for made in (1000.0, 1003.7, 1014.9, 1015.0):
            for last in (math.floor(made / 15) * 15, math.floor(made / 15) * 15 - 15):
                want = [last + h * 15 for h in horizon_steps(made, last, 15, 60, 60)]
                self.assertEqual(ev.window_targets(made, 60, 60), want, (made, last))

    def test_forecast_scores(self):
        # A trace of 4 cycles of 300 s, 2 of history; flat 100 RPS except 160 at t0+780.
        t0 = 3000.0
        trace = {"history_cycles": 2, "cycle_seconds": 300, "duration_seconds": 1200,
                 "spike": None}
        grid = [t0 + k * 15 for k in range(-4, 85)]
        arrivals = {t: (160.0 if t == t0 + 780 else 100.0) for t in grid}
        made_at, mean, upper = {}, {}, {}
        for made in range(int(t0) + 600, int(t0) + 1080, 30):
            for t in (made + 5, made + 20):  # two scrapes see the same forecast
                made_at[t], mean[t], upper[t] = made, 100.0, 120.0
        series = {
            "arrivals": [{"metric": {}, "values": [[t, v] for t, v in arrivals.items()]}],
            "forecast_made_at": [{"metric": {"model": "naive"},
                                  "values": [[t, v] for t, v in made_at.items()]}],
            "forecast_rps": [
                {"metric": {"model": "naive", "bound": "mean"},
                 "values": [[t, v] for t, v in mean.items()]},
                {"metric": {"model": "naive", "bound": "upper"},
                 "values": [[t, v] for t, v in upper.items()]}],
            "forecast_lead": [{"metric": {}, "values": [[t, 60.0] for t in grid]}],
            "forecast_window": [{"metric": {}, "values": [[t, 60.0] for t in grid]}],
        }
        s = ev.forecast_scores(series, trace, t0)
        self.assertNotIn("holtwinters_scored", s)
        n = s["naive_scored"]["n"]
        # Forecasts made at 600..1050 s; the window [made+60, made+120] must end by 1200 s.
        self.assertEqual(n, len(range(600, 1080, 30)))
        # Windows that include t = 780 s (made 660..720 s) see a peak of 160.
        hit = sum(1 for m in range(600, 1080, 30) if m + 60 <= 780 <= m + 120)
        self.assertEqual(hit, 3)
        self.assertAlmostEqual(s["naive_scored"]["smape"], hit * (2 * 60 / 260) / n)
        self.assertAlmostEqual(s["naive_scored"]["coverage"], (n - hit) / n)
        self.assertAlmostEqual(s["naive_scored"]["short"], hit / n)  # 3 pods < 4


@unittest.skipUnless((SMOKE / "smoke-d30-d" / "run.json").exists(), "smoke results missing")
class SmokeRunTest(unittest.TestCase):
    """The committed smoke grid: Phase 7's row agrees with the harness's summary.csv."""

    def test_rows_match_the_harness_summary(self):
        rows = {r["arm"]: r for r in ev.load_rows([SMOKE])}
        with open(SMOKE / "summary.csv") as f:
            want = {r["arm"]: r for r in csv.DictReader(f)}
        self.assertEqual(set(rows), set(ev.ARMS))
        for arm, r in rows.items():
            for k in ("slo_violation_minutes", "replica_seconds", "scale_events"):
                self.assertAlmostEqual(float(r[k]), float(want[arm][k]), msg=(arm, k))
            self.assertIsNone(r["spike_violation_minutes"])  # the smoke trace has no spike
            self.assertEqual(r["nonspike_violation_minutes"], r["slo_violation_minutes"])
            self.assertGreater(r["fc_naive_scored_n"], 5)
            self.assertGreaterEqual(r["scored_bad_share"], 0)
        self.assertGreater(rows["r100"]["scored_bad_share"], 0)
        # k6 sends a request at a minute boundary in either minute, so a minute's sent count
        # can be 1 below its offered count with nothing dropped: a share of about 1e-4.
        self.assertLess(rows["d"]["scored_bad_share"], 1e-3)


if __name__ == "__main__":
    unittest.main()
