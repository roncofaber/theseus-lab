"""Profile fallback and key-level rate limits, host-wide: a model 5xx takes a profile down and its agents move to the
fallback with the project's counts kept, consecutive OK probes bring it back, and a key-level 429 pauses turns (newest
working agents first, resumed one at a time as the sticky step-down lifts) without touching the profile's health. A
fake clock (explicit epochs), a fake probe and the in-process panes of test_gateway; no real waits or network."""
import unittest
from unittest import mock

from test_gateway import FakeSend, screen

ERROR_500 = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"}}'
KEY_429 = ("API Error: Request rejected (429) · Rate limit exceeded for api_key: "
           "3f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a. Limit type: tokens_per_minute. "
           "Current limit: 4000000, Remaining: 0")
HOST = """
[workers.claude-code]
pool = "shared"
capacity = 2
fallback = "backup"

[workers.claude-code.health]
probe = ["probe-model", "primary"]
probe_seconds = 600
recover_after = 3

[workers.backup]
preset = "claude-code"
pool = "shared"
capacity = 1

[pools.shared]
max = 4
"""
T0 = 1_000_000.0


class FallbackCase(FakeSend):
    def setUp(self):
        super().setUp()
        self.host = self.tmp / "config.toml"
        self.host.write_text(HOST)
        self.write("theseus.local.toml", "[agents]\nmanaged = true\n\n[agents.counts]\nclaude-code = 2\nbackup = 1\n")
        from theseus import config as cfg
        self.conf = cfg.load(root=self.project)
        self.probes = []

    def probe(self, *results):
        answers = list(results)

        def run(command, timeout):
            self.probes.append(list(command))
            return answers.pop(0)
        return run

    def standing(self, profiles):
        """The agent names reconcile works toward when the project runs these live supervised agents."""
        from theseus import supervisor
        regs = {}
        for profile, count in profiles.items():
            for n in range(1, count + 1):
                name = f"{self.conf.name}-{profile}-{n}"
                regs[name] = {"name": name, "profile": profile, "slot": n, "managed": True, "desired": "up", "enabled": True}
        with mock.patch.object(supervisor, "current_task", return_value=None), \
                mock.patch.object(supervisor, "last_run", return_value=None):
            return sorted(slot["name"] for slot in supervisor.standing(self.conf, regs))

    def names(self, **profiles):
        return sorted(f"{self.conf.name}-{p.replace('_', '-')}-{n}" for p, count in profiles.items() for n in range(1, count + 1))

    def go_down(self):
        from theseus import health
        events = self.handle(self.info(screen(ERROR_500)), T0)
        self.assertEqual(["gateway_error"], [kind for kind, _ in events])
        state = health.state("claude-code")
        self.assertEqual(("up", T0), (state["state"], state["next_probe"]), "a model error asks for a probe at once")
        health.host_tick(T0 + 5, self.probe(("fail", "primary fail 500 Internal server error")))
        self.assertEqual([["probe-model", "primary"]], self.probes)


