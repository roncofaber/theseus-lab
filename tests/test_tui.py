"""The console's data and a few smoke tests of the Textual app: the host snapshot over several projects, the project
snapshot and its waiting reasons, the transcript adapter, and the app driven with Pilot over a fake tmux."""
import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from helpers import Sandbox

from test_queue import FAKE_TMUX
from theseus import host, store


def quick_idle(case):
    """Cap Textual's wait for an idle CPU after each Pilot key press at one 20 ms slice: the dashboard's 0.05 s mirror
    timer keeps the CPU busy, so the default waits up to 1 s twice per key, and every test waits on its own condition."""
    from unittest import mock
    from textual import _wait
    original = _wait.wait_for_idle

    async def capped(min_sleep=_wait.SLEEP_GRANULARITY, max_sleep=1):
        await original(0, min(max_sleep, _wait.SLEEP_GRANULARITY))

    for module in ("textual.app", "textual.pilot"):
        patcher = mock.patch(f"{module}.wait_for_idle", capped)
        patcher.start()
        case.addCleanup(patcher.stop)


class HostCase(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold("alpha")
        self.second = self.tmp / "project2"
        self.theseus("scaffold", self.second, "--name", "beta", cwd=self.tmp)
        for root, label in ((self.project, "alpha"), (self.second, "beta")):
            (root / "theseus.local.toml").write_text(
                f'[paths]\ndata = "{self.tmp}/data-{label}"\nscratch = "{self.tmp}/scratch-{label}"\n'
                f'worktrees = "{self.tmp}/worktrees-{label}"\n')
        for variable in ("THESEUS_DATA", "THESEUS_SCRATCH", "THESEUS_WORKTREES"):
            self.env.pop(variable)
            os.environ.pop(variable)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "tmux").write_text(FAKE_TMUX)
        (bin_dir / "tmux").chmod(0o755)
        (self.tmp / "state").mkdir()
        extra = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_STATE": str(self.tmp / "state"),
                 "FAKE_PANES": "%1 %2"}
        self.env.update(extra)
        os.environ.update(extra)
        os.environ.pop("TMUX", None)
        self.theseus("agents", "add", "worker-a", "%1")
        self.theseus("agents", "add", "worker-b", "%2")
        self.theseus("queue", "status", cwd=self.second)

    def event(self, label, kind, minutes_ago, task="t1"):
        stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        with store.opened() as conn:
            store.add_event(conn, label, kind, agent="worker-a", task=task, at=stamp)

    def add_task(self, task_id, *extra):
        self.theseus("tasks", "add", task_id, "--role", "research", "--owned", f"docs/investigations/{task_id}.md",
                     "--objective", "Do it.", "--contract", "All variants.", "--accept", "Done.", *extra)


class HostSnapshotTests(HostCase):
    def test_projects_events_and_activity_are_merged_and_a_broken_project_is_not_fatal(self):
        self.event("alpha", "assigned", 5)
        self.event("beta", "delivered", 1, "t2")
        state = host.snapshot()
        self.assertEqual(["alpha", "beta"], [p["name"] for p in state["projects"]])
        self.assertTrue(all(p["available"] for p in state["projects"]))
        self.assertEqual(2, len(state["projects"][0]["snapshot"]["agents"]))
        self.assertEqual([("alpha", "assigned"), ("beta", "delivered")], [(e["project"], e["kind"]) for e in state["events"]])
        self.assertEqual(["alpha/worker-a", "beta/worker-a"], [e["agentLabel"] for e in state["events"]])
        self.assertEqual(1, state["projects"][0]["activity"]["assigned"][-1])
        self.assertEqual(1, state["projects"][1]["activity"]["delivered"][-1])
        (self.second / "theseus.toml").write_text("this is [not toml")
        by_name = {p["name"]: p for p in host.snapshot([self.tmp / "nowhere"])["projects"]}
        self.assertTrue(by_name["alpha"]["available"])
        self.assertFalse(by_name["beta"]["available"])
        self.assertIn("ConfigError", by_name["beta"]["error"])
        self.assertFalse(by_name["nowhere"]["available"])


