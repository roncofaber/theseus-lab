"""The host store through the CLI and the project registry: the version guard on every command with restore, the
registry's throttled writes, hidden projects and name conflicts reported by doctor."""
import os
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


class RegistryTests(Sandbox):
    def make_project(self, label):
        root = self.tmp / label
        root.mkdir()
        (root / "theseus.toml").write_text("[project]\n")
        return root

    def conf(self, root, name="alpha"):
        return SimpleNamespace(root=root, name=name, data=root / "data",
                               section=lambda section: {"coordinator": "coord"} if section == "instances" else {})

    def test_record_writes_the_project_at_most_once_a_minute(self):
        root = self.make_project("alpha")
        self.assertTrue(registry.record(self.conf(root), now=1000.0))
        [entry] = registry.entries()
        self.assertEqual(
            {"name": "alpha", "root": str(root), "data": str(root / "data"), "coordinator": "coord", "last_seen": 1000.0}, entry)
        self.assertFalse(registry.record(self.conf(root), now=1059.0))
        self.assertEqual(1000.0, registry.entries()[0]["last_seen"])
        self.assertTrue(registry.record(self.conf(root), now=1061.0))
        self.assertEqual(1061.0, registry.entries()[0]["last_seen"])

    def test_hides_projects_whose_root_lost_its_config_without_deleting_them(self):
        first, second = self.make_project("alpha"), self.make_project("beta")
        registry.record(self.conf(first), now=1.0)
        registry.record(self.conf(second, "beta"), now=1.0)
        (first / "theseus.toml").unlink()
        self.assertEqual(["beta"], [e["name"] for e in registry.entries()])
        registry.record(self.conf(second, "beta"), now=500.0)
        with store.opened() as conn:
            self.assertEqual(["alpha", "beta"], [p["name"] for p in store.list_projects(conn)])

    def test_a_failed_write_never_raises(self):
        root = self.make_project("alpha")
        blocker = self.tmp / "blocker"
        blocker.write_text("not a directory")
        os.environ["THESEUS_DB"] = str(blocker / "theseus.db")
        self.assertFalse(registry.record(self.conf(root)))
        self.assertEqual([], registry.entries())

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

