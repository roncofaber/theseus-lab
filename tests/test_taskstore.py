"""Task store (schema v7, taskrepo, taskservice): the lossless round trip of the legacy files, the v6 to v7 migration,
claim races decided by the database with no transaction held across worktree creation, and who may call what."""
import json
import multiprocessing
import os
import sqlite3
import tempfile
import time
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
        with store.opened() as conn:
            restored = conn.execute("""SELECT a.result, a.source FROM task_attempts a JOIN tasks t ON t.pk = a.task_pk
                                       WHERE t.id = 'alpha' AND a.result IS NOT NULL ORDER BY a.n""").fetchall()
        self.assertEqual([("delivered", "command"), ("partial", "detected")], [tuple(row) for row in restored],
                         "each delivery's result and source come back from the history")

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
        self.assertNotIn("previousAttempts", records["gamma"], "a claim that never became active leaves no attempt")
        self.assertIn("unclaimed", [e["kind"] for e in svc.get(COORDINATOR, "gamma", history=True)["history"]])


class SoftOwnershipTests(TaskStoreCase):
    def test_touches_and_overlapping_ownership_hold_a_task_while_the_other_runs(self):
        self.start()
        svc = self.svc
        svc.add(COORDINATOR, spec("first"))
        run = lambda task_id, agent: svc.assign(QUEUE, task_id, agent, lambda c: (self.tmp / task_id, "b",
                                                                                   self.tmp / task_id / ".theseus"),
                                                owner=f"p:{agent}:{agent}-x", provider="p")
        run("first", "agent-1")
        added = svc.add(COORDINATOR, spec("patcher", owned=["docs/investigations/patcher.md"],
                                          touches=["src/first/shared.py"]))
        self.assertIn("touches:src/first/shared.py@first", added["warnings"][0])
        svc.add(COORDINATOR, spec("inner", owned=["docs/investigations/inner.md", "src/first/inner.py"]))
        svc.add(COORDINATOR, spec("third", owned=["docs/investigations/third.md", "src/third.py"]))
        self.assertEqual({"patcher": ["touches:src/first/shared.py@first"], "inner": ["owns:src/first/@first"]},
                         svc.holds(COORDINATOR))
        self.assertEqual(["third"], svc.ready(COORDINATOR))
        refused = self.code(svc.claim, QUEUE, "patcher", "agent-2", owner="o", provider="p")
        self.assertEqual("refused", refused)

        self.assertTrue(svc.set_observed(QUEUE, "first", ["src/third.py"]))
        self.assertFalse(svc.set_observed(QUEUE, "first", ["src/third.py"]), "an unchanged scan writes nothing")
        self.assertEqual(["touches:src/third.py@first"], svc.holds(COORDINATOR)["third"],
                         "the tick's patch scan holds a conflicting task before the delivery")
        svc.deliver(QUEUE, "first", handoff_path="/h", observed_touches=["src/third.py"])
        self.assertIn("third", svc.holds(COORDINATOR), "held while the first task is in verification")
        svc.mark_merged(COORDINATOR, "first", "abc")
        self.assertEqual({}, svc.holds(COORDINATOR))
        self.assertEqual(["patcher", "inner", "third"], svc.ready(COORDINATOR))


class ImportTests(TaskStoreCase):
    def test_task_files_wait_for_an_import_and_identity_survives_replace_and_prune(self):
        files = self.conf.root / "docs/project"
        files.mkdir(parents=True)
        (files / "tasks.json").write_text(json.dumps({"schema": 1, "tasks": [{"id": "old", "status": "pending"}]}))
        self.conf.data = self.tmp / "data"
        self.assertEqual("not-imported", self.code(self.svc.list, COORDINATOR))
        fresh = ts.open(SimpleNamespace(name="new-project", root=self.tmp / "empty", data=self.tmp / "data"))
        self.assertEqual([], fresh.list(COORDINATOR), "a project with no tasks imports itself on first use")

        first = self.svc.import_(COORDINATOR, taskrecords.read(self.conf.root))
        moved = ts.open(SimpleNamespace(name="demo", root=self.tmp / "moved", data=self.tmp / "data"))
        self.assertEqual("wrong-root", self.code(moved.list, COORDINATOR), "another checkout claiming the name")
        moved.adopt(COORDINATOR)
        self.assertEqual(["old"], [t["id"] for t in moved.list(COORDINATOR)])
        self.assertEqual("wrong-root", self.code(self.svc.list, COORDINATOR))
        self.svc.adopt(COORDINATOR)
        self.assertEqual("refused", self.code(self.svc.import_, COORDINATOR, taskrecords.read(self.conf.root)))
        self.assertEqual("refused", self.code(self.svc.import_, COORDINATOR, taskrecords.read(self.conf.root),
                                              new_id=True))
        replaced = self.svc.import_(COORDINATOR, taskrecords.read(self.conf.root), replace=True)
        self.assertEqual(first["id"], replaced["id"])
        self.assertIn('"old"', (Path(replaced["saved"]) / "tasks.json").read_text())

        saved = self.svc.drop_tasks(COORDINATOR)
        self.assertIn('"old"', (Path(saved) / "tasks.json").read_text())
        self.assertNotEqual(first["id"], self.svc.import_(COORDINATOR, taskrecords.read(self.conf.root), new_id=True)["id"])


