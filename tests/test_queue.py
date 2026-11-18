"""Agent registry, work queue and dashboard with a fake tmux: panes are idle prompts whose typed input is logged and that
show the busy marker once after Enter, so assignments, resets and nudges can be observed without real sessions."""
import json
import os
import unittest

from helpers import Sandbox

FAKE_TMUX = """#!/usr/bin/env bash
case "$1" in
    display) case " $FAKE_PANES " in *" $4 "*) echo 1 ;; *) exit 1 ;; esac ;;
    capture-pane) cat "$FAKE_STATE/screen-$4" 2>/dev/null || echo "> "
        if [ -e "$FAKE_STATE/busy-$4" ]; then echo "working (esc to interrupt)"; rm -f "$FAKE_STATE/busy-$4"; fi ;;
    send-keys) if [ -e "$FAKE_STATE/gone-$3" ]; then echo "can't find session: $3" >&2; exit 1; fi
        printf '%s\\n' "$*" >> "$FAKE_STATE/typed"
        if [ "${!#}" = Enter ] && [ ! -e "$FAKE_STATE/stuck-$3" ]; then touch "$FAKE_STATE/busy-$3"; fi ;;
esac
"""


class QueueTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        self.state = self.tmp / "state"
        bin_dir = self.tmp / "bin"
        self.state.mkdir()
        bin_dir.mkdir()
        (bin_dir / "tmux").write_text(FAKE_TMUX)
        (bin_dir / "tmux").chmod(0o755)
        self.env.update({"PATH": f"{bin_dir}{os.pathsep}{self.env['PATH']}", "FAKE_STATE": str(self.state),
                         "FAKE_PANES": "%1 %2", "THESEUS_COMPACT_POLL": "0.01", "THESEUS_TYPE_POLL": "0.01",
                         "THESEUS_SEND_POLL": "0.01"})
        self.theseus("tasks", "milestone", "M1")
        self.git("commit", "-q", "-am", "milestone")
        self.theseus("agents", "add", "worker-a", "%1")

    def add(self, task_id, room=None, **extra):
        args = ["tasks", "add", task_id, "--role", "research", "--owned", f"docs/investigations/{task_id}.md",
                "--objective", "Do it.", "--contract", "All variants.", "--accept", "Done."]
        if room:
            args += ["--room", room]
        self.theseus(*args)
        if extra:
            path = self.project / "docs/project/tasks.json"
            document = json.loads(path.read_text())
            document["tasks"][-1].update(extra)
            path.write_text(json.dumps(document, indent=2) + "\n")
        self.git("commit", "-q", "-am", f"queue {task_id}")

    def typed(self):
        path = self.state / "typed"
        return path.read_text() if path.exists() else ""

    def deliver(self, task_id, text=None, settled=True):
        record = json.loads(self.theseus("tasks", "show", task_id).stdout)
        handoff = self.tmp / (record["execution"]["logDir"] + "/handoff.md")
        self.write(handoff, text if text is not None else f"Task: {task_id}\nResult: delivered\n", root=self.tmp)
        if settled:
            old = handoff.stat().st_mtime - 120
            os.utime(handoff, (old, old))
        return handoff

    def test_tick_assigns_delivers_resets_and_assigns_again(self):
        self.add("first-task")
        self.add("second-task")
        out = self.theseus("queue", "tick").stdout
        self.assertIn("assigned   worker-a", out)
        self.assertIn("first-task", out)
        self.assertIn("New task first-task", self.typed())
        self.assertEqual("in_progress", json.loads(self.theseus("tasks", "show", "first-task").stdout)["status"])
        self.assertNotIn("assigned", self.theseus("queue", "tick").stdout, "a working agent gets nothing new")
        self.deliver("first-task")
        out = self.theseus("queue", "tick").stdout
        self.assertIn("delivered  worker-a", out)
        self.assertIn("compacted  worker-a", out)
        self.assertIn("assigned   worker-a", out)
        self.assertIn("/compact Keep only", self.typed())
        self.assertIn("New task second-task", self.typed())
        self.assertEqual("verification", json.loads(self.theseus("tasks", "show", "first-task").stdout)["status"])
        self.assertIn("review", self.theseus("queue").stdout)
        from theseus import store
        with store.opened() as conn:
            samples = store.samples(conn, "demo-port", agent="worker-a")
            [delivery] = store.deliveries(conn, "demo-port")
        self.assertEqual([("free", None), ("working", "first-task"), ("free", None)],
                         [(s["state"], s["task"]) for s in samples])
        self.assertEqual(("first-task", "worker-a", "delivered"), (delivery["task"], delivery["agent"], delivery["result"]))

    def test_a_fresh_context_gets_one_restart_prompt_before_anything_else(self):
        self.replace_config("idle_minutes = 5", "idle_minutes = 0")
        self.git("commit", "-q", "-am", "instant stall")
        self.env["THESEUS_SEND_VERIFY"] = "0.1"
        self.add("long-task")
        self.theseus("queue", "tick")
        from theseus import config as cfg, store
        with store.opened() as conn:
            store.put_gateway_retry(conn, "demo-port", "worker-a", task="long-task", pool="claude-code", code=524,
                                    phase="waiting", next_epoch=1.0)
        hint = "> \n  new task? /clear to save 150.0k tokens\n"
        (self.state / "screen-%1").write_text(hint)
        (self.state / "stuck-%1").write_text("")
        out = self.theseus("queue", "tick").stdout
        self.assertIn("nudged", out)
        self.assertIn("send_failed", out, "the first restart prompt was not confirmed")
        with store.opened() as conn:
            self.assertEqual("resent", store.gateway_retry(conn, "demo-port", "worker-a")["phase"],
                             "the scheduled gateway resend is called off")
        self.theseus("queue", "tick")
        (self.state / "stuck-%1").unlink()
        out = self.theseus("queue", "tick").stdout
        self.assertRegex(out, r"nudged\s+worker-a\s+long-task restart prompt after an idle stop")
        (self.state / "screen-%1").write_text("> \n")
        self.assertIn("stalled", self.theseus("queue", "tick").stdout, "a stall after the prompt goes to the coordinator")
        texts = [line.split(" -l ", 1)[1] for line in self.typed().splitlines() if " -t %1 -l " in line]
        after = texts[texts.index("/clear") + 1:]
        self.assertEqual(3, len(after), after)
        self.assertTrue(all("Your context was cleared" in text for text in after), "nothing but the restart prompt")
        prompt = after[0]
        for expected in ("long-task", "Objective: Do it.", "docs/investigations/long-task.md", "prompt.md",
                         "read large files in line ranges".lower()):
            self.assertIn(expected.lower(), prompt.lower())
        conf = cfg.load(root=self.project)
        from theseus import workqueue as wq
        self.assertIsNone(wq.owed(conf, "worker-a"))

    def test_a_minimum_capability_limits_which_agents_take_it(self):
        (self.tmp / "config.toml").write_text('[workers.strong]\npreset = "claude-code"\ncapability = 3\n'
                                              '[workers.with-mcp]\npreset = "claude-code"\nmcp_servers = ["theseus"]\n')
        self.theseus("tasks", "add", "hard-task", "--role", "research", "--owned", "docs/investigations/hard-task.md",
                     "--objective", "Do it.", "--contract", "All variants.", "--accept", "Done.", "--min-capability", "2")
        self.git("commit", "-q", "-am", "queue hard-task")
        self.assertEqual(2, json.loads(self.theseus("tasks", "show", "hard-task").stdout)["minCapability"])
        self.assertNotIn("assigned", self.theseus("queue", "tick").stdout, "capability 1 is below the minimum")
        self.theseus("agents", "add", "worker-b", "%2", "--profile", "strong")
        self.assertRegex(self.theseus("queue", "tick").stdout, r"assigned\s+worker-b\s+hard-task")
        self.theseus("tasks", "release", "hard-task", "--reason", "test")
        self.theseus("tasks", "capability", "hard-task", "0")
        self.assertNotIn("minCapability", json.loads(self.theseus("tasks", "show", "hard-task").stdout))
        from theseus import config as cfg
        from theseus import workqueue as wq
        conf = cfg.load(root=self.project)
        weak, strong = {"name": "weak", "provider": "claude-code"}, {"name": "strong", "provider": "strong"}
        self.assertTrue(wq.fits(conf, weak, {"id": "x", "minCapability": 5, "profile": "claude-code"}, {})[0],
                        "exact profile routing wins")
        self.assertFalse(wq.fits(conf, strong, {"id": "x", "minCapability": 2, "profile": "with-mcp"}, {})[0],
                         "a relaunch onto a weaker compatible profile must still meet the minimum")

    def test_wait_returns_when_a_delivery_needs_the_coordinator(self):
        self.add("watched-task")
        self.theseus("queue", "tick")
        self.deliver("watched-task")
        result = self.theseus("queue", "wait", "--timeout", "20", "--interval", "1")
        self.assertIn("delivered", result.stdout)

    def test_an_assignment_failure_backs_off_that_task_and_the_queue_keeps_going(self):
        self.add("blocked-task")
        self.add("other-task")
        (self.tmp / "worktrees/worker-a-blocked-task").mkdir(parents=True)
        out = self.theseus("queue", "tick").stdout
        self.assertRegex(out, r"error\s+worker-a\s+blocked-task worktree already exists")
        out = self.theseus("queue", "tick").stdout
        self.assertRegex(out, r"assigned\s+worker-a\s+other-task")
        self.assertNotIn("error", out, "the failed task waits out its backoff")
        from theseus import config as cfg, workqueue
        self.assertEqual(1, len(workqueue.events(cfg.load(root=self.project), kinds=["error"])))
        self.assertEqual("pending", json.loads(self.theseus("tasks", "show", "blocked-task").stdout)["status"])

    def test_a_task_message_that_cannot_be_typed_rolls_the_assignment_back(self):
        self.add("lost-task")
        from theseus import config as cfg, workqueue
        workqueue.save_runtime(cfg.load(root=self.project), {"worker-a": {"needs_reset": False}})
        (self.state / "gone-%1").write_text("")
        out = self.theseus("queue", "tick").stdout
        self.assertRegex(out, r"error\s+worker-a\s+lost-task task message not submitted: .*can't find session")
        self.assertIn("lost-task released, its worktree removed", out)
        record = json.loads(self.theseus("tasks", "show", "lost-task").stdout)
        self.assertEqual("pending", record["status"])
        self.assertIn("task message not submitted", record["previousAttempts"][-1]["reason"])
        self.assertFalse((self.tmp / "worktrees/worker-a-lost-task").exists())
        self.assertNotIn("work/worker-a-lost-task", self.git("branch").stdout)
        self.assertEqual("", self.git("status", "--porcelain", "--untracked-files=no").stdout)


