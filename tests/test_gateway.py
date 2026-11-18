"""Gateway errors in agent panes: classification from real pane texts, the backoff schedule, fatal against retryable,
the verified send with a stuck input box, fresh-context recovery and the host-wide circuit breaker. Panes are fakes:
an in-process stand-in for tmux, or the fake tmux executable of test_queue for the command-line checks."""
import os
import subprocess
import unittest
from pathlib import Path

from helpers import Sandbox, fake_clock
from test_queue import FAKE_TMUX

PROMPT = "\u276f"
CF_524 = ('API Error: 524 {"type":"https://developers.cloudflare.com/support/troubleshooting/http-status-codes/'
          'cloudflare-5xx-errors/error-524/","title":"Error 524: A timeout occurred","status":524,"detail":"The origin web '
          'server did not return a complete response within the 120-second Proxy Read Timeout window.","error_code":524,'
          '"error_name":"origin_response_timeout","error_category":"origin","zone":"gateway.example.org",'
          '"cloudflare_error":true,"retryable":true,"retry_after":120,"owner_action_required":true}')
CF_530 = ('API Error: 530 {"type":"https://developers.cloudflare.com/support/troubleshooting/http-status-codes/'
          'cloudflare-1xxx-errors/error-1033/","title":"Error 1033: Cloudflare Tunnel error","status":530,"detail":"The '
          'host is configured as a Cloudflare Tunnel, but Cloudflare is currently unable to reach it.","error_code":1033,'
          '"error_name":"tunnel_error","error_category":"config","cloudflare_error":true}')
CF_403 = ("API Error: 403 Attention Required! | Cloudflare (error type authentication_failed, HTTP 403, model sent to the "
          "API: some-model)")
RATE_429 = ('API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"Number of request tokens has '
            'exceeded your per-minute rate limit"}}')
OVERLOADED = 'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
RESEND = f"{PROMPT} Resend your message to try again"
FOOTER = ["", "  new task? /clear to save 142.4k tokens", "-" * 60, RESEND, "-" * 60, "  ? for shortcuts"]


def wrap(text, width=100):
    """Lines as tmux capture-pane prints a long line in a pane of this width."""
    return [text[i:i + width] for i in range(0, len(text), width)]


def screen(*errors, width=100):
    lines = ["Some earlier output of the worker.", "", f"{PROMPT} the last prompt", ""]
    for error in errors:
        lines += wrap(error, width)
    return lines + FOOTER