class WriteRuleTests(TaskStoreCase):
    def test_forced_claims_record_what_they_bypass_and_abandoning_one_restores_it(self):
        self.start(taskrecords.Snapshot(tasks=json.dumps({"schema": 2, "currentMilestone": "M1", "tasks": [
            {"id": "paused", "status": "pending", "pause": {"state": "kept"}}]})))
        self.svc.add(COORDINATOR, spec("later", milestone="M2"))
        self.svc.add(COORDINATOR, spec("busy"))
        claim = lambda task_id, agent, **kw: self.svc.claim(QUEUE, task_id, agent, owner=f"p:{agent}:{agent}-x",
                                                            provider="p", **kw)
        self.assertIn("milestone:M2", str(self.assert_refused(claim, "later", "agent-1")))
        self.assertIn("capacity", str(self.assert_refused(claim, "busy", "agent-1", capacity=0)))
        forced = claim("later", "agent-1", force=True)
        events = {e["kind"]: e["detail"] for e in self.svc.get(COORDINATOR, "later", history=True)["history"]}
        self.assertEqual(["milestone:M2"], events["forced"]["bypassed"])
        self.svc.abandon(QUEUE, forced["attempt"], "test")

        self.svc.assign(QUEUE, "busy", "agent-1", lambda c: (self.tmp / "wt1", "b", self.tmp / "log1"), owner="p:a:a-1",
                        provider="p")
        taken = claim("busy", "agent-2", force=True)
        self.assertEqual("p:agent-2:agent-2-x", self.svc.get(COORDINATOR, "busy")["task"]["owner"])
        self.svc.abandon(QUEUE, taken["attempt"], "worktree failed")
        record = self.svc.get(COORDINATOR, "busy")["task"]
        self.assertEqual(("in_progress", "p:a:a-1", str(self.tmp / "wt1")), (record["status"], record["owner"],
                                                                            record["worktree"]))
        self.assertNotIn("previousAttempts", record)

        resumed = claim("paused", "agent-2", force=True)
        self.assertNotIn("pause", self.svc.get(COORDINATOR, "paused")["task"])
        self.svc.abandon(QUEUE, resumed["attempt"], "send failed")
        record = self.svc.get(COORDINATOR, "paused")["task"]
        self.assertEqual(("pending", {"state": "kept"}), (record["status"], record["pause"]))
        self.assertNotIn("previousAttempts", record)

        self.svc.complete(COORDINATOR, "later", "Done by hand.", "abc", ["looked"], "/log", force=True)
        forced = [e for e in self.svc.get(COORDINATOR, "later", history=True)["history"] if e["kind"] == "forced"]
        self.assertIn("never attempted", forced[-1]["detail"]["bypassed"])

    def test_blockers_are_validated_on_edit(self):
        self.start()
        self.svc.add(COORDINATOR, spec("one"))
        half = {"evidence": "the capture is missing"}
        self.assertEqual("invalid", self.code(self.svc.edit, COORDINATOR, "one", {"researchRequired": half}))
        both = {**half, "unblockCondition": "a capture exists"}
        self.assertEqual("invalid", self.code(self.svc.edit, COORDINATOR, "one",
                                              {"researchRequired": both, "externalBlocker": both}))
        self.svc.edit(COORDINATOR, "one", {"externalBlocker": both})
        self.assertEqual(both, self.svc.get(COORDINATOR, "one")["task"]["externalBlocker"])
        self.assertEqual([], self.svc.ready(COORDINATOR))

    def test_a_read_writes_nothing_and_a_read_only_store_says_why_it_cannot_be_read(self):
        fresh = ts.open(SimpleNamespace(name="new-project", root=self.tmp / "empty", data=self.tmp / "data"))
        self.assertEqual([], fresh.list(COORDINATOR))
        self.assertFalse(self.db.exists(), "a read never creates the store")
        self.start()
        self.svc.add(COORDINATOR, spec("one"))
        before = sorted(self.db.parent.iterdir())
        self.addCleanup(os.chmod, self.db.parent, 0o755)
        os.chmod(self.db.parent, 0o555)
        self.assertEqual("unreadable", self.code(self.svc.get, COORDINATOR, "one"))
        self.assertEqual(before, sorted(self.db.parent.iterdir()))

    def assert_refused(self, call, *args, **kwargs):
        with self.assertRaises(ts.TaskError) as caught:
            call(*args, **kwargs)
        self.assertEqual("refused", caught.exception.code)
        return caught.exception


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
            new_columns = {"projects": {name for name, _ in store.PROJECT_TASK_COLUMNS}, "deliveries": {"source"},
                           "agents": {"pane_pid"}}
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
        path = self.credentials("agent-a", "agent-a", role="coordinator")
        worker = ts.principal({"THESEUS_CREDENTIALS": str(path)}, self.tmp)
        self.assertEqual(("worker", "agent-a"), (worker.role, worker.agent_id))
        self.assertEqual("unauthenticated", self.code(ts.principal, {"THESEUS_AGENT_ID": "agent-a"}, self.tmp))
        self.assertEqual("unauthenticated", self.code(ts.principal, {}, self.worktree / "src"))
        self.assertEqual("coordinator", ts.principal({}, self.tmp).role)
        self.svc.add(COORDINATOR, spec("in-place"))
        self.svc.assign(QUEUE, "in-place", "agent-c", lambda c: (self.conf.root, "uncommitted", self.tmp / "log"),
                        owner="p:c:c-in-place", provider="p")
        self.assertEqual("coordinator", ts.principal({}, self.conf.root).role, "a task assigned in place")
        self.svc.mark_merged(COORDINATOR, "mine", "abc1234")
        self.svc.complete(COORDINATOR, "mine", "Done.", checks=["tests"], log="/logs/mine.log")
        self.assertEqual("coordinator", ts.principal({}, self.worktree).role, "a finished task's worktree")
        with store.opened() as conn:
            store.upsert_agent(conn, "demo", "by-hand", pane="%7")
        self.assertEqual("unauthenticated", self.code(ts.principal, {"TMUX_PANE": "%7"}, self.tmp))
        self.assertEqual("coordinator", ts.principal({"TMUX_PANE": "%8"}, self.tmp).role)
        with store.opened() as conn:
            store.update_agent(conn, "demo", "by-hand", pane_pid=999999999)
        self.assertEqual("coordinator", ts.principal({"TMUX_PANE": "%7"}, self.tmp).role,
                         "a pane id tmux reused for another process is a stale registration")
        with store.opened() as conn:
            store.update_agent(conn, "demo", "by-hand", pane_pid=os.getpid())
        self.assertEqual("unauthenticated", self.code(ts.principal, {"TMUX_PANE": "%7"}, self.tmp))
        ts.revoke_credentials(COORDINATOR, "agent-a", "agent-a")
        self.assertEqual("unauthenticated", self.code(ts.principal, {"THESEUS_CREDENTIALS": str(path)}, self.tmp))
        forged = self.tmp / "forged.json"
        forged.write_text(json.dumps({"agent_id": "agent-a", "run_secret": "guess"}))
        self.assertEqual("unauthenticated", self.code(ts.principal, {"THESEUS_CREDENTIALS": str(forged)}, self.tmp))

    def test_a_supervised_run_s_credentials_end_with_the_run(self):
        with store.opened() as conn:
            agent_id = store.upsert_agent(conn, "demo", "w-1", pane="%3")
            run = store.add_run(conn, "demo", "w-1", "start")
        path = self.credentials(agent_id, str(run["id"]))
        self.assertEqual(agent_id, ts.principal({"THESEUS_CREDENTIALS": str(path)}, self.tmp).agent_id)
        with store.opened() as conn:
            store.close_run(conn, run["id"], "exited", at=time.time() - 31 * 86400)
        self.assertEqual("unauthenticated", self.code(ts.principal, {"THESEUS_CREDENTIALS": str(path)}, self.tmp))
        with store.opened() as conn:
            self.assertEqual(1, store.trim(conn)["agent_credentials"], "credentials of a run ended 31 days ago go")

    def test_a_worker_acts_only_on_its_own_active_task(self):
        worker = ts.principal({"THESEUS_CREDENTIALS": str(self.credentials("agent-a", "agent-a"))}, self.tmp)
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