class WakeTests(Sandbox):
    """`theseus queue wait` sleeps until the earlier of its interval and the next pending wake time, so a gateway resend
    fires within a second or two of its scheduled time instead of on the next interval boundary."""

    def setUp(self):
        super().setUp()
        self.scaffold()
        from theseus import config, workqueue
        self.conf = config.load(root=self.project)
        self.wq = workqueue
        saved = workqueue.tick
        workqueue.tick = lambda conf, assign=True: []
        self.addCleanup(setattr, workqueue, "tick", saved)

    def test_wait_wakes_for_a_gateway_resend_before_the_interval_but_never_for_an_overdue_one(self):
        from theseus import store
        now = [1000.0]
        slept = []

        def sleep(seconds):
            slept.append(round(seconds, 2))
            now[0] += seconds

        with store.for_project(self.conf) as conn:
            store.put_gateway_retry(conn, self.conf.name, "worker-a", phase="waiting", next_epoch=1007.0, pool="p")
        status = self.wq.wait(self.conf, timeout=100, interval=60, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(1, status)
        self.assertEqual([7.5, 60], slept[:2])
        self.assertEqual(100, round(sum(slept), 2))
        with store.for_project(self.conf) as conn:
            store.put_gateway_retry(conn, self.conf.name, "worker-a", phase="waiting", next_epoch=900.0, pool="p")
        self.assertEqual(30.0, self.wq.next_sleep(self.conf, 30, 1000.0))
        self.assertEqual(5.0, self.wq.next_sleep(self.conf, 5, 1000.0))


class NewerStoreTests(Sandbox):
    """`queue wait` and `queue run` outlive a store upgrade: on a schema newer than they know they become the installed
    theseus with the same arguments, or wait for one that knows it."""

    def setUp(self):
        super().setUp()
        self.scaffold()
        from theseus import store
        with store.opened() as conn:
            conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 1}")
        self.fake = self.tmp / "installed-theseus"
        self.record = self.tmp / "exec-record"

    def installed(self, schema):
        self.fake.write_text(f"""#!/usr/bin/env bash
if [ "$1" = --schema-version ]; then echo {schema}; exit 0; fi
printf '%s\\n' "$*" > "{self.record}"
""")
        self.fake.chmod(0o755)
        return {**self.env, "THESEUS_INSTALLED": str(self.fake)}

    def test_wait_becomes_the_installed_theseus_with_its_arguments(self):
        from theseus import store
        result = self.theseus("queue", "wait", "--interval", "1", "--timeout", "5",
                              env=self.installed(store.SCHEMA_VERSION + 1))
        self.assertEqual("queue wait --interval 1 --timeout 5\n", self.record.read_text())
        self.assertIn(f"schema {store.SCHEMA_VERSION + 1}; continuing as", result.stderr)


class CodeChangeTests(Sandbox):
    def test_wait_restarts_itself_when_the_installed_code_changes(self):
        from unittest import mock
        from theseus import cli, upgrade
        self.scaffold()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.project)
        argv = ["queue", "wait", "--interval", "1", "--timeout", "5"]
        stamps = [(1, 1), (1, 2), (1, 2)]
        with mock.patch.object(upgrade, "code_stamp", side_effect=stamps), mock.patch("os.execv") as execv, \
                mock.patch("sys.argv", ["/bin/theseus"]):
            cli.main(argv)
        execv.assert_called_once()
        self.assertEqual(["/bin/theseus", *argv], execv.call_args[0][1][1:])


if __name__ == "__main__":
    unittest.main()