class FallbackTests(FallbackCase):
    def test_a_model_5xx_marks_the_profile_down_and_agents_move_to_the_fallback_keeping_counts(self):
        from theseus import gateway, health, store, tasks
        from theseus import workqueue as wq
        self.assertEqual(self.names(claude_code=2, backup=1), self.standing({"claude-code": 2, "backup": 1}))
        self.go_down()
        state = health.state("claude-code")
        self.assertEqual(("down", True, "backup"), (state["state"], state["on_fallback"], state["fallback"]))
        self.assertIn("probe failed", state["reason"])
        self.assertEqual({"claude-code": "backup"}, health.fallbacks())
        self.assertEqual(self.names(backup=3), self.standing({"claude-code": 2, "backup": 1}),
                         "the two primary agents are retired and two more fallback agents start: three either way")
        self.assertEqual(3, tasks.provider_capacity(self.conf, "backup"), "the fallback takes over the primary's capacity")
        task = {"id": "x", "profile": "claude-code", "acceptance": [], "ownedPaths": []}
        self.assertTrue(wq.fits(self.conf, {"name": "b", "provider": "backup"}, task, {})[0])
        found = health.notify(self.conf)
        self.assertEqual(["fallback"], [e["kind"] for e in found])
        self.assertIn("fallback", wq.ATTENTION, "queue wait wakes for the switch")
        self.assertIn("claude-code is down (probe failed", found[0]["detail"])
        self.assertEqual([], health.notify(self.conf), "one event per switch")
        for agent in ("worker-b", "worker-c"):
            gateway.handle(self.conf, {"name": agent, "pane": "%2"}, self.info(screen(ERROR_500)), now=T0 + 10)
        self.assertIsNone(gateway.paused_until("shared", T0 + 11), "a down profile's errors do not trip the pool breaker")
        task = tasks.Queue(self.conf).get("slow-task")
        worktree = task["worktree"]
        recorded = wq.release_for_fallback(self.conf, self.agent, "slow-task", "claude-code", "backup")
        self.assertEqual(["released"], [e["kind"] for e in recorded])
        task = tasks.Queue(self.conf).get("slow-task")
        self.assertEqual("pending", task["status"])
        self.assertIn(worktree, wq.kept_work_note(task), "the next attempt is pointed at the kept worktree")
        with store.opened() as conn:
            self.assertIsNone(store.gateway_retry(conn, self.conf.name, "worker-a"))

    def test_consecutive_ok_probes_move_the_agents_back(self):
        from theseus import health
        self.go_down()
        health.notify(self.conf)
        run = self.probe(("ok", "primary ok"), ("fail", "primary fail 500"), ("ok", "primary ok"),
                         ("inconclusive", "primary fail 429 rate limited"), ("ok", "primary ok"), ("ok", "primary ok"))
        health.host_tick(T0 + 300, run)
        self.assertEqual(1, len(self.probes), "no probe before probe_seconds")
        for n, epoch in enumerate([T0 + 606, T0 + 1207, T0 + 1808, T0 + 2409, T0 + 3010], 2):
            health.host_tick(epoch, run)
            self.assertEqual(n, len(self.probes))
            self.assertTrue(health.state("claude-code")["on_fallback"], f"still on fallback after probe {n - 1}")
        state = health.state("claude-code")
        self.assertEqual((2, T0 + 3010 + 600), (state["oks"], state["next_probe"]),
                         "a failed probe starts the count over, a rate-limited one says nothing")
        health.host_tick(T0 + 3611, run)
        state = health.state("claude-code")
        self.assertEqual(("up", False, None), (state["state"], state["on_fallback"], state["next_probe"]))
        self.assertIn("3 consecutive OK probes", state["reason"])
        self.assertEqual({}, health.fallbacks())
        self.assertEqual(self.names(claude_code=2, backup=1), self.standing({"claude-code": 2, "backup": 3}),
                         "back on the primary: the two extra fallback agents retire")
        found = health.notify(self.conf)
        self.assertEqual(["fallback"], [e["kind"] for e in found])
        self.assertIn("healthy again", found[0]["detail"])
        self.assertEqual([], health.notify(self.conf))

    def test_errors_stay_out_of_the_breaker_only_while_a_probe_is_pending_or_the_profile_down(self):
        from theseus import gateway, health
        for n, agent in enumerate(("worker-a", "worker-b", "worker-c")):
            gateway.handle(self.conf, {"name": agent, "pane": f"%{n + 1}"}, self.info(screen(ERROR_500)), now=T0 + n)
        self.assertIsNone(gateway.paused_until("shared", T0 + 3), "three agents' 500s wait for the probe's verdict")
        health.host_tick(T0 + 5, self.probe(("ok", "primary ok")))
        self.assertEqual("up", health.state("claude-code")["state"])
        kinds = [e["kind"] for e in gateway.handle(self.conf, {"name": "worker-d", "pane": "%4"},
                                                     self.info(screen(ERROR_500)), now=T0 + 70)]
        self.assertIn("gateway_paused", kinds, "the probe found the model healthy, so the errors trip the breaker again")

    def test_probe_results_from_the_command(self):
        from theseus import health
        self.assertEqual("ok", health.run_probe(["sh", "-c", "echo primary ok"], 5)[0])
        self.assertEqual(("fail", "primary fail 500"), health.run_probe(["sh", "-c", "echo primary fail 500; exit 1"], 5))
        self.assertEqual("inconclusive", health.run_probe("sh -c 'echo primary fail 429; exit 1'", 5)[0])
        self.assertEqual("inconclusive", health.run_probe(["/nonexistent/probe"], 5)[0])


