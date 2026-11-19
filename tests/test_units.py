"""Configuration loading, task record rules and cleanup, exercised directly through the package."""
import json
import os
import time
import unittest
from pathlib import Path

from helpers import Sandbox

from theseus import cleanup
from theseus import config as cfg
from theseus import rules
from theseus import tasks as t


class ConfigTests(Sandbox):
    def test_missing_config_is_an_error_not_a_default(self):
        self.tmp.joinpath("nothing").mkdir()
        with self.assertRaises(cfg.ConfigError):
            cfg.load(start=self.tmp / "nothing")

    def test_local_overlay_env_overrides_and_expansion(self):
        self.scaffold()
        self.write("theseus.local.toml", '[paths]\noriginal = ["${THESEUS_ROOT}/../original"]\n[workers.codex]\ncapacity = 5\n')
        conf = cfg.load(root=self.project)
        self.assertEqual(self.tmp / "data", conf.data)
        self.assertEqual([Path(f"{self.project}/../original")], conf.original)
        from theseus import profiles
        self.assertEqual(5, profiles.resolve(conf, "codex")["capacity"])
        self.assertEqual("codex", profiles.resolve(conf, "codex")["command"])
        self.assertEqual("claude-code", profiles.resolve(conf)["name"])
        self.assertEqual("demo-port", conf.prefix)
        self.assertEqual(str(self.tmp / "worktrees"), conf.environment()["THESEUS_WORKTREES"])
        other = self.tmp / "worktrees/elsewhere"
        self.assertTrue(conf.environment()["THESEUS_BUILD_DIR"].startswith(str(self.tmp / "scratch/build/project-")))
        self.assertNotEqual(conf.build_dir, cfg.Config(other, conf.raw).build_dir)
        self.assertEqual("default", cfg.expand("${UNSET_THESEUS_VARIABLE:-default}", {}))
        with self.assertRaises(cfg.ConfigError):
            cfg.expand("$UNSET_THESEUS_VARIABLE", {})

    def test_roots_inside_the_project_are_rejected(self):
        self.scaffold()
        os.environ["THESEUS_DATA"] = str(self.project / "data")
        with self.assertRaises(cfg.ConfigError):
            cfg.load(root=self.project)


    def test_the_scaffolded_data_root_follows_xdg_data_home(self):
        from unittest import mock
        self.scaffold()
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.tmp / "xdg")}):
            os.environ.pop("THESEUS_DATA", None)
            self.assertEqual(self.tmp / "xdg/demo-port", cfg.load(root=self.project).data)


class OwnershipTests(unittest.TestCase):
    def test_directories_own_their_contents_with_or_without_a_slash(self):
        self.assertTrue(t.is_owned("scripts/Campaign/FlickRuntime/World.cs", ["scripts/Campaign/FlickRuntime"]))
        self.assertTrue(t.is_owned("scripts/Campaign/FlickRuntime/a/b.cs", ["scripts/Campaign/FlickRuntime/"]))
        self.assertTrue(t.is_owned("src/x.py", ["src/x.py"]))
        self.assertFalse(t.is_owned("scripts/Campaign/FlickRuntimeOther/World.cs", ["scripts/Campaign/FlickRuntime"]))
        self.assertTrue(t.is_owned("src/a/b.c", ["src/*/b.c"]))
        self.assertFalse(t.is_owned("src/a/c/b.c", ["src/*/b.c"]))


class ProfileTests(Sandbox):
    def test_placeholders_fill_in_one_pass_and_variants_inherit(self):
        from theseus import profiles
        self.scaffold()
        self.write("theseus.local.toml", '[workers.gateway]\npreset = "claude-code"\ncommand = "wrapped"\nmodel = "m1"\n'
                   'model_flag = ""\nenv = { GW_MODEL = "{model}" }\n')
        conf = cfg.load(root=self.project)
        profile = profiles.resolve(conf, "gateway")
        argv = profiles.launch_argv(profile, "fix {session} and {model}", "S1", "m1")
        self.assertEqual(["wrapped", "-p", "fix {session} and {model}", "--session-id", "S1"], argv[:5])
        self.assertNotIn("--model", argv)
        self.assertEqual({"GW_MODEL": "m1"}, profiles.env(profile))
        self.assertIn("--model", profiles.launch_argv(profiles.resolve(conf, "claude-code"), "p", "S", "opus"))
        with self.assertRaises(profiles.ProfileError):
            profiles.resolve(conf, "no-such-agent")


class WorkerMcpTests(Sandbox):
    def test_mcp_servers_limit_what_workers_load_and_launch_follows_the_command(self):
        from theseus import profiles
        self.scaffold()
        self.write("theseus.local.toml", '[workers.gateway]\npreset = "claude-code"\ncommand = "wrapped-claude"\n'
                   'mcp_servers = ["theseus"]\n')
        conf = cfg.load(root=self.project)
        profile = profiles.resolve(conf, "gateway")
        env = profiles.instance_env(conf, "gateway", "worker-1")
        self.assertTrue(env["THESEUS_AGENT_LAUNCH"].startswith(
            "wrapped-claude --name worker-1 --permission-mode auto --strict-mcp-config --mcp-config "))
        path = Path(env["THESEUS_AGENT_LAUNCH"].split()[-1])
        self.assertEqual({"theseus"}, set(json.loads(path.read_text())["mcpServers"]))
        argv = profiles.launch_argv(profile, "do it", "S1", "", conf)
        self.assertEqual(["--strict-mcp-config", "--mcp-config", str(path)], argv[-3:])
        self.assertEqual("claude --name worker-1 --permission-mode auto",
                         profiles.instance_env(conf, "claude-code", "worker-1")["THESEUS_AGENT_LAUNCH"])
        self.assertEqual("claude --name {agent} --permission-mode auto", profiles.instance_env(conf)["THESEUS_AGENT_LAUNCH"])
        self.write("theseus.local.toml", '[workers.gateway]\npreset = "claude-code"\nmcp_servers = ["nonexistent"]\n')
        with self.assertRaises(profiles.ProfileError):
            profiles.instance_env(cfg.load(root=self.project), "gateway")


