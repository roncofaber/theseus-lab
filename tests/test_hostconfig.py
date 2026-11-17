"""Host configuration layer: precedence of package, host, project, project-local and environment values, host profiles
merged with project overrides, extra_args, the deprecated pools.toml and store.toml, config show --sources, config
check and setup host."""
import os
import re
import tomllib
from pathlib import Path

from helpers import Sandbox

from theseus import config as cfg
from theseus import hostconfig, pools, profiles, setup, store, tools

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

    def test_environment_beats_every_file_for_paths(self):
        os.environ.pop("THESEUS_DATA")
        self.host_file('[defaults]\npaths.data = "' + str(self.tmp / "host-data") + '"\n')
        self.assertEqual(self.tmp / "host-data", self.conf().data)
        self.write("theseus.local.toml", '[paths]\ndata = "' + str(self.tmp / "local-data") + '"\n')
        self.assertEqual(self.tmp / "local-data", self.conf().data)
        os.environ["THESEUS_DATA"] = str(self.tmp / "env-data")
        conf = self.conf()
        self.assertEqual(self.tmp / "env-data", conf.data)
        self.assertEqual("env", cfg.source(conf, "paths.data")[0])

    def test_unknown_defaults_are_ignored_and_host_only_sections_stay_out_of_projects(self):
        self.host_file("[defaults]\ngit.remote = \"upstream\"\n")
        self.assertEqual("origin", self.conf().raw["git"]["remote"])
        self.write("theseus.toml", '[project]\nname = "demo"\n[pools.gw]\nmax = 1\n')
        with self.assertRaisesRegex(cfg.ConfigError, "theseus.toml: unknown configuration sections: pools"):
            self.conf()

    def test_a_malformed_host_file_names_itself(self):
        self.host_file("[workers\n")
        with self.assertRaisesRegex(cfg.ConfigError, str(self.host)):
            self.conf()
        self.host_file("[surprise]\nx = 1\n")
        with self.assertRaisesRegex(cfg.ConfigError, "unknown section.*surprise"):
            self.conf()


class HostProfileTests(HostCase):
    def test_project_section_overrides_only_the_keys_it_sets(self):
        self.host_file(HOST_PROFILE)
        self.write("theseus.toml", '[project]\nname = "demo"\n[workers]\ndefault = "gw"\n[workers.gw]\ncapacity = 4\n'
                                   'env = { B = "3" }\n[workers.gw.interactive.env]\nY = "2"\n')
        profile = profiles.resolve(self.conf())
        self.assertEqual(("gw", "launcher", "m1", 4, "gw"), (profile["name"], profile["command"], profile["model"],
                                                           profile["capacity"], profile["pool"]))
        self.assertEqual({"A": "1", "B": "3"}, profile["env"])
        self.assertEqual({"X": "1", "Y": "2"}, profile["interactive"]["env"])
        self.assertEqual("esc to interrupt", profile["interactive"]["busy_pattern"])
        self.assertEqual(["theseus"], profile["mcp_servers"])
        self.assertIn("gw", profiles.names(self.conf()))

    def test_a_host_section_named_after_a_preset_adjusts_it_under_the_project(self):
        self.host_file('[workers.claude-code]\nmodel = "host-model"\ncapacity = 9\n')
        self.write("theseus.toml", '[project]\nname = "demo"\n[workers.claude-code]\ncapacity = 2\n')
        profile = profiles.resolve(self.conf(), "claude-code")
        self.assertEqual(("claude", "host-model", 2), (profile["command"], profile["model"], profile["capacity"]))


