"""Configuration loading, task record rules and cleanup, exercised directly through the package."""
import contextlib
import io
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

    def test_unknown_sections_are_ignored_but_fail_config_check_and_roots_inside_the_project_are_rejected(self):
        self.scaffold()
        self.set_config("[surprise]\nx = 1\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotIn("surprise", cfg.load(root=self.project).raw)
        self.assertIn("unknown configuration sections: surprise", self.theseus("config", "check", check=False).stderr)
        self.replace_config("[surprise]\nx = 1\n", "")
        os.environ["THESEUS_DATA"] = str(self.project / "data")
        with self.assertRaises(cfg.ConfigError):
            cfg.load(root=self.project)

    def test_config_get_prints_resolved_values(self):
        self.scaffold()
        self.assertEqual(str(self.tmp / "data"), self.theseus("config", "get", "paths.data").stdout.strip())
        self.assertEqual("claude-code", self.theseus("config", "get", "workers.default").stdout.strip())
        self.assertIn("codex", self.theseus("agents", "profiles").stdout)
        self.theseus("config", "check")


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

    def mutate(self, function):
        document = self.queue.load()
        function(document)
        self.queue.save(document)

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
        self.assertEqual([], rules.task_record_problems(self.conf))

        def break_records(document):
            document["tasks"][0]["dependencies"] = ["b-task"]
            document["tasks"][1]["status"] = "merged"
            document["tasks"][1]["owner"] = "w"
            document["tasks"].append({"id": "blocked", "status": "pending", "owner": None, "objective": "x",
                                      "ownedPaths": ["docs/investigations/blocked.md"], "acceptance": ["x"],
                                      "externalBlocker": {"evidence": "x"}, "result": None})
        self.mutate(break_records)
        problems = "\n".join(rules.task_record_problems(self.conf))
        self.assertIn("dependency cycle", problems)
        self.assertIn("merged without a merge commit", problems)
        self.assertIn("needs evidence and unblockCondition", problems)

    def test_milestone_and_capacity_rules(self):
        self.add("m1-task", milestone="M1")
        self.mutate(lambda d: d.update(currentMilestone="M0"))
        self.assertEqual([], t.ready(self.queue))
        t.assign(self.queue, "m1-task", "w", "claude-code", "m", "/nowhere", "abc", "/nowhere/.theseus", force=True)
        self.assertIn("outside the current milestone", "\n".join(rules.task_record_problems(self.conf)))
        self.mutate(lambda d: d.update(currentMilestone="M1"))
        self.assertEqual([], rules.task_record_problems(self.conf))
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
        document = json.loads(conf.tasks_file.read_text())
        document["tasks"].append({"id": "x", "status": "pending", "owner": None, "objective": f"see {referenced}",
                                  "ownedPaths": ["docs/investigations/x.md"], "acceptance": ["x"], "result": None})
        conf.tasks_file.write_text(json.dumps(document))
        dry = "\n".join(cleanup.run(conf))
        self.assertIn("would rm old-run", dry)
        self.assertTrue(old.exists())
        applied = "\n".join(cleanup.run(conf, apply=True))
        self.assertIn("remove old-run", applied)
        self.assertIn("keep     kept-run (referenced by an open task)", applied)
        self.assertIn("keep     fresh-run (modified recently)", applied)
        self.assertFalse(old.exists())
        self.assertTrue((conf.data / "archive/scratch/old-run/log.txt").exists())


if __name__ == "__main__":
    unittest.main()


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

    def test_an_unpushed_record_says_to_push_it(self):
        from theseus import integrate
        base = self.git("rev-parse", "HEAD").stdout.strip()
        self.theseus("tasks", "add", "late-task", "--role", "research", "--owned", "docs/investigations/late-task.md",
                     "--objective", "Do it.", "--contract", "All variants.", "--accept", "Done.")
        self.git("commit", "-q", "-am", "queue late-task")
        self.write("theseus.local.toml", '[git]\nbase = "origin/main"\n')
        message = integrate.base_mismatch(cfg.load(root=self.project), "late-task", base)
        self.assertIn("committed locally but not at the assignment base origin/main", message)
        self.assertIn("`git push origin HEAD:main`", message)

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

    def test_scale_up_reuses_the_lowest_free_name_down_or_removed(self):
        from theseus import supervisor
        members = self.members(3, n2={"desired": "down"})
        self.assertEqual([1, 2], supervisor.plan(3, members)[1])
        self.assertEqual([4], supervisor.plan(2, {**members, 2: {"desired": "down"}}, holds=lambda n: n == 2,
                                              taken=lambda n: n == 1)[1], "a name of an extra or a task holder is not free")

    def test_a_deferred_victim_comes_back_before_a_new_name(self):
        from theseus import supervisor
        members = self.members(1, n2={"desired": "retire", "enabled": False})
        self.assertEqual(([1], [2], []), supervisor.plan(2, members))
        self.assertEqual(([1], [], []), supervisor.plan(1, members))

    def test_the_field_case_with_or_without_a_reconcile_between_down_and_scale(self):
        from theseus import supervisor
        downed = self.members(1, 3, 4, n2={"desired": "down"})
        self.assertEqual(([1, 3], [], [4]), supervisor.plan(2, downed, started=lambda n: n))
        self.assertEqual([2], supervisor.plan(4, downed)[1])
        refilled = self.members(1, 2, 3, 4)
        self.assertEqual(([1, 3], [], [2, 4]), supervisor.plan(2, refilled, started=lambda n: 9 if n == 2 else n))
