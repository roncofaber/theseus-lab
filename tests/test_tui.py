"""Host registry, host snapshot and the Textual dashboard (driven with Pilot, a fake tmux and temporary registries)."""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from helpers import REPO, Sandbox

from test_queue import FAKE_TMUX
from theseus import config as cfg
from theseus import host, registry, store, top
from theseus import workqueue as wq

ALLOWED_GLYPHS = re.compile("[\u00b7\u2026\u2190-\u21ff\u2500-\u257f\u2580-\u259f\u25a0-\u25ff]")


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
    def test_commands_register_both_projects(self):
        self.assertEqual(["alpha", "beta"], [e["name"] for e in registry.entries()])
        listed = self.theseus("projects").stdout
        self.assertIn(str(self.project), listed)
        self.assertIn(str(self.second), listed)

    def test_snapshot_has_projects_pools_and_merged_events(self):
        self.event("alpha", "assigned", 5)
        self.event("beta", "delivered", 1, "t2")
        state = host.snapshot()
        self.assertEqual(["alpha", "beta"], [p["name"] for p in state["projects"]])
        self.assertTrue(all(p["available"] for p in state["projects"]))
        self.assertEqual(2, len(state["projects"][0]["snapshot"]["agents"]))
        self.assertEqual([("alpha", "assigned"), ("beta", "delivered")], [(e["project"], e["kind"]) for e in state["events"]])
        self.assertEqual(1, state["projects"][0]["activity"]["assigned"][-1])
        self.assertEqual(1, state["projects"][1]["activity"]["delivered"][-1])
        self.assertEqual([], state["pools"])

    def test_pools_are_listed(self):
        (self.tmp / "pools.toml").write_text('[pools.gw]\nmax = 3\n')
        [pool] = host.snapshot()["pools"]
        self.assertEqual(("gw", 3, 0), (pool["pool"], pool["max"], pool["active"]))

    def test_broken_and_missing_projects_are_unavailable_not_fatal(self):
        (self.second / "theseus.toml").write_text("this is [not toml")
        state = host.snapshot([self.tmp / "nowhere"])
        by_name = {p["name"]: p for p in state["projects"]}
        self.assertTrue(by_name["alpha"]["available"])
        self.assertFalse(by_name["beta"]["available"])
        self.assertIn("ConfigError", by_name["beta"]["error"])
        self.assertFalse(by_name["nowhere"]["available"])

    def test_cli_host_summary_and_json(self):
        plain = self.theseus("top", "--host", "--once").stdout
        for word in ("alpha", "beta", "POOLS", "EVENTS"):
            self.assertIn(word, plain)
        data = json.loads(self.theseus("top", "--host", "--json").stdout)
        self.assertEqual(["alpha", "beta"], [p["name"] for p in data["projects"]])

    def test_top_outside_a_project_shows_the_host(self):
        out = self.theseus("top", "--once", cwd=self.tmp).stdout
        self.assertIn("alpha", out)
        self.assertIn("beta", out)

    def test_activity_bins(self):
        now = time.time()
        stamp = lambda minutes: datetime.fromtimestamp(now - minutes * 60, timezone.utc).isoformat()
        found = host.activity([{"time": stamp(1), "kind": "assigned"}, {"time": stamp(16), "kind": "merged"},
                               {"time": stamp(13 * 60), "kind": "error"}], now=now)
        self.assertEqual(1, found["assigned"][-1])
        self.assertEqual(1, found["merged"][-2])
        self.assertEqual(0, sum(found["errors"]))

    def test_a_worktree_never_registers_as_a_project(self):
        from theseus import config as cfg
        conf = cfg.load(root=self.project)
        worktree = self.tmp / "wt-of-alpha"
        self.git("commit", "-q", "--allow-empty", "-m", "base", cwd=self.project)
        self.git("worktree", "add", "-q", "--detach", str(worktree), cwd=self.project)
        self.assertFalse(registry.record(cfg.Config(worktree, conf.raw), now=10 ** 9))
        self.assertNotIn(str(worktree), [e["root"] for e in registry.entries()])

    def test_default_send_capture_and_keys_go_through_the_verified_send_and_tmux(self):
        from theseus.tui import app
        os.environ.update({"THESEUS_SEND_VERIFY": "0", "THESEUS_SEND_POLL": "0.01"})
        conf = cfg.load(root=self.project)
        agent = next(a for a in host.snapshot()["projects"][0]["snapshot"]["agents"] if a["name"] == "worker-a")
        message = app.send_verified(conf, agent, "hello there")
        self.assertIn("worker-a", message)
        typed = lambda: (self.tmp / "state" / "typed").read_text()
        self.assertIn("-t %1 -l hello there", typed())
        self.assertEqual("", top.send_keys("%1", "Escape"))
        self.assertIn("send-keys -t %1 Escape", typed())
        self.assertIsInstance(top.capture("%1"), str)

    def test_jump_refuses_outside_tmux(self):
        self.assertIn("tmux session", top.jump_to("%1"))