class ExtraArgsTests(HostCase):
    def test_extra_args_follow_the_mcp_flags_on_every_launch_path(self):
        self.host_file(HOST_PROFILE.replace('pool = "gw"', 'extra_args = ["--settings", "~/worker settings.json", "--tag", "{model}"]'))
        conf = self.conf()
        profile = profiles.resolve(conf, "gw")
        settings = str(self.home / "worker settings.json")
        mcp = str(conf.data / "mcp" / "gw.json")
        tail = ["--strict-mcp-config", "--mcp-config", mcp, "--settings", settings, "--tag", "m1"]
        self.assertEqual(tail, profiles.launch_argv(profile, "do it", "S1", "m1", conf)[-7:])
        resume = profiles.resume_template(profile, "S1", "m1", conf)
        self.assertEqual(tail, resume[-7:])
        self.assertIn("{message}", resume)
        launch = profiles.instance_env(conf, "gw", "worker-1")["THESEUS_AGENT_LAUNCH"]
        self.assertTrue(launch.endswith(f"--mcp-config {mcp} --settings '{settings}' --tag m1"), launch)
        self.assertTrue(launch.startswith("env A=1 B=2 X=1 launcher --name worker-1 "), launch)

    def test_extra_args_without_mcp_servers_and_bad_types(self):
        self.write("theseus.local.toml", '[workers.claude-code]\nextra_args = ["--verbose"]\n')
        profile = profiles.resolve(self.conf(), "claude-code")
        self.assertEqual("--verbose", profiles.launch_argv(profile, "p", "S", "")[-1])
        self.assertEqual(["extra_args must be a list of strings"], profiles.problems("x", {"extra_args": "--verbose"}))


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

    def test_store_toml_is_read_below_config_toml(self):
        Path(self.env["THESEUS_STORE_CONFIG"]).write_text("[retention]\nsamples_days = 2\nevents_days = 30\n")
        self.host_file("[store.retention]\nsamples_days = 5\n[store.backups]\ndaily = 3\n")
        settings = store.settings()
        self.assertEqual({"samples_days": 5, "events_days": 30}, settings["retention"])
        self.assertEqual({"directory": "", "daily": 3, "weekly": 4}, settings["backups"])
        notes = hostconfig.deprecation_notes()
        self.assertEqual(1, len(notes))
        self.assertIn("store.toml is deprecated; move its tables into [store]", notes[0])
        self.assertIn("store.toml is deprecated", self.theseus("config", "check").stdout)
        self.host_file("[store.retention\n")
        with self.assertRaises(store.StoreError):
            store.settings()


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

    def test_check_warns_on_project_profile_keys_and_works_outside_a_project(self):
        self.host_file('[defaults]\nfoo = 1\n')
        self.write("theseus.toml", '[project]\nname = "demo"\n[workers.claude-code]\ncolour = "red"\n')
        result = self.theseus("config", "check")
        self.assertIn("[workers.claude-code] unknown key colour", result.stdout)
        self.assertIn(f"host config {self.host} is valid", result.stdout)
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        self.assertIn("not inside a project; checked the host config only", self.theseus("config", "check", cwd=outside).stdout)


class SetupHostTests(HostCase):
    def test_write_refuse_force_and_print(self):
        printed = self.theseus("setup", "host", "--print").stdout
        self.assertFalse(self.host.exists())
        self.assertEqual(setup.host_template(), printed)
        self.assertEqual({}, tomllib.loads(printed))
        self.assertEqual(([], []), setup.host_check(printed))
        example = re.compile(r"^# (\[[\w.-]+\]|[\w.-]+ = .*)$")
        uncommented = "\n".join(line[2:] if example.match(line) else line for line in printed.splitlines())
        self.assertEqual(([], []), setup.host_check(uncommented))
        self.assertIn("wrote", self.theseus("setup", "host").stdout)
        self.assertEqual(setup.host_template(), self.host.read_text())
        self.host.write_text("# mine\n")
        refused = self.theseus("setup", "host", check=False)
        self.assertEqual(1, refused.returncode)
        self.assertIn("pass --force", refused.stdout)
        self.assertEqual("# mine\n", self.host.read_text())
        self.theseus("setup", "host", "--force")
        self.assertEqual(setup.host_template(), self.host.read_text())

    def test_import_project_dry_run_then_write_leaves_the_project_alone(self):
        project_text = ('[project]\nname = "demo"\n\n[workers]\ndefault = "gw"\n\n# the gateway profile\n' + HOST_PROFILE
                        + '\n# instances follow\n[instances]\npanes = []\n')
        self.write("theseus.toml", project_text)
        self.write("theseus.local.toml", '[paths]\noriginal = []\n\n[tools.paths]\nghidra = "/opt/ghidra"\n')
        before = self.conf().raw["workers"]["gw"]
        dry = self.theseus("setup", "host", "--import-project", self.project, "--dry-run", cwd=self.tmp).stdout
        start = project_text.splitlines().index("[workers.gw]") + 1
        end = project_text.splitlines().index('env = { X = "1" }') + 1
        self.assertIn(f"would move [workers.gw] from {self.project / 'theseus.toml'} lines {start}-{start + 7}", dry)
        self.assertIn(f"would move [workers.gw] from {self.project / 'theseus.toml'} lines {end - 1}-{end}", dry)
        self.assertIn(f"would move [tools.paths] from {self.project / 'theseus.local.toml'} lines 4-5", dry)
        self.assertIn("nothing written", dry)
        self.assertFalse(self.host.exists())
        done = self.theseus("setup", "host", "--import-project", self.project, cwd=self.tmp).stdout
        self.assertIn("remove these lines", done)
        self.assertEqual(project_text, (self.project / "theseus.toml").read_text())
        written = tomllib.loads(self.host.read_text())
        self.assertEqual(before, written["workers"]["gw"])
        self.assertEqual({"ghidra": "/opt/ghidra"}, written["tools"]["paths"])
        self.assertNotIn("instances follow", self.host.read_text())
        again = self.theseus("setup", "host", "--import-project", self.project, cwd=self.tmp, check=False)
        self.assertEqual(1, again.returncode)
        self.assertIn("[workers.gw] already exists", again.stdout)
