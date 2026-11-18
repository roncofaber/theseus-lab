"""Runs last in discovery order and fails when any test that ran before it took longer than the per-test budget:
THESEUS_TEST_BUDGET seconds, default 3, doubled on CI runners. Tests use fakes and fake clocks, never real waits."""
import os
import unittest


class TimeBudgetTests(unittest.TestCase):
    def run(self, result=None):
        self.durations = list(getattr(result, "collectedDurations", None) or [])
        return super().run(result)

    def test_no_single_test_exceeds_the_time_budget(self):
        if not hasattr(unittest.TestResult, "collectedDurations"):
            self.skipTest("per-test durations need Python 3.12")
        budget = float(os.environ.get("THESEUS_TEST_BUDGET") or 3.0) * (2 if os.environ.get("CI") else 1)
        slow = [f"{name} {elapsed:.1f}s" for name, elapsed in self.durations if elapsed > budget]
        self.assertEqual([], slow, f"tests over the {budget:g}s budget; use fakes and fake clocks, not real waits")
