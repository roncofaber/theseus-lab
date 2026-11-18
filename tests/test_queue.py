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
        self.theseus("agents", "add", "worker-a", "%1")

    def add(self, task_id, room=None, **extra):
        args = ["tasks", "add", task_id, "--role", "research", "--owned", f"docs/investigations/{task_id}.md",
                "--objective", "Do it.", "--contract", "All variants.", "--accept", "Done."]
        if room:
            args += ["--room", room]
        self.theseus(*args)
        if extra:
            self.edit_record(task_id, **extra)

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

    def test_a_worker_that_reports_with_task_done_is_reset_and_given_the_next_task(self):
        from theseus import taskservice
        self.add("first-task")
        self.assertIn("assigned", self.theseus("queue", "tick").stdout)
        record = json.loads(self.theseus("tasks", "show", "first-task").stdout)
        self.write(f"{record['worktree']}/.theseus/handoff.md", "Task: first-task\nResult: delivered\n", root=self.tmp)
        worker = {**self.env, **taskservice.grant(taskservice.QUEUE, record["ownerId"], record["ownerId"],
                                                  self.tmp / "credentials")}
        self.theseus("task", "done", "first-task", cwd=record["worktree"], env=worker)
        self.add("second-task")
        self.theseus("queue", "tick")
        typed = self.typed()
        self.assertLess(typed.index("/compact"), typed.index("New task second-task"), "reset before the next task")
        self.assertEqual("in_progress", json.loads(self.theseus("tasks", "show", "second-task").stdout)["status"])

    def test_a_running_task_s_patches_hold_a_task_that_owns_their_target(self):
        self.add("first-task")
        self.theseus("queue", "tick")
        worktree = json.loads(self.theseus("tasks", "show", "first-task").stdout)["worktree"]
        self.write(f"{worktree}/.theseus/patches/01-readme.patch", "diff --git a/README.md b/README.md\n"
                   "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-# old\n+# new\n", root=self.tmp)
        self.theseus("tasks", "add", "readme-task", "--role", "docs", "--owned", "README.md",
                     "docs/investigations/readme-task.md", "--objective", "Do it.", "--contract", "All.", "--accept", "Done.")
        self.theseus("agents", "add", "worker-b", "%2")
        self.assertNotIn("readme-task", self.theseus("queue", "tick").stdout)
        self.assertIn("touches:README.md@first-task", self.theseus("tasks", "list").stdout)

    def test_wait_returns_when_a_delivery_needs_the_coordinator(self):
        self.add("watched-task")
        self.theseus("queue", "tick")
        self.deliver("watched-task")
        result = self.theseus("queue", "wait", "--timeout", "20", "--interval", "1")
        self.assertIn("delivered", result.stdout)

    def test_tick_holds_a_dependent_until_the_dependency_merge_reaches_the_base(self):
        self.add("first-task")
        self.add("second-task", dependencies=["first-task"])
        self.git("init", "-q", "--bare", self.tmp / "origin.git", cwd=self.tmp)
        self.git("remote", "add", "origin", self.tmp / "origin.git")
        self.git("push", "-q", "--no-verify", "origin", "main")
        self.write("theseus.local.toml", '[git]\nbase = "origin/main"\n')
        self.write("src/first.py", "FIRST = 1\n")
        self.git("add", "src/first.py")
        self.git("commit", "-q", "-m", "merge first-task")
        merge = self.git("rev-parse", "HEAD").stdout.strip()
        self.edit_record("first-task", status="merged", merge={"commit": merge, "mergedAt": "2026-11-18T00:00:00Z"})

        out = self.theseus("queue", "tick").stdout
        self.assertRegex(out, rf"held\s+-\s+second-task waiting for first-task's merge {merge[:7]} to reach origin/main")
        self.assertNotIn("assigned", out)
        self.assertNotIn("error", out)
        waited = self.theseus("queue", "wait", "--timeout", "1", "--interval", "1", check=False)
        self.assertEqual(1, waited.returncode, "a hold is not something the coordinator is woken for")
        from theseus import config as cfg, workqueue
        conf = cfg.load(root=self.project)
        self.assertEqual(1, len(workqueue.events(conf, kinds=["held"])), "the hold is recorded once")
        self.assertEqual([], workqueue.events(conf, kinds=["error"]))

        self.git("push", "-q", "--no-verify", "origin", "main")
        self.assertRegex(self.theseus("queue", "tick").stdout, r"assigned\s+worker-a\s+second-task")
        self.assertTrue((self.tmp / "worktrees/worker-a-second-task/src/first.py").exists())

    def test_a_truncated_worktree_name_never_ends_in_a_hyphen(self):
        self.add("fix-placed-item-restore-identity")
        self.assertRegex(self.theseus("queue", "tick").stdout, r"assigned\s+worker-a\s+fix-placed-item-restore-identity")
        self.assertTrue((self.tmp / "worktrees/worker-a-fix-placed-item-restore").is_dir())

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

    def test_a_task_message_that_raises_rolls_the_assignment_back(self):
        from unittest import mock
        from theseus import agents, config as cfg, tasks as t, workqueue
        self.add("raising-task")
        conf = cfg.load(root=self.project)
        with mock.patch.object(agents, "send_verified", side_effect=RuntimeError("pane vanished")):
            ok, detail = workqueue.assign_and_send(conf, agents.agents(conf)[0], t.Queue(conf).get("raising-task"))
        self.assertFalse(ok)
        self.assertIn("pane vanished; raising-task released", detail)
        self.assertEqual("pending", t.Queue(conf).get("raising-task")["status"])
        self.assertFalse((self.tmp / "worktrees/worker-a-raising-task").exists())

    def test_only_commands_that_touch_worktrees_wait_for_a_running_tick(self):
        import subprocess
        import sys
        from theseus import config as cfg, workqueue
        self.add("locked-task")
        with workqueue.locked(cfg.load(root=self.project)):
            self.theseus("tasks", "priority", "locked-task", "3", timeout=20)
            self.assertEqual(3, json.loads(self.theseus("tasks", "show", "locked-task").stdout)["priority"])
            self.theseus("tasks", "export")
            process = subprocess.Popen([sys.executable, "-m", "theseus", "tasks", "import", "--replace"],
                                       cwd=self.project, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True)
            self.assertIn("waiting for the queue lock", process.stderr.readline())
        process.communicate(timeout=60)
        self.assertEqual(0, process.returncode)

    def test_a_hand_made_pane_at_a_permission_prompt_is_waiting_and_never_typed_into(self):
        self.add("prompt-task")
        (self.state / "screen-%1").write_text("Do you want to proceed with the plan? I can.\n> ")
        self.assertRegex(self.theseus("agents").stdout, r"worker-a\s+%1\s+free", "an agent's own prose is no prompt")
        (self.state / "screen-%1").write_text(" \u2502 Do you want to proceed?\n \u2502 \u276f 1. Yes\n \u2502   2. No\n")
        out = self.theseus("queue", "tick").stdout
        self.assertNotIn("assigned", out)
        self.assertEqual("", self.typed())
        self.assertEqual("pending", json.loads(self.theseus("tasks", "show", "prompt-task").stdout)["status"])
        self.assertRegex(self.theseus("agents").stdout, r"worker-a\s+%1\s+waiting")
        refused = self.theseus("instance", "restart", "worker-a", check=False)
        self.assertIn("shows a prompt that needs a human", refused.stderr)
        self.assertEqual("", self.typed())


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
