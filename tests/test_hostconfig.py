"""Host configuration layer: precedence of package, host, project, project-local and environment values, host profiles
merged with project overrides, extra_args, the deprecated pools.toml and store.toml, config show --sources, config
check and setup host."""
import contextlib
import io
import os
import tomllib
from pathlib import Path

from helpers import Sandbox

from theseus import config as cfg
from theseus import hostconfig, pools, profiles, setup, tools

HOST_PROFILE = '''[workers.gw]
preset = "claude-code"
command = "launcher"
model = "m1"
model_flag = ""
env = { A = "1", B = "2" }
mcp_servers = ["theseus"]
pool = "gw"

[workers.gw.interactive]
env = { X = "1" }
'''


class HostCase(Sandbox):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        os.environ["HOME"] = self.env["HOME"] = str(self.home)
        self.host = Path(self.env["THESEUS_CONFIG"])
        self.project.mkdir()
        self.write("theseus.toml", '[project]\nname = "demo"\n')

    def conf(self):
        return cfg.load(root=self.project)

    def host_file(self, text):
        self.host.write_text(text)


class PrecedenceTests(HostCase):
    def test_each_layer_overrides_the_one_below(self):
        conf = self.conf()
        self.assertEqual(2, conf.raw["limits"]["heavy_runs"])
        self.assertEqual("package", cfg.source(conf, "limits.heavy_runs")[0])
        self.host_file("[defaults]\nlimits.heavy_runs = 4\nworkers.default = \"codex\"\n")
        conf = self.conf()
        self.assertEqual((4, "codex"), (conf.raw["limits"]["heavy_runs"], profiles.default_name(conf)))
        self.assertEqual(("host", self.host), cfg.source(conf, "limits.heavy_runs"))
        self.write("theseus.toml", '[project]\nname = "demo"\n[limits]\nheavy_runs = 5\n')
        conf = self.conf()
        self.assertEqual((5, "codex"), (conf.raw["limits"]["heavy_runs"], profiles.default_name(conf)))
        self.assertEqual("project", cfg.source(conf, "limits.heavy_runs")[0])
        self.write("theseus.local.toml", "[limits]\nheavy_runs = 6\n[workers]\ndefault = \"opencode\"\n")
        conf = self.conf()
        self.assertEqual((6, "opencode"), (conf.raw["limits"]["heavy_runs"], profiles.default_name(conf)))
        self.assertEqual(("project-local", self.project / "theseus.local.toml"), cfg.source(conf, "limits.heavy_runs"))

    def test_unknown_defaults_are_ignored_and_host_only_sections_stay_out_of_projects(self):
        self.host_file("[defaults]\ngit.remote = \"upstream\"\n")
        self.assertEqual("origin", self.conf().raw["git"]["remote"])
        self.write("theseus.toml", '[project]\nname = "demo"\n[pools.gw]\nmax = 1\n')
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertNotIn("pools", self.conf().raw)
        self.assertIn("ignoring unknown configuration sections: pools", stderr.getvalue())
        result = self.theseus("config", "check", check=False)
        self.assertEqual(1, result.returncode)
        self.assertIn("theseus.toml: unknown configuration sections: pools", result.stderr)
        self.assertNotIn("warning", result.stderr)

    def test_a_malformed_host_file_names_itself_and_commands_fail_cleanly(self):
        self.host_file("[pools.gw\n")
        with self.assertRaisesRegex(cfg.ConfigError, str(self.host)):
            self.conf()
        result = self.theseus("pool", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(f"theseus: {self.host}", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_an_unknown_section_from_a_newer_release_is_a_warning_once_except_in_config_check(self):
        self.addCleanup(hostconfig._warned.clear)
        hostconfig._warned.clear()
        self.host_file('[newer]\nx = 1\n[supervisor]\nsize = "100x40"\n')
        self.write("theseus.local.toml", "[later]\ny = 2\n")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual({"supervisor": {"size": "100x40"}}, hostconfig.read())
            self.conf()
            self.conf()
        lines = stderr.getvalue().splitlines()
        self.assertEqual(2, len(lines), lines)
        self.assertIn("ignoring unknown section(s) newer", lines[0])
        self.assertIn("theseus.local.toml: ignoring unknown configuration sections: later", lines[1])
        result = self.theseus("config", "check", check=False)
        self.assertEqual(1, result.returncode)
        self.assertIn("unknown section [newer]", result.stderr)
        self.assertIn("theseus.local.toml: unknown configuration sections: later", result.stderr)
        self.assertNotIn("warning", result.stderr)


class ExtraArgsTests(HostCase):
    def test_extra_args_without_mcp_servers_and_bad_types(self):
        self.write("theseus.local.toml", '[workers.claude-code]\nextra_args = ["--verbose"]\n')
        profile = profiles.resolve(self.conf(), "claude-code")
        self.assertEqual("--verbose", profiles.launch_argv(profile, "p", "S", "")[-1])
        self.assertEqual(["extra_args must be a list of strings"], profiles.problems("x", {"extra_args": "--verbose"}))
        self.write("theseus.local.toml", '[workers.claude-code]\nextra_args = "--verbose"\n')
        with self.assertRaisesRegex(profiles.ProfileError, "extra_args must be a list of strings"):
            profiles.launch_argv(profiles.resolve(self.conf(), "claude-code"), "p", "S", "")


class LegacyFileTests(HostCase):
    def test_pools_toml_is_read_below_config_toml_and_reported(self):
        Path(self.env["THESEUS_POOLS"]).write_text('[pools.gw]\nmax = 2\npriority = ["a"]\n[pools.other]\nmax = 1\n')
        self.assertEqual({"gw": {"max": 2, "priority": ["a"]}, "other": {"max": 1}}, pools.definitions())
        self.host_file("[pools.gw]\nmax = 5\n[pools.third]\nmax = 3\n")
        self.assertEqual({"gw": {"max": 5, "priority": ["a"]}, "other": {"max": 1}, "third": {"max": 3}}, pools.definitions())
        rows = [row for row in setup.host_rows() if row[1] == "deprecated file"]
        self.assertEqual(1, len(rows))
        self.assertEqual("WARN", rows[0][0])
        self.assertIn("pools.toml is deprecated; move its tables into [pools.<name>]", rows[0][2])
        result = self.theseus("config", "check")
        self.assertIn("note: " + self.env["THESEUS_POOLS"] + " is deprecated", result.stdout)


class ToolLayerTests(HostCase):
    def make_ghidra(self, name):
        root = self.tmp / name
        (root / "Ghidra").mkdir(parents=True)
        (root / "Ghidra/application.properties").write_text("application.version=x\n")
        return root

    def test_environment_then_project_local_then_host(self):
        for variable in ("GHIDRA_INSTALL_DIR", "THESEUS_TOOL_GHIDRA"):
            os.environ.pop(variable, None)
        host, local = self.make_ghidra("host-ghidra"), self.make_ghidra("local-ghidra")
        self.host_file(f'[tools.paths]\nghidra = "{host}"\n')
        self.assertEqual((str(host), "host config.toml"), tools.resolve(self.conf(), "ghidra"))
        self.write("theseus.local.toml", f'[tools.paths]\nghidra = "{self.tmp / "missing"}"\n')
        self.assertEqual((str(host), "host config.toml"), tools.resolve(self.conf(), "ghidra"))
        self.write("theseus.local.toml", f'[tools.paths]\nghidra = "{local}"\n')
        self.assertEqual((str(local), "theseus.local.toml"), tools.resolve(self.conf(), "ghidra"))
        os.environ["GHIDRA_INSTALL_DIR"] = str(host)
        self.assertEqual((str(host), "environment GHIDRA_INSTALL_DIR"), tools.resolve(self.conf(), "ghidra"))

    def test_set_tool_host_writes_the_host_file(self):
        os.environ.pop("GHIDRA_INSTALL_DIR", None)
        chosen = self.make_ghidra("ghidra")
        self.host.write_text(setup.host_template())
        tools.set_tool(self.conf(), "ghidra", str(chosen), host=True)
        self.assertEqual({"ghidra": str(chosen)}, tomllib.loads(self.host.read_text())["tools"]["paths"])
        self.assertFalse((self.project / "theseus.local.toml").exists())


class ConfigCommandTests(HostCase):
    def test_show_sources_names_the_layer_of_each_value(self):
        self.host_file(HOST_PROFILE + "[pools.gw]\nmax = 4\n")
        Path(self.env["THESEUS_POOLS"]).write_text("[pools.old]\nmax = 1\n")
        self.write("theseus.toml", '[project]\nname = "demo"\n[workers.gw]\ncapacity = 4\n')
        self.write("theseus.local.toml", "[limits]\nheavy_runs = 7\n")
        out = self.theseus("config", "show", "--sources").stdout
        self.assertIn(f"# layers, lowest to highest: package; host {self.host}; project {self.project / 'theseus.toml'}; "
                      f"project-local {self.project / 'theseus.local.toml'}; env", out)
        for line in ('workers.gw.command = "launcher"    # host', "workers.gw.capacity = 4    # project",
                     'workers.gw.interactive.busy_pattern = "esc to interrupt"    # package (preset claude-code)',
                     'workers.gw.interactive.env.X = "1"    # host', "limits.heavy_runs = 7    # project-local",
                     "workers.default = \"claude-code\"    # package", f'paths.data = "{self.tmp / "data"}"    # env',
                     "pools.gw.max = 4    # host", "pools.old.max = 1    # host (pools.toml, deprecated)",
                     "store.retention.samples_days = 14    # package"):
            self.assertIn(line, out)

    def test_check_reports_host_errors_with_file_and_key(self):
        self.host_file(HOST_PROFILE + '[workers.gw.extra]\nx = 1\n[workers.typo]\npreset = "claude-code"\ncolour = "red"\n'
                       '[defaults]\nworkers.default = "gw"\nfoo.bar = 1\n[pools.gw]\nmax = 1\nmin = 0\n')
        result = self.theseus("config", "check", check=False)
        self.assertEqual(1, result.returncode)
        self.assertIn(f"error: {self.host}: [workers.typo] unknown key colour", result.stderr)
        self.assertIn(f"error: {self.host}: [workers.gw] unknown key extra", result.stderr)
        self.assertIn(f"error: {self.host}: [pools.gw] unknown key min", result.stderr)
        self.assertIn(f"warning: {self.host}: [defaults] foo.bar is not a host default and is ignored", result.stdout)
        self.host_file("[bogus]\nx = 1\n")
        result = self.theseus("config", "check", check=False)
        self.assertEqual(1, result.returncode)
        self.assertEqual(1, result.stderr.count("bogus"), result.stderr)
        self.assertIn(str(self.host), result.stderr)