class KeyRateTests(FallbackCase):
    def sync(self, epoch):
        from theseus import pools
        ages = {"agent-b": 3000, "agent-c": 2000, "agent-d": 1000, "worker-a": 100}
        tokens = {"agent-b": 150000, "agent-c": 120000, "agent-d": 100000, "worker-a": 50000}
        pools.sync_turns(self.conf, "shared", {name: {"since": T0 - age, "tokens": tokens[name]} for name, age in ages.items()},
                         now=epoch)

    def held(self, epoch):
        from theseus import pools
        self.sync(epoch)
        return pools.turn_plan("shared", epoch)["held"]

    def test_a_key_level_429_pauses_turns_without_fallback_and_the_step_down_holds(self):
        from theseus import gateway, health, pools
        project = self.conf.name
        self.assertEqual(("key", "429 key rate (tokens_per_minute)"),
                         (gateway.classify(KEY_429)["scope"], gateway.classify(KEY_429)["label"]))
        self.assertEqual([], self.held(T0))
        kinds = [kind for kind, _ in self.handle(self.info(screen(KEY_429)), T0)]
        self.assertEqual(["gateway_error", "gateway_capped", "gateway_paused"], kinds)
        state = health.state("claude-code")
        self.assertEqual(("up", [], None), (state["state"], state["errors"], state["next_probe"]), "rate is not health")
        self.assertEqual({}, health.fallbacks())
        self.assertTrue(gateway.paused_until("shared", T0 + 1), "every agent's retries wait for the pause")
        self.assertEqual([f"{project}/worker-a"], self.held(T0 + 1), "the newest working agent is held first")
        gateway.handle(self.conf, {"name": "agent-c", "pane": "%3"}, self.info(screen(KEY_429)), now=T0 + 90)
        self.assertEqual([f"{project}/agent-d", f"{project}/worker-a"], self.held(T0 + 91))
        self.assertEqual([], self.handle(self.info(screen(KEY_429)), T0 + 300), "no resend into a held agent")
        self.assertEqual([], self.sent)
        allowed, why = pools.may_add_turn("shared", 10000, T0 + 300)
        self.assertFalse(allowed)
        self.assertIn("held", why)
        self.assertEqual([f"{project}/agent-d", f"{project}/worker-a"], self.held(T0 + 90 + 599), "the cut holds")
        self.assertEqual([f"{project}/worker-a"], self.held(T0 + 90 + 601), "then one slot comes back, oldest task first")
        self.assertEqual([], self.handle(self.info(screen(KEY_429)), T0 + 90 + 601))
        self.assertEqual([f"{project}/worker-a"], self.held(T0 + 90 + 600 + 599))
        self.assertEqual([], self.held(T0 + 90 + 600 + 601))
        [(kind, _)] = self.handle(self.info(screen(KEY_429)), T0 + 90 + 600 + 601)
        self.assertEqual("resent", kind)

    def test_the_token_budget_holds_the_newest_agents_and_new_tasks(self):
        from theseus import pools
        project = self.conf.name
        self.host.write_text(HOST + "turns_per_minute = 3\ntokens_per_minute = 1000000\n")
        self.assertEqual([f"{project}/agent-d", f"{project}/worker-a"], self.held(T0),
                         "three turns a minute of 150K and 120K contexts fill most of 1M; the 100K one would overflow")
        self.assertFalse(pools.may_add_turn("shared", 10000, T0)[0])
        self.host.write_text(HOST + "turns_per_minute = 3\ntokens_per_minute = 2000000\n")
        self.assertEqual([], self.held(T0))
        allowed, why = pools.may_add_turn("shared", 300000, T0)
        self.assertFalse(allowed)
        self.assertIn("token budget: 1,260,000 of 2,000,000 tokens/min", why)
        self.assertTrue(pools.may_add_turn("shared", 50000, T0)[0])