class InteractiveLaunchTests(Sandbox):
    def test_env_prefix_quoting_empty_options_and_stall_patterns(self):
        from theseus import profiles
        self.scaffold()
        self.write("theseus.local.toml", '[workers.gateway]\npreset = "claude-code"\ncommand = "wrapped-claude"\n'
                   'model = "m1"\nmodel_flag = ""\npermission_mode = ""\nenv = { GW_MODEL = "{model}" }\n'
                   '[workers.gateway.interactive]\nenv = { READ_LIMIT = "10000", NOTE = "a b" }\n')
        conf = cfg.load(root=self.project)
        env = profiles.instance_env(conf, "gateway", "it's-me")
        self.assertEqual("env GW_MODEL=m1 READ_LIMIT=10000 'NOTE=a b' wrapped-claude --name 'it'\"'\"'s-me'",
                         env["THESEUS_AGENT_LAUNCH"])
        self.assertEqual("{launch} --resume {session}", env["THESEUS_RESUME_TEMPLATE"])
        self.assertEqual("No conversation found with session ID", env["THESEUS_RESUME_MISSING"])
        self.assertEqual(["Autocompact is thrashing"], profiles.stall_patterns(conf, "gateway"))
        self.assertEqual([], profiles.stall_patterns(conf, "codex"))
        self.assertEqual("codex", profiles.instance_env(conf, "codex", "w")["THESEUS_AGENT_LAUNCH"])


class TaskRuleTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        self.conf = cfg.load(root=self.project)
        self.queue = t.Queue(self.conf)

    def add(self, task_id, **overrides):
        argv = dict(role="implementation", owned=["src/x.py", f"docs/investigations/{task_id}.md"], objective="Do it.",
                    contract="All variants.", accept=["It passes."], depends=(), milestone=None)
        argv.update(overrides)
        return t.add(self.queue, task_id, **argv)

    def exported_problems(self, change=None):
        """The rules' problems after change(document) edits the exported tasks.json and the edit is imported (the
        service would refuse such records, so they arrive the way a hand edit before the cutover would)."""
        if change:
            t.export(self.queue)
            document = json.loads(self.conf.tasks_file.read_text())
            change(document)
            self.conf.tasks_file.write_text(json.dumps(document, indent=2) + "\n")
            t.import_(self.queue, replace=True)
        return rules.task_record_problems(self.conf)

    def test_add_refuses_bad_records_and_leaves_the_queue_unchanged(self):
        self.add("first-task")
        before = self.conf.tasks_file.read_text()
        cases = {
            "already exists": dict(task_id="first-task"),
            "kebab-case": dict(task_id="Bad_Id"),
            "report under": dict(task_id="no-report", owned=["src/x.py"]),
            "unknown dependencies": dict(task_id="bad-dep", depends=["missing"]),
            "acceptance items": dict(task_id="too-many", accept=[f"item {n}" for n in range(7)]),
            "record is": dict(task_id="too-big", objective="x" * t.TASK_RECORD_LIMIT),
            "empty value": dict(task_id="blank", accept=["  "]),
        }
        for message, argv in cases.items():
            task_id = argv.pop("task_id")
            with self.subTest(message=message), self.assertRaises(t.TaskError) as caught:
                self.add(task_id, **argv)
            self.assertIn(message, str(caught.exception))
        self.assertEqual(before, self.conf.tasks_file.read_text())

    def test_rules_flag_broken_records(self):
        self.add("a-task")
        self.add("b-task", depends=["a-task"])
        self.assertEqual([], self.exported_problems())

        def break_records(document):
            document["tasks"][0]["dependencies"] = ["b-task"]
            document["tasks"][1]["status"] = "merged"
            document["tasks"][1]["owner"] = "w"
            document["tasks"].append({"id": "blocked", "status": "pending", "owner": None, "objective": "x",
                                      "ownedPaths": ["docs/investigations/blocked.md"], "acceptance": ["x"],
                                      "externalBlocker": {"evidence": "x"}, "result": None})
        problems = "\n".join(self.exported_problems(break_records))
        self.assertIn("dependency cycle", problems)
        self.assertIn("merged without a merge commit", problems)
        self.assertIn("needs evidence and unblockCondition", problems)

    def test_milestone_and_capacity_rules(self):
        self.add("m1-task", milestone="M1")
        t.set_milestone(self.queue, "M0")
        self.assertEqual([], t.ready(self.queue))
        t.assign(self.queue, "m1-task", "w", "claude-code", "m", "/nowhere", "abc", "/nowhere/.theseus", force=True)
        self.assertIn("outside the current milestone", "\n".join(self.exported_problems()))
        t.set_milestone(self.queue, "M1")
        self.assertEqual([], self.exported_problems())
        self.set_config("[workers.claude-code]\ncapacity = 0\n")
        self.assertIn("exceed workers.claude-code.capacity", "\n".join(rules.task_record_problems(cfg.load(root=self.project))))

    def test_evidence_levels_are_separate(self):
        self.add("e-task")
        t.assign(self.queue, "e-task", "w", "cborg", "m", "/nowhere", "abc", "/nowhere/.theseus")
        with self.assertRaises(t.TaskError):
            t.observe(self.queue, "e-task", "too early")
        t.mark_merged(self.queue, "e-task", "abc123")
        self.assertEqual("merged", t.evidence_level(self.queue.get("e-task")))
        t.observe(self.queue, "e-task", "ran it")
        self.assertEqual("observed", t.evidence_level(self.queue.get("e-task")))
        t.observe(self.queue, "e-task", "user ran the camera workflow", level="userTested")
        self.assertEqual("user-tested", t.evidence_level(self.queue.get("e-task")))

    def test_scratch_script_references_are_rejected(self):
        self.write(".agents/skills/theseus-worker/notes.md", "Run /tmp/helper/decode.py first.\nOr ${THESEUS_SCRATCH}/x.sh\n")
        problems = rules.scratch_script_references(self.conf)
        self.assertEqual(2, len(problems), problems)
        self.assertEqual([], rules.scratch_script_references(self.conf, files=[self.project / "AGENTS.md"]))