class DashboardTests(HostCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from theseus.tui.app import TopApp
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
        for _ in range(100):
            await pilot.pause(0.05)
            if ready():
                return
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

    async def test_enter_or_a_double_click_on_a_project_row_opens_its_tab(self):
        from textual.widgets import TabbedContent
        from theseus.tui.app import pane_id
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.settle(pilot)
            table = self.app.query_one("#projects-table")
            await self.settle(pilot, lambda: self.app.focused is table and table.row_count == 2)
            await pilot.press("enter")
            await self.settle(pilot, lambda: self.app.query_one(TabbedContent).active == pane_id(self.project))
            await pilot.press("1")
            await self.settle(pilot, lambda: self.app.query_one(TabbedContent).active == "tab-host")
            await pilot.click(table, offset=(4, 2), times=2)
            await self.settle(pilot, lambda: self.app.query_one(TabbedContent).active == pane_id(self.second))

    async def test_charts_show_every_project_and_kind_with_a_time_axis(self):
        from theseus.tui.app import BarChart
        self.event("alpha", "assigned", 5)
        self.event("beta", "merged", 20, "t2")
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.settle(pilot)
            await self.settle(pilot, lambda: self.app.query_one("#host-chart", BarChart).series)
            host_chart = self.app.query_one("#host-chart", BarChart).render().plain
            for word in ("■ alpha 1", "■ beta 1", "-12h", "now", "└"):
                self.assertIn(word, host_chart)
            await self.switch(pilot, "2")
            await self.settle(pilot, lambda: self.app.active_view().query_one("#chart", BarChart).size.width > 0)
            project_chart = self.app.active_view().query_one("#chart", BarChart).render().plain
            self.assertIn("■ assigned 1", project_chart)
            self.assertIn("■ errors/stalled 0", project_chart)

    async def test_project_rows_follow_projects_appearing_and_disappearing(self):
        from textual.widgets import DataTable
        from theseus.tui.app import TopApp
        self.event("alpha", "assigned", 5)
        shown = {"count": 2}

        def snapshot():
            state = host.snapshot()
            state["projects"] = state["projects"][:shown["count"]]
            return state

        app = TopApp(interval=3600, snapshot_fn=snapshot)
        self.app = app
        async with app.run_test(size=(170, 60)) as pilot:
            await self.settle(pilot, lambda: app.query_one("#host-view").project_names() == ["alpha", "beta"])
            for count, names in ((1, ["alpha"]), (2, ["alpha", "beta"])):
                shown["count"] = count
                await pilot.press("r")
                await self.settle(pilot, lambda: app.query_one("#host-view").project_names() == names
                                  and app.query_one("#host-view #events", DataTable).row_count == 1)

    async def test_screen_shows_only_box_block_geometric_and_arrow_glyphs(self):
        from textual.widgets import TabbedContent
        self.event("alpha", "assigned", 5)
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.settle(pilot)
            for key, tab in zip("123", self.app.tab_ids()):
                await pilot.press(key)
                await self.settle(pilot, lambda: self.app.query_one(TabbedContent).active == tab)
                await pilot.pause()
                screen = self.app.export_screenshot()
                self.assertEqual(set(), {c for c in screen if ord(c) > 127 and not ALLOWED_GLYPHS.match(c)})

    async def test_opens_on_the_current_project_tab(self):
        from textual.widgets import TabbedContent
        from theseus.tui.app import TopApp, pane_id
        app = TopApp(interval=3600, current=self.second, snapshot_fn=host.snapshot)
        async with app.run_test(size=(170, 60)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(pane_id(self.second), app.query_one(TabbedContent).active)

    async def test_filter_narrows_rows_and_escape_clears_it(self):
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.settle(pilot)
            await self.switch(pilot, "2")
            self.assertEqual(2, self.rows("agents"))
            await pilot.press("slash", "minus", "b")
            await pilot.pause()
            self.assertEqual(1, self.rows("agents"))
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(2, self.rows("agents"))

    async def test_pause_key_toggles_the_queue_flag(self):
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.settle(pilot)
            await pilot.press("2")
            conf = cfg.load(root=self.project)
            self.assertEqual(str(self.project), self.app.active_item()["root"])
            self.assertFalse(wq.paused(conf))
            await pilot.press("p")
            await self.app.workers.wait_for_complete()
            self.assertTrue(wq.paused(conf))
            await self.settle(pilot)
            await pilot.press("p")
            await self.app.workers.wait_for_complete()
            self.assertFalse(wq.paused(conf))

    async def test_enter_on_an_agent_row_opens_its_popup_whose_button_jumps_to_its_pane(self):
        from theseus.tui.popups import AgentScreen
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.settle(pilot)
            await pilot.press("2", "a", "down", "enter")
            await self.settle(pilot, lambda: isinstance(self.app.screen, AgentScreen))
            self.assertIn("%2", self.app.screen.text())
            await pilot.click("#open")
            await self.settle(pilot, lambda: self.jumps == ["%2"])
            await pilot.press("escape")
            await self.settle(pilot, lambda: len(self.app.screen_stack) == 1)


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


def calibration(rounds=9):
    """Median CPU seconds of a fixed workload of Rich text and sorting: the unit of the refresh-cost budget."""
    from rich.text import Text as RichText

    def work():
        rows = []
        for n in range(3000):
            line = RichText(f"row {n} " * 3, style="bold")
            line.append(str(n * 7), style="dim")
            rows.append((line.plain, line.cell_len))
        rows.sort()
        return len(rows)
    costs = []
    for _ in range(rounds):
        started = time.process_time()
        work()
        costs.append(time.process_time() - started)
    return sorted(costs)[rounds // 2]


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

    async def asyncSetUp(self):
        from theseus.tui.app import TopApp
        self.event("alpha", "assigned", 5)
        self.event("alpha", "delivered", 3, "t2")
        self.event("alpha", "error", 2, "t3")
        self.base = host.snapshot()
        self.extra = {}
        self.jumps, self.sent, self.keys, self.captures = [], [], [], []
        # The terminal's color system is pinned: CI runners have no TERM or COLORTERM.
        self.app = TopApp(interval=3600, snapshot_fn=lambda: grown(self.base, **self.extra), tmux_ok=True,
                          color_system="truecolor",
                          mirror_seconds=0.05, jump_fn=lambda pane: self.jumps.append(pane) or f"jumped {pane}",
                          capture_fn=lambda pane: self.captures.append(pane) or "\033[32mready for input\033[0m\n> ",
                          send_fn=lambda conf, agent, text, expect_busy=True: self.sent.append(
                              (agent["pane"], text, expect_busy)) or "sent",
                          keys_fn=lambda pane, *keys: self.keys.append((pane, keys)) or "")

    async def settle(self, pilot, condition):
        await self.app.workers.wait_for_complete()
        for _ in range(100):
            await pilot.pause(0.05)
            if condition():
                return
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

    def rendered_rows(self, name):
        table = self.table(name)
        return [[str(cell.plain if hasattr(cell, "plain") else cell) for cell in table.get_row(row.key)]
                for row in table.ordered_rows]

    def setUpApp(self):
        from theseus.tui.app import TopApp
        old = self.app
        self.app = TopApp(interval=3600, snapshot_fn=old.snapshot_fn, tmux_ok=True, mirror_seconds=0.05,
                          jump_fn=old.jump_fn, capture_fn=old.capture_fn, send_fn=old.send_fn, keys_fn=old.keys_fn,
                          color_system="truecolor", cli_fn=old.cli_fn)


class TopPolishTests(DashboardBase):
    """No default highlight, sorting, help, popups, the pane mirror, the layout at several sizes, GPUs, themes and
    the refresh cost."""

    async def test_no_row_is_highlighted_until_asked_and_escape_or_a_click_elsewhere_clears_it(self):
        self.extra = {"tasks": 3}
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.start(pilot)
            tables = [self.table(name) for name in ("agents", "waiting", "events")]
            self.assertTrue(all(table.row_count for table in tables))
            self.assertEqual([False] * 3, [table.show_cursor for table in tables])
            await pilot.press("r")
            await self.settle(pilot, lambda: not self.app.busy)
            await pilot.press("1", "2")
            await self.settle(pilot, lambda: self.app.active_item() is not None)
            self.assertEqual([False] * 3, [table.show_cursor for table in tables])
            agents = tables[0]
            await pilot.click(agents, offset=(4, 1))
            await self.settle(pilot, lambda: agents.show_cursor)
            self.assertEqual(1, len(self.app.screen_stack), "the first click on the row under the cursor opened a popup")
            self.assertEqual(0, agents.cursor_row)
            await pilot.click("#status", offset=(2, 0))
            await self.settle(pilot, lambda: not agents.show_cursor)
            await pilot.press("w")
            await self.settle(pilot, lambda: tables[1].show_cursor)
            await pilot.press("escape")
            await self.settle(pilot, lambda: not tables[1].show_cursor and self.app.focused is None)
            await pilot.press("e", "down")
            await self.settle(pilot, lambda: tables[2].show_cursor and tables[2].cursor_row == 1)

    async def test_events_sort_by_header_click_or_keys_with_an_indicator(self):
        self.extra = {"events": 12}
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.start(pilot)
            events = self.table("events")
            labels = lambda: [column.label.plain for column in events.columns.values()]
            self.assertEqual("time ▼", labels()[0])
            times = [record["time"] for record in self.app.active_view().records.values()]
            shown = [self.app.active_view().records[row.key.value]["time"] for row in events.ordered_rows]
            self.assertEqual(sorted(times, reverse=True), shown)
            await pilot.press("s")
            await pilot.pause()
            self.assertEqual(["time  ", "kind ▲"], labels()[:2])
            kinds = [row[1] for row in self.rendered_rows("events")]
            self.assertEqual(sorted(kinds), kinds)
            await pilot.press("S")
            await pilot.pause()
            self.assertEqual("kind ▼", labels()[1])
            kinds = [row[1] for row in self.rendered_rows("events")]
            self.assertEqual(sorted(kinds, reverse=True), kinds)
            agent_x = sum(column.get_render_width(events) for column in list(events.columns.values())[:2]) + 2
            await pilot.click(events, offset=(agent_x, 0))
            await pilot.pause()
            self.assertEqual("agent ▲", labels()[2])
            await pilot.click(events, offset=(agent_x, 0))
            await pilot.pause()
            self.assertEqual("agent ▼", labels()[2])
            agents = [row[2] for row in self.rendered_rows("events")]
            self.assertEqual(sorted(agents, reverse=True), agents)
            self.assertNotIn("newest first", self.app.active_view().query_one("#events-panel").border_title or "")

    async def test_help_lists_every_binding_and_blocks_dashboard_keys_while_open(self):
        from theseus.tui.app import TopApp
        from theseus.tui.popups import HelpScreen
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.start(pilot)
            await pilot.press("question_mark")
            await self.settle(pilot, lambda: isinstance(self.app.screen, HelpScreen))
            body = self.app.screen.text()
            for binding in TopApp.BINDINGS:
                self.assertIn(binding.tooltip or binding.description, body)
            for words in ("double-click", "click an EVENTS header", "Open tmux pane", "of system RAM", "Actions (Ctrl+P)", "C-q detaches"):
                self.assertIn(words, body)
            conf = cfg.load(root=self.project)
            await pilot.press("p", "r", "1")
            await pilot.pause(0.2)
            self.assertFalse(wq.paused(conf))
            self.assertIsNotNone(self.app.active_item())
            await pilot.press("escape")
            await self.settle(pilot, lambda: len(self.app.screen_stack) == 1)
            await pilot.press("f1")
            await self.settle(pilot, lambda: isinstance(self.app.screen, HelpScreen))
            await pilot.press("question_mark")
            await self.settle(pilot, lambda: len(self.app.screen_stack) == 1)

    async def test_enter_on_waiting_and_event_rows_opens_their_details(self):
        from theseus.tui.popups import Dialog
        self.extra = {"tasks": 3, "events": 3}
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.start(pilot)
            await pilot.press("w", "enter")
            await self.settle(pilot, lambda: isinstance(self.app.screen, Dialog))
            body = self.app.screen.text()
            for words in ("task-000", "owned paths", "docs/task-000.md", "acceptance", "Objective 0.", "createdAt"):
                self.assertIn(words, body)
            await pilot.press("escape")
            await self.settle(pilot, lambda: len(self.app.screen_stack) == 1)
            await pilot.press("e")
            events = self.table("events")
            events.move_cursor(row=events.get_row_index("event:alpha:10002"))
            await pilot.press("enter")
            await self.settle(pilot, lambda: isinstance(self.app.screen, Dialog))
            body = self.app.screen.text()
            self.assertIn("detail 2 xx", body)
            self.assertIn("linked task", body)
            await pilot.click("#close")
            await self.settle(pilot, lambda: len(self.app.screen_stack) == 1)

    async def test_agent_popup_mirrors_the_pane_sends_lines_and_keys_and_stops_polling_when_closed(self):
        from theseus.tui.popups import AgentScreen, Confirm
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.start(pilot)
            agents = self.table("agents")
            await pilot.click(agents, offset=(4, 1), times=2)
            await self.settle(pilot, lambda: isinstance(self.app.screen, AgentScreen))
            self.assertEqual(2, len(self.app.screen_stack))
            screen = self.app.screen
            await self.settle(pilot, lambda: "ready for input" in screen.query_one("#mirror-text").render().plain)
            self.assertEqual("worker-a · live", str(screen.query_one("#mirror").border_title))
            self.assertFalse(screen.query("#note"))
            await pilot.click("#send")
            await pilot.press(*"hello", "enter")
            await self.settle(pilot, lambda: self.sent == [("%1", "hello", True)])
            await pilot.click("#compact")
            await self.settle(pilot, lambda: self.sent[-1] == ("%1", "/compact", False))
            await pilot.click("#key-escape")
            await self.settle(pilot, lambda: ("%1", ("Escape",)) in self.keys)
            await pilot.click("#interrupt")
            await self.settle(pilot, lambda: isinstance(self.app.screen, Confirm))
            await pilot.press("n")
            await self.settle(pilot, lambda: self.app.screen is screen)
            self.assertNotIn(("%1", ("C-c",)), self.keys)
            await pilot.click("#interrupt")
            await self.settle(pilot, lambda: isinstance(self.app.screen, Confirm))
            await pilot.press("y")
            await self.settle(pilot, lambda: ("%1", ("C-c",)) in self.keys)
            await pilot.press("escape")
            await self.settle(pilot, lambda: len(self.app.screen_stack) == 1)
            await pilot.pause(0.2)
            count = len(self.captures)
            await pilot.pause(0.3)
            self.assertEqual(count, len(self.captures))
            self.assertIsNone(screen.timer._task, "the mirror timer still runs after the popup closed")

    async def test_the_open_pane_button_needs_tmux(self):
        from theseus.tui.popups import AgentScreen
        self.app.tmux_ok = False
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.start(pilot)
            await pilot.press("a", "enter")
            await self.settle(pilot, lambda: isinstance(self.app.screen, AgentScreen))
            self.assertFalse(self.app.screen.query("#open"))
            await pilot.press("o")
            await pilot.pause(0.1)
            self.assertEqual([], self.jumps)

    async def test_every_section_fits_without_scrolling_the_screen(self):
        from theseus.tui.app import Panel
        self.extra = {"agents": 6, "events": 40, "tasks": 12, "gpus": 2}
        for size in ((80, 24), (120, 40), (200, 60)):
            async with self.app.run_test(size=size) as pilot:
                await self.start(pilot, tab=None)
                for tab in ("1", "2"):
                    await pilot.press(tab)
                    await pilot.pause(0.1)
                    view = self.app.active_view()
                    self.assertEqual(0, view.max_scroll_y, f"{size} tab {tab} scrolls as a whole")
                    for panel in view.query(Panel):
                        if not panel.display:
                            continue
                        region = panel.region
                        self.assertGreaterEqual(region.height, 3, f"{size} tab {tab} {panel.id} {region}")
                        self.assertLessEqual(region.bottom, size[1] - 1, f"{size} tab {tab} {panel.id} {region}")
                        self.assertLessEqual(region.right, size[0], f"{size} tab {tab} {panel.id} {region}")
            self.setUpApp()

    async def test_gpu_block_shows_one_line_per_gpu_and_collapses_without_gpus(self):
        for count, size in ((0, (170, 50)), (1, (170, 50)), (4, (170, 50)), (4, (80, 30)), (8, (200, 60))):
            self.extra = {"gpus": count}
            self.setUpApp()
            async with self.app.run_test(size=size) as pilot:
                await self.start(pilot, tab=None)
                await pilot.pause(0.1)
                gpus = self.app.base.query_one("#gpus")
                self.assertEqual(bool(count), gpus.display)
                if count:
                    lines = gpus.render().plain.splitlines()
                    self.assertEqual(count + 1, len(lines))
                    self.assertIn("Fake GPU", lines[0])
                    self.assertLessEqual(gpus.region.right, size[0])
                    self.assertGreater(gpus.region.height, count)
                self.assertNotIn("GPU0", self.app.base.query_one("#meters").render().plain)

    async def test_resizing_repeatedly_raises_nothing(self):
        async with self.app.run_test(size=(120, 40)) as pilot:
            await self.start(pilot, tab=None)
            for tab in ("1", "2"):
                await pilot.press(tab)
                for size in ((80, 24), (200, 60), (60, 20), (170, 50), (100, 30)) * 3:
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
            await pilot.pause(0.2)

    async def test_refresh_cost_with_50_agents_500_events_and_200_tasks(self):
        self.extra = {"agents": 50, "events": 500, "tasks": 200}
        asyncio.get_running_loop().set_debug(False)
        async with self.app.run_test(size=(200, 60)) as pilot:
            await self.start(pilot)
            costs = []
            for n in range(15):
                snap = grown(self.base, agents=50, events=500 + 3 * n, tasks=200)
                for number, agent in enumerate(snap["projects"][0]["snapshot"]["agents"]):
                    agent["cpu"], agent["rss"] = float((n * 7 + number) % 300), 2**27 * (1 + (n + number) % 40)
                started = time.process_time()
                await self.app.apply(snap)
                await pilot.pause()
                costs.append(time.process_time() - started)
            average = sorted(costs[1:])[len(costs[1:]) // 2]
            unit = calibration()
            load = os.getloadavg()[0] / (os.cpu_count() or 1)
            print(f"\nrefresh cost (CPU time of apply and the repaint) with 50 agents, 500 events, 200 tasks: "
                  f"median {average * 1000:.1f} ms, {average / unit:.1f} calibration units (first {costs[0] * 1000:.1f} ms,"
                  f" unit {unit * 1000:.2f} ms, load {load:.2f} per CPU)")
            if load > 1.0:
                self.skipTest(f"host load {load:.2f} per CPU makes CPU timings meaningless")
            # Measured against a fixed Python workload on the same machine, so a slower runner moves both; about 9 units
            # on a workstation, so the budget of 20 catches a threefold regression without flaking on CI.
            self.assertLess(average / unit, 20 * float(os.environ.get("THESEUS_PERF_SLACK", "1")))

    async def test_tables_are_readable_and_activity_colors_differ_from_errors_under_every_builtin_theme(self):
        from rich.color import Color as RichColor
        from textual.theme import BUILTIN_THEMES
        from theseus.tui import theme

        def rgb(color, terminal, foreground):
            if color is None or color.is_default:
                return (terminal.foreground_color if foreground else terminal.background_color)[:3]
            return color.get_truecolor(terminal, foreground)[:3]

        async with self.app.run_test(size=(170, 50)) as pilot:
            await self.start(pilot)
            self.addCleanup(theme.apply)
            for name in BUILTIN_THEMES:
                self.app.theme = name
                await pilot.pause(0.1)
                self.assertEqual("ansi" if BUILTIN_THEMES[name].ansi else "rgb", theme.MODE, name)
                table = self.table("events")
                strip = table.render_line(0)
                terminal = self.app.ansi_theme
                ratios = [theme.contrast(rgb(seg.style.color, terminal, True), rgb(seg.style.bgcolor, terminal, False))
                          for seg in strip if seg.text.strip() and seg.style]
                self.assertTrue(ratios, name)
                self.assertGreaterEqual(min(ratios), 4.5, f"{name}: header contrast {min(ratios):.2f}")
                error = theme.SERIES["errors"]
                self.assertEqual(6, len(set(theme.SERIES.values())), name)
                for category in ("assigned", "delivered", "partial", "blocked", "merged"):
                    self.assertNotEqual(error, theme.SERIES[category], f"{name} {category}")
                    if theme.MODE == "rgb":
                        gap = abs((theme.hue_of(RichColor.parse(theme.SERIES[category]).triplet)
                                   - theme.hue_of(RichColor.parse(error).triplet) + 180) % 360 - 180)
                        self.assertGreaterEqual(gap, 30, f"{name} {category} too close to the error hue")


class RoundTwoTests(DashboardBase):
    """The CPU history axis and mean, GATEWAY rows and popup, closing popups by a click outside, the pane mirror's
    horizontal scroll and type-through mode, one-row buttons, formatted popups and the agent columns."""

    async def test_cpu_shows_the_mean_of_every_thread_and_the_history_has_a_time_axis(self):
        from theseus.tui.app import HistoryChart, resource_text
        state = {"cpus": 4, "load": [1.0, 1.0, 1.0], "memTotal": 8, "memAvailable": 4, "swapTotal": 0, "swapFree": 0}
        text = resource_text(state, {"total": 30.0, "cores": [10.0, 20.0, 0.0, 90.0]}).plain
        self.assertIn("30.0%  avg of 4, max 90%", text)
        async with self.app.run_test(size=(170, 50)) as pilot:
            await self.start(pilot, tab=None)
            for value in range(60):
                self.app.history.push(float(value))
            chart = self.app.base.query_one("#cpu-history", HistoryChart)
            chart.seconds = 5
            chart.refresh()
            await pilot.pause()
            lines = chart.render().plain.splitlines()
            self.assertTrue(lines[-1].endswith("now"))
            self.assertIn("┴", lines[-2])
            self.assertRegex(lines[-1], r"-\d+(s|m)")

    async def test_gateway_rows_show_who_what_attempt_outcome_and_pool_and_open_a_popup(self):
        from theseus.tui.popups import Dialog
        self.extra = {"gateway": True}
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.start(pilot, tab=None)
            table = self.app.base.query_one("#gateway")
            rows = [[cell.plain for cell in table.get_row(row.key)] for row in table.ordered_rows]
            self.assertEqual(["alpha", "worker-a", "t1", "429 parallel cap", "1/3"], rows[0][1:6])
            self.assertTrue(rows[0][6].startswith("retry at "))
            self.assertTrue(rows[0][7].startswith("cborg cap -1 until"))
            self.assertEqual(["conn lost", "2/3", "recovered"], rows[1][4:7])
            title = self.app.base.query_one("#gateway-panel").border_title
            self.assertIn("cborg cap -1 until", str(title))
            table.focus()
            table.reveal()
            await pilot.press("enter")
            await self.settle(pilot, lambda: isinstance(self.app.screen, Dialog))
            body = self.app.screen.text()
            for words in ("gateway error 429 parallel cap", "api_key: [redacted]", "error text (redacted)", "1 of 3",
                          "cap -1 until"):
                self.assertIn(words, body)

    async def test_a_click_outside_a_popup_closes_it(self):
        from theseus.tui.popups import Confirm, HelpScreen
        async with self.app.run_test(size=(170, 50)) as pilot:
            await self.start(pilot)
            await pilot.press("question_mark")
            await self.settle(pilot, lambda: isinstance(self.app.screen, HelpScreen))
            await pilot.click(offset=(0, 2))
            await self.settle(pilot, lambda: len(self.app.screen_stack) == 1)
            answers = []
            self.app.push_screen(Confirm("sure?"), answers.append)
            await self.settle(pilot, lambda: isinstance(self.app.screen, Confirm))
            await pilot.click(offset=(1, 1))
            await self.settle(pilot, lambda: answers == [False])

    async def test_mirror_scrolls_sideways_and_types_through_to_the_pane(self):
        from theseus.tui.popups import AgentScreen, Confirm
        self.app.mirror_seconds = 1.0
        self.app.capture_fn = lambda pane: self.captures.append(pane) or ("x" * 400 + "\n> ")
        async with self.app.run_test(size=(170, 50)) as pilot:
            await self.start(pilot)
            await pilot.press("a", "enter")
            await self.settle(pilot, lambda: isinstance(self.app.screen, AgentScreen))
            screen = self.app.screen
            mirror = screen.query_one("#mirror")
            await self.settle(pilot, lambda: mirror.max_scroll_x > 0)
            self.assertEqual(1.0, screen.timer._interval)
            buttons = list(screen.query("Button"))
            self.assertTrue(buttons)
            self.assertEqual({1}, {button.region.height for button in buttons})
            await pilot.press("i")
            await self.settle(pilot, lambda: screen.typing)
            self.assertIn("INSERT", str(mirror.border_title))
            self.assertEqual(0.25, screen.timer._interval)
            await pilot.press("a", "b", "enter", "backspace", "up", "tab", "q", "o", "ctrl+u")
            await self.settle(pilot, lambda: len(self.keys) >= 7 and not screen.pumping)
            sent = [keys for _, keys in self.keys]
            typed = "".join(keys[1] for keys in sent if keys[0] == "-l")
            self.assertEqual("abqo", typed)
            self.assertEqual([("Enter",), ("BSpace",), ("Up",), ("Tab",), ("C-u",)], [k for k in sent if k[0] != "-l"])
            self.assertIs(self.app.screen, screen, "q and o went to the pane, not to the dashboard")
            self.assertEqual([], self.jumps)
            await pilot.press("ctrl+c")
            await self.settle(pilot, lambda: isinstance(self.app.screen, Confirm))
            await pilot.press("n")
            await self.settle(pilot, lambda: self.app.screen is screen)
            self.assertNotIn(("C-c",), [keys for _, keys in self.keys])
            await pilot.press("escape")
            await pilot.pause(0.7)
            await self.settle(pilot, lambda: ("Escape",) in [keys for _, keys in self.keys])
            self.assertTrue(screen.typing, "a lone Escape goes to the pane")
            await pilot.press("escape", "escape")
            await self.settle(pilot, lambda: not screen.typing)
            self.assertEqual(1.0, screen.timer._interval)
            self.assertEqual(1, [keys for _, keys in self.keys].count(("Escape",)))
            await pilot.press("i", "ctrl+right_square_bracket")
            await self.settle(pilot, lambda: not screen.typing)
            await pilot.press("escape")
            await self.settle(pilot, lambda: len(self.app.screen_stack) == 1)

    async def test_popups_have_titled_sections_bullets_and_colored_values(self):
        from theseus.tui.popups import AgentScreen, Dialog
        from theseus.tui import theme
        self.extra = {"tasks": 2}
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.start(pilot)
            await pilot.press("w", "enter")
            await self.settle(pilot, lambda: isinstance(self.app.screen, Dialog))
            body = self.app.screen.text()
            for section in ("task task-000", "objective", "contract", "acceptance"):
                self.assertRegex(body, rf"(?m)^── {section} ─+ *$")
            self.assertIn("▸ Done.", body)
            self.assertIn("▸ docs/task-000.md", body)
            grid = next(part for part in self.app.screen.body.renderables if hasattr(part, "columns"))
            self.assertEqual("dim", grid.columns[0].style)
            await pilot.press("escape")
            await self.settle(pilot, lambda: len(self.app.screen_stack) == 1)
            await pilot.press("a", "enter")
            await self.settle(pilot, lambda: isinstance(self.app.screen, AgentScreen))
            body = self.app.screen.text()
            self.assertRegex(body, r"(?m)^── agent worker-a ─+ *$")
            for words in ("state", "pane", "CPU", "memory", "context", "worktree"):
                self.assertRegex(body, rf"(?m)^{words} ")

    async def test_agent_columns_read_clearly_at_80_and_120_columns(self):
        self.extra = {"agents": 2}
        for size in ((80, 30), (120, 40)):
            self.setUpApp()
            async with self.app.run_test(size=size) as pilot:
                await self.start(pilot)
                table = self.table("agents")
                labels = [column.label.plain for column in table.columns.values()]
                self.assertEqual(["agent", "pane", "state", "task", "CPU (core %)", "MEM (RAM)"], labels[:6])
                if size[0] >= 120:
                    self.assertEqual("context", labels[6])
                extra = next(row for row in table.ordered_rows if row.key.value.startswith("agent:extra-00"))
                cells = [cell.plain if hasattr(cell, "plain") else cell for cell in table.get_row(extra.key)]
                self.assertEqual("3%", cells[4])
                self.assertRegex(cells[5], r"^256\.0M +\d+%$")
                screen = "\n".join(strip.text for strip in self.app.screen._compositor.render_strips())
                self.assertIn("CPU (core %)  MEM (RAM)", screen, size)
                self.assertRegex(screen, r"\d[KMG] +\d+%  ")
                self.assertLessEqual(max(len(line.rstrip()) for line in screen.splitlines()), size[0])


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

    def screen_text(self):
        return "\n".join(strip.text for strip in self.app.screen._compositor.render_strips())

    async def test_the_overview_shows_coordinator_watcher_and_supervised_agents(self):
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.start(pilot)
            screen = self.screen_text()
            for words in ("coordinator lead busy", "watcher queue wait", "fake-1", "private"):
                self.assertIn(words, screen)

    async def test_a_supervised_agent_popup_has_tabs_fields_and_confirmed_lifecycle_actions(self):
        from theseus.tui.popups import AgentScreen, Confirm
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.start(pilot)
            await pilot.press("a", "enter")
            await self.settle(pilot, lambda: isinstance(self.app.screen, AgentScreen))
            popup = self.app.screen
            body = popup.text()
            for words in ("some/model", "fake-agent", "abcdef12", "shared", "gen 2 · restarts 1"):
                self.assertIn(words, body)
            self.assertNotIn("abcdef1234567", body)
            self.assertEqual("fake-1 · live", str(popup.query_one("#mirror").border_title))
            await pilot.press("v")
            await self.settle(pilot, lambda: popup.query_one("#tabs").active == "events")
            await pilot.press("R")
            await self.settle(pilot, lambda: isinstance(self.app.screen, Confirm))
            await pilot.press("n")
            await self.settle(pilot, lambda: self.app.screen is popup)
            self.assertEqual([], self.ran)
            await pilot.press("R")
            await self.settle(pilot, lambda: isinstance(self.app.screen, Confirm))
            await pilot.press("y")
            await self.settle(pilot, lambda: self.ran == [["agents", "restart", "worker-a"]])

    async def test_the_coordinator_opens_with_c_and_mirrors_its_pane(self):
        from theseus.tui.popups import AgentScreen
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.start(pilot)
            await pilot.press("C")
            await self.settle(pilot, lambda: isinstance(self.app.screen, AgentScreen) and "%9" in self.captures)
            self.assertEqual("lead · live", str(self.app.screen.query_one("#mirror").border_title))

    async def test_the_palette_runs_actions_and_asks_before_destructive_ones(self):
        from textual.command import CommandPalette
        from theseus.tui.popups import Confirm
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.start(pilot)
            for words in ("Queue tick", "Restart fake-1"):
                await pilot.press("ctrl+p")
                await self.settle(pilot, lambda: isinstance(self.app.screen, CommandPalette))
                await pilot.press(*words)
                await pilot.pause(0.5)
                await pilot.press("enter")
                await pilot.pause(0.2)
            await self.settle(pilot, lambda: isinstance(self.app.screen, Confirm))
            self.assertEqual([["queue", "tick"]], self.ran)
            await pilot.press("y")
            await self.settle(pilot, lambda: self.ran == [["queue", "tick"], ["agents", "restart", "worker-a"]])

    async def test_tables_never_scroll_sideways(self):
        for size in ((80, 40), (120, 40), (170, 50)):
            self.setUpApp()
            async with self.app.run_test(size=size) as pilot:
                await self.start(pilot)
                for name in ("agents", "waiting", "events"):
                    table = self.table(name)
                    self.assertEqual(0, table.max_scroll_x, (size, name))

    def test_hand_made_agents_get_distinct_short_names(self):
        from theseus import agents
        infos = [{"name": f"team-gw-{n}", "display": f"team-gw-{n}"} for n in (1, 2)] + [
            {"name": "x-cborg-1", "display": "cborg-1", "managed": True}]
        agents.shorten(infos)
        self.assertEqual(["gw-1", "gw-2", "cborg-1"], [info["display"] for info in infos])

    def test_the_diff_shows_changed_and_new_files(self):
        import subprocess
        from theseus.tui.app import task_diff
        worktree = self.tmp / "wt"
        worktree.mkdir()
        run = lambda *args: subprocess.run(["git", "-C", str(worktree), *args], check=True, capture_output=True, env=self.env)
        run("init", "-q")
        (worktree / "a.txt").write_text("one\n")
        run("add", "a.txt")
        run("commit", "-q", "-m", "base")
        base = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        (worktree / "a.txt").write_text("two\n")
        (worktree / "new.txt").write_text("new\n")
        text = task_diff({"worktree": str(worktree), "baseCommit": base}).plain
        self.assertIn("a.txt", text)
        self.assertIn("+two", text)
        self.assertIn("new.txt (new)", text)

    def test_palette_actions_are_cli_commands_and_destructive_ones_ask(self):
        from theseus.tui import commands
        item = grown(self.base, tasks=2)["projects"][0]
        found = {title: (argv, question) for title, _, argv, question in commands.project_commands(item)}
        self.assertEqual((["queue", "tick"], None), found["Queue tick"])
        self.assertEqual(["agents", "scale", "fake", "3"], found["Add a fake agent (2 to 3)"][0])
        self.assertTrue(found["Remove a fake agent (2 to 1)"][1])
        self.assertEqual(["agents", "restart", "worker-a"], found["Restart fake-1"][0])
        self.assertTrue(found["Restart fake-1"][1])
        self.assertEqual((["tasks", "hold", "task-000"], None), found["Hold task-000"])
        self.assertTrue(found["Dispatch task-000"][1])
        self.assertEqual(["tasks", "priority", "task-000", "1"], found["Raise the priority of task-000 (0 to 1)"][0])


class TranscriptTests(Sandbox):
    """Session lookup from a client's sessions directory and the incremental transcript reader (temporary files only)."""

    def setUp(self):
        super().setUp()
        from theseus import transcripts
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
        from theseus import transcripts
        self.session("1", os.getpid(), "$5:@3.%9", 100)
        self.session("2", 2**22 + 12345, "$5:@3.%9", 200, session="dead")
        self.session("3", os.getpid(), "$5:@3.%10", 300, session="other")
        self.assertEqual(("abc-123", self.cwd), transcripts.session_of(self.sessions, "@3.%9"))
        self.assertIsNone(transcripts.session_of(self.sessions, "1"), "a fake tmux answer never matches")
        self.assertEqual(self.tmp / "claude/projects/-work-my-project-worktree-1/abc-123.jsonl",
                         transcripts.transcript_path(self.sessions, "abc-123", self.cwd))

    def test_the_reader_follows_appends_and_reports_context_and_api_errors(self):
        from theseus import transcripts
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

    def test_a_session_file_rewritten_in_place_is_read_again(self):
        from theseus import transcripts
        self.session("1", os.getpid(), "$5:@3.%9", 100)
        self.assertEqual("abc-123", transcripts.session_of(self.sessions, "@3.%9")[0])
        stamp = os.stat(self.sessions).st_mtime_ns
        self.session("1", os.getpid(), "$5:@3.%9", 200, session="after-clear-session")
        os.utime(self.sessions, ns=(stamp, stamp))
        self.assertEqual("after-clear-session", transcripts.session_of(self.sessions, "@3.%9")[0])

    def test_a_long_transcript_is_read_from_its_tail(self):
        from theseus import transcripts
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

    def test_project_colors_are_stable_across_processes(self):
        from theseus.tui import theme
        names = ["alpha", "beta", "gamma"]
        here = [theme.project_color(name) for name in names]
        script = f"from theseus.tui import theme; print([theme.project_color(n) for n in {names!r}])"
        for seed in ("1", "2"):
            env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(REPO / "src")}
            out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, check=True).stdout
            self.assertEqual(str(here), out.strip())
        self.assertTrue(all(re.fullmatch("#[0-9a-f]{6}", color) for color in here))


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
