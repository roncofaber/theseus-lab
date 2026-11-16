"""theseus doctor and theseus setup rea against a fake `rea` on PATH, so the real REA install and agent client
configuration are never touched."""
import os
import sys
import unittest

from helpers import Sandbox

FAKE_REA = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
state = os.environ["FAKE_REA_STATE"]
with open(state + ".log", "a") as log:
    log.write(" ".join(args) + "\n")
if args == ["--version"]:
    print("6.9.0"); sys.exit(0)
if args[:1] == ["update"]:
    print("status: current"); sys.exit(0)
configured = os.path.exists(state + ".configured")
def report(configured, code=None, remediation=None):
    states = [{"client": {"name": "claude_code"}, "configured": configured,
               "status": "configured" if configured else "needs_configuration"}]
    return {"status": "ok" if configured else "needs_human", "code": code, "remediation": remediation, "clientStates": states}
if "--dry-run" in args:
    print(json.dumps(report(configured))); sys.exit(0)
if "--skill-client" in args and os.environ.get("FAKE_REA_SYMLINK"):
    print(json.dumps(report(False, "skill_install_failed", "Reason: Skill directory is a symbolic link.")))
    sys.exit(0)
open(state + ".configured", "w").write(os.environ.get("GHIDRA_INSTALL_DIR", ""))
print(json.dumps(report(True)))
"""


class SetupTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "rea"
        fake.write_text(FAKE_REA)
        fake.chmod(0o755)
        self.state = self.tmp / "rea-state"
        ghidra = self.tmp / "ghidra"
        (ghidra / "Ghidra").mkdir(parents=True)
        (ghidra / "Ghidra/application.properties").write_text("application.version=12.2\n")
        home = self.tmp / "home"
        (home / ".claude/skills/reverse-engineer-anything").mkdir(parents=True)
        (home / ".claude/skills/reverse-engineer-anything/SKILL.md").write_text("---\nname: reverse-engineer-anything\n---\n")
        self.env.update({"PATH": f"{bin_dir}{os.pathsep}{self.env['PATH']}", "FAKE_REA_STATE": str(self.state), "HOME": str(home),
                         "GHIDRA_INSTALL_DIR": str(ghidra)})

    def test_doctor_flags_an_unregistered_client_and_setup_fixes_it(self):
        before = self.theseus("doctor", check=False)
        self.assertEqual(1, before.returncode, before.stdout)
        self.assertRegex(before.stdout, r"FAIL\s+rea mcp \(claude_code\)\s+needs_configuration")
        self.assertRegex(before.stdout, r"ok\s+ghidra")
        setup = self.theseus("setup", "rea", "--update")
        self.assertIn("rea update: status: current", setup.stdout)
        self.assertIn("claude_code: REA MCP server registered with GHIDRA_INSTALL_DIR", setup.stdout)
        self.assertEqual(str(self.tmp / "ghidra"), (self.tmp / "rea-state.configured").read_text())
        after = self.theseus("doctor")
        self.assertRegex(after.stdout, r"ok\s+rea mcp \(claude_code\)\s+registered")
        self.assertIn("all required checks passed", after.stdout)

    def test_symlinked_skill_falls_back_to_registering_the_server_only(self):
        self.env["FAKE_REA_SYMLINK"] = "1"
        setup = self.theseus("setup", "rea")
        self.assertIn("is a symbolic link that REA will not overwrite", setup.stdout)
        self.assertIn("REA MCP server registered", setup.stdout)
        calls = (self.tmp / "rea-state.log").read_text().splitlines()
        self.assertTrue(any("--skill-client claude_code" in c for c in calls))
        self.assertTrue(any("--skill=false" in c and "--dry-run" not in c for c in calls))

    def test_missing_rea_is_reported_not_fatal_to_doctor(self):
        self.env["PATH"] = "/usr/bin:/bin"
        result = self.theseus("doctor", check=False)
        self.assertRegex(result.stdout, r"WARN\s+rea\s+not found; optional")
        self.assertNotEqual(0, self.theseus("setup", "rea", check=False).returncode)



class SetupSkillTests(Sandbox):
    def test_setup_skill_installs_outside_a_project_and_refuses_to_clobber(self):
        target = self.tmp / "skills"
        out = self.run_cmd([sys.executable, "-m", "theseus", "setup", "skill", "--dir", str(target)], cwd=self.tmp).stdout
        self.assertIn("installed", out)
        skill = target / "theseus-setup/SKILL.md"
        self.assertIn("name: theseus-setup", skill.read_text())
        skill.write_text("edited\n")
        refused = self.run_cmd([sys.executable, "-m", "theseus", "setup", "skill", "--dir", str(target)], cwd=self.tmp, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.run_cmd([sys.executable, "-m", "theseus", "setup", "skill", "--dir", str(target), "--force"], cwd=self.tmp)
        self.assertIn("name: theseus-setup", skill.read_text())


if __name__ == "__main__":
    unittest.main()