class OwnershipPatternTests(unittest.TestCase):
    def test_patterns_conflict_only_when_some_path_matches_both(self):
        pairs = [("scripts/**/Tests/**", "scripts/Loot/Pickup/X.cs", False),
                 ("scripts/**/Tests/**", "scripts/A/B/Tests/x.cs", True),
                 ("scripts/**/Tests/**", "scripts/Tests/x.cs", True),
                 ("src/", "src/x.py", True), ("src", "src/x.py", True), ("src/x.py", "src/y.py", False),
                 ("src/*.cs", "src/a/b.cs", False), ("src/*.cs", "src/a.cs", True),
                 ("src/a*", "src/*b", True), ("src/a*", "src/b*", False),
                 ("docs/investigations/a.md", "docs/investigations/ab.md", False),
                 ("src/**/x.py", "src/a/**/y.py", False), ("src/**/x.py", "src/a/**", True),
                 ("a/*/c", "a/b/*", True), ("a/*/c", "a/b/d/c", False), ("src/**", "src", True)]
        for a, b, expected in pairs:
            with self.subTest(a=a, b=b):
                self.assertEqual(expected, t.patterns_intersect(a, b))
                self.assertEqual(expected, t.patterns_intersect(b, a))


class RepointTests(Sandbox):
    def test_links_to_archived_reports_are_repointed_everywhere(self):
        from theseus import integrate
        root = self.tmp / "repo"
        self.write("docs/project/archive/investigations/old-report.md", "# Old\n", root=root)
        self.write("docs/protocol/spec.md", "See [it](../investigations/old-report.md#2-x) and `docs/investigations/old-report.md`.\n", root=root)
        self.write("docs/investigations/live.md", "Live [r](live.md) and [o](old-report.md)?\n", root=root)
        changed = integrate.repoint_folded_links(root, [])
        self.assertEqual(["docs/protocol/spec.md"], changed)
        text = (root / "docs/protocol/spec.md").read_text()
        self.assertIn("(../project/archive/investigations/old-report.md#2-x)", text)
        self.assertIn("`docs/project/archive/investigations/old-report.md`", text)


class CleanupTests(Sandbox):
    def test_old_unreferenced_scratch_is_removed_and_referenced_is_kept(self):
        self.scaffold()
        conf = cfg.load(root=self.project)
        old, referenced, fresh = (conf.scratch / name for name in ("old-run", "kept-run", "fresh-run"))
        for path in (old, referenced, fresh):
            self.write("log.txt", "evidence\n", root=path)
        stamp = time.time() - 3 * 86400
        for path in (old, referenced):
            for item in (path, path / "log.txt"):
                os.utime(item, (stamp, stamp))
        t.add(t.Queue(conf), "x", "research", ["docs/investigations/x.md"], f"see {referenced}", "All.", ["x"])
        dry = "\n".join(cleanup.run(conf))
        self.assertIn("would rm old-run", dry)
        self.assertTrue(old.exists())
        applied = "\n".join(cleanup.run(conf, apply=True))
        self.assertIn("remove old-run", applied)
        self.assertIn("keep     kept-run (referenced by an open task)", applied)
        self.assertIn("keep     fresh-run (modified recently)", applied)
        self.assertFalse(old.exists())
        self.assertTrue((conf.data / "archive/scratch/old-run/log.txt").exists())



