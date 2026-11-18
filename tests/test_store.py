"""Host store: schema and migrations with the version guard, pool semantics under concurrent processes, events and
their activity bins, deliveries, samples, the importers and the one-time migration built on them, retention and
maintenance, backups with rotation and restore."""
import json
import multiprocessing
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import helpers  # noqa: F401  (isolates the host store for the whole run)
from theseus import store

SPEC = {"max": 2, "priority": ["first", "second"]}


def take_slot(db, project, barrier, spec, results):
    barrier.wait()
    with store.opened(db) as conn:
        allowed, _ = store.acquire(conn, "gw", project, "agent", "task", spec=spec)
    results.put((project, allowed))


def hold_slot(db, project, barrier, spec, results):
    barrier.wait()
    with store.opened(db) as conn:
        allowed, _ = store.hold(conn, "gw", project, "pane", "task", spec=spec)
    results.put((project, allowed))


def write_events(db, project, count, barrier):
    barrier.wait()
    with store.opened(db) as conn:
        for n in range(count):
            store.add_event(conn, project, "tick", detail=str(n))


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="theseus-store-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.saved = {k: os.environ.get(k) for k in ("THESEUS_DB", "XDG_DATA_HOME", "THESEUS_POOLS", "THESEUS_POOL_STATE",
                                                     "THESEUS_REGISTRY", "THESEUS_BACKUPS", "THESEUS_STORE_CONFIG",
                                                     "THESEUS_CONFIG")}
        self.addCleanup(self.restore)
        self.db = self.tmp / "host" / "theseus.db"
        os.environ["THESEUS_DB"] = str(self.db)
        os.environ["THESEUS_REGISTRY"] = str(self.tmp / "projects.json")
        os.environ["THESEUS_BACKUPS"] = str(self.tmp / "backups")
        os.environ["THESEUS_STORE_CONFIG"] = str(self.tmp / "store.toml")
        os.environ["THESEUS_CONFIG"] = str(self.tmp / "config.toml")
        os.environ["THESEUS_POOLS"] = str(self.tmp / "pools.toml")
        os.environ["THESEUS_POOL_STATE"] = str(self.tmp / "pool-state")
        (self.tmp / "pools.toml").write_text('[pools.gw]\nmax = 2\npriority = ["first", "second"]\n')
        self.conn = store.connect()
        self.addCleanup(self.conn.close)

    def restore(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def backdate(self, table, column, seconds):
        self.conn.execute(f"UPDATE {table} SET {column} = {column} - ?", (seconds,))


class SchemaTests(StoreCase):
    def test_the_path_comes_from_theseus_db_or_xdg_and_every_connection_gets_its_pragmas(self):
        self.assertEqual(self.db, store.path())
        self.assertTrue(self.db.exists())
        self.assertEqual("wal", self.conn.execute("PRAGMA journal_mode").fetchone()[0])
        self.assertEqual(1, self.conn.execute("PRAGMA foreign_keys").fetchone()[0])
        self.assertGreaterEqual(self.conn.execute("PRAGMA busy_timeout").fetchone()[0], 1000)
        self.assertEqual(2, self.conn.execute("PRAGMA auto_vacuum").fetchone()[0], "a fresh database uses incremental vacuum")
        del os.environ["THESEUS_DB"]
        os.environ["XDG_DATA_HOME"] = str(self.tmp / "xdg")
        self.assertEqual(self.tmp / "xdg/theseus/theseus.db", store.path())

    def test_migrations_run_once_and_reopening_changes_nothing(self):
        self.assertEqual(store.SCHEMA_VERSION, store.version(self.conn))
        store.add_event(self.conn, "first", "tick")
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"projects", "coordinator_sessions", "agents", "events", "pool_leases", "pool_claims",
                         "deliveries", "samples", "task_index"} <= tables)
        for _ in range(2):
            with store.opened() as again:
                self.assertEqual(store.SCHEMA_VERSION, store.version(again))
                self.assertEqual(1, len(store.events(again)))
        store.migrate(self.conn)
        self.assertEqual(1, len(store.events(self.conn)))

    def test_a_later_migration_applies_on_top_of_an_existing_database(self):
        saved = list(store.MIGRATIONS)
        self.addCleanup(lambda: setattr(store, "MIGRATIONS", saved))
        self.addCleanup(lambda: setattr(store, "SCHEMA_VERSION", len(saved)))
        store.MIGRATIONS = saved + [["ALTER TABLE events ADD COLUMN extra TEXT"]]
        store.SCHEMA_VERSION = len(store.MIGRATIONS)
        with store.opened() as conn:
            self.assertEqual(len(saved) + 1, store.version(conn))
            store.migrate(conn)
            columns = [r[1] for r in conn.execute("PRAGMA table_info(events)")]
        self.assertEqual(1, columns.count("extra"))

    def test_the_guard_refuses_a_fenced_database_and_an_old_schema_migrates(self):
        import pwd
        home = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".local/share/theseus")
        self.assertIn(home, os.environ["THESEUS_DB_GUARD"].split(os.pathsep), "the suite fences the real host store")
        self.assertEqual(os.environ["THESEUS_DB_GUARD"], helpers.base_env(self.tmp)["THESEUS_DB_GUARD"],
                         "and so does every theseus subprocess a test starts")
        os.environ["THESEUS_DB_GUARD"] = os.pathsep.join([str(self.tmp / "elsewhere"), str(self.tmp / "fenced")])
        os.environ["THESEUS_DB"] = str(self.tmp / "fenced/theseus.db")
        with self.assertRaisesRegex(store.StoreError, "refusing to open"):
            store.path()
        os.environ["THESEUS_DB"] = str(self.tmp / "open/theseus.db")
        self.assertEqual(self.tmp / "open/theseus.db", store.path())
        db = self.tmp / "v2.db"
        raw = sqlite3.connect(db)
        for statement in store.MIGRATIONS[0] + store.MIGRATIONS[1]:
            raw.execute(statement)
        raw.execute(store.MIGRATIONS[2][-1])
        raw.execute("PRAGMA user_version = 2")
        raw.commit()
        raw.close()
        conn = store.connect(db)
        try:
            self.assertEqual(store.SCHEMA_VERSION, store.version(conn))
            self.assertEqual([], store.gateway_pools(conn))
        finally:
            conn.close()


