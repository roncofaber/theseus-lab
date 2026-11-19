"""Which free agent takes which ready task: priority on its 1 to 5 scale with aging, and capability matching."""
import unittest

from theseus import scheduling

HOUR = 3600.0
NOW = 1_000_000.0


def task(task_id, priority=None, need=0, age_hours=0.0):
    created = NOW - age_hours * HOUR
    from datetime import datetime, timezone
    return {"id": task_id, "priority": priority, "minCapability": need,
            "createdAt": datetime.fromtimestamp(created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def plan(agents, tasks, hours=24.0):
    """agents: {name: capability}; every agent fits every task whose minimum it meets."""
    free = [({"name": name}, {}) for name in agents]
    chosen = scheduling.plan(free, tasks,
                             lambda agent, info, t: (agents[agent["name"]] >= scheduling.need(t), None),
                             lambda agent: agents[agent["name"]], hours, now=NOW)
    return {agent["name"]: t["id"] for agent, t, _ in chosen}


class PriorityTests(unittest.TestCase):
    def test_priorities_live_on_a_one_to_five_scale_and_legacy_values_are_read_onto_it(self):
        self.assertEqual([3, 3, 1, 5, 4, 3], [scheduling.priority({"priority": p}) for p in (None, 0, -1, 25, 4, "P2")])
        for bad in (0, 6, -1, "high"):
            with self.assertRaises(ValueError):
                scheduling.check_priority(bad)
        self.assertEqual(5, scheduling.check_priority("5"))

    def test_a_waiting_task_gains_a_level_per_aging_period_up_to_the_top(self):
        self.assertEqual((1, 0), scheduling.effective(task("t", 1, age_hours=23), 24, NOW))
        self.assertEqual((3, 2), scheduling.effective(task("t", 1, age_hours=49), 24, NOW))
        self.assertEqual((5, 4), scheduling.effective(task("t", 1, age_hours=24 * 30), 24, NOW))
        self.assertEqual((1, 0), scheduling.effective(task("t", 1, age_hours=24 * 30), 0, NOW), "0 turns aging off")


class PlanTests(unittest.TestCase):
    def test_higher_priority_first_and_an_old_low_priority_task_eventually_wins(self):
        self.assertEqual({"a": "urgent"}, plan({"a": 1}, [task("normal", 3), task("urgent", 5)]))
        self.assertEqual({"a": "old"}, plan({"a": 1}, [task("new", 4, age_hours=1), task("old", 1, age_hours=24 * 4)]))
        self.assertEqual({"a": "older"}, plan({"a": 1}, [task("newer", 3, age_hours=2), task("older", 3, age_hours=5)]),
                         "equal effective priority: the task that waited longest")

    def test_the_strong_agent_takes_the_task_only_it_can_do_even_over_a_higher_priority_one(self):
        self.assertEqual({"strong": "hard", "weak": "easy"},
                         plan({"strong": 3, "weak": 1}, [task("easy", 5), task("hard", 2, need=3)]))

    def test_the_strong_agent_leaves_easy_work_to_free_weaker_agents(self):
        tasks = [task("easy-1", 5), task("easy-2", 4)]
        self.assertEqual({"weak-1": "easy-1", "weak-2": "easy-2"},
                         plan({"strong": 3, "weak-1": 1, "weak-2": 1}, tasks), "the strong agent stays free")

    def test_the_strong_agent_takes_the_surplus_weaker_agents_cannot_cover(self):
        tasks = [task("easy-1", 5), task("easy-2", 4), task("mid", 3, need=2)]
        found = plan({"strong": 3, "weak": 1}, tasks)
        self.assertEqual("easy-1", found["weak"])
        self.assertEqual("mid", found["strong"], "of the surplus it takes the harder task")

    def test_alone_the_strong_agent_works_on_easy_tasks(self):
        self.assertEqual({"strong": "easy"}, plan({"strong": 3}, [task("easy", 3)]))

    def test_peers_share_by_priority(self):
        self.assertEqual({"a": "first", "b": "second"}, plan({"a": 2, "b": 2}, [task("second", 3), task("first", 4)]))

    def test_no_task_goes_to_an_agent_below_its_minimum(self):
        self.assertEqual({}, plan({"weak": 1}, [task("hard", 5, need=2)]))


if __name__ == "__main__":
    unittest.main()
