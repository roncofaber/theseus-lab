"""Host registry, host snapshot and the Textual dashboard (driven with Pilot, a fake tmux and temporary registries)."""
import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from helpers import Sandbox

from test_queue import FAKE_TMUX
from theseus import host, registry, store, top


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


def fake_conf(root, name="alpha"):
    return SimpleNamespace(root=Path(root), name=name, data=Path(root) / "data",
                           section=lambda section: {"coordinator": "coord"} if section == "instances" else {})


class RegistryTests(Sandbox):
    def make_project(self, name):
        root = self.tmp / name
        root.mkdir()
        (root / "theseus.toml").write_text("[project]\n")
        return root

    def test_record_writes_name_root_data_coordinator_and_time(self):
        root = self.make_project("alpha")
        self.assertTrue(registry.record(fake_conf(root), now=1000.0))
        [entry] = registry.entries()
        self.assertEqual(
            {"name": "alpha", "root": str(root), "data": str(root / "data"), "coordinator": "coord", "last_seen": 1000.0}, entry)

    def test_writes_at_most_once_a_minute_per_project(self):
        root = self.make_project("alpha")
        registry.record(fake_conf(root), now=1000.0)
        self.assertFalse(registry.record(fake_conf(root), now=1059.0))
        self.assertEqual(1000.0, registry.entries()[0]["last_seen"])
        self.assertTrue(registry.record(fake_conf(root), now=1061.0))
        self.assertEqual(1061.0, registry.entries()[0]["last_seen"])

    def test_hides_projects_whose_root_lost_its_config_without_deleting_them(self):
        first, second = self.make_project("alpha"), self.make_project("beta")
        registry.record(fake_conf(first), now=1.0)
        registry.record(fake_conf(second, "beta"), now=1.0)
        (first / "theseus.toml").unlink()
        self.assertEqual(["beta"], [e["name"] for e in registry.entries()])
        registry.record(fake_conf(second, "beta"), now=500.0)
        with store.opened() as conn:
            self.assertEqual(["alpha", "beta"], [p["name"] for p in store.list_projects(conn)])

    def test_override_and_xdg_paths(self):
        self.assertEqual(self.tmp / "registry/projects.json", registry.path())
        del os.environ["THESEUS_REGISTRY"]
        os.environ["XDG_DATA_HOME"] = str(self.tmp / "xdg")
        self.assertEqual(self.tmp / "xdg/theseus/projects.json", registry.path())

    def test_a_failed_write_never_raises(self):
        root = self.make_project("alpha")
        blocker = self.tmp / "blocker"
        blocker.write_text("not a directory")
        os.environ["THESEUS_DB"] = str(blocker / "theseus.db")
        self.assertFalse(registry.record(fake_conf(root)))
        self.assertEqual([], registry.entries())


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


class HostSnapshotTests(HostCase):
    def test_snapshot_has_projects_pools_and_merged_events(self):
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
        self.assertEqual([], state["pools"])

    def test_broken_and_missing_projects_are_unavailable_not_fatal(self):
        (self.second / "theseus.toml").write_text("this is [not toml")
        state = host.snapshot([self.tmp / "nowhere"])
        by_name = {p["name"]: p for p in state["projects"]}
        self.assertTrue(by_name["alpha"]["available"])
        self.assertFalse(by_name["beta"]["available"])
        self.assertIn("ConfigError", by_name["beta"]["error"])
        self.assertFalse(by_name["nowhere"]["available"])