class ProjectAndAgentTests(StoreCase):
    def test_projects_upsert_keep_unset_fields_and_prune_by_missing_root_or_age(self):
        root = self.tmp / "alive"
        root.mkdir()
        store.upsert_project(self.conn, "alive", root=root, data=self.tmp / "data", coordinator="%1")
        store.upsert_project(self.conn, "alive")
        store.upsert_project(self.conn, "gone", root=self.tmp / "missing")
        store.upsert_project(self.conn, "old", root=root, seen=time.time() - 86400)
        alive = store.list_projects(self.conn)[0]
        self.assertEqual(("alive", str(root), "%1"), (alive["name"], alive["root"], alive["coordinator"]))
        self.assertTrue(alive["last_seen"].endswith("Z"))
        store.add_event(self.conn, "gone", "tick")
        store.upsert_agent(self.conn, "gone", "a1", pane="%2")
        self.assertEqual(["gone"], store.prune_projects(self.conn, dry_run=True))
        self.assertEqual(1, len(store.events(self.conn, project="gone")), "a dry run deletes nothing")
        self.assertEqual(["gone"], store.prune_projects(self.conn))
        self.assertEqual([], store.events(self.conn, project="gone"))
        self.assertEqual([], store.list_agents(self.conn, project="gone"))
        self.assertEqual(["old"], store.prune_projects(self.conn, older_than=3600))
        self.assertEqual(["alive"], [p["name"] for p in store.list_projects(self.conn)])

    def test_agents_upsert_list_disable_and_remove(self):
        store.upsert_agent(self.conn, "first", "a1", pane="%1", profile="claude-code", rooms=["clean"], source="theseus.toml")
        store.upsert_agent(self.conn, "first", "a2", pane="%2", enabled=False)
        store.upsert_agent(self.conn, "first", "a1", pane="%3", profile="codex", rooms=["clean"])
        agents = store.list_agents(self.conn, "first")
        self.assertEqual(["a1", "a2"], [a["name"] for a in agents])
        self.assertEqual(("%3", "codex", ["clean"], True), (agents[0]["pane"], agents[0]["profile"], agents[0]["rooms"],
                                                            agents[0]["enabled"]))
        self.assertEqual(["a1"], [a["name"] for a in store.list_agents(self.conn, "first", include_disabled=False)])
        self.assertTrue(store.set_enabled(self.conn, "first", "a1", False))
        [a1] = [a for a in store.list_agents(self.conn, "first") if a["name"] == "a1"]
        self.assertEqual(("%3", "codex", False), (a1["pane"], a1["profile"], a1["enabled"]), "only the flag changes")
        self.assertFalse(store.set_enabled(self.conn, "first", "missing", False))
        self.assertTrue(store.remove_agent(self.conn, "first", "a2"))
        self.assertFalse(store.remove_agent(self.conn, "first", "a2"))


