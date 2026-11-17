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

    def test_events_keep_a_plain_text_audit_trail(self):
        from theseus import config as cfg, workqueue as wq
        conf = cfg.load(root=self.project)
        record = wq.event(conf, "note", "a1", "t1", "hello")
        self.assertIsNotNone(record["id"])
        [line] = (self.tmp / "data/queue/events.jsonl").read_text().splitlines()
        self.assertEqual({"time": record["time"], "kind": "note", "agent": "a1", "task": "t1", "detail": "hello"},
                         json.loads(line))
        self.assertEqual(["note"], [e["kind"] for e in wq.events(conf)])

    def test_restore_refuses_without_yes_and_saves_the_current_database_with_it(self):
        self.theseus("queue", "status")
        out = self.theseus("db", "backup").stdout
        copy = out.split()[1]
        self.assertIn("integrity_check ok", out)
        with store.opened(self.db) as conn:
            store.add_event(conn, "demo-port", "after-backup")
        refused = self.theseus("db", "restore", copy, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("--yes", refused.stderr)
        self.assertEqual(["after-backup"], [e["kind"] for e in self.events()])
        self.assertEqual([], list(self.backups.glob("*pre-restore*")))
        restored = self.theseus("db", "restore", copy, "--yes").stdout
        self.assertIn("restored", restored)
        self.assertEqual([], self.events())
        [saved] = self.backups.glob("*pre-restore*")
        with store.opened(saved) as conn:
            self.assertEqual(["after-backup"], [e["kind"] for e in store.events(conn, "demo-port")])

    def test_check_maintain_and_prune(self):
        self.theseus("queue", "status")
        check = self.theseus("db", "check").stdout
        for text in ("integrity     ok", f"schema        version {store.SCHEMA_VERSION}", "last backup   none",
                     "maintenance   never", "projects 1"):
            self.assertIn(text, check)
        self.assertIn("WAL truncated", self.theseus("db", "maintain").stdout)
        self.theseus("db", "backup")
        check = self.theseus("db", "check").stdout
        self.assertNotIn("maintenance   never", check)
        self.assertIn(str(self.backups), check.split("last backup")[1].splitlines()[0])
        with store.opened(self.db) as conn:
            store.upsert_project(conn, "vanished", root=self.tmp / "nowhere")
        self.assertIn("would remove vanished", self.theseus("db", "prune").stdout)
        self.assertIn("removed vanished", self.theseus("db", "prune", "--yes").stdout)
        self.assertIn("nothing to prune", self.theseus("db", "prune").stdout)

    def test_queue_tick_backs_up_and_maintains_once_a_day(self):
        self.theseus("queue", "tick")
        self.assertEqual(1, len(list(self.backups.glob("theseus-*.db"))))
        with store.opened(self.db) as conn:
            self.assertIsNotNone(store.get_meta(conn, "maintain.last"))
        self.theseus("queue", "tick")
        self.assertEqual(1, len(list(self.backups.glob("theseus-*.db"))))

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

    def test_restore_replaces_a_live_file_that_is_not_a_database(self):
        self.theseus("queue", "status")
        copy = self.theseus("db", "backup").stdout.split()[1]
        for suffix in ("-wal", "-shm"):
            Path(f"{self.db}{suffix}").unlink(missing_ok=True)
        self.db.write_bytes(b"garbage" * 4096)
        self.assertIn("restored", self.theseus("db", "restore", copy, "--yes").stdout)
        [saved] = self.backups.glob("*pre-restore*")
        self.assertEqual(b"garbage" * 4096, saved.read_bytes())
        self.assertIn("integrity     ok", self.theseus("db", "check").stdout)


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

    def test_doctor_shows_the_store(self):
        self.scaffold()
        out = self.theseus("doctor", check=False).stdout
        for text in ("store ", "store backup", "store maintenance", "project names", str(self.tmp / "host/theseus.db")):
            self.assertIn(text, out)
