"""Cluster-free checks of deploy/arms/ (make test-experiments; CI). The full check, which parses
the manifests through kubectl, is `make arm-check`.

The manifests are compared as text, which is enough for these files: the behavior block must
be byte-identical (after de-indenting) in every arm, and every value checked here sits on a
line of its own.
"""

import re
import textwrap
import unittest

import arms


def text(arm):
    """The manifest without its comment lines."""
    lines = (arms.REPO / arms.ARMS[arm].file).read_text().splitlines()
    return "\n".join(l for l in lines if not l.lstrip().startswith("#")) + "\n"


def behavior(arm):
    """The HPA behavior block, de-indented, wherever it sits in the manifest."""
    lines = text(arm).splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip() == "behavior:")
    indent = len(lines[start]) - len(lines[start].lstrip())
    block = [lines[start]]
    for l in lines[start + 1:]:
        if l.strip() and len(l) - len(l.lstrip()) <= indent:
            break
        block.append(l)
    return textwrap.dedent("\n".join(block))


class ArmsTest(unittest.TestCase):
    def test_same_behavior_everywhere(self):
        ref = behavior("a")
        self.assertIn("stabilizationWindowSeconds: 120", ref)
        for arm in arms.ARMS:
            self.assertEqual(behavior(arm), ref, arm)

    def test_same_bounds_everywhere(self):
        for arm, a in arms.ARMS.items():
            t = text(arm)
            if a.kind == "HorizontalPodAutoscaler":
                lo, hi = r"minReplicas: (\d+)", r"maxReplicas: (\d+)"
            else:
                lo, hi = r"minReplicaCount: (\d+)", r"maxReplicaCount: (\d+)"
            self.assertEqual(re.findall(lo, t), [str(arms.MIN_REPLICAS)], arm)
            self.assertEqual(re.findall(hi, t), [str(arms.MAX_REPLICAS)], arm)

    def test_triggers(self):
        for arm, a in arms.ARMS.items():
            if a.kind != "ScaledObject":
                continue
            t = text(arm)
            queries = re.findall(r"^\s+query: (.+)$", t, re.M)
            thresholds = [float(x) for x in re.findall(r'^\s+threshold: "([\d.]+)"$', t, re.M)]
            self.assertEqual(list(zip(queries, thresholds)), list(a.triggers), arm)
            self.assertEqual(t.count("metricType: AverageValue"), len(a.triggers), arm)
            self.assertNotIn("pollingInterval:", t, arm)
            self.assertIn(f"name: {a.name}\n", t, arm)

    def test_forecast_arms_fail_safe(self):
        # C has nothing else to scale on, so a missing forecast must be an error (-> fallback);
        # D must treat it as 0 and keep scaling on arrivals.
        self.assertIn('ignoreNullValues: "false"', text("c"))
        self.assertNotIn("ignoreNullValues", text("d"))
        for arm in ("c", "d"):
            self.assertIn(f"replicas: {arms.MAX_REPLICAS}", text(arm), arm)
        for arm in ("r100", "r80", "r60"):
            self.assertNotIn("fallback:", text(arm), arm)


if __name__ == "__main__":
    unittest.main()