class PoolTests(StoreCase):
    """Pool semantics test_pools.py does not cover: claim and hold expiry, the combined check-and-take and races."""

    def test_claims_expire_and_lower_priority_projects_resume(self):
        store.sync(self.conn, "gw", "second", {"b1": "t1", "b2": "t2"})
        store.may_start(self.conn, "gw", "first")
        store.sync(self.conn, "gw", "second", {"b1": "t1"})
        self.assertFalse(store.may_start(self.conn, "gw", "second")[0])
        self.backdate("pool_claims", "claimed_epoch", store.CLAIM_TTL)
        self.assertTrue(store.may_start(self.conn, "gw", "second")[0])
        self.assertEqual([], store.status(self.conn, "gw")["claims"])

    def test_holds_survive_sync_and_expire_at_their_until(self):
        store.hold(self.conn, "gw", "first", "manual", "t", hours=1)
        store.sync(self.conn, "gw", "first", {"a1": "t1"})
        store.sync(self.conn, "gw", "first", {})
        leases = store.status(self.conn, "gw")["leases"]
        self.assertEqual(["first/manual"], list(leases))
        self.assertIn("until", leases["first/manual"])
        self.backdate("pool_leases", "until_epoch", 3601)
        self.assertEqual(0, store.status(self.conn, "gw")["active"])

    def test_acquire_checks_and_takes_in_one_step(self):
        self.assertEqual((True, ""), store.acquire(self.conn, "gw", "second", "b1", "t1"))
        self.assertEqual((True, ""), store.acquire(self.conn, "gw", "second", "b2", "t2"))
        self.assertTrue(store.acquire(self.conn, "gw", "second", "b2", "t2b")[0], "refreshing an own slot needs no room")
        allowed, why = store.acquire(self.conn, "gw", "first", "a1", "t3")
        self.assertFalse(allowed)
        self.assertIn("full (2/2)", why)
        store.release(self.conn, "gw", "second", "b1")
        self.assertIn("yielding to first", store.acquire(self.conn, "gw", "second", "b3", "t4")[1])
        self.assertTrue(store.acquire(self.conn, "gw", "first", "a1", "t3")[0])

    def run_racers(self, target, projects, spec):
        fresh = self.tmp / "race" / "theseus.db"
        context = multiprocessing.get_context("spawn")
        barrier, results = context.Barrier(len(projects)), context.Queue()
        racers = [context.Process(target=target, args=(str(fresh), project, barrier, spec, results))
                  for project in projects]
        for racer in racers:
            racer.start()
        try:
            outcomes = [results.get(timeout=60) for _ in racers]
        finally:
            for racer in racers:
                racer.join(timeout=30)
                if racer.is_alive():
                    racer.kill()
                    racer.join()
        self.assertEqual([0] * len(racers), [r.exitcode for r in racers])
        with store.opened(fresh) as conn:
            return outcomes, store.status(conn, "gw", spec=spec)

    def test_concurrent_processes_never_take_more_than_max(self):
        spec = {"max": 3, "priority": []}
        outcomes, final = self.run_racers(take_slot, [f"p{n}" for n in range(12)], spec)
        self.assertEqual(3, sum(allowed for _, allowed in outcomes))
        self.assertEqual(3, final["active"])
        self.assertEqual(9, len(final["claims"]))

    def test_concurrent_manual_holds_never_take_more_than_max(self):
        spec = {"max": 2, "priority": []}
        outcomes, final = self.run_racers(hold_slot, [f"p{n}" for n in range(8)], spec)
        self.assertEqual(2, sum(allowed for _, allowed in outcomes))
        self.assertEqual(2, final["active"])


