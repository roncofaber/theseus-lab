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
            await self.settle(pilot, lambda: len(self.app.active_view().cards()) == 2)
            self.assertEqual(["alpha", "beta"], [card.project for card in self.app.active_view().cards()])

    async def test_enter_or_a_click_on_a_project_card_opens_its_tab(self):
        from textual.widgets import TabbedContent
        from theseus.tui.app import pane_id
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.settle(pilot)
            await self.settle(pilot, lambda: self.app.focused is next(iter(self.app.active_view().cards()), None))
            await pilot.press("enter")
            await self.settle(pilot, lambda: self.app.query_one(TabbedContent).active == pane_id(self.project))
            await pilot.press("1")
            await self.settle(pilot, lambda: self.app.query_one(TabbedContent).active == "tab-host")
            card = self.app.query_one(f"#card-{pane_id(self.second)}")
            await self.settle(pilot, lambda: self.app.screen.get_widget_at(card.region.x + 2, card.region.y + 1)[0] is card)
            await pilot.click(card, offset=(2, 1))
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

    async def test_screen_has_only_allowed_glyphs_and_no_em_dash(self):
        self.event("alpha", "assigned", 5)
        async with self.app.run_test(size=(170, 60)) as pilot:
            await self.settle(pilot)
            for key in ("1", "2", "3"):
                await pilot.press(key)
                await pilot.pause()
                screen = self.app.export_screenshot()
                self.assertNotIn(chr(0x2014), screen)
                self.assertIsNone(re.search("[" + chr(0x2600) + "-" + chr(0x27BF) + chr(0x1F300) + "-" + chr(0x10FFFF) + "]",
                                            screen))

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


class ChartTests(unittest.TestCase):
    def project(self, name, assigned=0, delivered=0, merged=0, errors=0, available=True):
        bins = lambda count: [0] * (host.BINS - 1) + [count]
        activity = {"assigned": bins(assigned), "delivered": bins(delivered), "merged": bins(merged), "errors": bins(errors)}
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
        from theseus.tui import charts
        series = charts.kind_series(self.project("alpha", 1, 2, 3, 4)["activity"])
        self.assertEqual(["green", "magenta", "cyan", "red"], [style for _, style, _ in series])
        self.assertEqual([1, 2, 3, 4], [values[-1] for _, _, values in series])
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

    def test_history_and_meters(self):
        from theseus.tui import charts
        lines = [line.plain for line in charts.history([0, 50, 100], 20, 5, seconds=60)]
        self.assertEqual(5, len(lines))
        self.assertTrue(lines[0].startswith("100%"))
        self.assertEqual("█", lines[0].rstrip()[-1])
        self.assertIn("-3m", lines[-1])
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
