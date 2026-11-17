"""Host store performance stays flat as history grows: with 200,000 events over 60 days and 100,000 samples, the
dashboard's activity bins over 12 hours, the latest-200 events query and a pool acquire each stay under 50 ms, and the
query plans use the indexes rather than scanning or sorting the whole table.

On a workstation (SQLite 3.53, best of 5) the bins took 0.03 ms for one project and 0.15 ms for the host, the latest
200 events 0.2 ms, a project's last 12 hours of events 0.4 ms, an acquire with its release 0.02 ms and trimming 30,000
samples 17 ms; without the events(project, id) index the latest-200 query took 25 ms, which the plan check catches even
where the time bound would not. Reading a project's whole history (40,000 events) took 57 ms, so readers use since=,
limit= or event_bins rather than the full list."""
import os
import random
import tempfile
import time
import unittest
from pathlib import Path

from theseus import store

EVENTS = 200_000
SAMPLES = 100_000
PROJECTS = [f"project-{n}" for n in range(5)]
KINDS = ["assigned", "delivered", "merged", "completed", "compacted", "nudged", "stalled", "error"]
BOUND_SECONDS = 0.05
SPEC = {"max": 4, "priority": PROJECTS[:2]}


def best_of(function, runs=5):
    timings = []
    for _ in range(runs):
        start = time.perf_counter()
        function()
        timings.append(time.perf_counter() - start)
    return min(timings)


class StoreBenchmark(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="theseus-bench-")
        cls.db = Path(cls._tmp.name) / "theseus.db"
        cls.saved = os.environ.get("THESEUS_STORE_CONFIG")
        os.environ["THESEUS_STORE_CONFIG"] = str(Path(cls._tmp.name) / "store.toml")
        cls.conn = store.connect(cls.db)
        cls.now = time.time()
        rng = random.Random(7)
        span = 60 * store.DAY
        events = sorted((cls.now - rng.random() * span, rng.choice(PROJECTS), rng.choice(KINDS)) for _ in range(EVENTS))
        samples = sorted((cls.now - rng.random() * 20 * store.DAY, rng.choice(PROJECTS)) for _ in range(SAMPLES))
        with store.transaction(cls.conn):
            for project in PROJECTS:
                store.touch_project(cls.conn, project)
            cls.conn.executemany("INSERT INTO events (project, time, epoch, kind, agent, task) VALUES (?, ?, ?, ?, ?, ?)",
                                 [(p, store.iso(e), e, k, f"agent-{n % 8}", f"task-{n % 900}")
                                  for n, (e, p, k) in enumerate(events)])
            cls.conn.executemany("INSERT INTO samples (project, agent, time, epoch, state, task) VALUES (?, ?, ?, ?, ?, ?)",
                                 [(p, f"agent-{n % 8}", store.iso(e), e, "working", None)
                                  for n, (e, p) in enumerate(samples)])
        cls.conn.execute("ANALYZE")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        if cls.saved is None:
            os.environ.pop("THESEUS_STORE_CONFIG", None)
        else:
            os.environ["THESEUS_STORE_CONFIG"] = cls.saved
        cls._tmp.cleanup()

    def report(self, name, seconds):
        self.assertLess(seconds, BOUND_SECONDS, f"{name} took {seconds * 1000:.1f} ms")

    def plan(self, sql, args=()):
        return " | ".join(row[3] for row in self.conn.execute("EXPLAIN QUERY PLAN " + sql, args))

    def test_queries_use_the_indexes(self):
        latest = self.plan("SELECT id FROM events WHERE project = ? ORDER BY id DESC LIMIT 200", (PROJECTS[0],))
        self.assertIn("SEARCH events", latest)
        self.assertNotIn("ORDER BY", latest)
        for where in ("project = ? AND epoch >= ? AND epoch < ? AND kind IN ('delivered', 'partial')",
                      "epoch >= ? AND epoch < ?"):
            args = ((PROJECTS[0],) if "project" in where else ()) + (self.now - 43200, self.now)
            self.assertIn("SEARCH events", self.plan(f"SELECT COUNT(*) FROM events WHERE {where}", args))
        self.assertIn("SEARCH samples", self.plan("DELETE FROM samples WHERE epoch < ?", (self.now,)))

    def test_loaded_row_counts(self):
        self.assertEqual(EVENTS, self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        self.assertEqual(SAMPLES, self.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0])

    def test_activity_bins_over_twelve_hours(self):
        since = self.now - 12 * 3600
        bins = []
        project = lambda: bins.append(store.event_bins(self.conn, PROJECTS[0], since, 900,
                                                       kinds=["delivered", "partial", "blocked"], until=self.now))
        self.report("event_bins, one project, 12 h", best_of(project))
        self.assertEqual(48, len(bins[-1]))
        self.report("event_bins, every project, 12 h",
                    best_of(lambda: store.event_bins(self.conn, None, since, 900, until=self.now)))

    def test_latest_two_hundred_events(self):
        found = []
        self.report("latest 200 events of a project", best_of(lambda: found.append(store.events(self.conn, PROJECTS[1],
                                                                                                limit=200))))
        self.assertEqual(200, len(found[-1]))
        self.report("latest 200 events of the host", best_of(lambda: store.events(self.conn, limit=200)))

    def test_events_of_the_last_twelve_hours(self):
        since = self.now - 12 * 3600
        found = []
        self.report("a project's events of the last 12 h",
                    best_of(lambda: found.append(store.events(self.conn, PROJECTS[0], since=since))))
        self.assertGreater(len(found[-1]), 100)
        self.assertTrue(all(e["epoch"] >= since for e in found[-1]))

    def test_pool_acquire(self):
        def cycle():
            allowed, _ = store.acquire(self.conn, "gw", PROJECTS[0], "agent-0", "task-0", spec=SPEC)
            self.assertTrue(allowed)
            store.release(self.conn, "gw", PROJECTS[0], "agent-0")
        store.sync(self.conn, "gw", PROJECTS[2], {"agent-1": "t1", "agent-2": "t2"})
        self.report("pool acquire and release", best_of(cycle))

    def test_trimming_old_samples(self):
        removed = store.trim(self.conn, now=self.now)
        self.assertGreater(removed["samples"], SAMPLES * 0.2)
        self.assertEqual(0, removed["events"])
        self.assertEqual(0, self.conn.execute("SELECT COUNT(*) FROM samples WHERE epoch < ?",
                                              (self.now - 14 * store.DAY,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
