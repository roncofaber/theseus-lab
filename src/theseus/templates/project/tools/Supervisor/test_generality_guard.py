"""General contracts, not scenario slices: production files must not be named for one fixture (a device model, a
sample file, a test scenario). Fill SCENARIO_WORDS with the project's fixture names; ALLOWLIST only shrinks."""
from pathlib import Path
import unittest

from theseus import rules

ROOT = Path(__file__).resolve().parents[2]
SCENARIO_WORDS = ""
PRODUCTION = ("src",)
ALLOWLIST: set[str] = set()


class GeneralityGuardTests(unittest.TestCase):
    def setUp(self):
        if not SCENARIO_WORDS:
            self.skipTest("SCENARIO_WORDS is empty; add the project's fixture names to enable the guard")
        self.named = rules.scenario_named(ROOT, SCENARIO_WORDS, PRODUCTION)

    def test_no_new_scenario_named_production_files(self):
        self.assertEqual(set(), self.named - ALLOWLIST, "new scenario-named production file; build a general system instead")

    def test_allowlist_only_shrinks(self):
        self.assertEqual(set(), ALLOWLIST - self.named, "remove retired files from ALLOWLIST")


if __name__ == "__main__":
    unittest.main()