class ProjectSnapshotTests(HostCase):
    def test_the_snapshot_says_why_each_waiting_task_has_not_started(self):
        from theseus import config, top
        self.add_task("first-task")
        self.add_task("later-task", "--depends", "first-task")
        self.add_task("hard-task", "--min-capability", "2")
        state = top.snapshot(config.load(root=self.project), host={})
        self.assertEqual(["worker-a", "worker-b"], sorted(a["name"] for a in state["agents"]))
        self.assertEqual({"hard-task": 2}, state["gated"])
        reasons = {e["task"]: (e["key"], e["reason"]) for e in top.waiting_entries(state)}
        self.assertEqual({"first-task": ("ready", "assigning on the next tick"),
                          "hard-task": ("ready", "needs capability >= 2"),
                          "later-task": ("blocked", "waiting for first-task")}, reasons)


class ConsoleTests(HostCase, unittest.IsolatedAsyncioTestCase):
    """The console on a real host snapshot, with tmux captures, sends and keys replaced by recorders."""

    async def asyncSetUp(self):
        from theseus.tui.app import TopApp
        asyncio.get_running_loop().set_debug(False)
        quick_idle(self)
        self.add_task("ready-task")
        self.event("alpha", "assigned", 5)
        self.event("alpha", "delivered", 3, "t2")
        self.sent, self.keys = [], []
        # The terminal's color system is pinned: CI runners have no TERM or COLORTERM.
        self.app = TopApp(interval=3600, snapshot_fn=host.snapshot, tmux_ok=True, color_system="truecolor",
                          mirror_seconds=0.05, jump_fn=lambda pane: f"jumped {pane}",
                          capture_fn=lambda pane: "\033[32mready for input\033[0m\n> ",
                          send_fn=lambda conf, agent, text, expect_busy=True: self.sent.append(
                              (agent["pane"], text, expect_busy)) or "sent",
                          keys_fn=lambda pane, *keys: self.keys.append((pane, keys)) or "")

    async def settle(self, pilot, condition):
        await self.app.workers.wait_for_complete()
        for _ in range(250):
            if condition():
                return
            await pilot.pause(0.02)
        self.fail(f"did not settle: screen={self.app.screen} focused={self.app.focused} title={self.app.title}")

    async def start(self, pilot):
        from textual.widgets import TabbedContent, TabPane
        await self.settle(pilot, lambda: "updated" in self.app.title
                          and len(list(self.app.base.query_one(TabbedContent).query(TabPane))) == 3)
        await pilot.press("2")
        await self.settle(pilot, lambda: self.app.active_item() is not None)

    def rows(self, table):
        return self.app.active_view().query_one(f"#{table}").row_count

    async def test_the_console_shows_every_project_and_its_agents_queue_and_events(self):
        from textual.widgets import TabbedContent, TabPane
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.start(pilot)
            panes = list(self.app.base.query_one(TabbedContent).query(TabPane))
            self.assertEqual(["Host", "alpha", "beta"], [str(p._title) for p in panes])
            await self.settle(pilot, lambda: self.rows("agents") == 2 and self.rows("waiting") >= 1
                              and self.rows("events") >= 2)

    async def test_the_agent_popup_mirrors_the_pane_and_sends_a_line(self):
        from theseus.tui.popups import AgentScreen, plain
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.start(pilot)
            await pilot.press("a", "enter")
            await self.settle(pilot, lambda: isinstance(self.app.screen, AgentScreen))
            popup = self.app.screen
            mirror = popup.query_one("#mirror-text")
            await self.settle(pilot, lambda: "ready for input" in plain(mirror.content))
            await pilot.click("#send")
            popup.query_one("#send").value = "hello"
            await pilot.press("enter")
            await self.settle(pilot, lambda: self.sent == [("%1", "hello", True)])
            await pilot.press("escape")
            await self.settle(pilot, lambda: len(self.app.screen_stack) == 1)

    async def test_the_help_screen_opens(self):
        from theseus.tui.popups import HelpScreen
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.start(pilot)
            await pilot.press("question_mark")
            await self.settle(pilot, lambda: isinstance(self.app.screen, HelpScreen))


