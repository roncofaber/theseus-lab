"""Agent registry, work queue and dashboard with a fake tmux: panes are idle prompts whose typed input is logged, so
assignments, resets and nudges can be observed without real sessions."""
import json
import os
import unittest

from helpers import Sandbox

FAKE_TMUX = """#!/usr/bin/env bash
case "$1" in
    display) case " $FAKE_PANES " in *" $4 "*) echo 1 ;; *) exit 1 ;; esac ;;
    capture-pane) cat "$FAKE_STATE/screen-$4" 2>/dev/null || echo "> " ;;
    send-keys) printf '%s\\n' "$*" >> "$FAKE_STATE/typed" ;;
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
                         "FAKE_PANES": "%1 %2", "THESEUS_COMPACT_POLL": "0.01", "THESEUS_TYPE_POLL": "0.01"})
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

    def test_registry_add_remove_enable(self):
        listed = self.theseus("agents").stdout
        self.assertRegex(listed, r"worker-a\s+%1\s+free")
        self.assertNotEqual(0, self.theseus("agents", "add", "worker-b", "%1", check=False).returncode)
        self.theseus("agents", "add", "worker-b", "%2")
        self.theseus("agents", "disable", "worker-b")
        self.assertRegex(self.theseus("agents").stdout, r"worker-b\s+%2\s+disabled")
        self.theseus("agents", "remove", "worker-b")
        self.assertNotIn("worker-b", self.theseus("agents").stdout)
        from theseus import store
        with store.opened() as conn:
            self.assertEqual(["worker-a"], [a["name"] for a in store.list_agents(conn, "demo-port")])
        self.assertFalse((self.tmp / "data/agents.json").exists())

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

    def test_an_agent_is_reset_even_when_its_task_was_merged_by_hand(self):
        self.add("hand-merged")
        self.add("after-hand-merge")
        self.theseus("queue", "tick")
        self.deliver("hand-merged")
        self.theseus("tasks", "deliver", "hand-merged", "--handoff", "/dev/null")
        self.git("commit", "-q", "-am", "delivered outside the queue")
        out = self.theseus("queue", "tick").stdout
        self.assertIn("compacted  worker-a", out)
        self.assertIn("after-hand-merge", out)

    def test_clean_work_after_dirty_work_clears_the_context_first(self):
        path = self.project / "theseus.toml"
        text = path.read_text()
        self.assertIn('enabled = false\ntaint = "until-clear"', text)
        path.write_text(text.replace('enabled = false\ntaint = "until-clear"', 'enabled = true\ntaint = "until-clear"'))
        self.git("commit", "-q", "-am", "rooms")
        self.add("re-dirty", room="dirty")
        self.add("clean-next", room="clean")
        self.theseus("queue", "tick")
        self.deliver("re-dirty")
        out = self.theseus("queue", "tick").stdout
        self.assertIn("cleared    worker-a", out)
        self.assertIn("/clear", self.typed())
        self.assertIn("clean-next", out)
        self.assertNotIn("worker-a", json.loads((self.tmp / "data/rooms.json").read_text())["tainted"])

    def test_held_tasks_paused_queue_and_disabled_agents_get_no_work(self):
        self.add("held-task", hold=True)
        self.assertNotIn("assigned", self.theseus("queue", "tick").stdout)
        self.theseus("tasks", "note", "held-task", "Camera is the 400B; cooling allowed.")
        self.theseus("tasks", "release-hold", "held-task")
        self.git("commit", "-q", "-am", "release")
        self.assertIn("held-task", self.theseus("queue", "tick").stdout)
        self.assertIn("Coordinator notes: Camera is the 400B; cooling allowed.", self.typed())
        self.deliver("held-task")
        self.theseus("queue", "tick")
        self.add("open-task")
        self.theseus("queue", "pause")
        self.assertNotIn("assigned", self.theseus("queue", "tick").stdout)
        self.theseus("queue", "resume")
        self.theseus("agents", "disable", "worker-a")
        self.assertNotIn("assigned", self.theseus("queue", "tick").stdout)
        self.theseus("agents", "enable", "worker-a")
        self.assertIn("open-task", self.theseus("queue", "tick").stdout)

    def test_stalled_agent_is_nudged_once_then_reported(self):
        self.replace_config("idle_minutes = 5", "idle_minutes = 0")
        self.git("commit", "-q", "-am", "instant stall")
        self.add("slow-task")
        self.theseus("queue", "tick")
        self.assertIn("nudged", self.theseus("queue", "tick").stdout)
        self.assertIn("Continue the current task", self.typed())
        self.assertIn("stalled", self.theseus("queue", "tick").stdout)
        self.assertNotIn("stalled", self.theseus("queue", "tick").stdout, "a stall is reported once")

    def test_a_thrashing_agent_is_reported_at_once_and_never_nudged(self):
        self.add("heavy-task")
        self.theseus("queue", "tick")
        (self.state / "screen-%1").write_text("Autocompact is thrashing: the context refilled right after compacting.\n"
                                              "Compacting conversation (esc to interrupt)\n")
        self.assertRegex(self.theseus("agents").stdout, r"worker-a\s+%1\s+thrashing")
        waited = self.theseus("queue", "wait", "--timeout", "20", "--interval", "1").stdout
        self.assertRegex(waited, r"stalled\s+worker-a\s+heavy-task thrashing: the pane shows 'Autocompact is thrashing'")
        self.assertNotIn("nudged", self.theseus("queue", "tick").stdout)
        self.assertNotIn("Continue the current task", self.typed())
        self.assertEqual(1, self.theseus("queue", "log").stdout.count("stalled"))
        self.assertIn("thrashing", self.theseus("top", "--once").stdout)
        self.deliver("heavy-task")
        (self.state / "screen-%1").write_text("Autocompact is thrashing: the context refilled right after compacting.\n> \n")
        self.assertIn("delivered", self.theseus("queue", "tick").stdout, "a final handoff wins over a lingering notice")

    def test_a_task_profile_limits_which_agents_take_it(self):
        self.write("theseus.local.toml", '[workers.other-cli]\npreset = "claude-code"\ncommand = "other-cli"\n'
                   '[workers.with-mcp]\npreset = "claude-code"\nmcp_servers = ["theseus"]\n')
        self.assertNotEqual(0, self.theseus("tasks", "add", "bad-profile", "--role", "research", "--owned",
                                            "docs/investigations/bad-profile.md", "--objective", "Do it.", "--contract",
                                            "All variants.", "--accept", "Done.", "--profile", "nope", check=False).returncode)
        self.add("other-task")
        self.theseus("tasks", "profile", "other-task", "other-cli")
        self.git("commit", "-q", "-am", "profile")
        self.assertEqual("other-cli", json.loads(self.theseus("tasks", "show", "other-task").stdout)["profile"])
        self.assertNotIn("assigned", self.theseus("queue", "tick").stdout, "a different command is never relaunched onto")
        self.assertIn("needs profile other-cli", " ".join(self.theseus("top", "--once").stdout.split()))
        from theseus import config as cfg, profiles
        conf = cfg.load(root=self.project)
        self.assertTrue(profiles.relaunch_compatible(conf, "claude-code", "with-mcp"))
        self.assertFalse(profiles.relaunch_compatible(conf, "claude-code", "other-cli"))
        self.theseus("agents", "add", "worker-b", "%2", "--profile", "other-cli")
        out = self.theseus("queue", "tick").stdout
        self.assertRegex(out, r"assigned\s+worker-b\s+other-task")
        self.assertNotIn("relaunched", out)
        self.theseus("tasks", "release", "other-task", "--reason", "test")
        self.theseus("tasks", "profile", "other-task")
        self.assertNotIn("profile", json.loads(self.theseus("tasks", "show", "other-task").stdout))

    def test_an_interim_handoff_while_busy_is_not_a_delivery(self):
        self.add("busy-task")
        self.theseus("queue", "tick")
        self.deliver("busy-task")
        (self.state / "screen-%1").write_text("working (esc to interrupt)\n")
        self.assertNotIn("delivered", self.theseus("queue", "tick").stdout)
        (self.state / "screen-%1").write_text("> \n")
        self.assertIn("delivered", self.theseus("queue", "tick").stdout)
        self.theseus("tasks", "reopen", "busy-task", "--reason", "interim handoff")
        self.assertEqual("in_progress", json.loads(self.theseus("tasks", "show", "busy-task").stdout)["status"])

    def test_a_fresh_or_unfinished_handoff_is_not_a_delivery(self):
        self.add("settling-task")
        self.theseus("queue", "tick")
        self.deliver("settling-task", settled=False)
        self.assertNotIn("delivered", self.theseus("queue", "tick").stdout)
        self.deliver("settling-task", text="Task: settling-task\nStill working on the decoder.\n")
        self.assertNotIn("delivered", self.theseus("queue", "tick").stdout)
        self.deliver("settling-task", text="Task: settling-task\n**Result**: partial. Two of three modes.\n")
        out = self.theseus("queue", "tick").stdout
        self.assertIn("partial", out)
        self.assertEqual("verification", json.loads(self.theseus("tasks", "show", "settling-task").stdout)["status"])

    def test_status_changes_outside_the_queue_appear_in_the_event_history(self):
        self.add("manual-task")
        self.theseus("queue", "tick")
        self.theseus("tasks", "release", "manual-task", "--reason", "reassigning by hand")
        log = self.theseus("queue", "log").stdout
        self.assertRegex(log, r"assigned\s+worker-a\s+manual-task")
        self.assertRegex(log, r"released\s+worker-a\s+manual-task")

    def test_dirty_work_after_clean_work_only_compacts(self):
        path = self.project / "theseus.toml"
        path.write_text(path.read_text().replace('enabled = false\ntaint = "until-clear"', 'enabled = true\ntaint = "until-clear"'))
        self.git("commit", "-q", "-am", "rooms")
        self.add("clean-first", room="clean")
        self.add("dirty-next", room="dirty")
        self.theseus("queue", "tick")
        self.deliver("clean-first")
        out = self.theseus("queue", "tick").stdout
        self.assertIn("compacted  worker-a", out)
        self.assertNotIn("cleared", out)
        self.assertIn("dirty-next", out)
        self.assertIn("/compact", self.typed())
        self.assertNotIn("/clear", self.typed())

    def test_an_agent_without_a_busy_marker_is_quiet_not_nudged(self):
        self.theseus("agents", "remove", "worker-a")
        added = self.theseus("agents", "add", "worker-a", "%1", "--profile", "codex")
        self.assertIn("no busy marker", added.stderr)
        self.add("silent-task")
        self.theseus("queue", "tick")
        old = 1_000_000_000
        for path in (self.tmp / "worktrees").rglob("*"):
            os.utime(path, (old, old))
        self.assertRegex(self.theseus("agents").stdout, r"worker-a\s+%1\s+quiet")
        self.assertNotIn("nudged", self.theseus("queue", "tick").stdout)

    def test_wait_ignores_deliveries_already_merged(self):
        from theseus import config as cfg, workqueue as wq
        self.add("merged-quickly")
        self.theseus("queue", "tick")
        self.deliver("merged-quickly")
        self.theseus("queue", "tick")
        conf = cfg.load(root=self.project)
        record = [e for e in wq.events(conf) if e["kind"] == "delivered"][-1]
        self.assertTrue(wq.still_open(conf, record))
        self.theseus("tasks", "merged", "merged-quickly", "--commit", "abc1234")
        self.assertFalse(wq.still_open(conf, record))

    def test_a_full_shared_pool_holds_assignment_until_a_slot_frees(self):
        (self.tmp / "pools.toml").write_text('[pools.gw]\nmax = 1\npriority = ["demo-port", "other"]\n')
        path = self.project / "theseus.toml"
        path.write_text(path.read_text() + '\n[workers.claude-code]\npool = "gw"\n')
        self.git("commit", "-q", "-am", "pool")
        self.theseus("pool", "hold", "gw", "x", "--project", "other", "--task", "t")
        self.add("pooled-task")
        self.assertNotIn("assigned", self.theseus("queue", "tick").stdout)
        self.assertIn("pool gw full (1/1)", " ".join(self.theseus("top", "--once").stdout.split()))
        self.assertIn("demo-port", self.theseus("pool").stdout)
        self.theseus("pool", "release", "gw", "x", "--project", "other")
        self.assertIn("assigned", self.theseus("queue", "tick").stdout)
        self.assertIn("demo-port 1", self.theseus("pool").stdout)

    def test_a_failed_assignment_gives_its_pool_slot_back(self):
        from unittest import mock
        from theseus import config as cfg, pools, workqueue as wq
        (self.tmp / "pools.toml").write_text('[pools.gw]\nmax = 1\n')
        path = self.project / "theseus.toml"
        path.write_text(path.read_text() + '\n[workers.claude-code]\npool = "gw"\n')
        self.git("commit", "-q", "-am", "pool")
        self.add("doomed-task")
        os.environ.update({k: self.env[k] for k in ("PATH", "FAKE_STATE", "FAKE_PANES", "THESEUS_COMPACT_POLL",
                                                    "THESEUS_TYPE_POLL")})
        held = []
        def fail(conf, agent, task):
            held.append(pools.status("gw")["active"])
            return False, "boom"
        with mock.patch.object(wq, "assign_and_send", side_effect=fail):
            recorded = wq.tick(cfg.load(root=self.project))
        self.assertEqual([1], held, "the slot is taken before the assignment starts")
        self.assertIn(("error", "doomed-task", "boom"), [(r["kind"], r["task"], r["detail"]) for r in recorded])
        self.assertEqual(0, pools.status("gw")["active"])

    def test_priority_orders_assignment_and_transitions_linger_on_the_dashboard(self):
        self.add("ordinary-task")
        self.theseus("tasks", "add", "urgent-task", "--role", "research", "--owned", "docs/investigations/urgent-task.md",
                     "--objective", "Do it.", "--contract", "All variants.", "--accept", "Done.", "--priority", "5")
        self.git("commit", "-q", "-am", "queue")
        before = " ".join(self.theseus("top", "--once").stdout.split())
        self.assertIn("▲5 urgent-task", before)
        self.assertLess(before.index("urgent-task"), before.index("ordinary-task"))
        self.assertIn("urgent-task", self.theseus("queue", "tick").stdout)
        after = " ".join(self.theseus("top", "--once").stdout.split())
        self.assertRegex(after, r"started urgent-task - \d+s .*→ worker-a")
        self.deliver("urgent-task")
        self.theseus("queue", "tick")
        wide = self.theseus("top", "--once", env={**self.env, "COLUMNS": "150"}).stdout
        self.assertRegex(" ".join(wide.split()), r"working ordinary-task .* delivered \d+s")
        self.theseus("tasks", "priority", "ordinary-task", "0")

    def test_wait_returns_when_a_delivery_needs_the_coordinator(self):
        self.add("watched-task")
        self.theseus("queue", "tick")
        self.deliver("watched-task")
        result = self.theseus("queue", "wait", "--timeout", "20", "--interval", "1")
        self.assertIn("delivered", result.stdout)

    def test_top_renders_and_exports_json(self):
        self.add("visible-task")
        frame = self.theseus("top", "--once").stdout
        self.assertIn("theseus top", frame)
        self.assertIn("worker-a", frame)
        self.assertIn("ready visible-task", " ".join(frame.split()))
        self.assertNotIn("\033[", frame)
        data = json.loads(self.theseus("top", "--json").stdout)
        self.assertEqual(["visible-task"], data["queue"]["ready"])
        self.assertEqual("free", data["agents"][0]["state"])
        self.assertIn("visible-task", data["times"])
        self.add("second-task")
        self.theseus("queue", "tick")
        flat = " ".join(self.theseus("top", "--once").stdout.split())
        self.assertIn("REASON", flat)
        self.assertRegex(flat, r"1 worker-a %1 working visible-task - 0m")
        self.assertRegex(flat, r"ready second-task - 0m \d\d:\d\d eligible agents busy \(1\)")
        wide = " ".join(self.theseus("top", "--once", env={**self.env, "COLUMNS": "140"}).stdout.split())
        self.assertRegex(wide, r"ready second-task - 0m \d\d:\d\d eligible agents busy \(1\)")
        self.theseus("agents", "disable", "worker-a")
        disabled = " ".join(self.theseus("top", "--once").stdout.split())
        self.assertIn("ready second-task - 0m", disabled)
        self.assertIn("no eligible agent", disabled)
        self.theseus("agents", "enable", "worker-a")
        sized = self.theseus("top", "--once", env={**self.env, "COLUMNS": "120", "LINES": "70"}).stdout
        self.assertEqual(70, len(sized.splitlines()))
        self.assertIn("after compact", flat)
        self.assertRegex(flat, r"M1 \S+ 0/2 done")
        narrow = self.theseus("top", "--once", env={**self.env, "COLUMNS": "84"}).stdout
        self.assertTrue(all(len(line) <= 84 for line in narrow.splitlines()))
        self.assertNotIn("~ │", narrow.split("AGENTS")[1].split("WAITING")[0])



if __name__ == "__main__":
    unittest.main()
