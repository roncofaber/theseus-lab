"""Tool resolution without hard-coded paths: environment, then theseus.local.toml, then PATH, then common install
locations (newest version first) and REA's own MCP registration. HOME and PATH are sandboxed per test."""
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
        home_only = [g for g in tools.CATALOG["java"]["globs"] if g.startswith("~")]
        self.addCleanup(tools.CATALOG["java"].__setitem__, "globs", tools.CATALOG["java"]["globs"])
        tools.CATALOG["java"]["globs"] = home_only
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

    def test_environment_exports_tools_to_scripts_and_workers(self):
        ghidra = make_ghidra(self.home / "software/ghidra_12.2_PUBLIC")
        env = self.conf().environment()
        self.assertEqual(str(ghidra), env["THESEUS_TOOL_GHIDRA"])
        self.assertEqual(str(ghidra), env["GHIDRA_INSTALL_DIR"])
        exported = self.theseus("tools", "env").stdout
        self.assertIn(f"export GHIDRA_INSTALL_DIR={ghidra}", exported)

if __name__ == "__main__":
    unittest.main()