class TickTests(FallbackCase):
    def test_a_tick_reports_the_switch_once_and_holds_the_newest_working_agent(self):
        import time
        from types import SimpleNamespace
        from theseus import config as cfg
        from theseus import health, pools, store
        from theseus import workqueue as wq
        self.write("theseus.local.toml", "")
        conf = cfg.load(root=self.project)
        with store.opened() as conn, store.transaction(conn):
            health.write(conn, "claude-code", {**health.STATE, "state": "down", "since": time.time(), "reason": "probe failed",
                                               "next_probe": time.time() + 600, "on_fallback": True, "fallback": "backup",
                                               "switches": 1, "switch_reason": "probe failed"})
        now = time.time()
        pools.sync_turns(SimpleNamespace(name="other"), "shared", {"old": {"since": now - 7200, "tokens": 1000}}, now=now)
        with store.opened() as conn:
            store.cut_cap(conn, "shared", 3, 600, 4, now=now)
        kinds = [e["kind"] for e in wq.tick(conf, assign=False)]
        self.assertEqual(1, kinds.count("fallback"))
        self.assertIn(f"{conf.name}/worker-a", pools.turn_plan("shared")["held"], "the tick recorded the working agent")
        kinds = [e["kind"] for e in wq.tick(conf, assign=False)]
        self.assertEqual((0, 1), (kinds.count("fallback"), kinds.count("turn_held")))
        self.assertEqual([], [e for e in wq.tick(conf, assign=False) if e["kind"] in ("fallback", "turn_held")])
        from theseus import top
        banner = [line for line in top.issues({"agents": [], "health": health.snapshot(), "gateway": [], "pools": [],
                                                "queue": {"review": [], "merged": [], "ready": []},
                                                "verification": {"limit": 0, "unverified": 0}, "project": conf.name})]
        self.assertTrue(any("profile claude-code ON FALLBACK backup" in line and "probe failed" in line for line in banner),
                        banner)

    def test_a_tick_releases_a_supervised_agent_stuck_on_a_gateway_error_and_retries_a_hand_made_one(self):
        import time
        from theseus import config as cfg
        from theseus import gateway, health, store, tasks
        from theseus import workqueue as wq
        self.write("theseus.local.toml", "")
        conf = cfg.load(root=self.project)
        with store.opened() as conn, store.transaction(conn):
            health.write(conn, "claude-code", {**health.STATE, "state": "down", "since": time.time(), "reason": "probe failed",
                                               "on_fallback": True, "fallback": "backup", "switches": 1})
        error = self.info(screen(ERROR_500))["gateway"]
        supervised = {**self.agent, "managed": True, "phase": "ready", "desired": "retire"}
        hand_made = {"name": "worker-b", "pane": "%2", "enabled": True}
        states = {"worker-a": {"name": "worker-a", "state": "gateway_error", "task": "slow-task", "gateway": error},
                  "worker-b": {"name": "worker-b", "state": "gateway_error", "task": "other-task", "gateway": error}}
        handled = []
        with mock.patch.object(wq.ag, "agents", return_value=[supervised, hand_made]), \
                mock.patch.object(wq.ag, "state", side_effect=lambda c, a: dict(states[a["name"]])), \
                mock.patch.object(gateway, "handle", side_effect=lambda c, a, i, now=None: handled.append(a["name"]) or []):
            recorded = wq.tick(conf, assign=False)
        self.assertIn(("released", "slow-task"), [(e["kind"], e["task"]) for e in recorded])
        self.assertEqual("pending", tasks.Queue(conf).get("slow-task")["status"])
        self.assertEqual(["worker-b"], handled, "a hand-made agent keeps its gateway retries")


if __name__ == "__main__":
    unittest.main()
