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
    send-keys) printf '%s\\n' "$*" >> "$FAKE_STATE/typed"
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

    def test_a_minimum_capability_limits_which_agents_take_it(self):
        (self.tmp / "config.toml").write_text('[workers.strong]\npreset = "claude-code"\ncapability = 3\n'
                                              '[workers.strong-mcp]\npreset = "claude-code"\ncapability = 3\n'
                                              'mcp_servers = ["theseus"]\n[workers.with-mcp]\npreset = "claude-code"\n'
                                              'mcp_servers = ["theseus"]\n')
        self.theseus("tasks", "add", "hard-task", "--role", "research", "--owned", "docs/investigations/hard-task.md",
                     "--objective", "Do it.", "--contract", "All variants.", "--accept", "Done.", "--min-capability", "2")
        self.git("commit", "-q", "-am", "queue hard-task")
        self.assertEqual(2, json.loads(self.theseus("tasks", "show", "hard-task").stdout)["minCapability"])
        self.assertNotIn("assigned", self.theseus("queue", "tick").stdout, "capability 1 is below the minimum")
        self.assertIn("needs capability >= 2", " ".join(self.theseus("top", "--once").stdout.split()))
        snapshot = json.loads(self.theseus("top", "--json").stdout)
        self.assertEqual({"hard-task": 2}, snapshot["gated"])
        self.theseus("agents", "add", "worker-b", "%2", "--profile", "strong")
        self.assertRegex(self.theseus("queue", "tick").stdout, r"assigned\s+worker-b\s+hard-task")
        self.theseus("tasks", "release", "hard-task", "--reason", "test")
        self.theseus("tasks", "capability", "hard-task", "0")
        self.assertNotIn("minCapability", json.loads(self.theseus("tasks", "show", "hard-task").stdout))
        from theseus import config as cfg, profiles
        from theseus import workqueue as wq
        conf = cfg.load(root=self.project)
        self.assertEqual((1, 3, 0), (profiles.capability(conf, "claude-code"), profiles.capability(conf, "strong"),
                                     profiles.capability(conf, "nope")))
        weak, strong = {"name": "weak", "provider": "claude-code"}, {"name": "strong", "provider": "strong"}
        cases = [({"minCapability": 2}, weak, False), ({"minCapability": 2}, strong, True),
                 ({"minCapability": 5, "profile": "claude-code"}, weak, True),
                 ({"minCapability": 2, "profile": "with-mcp"}, strong, False),
                 ({"minCapability": 2, "profile": "strong-mcp"}, strong, True),
                 ({"minCapability": 2, "profile": "strong-mcp"}, weak, False)]
        for task, agent, expected in cases:
            self.assertEqual(expected, wq.fits(conf, agent, {"id": "x", **task}, {})[0], (task, agent["name"]))
        self.assertTrue(wq.fits(conf, weak, {"id": "x", "minCapability": 2}, {}, gate=False)[0])
        self.assertTrue(profiles.relaunch_compatible(conf, "claude-code", "strong-mcp"), "capability is not a launch key")
        self.assertIn("capability must be a non-negative integer", " ".join(profiles.problems("x", {"capability": "high"})))
        from theseus.tui import commands
        state = {"agents": [], "queue": {"ready": ["hard-task"]}, "capabilities": {"hard-task": 2}, "project": "demo-port"}
        titles = [title for title, *_ in commands.project_commands({"snapshot": state})]
        self.assertIn("Raise the minimum capability of hard-task (2 to 3)", titles)
        self.assertIn("Lower the minimum capability of hard-task (2 to 1)", titles)

    def test_wait_returns_when_a_delivery_needs_the_coordinator(self):
        self.add("watched-task")
        self.theseus("queue", "tick")
        self.deliver("watched-task")
        result = self.theseus("queue", "wait", "--timeout", "20", "--interval", "1")
        self.assertIn("delivered", result.stdout)


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

    def test_wait_wakes_for_a_gateway_resend_before_the_interval(self):
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
        self.assertEqual(7.5, slept[0])
        self.assertEqual(60, slept[1])
        self.assertEqual(100, round(sum(slept), 2))

    def test_an_overdue_or_missing_wake_time_keeps_the_interval(self):
        from theseus import store
        with store.for_project(self.conf) as conn:
            store.put_gateway_retry(conn, self.conf.name, "worker-a", phase="waiting", next_epoch=900.0, pool="p")
        self.assertEqual(30.0, self.wq.next_sleep(self.conf, 30, 1000.0))
        with store.for_project(self.conf) as conn:
            store.put_gateway_retry(conn, self.conf.name, "worker-a", phase="waiting", next_epoch=1010.0, pool="p")
        self.assertEqual(10.5, self.wq.next_sleep(self.conf, 30, 1000.0))
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

    def test_other_commands_still_refuse_a_newer_store(self):
        result = self.theseus("queue", "tick", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("newer than", result.stderr)


if __name__ == "__main__":
    unittest.main()
