"""Byte budgets for what agents read at startup or on every task. Past a budget the suite warns; past budget plus
15 percent it fails. Raising a budget is a deliberate review decision: prefer pruning, moving detail to a reference
file, or replacing prose with a tool. Project skills without an explicit entry get DEFAULT_SKILL_BUDGET."""
from pathlib import Path
import sys
import unittest

try:
    from theseus import rules
except ImportError:
    raise unittest.SkipTest("theseus is not importable here; run the Supervisor suite with `theseus check`")

ROOT = Path(__file__).resolve().parents[2]
FILE_BUDGETS = {
    "AGENTS.md": 7000,
    "docs/project/STATE.md": 10000,
    "docs/project/MANIFEST.md": 8000,
    "docs/project/ROADMAP.md": 11500,
    "docs/investigations/TEMPLATE.md": 2500,
    ".agents/skills/theseus-worker/prompt-template.md": 1800,
    ".agents/skills/theseus-worker/instance-brief.md": 4000,
    ".agents/skills/theseus-worker/SKILL.md": 5500,
    ".agents/skills/theseus-coordinating/SKILL.md": 8000,
    ".agents/skills/theseus-native-re/SKILL.md": 7500,
    ".agents/skills/theseus-reference-runs/SKILL.md": 8000,
}
DEFAULT_SKILL_BUDGET = 6000
WORKER_STARTUP = ["AGENTS.md", "docs/project/STATE.md", ".agents/skills/theseus-worker/SKILL.md",
                  ".agents/skills/theseus-worker/prompt-template.md"]
COORDINATOR_STARTUP = ["AGENTS.md", "docs/project/STATE.md", "docs/project/MANIFEST.md", "docs/project/ROADMAP.md",
                       ".agents/skills/theseus-coordinating/SKILL.md"]
READ_SETS = {"worker startup read set": (WORKER_STARTUP, 19000),
             "coordinator startup read set": (COORDINATOR_STARTUP, 42000)}


class ContextBudgetTests(unittest.TestCase):
    def test_files_skills_and_read_sets_stay_within_budget(self):
        rows, missing = rules.budget_rows(ROOT, FILE_BUDGETS, DEFAULT_SKILL_BUDGET, READ_SETS)
        for warning in rules.budget_warnings(rows):
            print(warning, file=sys.stderr)
        self.assertEqual([], missing, "budgeted files are missing; update FILE_BUDGETS")
        self.assertEqual([], rules.budget_problems(rows))


if __name__ == "__main__":
    unittest.main()