class EventTests(StoreCase):
    def test_events_filter_by_project_time_kind_and_limit_keeps_the_latest(self):
        base = time.time() - 600
        for n, kind in enumerate(["assigned", "delivered", "stalled", "delivered"]):
            store.add_event(self.conn, "first", kind, agent="a1", task=f"t{n}", at=base + n * 60)
        record = store.add_event(self.conn, "second", "delivered", task="x")
        self.assertIsNotNone(record["id"])
        self.assertTrue(record["time"].endswith("Z"))
        self.assertEqual(5, len(store.events(self.conn)))
        self.assertEqual(["t1", "t3"], [e["task"] for e in store.events(self.conn, "first", kinds=["delivered"])])
        self.assertEqual(["t2", "t3"], [e["task"] for e in store.events(self.conn, "first", limit=2)])
        self.assertEqual(["t2", "t3"], [e["task"] for e in store.events(self.conn, "first", since=base + 120)])
        self.assertEqual(["t2", "t3"], [e["task"] for e in store.events(self.conn, "first", since=store.iso(base + 120))])
        first_id = store.events(self.conn, "first")[0]["id"]
        self.assertEqual(4, len(store.events(self.conn, after_id=first_id)))

    def test_event_bins_are_dense_and_count_per_bin(self):
        since = 1_700_000_000
        for offset in (0, 10, 59, 61, 190):
            store.add_event(self.conn, "first", "delivered", at=since + offset)
        store.add_event(self.conn, "first", "stalled", at=since + 5)
        store.add_event(self.conn, "second", "delivered", at=since + 5)
        bins = store.event_bins(self.conn, "first", since, 60, kinds=["delivered"], until=since + 240)
        self.assertEqual([(since, 3), (since + 60, 1), (since + 120, 0), (since + 180, 1)], bins)
        self.assertEqual([5, 1, 0, 1], [c for _, c in store.event_bins(self.conn, None, since, 60, until=since + 240)])
        self.assertEqual(3, len(store.event_bins(self.conn, "first", since, 100, until=since + 250)))

    def test_concurrent_writers_lose_no_events(self):
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(4)
        writers = [context.Process(target=write_events, args=(str(self.db), f"p{n}", 25, barrier)) for n in range(4)]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(timeout=60)
            if writer.is_alive():
                writer.kill()
                writer.join()
        self.assertEqual([0] * 4, [w.exitcode for w in writers])
        self.assertEqual(100, len(store.events(self.conn)))