class ContextTests(Sandbox):
    def test_context_use_prefers_the_transcript_and_its_model_window_then_the_pane_hint(self):
        from types import SimpleNamespace
        from unittest import mock
        from theseus import agents, profiles
        table = {"context": {"claude-*": 1000000, "claude-haiku-4-5*": 200000, "lbl/cborg-coder-ultra": 229376}}
        conf = SimpleNamespace(section=lambda name: table if name == "models" else {}, root=self.tmp)
        with mock.patch.object(profiles, "resolve", lambda conf, name: self.fail("the profile is never consulted")):
            self.assertEqual({"tokens": 5, "limit": 200000, "limitSource": "models", "model": "claude-haiku-4-5",
                              "source": "transcript"},
                             agents.context_use(conf, "p", {"tokens": 5, "model": "claude-haiku-4-5"}, 9))
            limit = lambda model: agents.context_use(conf, "p", {"tokens": 5, "model": model}, None)["limit"]
            self.assertEqual([1000000, 229376, None], [limit(m) for m in ("claude-opus-5-5", "lbl/cborg-coder-ultra", "other")],
                             "a model with no known window has none")
            self.assertEqual({"tokens": 9, "limit": None, "limitSource": None, "model": None, "source": "pane hint"},
                             agents.context_use(conf, "p", {"tokens": None}, 9))
            harness = agents.context_use(conf, "p", {"tokens": 5, "model": "claude-opus-5-5", "limit": 922000}, None)
            self.assertEqual((922000, "harness"), (harness["limit"], harness["limitSource"]),
                             "the CLI's own window for the model wins over [models] context and the profile")
            self.assertIsNone(agents.context_use(conf, "p", None, None))


class RedactionTests(Sandbox):
    def test_a_saved_screen_is_redacted_and_private(self):
        from theseus import supervisor
        screen = ("> run it\nusing api_key=abc123secret for someone@example.org\n"
                  "token sk-abcdefghijklmnop and hash 3f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c\n  indented line kept\n")
        path = self.tmp / "crash-1.txt"
        supervisor.save_screen(path, screen)
        text = path.read_text()
        for secret in ("abc123secret", "someone@example.org", "sk-abcdefghijklmnop", "3f9a8b7c6d5e"):
            self.assertNotIn(secret, text)
        self.assertIn("api_key=[redacted]", text)
        self.assertIn("[email]", text)
        self.assertEqual(5, len(text.split("\n")), "lines are kept")
        self.assertIn("\n  indented line kept\n", text)
        self.assertEqual(0o600, path.stat().st_mode & 0o777)


class LongRunningQueueTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()

    def test_a_running_queue_rereads_changed_config_and_never_retires_an_agent_added_since(self):
        from theseus import supervisor, workqueue as wq
        conf = cfg.load(root=self.project)
        conf, seen = wq.reloaded(conf, None)
        self.assertIs(conf, wq.reloaded(conf, seen)[0], "unchanged files keep the configuration")
        self.write("theseus.local.toml", "[agents]\nmanaged = true\n\n[agents.counts]\nclaude-code = 1\n")
        old = conf
        conf, seen = wq.reloaded(conf, seen)
        self.assertEqual({"claude-code": 1}, supervisor.counts(conf))
        self.assertEqual({}, supervisor.counts(old))
        self.assertEqual({"demo-port-claude-code-1"}, supervisor.configured_now(old),
                         "reconcile asks the current files before retiring")

    def test_down_all_pauses_supervision_until_agents_up(self):
        from unittest import mock
        from theseus import supervisor
        self.write("theseus.local.toml", "[agents]\nmanaged = true\n\n[agents.counts]\nclaude-code = 2\n")
        conf = cfg.load(root=self.project)
        with mock.patch.object(supervisor, "records", return_value={}):
            self.assertEqual("paused", supervisor.down(conf)[0][1])
        with mock.patch.object(supervisor, "server", side_effect=AssertionError("reconcile must start nothing")):
            self.assertEqual([("-", "paused")], [row[:2] for row in supervisor.reconcile(conf)])
        self.assertTrue(json.loads(self.theseus("agents", "ls", "--json").stdout)["paused"])
        with mock.patch.object(supervisor, "records", return_value={}):
            supervisor.down(conf, ["x"])
        self.assertTrue(supervisor.paused(conf), "down NAME leaves the pause alone")
        self.theseus("agents", "up", "--dry-run")
        self.assertTrue(supervisor.paused(conf), "a dry run resumes nothing")
        supervisor.set_paused(conf, False)
        self.assertFalse(supervisor.paused(conf))

    def test_down_force_on_a_busy_agent_kills_it_without_waiting_for_idle(self):
        from unittest import mock
        from theseus import supervisor
        rec = {"name": "a1", "managed": True, "target": "=demo-port--a1:", "pane": "=demo-port--a1:"}
        server = mock.Mock()
        with mock.patch.object(supervisor, "records", return_value={"a1": rec}), \
                mock.patch.object(supervisor, "current_task", return_value={"id": "t1"}), \
                mock.patch.object(supervisor, "busy", return_value=True), \
                mock.patch.object(supervisor, "server_of", return_value=server), \
                mock.patch.object(supervisor, "update"), \
                mock.patch.object(supervisor, "stop", return_value=(True, "killed")) as stop:
            self.assertEqual([("a1", "stopped", "killed")], supervisor.down(cfg.load(root=self.project), ["a1"], force=True))
        stop.assert_called_once_with(mock.ANY, rec, graceful=False, reason="down", grace=5)

    def test_a_stop_refused_as_busy_leaves_the_run_to_end_as_a_crash(self):
        from unittest import mock
        from theseus import store, supervisor
        conf = cfg.load(root=self.project)
        with store.for_project(conf) as conn:
            store.upsert_agent(conn, conf.name, "a1", pane="=demo-port--a1:", profile="claude-code", source="supervisor")
            run = store.add_run(conn, conf.name, "a1", "start", profile="claude-code")
        rec = {"name": "a1", "managed": True, "target": "=demo-port--a1:", "pane": "=demo-port--a1:"}
        with mock.patch.object(supervisor, "server_of"), \
                mock.patch.object(supervisor, "pane_info", return_value={"pane_dead": "0", "pane_pid": "1"}), \
                mock.patch.object(supervisor, "at_prompt", return_value=True), \
                mock.patch.object(supervisor, "wait_idle", return_value=False):
            self.assertEqual((False, "still busy after 180s"), supervisor.stop(conf, rec, reason="down"))
        with store.for_project(conf) as conn:
            self.assertIsNone(store.run_of(conn, conf.name, "a1", run["gen"])["stop_reason"])

    def test_reconcile_after_down_and_scale_keeps_live_agents_and_stops_the_newest(self):
        from unittest import mock
        from theseus import supervisor
        self.write("theseus.local.toml", "[agents]\nmanaged = true\n\n[agents.counts]\ngw = 2\n")
        conf = cfg.load(root=self.project)
        name = lambda n: supervisor.template(conf).format(project=conf.name, profile="gw", n=n)
        regs = {name(n): {"name": name(n), "managed": True, "profile": "gw", "slot": n, "enabled": True,
                          "desired": "down" if n == 2 else "up"} for n in range(1, 5)}
        regs[name(5)] = {"name": name(5), "managed": True, "profile": "gw", "slot": 5, "enabled": False,
                         "desired": "up"}
        server = mock.Mock()
        server.list_panes.return_value = []
        one = lambda conf, srv, slot, rec, *rest: ("down", "") if rec["desired"] == "down" else ("kept", "")
        with mock.patch.object(supervisor, "server", return_value=server), \
                mock.patch.object(supervisor, "records", return_value=regs), \
                mock.patch.object(supervisor, "close_lost", return_value=set()), \
                mock.patch.object(supervisor, "current_task", return_value=None), \
                mock.patch.object(supervisor, "last_run", side_effect=lambda c, n: {"started_epoch": int(n[-1])}), \
                mock.patch.object(supervisor, "reconcile_one", side_effect=one), \
                mock.patch.object(supervisor, "retire", return_value=("removed", "")) as retire:
            actions = {n: a for n, a, _ in supervisor.reconcile(conf)}
        self.assertEqual({name(1): "kept", name(2): "down", name(3): "kept", name(4): "removed", name(5): "kept"},
                         actions, "a disabled agent runs on uncounted")
        retire.assert_called_once_with(conf, server, regs[name(4)], False)


