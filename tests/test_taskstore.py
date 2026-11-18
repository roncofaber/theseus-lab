"""Task store (schema v7, taskrepo, taskservice): the lossless round trip of the legacy files, the v6 to v7 migration,
claim races decided by the database with no transaction held across worktree creation, and who may call what."""
import json
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import helpers  # noqa: F401  (isolates the host store for the whole run)
from theseus import store, taskrecords, taskservice as ts

FIXTURE = Path(__file__).parent / "fixtures" / "taskstore"
COORDINATOR = ts.COORDINATOR
QUEUE = ts.QUEUE


def spec(task_id, **extra):
    return {"id": task_id, "role": "implementation", "owned": [f"docs/investigations/{task_id}.md", f"src/{task_id}/"],
            "objective": f"Build {task_id}.", "contract": "All inputs.", "accept": ["It works."], **extra}


def claim_in_child(db, conf, task_id, agent_id, rev, barrier, results):
    os.environ["THESEUS_DB"] = db
    barrier.wait()
    try:
        ts.open(conf).claim(QUEUE, task_id, agent_id, owner=f"p:{agent_id}:{agent_id}-{task_id}", provider="p",
                            expected_rev=rev)
        results.put((task_id, agent_id, "won"))
    except ts.TaskError as error:
        results.put((task_id, agent_id, error.code))


class TaskStoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="theseus-taskstore-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        saved = os.environ["THESEUS_DB"]
        self.addCleanup(os.environ.__setitem__, "THESEUS_DB", saved)
        self.use_db(self.tmp / "host" / "theseus.db")
        self.conf = SimpleNamespace(name="demo", root=self.tmp / "project")
        self.svc = ts.open(self.conf)

    def use_db(self, db):
        os.environ["THESEUS_DB"] = str(db)
        self.db = db

    def start(self, snapshot=None):
        self.svc.import_(COORDINATOR, snapshot or taskrecords.Snapshot(tasks='{"schema": 2, "tasks": []}\n'))

    def code(self, call, *args, **kwargs):
        with self.assertRaises(ts.TaskError) as caught:
            call(*args, **kwargs)
        return caught.exception.code