class DashboardTests(HostCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from theseus.tui.app import TopApp
        asyncio.get_running_loop().set_debug(False)
        quick_idle(self)
        self.jumps = []
        self.sent, self.keys, self.captures = [], [], []
        self.app = TopApp(interval=3600, snapshot_fn=host.snapshot, tmux_ok=True, mirror_seconds=0.05,
                          jump_fn=lambda pane: self.jumps.append(pane) or f"jumped {pane}",
                          capture_fn=lambda pane: self.captures.append(pane) or "\033[32mready\033[0m\n> ",
                          send_fn=lambda conf, agent, text, expect_busy=True: self.sent.append(
                              (agent["pane"], text, expect_busy)) or "sent",
                          keys_fn=lambda pane, *keys: self.keys.append((pane, keys)) or "")

    async def settle(self, pilot, condition=None):
        """Wait for the first refresh to be applied and its tabs mounted (slow machines need more than one pause)."""
        from textual.widgets import TabbedContent, TabPane
        await self.app.workers.wait_for_complete()
        ready = condition or (lambda: "updated" in self.app.title
                              and len(list(self.app.query_one(TabbedContent).query(TabPane))) == 3)
        for _ in range(250):
            if ready():
                return
            await pilot.pause(0.02)
        self.fail(f"dashboard did not settle: active={self.app.query_one(TabbedContent).active} focused={self.app.focused} title={self.app.title}")

    async def switch(self, pilot, key):
        await pilot.press(key)
        await self.settle(pilot, lambda: self.app.active_item() is not None)

    def rows(self, table_id):
        from textual.widgets import DataTable
        return self.app.active_view().query_one(f"#{table_id}", DataTable).row_count

    async def test_tabs_are_host_then_one_per_project(self):
        from textual.widgets import TabbedContent, TabPane
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.settle(pilot)
            panes = list(self.app.query_one(TabbedContent).query(TabPane))
            self.assertEqual(["Host", "alpha", "beta"], [str(p._title) for p in panes])
            await self.settle(pilot, lambda: len(self.app.active_view().project_names()) == 2)
            self.assertEqual(["alpha", "beta"], self.app.active_view().project_names())

GATEWAY_ERRORS = [
    {"id": 7, "pool": "cborg", "project": "alpha", "agent": "worker-a", "task": "t1", "code": 429, "retryable": 1,
     "time": "2026-11-17T17:57:43Z", "epoch": 1.0, "kind": "rate_limit", "subtype": "parallel cap",
     "label": "429 parallel cap", "excerpt": "API Error: Request rejected (429) · Rate limit exceeded for api_key: "
     "[redacted]. Limit type: max_parallel_requests.", "attempt": 1, "attempts": 3, "outcome": "retry",
     "next": 4102444800.0, "pausedUntil": None, "capCut": 1, "capUntil": 4102444800.0},
    {"id": 6, "pool": "cborg", "project": "alpha", "agent": "worker-b", "task": "t2", "code": None, "retryable": 1,
     "time": "2026-11-17T17:50:00Z", "epoch": 0.5, "kind": "connection_lost", "subtype": "connection lost",
     "label": "conn lost", "excerpt": "API Error: Connection lost mid-response.", "attempt": 2, "attempts": 3,
     "outcome": "recovered", "next": None, "pausedUntil": None, "capCut": 1, "capUntil": 4102444800.0}]
GATEWAY_HEALTH = [{"pool": "cborg", "codes": [429, None], "labels": ["429 parallel cap", "conn lost"], "errors": 2,
                   "lastError": 1.0, "lastSuccess": None, "pausedUntil": None, "reason": None, "capCut": 1,
                   "capUntil": 4102444800.0}]


def grown(base, agents=0, events=0, tasks=0, gpus=None, gateway=False):
    """A copy of a real host snapshot with extra agents, events and ready tasks in the first project (and fake GPUs and
    gateway errors)."""
    import copy
    snap = copy.deepcopy(base)
    if gateway:
        snap["gatewayErrors"], snap["gateway"] = copy.deepcopy(GATEWAY_ERRORS), copy.deepcopy(GATEWAY_HEALTH)
    if gpus is not None:
        snap["host"]["gpus"] = [{"name": "Fake GPU", "util": 10.0 * n, "memUsed": 2.0**30 * n, "memTotal": 2.0**34,
                                 "temp": 40.0 + n} for n in range(gpus)]
    state = snap["projects"][0]["snapshot"]
    template = state["agents"][0]
    for n in range(agents):
        state["agents"].append({**template, "name": f"extra-{n:02d}", "pane": f"%{100 + n}", "cpu": 3.0, "rss": 2**28})
    kinds = ["assigned", "delivered", "partial", "blocked", "merged", "error"]
    stamp = datetime.fromisoformat(base["time"])
    for n in range(events):
        record = {"id": 10000 + n, "project": state["project"], "kind": kinds[n % len(kinds)], "agent": f"extra-{n % 7}",
                  "task": f"task-{n % 50:03d}", "detail": f"detail {n} " + "x" * (n % 40),
                  "time": (stamp - timedelta(seconds=30 * (1000 - n))).isoformat().replace("+00:00", "Z")}
        state["events"].append(record)
        snap["events"].append(record)
    for n in range(tasks):
        task_id = f"task-{n:03d}"
        state["tasks"][task_id] = {"id": task_id, "role": "research", "status": "pending", "objective": f"Objective {n}. " * 30,
                                   "generalContract": "All variants.", "acceptance": ["Done."], "ownedPaths": [f"docs/{task_id}.md"],
                                   "dependencies": [], "createdAt": "2026-01-01T00:00:00Z"}
        state["queue"]["ready"].append(task_id)
        state["rooms"][task_id] = None
    return snap


class DashboardBase(HostCase, unittest.IsolatedAsyncioTestCase):
    """A dashboard app on a grown copy of a real host snapshot, with fakes for tmux jumps, sends, keys and captures."""

    cached = None

    async def asyncSetUp(self):
        from theseus.tui.app import TopApp
        asyncio.get_running_loop().set_debug(False)
        quick_idle(self)
        self.event("alpha", "assigned", 5)
        self.event("alpha", "delivered", 3, "t2")
        self.event("alpha", "error", 2, "t3")
        self.base = self.shared_snapshot()
        self.extra = {}
        self.jumps, self.sent, self.keys, self.captures, self.fits = [], [], [], [], []
        self.conversation = []
        # The terminal's color system is pinned: CI runners have no TERM or COLORTERM.
        self.app = TopApp(interval=3600, snapshot_fn=lambda: grown(self.base, **self.extra), tmux_ok=True,
                          color_system="truecolor",
                          fit_fn=lambda root, name: lambda columns=None, rows=None: self.fits.append((name, columns, rows)),
                          conversation_fn=lambda root, name, pane=None, mode="none": None if mode == "none" else (
                              lambda after=0: (len(self.conversation), list(self.conversation[after:]), False)),
                          mirror_seconds=0.05, jump_fn=lambda pane: self.jumps.append(pane) or f"jumped {pane}",
                          capture_fn=lambda pane: self.captures.append(pane) or "\033[32mready for input\033[0m\n> ",
                          send_fn=lambda conf, agent, text, expect_busy=True: self.sent.append(
                              (agent["pane"], text, expect_busy)) or "sent",
                          keys_fn=lambda pane, *keys: self.keys.append((pane, keys)) or "")

    def shared_snapshot(self):
        """The host snapshot is the same for every test of the class but its temporary root, so it is taken once."""
        cached = DashboardBase.cached
        if cached is None:
            cached = DashboardBase.cached = (str(self.tmp), json.dumps(host.snapshot()))
        return json.loads(cached[1].replace(cached[0], str(self.tmp)))

    async def settle(self, pilot, condition):
        await self.app.workers.wait_for_complete()
        for _ in range(250):
            if condition():
                return
            await pilot.pause(0.02)
        self.fail(f"did not settle: screen={self.app.screen} focused={self.app.focused} title={self.app.title}")

    async def start(self, pilot, tab="2"):
        from textual.widgets import TabbedContent, TabPane
        await self.settle(pilot, lambda: "updated" in self.app.title
                          and len(list(self.app.base.query_one(TabbedContent).query(TabPane))) == 3)
        if tab:
            await pilot.press(tab)
            await self.settle(pilot, lambda: self.app.active_item() is not None)
        await pilot.pause()

    def table(self, name):
        return self.app.active_view().query_one(f"#{name}")

    def setUpApp(self):
        from theseus.tui.app import TopApp
        old = self.app
        self.app = TopApp(interval=3600, snapshot_fn=old.snapshot_fn, tmux_ok=True, mirror_seconds=0.05,
                          jump_fn=old.jump_fn, capture_fn=old.capture_fn, send_fn=old.send_fn, keys_fn=old.keys_fn,
                          color_system="truecolor", cli_fn=old.cli_fn, fit_fn=old.fit_fn,
                          conversation_fn=old.conversation_fn)


class ConsoleTests(DashboardBase):
    """The console's supervised-agent popup (tabs, lifecycle actions behind a confirm), the coordinator popup, the
    PROJECT overview and the command palette's actions, with the CLI and attach replaced by recorders."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ran = []
        state = self.base["projects"][0]["snapshot"]
        state["agents"][0].update({"managed": True, "display": "fake-1", "gen": 2, "restarts": 1, "session": "abcdef1234567",
                                   "model": "some/model", "command": "fake-agent", "pool": "shared", "uptime": 600})
        state["coordinator"] = {"name": "lead", "state": "busy", "pane": "%9", "session": "s-1", "pid": 1,
                                "context": None, "lastActivity": None, "waiting": None}
        state["watcher"] = {"pid": os.getpid(), "command": "wait", "tick": time.time(), "next": time.time() + 20,
                            "alive": True}
        state["agentCounts"] = {"fake": 2}
        self.app.cli_fn = lambda root, argv: self.ran.append(argv) or (True, "done")

    async def test_a_supervised_agent_window_follows_the_mirror_and_is_restored_on_close(self):
        from textual.events import Resize
        from textual.geometry import Size
        from theseus.tui.popups import AgentScreen
        width = [0]
        plain = self.app.capture_fn
        self.app.capture_fn = lambda pane: ("x" * width[0] + "\n") * 80 + "> " if width[0] else plain(pane)
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.start(pilot)
            await pilot.press("a", "enter")
            await self.settle(pilot, lambda: isinstance(self.app.screen, AgentScreen))
            popup = self.app.screen
            mirror = popup.query_one("#mirror")
            await self.settle(pilot, lambda: ("worker-a", *mirror.visible_size()) in self.fits)
            first = mirror.visible_size()
            await pilot.click("#send")
            popup.query_one("#send").value = "hello"
            await pilot.press("enter")
            await self.settle(pilot, lambda: self.sent == [("%1", "hello", True)])
            self.assertGreater(first[0], 100)
            self.assertGreater(first[1], 10)
            width[0] = first[0]
            await self.settle(pilot, lambda: mirror.virtual_size.width == first[0] and mirror.max_scroll_y > 0
                              and mirror.max_scroll_x == 0)
            width[0] = first[0] + 1
            await self.settle(pilot, lambda: mirror.virtual_size.width == first[0] + 1 and mirror.max_scroll_x > 0)
            width[0] = 0
            popup.fit_seconds = 3600
            sizes = []
            for size in (Size(150, 50), Size(130, 45)):
                self.app._driver._size = size
                self.app.post_message(Resize(size, size))
                await self.settle(pilot, lambda: mirror.visible_size() not in (first, *sizes))
                sizes.append(mirror.visible_size())
            self.assertIsNotNone(popup.fit_timer)
            self.assertEqual({first}, {(columns, rows) for _, columns, rows in self.fits[1:]},
                             "the window keeps its size while the mirror is still changing")
            popup.fit_timer.stop()
            popup.fit_now()
            await self.settle(pilot, lambda: ("worker-a", *sizes[-1]) in self.fits)
            self.assertNotIn(("worker-a", *sizes[0]), self.fits, "a size the mirror only passed through is not applied")
            await pilot.press("escape")
            await self.settle(pilot, lambda: len(self.app.screen_stack) == 1 and self.fits[-1] == ("worker-a", None, None))
            done = list(self.fits)
            await pilot.press("a", "down", "enter")
            await self.settle(pilot, lambda: isinstance(self.app.screen, AgentScreen))
            self.assertIn("worker-b", self.app.screen.heading)
            count = len(self.captures)
            await self.settle(pilot, lambda: len(self.captures) >= count + 3)
            await pilot.press("escape")
            await self.settle(pilot, lambda: len(self.app.screen_stack) == 1)
            await pilot.press("C")
            await self.settle(pilot, lambda: isinstance(self.app.screen, AgentScreen) and "%9" in self.captures)
            await pilot.press("escape")
            await self.settle(pilot, lambda: len(self.app.screen_stack) == 1)
            self.assertEqual(done, self.fits, "a hand-made pane or the coordinator is never resized")
            await pilot.press("a", "up", "enter")
            await self.settle(pilot, lambda: isinstance(self.app.screen, AgentScreen) and self.fits[-1][1])
        self.assertEqual(("worker-a", None, None), self.fits[-1], "quitting with the popup open restores the size")

    async def test_the_agent_popup_has_four_buttons_that_fit_and_the_rest_in_keys_and_the_palette(self):
        from textual.command import CommandList, CommandPalette
        from textual.widgets import Button, Input
        from theseus.tui.popups import AgentScreen
        async with self.app.run_test(size=(80, 40)) as pilot:
            await self.start(pilot)
            await pilot.press("a", "enter")
            await self.settle(pilot, lambda: isinstance(self.app.screen, AgentScreen))
            popup = self.app.screen
            buttons = list(popup.query_one("#buttons").query(Button))
            self.assertEqual(["attach", "type", "interrupt", "close"], [b.id for b in buttons])
            await self.settle(pilot, lambda: buttons[-1].region.width > 0)
            self.assertLessEqual(buttons[-1].region.right, popup.query_one("#dialog").content_region.right)
            self.assertEqual(["Send Enter", "Send Escape (E)", "Compact (K)", "Open in my tmux (o)", "Restart (R)",
                              "Stop (X)", "Start (U)"], [title.split(": ", 1)[1] for title, _, _ in popup.palette()])
            await pilot.press("E")
            await self.settle(pilot, lambda: ("%1", ("Escape",)) in self.keys)
            await pilot.press("ctrl+p")
            await self.settle(pilot, lambda: isinstance(self.app.screen, CommandPalette))
            palette = self.app.screen
            palette.query_one(Input).value = "fake-1: send enter"
            commands = palette.query_one(CommandList)
            await self.settle(pilot, lambda: commands.highlighted is not None and "Send Enter" in str(
                commands.get_option_at_index(commands.highlighted).prompt))
            await pilot.press("enter")
            await self.settle(pilot, lambda: ("%1", ("Enter",)) in self.keys)

    async def test_the_conversation_tab_folds_tool_calls_and_follows_new_entries_unless_scrolled_up(self):
        from textual import events
        from theseus.tui.popups import AgentScreen, Fold, plain
        def turn(n):
            return [{"kind": "user", "text": f"question {n}"},
                    {"kind": "assistant", "text": f"**answer** {n}\n\n- second line", "format": "markdown"},
                    {"kind": "tool", "name": "Read", "summary": f"f{n}.py", "text": f"file_path: f{n}.py\nlimit: 2"},
                    {"kind": "result", "summary": "a", "text": "a\nb\nc", "error": n == 0}]
        self.conversation.extend(turn(0))
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.start(pilot)
            await pilot.press("a", "enter")
            await self.settle(pilot, lambda: isinstance(self.app.screen, AgentScreen))
            popup = self.app.screen
            await pilot.press("c")
            log = popup.query_one("#conversation")
            await self.settle(pilot, lambda: len(log.query(".turn, .fold")) == 4)
            texts = [plain(w.content) for w in log.query(".turn, .fold")]
            self.assertIn("question 0", texts[0])
            self.assertEqual(["answer 0", "• second line"], [line.strip() for line in texts[1].splitlines() if line.strip()],
                             "assistant text is Markdown")
            self.assertEqual("▸ Read: f0.py", texts[2].strip())
            self.assertEqual("↳ error: a  (3 lines)", texts[3].strip())
            fold = log.query(Fold).first()
            await self.settle(pilot, lambda: fold.region.height == 1)
            await pilot.click(fold)
            await self.settle(pilot, lambda: fold.expanded and fold.region.height == 2)
            for n in range(1, 8):
                self.conversation.extend(turn(n))
            await self.settle(pilot, lambda: len(log.query(".turn, .fold")) == 32 and log.max_scroll_y > 0
                              and log.scroll_y == log.max_scroll_y)
            log.scroll_to(y=0, animate=False)
            await self.settle(pilot, lambda: log.scroll_y == 0)
            self.conversation.extend(turn(8))
            await self.settle(pilot, lambda: len(log.query(".turn, .fold")) == 36)
            await pilot.pause(0.1)
            self.assertEqual(0, log.scroll_y, "a reader who scrolled up stays where they are")
            for widget in (log, popup.query_one("#mirror")):
                for kind, delta in ((events.MouseScrollUp, -1), (events.MouseScrollDown, 1)):
                    event = kind(widget, 1, 1, 0, delta, 0, False, False, False)
                    getattr(widget, f"_on_mouse_scroll_{'up' if delta < 0 else 'down'}")(event)
                    self.assertTrue(event._stop_propagation, f"{kind.__name__} left {widget.id}")
            await pilot.press("l")
            await self.settle(pilot, lambda: popup.query_one("#tabs").active == "live")
            self.assertIsNone(popup.conversation_timer, "the transcript is read only while its tab is shown")


class TranscriptTests(Sandbox):
    """Session lookup from a client's sessions directory and the incremental transcript reader (temporary files only)."""

    def setUp(self):
        super().setUp()
        from theseus.adapters import claude_code as transcripts
        transcripts.READS.clear()
        transcripts.SESSIONS.clear()
        self.sessions = self.tmp / "claude" / "sessions"
        self.sessions.mkdir(parents=True)
        self.cwd = "/work/my.project/worktree_1"

    def session(self, name, pid, tmux, updated, session="abc-123"):
        (self.sessions / f"{name}.json").write_text(json.dumps({"pid": pid, "sessionId": session, "cwd": self.cwd,
                                                                "tmux": tmux, "updatedAt": updated}))

    def entry(self, **record):
        return json.dumps(record) + "\n"

    def test_the_live_session_of_a_pane_and_its_transcript_path(self):
        from theseus.adapters import claude_code as transcripts
        self.session("1", os.getpid(), "$5:@3.%9", 100)
        self.session("2", 2**22 + 12345, "$5:@3.%9", 200, session="dead")
        self.session("3", os.getpid(), "$5:@3.%10", 300, session="other")
        self.assertEqual(("abc-123", self.cwd), transcripts.session_of(self.sessions, "@3.%9"))
        self.assertIsNone(transcripts.session_of(self.sessions, "1"), "a fake tmux answer never matches")
        self.assertEqual(self.tmp / "claude/projects/-work-my-project-worktree-1/abc-123.jsonl",
                         transcripts.transcript_path(self.sessions, "abc-123", self.cwd))

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

    def test_the_conversation_keeps_text_and_tool_calls_and_drops_thinking_sidechains_and_meta(self):
        from theseus.adapters import claude_code as transcripts
        from theseus.tui.popups import conversation_widget, plain
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
                                                                           "limit": 20}},
                {"type": "tool_use", "id": "u3", "name": "TodoWrite", "input": {"todos": [{"content": "x"}]}}]})
            + self.entry(type="user", message={"content": [
                {"type": "tool_result", "tool_use_id": "u1", "content": [{"type": "text", "text": "line 1\nline 2"}]}]})
            + self.entry(type="assistant", isSidechain=True, message={"content": [{"type": "text", "text": "subagent"}]})
            + self.entry(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "u2", "is_error": True,
                                                            "content": "no such file"}]})
            + self.entry(type="user", message={"content": "<command-name>/compact</command-name>\n<command-message>compact"
                                                          "</command-message>\n<command-args>keep notes</command-args>"}))
        state = transcripts.read(path)
        todo = state["entries"].pop(3)
        self.assertEqual("▸ TodoWrite", plain(conversation_widget(todo).content).strip(), "no JSON on a folded line")
        self.assertEqual(6, state["total"] - 1)
        self.assertEqual([("user", "decode the frame"), ("assistant", "Reading the decoder."), ("tool", None),
                          ("result", "line 1\nline 2"), ("result", "no such file"), ("user", "/compact keep notes")],
                         [(e["kind"], None if e["kind"] == "tool" else e["text"]) for e in state["entries"]])
        self.assertEqual("markdown", state["entries"][1]["format"])
        self.assertEqual(("Read", "src/status.py"), (state["entries"][2]["name"], state["entries"][2]["summary"]))
        self.assertEqual("file_path: /work/src/status.py\nlimit: 20", state["entries"][2]["text"])
        self.assertEqual("line 1", state["entries"][3]["summary"])
        self.assertTrue(state["entries"][4]["error"])
        self.assertNotIn("private reasoning", json.dumps(state["entries"]))
        with open(path, "a") as handle:
            handle.write(self.entry(type="assistant", message={"content": [{"type": "text", "text": "x" * 9000}]}))
        state = transcripts.read(path)
        self.assertEqual(8, state["total"])
        self.assertIn("1000 more characters", state["entries"][-1]["text"])

    def test_the_console_conversation_is_redacted_and_read_from_where_it_left_off(self):
        from unittest import mock
        from theseus import agents
        from theseus.tui.app import conversation_reader
        self.scaffold()
        secret = "sk-ant-api03-abcdefghijklmnop"
        state = {"total": 3, "key": "a", "entries": [
            {"kind": "user", "text": "first", "n": 1},
            {"kind": "assistant", "text": f"key {secret} for someone@example.org", "n": 2},
            {"kind": "tool", "name": "Bash", "summary": f"curl -H {secret}", "text": "ok", "n": 3}]}
        with mock.patch.object(agents, "get", return_value={"name": "w"}), \
                mock.patch.object(agents, "session_transcript", return_value=state):
            read = conversation_reader(self.project, "w", "%1", "claude-code-jsonl")
            total, entries, reset = read(0)
            self.assertEqual((3, False), (total, reset))
            self.assertNotIn(secret, entries[1]["text"])
            self.assertNotIn("someone@example.org", entries[1]["text"])
            self.assertNotIn(secret, entries[2]["summary"], "the folded line is redacted like what it unfolds to")
            self.assertEqual(["ok"], [e["text"] for e in read(2)[1]])
            self.assertEqual((3, 3, True), (read(5)[0], len(read(5)[1]), read(5)[2]))
            state.update(key="b", entries=[{"kind": "user", "text": "new session", "n": 1}], total=1)
            self.assertEqual((1, True), (read(3)[0], read(3)[2]), "another record starts over")
            self.assertEqual((1, [], False), read(1))
        with mock.patch.object(agents, "get", side_effect=agents.AgentError("gone")):
            self.assertIsNone(conversation_reader(self.project, "w", "%1", "claude-code-jsonl")(0))

    def test_pane_history_is_the_conversation_of_a_cli_without_a_transcript_adapter(self):
        from unittest import mock
        from theseus import adapters, profiles, top
        from theseus.tui.app import conversation_reader
        self.assertIsNone(conversation_reader(self.project, "w", "%1", "none"))
        self.assertEqual({"claude-code": "claude-code-jsonl", "codex": "pane-history", "opencode": "pane-history",
                          "gemini-cli": "pane-history"},
                         {name: adapters.name_of(profiles.PRESETS[name]["interactive"])
                          for name in ("claude-code", "codex", "opencode", "gemini-cli")})
        self.assertIsNotNone(adapters.load("claude-code-jsonl"))
        self.assertIsNone(adapters.load("pane-history"))
        self.assertEqual(["interactive.transcript 'jsonl' is not an adapter; known: claude-code-jsonl, pane-history, none"],
                         profiles.problems("x", {"interactive": {"transcript": "jsonl"}}))
        screens = ["$ make\nkey sk-ant-abcdefghijklmn\n", "$ make\nkey sk-ant-abcdefghijklmn\n", "$ make\nok\n"]
        with mock.patch.object(top, "capture", side_effect=lambda pane, lines: screens.pop(0)) as capture:
            read = conversation_reader(self.project, "w", "%1", "pane-history")
            total, entries, reset = read(0)
            self.assertEqual((1, True), (total, reset))
            self.assertEqual("history", entries[0]["kind"])
            self.assertNotIn("sk-ant-abcdefghijklmn", entries[0]["text"])
            self.assertEqual((1, [], False), read(1), "an unchanged scrollback is not sent again")
            self.assertIn("ok", read(1)[1][0]["text"])
            self.assertEqual(("%1", 5000), capture.call_args.args)

    def test_a_session_file_rewritten_in_place_is_read_again(self):
        from theseus.adapters import claude_code as transcripts
        self.session("1", os.getpid(), "$5:@3.%9", 100)
        self.assertEqual("abc-123", transcripts.session_of(self.sessions, "@3.%9")[0])
        stamp = os.stat(self.sessions).st_mtime_ns
        self.session("1", os.getpid(), "$5:@3.%9", 200, session="after-clear-session")
        os.utime(self.sessions, ns=(stamp, stamp))
        self.assertEqual("after-clear-session", transcripts.session_of(self.sessions, "@3.%9")[0])

    def test_a_long_transcript_is_read_from_its_tail(self):
        from theseus.adapters import claude_code as transcripts
        path = self.tmp / "long.jsonl"
        filler = self.entry(type="user", message={"content": "x" * 1000})
        usage = {"input_tokens": 5, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        path.write_text(filler * 1000 + self.entry(type="assistant", message={"usage": usage}))
        state = transcripts.read(path)
        self.assertEqual(5, state["tokens"])
        self.assertEqual(path.stat().st_size, state["offset"])

    def test_context_prefers_the_transcript_then_the_pane_hint(self):
        from theseus import agents
        from types import SimpleNamespace
        conf = SimpleNamespace(section=lambda name: {}, root=self.tmp)
        saved = agents.profiles if hasattr(agents, "profiles") else None
        from theseus import profiles
        original = profiles.resolve
        profiles.resolve = lambda conf, name: {"context_limit": 1000000} if name == "big" else {}
        self.addCleanup(setattr, profiles, "resolve", original)
        self.assertEqual({"tokens": 5, "limit": 1000000, "model": "m", "source": "transcript"},
                         agents.context_use(conf, "big", {"tokens": 5, "model": "m"}, 9))
        self.assertEqual({"tokens": 9, "limit": 200000, "model": None, "source": "pane hint"},
                         agents.context_use(conf, "small", {"tokens": None}, 9))
        self.assertIsNone(agents.context_use(conf, "small", None, None))
        del saved

    def test_the_context_limit_follows_the_transcript_model(self):
        from theseus import agents, profiles
        from types import SimpleNamespace
        table = {"context": {"claude-*": 1000000, "claude-haiku-4-5*": 200000, "lbl/cborg-coder-ultra": 229376}}
        conf = SimpleNamespace(section=lambda name: table if name == "models" else {}, root=self.tmp)
        original = profiles.resolve
        profiles.resolve = lambda conf, name: {"context_limit": 123}
        self.addCleanup(setattr, profiles, "resolve", original)
        limit = lambda model: agents.context_use(conf, "p", {"tokens": 5, "model": model}, None)["limit"]
        self.assertEqual(1000000, limit("claude-opus-5-5"))
        self.assertEqual(200000, limit("claude-haiku-4-5"))
        self.assertEqual(229376, limit("lbl/cborg-coder-ultra"))
        self.assertEqual(123, limit("some-other-model"))
        self.assertEqual(123, agents.context_use(conf, "p", None, 9)["limit"])


class HostLabelTests(unittest.TestCase):
    def test_host_views_show_project_slash_short_and_project_tabs_the_bare_name(self):
        from theseus.tui import rows as R
        shorts = {"proj2": {"proj2-gw-4": "gw-4"}}
        self.assertEqual("proj2/gw-4", host.agent_label("proj2", "proj2-gw-4", shorts))
        self.assertEqual("proj2/gone-1", host.agent_label("proj2", "proj2-gone-1", shorts))
        record = {"id": 1, "time": "2026-11-17T10:00:00+00:00", "kind": "assigned", "agent": "proj2-gw-4",
                  "project": "proj2"}
        [project_row] = R.event_rows([record])
        [host_row] = R.event_rows([{**record, "agentLabel": "proj2/gw-4"}], project=True)
        self.assertEqual("proj2-gw-4", project_row[1][2].plain)
        self.assertEqual("proj2/gw-4", host_row[1][3].plain)
        [gateway_row] = R.gateway_rows([{"id": 1, "project": "proj2", "agent": "proj2-gw-4",
                                         "agentLabel": "proj2/gw-4", "pool": "gw"}])
        self.assertEqual("proj2/gw-4", gateway_row[1][2].plain)
        [pool_row] = R.pool_rows([{"pool": "gw", "max": 4, "active": 1, "projects": {"proj2": 1}, "claims": [],
                                   "leases": {"proj2/proj2-gw-4": {"project": "proj2", "agent": "proj2-gw-4",
                                                                          "agentLabel": "proj2/gw-4"}}}])
        self.assertEqual("proj2/gw-4", pool_row[1][2].plain)


class ColorFallbackTests(unittest.TestCase):
    def test_activity_categories_keep_distinct_ansi_colors_on_a_16_color_console(self):
        import io
        from rich.console import Console
        from textual.theme import BUILTIN_THEMES
        from theseus.tui import charts, theme
        bins = lambda count: [0] * (host.BINS - 1) + [count]
        activity = {name: bins(n + 1) for n, (name, _) in enumerate(host.ACTIVITY_GROUPS)}
        try:
            for name, found in BUILTIN_THEMES.items():
                variables = found.to_color_system().generate()
                theme.apply(variables, found.dark, found.ansi, "standard")
                console = Console(color_system="standard", force_terminal=True, file=io.StringIO(), width=300)
                console.print(charts.legend(charts.kind_series(activity))[0])
                codes = re.findall(r"\033\[([0-9;]+)m■", console.file.getvalue())
                self.assertEqual(6, len(codes), name)
                self.assertEqual(6, len(set(codes)), f"{name}: {codes}")
                self.assertNotIn(codes[-1], codes[:-1])
                projects = {console.export_text if False else c for c in theme.PROJECTS}
                self.assertEqual(len(theme.PROJECTS), len(projects), name)
        finally:
            theme.apply()

    def test_project_colors_are_distinct_and_never_the_error_color(self):
        from rich.color import Color, ColorSystem
        from textual.theme import BUILTIN_THEMES
        from theseus.tui import theme
        try:
            for name, found in BUILTIN_THEMES.items():
                variables = found.to_color_system().generate()
                theme.apply(variables, found.dark, found.ansi, "standard")
                numbers = [int(c[6:-1]) if c.startswith("color(") else Color.parse(c).downgrade(ColorSystem.STANDARD).number
                           for c in theme.PROJECTS]
                self.assertEqual(len(numbers), len(set(numbers)), name)
                self.assertFalse({1, 9} & set(numbers), name)
                if not found.ansi:
                    theme.apply(variables, found.dark, False, "truecolor")
                    error = theme.hue_of(Color.parse(theme.RED).triplet)
                    for color in theme.PROJECTS:
                        gap = abs((theme.hue_of(Color.parse(color).triplet) - error + 180) % 360 - 180)
                        self.assertGreaterEqual(gap, 30, f"{name} {color}")
        finally:
            theme.apply()


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")


class PrivateTmuxTests(Sandbox):
    """The tmux option syntax of jump_to and go_back on a private, throwaway tmux server (never the user's)."""

    def test_the_back_option_is_valid_tmux_syntax(self):
        socket = f"theseus-test-{os.getpid()}"
        env = {**os.environ, "TMUX_TMPDIR": str(self.tmp)}
        env.pop("TMUX", None)
        tmux = lambda *args: subprocess.run(["tmux", "-L", socket, "-f", "/dev/null", *args], capture_output=True,
                                            text=True, env=env)
        self.assertEqual(0, tmux("new-session", "-d", "-s", "probe").returncode)
        self.addCleanup(tmux, "kill-server")
        option = top.back_option("/dev/pts/9")
        result = tmux("set-option", "-g", option, "%0")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("%0", tmux("show-options", "-gqv", option).stdout.strip())


class JumpBackTests(Sandbox):
    """jump_to and go_back against a fake tmux that records its arguments (never a real tmux server)."""

    FAKE = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_STATE/calls"
case "$1" in
    display-message)
        if [ "$2" = "-p" ]; then
            case "$4" in
                %0) echo '$1 @1' ;;
                %7) echo '$2 @5' ;;
                *) echo '$1' ;;
            esac
        fi ;;
    list-clients) echo /dev/pts/9 ;;
    show-options) cat "$FAKE_STATE/option" 2>/dev/null ;;
    set-option) printf '%s' "$4" > "$FAKE_STATE/option" ;;
