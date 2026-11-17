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
