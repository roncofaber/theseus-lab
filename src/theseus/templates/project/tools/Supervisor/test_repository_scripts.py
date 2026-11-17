"""Reusable scripts live in the repository: AGENTS.md, docs/project, the skills and the task records must not point
at scripts under the scratch root or a temporary directory, which disappear and cannot be reviewed."""
from pathlib import Path
import unittest

try:
    from theseus import config, rules
except ImportError:
    raise unittest.SkipTest("theseus is not importable here; run the Supervisor suite with `theseus check`")

ROOT = Path(__file__).resolve().parents[2]


class RepositoryScriptTests(unittest.TestCase):
    def test_governance_does_not_cite_scratch_scripts(self):
        self.assertEqual([], rules.scratch_script_references(config.load(root=ROOT)))


if __name__ == "__main__":
    unittest.main()
