"""Host registry, host snapshot and the Textual dashboard (driven with Pilot, a fake tmux and temporary registries)."""
import asyncio
import json
import os
import shutil
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from helpers import Sandbox

from test_queue import FAKE_TMUX
from theseus import config as cfg
from theseus import host, registry, store, top
from theseus import workqueue as wq


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

    def test_jump_refuses_outside_tmux(self):
        self.assertIn("tmux session", top.jump_to("%1"))


class DashboardTests(HostCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from theseus.tui.app import TopApp
        self.jumps = []
        self.app = TopApp(interval=3600, snapshot_fn=host.snapshot, jump_fn=lambda pane: self.jumps.append(pane) or f"jumped {pane}")

    async def settle(self, pilot, condition=None):
        """Wait for the first refresh to be applied and its tabs mounted (slow machines need more than one pause)."""
        from textual.widgets import TabbedContent, TabPane
        await self.app.workers.wait_for_complete()
        ready = condition or (lambda: self.app.snap is not None
                              and len(list(self.app.query_one(TabbedContent).query(TabPane))) == 3)
        for _ in range(100):
            await pilot.pause(0.05)
            if ready():
                return
        self.fail("dashboard did not settle")

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
            self.assertEqual(2, self.rows("projects"))

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
            self.assertTrue(wq.paused(conf))
            await self.settle(pilot)
            await pilot.press("p")
            self.assertFalse(wq.paused(conf))

    async def test_enter_on_an_agent_row_jumps_to_its_pane(self):
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.settle(pilot)
            await pilot.press("2", "a", "down", "enter")
            self.assertEqual(["%2"], self.jumps)


if __name__ == "__main__":
    unittest.main()
