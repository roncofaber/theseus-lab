"""Agent IDs: every registration gets a ULID, records carry it, a reused name never inherits an earlier agent's history,
the store migration backfills IDs, and task records gain ownerId and agentId lazily or through migrate-ids."""
import json
import re

from helpers import Sandbox
from test_store import StoreCase
from theseus import store


class StoreIdTests(StoreCase):
    def register(self, name="w-1"):
        return store.upsert_agent(self.conn, "demo", name, pane="%1", profile="p")

    def test_ids_are_ulids_that_sort_by_time(self):
        early, late = store.new_id(1_700_000_000), store.new_id(1_700_000_001)
        self.assertRegex(early, r"^[0-9A-HJKMNP-TV-Z]{26}$")
        self.assertLess(early, late)

    def test_updating_a_registration_keeps_its_id(self):
        first = self.register()
        self.assertTrue(first)
        self.assertEqual(first, store.upsert_agent(self.conn, "demo", "w-1", pane="%2", profile="q"))

    def test_a_reused_name_gets_a_new_id_and_the_old_history_keeps_the_old_one(self):
        old = self.register()
        store.add_event(self.conn, "demo", "assigned", agent="w-1")
        store.add_run(self.conn, "demo", "w-1", "start")
        store.remove_agent(self.conn, "demo", "w-1")
        new = self.register()
        store.add_event(self.conn, "demo", "assigned", agent="w-1")
        store.add_run(self.conn, "demo", "w-1", "start")
        self.assertNotEqual(old, new)
        self.assertEqual([old, new], [e["agent_id"] for e in store.events(self.conn, "demo")])
        self.assertEqual([(new, 2)], [(r["agent_id"], r["gen"]) for r in store.runs(self.conn, "demo", "w-1")])

    def test_a_reused_name_does_not_inherit_a_gateway_retry_episode(self):
        self.register()
        store.put_gateway_retry(self.conn, "demo", "w-1", phase="exhausted", errors=5)
        store.remove_agent(self.conn, "demo", "w-1")
        self.register()
        self.assertIsNone(store.gateway_retry(self.conn, "demo", "w-1"))

    def test_records_carry_the_id(self):
        agent = self.register()
        store.add_delivery(self.conn, "demo", "t1", agent="w-1")
        store.add_gateway_error(self.conn, "gw", "demo", 429, True, agent="w-1")
        store.add_sample(self.conn, "demo", "w-1", "working")
        store.take(self.conn, "gw", "demo", "w-1", "t1")
        self.assertEqual(agent, store.deliveries(self.conn, "demo")[0]["agent_id"])
        self.assertEqual(agent, store.gateway_errors(self.conn)[0]["agent_id"])
        self.assertEqual(agent, store.samples(self.conn, "demo", agent="w-1")[0]["agent_id"])
        self.assertEqual(agent, store.status(self.conn, "gw", spec={"max": 2})["leases"]["demo/w-1"]["agentId"])


class MigrationTests(StoreCase):
    def test_version_5_gains_one_id_per_agent_and_maps_records_by_name(self):
        self.conn.close()
        self.db.unlink()
        saved = list(store.MIGRATIONS)
        self.addCleanup(lambda: (setattr(store, "MIGRATIONS", saved), setattr(store, "SCHEMA_VERSION", len(saved))))
        store.MIGRATIONS, store.SCHEMA_VERSION = saved[:5], 5
        with store.opened() as old:
            old.execute("INSERT INTO projects (name) VALUES ('demo')")
            old.execute("INSERT INTO agents (project, name) VALUES ('demo', 'w-1'), ('demo', 'w-2')")
            for agent in ("w-1", "w-2", "gone"):
                old.execute("INSERT INTO events (project, time, epoch, kind, agent) VALUES ('demo', '', 0, 'x', ?)", (agent,))
            old.execute("INSERT INTO pool_leases (pool, project, agent, seen, seen_epoch) VALUES ('gw', 'demo', 'w-1', '', 0)")
        store.MIGRATIONS, store.SCHEMA_VERSION = saved, len(saved)
        with store.opened() as conn:
            ids = {r["name"]: r["id"] for r in store.list_agents(conn, "demo")}
            events = {e["agent"]: e["agent_id"] for e in store.events(conn, "demo")}
            lease = conn.execute("SELECT agent_id FROM pool_leases").fetchone()[0]
            store.backfill_agent_ids(conn)
            again = {r["name"]: r["id"] for r in store.list_agents(conn, "demo")}
        self.assertEqual(2, len(set(ids.values())))
        self.assertEqual({"w-1": ids["w-1"], "w-2": ids["w-2"], "gone": None}, events)
        self.assertEqual(ids["w-1"], lease)
        self.assertEqual(ids, again)


class TaskIdTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        from theseus import agents, config as cfg, tasks as t
        self.t, self.agents = t, agents
        self.conf = cfg.load(root=self.project)
        self.queue = t.Queue(self.conf)
        agents.add(self.conf, "w-1", "%1")
        self.theseus("tasks", "add", "t1", "--role", "research", "--owned", "docs/investigations/t1.md",
                     "--objective", "Do it.", "--contract", "All.", "--accept", "Done.")

    def assign(self):
        self.t.assign(self.queue, "t1", "p:w-1:w-1-t1", "p", "persistent-instance", self.tmp / "wt", "abc", self.tmp / "log",
                      force=True)
        return self.queue.get("t1")

    def agent_id(self):
        return self.agents.get(self.conf, "w-1")["id"]

    def test_assign_records_the_owner_id_and_release_keeps_it_on_the_attempt(self):
        self.assertEqual(self.agent_id(), self.assign()["ownerId"])
        self.t.release(self.queue, "t1", "test")
        task = self.queue.get("t1")
        self.assertNotIn("ownerId", task)
        self.assertEqual(self.agent_id(), task["previousAttempts"][-1]["agentId"])

    def test_a_task_of_a_removed_agent_does_not_belong_to_its_successor(self):
        self.assign()
        self.agents.remove(self.conf, "w-1")
        self.agents.add(self.conf, "w-1", "%1")
        self.assertIsNone(self.agents.current_task(self.conf, "w-1"))

    def test_task_checks_accept_the_new_fields(self):
        self.assign()
        self.t.release(self.queue, "t1", "test")
        from theseus import rules
        self.assertFalse([p for p in rules.task_record_problems(self.conf) if re.search("ownerId|agentId|t1", p)])

    def test_migrate_ids_fills_records_of_the_same_agent_once(self):
        self.assign()
        document = self.queue.load()
        document["tasks"][-1].pop("ownerId")
        stale = dict(document["tasks"][-1], execution={"model": "persistent-instance", "assignedAt": "2000-01-01T00:00:00Z"})
        document["tasks"][-1]["previousAttempts"] = [stale]
        self.queue.save(document)
        self.assertIn("1 task(s), agentId on 0 attempt(s)", self.theseus("tasks", "migrate-ids").stdout)
        self.assertIn("0 task(s), agentId on 0 attempt(s)", self.theseus("tasks", "migrate-ids").stdout)
        task = self.queue.get("t1")
        self.assertEqual(self.agent_id(), task["ownerId"])
        self.assertNotIn("agentId", task["previousAttempts"][0])

    def test_json_views_carry_the_id_and_plain_views_do_not(self):
        fake = self.tmp / "bin"
        fake.mkdir()
        (fake / "tmux").write_text("#!/bin/sh\nexit 1\n")
        (fake / "tmux").chmod(0o755)
        env = {**self.env, "PATH": f"{fake}:{self.env['PATH']}"}
        agent = self.agent_id()
        self.assertTrue(agent)
        states = json.loads(self.theseus("agents", "ls", "--json", env=env).stdout)
        self.assertEqual(agent, states["unmanaged"][0]["id"])
        self.assertEqual(agent, json.loads(self.theseus("top", "--once", "--json", env=env).stdout)["agents"][0]["id"])
        self.assertNotIn(agent, self.theseus("agents", "ls", env=env).stdout + self.theseus("agents", env=env).stdout)
