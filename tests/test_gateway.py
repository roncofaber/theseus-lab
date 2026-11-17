"""Gateway errors in agent panes: classification from real pane texts, the backoff schedule, fatal against retryable,
the verified send with a stuck input box, fresh-context recovery and the host-wide circuit breaker. Panes are fakes:
an in-process stand-in for tmux, or the fake tmux executable of test_queue for the command-line checks."""
import os
import subprocess
from pathlib import Path

from helpers import Sandbox
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

        def fake_send(conf, agent, text, task=None, expect_busy=True, profile=None, record=True, sleep=None):
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


class GuardTests(GatewayCase):
    def test_the_host_store_guard(self):
        import pwd
        from theseus import store
        home = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".local/share/theseus")
        self.assertIn(home, os.environ["THESEUS_DB_GUARD"].split(os.pathsep))
        self.assertIn(home, self.env["THESEUS_DB_GUARD"].split(os.pathsep))
        os.environ["THESEUS_DB_GUARD"] = os.pathsep.join([str(self.tmp / "elsewhere"), str(self.tmp / "fenced")])
        os.environ["THESEUS_DB"] = str(self.tmp / "fenced/theseus.db")
        with self.assertRaisesRegex(store.StoreError, "refusing to open"):
            store.path()
        os.environ["THESEUS_DB"] = str(self.tmp / "open/theseus.db")
        self.assertEqual(self.tmp / "open/theseus.db", store.path())
        import sqlite3
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


PARALLEL_429 = ("API Error: Request rejected (429) · Rate limit exceeded for api_key: "
                "3f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a. Limit type: max_parallel_requests. "
                "Current limit: 5, Remaining: 0. Limit resets at: 2026-11-17 18:00:00")
CAPACITY_429 = ("API Error: Request rejected (429) · Model capacity reached for kimi-k31-pro. Priority: None, Rate limit "
                "type: tokens, Model TPM: 600000, Model RPM: not configured, Remaining: 0")
CONN_LOST = "API Error: Connection lost mid-response. The response above may be incomplete."
BUDGET_422 = "API Error: 422 ExceededBudget: User=someone@example.org over budget. Spend=500.03, Budget=500.0"


class TypedErrorTests(FakeSend):
    """Typed classification from the exact error texts, redaction, the transcript as the first source, the stored
    columns, the host-wide cap cut on a parallel cap and the recent-error rows the dashboard shows."""

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
        from theseus import gateway
        parallel = gateway.classify(PARALLEL_429)["excerpt"]
        self.assertIn("api_key: [redacted]", parallel)
        self.assertNotIn("3f9a8b7c6d5e", parallel)
        self.assertIn("max_parallel_requests", parallel)
        self.assertNotIn("someone@example.org", gateway.classify(BUDGET_422)["excerpt"])
        self.assertEqual("token [redacted] used", gateway.redact("token sk-abcdefghijklmnop used"))