class FakePane:
    """In-process tmux: records keys and answers capture-pane from a screen that the test (or a reaction to Enter)
    sets."""

    def __init__(self, lines=None):
        self.lines = list(lines or ["> "])
        self.keys = []
        self.on_enter = []

    def __call__(self, *args):
        args = list(args)
        if args[0] == "send-keys":
            self.keys.append(args[3:])
            if args[-1] == "Enter" and self.on_enter:
                self.lines = self.on_enter.pop(0)
        elif args[0] == "capture-pane":
            return subprocess.CompletedProcess(args, 0, "\n".join(self.lines) + "\n", "")
        elif args[0] == "display":
            return subprocess.CompletedProcess(args, 0, "1\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def typed(self):
        return [k[-1] for k in self.keys if k[:1] == ["-l"]]


class GatewayCase(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        bin_dir = self.tmp / "bin"
        self.state = self.tmp / "state"
        bin_dir.mkdir()
        self.state.mkdir()
        (bin_dir / "tmux").write_text(FAKE_TMUX)
        (bin_dir / "tmux").chmod(0o755)
        extra = {"PATH": f"{bin_dir}{os.pathsep}{self.env['PATH']}", "FAKE_STATE": str(self.state), "FAKE_PANES": "%1 %2",
                 "THESEUS_COMPACT_POLL": "0.01", "THESEUS_TYPE_POLL": "0.01", "THESEUS_SEND_VERIFY": "0.3",
                 "THESEUS_SEND_POLL": "0.01"}
        self.env.update(extra)
        os.environ.update(extra)
        self.theseus("tasks", "milestone", "M1")
        self.git("commit", "-q", "-am", "milestone")
        self.theseus("agents", "add", "worker-a", "%1")
        self.theseus("tasks", "add", "slow-task", "--role", "research", "--owned", "docs/investigations/slow-task.md",
                     "--objective", "Do it.", "--contract", "All variants.", "--accept", "Done.")
        self.git("commit", "-q", "-am", "queue slow-task")
        self.theseus("queue", "tick")
        from theseus import agents as ag
        from theseus import config as cfg
        self.ag = ag
        self.conf = cfg.load(root=self.project)
        self.agent = ag.get(self.conf, "worker-a")
        self.pane = FakePane()
        saved = ag.tmux
        ag.tmux = self.pane
        self.addCleanup(setattr, ag, "tmux", saved)
        self.clock = fake_clock(self)

    def info(self, lines, tokens=None):
        from theseus import gateway
        error = gateway.detect(self.conf, None, lines)
        self.assertIsNotNone(error)
        return {"task": "slow-task", "gateway": error, "contextTokens": tokens}


class ClassificationTests(GatewayCase):
    def detect(self, lines):
        from theseus import gateway
        return gateway.detect(self.conf, None, lines)

    def test_real_error_texts_are_classified(self):
        found = self.detect(screen(CF_524))
        self.assertEqual((524, 120.0, True, "origin_response_timeout", "origin"),
                         (found["code"], found["retry_after"], found["retryable"], found["error_name"], found["error_category"]))
        self.assertFalse(found["fatal"])
        self.assertTrue(found["ready"])
        cut = CF_524[:CF_524.index('"detail"')]
        found = self.detect(screen(cut, width=70))
        self.assertEqual((524, None), (found["code"], found["retry_after"]))
        self.assertFalse(found["fatal"])
        long_524 = CF_524.replace('"detail":"', '"detail":"' + "The origin web server timed out. " * 8)
        found = self.detect(screen(long_524, width=80))
        self.assertEqual((524, 120.0), (found["code"], found["retry_after"]))
        found = self.detect(screen(CF_530 + " " + "x" * 600, width=80))
        self.assertEqual(530, found["code"])
        found = self.detect(screen(CF_530))
        self.assertEqual((530, None, "tunnel_error"), (found["code"], found["retryable"], found["error_name"]))
        self.assertFalse(found["fatal"])
        found = self.detect(screen(CF_403))
        self.assertEqual(403, found["code"])
        self.assertTrue(found["fatal"])
        self.assertEqual((429, "rate_limit_error"), (self.detect(screen(RATE_429))["code"], self.detect(screen(RATE_429))["error_type"]))
        self.assertEqual(529, self.detect(screen(OVERLOADED))["code"])
        self.assertFalse(self.detect(screen(OVERLOADED))["fatal"])
        mislabeled = OVERLOADED.replace("API Error: 529", "API Error: 500")
        self.assertEqual(529, self.detect(screen(mislabeled))["code"])
        self.assertTrue(self.detect(screen("API Error: 400 Prompt is too long: 210000 tokens > 200000 maximum"))["fatal"])
        self.assertTrue(self.detect(screen('API Error: 400 {"type":"error","error":{"type":"invalid_request_error"}}'))["fatal"])
        found = self.detect(["worker output"] + FOOTER)
        self.assertEqual((None, False, True), (found["code"], found["fatal"], found["ready"]))
        self.assertTrue(self.detect(screen(CF_524, CF_403))["fatal"])
        self.assertFalse(self.detect(screen(CF_403, CF_524))["fatal"])
        self.assertIsNone(self.detect(["Processed 529 files and 429 records; the rate is fine.", "> "]))
        self.assertIsNone(self.detect([CF_524] + [f"line {n}" for n in range(30)] + ["> "]))
        from theseus import gateway
        self.assertEqual(142400, gateway.context_tokens(self.conf, None, screen(CF_524)))
        self.assertIsNone(gateway.context_tokens(self.conf, None, ["> "]))


class FakeSend(GatewayCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        saved = self.ag.send_verified

        def fake_send(conf, agent, text, task=None, expect_busy=True, profile=None, record=True, sleep=None, **extra):
            self.sent.append(text)
            return True, "sent"
        self.ag.send_verified = fake_send
        self.addCleanup(setattr, self.ag, "send_verified", saved)

    def handle(self, info, now):
        from theseus import gateway
        return [(e["kind"], e["detail"]) for e in gateway.handle(self.conf, self.agent, info, now=now)]


class RetryTests(FakeSend):

    def test_retryable_errors_wait_resend_and_stall_after_three_attempts(self):
        from theseus import store
        from theseus import workqueue as wq
        info = self.info(screen(CF_524))
        now = 1_000_000.0
        [(kind, detail)] = self.handle(info, now)
        self.assertEqual("gateway_error", kind)
        self.assertIn("code 524 attempt 1/3, next retry at", detail)
        self.assertIn("in 120s, retry_after 120s", detail)
        self.assertEqual([], self.handle(info, now + 60), "no resend before the backoff passes")
        self.assertEqual([], self.sent)
        with store.opened() as conn:
            row = store.gateway_retry(conn, self.conf.name, "worker-a")
        self.assertEqual(("waiting", now + 120), (row["phase"], row["next_epoch"]), "the timing survives a restart")
        [(kind, detail)] = self.handle(info, now + 121)
        self.assertEqual("resent", kind)
        self.assertIn("continue after gateway 524 origin timeout, attempt 1/3", detail)
        self.assertIn("The gateway error is over", self.sent[-1])
        steps = [(now + 130, 240), (now + 130 + 241 + 10, 480)]
        for number, (seen, wait) in enumerate(steps, 2):
            [(kind, detail)] = self.handle(info, seen)
            self.assertIn(f"attempt {number}/3", detail)
            self.assertIn(f"in {wait}s", detail)
            [(kind, detail)] = self.handle(info, seen + wait + 1)
            self.assertEqual("resent", kind)
            self.assertIn("fresh context", detail, "the second error of a task gets a fresh context")
        self.assertEqual(["stalled"], [k for k, _ in self.handle(info, now + 5000)])
        self.assertEqual([], self.handle(info, now + 6000), "exhaustion is reported once")
        kinds = [e["kind"] for e in wq.events(self.conf)]
        self.assertEqual(3, kinds.count("gateway_error"))
        self.assertIn("gateway 524 origin timeout x3", wq.events(self.conf, 1)[-1]["detail"])
        self.assertIn("/clear", self.sent)
        restart = self.sent[-1]
        for expected in ("slow-task", str(self.tmp / "worktrees"), "Continue the partial report or changes in place",
                         "prompt.md", "instance-brief.md"):
            self.assertIn(expected, restart)
        prompt = restart.split("The task prompt is ")[1].split(". ")[0]
        self.assertIn("slow-task", Path(prompt).read_text())


PARALLEL_429 = ("API Error: Request rejected (429) · Rate limit exceeded for api_key: "
                "3f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a. Limit type: max_parallel_requests. "
                "Current limit: 5, Remaining: 0. Limit resets at: 2026-11-17 18:00:00")
CAPACITY_429 = ("API Error: Request rejected (429) · Model capacity reached for kimi-k31-pro. Priority: None, Rate limit "
                "type: tokens, Model TPM: 600000, Model RPM: not configured, Remaining: 0")
CONN_LOST = "API Error: Connection lost mid-response. The response above may be incomplete."
BUDGET_422 = "API Error: 422 ExceededBudget: User=someone@example.org over budget. Spend=500.03, Budget=500.0"


class TypedErrorTests(unittest.TestCase):
    """Typed labels and redacted excerpts from the exact error texts."""

    def test_each_error_text_gets_a_typed_label_and_a_redacted_excerpt(self):
        from theseus import gateway
        found = {name: gateway.classify(text, kind) for name, text, kind in (
            ("parallel", PARALLEL_429, "rate_limit"), ("capacity", CAPACITY_429, "rate_limit"), ("524", CF_524, "server_error"),
            ("530", CF_530, None), ("lost", CONN_LOST, "server_error"), ("529", OVERLOADED, None), ("budget", BUDGET_422, None),
            ("marker", "Resend your message to try again", None))}
        self.assertEqual({"parallel": "429 parallel cap", "capacity": "429 model TPM (kimi-k31-pro)",
                          "524": "524 origin timeout", "530": "530 tunnel down", "lost": "conn lost",
                          "529": "529 overloaded", "budget": "422 budget exceeded", "marker": "unknown (resend marker)"},
                         {name: typed["label"] for name, typed in found.items()})
        self.assertEqual(("connection_lost", None, True), (found["lost"]["kind"], found["lost"]["status"], found["lost"]["retryable"]))
        self.assertEqual((429, "rate_limit", "parallel cap"),
                         (found["parallel"]["status"], found["parallel"]["kind"], found["parallel"]["subtype"]))
        self.assertFalse(found["budget"]["retryable"])
        self.assertTrue(all(found[name]["retryable"] for name in ("parallel", "capacity", "524", "530", "lost", "529")))
        parallel = gateway.classify(PARALLEL_429)["excerpt"]
        self.assertIn("api_key: [redacted]", parallel)
        self.assertNotIn("3f9a8b7c6d5e", parallel)
        self.assertIn("max_parallel_requests", parallel)
        self.assertNotIn("someone@example.org", gateway.classify(BUDGET_422)["excerpt"])
        self.assertEqual("token [redacted] used", gateway.redact("token sk-abcdefghijklmnop used"))


class FakePanes:
    """In-process tmux for many panes at once: Enter makes a pane show the busy marker on its next capture only, unless
    the pane is deaf (nothing happens) or sticky (the typed text stays in its input line)."""

    def __init__(self, deaf=(), sticky=()):
        import threading
        self.deaf, self.sticky, self.lines, self.busy, self.typed = set(deaf), set(sticky), {}, set(), []
        self.lock = threading.Lock()

    def __call__(self, *args):
        args = list(args)
        pane = args[args.index("-t") + 1] if "-t" in args else None
        with self.lock:
            if args[0] == "send-keys" and args[-1] == "Enter":
                if pane not in self.deaf and pane not in self.sticky:
                    self.busy.add(pane)
            elif args[0] == "send-keys" and "-l" in args:
                self.typed.append((pane, args[-1]))
                if pane in self.sticky:
                    self.lines[pane] = [f"{PROMPT} {args[-1]}"]
            elif args[0] == "capture-pane":
                lines = list(self.lines.get(pane, ["> "]))
                if pane in self.busy:
                    self.busy.discard(pane)
                    lines.append("working (esc to interrupt)")
                return subprocess.CompletedProcess(args, 0, "\n".join(lines) + "\n", "")
            elif args[0] == "display":
                return subprocess.CompletedProcess(args, 0, "1\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")


class BatchedSendTests(GatewayCase):
    """A queue tick types every message at once and verifies them together within its send budget, so many agents
    that never confirm cost one verification window, not one each."""

    def agents(self, count):
        return [{"name": f"agent-{n:02d}", "pane": f"%{100 + n}"} for n in range(count)]

    def use(self, panes):
        saved = self.ag.tmux
        self.ag.tmux = panes
        self.addCleanup(setattr, self.ag, "tmux", saved)

    def test_agents_that_never_confirm_cost_one_window_together_and_keep_send_failed(self):
        from theseus import workqueue as wq
        os.environ["THESEUS_SEND_VERIFY"] = "1"
        agents = self.agents(8)
        panes = FakePanes(deaf={a["pane"] for a in agents[:6]})
        self.use(panes)
        started = self.clock.monotonic()
        with self.ag.batched(30) as sends:
            for agent in agents:
                self.assertEqual((True, "typed; verifying"), self.ag.send_verified(self.conf, agent, f"Hello {agent['name']}."))
            recorded = wq.verify_sends(self.conf, sends)
        self.assertLess(self.clock.monotonic() - started, 4, "six silent agents verified one after another take 6 s or more")
        self.assertEqual(sorted(a["name"] for a in agents[:6]), sorted(e["agent"] for e in recorded))
        self.assertTrue(all(e["kind"] == "send_failed" and "not busy after Enter" in e["detail"] for e in recorded))

    def test_the_budget_starts_when_the_tick_verifies_and_an_unverified_profile_owes_nothing_twice(self):
        from theseus import workqueue as wq
        agent = self.agents(1)[0]
        self.use(FakePanes())
        with self.ag.batched(0.5) as sends:
            self.ag.send_verified(self.conf, agent, "Continue.", "slow-task", kind="nudge")
            self.clock.sleep(0.6)
            self.assertEqual([], wq.verify_sends(self.conf, sends))
        self.assertEqual(["ok"], [item["result"] for item in sends.items], "time spent before verifying is not budget")
        self.write("theseus.local.toml", '[workers.claude-code.interactive]\nbusy_pattern = ""\n')
        from theseus import config as cfg
        conf = cfg.load(root=self.project)
        wq.owe_prompt(conf, "worker-a", "slow-task", "a test")
        with self.ag.batched(5):
            self.assertEqual("sent", wq.deliver_owed(conf, self.agent)[0])
        self.assertIsNone(wq.owed(conf, "worker-a"), "a profile without a busy marker gets its prompt once")

    def test_sends_left_over_by_the_budget_are_finished_by_the_next_tick(self):
        from theseus import workqueue as wq
        os.environ["THESEUS_SEND_VERIFY"] = "5"
        answered, stuck, quick = self.agents(3)
        panes = FakePanes(deaf={answered["pane"]}, sticky={stuck["pane"]})
        self.use(panes)
        started = self.clock.monotonic()
        with self.ag.batched(0.2) as sends:
            for agent in (answered, stuck, quick):
                self.ag.send_verified(self.conf, agent, "Continue the task.", "slow-task", kind="nudge")
            self.assertEqual([], wq.verify_sends(self.conf, sends))
        self.assertLess(self.clock.monotonic() - started, 3, "the budget bounds the tick")
        carried = {name: entry["verify"] for name, entry in wq.runtime(self.conf).items() if entry.get("verify")}
        self.assertEqual({answered["name"], stuck["name"]}, set(carried), "the quick agent confirmed within the budget")
        self.assertTrue(all(entry["carried"] for entry in carried.values()))
        os.environ["THESEUS_SEND_VERIFY"] = "0.3"
        with self.ag.batched(10) as sends:
            found = wq.resume_sends(self.conf, [answered, stuck, quick], sends)
            self.assertEqual([], found, "an agent that answered between ticks is not a failed send")
            self.assertEqual([stuck["name"]], [item["agent"] for item in sends.items])
            found = wq.verify_sends(self.conf, sends)
        self.assertEqual([("send_failed", stuck["name"])], [(e["kind"], e["agent"]) for e in found])
        self.assertIn("still holds the text after a second Enter", found[0]["detail"])
        self.assertFalse(any(entry.get("verify") for entry in wq.runtime(self.conf).values()))


class RestartPromptTests(GatewayCase):
    def test_a_fresh_restart_with_its_task_owes_one_prompt_and_calls_off_a_gateway_resend(self):
        from unittest import mock
        from theseus import cli, store, supervisor
        from theseus import workqueue as wq
        with store.opened() as conn:
            store.put_gateway_retry(conn, self.conf.name, "worker-a", task="slow-task", pool="claude-code", phase="waiting",
                                    next_epoch=1.0)
        task = {"id": "slow-task"}
        restarts = []
        gens = iter([{"gen": 1}, {"gen": 2}])
        with mock.patch.object(supervisor, "current_task", return_value=task), \
                mock.patch.object(supervisor, "last_run", side_effect=lambda *a: next(gens)), \
                mock.patch.object(supervisor, "update"), mock.patch.object(supervisor, "record", return_value=None), \
                mock.patch.object(supervisor, "restart", side_effect=lambda *a, **k: restarts.append(k) or (True, "ready")), \
                mock.patch.object(self.ag, "entry", return_value=self.agent), mock.patch("builtins.print"):
            self.assertEqual(1, cli.restart_agents(self.conf, ["worker-a"], fresh=True, with_task=False))
            self.assertEqual([], restarts, "an agent with a task is not restarted fresh without --with-task")
            self.pane.on_enter = [["Working (esc to interrupt)"]]
            self.assertEqual(0, cli.restart_agents(self.conf, ["worker-a"], True, True, "Resume from your report."))
        self.assertEqual([{"fresh": True}], restarts)
        self.assertEqual(["Resume from your report."], self.pane.typed())
        self.assertIsNone(wq.owed(self.conf, "worker-a"))
        with store.opened() as conn:
            self.assertEqual("resent", store.gateway_retry(conn, self.conf.name, "worker-a")["phase"])