class DriftTests(Sandbox):
    def test_a_model_change_is_drift_that_names_the_model_and_the_env_built_from_it(self):
        from theseus import store, supervisor
        self.scaffold()
        local = ('[agents]\nmanaged = true\n\n[agents.counts]\ngw = 1\n\n[workers.gw]\npreset = "claude-code"\n'
                 'model = "{model}"\nenv = {{ AGENT_MODEL = "{{model}}", TOKEN = "t" }}\n')
        self.write("theseus.local.toml", local.format(model="m-1"))
        conf = cfg.load(root=self.project)
        name = "demo-port-gw-1"
        spec = supervisor.launch(conf, "gw", name)[3]
        with store.for_project(conf) as conn:
            store.upsert_agent(conn, conf.name, name, pane="=demo-port--gw-1:", profile="gw", source="supervisor")
            store.update_agent(conn, conf.name, name, managed=True)
            run = store.add_run(conn, conf.name, name, "start", profile="gw", launch_hash=spec["hash"])
        supervisor.save_spec(conf, name, run["gen"], spec)
        self.assertEqual([], supervisor.drift(conf, supervisor.record(conf, name)))
        self.assertIn("no drifted agents", self.theseus("agents", "restart", "--drifted").stdout)
        self.write("theseus.local.toml", local.format(model="m-2"))
        conf = cfg.load(root=self.project)
        self.assertEqual(["model m-1 -> m-2", "env AGENT_MODEL"], supervisor.drift(conf, supervisor.record(conf, name)))
        self.assertIn("drift: model m-1 -> m-2; env AGENT_MODEL", self.theseus("agents", "ls").stdout)
        refused = self.theseus("agents", "restart", name, "--drifted", check=False)
        self.assertIn("usage:", refused.stderr)


class CountPlanTests(unittest.TestCase):
    """[agents.counts] as live agents per profile: supervisor.plan over {slot: record}."""

    @staticmethod
    def members(*live, **others):
        found = {n: {"desired": "up", "enabled": True} for n in live}
        found.update({int(k[1:]): v for k, v in others.items()})
        return found

    def test_scale_down_picks_free_agents_before_busy_ones_then_the_newest(self):
        from theseus import supervisor
        kept, starts, victims = supervisor.plan(1, self.members(1, 2, 3), holds=lambda n: n == 3, started=lambda n: n)
        self.assertEqual(([3], [], [1, 2]), (kept, starts, victims))
        self.assertEqual(([1, 3], [], [4]), supervisor.plan(2, self.members(1, 3, 4), started=lambda n: 9 if n == 1 else n,
                                                           stopping=lambda n: n == 4), "a victim already stopping stays one")

    def test_scale_up_takes_the_lowest_unused_or_removed_name_never_a_downed_one(self):
        from theseus import supervisor
        members = self.members(3, n2={"desired": "down"})
        self.assertEqual([1, 4], supervisor.plan(3, members)[1])
        self.assertEqual([4], supervisor.plan(2, members, taken=lambda n: n == 1)[1], "a name of an extra is not free")

    def test_a_deferred_victim_comes_back_before_a_new_name(self):
        from theseus import supervisor
        members = self.members(1, n2={"desired": "retire", "enabled": False})
        self.assertEqual(([1], [2], []), supervisor.plan(2, members))
        self.assertEqual(([1], [], []), supervisor.plan(1, members))

    def test_the_field_case_with_or_without_a_reconcile_between_down_and_scale(self):
        from theseus import supervisor
        downed = self.members(1, 3, 4, n2={"desired": "down"})
        self.assertEqual(([1, 3], [], [4]), supervisor.plan(2, downed, started=lambda n: n))
        self.assertEqual([5], supervisor.plan(4, downed)[1])
        replaced = self.members(1, 3, 4, 5, n2={"desired": "down"})
        self.assertEqual(([1, 3], [], [4, 5]), supervisor.plan(2, replaced, started=lambda n: n))


