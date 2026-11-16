"""Pre-push guard: fast checks on the pushed commit, full verification for code, the logged override and its budget."""
import os
import sys
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

    def code_commit(self, n):
        self.write(f"src/module{n}.py", f"VALUE = {n}\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"code {n}")

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

    def test_override_is_logged_and_bounded_by_the_unverified_budget(self):
        self.theseus("verify")
        self.code_commit(1)
        allowed = self.guard("code-only round")
        self.assertEqual(0, allowed.returncode, allowed.stderr)
        log = (self.tmp / "data/logs/unverified-pushes.log").read_text()
        self.assertIn("reason=code-only round", log)
        self.code_commit(2)
        self.code_commit(3)
        refused = self.guard("code-only round")
        self.assertEqual(1, refused.returncode)
        self.assertIn("exceed verification.max_unverified_merges=2", refused.stderr)
        self.assertEqual(1, self.theseus("verify", "--debt", check=False).returncode)

    def test_override_is_refused_when_unverified_code_is_too_old(self):
        self.theseus("verify")
        self.write("src/old.py", "OLD = 1\n")
        self.git("add", "-A")
        env = dict(self.env, GIT_COMMITTER_DATE="2000-01-01T00:00:00", GIT_AUTHOR_DATE="2000-01-01T00:00:00")
        self.run_cmd(["git", "commit", "-q", "-m", "old code"], env=env)
        refused = self.guard("code-only round")
        self.assertEqual(1, refused.returncode)
        self.assertIn("max_unverified_hours=24", refused.stderr)

    def test_failing_supervisor_check_on_the_pushed_commit_blocks_the_push(self):
        self.theseus("verify")
        self.write("tools/broken.sh", "#!/usr/bin/env bash\nif true; then\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "broken script")
        result = self.guard("anything")
        self.assertEqual(1, result.returncode)
        self.assertIn("check failed", result.stderr)

    def test_failing_verify_command_writes_a_failed_log(self):
        self.replace_config('verify = ["true"]', 'verify = ["false"]')
        self.git("commit", "-q", "-am", "failing verify")
        self.assertEqual(1, self.theseus("verify", check=False).returncode)
        self.assertEqual(1, self.guard().returncode)

    def test_verify_commands_resolve_the_clean_tree(self):
        self.replace_config('verify = ["true"]', 'verify = [\'test "$THESEUS_ROOT" = "$THESEUS_VERIFY_TREE" && test "$PWD" = "$THESEUS_ROOT" && test -f theseus.local.toml\']')
        self.write("theseus.local.toml", "[paths]\noriginal = []\n")
        self.git("commit", "-q", "-am", "root check")
        self.theseus("verify")

    def test_real_git_push_runs_the_hook(self):
        remote = self.tmp / "remote.git"
        self.git("init", "-q", "--bare", remote)
        self.git("remote", "add", "origin", remote)
        shim = self.tmp / "bin/theseus"
        shim.parent.mkdir()
        shim.write_text(f"#!/usr/bin/env bash\nexec {sys.executable} -m theseus \"$@\"\n")
        shim.chmod(0o755)
        env = dict(self.env, PATH=f"{shim.parent}{os.pathsep}{self.env['PATH']}")
        refused = self.run_cmd(["git", "push", "-q", "origin", "main"], env=env, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("no passing full verification covers it", refused.stderr)
        allowed = self.run_cmd(["git", "push", "-q", "origin", "main"], env=dict(env, THESEUS_PUSH_UNVERIFIED="first push"), check=False)
        self.assertEqual(0, allowed.returncode, allowed.stderr)
        self.assertIn("reason=first push", (self.tmp / "data/logs/unverified-pushes.log").read_text())
        self.code_commit(9)
        self.theseus("verify")
        verified = self.run_cmd(["git", "push", "-q", "origin", "main"], env=env, check=False)
        self.assertEqual(0, verified.returncode, verified.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, self.git("--git-dir", remote, "rev-parse", "main").stdout)

    def test_unconfigured_commands_report_not_configured(self):
        for command in ("build", "smoke"):
            result = self.theseus(command, check=False)
            self.assertEqual(4, result.returncode)
            self.assertIn("NOT CONFIGURED", result.stdout)


if __name__ == "__main__":
    unittest.main()
