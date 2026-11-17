"""Every shell script Git tracks (by .sh suffix or a bash/sh shebang) parses and passes shellcheck at warning
severity. Without shellcheck the lint is skipped and reported as skipped."""
from pathlib import Path
import unittest

try:
    from theseus import rules
except ImportError:
    raise unittest.SkipTest("theseus is not importable here; run the Supervisor suite with `theseus check`")

ROOT = Path(__file__).resolve().parents[2]


class ShellScriptTests(unittest.TestCase):
    def setUp(self):
        self.scripts = rules.tracked_shell_scripts(ROOT)

    def test_hook_is_discovered(self):
        self.assertIn(ROOT / "tools/git-hooks/pre-push", self.scripts)

    def test_bash_syntax(self):
        self.assertEqual([], rules.syntax_errors(self.scripts))

    def test_shellcheck_warning_severity(self):
        if not rules.shellcheck_available():
            self.skipTest("shellcheck is not installed; lint skipped")
        self.assertEqual([], rules.shellcheck_errors(self.scripts, cwd=ROOT))


if __name__ == "__main__":
    unittest.main()