class GitLockRetryTests(unittest.TestCase):
    def test_a_held_index_lock_is_retried_then_succeeds(self):
        import subprocess
        from unittest import mock
        from theseus import gitutil
        locked = subprocess.CompletedProcess([], 128, "", "fatal: Unable to create '.git/index.lock': File exists.")
        done = subprocess.CompletedProcess([], 0, "ok", "")
        with mock.patch.object(gitutil.subprocess, "run", side_effect=[locked, locked, done]) as run, \
                mock.patch.object(gitutil.time, "sleep"):
            self.assertEqual("ok", gitutil.git("/repo", "commit", "-m", "x").stdout)
        self.assertEqual(3, run.call_count)

if __name__ == "__main__":
    unittest.main()


class CoordinatorFrictionTests(Sandbox):
    def test_the_manifest_reads_a_deletion_written_either_way_round(self):
        worktree = self.tmp / "wt"
        self.write(".theseus/changed.sha256", f"docs/old report.md  DELETED\nDELETED  tools/x.sh\n{'a' * 64}  src/y.c\n"
                   f"src/z.c {'b' * 64}\nD  gone.txt\n", root=worktree)
        self.assertEqual([("DELETED", "docs/old report.md"), ("DELETED", "tools/x.sh"), ("a" * 64, "src/y.c"),
                          ("b" * 64, "src/z.c"), ("DELETED", "gone.txt")], t.manifest_entries(worktree))
        self.assertEqual(["src/y.c", "src/z.c"], t.manifest_mismatches(worktree), "deleted files are absent, as listed")

    def test_archiving_a_worker_log_keeps_dangling_symlinks_as_links(self):
        worktree = self.tmp / "wt"
        self.write(".theseus/handoff.md", "Result: delivered\n", root=worktree)
        (worktree / ".theseus/build").mkdir()
        os.symlink("/proc/999999999/fd/3", worktree / ".theseus/build/stale")
        self.assertIsNone(t.archive_worker_log(worktree, self.tmp / "archive"))
        self.assertTrue((self.tmp / "archive/.theseus/handoff.md").is_file())
        self.assertEqual("/proc/999999999/fd/3", os.readlink(self.tmp / "archive/.theseus/build/stale"))

    def test_an_administrative_completion_without_a_worker_needs_no_owner(self):
        from types import SimpleNamespace
        from unittest import mock
        from theseus import taskservice
        base = {"status": "completed", "objective": "o", "ownedPaths": ["docs/investigations/a.md"], "acceptance": ["a"],
                "result": {"summary": "closed"}, "owner": None}
        archived = [{**base, "id": "never-assigned"}, {**base, "id": "worked", "execution": {"logDir": "/x"}}]
        live = mock.Mock(return_value=({"tasks": []}, archived))
        conf = SimpleNamespace(section=lambda name: {}, root=self.tmp)
        with mock.patch.object(taskservice, "open", lambda conf: SimpleNamespace(live=live)):
            problems = [p for p in rules.task_record_problems(conf) if "owner" in p]
        self.assertEqual(["worked: completed without owner or result"], problems)

    def test_dispatch_is_stopped_only_by_its_own_or_global_rule_problems(self):
        from types import SimpleNamespace
        from theseus import dispatch
        queue = SimpleNamespace(index=lambda: {"mine": {}, "legacy": {}, "old-done": {}})
        problems = ["legacy: more than 6 acceptance items", "old-done: completed without owner or result",
                    "mine: empty acceptance", "dependency cycle: a -> b -> a"]
        self.assertEqual(["mine: empty acceptance", "dependency cycle: a -> b -> a"],
                         dispatch.scoped_problems(problems, "mine", queue))

    def test_a_dirty_prefix_is_a_file_name_glob_everywhere(self):
        from types import SimpleNamespace
        from theseus import rooms
        conf = SimpleNamespace(section=lambda name: {"dirty_paths": ["docs/investigations/re-", "docs/re/", "a/b.md"]})
        self.assertEqual(["docs/investigations/re-*", "docs/re/", "a/b.md"], rooms.dirty_globs(conf))
        self.assertEqual(["/*", "!/docs/investigations/re-*", "!/docs/re/", "!/a/b.md"], rooms.sparse_patterns(conf))
        self.assertFalse(t.patterns_intersect("docs/investigations/research-notes.md", "docs/investigations/re-*"))
        self.assertTrue(t.patterns_intersect("docs/investigations/re-camera.md", "docs/investigations/re-*"))

    def test_a_gateway_pause_needs_the_coordinator_only_while_it_holds_work_back(self):
        from types import SimpleNamespace
        from unittest import mock
        from theseus import attention, gateway
        conf = SimpleNamespace(name="idle-port")
        health = [{"pool": "cborg", "pausedUntil": 2e9, "reason": "3 errors", "capCut": 0, "lastError": 1000.0,
                   "sources": ["other-port/worker-1"]}]
        idle = [{"name": "w1", "task": None}]
        with mock.patch.object(gateway, "health", lambda now: health), \
                mock.patch.object(gateway, "pool_key", lambda conf, profile: "cborg"), \
                mock.patch.object(attention.wq, "launched", lambda conf, info: "p"):
            self.assertEqual([], attention.gateway_items(conf, idle, [], 2000.0, tasks=[]))
            self.assertEqual([], attention.gateway_items(conf, idle, [], 2000.0, tasks=[{"status": "pending", "hold": 1}]))
            [paused] = attention.gateway_items(conf, idle, [{"kind": "gateway_paused", "epoch": 1.0}], 2000.0,
                                               tasks=[{"status": "pending"}])
            self.assertEqual(1000.0, paused["since"], "its age is the pool's latest error, not an old pause of this project")
            self.assertIn("errors from other-port/worker-1", paused["summary"])
            working = [{"name": "w1", "task": "t1"}]
            self.assertEqual(1, len(attention.gateway_items(conf, working, [], 2000.0, tasks=[])))

    def test_completing_from_a_verify_log_needs_a_commit_that_holds_the_merge(self):
        from types import SimpleNamespace
        from theseus import verify
        repo = self.tmp / "repo"
        repo.mkdir()
        run = lambda *args: __import__("subprocess").run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                                                         text=True).stdout.strip()
        run("init", "-q")
        run("commit", "-q", "--allow-empty", "-m", "parent")
        parent = run("rev-parse", "HEAD")
        run("commit", "-q", "--allow-empty", "-m", "merge")
        merged = run("rev-parse", "HEAD")
        log = self.write("verify.log", f"{verify.LOG_HEADER}{parent}\nfinal status 0\n")
        queue = SimpleNamespace(root=repo, get=lambda task_id: {"status": "merged", "merge": {"commit": merged[:12]}})
        with self.assertRaisesRegex(t.TaskError, "does not contain"):
            t.complete(queue, "a-task", "done", from_log=log)

    def test_a_command_that_returns_a_failure_status_exits_with_it(self):
        import argparse
        from unittest import mock
        from theseus import cli
        parser = argparse.ArgumentParser()
        parser.set_defaults(func=lambda args: 3, command="x")
        with mock.patch.object(cli, "build_parser", lambda: parser), self.assertRaises(SystemExit) as raised:
            cli.main([])
        self.assertEqual(3, raised.exception.code)
        parser.set_defaults(func=lambda args: None)
        with mock.patch.object(cli, "build_parser", lambda: parser):
            self.assertIsNone(cli.main([]))

    def test_folding_refuses_a_report_an_open_task_owns(self):
        from types import SimpleNamespace
        from unittest import mock
        from theseus.docs import fold
        root = self.tmp / "proj"
        moves = {root / "docs/investigations/owned.md": root / "x", root / "docs/investigations/free.md": root / "y"}
        queue = SimpleNamespace(load=lambda: {"tasks": [{"id": "open-task", "ownedPaths": ["docs/investigations/owned.md"]}]})
        with mock.patch.object(t, "Queue", lambda conf: queue):
            [problem] = fold.owned_by_open_tasks(None, root, moves)
        self.assertIn("docs/investigations/owned.md is owned by open task(s) open-task", problem)