class RoundTripTests(TaskStoreCase):
    def test_legacy_files_round_trip_byte_for_byte_through_every_service_call(self):
        legacy = taskrecords.Snapshot(tasks=(FIXTURE / "tasks.json").read_text(),
                                      archive=(FIXTURE / "tasks-archive.jsonl").read_text())
        report = self.svc.import_(COORDINATOR, legacy)
        first = self.svc.export(COORDINATOR)
        self.assertEqual(legacy.archive, first.archive)
        header = json.loads(first.tasks)
        self.assertEqual((2, "demo", report["id"]), (header["schema"], header["project"]["name"], header["project"]["id"]))
        expected = legacy.tasks.replace('"schema": 1,', '"schema": 2,\n  "project": {\n    "id": "%s",\n    "name": "demo"\n  },'
                                        % report["id"], 1)
        self.assertEqual(expected, first.tasks)

        self.drive_every_call()
        second = self.svc.export(COORDINATOR)
        self.assertTrue(second.archive.startswith(legacy.archive), "exported archive lines never change")
        self.assertGreater(len(second.history.splitlines()), 20)
        edited = next(t for t in json.loads(second.tasks)["tasks"] if t["id"] == "task-11")
        before = next(t for t in json.loads(legacy.tasks)["tasks"] if t["id"] == "task-11")
        self.assertEqual(list(before), list(edited), "a write keeps the imported key order")
        self.assertEqual(9, edited["priority"])

        self.use_db(self.tmp / "other" / "theseus.db")
        fresh = ts.open(self.conf)
        fresh.import_(COORDINATOR, second)
        self.assertEqual(second, fresh.export(COORDINATOR))

    def drive_every_call(self):
        svc, worker = self.svc, ts.Principal("worker", "agent-1")
        svc.add(COORDINATOR, spec("alpha"))
        svc.add(COORDINATOR, spec("beta", depends=["alpha"]))
        svc.add(COORDINATOR, spec("gamma", priority=2))
        svc.set_milestone(COORDINATOR, "M2")
        svc.edit(COORDINATOR, "task-11", {"priority": 9})
        svc.edit(COORDINATOR, "gamma", {"objective": "Build gamma well.", "milestone": "M2"})
        svc.hold(COORDINATOR, "gamma", True)
        svc.hold(COORDINATOR, "gamma", False)
        svc.edit(COORDINATOR, "alpha", {"milestone": "M2"})
        prepared = lambda claim: (self.tmp / "wt" / claim["task"], "base1", self.tmp / "wt" / claim["task"] / ".theseus")
        svc.assign(QUEUE, "alpha", "agent-1", prepared, owner="p:w1:w1-alpha", provider="p")
        svc.note(worker, "alpha", "Which format?", question=True)
        self.assertEqual(1, len(svc.questions(COORDINATOR)))
        svc.note(COORDINATOR, "alpha", "Use the second format.")
        self.assertEqual([], svc.questions(COORDINATOR))
        svc.deliver(worker, "alpha", handoff_path="/wt/alpha/.theseus/handoff.md", result="delivered",
                    handoff_text="Result: delivered", observed_touches=["src/shared.py"])
        svc.reopen(COORDINATOR, "alpha", "missing test", handoff="/wt/alpha/.theseus/handoff.attempt1.md")
        svc.deliver(QUEUE, "alpha", handoff_path="/wt/alpha/.theseus/handoff.md", result="partial")
        svc.mark_merged(COORDINATOR, "alpha", "abc1234")
        svc.observe(COORDINATOR, "alpha", "ran the scenario")
        svc.complete(COORDINATOR, "alpha", "Built alpha.", checks=["unit tests"], log="/logs/alpha.log")
        svc.edit(COORDINATOR, "beta", {"milestone": "M2"})
        svc.assign(QUEUE, "beta", "agent-1", prepared, owner="p:w1:w1-beta", provider="p")
        svc.deliver(worker, "beta", handoff_path="/wt/beta/.theseus/handoff.md")
        svc.mark_merged(COORDINATOR, "beta", "def5678")
        svc.unmerge(COORDINATOR, "beta", "broke the build", "fedcba9")
        svc.release(COORDINATOR, "beta", "redo later")

        def broken(claim):
            raise OSError("no space left for the worktree")
        with self.assertRaises(OSError):
            svc.assign(QUEUE, "gamma", "agent-2", broken, owner="p:w2:w2-gamma", provider="p")
        svc.assign(QUEUE, "gamma", "agent-2", prepared, owner="p:w2:w2-gamma", provider="p")
        svc.rebase(COORDINATOR, "gamma", "base2")
        svc.blocked(ts.Principal("worker", "agent-2"), "gamma", "needs a capture")
        svc.rebase(COORDINATOR, "task-9", "base3")
        svc.release(COORDINATOR, "task-10", "reassigning")
        records = {t["id"]: t for t in svc.list(COORDINATOR, include_completed=True)}
        self.assertEqual(("completed", "pending", "verification"),
                         (records["alpha"]["status"], records["beta"]["status"], records["gamma"]["status"]))
        self.assertEqual(["reopenedAt"], [k for k in records["alpha"]["previousAttempts"][0] if k.endswith("At")])
        self.assertEqual(["unmergedAt", "releasedAt"],
                         [next(k for k in a if k.endswith("At")) for a in records["beta"]["previousAttempts"]])
        self.assertEqual("assignment failed: no space left for the worktree",
                         records["gamma"]["previousAttempts"][0]["reason"])


