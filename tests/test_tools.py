"""Tool resolution without hard-coded paths: environment, then theseus.local.toml, then PATH, then common install
locations (newest version first) and REA's own MCP registration. HOME and PATH are sandboxed per test."""
import json
import os
import unittest

from helpers import Sandbox

from theseus import config as cfg
from theseus import tools


def make_ghidra(root):
    (root / "Ghidra").mkdir(parents=True)
    (root / "Ghidra/application.properties").write_text("application.version=x\n")
    return root


def make_executable(path, body="#!/bin/sh\necho 6.9.0\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


class ToolResolutionTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        self.home = self.tmp / "home"
        self.home.mkdir()
        os.environ["HOME"] = str(self.home)
        os.environ["PATH"] = "/usr/bin:/bin"
        for variable in ("GHIDRA_INSTALL_DIR", "JAVA_HOME", "THESEUS_TOOL_REA", "THESEUS_TOOL_GHIDRA"):
            os.environ.pop(variable, None)
        self.env.update({"HOME": str(self.home), "PATH": "/usr/bin:/bin"})

    def conf(self):
        return cfg.load(root=self.project)

    def test_install_locations_pick_the_newest_version(self):
        make_ghidra(self.home / "software/ghidra_11.4_PUBLIC")
        newest = make_ghidra(self.home / "software/ghidra_12.2_PUBLIC")
        self.assertEqual((str(newest), "install location"), tools.resolve(self.conf(), "ghidra"))
        make_executable(self.home / "software/jdk-21/bin/java")
        jdk25 = self.home / "software/jdk-25-temurin"
        make_executable(jdk25 / "bin/java")
        self.assertEqual(str(jdk25), tools.resolve(self.conf(), "java")[0])

    def test_environment_beats_local_setting_beats_discovery(self):
        make_ghidra(self.home / "software/ghidra_12.2_PUBLIC")
        chosen = make_ghidra(self.tmp / "elsewhere/ghidra")
        tools.set_tool(self.conf(), "ghidra", str(chosen))
        self.assertEqual((str(chosen), "theseus.local.toml"), tools.resolve(self.conf(), "ghidra"))
        override = make_ghidra(self.tmp / "override/ghidra")
        os.environ["GHIDRA_INSTALL_DIR"] = str(override)
        self.assertEqual((str(override), "environment GHIDRA_INSTALL_DIR"), tools.resolve(self.conf(), "ghidra"))
        self.assertIn("[tools.paths]", (self.project / "theseus.local.toml").read_text())

    def test_set_refuses_a_path_that_is_not_the_tool_and_rewrites_an_existing_entry(self):
        with self.assertRaises(cfg.ConfigError):
            tools.set_tool(self.conf(), "ghidra", str(self.tmp))
        with self.assertRaises(cfg.ConfigError):
            tools.set_tool(self.conf(), "unknown-tool", "/bin/sh")
        first, second = make_ghidra(self.tmp / "g1"), make_ghidra(self.tmp / "g2")
        tools.set_tool(self.conf(), "ghidra", str(first))
        tools.set_tool(self.conf(), "ghidra", str(second))
        text = (self.project / "theseus.local.toml").read_text()
        self.assertEqual(1, text.count("ghidra ="))
        self.assertIn(str(second), text)
        cfg.load(root=self.project)

    def test_rea_falls_back_to_its_mcp_registration_and_runs_with_the_node_beside_it(self):
        node = make_executable(self.home / ".nvm/versions/node/v24/bin/node")
        rea = make_executable(self.home / ".nvm/versions/node/v24/bin/rea")
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"rea": {"command": str(node), "args": [str(rea), "mcp"]}}}))
        self.assertEqual(([str(node), str(rea)], "REA MCP registration"), tools.resolve(self.conf(), "rea"))
        (self.home / ".claude.json").unlink()
        self.assertEqual(([str(node), str(rea)], "install location"), tools.resolve(self.conf(), "rea"))

    def test_environment_exports_tools_to_scripts_and_workers(self):
        ghidra = make_ghidra(self.home / "software/ghidra_12.2_PUBLIC")
        env = self.conf().environment()
        self.assertEqual(str(ghidra), env["THESEUS_TOOL_GHIDRA"])
        self.assertEqual(str(ghidra), env["GHIDRA_INSTALL_DIR"])
        exported = self.theseus("tools", "env").stdout
        self.assertIn(f"export GHIDRA_INSTALL_DIR={ghidra}", exported)

    def test_needed_tool_missing_fails_doctor(self):
        self.replace_config("need = []", 'need = ["ghidra"]')
        result = self.theseus("doctor", check=False)
        self.assertEqual(1, result.returncode)
        self.assertRegex(result.stdout, r"FAIL\s+ghidra\s+needed by this project")
        make_ghidra(self.home / "software/ghidra_12.2_PUBLIC")
        self.assertRegex(self.theseus("doctor", check=False).stdout, r"ok\s+ghidra")


if __name__ == "__main__":
    unittest.main()