class TranscriptTests(Sandbox):
    """The Claude Code transcript adapter over temporary session files."""

    def setUp(self):
        super().setUp()
        from theseus.adapters import claude_code as transcripts
        transcripts.READS.clear()
        transcripts.SESSIONS.clear()

    def entry(self, **record):
        return json.dumps(record) + "\n"

    def test_the_reader_follows_appends_and_reports_context_and_api_errors(self):
        from theseus.adapters import claude_code as transcripts
        path = self.tmp / "t.jsonl"
        usage = {"input_tokens": 2, "cache_read_input_tokens": 80000, "cache_creation_input_tokens": 7000}
        path.write_text(self.entry(type="user", message={"content": "go"})
                        + self.entry(type="assistant", message={"model": "m1", "usage": usage, "content": []})
                        + self.entry(type="assistant", isApiErrorMessage=True, error="rate_limit", timestamp="t1",
                                     message={"content": [{"type": "text", "text": "API Error: Request rejected (429)"}]})
                        + '{"type": "assist')
        state = transcripts.read(path)
        self.assertEqual((87002, "m1", "error"), (state["tokens"], state["model"], state["last"]))
        self.assertEqual([("rate_limit", "API Error: Request rejected (429)")],
                         [(e["error"], e["text"]) for e in state["errors"]])
        offset = state["offset"]
        with open(path, "a") as handle:
            handle.write('ant", "message": {"usage": {"input_tokens": 90000}}}\n')
        state = transcripts.read(path)
        self.assertEqual((90000, "answer"), (state["tokens"], state["last"]))
        self.assertGreater(state["offset"], offset)
        self.assertEqual(state, transcripts.read(path))
        long = self.tmp / "long.jsonl"
        usage = {"input_tokens": 5, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        long.write_text(self.entry(type="user", message={"content": "x" * 1000}) * 1000
                        + self.entry(type="assistant", message={"usage": usage}))
        state = transcripts.read(long)
        self.assertEqual((5, long.stat().st_size), (state["tokens"], state["offset"]), "a long transcript is read from its tail")

    def test_the_conversation_keeps_text_and_tool_calls_and_drops_thinking_sidechains_and_meta(self):
        from theseus.adapters import claude_code as transcripts
        path = self.tmp / "c.jsonl"
        path.write_text(
            self.entry(type="user", message={"content": "decode the frame<system-reminder>hint</system-reminder>"},
                       timestamp="t1")
            + self.entry(type="user", isMeta=True, message={"content": "<command caveat>"})
            + self.entry(type="user", message={"content": "<local-command-stdout>Compacted</local-command-stdout>"})
            + self.entry(type="user", message={"content": [{"type": "text", "text": "<task-notification>x</task-notification>"}]})
            + self.entry(type="user", isCompactSummary=True, message={"content": "This session is being continued"})
            + self.entry(type="system", content="hook ran")
            + self.entry(type="assistant", cwd="/work", message={"content": [
                {"type": "thinking", "thinking": "private reasoning"},
                {"type": "text", "text": "Reading the decoder."},
                {"type": "tool_use", "id": "u1", "name": "Read", "input": {"file_path": "/work/src/status.py",
                                                                           "limit": 20}}]})
            + self.entry(type="user", message={"content": [
                {"type": "tool_result", "tool_use_id": "u1", "content": [{"type": "text", "text": "line 1\nline 2"}]}]})
            + self.entry(type="assistant", isSidechain=True, message={"content": [{"type": "text", "text": "subagent"}]})
            + self.entry(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "u2", "is_error": True,
                                                            "content": "no such file"}]})
            + self.entry(type="user", message={"content": "<command-name>/compact</command-name>\n<command-message>compact"
                                                          "</command-message>\n<command-args>keep notes</command-args>"}))
        state = transcripts.read(path)
        self.assertEqual([("user", "decode the frame"), ("assistant", "Reading the decoder."), ("tool", None),
                          ("result", "line 1\nline 2"), ("result", "no such file"), ("user", "/compact keep notes")],
                         [(e["kind"], None if e["kind"] == "tool" else e["text"]) for e in state["entries"]])
        self.assertEqual(("Read", "src/status.py"), (state["entries"][2]["name"], state["entries"][2]["summary"]))
        self.assertTrue(state["entries"][4]["error"])
        self.assertNotIn("private reasoning", json.dumps(state["entries"]))
        with open(path, "a") as handle:
            handle.write(self.entry(type="assistant", message={"content": [{"type": "text", "text": "x" * 9000}]}))
        state = transcripts.read(path)
        self.assertEqual(7, state["total"])
        self.assertIn("1000 more characters", state["entries"][-1]["text"])


if __name__ == "__main__":
    unittest.main()