class MigrationTests(TaskStoreCase):
    def test_version_6_gains_project_ids_and_task_tables_with_nothing_else_changed(self):
        saved = list(store.MIGRATIONS)
        self.addCleanup(lambda: (setattr(store, "MIGRATIONS", saved), setattr(store, "SCHEMA_VERSION", len(saved))))
        store.MIGRATIONS, store.SCHEMA_VERSION = saved[:6], 6
        with store.opened() as old:
            old.execute("INSERT INTO projects (name, root, data) VALUES ('kept', ?, ?), ('gone', ?, NULL)",
                        (str(self.tmp), str(self.tmp / "data"), str(self.tmp / "missing")))
            old.execute("INSERT INTO agents (project, name, pane, id) VALUES ('kept', 'w-1', '%1', 'A1')")
            old.execute("INSERT INTO events (project, time, epoch, kind, agent, task, agent_id) "
                        "VALUES ('kept', 't', 1, 'assigned', 'w-1', 't1', 'A1')")
            old.execute("INSERT INTO deliveries (project, task, agent, result, time, epoch) "
                        "VALUES ('kept', 't1', 'w-1', 'delivered', 't', 1)")
            before = self.dump(old)
        store.MIGRATIONS, store.SCHEMA_VERSION = saved, len(saved)
        with store.opened() as conn:
            self.assertEqual(7, store.version(conn))
            projects = {p["name"]: p for p in store.list_projects(conn)}
            self.assertEqual(2, len({p["id"] for p in projects.values()} - {None}))
            after = self.dump(conn)
            self.assertNotIn("task_index", after)
            new_columns = {"projects": {name for name, _ in store.PROJECT_TASK_COLUMNS}, "deliveries": {"source"}}
            for table, rows in before.items():
                if table == "task_index":
                    continue
                trimmed = [{k: v for k, v in row.items() if k not in new_columns.get(table, ())} for row in after[table]]
                self.assertEqual(rows, trimmed, table)
            store.touch_project(conn, "later")
            self.assertIsNotNone(store.get_project(conn, "later")["id"])
        self.assertEqual(1, len(list(store.backup_dir().glob("theseus-*-pre-v7.db"))))

        self.conf = SimpleNamespace(name="gone", root=self.tmp / "missing")
        ts.open(self.conf).import_(COORDINATOR, taskrecords.Snapshot(tasks=json.dumps({"tasks": [
            {"id": "t1", "status": "pending"}]})))
        with store.opened() as conn:
            self.assertEqual([], store.prune_projects(conn), "a project with tasks outlives its missing root")

    @staticmethod
    def dump(conn):
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {table: [dict(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')] for table in tables}


class ConcurrencyTests(TaskStoreCase):
    def race(self, entrants):
        context = multiprocessing.get_context("spawn")
        barrier, results = context.Barrier(len(entrants)), context.Queue()
        racers = [context.Process(target=claim_in_child, args=(str(self.db), self.conf, task_id, agent_id, rev, barrier,
                                                               results)) for task_id, agent_id, rev in entrants]
        for racer in racers:
            racer.start()
        for racer in racers:
            racer.join(60)
        return sorted(results.get(timeout=5)[2] for _ in racers)

    def test_claim_races_are_decided_by_the_database(self):
        self.start()
        for name in ("one", "two", "three"):
            self.svc.add(COORDINATOR, spec(name))
        rev = self.svc.get(COORDINATOR, "one")["rev"]
        self.assertEqual(["conflict", "won"], self.race([("one", "agent-1", rev), ("one", "agent-2", rev)]))
        revs = [self.svc.get(COORDINATOR, name)["rev"] for name in ("two", "three")]
        self.assertEqual(["conflict", "won"], self.race([("two", "agent-3", revs[0]), ("three", "agent-3", revs[1])]))

    def test_no_transaction_stays_open_while_the_worktree_is_made(self):
        self.start()
        self.svc.add(COORDINATOR, spec("one"))
        seen = {}

        def prepare(claim):
            other = sqlite3.connect(self.db, timeout=0.2, isolation_level=None)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("COMMIT")
            finally:
                other.close()
            seen["status"] = self.svc.get(COORDINATOR, "one")["task"]["status"]
            return self.tmp / "wt", "base", self.tmp / "wt" / ".theseus"

        self.svc.assign(QUEUE, "one", "agent-1", prepare, owner="p:w1:w1-one", provider="p")
        self.assertEqual("in_progress", seen["status"], "the claim is committed before the worktree is made")
        self.assertEqual(str(self.tmp / "wt"), self.svc.get(COORDINATOR, "one")["task"]["worktree"])


class AuthorizationTests(TaskStoreCase):
    def setUp(self):
        super().setUp()
        self.start()
        for name in ("mine", "theirs"):
            self.svc.add(COORDINATOR, spec(name))
        self.worktree = self.tmp / "wt" / "mine"
        self.worktree.mkdir(parents=True)
        self.svc.assign(QUEUE, "mine", "agent-a", lambda c: (self.worktree, "b", self.worktree / ".theseus"),
                        owner="p:a:a-mine", provider="p")
        self.svc.assign(QUEUE, "theirs", "agent-b", lambda c: (self.tmp / "wt" / "theirs", "b", self.tmp / "log"),
                        owner="p:b:b-theirs", provider="p")

    def credentials(self, agent_id, run_id, **extra):
        secret = ts.issue_credentials(COORDINATOR, agent_id, run_id)
        path = self.tmp / f"{agent_id}-{run_id}.json"
        path.write_text(json.dumps({"agent_id": agent_id, "run_secret": secret, **extra}))
        return path

    def test_the_role_comes_from_credentials_and_anything_unproven_is_refused(self):
        path = self.credentials("agent-a", "run-1", role="coordinator")
        worker = ts.principal({"THESEUS_CREDENTIALS": str(path)}, self.tmp)
        self.assertEqual(("worker", "agent-a"), (worker.role, worker.agent_id))
        self.assertEqual("unauthenticated", self.code(ts.principal, {"THESEUS_AGENT_ID": "agent-a"}, self.tmp))
        self.assertEqual("unauthenticated", self.code(ts.principal, {}, self.worktree / "src"))
        self.assertEqual("coordinator", ts.principal({}, self.tmp).role)
        ts.revoke_credentials(COORDINATOR, "agent-a", "run-1")
        self.assertEqual("unauthenticated", self.code(ts.principal, {"THESEUS_CREDENTIALS": str(path)}, self.tmp))
        forged = self.tmp / "forged.json"
        forged.write_text(json.dumps({"agent_id": "agent-a", "run_secret": "guess"}))
        self.assertEqual("unauthenticated", self.code(ts.principal, {"THESEUS_CREDENTIALS": str(forged)}, self.tmp))

    def test_a_worker_acts_only_on_its_own_active_task(self):
        worker = ts.principal({"THESEUS_CREDENTIALS": str(self.credentials("agent-a", "run-2"))}, self.tmp)
        self.assertEqual("mine", self.svc.show(worker, "mine")["task"]["id"])
        self.assertEqual("theirs", self.svc.get(worker, "theirs")["task"]["id"], "reads need no ownership")
        self.assertEqual("not-your-task", self.code(self.svc.show, worker, "theirs"))
        self.assertEqual("not-your-task", self.code(self.svc.deliver, worker, "theirs", handoff_path="/x"))
        for call, args in ((self.svc.add, (spec("more"),)), (self.svc.release, ("mine", "done")),
                           (self.svc.complete, ("mine", "s")), (self.svc.claim, ("theirs", "agent-a"))):
            kwargs = {"owner": "o", "provider": "p"} if call == self.svc.claim else {}
            self.assertEqual("forbidden", self.code(call, worker, *args, **kwargs))
        self.svc.note(worker, "mine", "Halfway there.")
        self.svc.release(COORDINATOR, "mine", "reassigning")
        self.assertEqual("not-your-task", self.code(self.svc.note, worker, "mine", "Still here?"))
        self.assertEqual("not-your-task", self.code(self.svc.deliver, worker, "mine", handoff_path="/x"))
        self.assertEqual("forbidden", self.code(self.svc.note, QUEUE, "theirs", "hello"))


if __name__ == "__main__":
    unittest.main()
