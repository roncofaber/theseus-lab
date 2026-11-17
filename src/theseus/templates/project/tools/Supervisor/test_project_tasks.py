"""Task records: identities, statuses and completion evidence, a report per open task, existing and acyclic
dependencies, blocker records, per-provider capacity from theseus.toml, record budgets, and one milestone at a time."""
from pathlib import Path
import unittest

try:
    from theseus import config, rules
except ImportError:
    raise unittest.SkipTest("theseus is not importable here; run the Supervisor suite with `theseus check`")

ROOT = Path(__file__).resolve().parents[2]


class ProjectTaskTests(unittest.TestCase):
    def test_task_records_follow_the_workflow_rules(self):
        self.assertEqual([], rules.task_record_problems(config.load(root=ROOT)))


if __name__ == "__main__":
    unittest.main()