class ConsoleCellTests(unittest.TestCase):
    def test_levels_are_dots_of_two_levels_and_resources_come_after_the_pool(self):
        from theseus.tui import rows
        self.assertEqual(["◐○○", "●○○", "●◐○", "●●○", "●●◐", "●●●", "●●●+"], [rows.pips(n) for n in range(1, 8)])
        self.assertEqual("●◐○", rows.level_cell(3, minimum=True).plain, "a task's minimum uses the same scale")
        state = {"capabilities": {"hard-task": 4}}
        self.assertEqual(["●●○ hard-task", "◐○○ plain-task", "-"],
                         [rows.task_cell(task, state).plain for task in ("hard-task", "plain-task", None)],
                         "a task with no minimum shows the lowest level")
        self.assertEqual(1, t.min_capability({}))
        self.assertEqual(3, t.min_capability({"minCapability": 3}))
        columns = rows.AGENT_COLUMNS
        self.assertEqual(("pool", "CPU", "RAM"), columns[columns.index("pool"):columns.index("pool") + 3])
        self.assertNotIn("session", columns)
        self.assertEqual(list(columns), list(rows.WIDTHS["agents"]))
        self.assertEqual(("608M", "1.2G"), (rows.memory(608 * 2**20, 0).plain, rows.memory(int(1.25 * 2**30), 2**34).plain))
        self.assertEqual(["1M", "229k"], [rows.tokens(1048576), rows.tokens(229376)])
        self.assertEqual("87k/n/a", rows.context({"tokens": 87000, "limit": None}).plain, "an unknown window is not guessed")
        self.assertIn("44k/229k", rows.context({"tokens": 44000, "limit": 229376}).plain)
        state = {"agents": [{"name": "w", "pane": "%1", "state": "free"}], "workers": [{"name": "h", "kind": "headless",
                 "task": "t"}], "events": []}
        self.assertEqual([len(columns)] * 2, [len(cells) for _, cells, _ in rows.agent_rows(state)])