class ImportTests(StoreCase):
    def write_project(self):
        data = self.tmp / "data"
        (data / "queue").mkdir(parents=True)
        lines = [{"time": "2026-11-16T10:00:00Z", "kind": "assigned", "agent": "a1", "task": "t1", "detail": ""},
                 {"time": "2026-11-16T10:00:00Z", "kind": "assigned", "agent": "a1", "task": "t1", "detail": ""},
                 {"time": "2026-11-16T10:30:00Z", "kind": "delivered", "agent": "a1", "task": "t1", "detail": "ok"}]
        (data / "queue/events.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines) + "\nnot json\n")
        (data / "agents.json").write_text(json.dumps({"agents": [
            {"name": "a1", "pane": "%1", "provider": "codex", "enabled": True, "registered": "2026-11-01T00:00:00Z",
             "rooms": ["dirty"]},
            {"name": "a2", "pane": "%2", "provider": "claude-code", "enabled": False, "source": "theseus.toml"}]}))
        return data

    def test_project_import_is_idempotent_and_picks_up_appended_events(self):
        data = self.write_project()
        self.assertEqual({"agents": 2, "events": 3}, store.import_project(self.conn, "first", self.tmp, data))
        self.assertEqual({"agents": 2, "events": 0}, store.import_project(self.conn, "first", self.tmp, data))
        events = store.events(self.conn, "first")
        self.assertEqual(3, len(events), "identical lines are distinct events")
        self.assertEqual(1, len(store.deliveries(self.conn, "first")))
        agents = store.list_agents(self.conn, "first")
        self.assertEqual([("a1", "codex", ["dirty"], True), ("a2", "claude-code", None, False)],
                         [(a["name"], a["profile"], a["rooms"], a["enabled"]) for a in agents])
        with open(data / "queue/events.jsonl", "a") as handle:
            handle.write(json.dumps({"time": "2026-11-16T11:00:00Z", "kind": "stalled", "agent": "a1", "task": "t2"}) + "\n")
        self.assertEqual(1, store.import_events(self.conn, "first", data / "queue/events.jsonl"))
        self.assertEqual(4, len(store.events(self.conn, "first")))
        self.assertEqual(str(data), store.list_projects(self.conn)[0]["data"])

class MigrationTests(StoreCase):
    def project(self):
        data = self.tmp / "data"
        (data / "queue").mkdir(parents=True)
        (data / "queue/events.jsonl").write_text("".join(json.dumps(
            {"time": f"2026-11-16T10:0{n}:00Z", "kind": "delivered", "agent": "a1", "task": f"t{n}"}) + "\n" for n in range(3)))
        (data / "agents.json").write_text(json.dumps({"agents": [{"name": "a1", "pane": "%1", "provider": "codex"}]}))
        return SimpleNamespace(name="first", root=self.tmp, data=data)

    def reopen(self, conf=None):
        store.READY.clear()
        with (store.for_project(conf) if conf else store.for_host()) as conn:
            return store.events(conn, "first"), store.list_agents(conn, "first")

    def test_a_project_is_imported_once_and_later_lines_never_repeat(self):
        conf = self.project()
        events, agents = self.reopen(conf)
        self.assertEqual((3, ["a1"]), (len(events), [a["name"] for a in agents]))
        with store.for_project(conf) as conn:
            store.add_event(conn, "first", "assigned", task="t9")
            store.remove_agent(conn, "first", "a1")
            self.assertIsNotNone(store.get_meta(conn, "agents:first"))
        with open(conf.data / "queue/events.jsonl", "a") as handle:
            handle.write(json.dumps({"time": "2026-11-16T11:00:00Z", "kind": "assigned", "task": "t9"}) + "\n")
        for _ in range(2):
            events, agents = self.reopen(conf)
            self.assertEqual((4, []), (len(events), agents))
        self.assertEqual(3, len(store.deliveries(self.conn, "first")))
        self.assertTrue((conf.data / "agents.json").exists())

    def test_host_files_are_imported_once(self):
        now = time.time()
        (self.tmp / "pool-state").mkdir()
        (self.tmp / "pool-state/gw.json").write_text(json.dumps(
            {"leases": {"second/b1": {"project": "second", "agent": "b1", "task": "t1", "seen": now}}, "claims": {}}))
        live, other = self.tmp / "live", self.tmp / "other"
        (self.tmp / "projects.json").write_text(json.dumps({"projects": {
            str(live): {"name": "first", "data": "d1", "coordinator": "", "last_seen": now},
            str(other): {"name": "first", "data": "d2", "coordinator": "", "last_seen": now}}}))
        self.reopen()
        self.assertEqual(["second/b1"], list(store.status(self.conn, "gw")["leases"]))
        self.assertEqual(str(live), store.get_project(self.conn, "first")["root"])
        self.assertEqual([("first", str(live), str(other))],
                         [(c["name"], c["root"], c["other"]) for c in store.conflicts(self.conn)])
        store.release(self.conn, "gw", "second", "b1")
        self.reopen()
        self.assertEqual({}, store.status(self.conn, "gw")["leases"], "a released slot is not imported again")


class MaintenanceTests(StoreCase):
    def test_trim_keeps_fourteen_days_of_samples_and_a_year_of_events_unless_store_toml_says_otherwise(self):
        now = time.time()
        for days in (1, 13, 15, 40, 400):
            store.add_sample(self.conn, "first", "a1", "working", at=now - days * store.DAY)
            store.add_event(self.conn, "first", "tick", at=now - days * store.DAY)
        removed = store.trim(self.conn, now)
        self.assertEqual((3, 1), (removed["samples"], removed["events"]))
        (self.tmp / "store.toml").write_text("[retention]\nsamples_days = 2\nevents_days = 30\n")
        for days in (1, 13, 40):
            store.add_event(self.conn, "first", "tick", at=now - days * store.DAY)
        store.add_sample(self.conn, "first", "a1", "working", at=now - 3 * store.DAY)
        removed = store.trim(self.conn, now)
        self.assertEqual((2, 2), (removed["samples"], removed["events"]))
        (self.tmp / "store.toml").write_text("[retention\n")
        with self.assertRaises(store.StoreError):
            store.settings()

    def test_every_growing_table_is_trimmed_by_age_and_the_latest_run_survives(self):
        now = time.time()
        old, recent = now - 400 * store.DAY, now - store.DAY
        for at in (old, recent):
            store.add_delivery(self.conn, "first", "t1", agent="a1", result="delivered", at=at)
            store.add_gateway_error(self.conn, "gw", "first", 524, True, agent="a1", at=at)
            session = store.start_session(self.conn, "first", started=at)
            store.end_session(self.conn, session, ended=at)
        for gen, at in enumerate((old, old, recent), 1):
            run = store.add_run(self.conn, "first", "a1", "start", at=at)
            store.close_run(self.conn, run["id"], "crash", at=at)
        lone = store.add_run(self.conn, "first", "a2", "start", at=old)
        store.close_run(self.conn, lone["id"], "down", at=old)
        store.add_run(self.conn, "first", "a3", "start", at=old)
        store.put_gateway_retry(self.conn, "first", "a1", now=old, task="t1", pool="gw", phase="exhausted")
        store.put_gateway_retry(self.conn, "first", "a2", now=old, task="t2", pool="gw", phase="waiting")
        store.put_gateway_retry(self.conn, "first", "a3", now=recent, task="t3", pool="gw", phase="recovered")
        removed = store.trim(self.conn, now)
        self.assertEqual({"deliveries": 1, "gateway_errors": 1, "coordinator_sessions": 1, "agent_runs": 2,
                          "gateway_retries": 1},
                         {key: removed[key] for key in ("deliveries", "gateway_errors", "coordinator_sessions",
                                                        "agent_runs", "gateway_retries")})
        self.assertEqual([3], [r["gen"] for r in store.runs(self.conn, "first", "a1")])
        self.assertEqual(4, store.add_run(self.conn, "first", "a1", "start")["gen"], "gen numbering continues")
        self.assertEqual(1, len(store.runs(self.conn, "first", "a2")), "an agent's latest run is kept")
        self.assertEqual(1, len(store.runs(self.conn, "first", "a3")), "an open run is kept")
        self.assertEqual({"a2", "a3"}, {r["agent"] for r in store.gateway_retries(self.conn)})

    def test_prune_removes_every_row_of_a_gone_project(self):
        store.upsert_project(self.conn, "gone", root=self.tmp / "missing")
        store.add_gateway_error(self.conn, "gw", "gone", 524, True, agent="a1")
        store.put_gateway_retry(self.conn, "gone", "a1", task="t1", pool="gw", phase="waiting")
        store.add_run(self.conn, "gone", "a1", "start")
        store.add_delivery(self.conn, "gone", "t1")
        store.add_sample(self.conn, "gone", "a1", "free")
        store.put_lease(self.conn, "gw", "gone", "a1", "t1", time.time())
        self.assertEqual(["gone"], store.prune_projects(self.conn))
        tables = [row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        left = {table: self.conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE project = ?', ("gone",)).fetchone()[0]
                for table in tables if "project" in [c[1] for c in self.conn.execute(f'PRAGMA table_info("{table}")')]}
        self.assertEqual({}, {table: count for table, count in left.items() if count})

    def test_maintain_returns_free_pages_converts_old_files_and_records_the_run(self):
        self.conn.execute("PRAGMA auto_vacuum = NONE")
        self.conn.execute("VACUUM")
        self.assertEqual(0, self.conn.execute("PRAGMA auto_vacuum").fetchone()[0])
        with store.transaction(self.conn):
            store.touch_project(self.conn, "first")
            self.conn.executemany("INSERT INTO samples (project, agent, time, epoch, state) VALUES ('first', 'a', '', ?, 'x')",
                                  [(n,) for n in range(20000)])
        result = store.maintain(self.conn)
        self.assertTrue(result["vacuumed"])
        self.assertEqual(20000, result["removed"]["samples"])
        self.assertEqual(2, self.conn.execute("PRAGMA auto_vacuum").fetchone()[0])
        with store.transaction(self.conn):
            self.conn.executemany("INSERT INTO samples (project, agent, time, epoch, state) VALUES ('first', 'a', '', ?, 'x')",
                                  [(n,) for n in range(20000)])
        result = store.maintain(self.conn)
        self.assertFalse(result["vacuumed"])
        self.assertGreater(result["freed_pages"], 0)
        self.assertEqual(0, self.conn.execute("PRAGMA freelist_count").fetchone()[0])
        self.assertIsNotNone(store.get_meta(self.conn, "maintain.last"))
        self.assertEqual(0, Path(f"{self.db}-wal").stat().st_size)

    def test_daily_runs_each_job_once_a_day_and_records_failures(self):
        now = time.time()
        lines = store.daily(self.conn, now=now)
        self.assertEqual(["maintain", "backup"], [line.split(":")[0] for line in lines])
        self.assertEqual(1, len(list((self.tmp / "backups").glob("theseus-*.db"))))
        self.assertEqual([], store.daily(self.conn, now=now + 3600))
        (self.tmp / "blocked").write_text("not a directory")
        os.environ["THESEUS_BACKUPS"] = str(self.tmp / "blocked")
        lines = store.daily(self.conn, now=now + store.DAY + 1)
        self.assertIn("backup failed", lines[-1])
        self.assertIn("backup", store.check(self.conn, quick=True)["errors"])
        os.environ["THESEUS_BACKUPS"] = str(self.tmp / "backups")
        store.daily(self.conn, now=now + 2 * store.DAY + 2)
        self.assertEqual({}, store.check(self.conn, quick=True)["errors"])


class BackupTests(StoreCase):
    def test_backup_is_a_checked_single_file_copy(self):
        store.add_event(self.conn, "first", "tick")
        result = store.backup(self.conn, now=1_794_000_000)
        copy = Path(result["path"])
        self.assertEqual(self.tmp / f"backups/theseus-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(1_794_000_000))}.db", copy)
        self.assertEqual("ok", result["integrity"])
        self.assertFalse(Path(f"{copy}-wal").exists())
        self.assertEqual([copy], list((self.tmp / "backups").iterdir()))
        with store.opened(copy) as conn:
            self.assertEqual(1, len(store.events(conn)))
        again = store.backup(self.conn, now=1_794_000_000)
        self.assertNotEqual(result["path"], again["path"])
        self.assertEqual(again["path"], store.last_backup()["path"])

    def test_rotation_keeps_seven_daily_and_four_weekly_copies(self):
        start = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc).timestamp()
        moments = [datetime.fromtimestamp(start + n * 43200, timezone.utc) for n in range(80)]
        names = [f"theseus-{m:%Y%m%dT%H%M%SZ}.db" for m in moments]
        extra = ["theseus-20260901T000000Z-pre-restore.db", "notes.txt"]
        keep = store.rotation(names + extra)
        self.assertTrue(set(extra) <= keep)
        kept = [m for m, name in zip(moments, names) if name in keep]
        days = sorted({m.date() for m in moments})[-7:]
        weeks = sorted({m.isocalendar()[:2] for m in moments})[-4:]
        newest = lambda group: max(m for m in moments if group(m))
        expected = {newest(lambda m, d=d: m.date() == d) for d in days}
        expected |= {newest(lambda m, w=w: m.isocalendar()[:2] == w) for w in weeks}
        self.assertEqual(sorted(expected), kept)
        self.assertEqual(moments[-1], kept[-1])
        directory = self.tmp / "backups"
        directory.mkdir()
        for name in names + extra[:1]:
            (directory / name).write_text("")
        removed = store.rotate(directory)
        self.assertEqual(80 - len(kept), len(removed))
        self.assertEqual(len(kept) + 1, len(list(directory.iterdir())))

    def test_restore_checks_the_file_saves_the_current_database_and_loads_the_copy(self):
        store.add_event(self.conn, "first", "kept")
        saved = store.backup(self.conn)["path"]
        store.add_event(self.conn, "first", "lost")
        garbage = self.tmp / "garbage.db"
        garbage.write_bytes(b"not a database" * 100)
        for bad in (self.tmp / "missing.db", garbage):
            with self.assertRaises(store.StoreError):
                store.restore(self.conn, bad)
        newer = self.tmp / "newer.db"
        with store.opened(newer) as conn:
            conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 1}")
        with self.assertRaises(store.StoreError):
            store.restore(self.conn, newer)
        self.assertEqual(["kept", "lost"], [e["kind"] for e in store.events(self.conn)])
        result = store.restore(self.conn, saved)
        self.assertEqual(["kept"], [e["kind"] for e in store.events(self.conn)])
        self.assertIn("pre-restore", result["saved"])
        with store.opened(result["saved"]) as conn:
            self.assertEqual(["kept", "lost"], [e["kind"] for e in store.events(conn)])
        with store.opened() as other:
            self.assertEqual(["kept"], [e["kind"] for e in store.events(other)])


class ConnectionTests(StoreCase):
    def test_a_failed_transaction_rolls_back(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with store.transaction(self.conn):
                self.conn.execute("INSERT INTO projects (name) VALUES ('x')")
                self.conn.execute("INSERT INTO projects (name) VALUES ('x')")
        self.assertEqual([], store.list_projects(self.conn))
        self.assertFalse(self.conn.in_transaction)


if __name__ == "__main__":
    unittest.main()
