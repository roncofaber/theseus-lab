"""No hard-coded developer paths in committed tooling: derive them from theseus.toml (`theseus config get KEY`) or the
THESEUS_* environment. LEGACY lists files that predate the rule; remove entries as they are fixed, never add one."""
from pathlib import Path
import unittest

from theseus import config, rules

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("tools",)
LEGACY: set[str] = set()


class PortablePathTests(unittest.TestCase):
    def setUp(self):
        self.offenders = rules.path_offenders(ROOT, rules.developer_needles(config.load(root=ROOT)), SCANNED)

    def test_tooling_does_not_hard_code_developer_paths(self):
        self.assertEqual(set(), self.offenders - LEGACY)

    def test_legacy_list_has_no_stale_entries(self):
        self.assertEqual(set(), LEGACY - self.offenders, "remove entries that no longer hard-code a path")


if __name__ == "__main__":
    unittest.main()