class ClosureTests(Sandbox):
    def test_close_refuses_while_agents_hold_tasks_unless_forced(self):
        from types import SimpleNamespace
        from unittest import mock
        from theseus import closure
        conf = SimpleNamespace(name="demo", data=self.tmp / "data", root=self.tmp)
        with mock.patch.object(closure, "holders", lambda conf: [("demo-w-1", "parse-header")]):
            with self.assertRaisesRegex(closure.ClosedError, r"demo-w-1 \(parse-header\).*--force"):
                closure.close(conf)
        self.assertIsNone(closure.closed(conf))
        (self.tmp / "data").mkdir()
        (self.tmp / "data/closed.json").write_text('{"closed": "2026-11-19T12:00:00", "reason": "done"}')
        with self.assertRaisesRegex(closure.ClosedError, "demo is closed .*theseus reopen"):
            closure.refuse_if_closed(conf, "`agents up`")


class ProfileFileTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.host = self.tmp / "host-config.toml"
        os.environ["THESEUS_CONFIG"] = self.env["THESEUS_CONFIG"] = str(self.host)
        self.addCleanup(os.environ.pop, "THESEUS_CONFIG", None)

    def test_add_show_edit_and_remove_a_host_profile_checked_at_every_step(self):
        from theseus import profileedit as pe
        self.host.write_text("# my profiles\n[workers.kept]\npreset = \"claude-code\"\n")
        file, line = pe.add(pe.HostConfig(), "gemini-argon", "agy", {"model": "gemini-4-argon-high", "capability": 3,
                                                                     "extra_args": ["--effort", "high"]})
        self.assertEqual((self.host, 5), (file, line))
        conf = pe.HostConfig()
        row = next(r for r in pe.rows(conf) if r["name"] == "gemini-argon")
        self.assertEqual(("antigravity", "gemini-4-argon-high", 3, "host"),
                         (row["preset"], row["model"], row["capability"], row["defined"]))
        shown = {key: source for key, _, source in pe.detail(conf, "gemini-argon")}
        self.assertEqual("host", shown["workers.gemini-argon.model"])
        self.assertEqual("package (preset antigravity)", shown["workers.gemini-argon.command"])
        with self.assertRaisesRegex(pe.ProfileFileError, "already has"):
            pe.add(conf, "gemini-argon", "agy")
        with self.assertRaisesRegex(pe.ProfileFileError, "unknown preset"):
            pe.add(conf, "x", "nope")
        with self.assertRaisesRegex(pe.ProfileFileError, "not a profile name"):
            pe.add(conf, "has space", "agy")

        opened = []
        def run(argv):
            opened.append(argv[-2:])
            text = self.host.read_text()
            self.host.write_text(text.replace("argon-high", "argon-medium") if len(opened) == 1 else text)
            return 0
        os.environ["EDITOR"] = "myeditor --wait"
        self.addCleanup(os.environ.pop, "EDITOR", None)
        self.assertIn("saved and valid", pe.edit(self.host, "gemini-argon", ask=lambda _: "e", run=run))
        self.assertEqual([["+5", str(self.host)]], opened, "the editor opens at the profile's section")
        self.assertIn('model = "gemini-4-argon-medium"', self.host.read_text())

        answers = iter(["e", "r"])
        broken = lambda argv: self.host.write_text(self.host.read_text().replace("capability", "capabillity")) or 0
        good = self.host.read_text()
        self.assertIn("reverted", pe.edit(self.host, "gemini-argon", ask=lambda _: next(answers), run=broken))
        self.assertEqual(good, self.host.read_text(), "an invalid save is reverted on request")

        self.assertEqual(self.host, pe.remove(pe.HostConfig(), "gemini-argon"))
        self.assertEqual("# my profiles\n[workers.kept]\npreset = \"claude-code\"\n", self.host.read_text())
        with self.assertRaisesRegex(pe.ProfileFileError, "package preset"):
            pe.file_of(pe.HostConfig(), "codex")

    def test_a_context_limit_in_a_profile_is_accepted_but_ignored_with_a_warning(self):
        self.host.write_text('[workers.old]\npreset = "opencode"\ncontext_limit = 200000\n')
        checked = self.theseus("config", "check", cwd=self.tmp)
        self.assertEqual(0, checked.returncode)
        self.assertIn("[workers.old] context_limit is ignored", checked.stdout)

    def test_the_cli_lists_and_shows_profiles_outside_a_project(self):
        self.host.write_text('[workers.gemini-neon]\npreset = "antigravity"\nmodel = "gemini-4-neon-high"\n')
        listed = self.theseus("profiles", cwd=self.tmp).stdout
        self.assertIn("host profiles (outside a project)", listed)
        self.assertRegex(listed, r"gemini-neon\s+antigravity\s+gemini-4-neon-high")
        shown = json.loads(self.theseus("profiles", "show", "gemini-neon", "--json", cwd=self.tmp).stdout)
        self.assertEqual({"value": "gemini-4-neon-high", "source": "host"}, shown["workers.gemini-neon.model"])
        added = self.theseus("profiles", "add", "gemini-carbon", "--preset", "agy", "--model", "gemini-4-carbon-high",
                             "--args=--effort high", cwd=self.tmp)
        self.assertIn("added [workers.gemini-carbon]", added.stdout)
        self.assertIn('extra_args = ["--effort", "high"]', self.host.read_text())
        self.assertEqual(0, self.theseus("config", "check", cwd=self.tmp).returncode)
