"""Pre-push guard: fast checks on the pushed commit, full verification for code, the logged override and its budget."""
import unittest

from helpers import Sandbox

ZERO = "0" * 40


class GuardTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        self.replace_config("verify = []", "verify = [\"true\"]")
        self.replace_config("max_unverified_merges = 15", "max_unverified_merges = 2")
        self.git("commit", "-q", "-am", "configure")

    def push_line(self):
        head = self.git("rev-parse", "HEAD").stdout.strip()
        return f"refs/heads/main {head} refs/heads/main {ZERO}\n"

    def guard(self, override=None):
        env = dict(self.env)
        if override:
            env["THESEUS_PUSH_UNVERIFIED"] = override
        return self.theseus("guard", "origin", "url", input=self.push_line(), env=env, check=False)

    def test_unverified_code_is_refused_and_a_full_pass_admits_it(self):
        refused = self.guard()
        self.assertEqual(1, refused.returncode, refused.stderr)
        self.assertIn("no passing full verification covers it", refused.stderr)
        self.theseus("verify")
        self.assertEqual(0, self.guard().returncode)
        self.write("docs/investigations/note.md", "# Note\n")
        self.theseus("docs", "index")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "docs only")
        self.assertEqual(0, self.guard().returncode, "docs-only commits after a verified commit need no new pass")

    def test_unconfigured_commands_report_not_configured(self):
        for command in ("build", "smoke"):
            result = self.theseus(command, check=False)
            self.assertEqual(4, result.returncode)
            self.assertIn("NOT CONFIGURED", result.stdout)


if __name__ == "__main__":
    unittest.main()
