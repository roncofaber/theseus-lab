"""The host store through the CLI: one-time migration of a project's legacy files, `theseus db` backup, restore,
check and prune, the daily housekeeping of the queue tick, the version guard, project name conflicts and doctor."""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from helpers import Sandbox

from theseus import registry, setup, store


class StoreCliTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        self.db = Path(self.env["THESEUS_DB"])
        self.backups = Path(self.env["THESEUS_BACKUPS"])

    def events(self):
        with store.opened(self.db) as conn:
            return store.events(conn, "demo-port")

    def test_legacy_project_files_are_imported_once_and_left_in_place(self):
        data = self.tmp / "data"
        (data / "queue").mkdir(parents=True, exist_ok=True)
        legacy = [{"time": "2026-11-16T10:00:00Z", "kind": "assigned", "agent": "old-agent", "task": "t1", "detail": ""},
                  {"time": "2026-11-16T10:30:00Z", "kind": "delivered", "agent": "old-agent", "task": "t1", "detail": ""}]
        (data / "queue/events.jsonl").write_text("".join(json.dumps(line) + "\n" for line in legacy))
        (data / "agents.json").write_text(json.dumps({"agents": [
            {"name": "old-agent", "pane": "%7", "provider": "claude-code", "enabled": False, "registered": "2026-11-01T00:00:00Z"}]}))
        self.assertRegex(self.theseus("agents").stdout, r"old-agent\s+%7")
        self.assertEqual(["assigned", "delivered"], [e["kind"] for e in self.events()])
        self.theseus("tasks", "add", "new-task", "--role", "research", "--owned", "docs/investigations/new-task.md",
                     "--objective", "Do it.", "--contract", "All variants.", "--accept", "Done.")
        for _ in range(2):
            log = self.theseus("queue", "log").stdout
            self.assertEqual(1, log.count("delivered"))
        self.assertEqual(2, len(self.events()))
        self.theseus("agents", "enable", "old-agent")
        self.theseus("agents", "remove", "old-agent")
        self.assertNotIn("old-agent", self.theseus("agents").stdout, "agents.json is not imported again")
        self.assertTrue((data / "agents.json").exists())
        self.assertEqual(2, len((data / "queue/events.jsonl").read_text().splitlines()))

    def test_a_newer_database_stops_every_command_with_a_clear_error(self):
        self.theseus("queue", "status")
        copy = self.theseus("db", "backup").stdout.split()[1]
        connection = sqlite3.connect(self.db)
        connection.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 5}")
        connection.close()
        for args in (("queue", "status"), ("projects",), ("pool", "hold", "gw", "x", "--project", "p"), ("db", "check")):
            result = self.theseus(*args, check=False)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("newer than", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
        self.assertIn("restored", self.theseus("db", "restore", copy, "--yes").stdout)
        self.theseus("queue", "status")


class ProjectNameTests(Sandbox):
    def make_project(self, label):
        root = self.tmp / label
        root.mkdir()
        (root / "theseus.toml").write_text("[project]\n")
        return root

    def conf(self, root, name="alpha"):
        return SimpleNamespace(root=root, name=name, data=root / "data", section=lambda section: {})

    def test_the_first_live_root_keeps_a_name_and_doctor_reports_the_other(self):
        first, second = self.make_project("first"), self.make_project("second")
        self.assertTrue(registry.record(self.conf(first), now=1000.0))
        self.assertFalse(registry.record(self.conf(second), now=1000.0))
        self.assertEqual([str(first)], [e["root"] for e in registry.entries()])
        [conflict] = registry.conflicts()
        self.assertEqual(("alpha", str(first), str(second)), (conflict["name"], conflict["root"], conflict["other"]))
        rows = {name: (status, detail) for status, name, detail in setup.store_rows()}
        self.assertEqual("WARN", rows["project name"][0])
        self.assertIn(str(second), rows["project name"][1])
        for name in ("store", "store backup", "store maintenance"):
            self.assertIn(name, rows)
        self.assertIn(str(self.tmp / "host/theseus.db"), rows["store"][1])
        (first / "theseus.toml").unlink()
        self.assertTrue(registry.record(self.conf(second), now=2000.0))
        self.assertEqual([str(second)], [e["root"] for e in registry.entries()])
        self.assertEqual([], registry.conflicts())
        self.assertEqual("ok", {n: s for s, n, _ in setup.store_rows()}["project names"])

