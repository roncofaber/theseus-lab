"""Task records: identities, statuses and completion evidence, a report per open task, existing and acyclic
dependencies, blocker records, per-provider capacity from theseus.toml, record budgets, and one milestone at a time."""
from pathlib import Path
import unittest

from theseus import config, rules

ROOT = Path(__file__).resolve().parents[2]


class ProjectTaskTests(unittest.TestCase):
    def test_task_records_follow_the_workflow_rules(self):
        self.assertEqual([], rules.task_record_problems(config.load(root=ROOT)))


if __name__ == "__main__":
    unittest.main()