esac
"""

    def setUp(self):
        super().setUp()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "tmux").write_text(self.FAKE)
        (bin_dir / "tmux").chmod(0o755)
        os.environ.update({"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_STATE": str(self.tmp),
                           "TMUX": "/tmp/fake-socket,1,0", "TMUX_PANE": "%0"})

    def calls(self):
        return (self.tmp / "calls").read_text().splitlines()

    def test_jump_remembers_the_dashboard_pane_and_tells_the_client_how_to_return(self):
        self.assertEqual("switched to %7", top.jump_to("%7"))
        calls = self.calls()
        self.assertIn("set-option -g @theseus-back--dev-pts-9 %0", calls)
        self.assertIn("switch-client -c /dev/pts/9 -t %7", calls)
        notice = next(c for c in calls if c.startswith("display-message -c /dev/pts/9"))
        self.assertIn("prefix + L (last session)", notice)
        self.assertIn("theseus top --back", notice)
        self.assertLess(calls.index("set-option -g @theseus-back--dev-pts-9 %0"),
                        calls.index("switch-client -c /dev/pts/9 -t %7"))

    def test_back_switches_the_same_client_to_the_remembered_pane(self):
        top.jump_to("%7")
        os.environ["TMUX_PANE"] = "%7"
        ok, message = top.go_back()
        self.assertTrue(ok, message)
        self.assertEqual("switch-client -c /dev/pts/9 -t %0", self.calls()[-1])
        result = self.theseus("top", "--back", env={**self.env, **{k: os.environ[k] for k in ("PATH", "FAKE_STATE", "TMUX", "TMUX_PANE")}},
                              check=False, cwd=self.tmp)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("switched back to %0", result.stdout)

    def test_back_without_a_remembered_pane_fails_cleanly(self):
        ok, message = top.go_back()
        self.assertFalse(ok)
        self.assertIn("--back", message)


class ChartTests(unittest.TestCase):
    def project(self, name, assigned=0, delivered=0, merged=0, errors=0, available=True):
        bins = lambda count: [0] * (host.BINS - 1) + [count]
        activity = {"assigned": bins(assigned), "delivered": bins(delivered), "partial": bins(0), "blocked": bins(0),
                    "merged": bins(merged), "errors": bins(errors)}
        return {"name": name, "root": f"/{name}", "available": available, "activity": activity if available else None}

    def test_project_series_sum_every_group_per_bin_with_project_colors(self):
        from theseus.tui import charts, theme
        series = charts.project_series([self.project("alpha", 1, 2, 3, 1), self.project("beta", merged=2),
                                        self.project("gone", available=False)])
        self.assertEqual(["alpha", "beta"], [label for label, _, _ in series])
        self.assertEqual([7, 2], [values[-1] for _, _, values in series])
        self.assertEqual([0, 0], [sum(values[:-1]) for _, _, values in series])
        self.assertEqual([theme.project_color("alpha"), theme.project_color("beta")], [style for _, style, _ in series])

    def test_kind_series_keep_the_state_colors_and_order(self):
        from theseus.tui import charts, theme
        series = charts.kind_series(self.project("alpha", 1, 2, 3, 4)["activity"])
        names = ["assigned", "delivered", "partial", "blocked", "merged", "errors"]
        self.assertEqual([theme.SERIES[name] for name in names], [style for _, style, _ in series])
        self.assertEqual(theme.RED, series[-1][1])
        self.assertEqual([1, 2, 0, 0, 3, 4], [values[-1] for _, _, values in series])
        self.assertTrue(all(len(values) == host.BINS for _, _, values in charts.kind_series(None)))

    def test_stacked_bars_draw_one_row_per_event_on_a_baseline_with_a_time_axis(self):
        from theseus.tui import charts
        series = [("a", "green", [0] * 46 + [1, 2]), ("b", "red", [0] * 46 + [2, 0])]
        lines = [line.plain for line in charts.stacked_bars(series, 148, 8)]
        self.assertEqual(8, len(lines))
        self.assertTrue(all(len(line) <= 148 for line in lines))
        plot = lines[:-2]
        self.assertEqual([" "] * 4 + ["█"] * 2, [line[len(plot[0]) - 2] for line in plot])
        self.assertEqual([" "] * 3 + ["█"] * 3, [line[len(plot[0]) - 5] for line in plot])
        self.assertIn("└", lines[-2])
        self.assertEqual(lines[-2].index("┴"), lines[-1].index("-12h"))
        self.assertTrue(lines[-1].endswith("now"))
        red = {span.style for span in charts.stacked_bars(series, 148, 8)[3].spans}
        self.assertIn("red", red)

    def test_narrow_charts_merge_bins_and_keep_every_event(self):
        from theseus.tui import charts
        values = list(range(host.BINS))
        self.assertEqual(sum(values), sum(charts.merge_bins(values, 5)))
        self.assertEqual(values[-5:], list(range(43, 48)))
        self.assertEqual(sum(values[-5:]), charts.merge_bins(values, 5)[-1])
        lines = charts.stacked_bars([("a", "green", values)], 30, 10)
        self.assertTrue(all(len(line.plain) <= 30 for line in lines))
        self.assertIn("now", lines[-1].plain)

    def test_legend_wraps_and_the_chart_keeps_its_time_axis(self):
        from theseus.tui import charts
        series = charts.kind_series(self.project("alpha", 1, 2, 3, 4)["activity"])
        self.assertEqual(1, len(charts.legend(series)))
        wrapped = charts.legend(series, 60)
        self.assertGreater(len(wrapped), 1)
        self.assertTrue(all(len(line) <= 60 for line in wrapped))
        self.assertEqual("■ assigned 1   ■ delivered 2   ■ partial 0   ■ blocked 0   ■ merged/completed 3   ■ errors/stalled 4",
                         " ".join(line.plain for line in charts.legend(series)))

    def test_agent_rows_show_plain_cpu_memory_and_a_context_meter(self):
        from theseus.tui import rows, theme
        self.assertEqual(("CPU (core %)", "MEM (RAM)", "context"), rows.AGENT_COLUMNS[4:7])
        agent = {"name": "a", "pane": "%1", "state": "working", "task": None, "room": None, "idleMinutes": None}
        state = {"agents": [{**agent, "cpu": 250.0, "rss": 2 * 2**30,
                             "context": {"tokens": 170000, "limit": 200000, "source": "transcript"}},
                            {**agent, "name": "b", "cpu": 40.0, "rss": 2**30,
                             "context": {"tokens": 87400, "limit": 200000, "source": "pane hint"}},
                            {**agent, "name": "c", "cpu": None, "rss": None},
                            {**agent, "name": "d", "cpu": 5.0, "rss": 2**28,
                             "context": {"tokens": 120000, "limit": 200000, "source": "transcript"}}],
                 "workers": [], "events": [], "host": {"memTotal": 8 * 2**30}}
        found = {key.split(":")[1]: cells[4:7] for key, cells, _ in rows.agent_rows(state)}
        cells = {name: [c.plain for c in triple] for name, triple in found.items()}
        self.assertEqual(["250%", "2.0G 25%", "███▍ 170k/200k"], cells["a"])
        self.assertEqual(["40%", "1.0G 12%", "█▋░░ 87k/200k ~"], cells["b"])
        self.assertEqual(["-", "-", "-"], cells["c"])
        self.assertEqual(["5%", "256.0M  3%", "██▍░ 120k/200k"], cells["d"])
        self.assertEqual("right", found["a"][0].justify)
        self.assertEqual([theme.RED, theme.YELLOW, ""], [found[name][1].style for name in ("a", "b", "d")])
        self.assertEqual([theme.RED, theme.GREEN, theme.YELLOW], [found[name][2].style for name in ("a", "b", "d")])

    def test_agent_capability_and_task_minimum_columns_are_narrow_and_drop_out_first(self):
        from types import SimpleNamespace
        from theseus.tui import rows
        from theseus.tui.app import Table
        state = {"agents": [{"name": "a", "pane": "%1", "state": "free", "capability": 2}], "workers": [], "events": [],
                 "host": {}}
        self.assertEqual("2", rows.agent_rows(state)[0][1][rows.AGENT_COLUMNS.index("cap")].plain)
        queue = {key: [] for key in ("review", "merged", "ready", "blocked", "held", "in_progress")}
        queue["ready"] = ["t1", "t2"]
        state = {"queue": queue, "rooms": {}, "times": {}, "capabilities": {"t1": 3}, "events": [], "eligible": {},
                 "gated": {}, "blocked": {}, "paused": False, "agents": []}
        found = {key.split(":")[2]: cells[rows.WAITING_COLUMNS.index("min")].plain
                 for key, cells, _ in rows.waiting_rows(state)}
        self.assertEqual({"t1": "3", "t2": ""}, found)
        for name, columns in (("agents", rows.AGENT_COLUMNS), ("waiting", rows.WAITING_COLUMNS)):
            short = {"agents": "cap", "waiting": "min"}[name]
            table = SimpleNamespace(names=columns, widths=rows.WIDTHS[name], optional=rows.OPTIONAL[name])
            self.assertIn(short, Table.fit(table, 200)[0])
            self.assertEqual(3, Table.fit(table, 200)[1][short])
            widths = rows.WIDTHS[name]
            rest = [c for c in columns if c != short]
            fits_rest = 1 + 2 * len(rest) + sum(widths[c] if isinstance(widths[c], int) else 6 for c in rest)
            self.assertEqual(rest, Table.fit(table, fits_rest)[0], f"{short} drops out before any other column")
            self.assertEqual(list(columns), Table.fit(table, fits_rest + 5)[0])

    def test_history_and_meters(self):
        from theseus.tui import charts
        lines = [line.plain for line in charts.history([0, 50, 100], 20, 5, seconds=60)]
        self.assertEqual(5, len(lines))
        self.assertTrue(lines[0].startswith("100%"))
        self.assertEqual("█", lines[0].rstrip()[-1])
        self.assertEqual("now", lines[-1].split()[-1])
        self.assertIn("-10m", lines[-1])
        self.assertEqual(lines[-2].index("┴"), lines[-1].index("-10m"))
        five = [line.plain for line in charts.history([10.0] * 200, 66, 6, seconds=5)]
        self.assertEqual(["-4m", "-3m", "-2m", "-1m", "now"], five[-1].split())
        self.assertEqual("█████░░░░░", charts.meter(0.5, 10).plain)
        self.assertEqual("░░░░", charts.meter(None, 4).plain)
        self.assertEqual(4, len(charts.meter(2.0, 4).plain))


class ResourceTests(unittest.TestCase):
    def test_ring_keeps_the_newest_samples_oldest_first(self):
        from theseus.tui.resources import Ring
        ring = Ring(3)
        self.assertEqual(([], None, 0), (ring.values(), ring.latest(), len(ring)))
        for value in range(5):
            ring.push(value)
        self.assertEqual([2, 3, 4], ring.values())
        self.assertEqual([3, 4], ring.values(2))
        self.assertEqual([], ring.values(0))
        self.assertEqual((4, 3), (ring.latest(), len(ring)))

    def test_cpu_sampler_reports_busy_percent_since_the_previous_read(self):
        from theseus.tui.resources import CpuSampler
        frames = iter(["cpu 100 0 100 800 0 0 0 0\ncpu0 50 0 50 400 0 0 0 0\ncpu1 50 0 50 400 0 0 0 0\n",
                       "cpu 200 0 200 1400 0 0 0 0\ncpu0 150 0 50 400 0 0 0 0\ncpu1 50 0 150 800 0 0 0 0\n",
                       "cpu 200 0 200 1400 0 0 0 0\n"])
        sampler = CpuSampler(lambda: next(frames))
        first = sampler.sample()
        self.assertAlmostEqual(25.0, first["total"])
        self.assertEqual([100.0, 20.0], [round(v, 1) for v in first["cores"]])
        self.assertIsNone(sampler.sample())

    def test_history_is_filled_by_refreshes_not_by_redraws(self):
        from theseus.tui.app import TopApp
        app = TopApp(interval=3600, snapshot_fn=lambda: None, sampler=SimpleNamespace(sample=lambda: {"total": 10.0, "cores": []}))
        asyncio.run(app.finish(None, "boom", {"total": 10.0, "cores": []}))
        asyncio.run(app.finish(None, "boom", {"total": 30.0, "cores": []}))
        app.redraw()
        self.assertEqual([10.0, 30.0], app.history.values())


if __name__ == "__main__":
    unittest.main()
