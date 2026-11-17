"""Gateway errors in agent panes: classification from real pane texts, the backoff schedule, fatal against retryable,
the verified send with a stuck input box, fresh-context recovery and the host-wide circuit breaker. Panes are fakes:
an in-process stand-in for tmux, or the fake tmux executable of test_queue for the command-line checks."""
import json
import os
import subprocess
import time
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

    def test_a_524_with_cloudflare_json_is_retryable_with_its_retry_after(self):
        found = self.detect(screen(CF_524))
        self.assertEqual((524, 120.0, True, "origin_response_timeout", "origin"),
                         (found["code"], found["retry_after"], found["retryable"], found["error_name"], found["error_category"]))
        self.assertFalse(found["fatal"])
        self.assertTrue(found["ready"])

    def test_json_cut_off_by_the_pane_width_still_gives_the_status_and_surviving_fields(self):
        cut = CF_524[:CF_524.index('"detail"')]
        found = self.detect(screen(cut, width=70))
        self.assertEqual((524, None), (found["code"], found["retry_after"]))
        self.assertFalse(found["fatal"])

    def test_a_full_length_error_in_a_narrow_pane_is_read_from_the_whole_screen(self):
        long_524 = CF_524.replace('"detail":"', '"detail":"' + "The origin web server timed out. " * 8)
        found = self.detect(screen(long_524, width=80))
        self.assertEqual((524, 120.0), (found["code"], found["retry_after"]))
        found = self.detect(screen(CF_530 + " " + "x" * 600, width=80))
        self.assertEqual(530, found["code"])

    def test_a_530_tunnel_error_without_a_retryable_field_is_retryable(self):
        found = self.detect(screen(CF_530))
        self.assertEqual((530, None, "tunnel_error"), (found["code"], found["retryable"], found["error_name"]))
        self.assertFalse(found["fatal"])

    def test_a_plain_text_403_is_fatal(self):
        found = self.detect(screen(CF_403))
        self.assertEqual(403, found["code"])
        self.assertTrue(found["fatal"])

    def test_anthropic_error_types_win_over_the_status_and_are_retryable(self):
        self.assertEqual((429, "rate_limit_error"), (self.detect(screen(RATE_429))["code"], self.detect(screen(RATE_429))["error_type"]))
        self.assertEqual(529, self.detect(screen(OVERLOADED))["code"])
        self.assertFalse(self.detect(screen(OVERLOADED))["fatal"])
        mislabeled = OVERLOADED.replace("API Error: 529", "API Error: 500")
        self.assertEqual(529, self.detect(screen(mislabeled))["code"])

    def test_context_too_long_and_invalid_requests_are_fatal(self):
        self.assertTrue(self.detect(screen("API Error: 400 Prompt is too long: 210000 tokens > 200000 maximum"))["fatal"])
        self.assertTrue(self.detect(screen('API Error: 400 {"type":"error","error":{"type":"invalid_request_error"}}'))["fatal"])

    def test_the_resend_marker_alone_is_a_retryable_error_of_unknown_status(self):
        found = self.detect(["worker output"] + FOOTER)
        self.assertEqual((None, False, True), (found["code"], found["fatal"], found["ready"]))

    def test_the_newest_error_on_screen_decides(self):
        self.assertTrue(self.detect(screen(CF_524, CF_403))["fatal"])
        self.assertFalse(self.detect(screen(CF_403, CF_524))["fatal"])

    def test_worker_output_with_numbers_is_not_a_gateway_error(self):
        self.assertIsNone(self.detect(["Processed 529 files and 429 records; the rate is fine.", "> "]))
        self.assertIsNone(self.detect([CF_524] + [f"line {n}" for n in range(30)] + ["> "]))

    def test_context_hint_is_read_in_tokens(self):
        from theseus import gateway
        self.assertEqual(142400, gateway.context_tokens(self.conf, None, screen(CF_524)))
        self.assertIsNone(gateway.context_tokens(self.conf, None, ["> "]))

    def test_an_idle_pane_with_an_error_and_a_task_is_in_gateway_error(self):
        self.pane.lines = screen(CF_524)
        info = self.ag.state(self.conf, self.agent)
        self.assertEqual(("gateway_error", 524, 142400), (info["state"], info["gateway"]["code"], info["contextTokens"]))
        self.pane.lines = screen(CF_524)[:-1] + ["Thinking (esc to interrupt)"]
        self.assertEqual("working", self.ag.state(self.conf, self.agent)["state"])


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

    def test_backoff_schedule_honors_retry_after_and_slow_codes(self):
        from theseus import gateway
        self.assertEqual([120, 240, 480, 480], [gateway.delay(self.conf, n, 524, 120) for n in range(4)])
        self.assertEqual([600, 600, 600], [gateway.delay(self.conf, n, 524, 600) for n in range(3)])
        self.assertEqual([240, 480, 960], [gateway.delay(self.conf, n, 530) for n in range(3)])

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
        self.assertIn("continue after gateway 524, attempt 1/3", detail)
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
        self.assertIn("gateway 524 x3", wq.events(self.conf, 1)[-1]["detail"])
        self.assertIn("/clear", self.sent)
        restart = self.sent[-1]
        for expected in ("slow-task", str(self.tmp / "worktrees"), "Continue the partial report or changes in place",
                         "prompt.md", "instance-brief.md"):
            self.assertIn(expected, restart)
        prompt = restart.split("The task prompt is ")[1].split(". ")[0]
        self.assertIn("slow-task", Path(prompt).read_text())

    def test_a_fatal_error_is_reported_at_once_and_never_resent(self):
        info = self.info(screen(CF_403))
        [(kind, detail)] = self.handle(info, 1000.0)
        self.assertEqual("error", kind)
        self.assertIn("gateway 403 fatal", detail)
        self.assertEqual([], self.handle(info, 5000.0))
        self.assertEqual([], self.sent)

    def test_a_large_context_gets_a_fresh_start_on_the_first_resend(self):
        info = self.info(screen(CF_524), tokens=142400)
        self.handle(info, 1000.0)
        [(kind, detail)] = self.handle(info, 1200.0)
        self.assertIn("fresh context", detail)
        self.assertEqual("/clear", self.sent[0])
        self.assertIn("Your context was cleared after gateway 524", self.sent[1])

    def test_busy_without_progress_is_no_recovery_and_the_attempts_run_out(self):
        from theseus import gateway
        info = self.info(screen(CF_524))
        now = 1_000_000.0
        for attempt in (1, 2, 3):
            [(kind, detail)] = self.handle(info, now)
            self.assertIn(f"attempt {attempt}/3", detail)
            now += 1000
            self.assertEqual("resent", self.handle(info, now)[0][0])
            gateway.observe(self.conf, self.agent, {"task": "slow-task", "lastChange": now - 5000}, now=now + 60)
            now += 180
        [(kind, detail)] = self.handle(info, now)
        self.assertEqual("stalled", kind)
        self.assertIn("gateway 524 x3", detail)

    def test_a_pane_that_gets_going_without_the_resend_is_not_resent(self):
        from theseus import gateway
        info = self.info(screen(CF_524))
        self.handle(info, 1000.0)
        gateway.observe(self.conf, self.agent, {"task": "slow-task"}, now=1050.0)
        [(kind, detail)] = self.handle(info, 1200.0)
        self.assertEqual("gateway_error", kind)
        self.assertIn("attempt 1/3", detail, "no resend of ours happened, so no attempt was used")
        self.assertEqual([], self.sent)

    def test_an_agent_that_made_progress_recovers_and_old_error_text_is_not_a_new_error(self):
        from theseus import gateway
        info = self.info(screen(CF_524))
        self.handle(info, 1000.0)
        self.handle(info, 1200.0)
        gateway.observe(self.conf, self.agent, {"task": "slow-task", "lastChange": 1250.0}, now=1260.0)
        stale = dict(info["gateway"], ready=False)
        self.assertTrue(gateway.stale(self.conf, "worker-a", stale))
        self.assertFalse(gateway.stale(self.conf, "worker-a", info["gateway"]), "a ready marker is always current")
        [(kind, detail)] = self.handle(info, 2000.0)
        self.assertIn("attempt 1/3", detail, "a recovered episode starts counting again")


class BreakerTests(FakeSend):
    def test_errors_across_projects_pause_the_pool_and_defer_resends(self):
        from theseus import gateway, store
        with store.opened() as conn:
            for project in ("other-port", "third-port"):
                for _ in range(2):
                    store.add_gateway_error(conn, "claude-code", project, 524, True, agent="w", at=time.time() - 60)
        now = time.time()
        events = self.handle(self.info(screen(CF_524)), now)
        self.assertEqual(["gateway_error", "gateway_paused"], [k for k, _ in events])
        self.assertIn("pool claude-code paused until", events[1][1])
        self.assertIn("3 agents hit retryable gateway errors in 10 min", events[1][1])
        self.assertIsNotNone(gateway.paused_until("claude-code", now))
        with store.opened() as conn:
            store.pause_gateway(conn, "claude-code", now + 3600, "test", now=now)
        self.assertEqual([], self.handle(self.info(screen(CF_524)), now + 200), "resends wait while the pool is paused")
        self.assertEqual([], self.sent)
        with store.opened() as conn:
            self.assertEqual(0, store.gateway_retry(conn, self.conf.name, "worker-a")["attempt"], "a deferral costs no attempt")
        health = gateway.health(now=now + 200)
        self.assertEqual(("claude-code", [524] * 5), (health[0]["pool"], health[0]["codes"]))
        self.assertIsNotNone(health[0]["pausedUntil"])

    def test_one_tunnel_error_pauses_the_pool_and_is_reported_once(self):
        now = time.time()
        events = self.handle(self.info(screen(CF_530)), now)
        self.assertEqual(["gateway_error", "gateway_paused"], [k for k, _ in events])
        self.assertIn("in 240s", events[0][1])
        from theseus import store
        with store.opened() as conn:
            self.assertFalse(store.pause_gateway(conn, "claude-code", now + 10, "again", now=now))

    def test_rate_limited_resends_are_staggered_across_the_pool(self):
        from theseus import store
        now = time.time()
        with store.opened() as conn:
            store.put_gateway_retry(conn, "other-port", "w", task="t", pool="claude-code", phase="waiting",
                                    next_epoch=now + 100)
        [(kind, detail)] = self.handle(self.info(screen(RATE_429)), now)
        self.assertIn("in 130s", detail)


class GuardTests(GatewayCase):
    def test_the_store_refuses_a_path_under_the_guard(self):
        from theseus import store
        os.environ["THESEUS_DB_GUARD"] = os.pathsep.join([str(self.tmp / "elsewhere"), str(self.tmp / "fenced")])
        os.environ["THESEUS_DB"] = str(self.tmp / "fenced/theseus.db")
        with self.assertRaisesRegex(store.StoreError, "refusing to open"):
            store.path()
        os.environ["THESEUS_DB"] = str(self.tmp / "open/theseus.db")
        self.assertEqual(self.tmp / "open/theseus.db", store.path())

    def test_migration_3_runs_from_version_2_even_over_leftover_gateway_tables(self):
        import sqlite3
        from theseus import store
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

    def test_the_session_fences_off_the_real_host_store(self):
        import pwd
        home = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".local/share/theseus")
        self.assertIn(home, os.environ["THESEUS_DB_GUARD"].split(os.pathsep))
        self.assertIn(home, self.env["THESEUS_DB_GUARD"].split(os.pathsep))


class VerifiedSendTests(GatewayCase):
    def test_a_stuck_input_box_gets_one_more_enter(self):
        text = "Continue the current task from the state of your worktree."
        self.pane.on_enter = [["output", f"{PROMPT} {text}", "  ? for shortcuts"], ["Working (esc to interrupt)"]]
        ok, detail = self.ag.send_verified(self.conf, self.agent, text, "slow-task")
        self.assertTrue(ok, detail)
        self.assertEqual("sent after a second Enter", detail)
        self.assertEqual(2, sum(1 for keys in self.pane.keys if keys == ["Enter"]))
        self.assertEqual(["C-u"], self.pane.keys[0], "the input line is emptied before typing")

    def test_a_send_that_never_starts_work_is_recorded_as_send_failed(self):
        from theseus import workqueue as wq
        text = "Continue the current task."
        self.pane.on_enter = [[f"{PROMPT} {text}"], [f"{PROMPT} {text}"]]
        ok, detail = self.ag.send_verified(self.conf, self.agent, text, "slow-task")
        self.assertFalse(ok)
        self.assertIn("still holds the text after a second Enter", detail)
        last = wq.events(self.conf, 1)[-1]
        self.assertEqual(("send_failed", "worker-a", "slow-task"), (last["kind"], last["agent"], last["task"]))

    def test_a_slash_command_counts_once_the_input_line_is_empty(self):
        self.pane.on_enter = [["> "]]
        self.assertEqual((True, "sent"), self.ag.send_verified(self.conf, self.agent, "/clear", expect_busy=False))


class QueueGatewayTests(GatewayCase):
    """Through the command line, with the fake tmux executable."""

    def test_tick_reports_the_error_and_wait_does_not_wake_for_it(self):
        (self.state / "screen-%1").write_text("\n".join(screen(CF_524)) + "\n")
        self.assertRegex(self.theseus("agents").stdout, r"worker-a\s+%1\s+gateway_error")
        out = self.theseus("queue", "tick").stdout
        self.assertIn("gateway_error", out)
        self.assertIn("code 524 attempt 1/3", out)
        self.assertNotIn("nudged", self.theseus("queue", "tick").stdout, "the idle nudge leaves a gateway error alone")
        waited = self.theseus("queue", "wait", "--timeout", "2", "--interval", "1", check=False)
        self.assertEqual(1, waited.returncode, waited.stdout)
        top = self.theseus("top", "--once").stdout
        self.assertIn("gateway claude-code", top)
        self.assertIn("1 error(s) in 24 h: 524", top)
        snapshot = json.loads(self.theseus("top", "--json").stdout)
        self.assertEqual([524], snapshot["gateway"][0]["codes"])

    def test_a_paused_pool_gets_no_new_assignments_and_shows_on_the_host_summary(self):
        from theseus import store
        self.theseus("tasks", "add", "next-task", "--role", "research", "--owned", "docs/investigations/next-task.md",
                     "--objective", "Do it.", "--contract", "All variants.", "--accept", "Done.")
        self.git("commit", "-q", "-am", "queue next-task")
        self.theseus("agents", "add", "worker-b", "%2")
        with store.opened() as conn:
            store.add_gateway_error(conn, "claude-code", "other-port", 530, True, agent="w")
            store.pause_gateway(conn, "claude-code", time.time() + 600, "gateway 530")
        self.assertNotIn("assigned", self.theseus("queue", "tick").stdout)
        host = self.theseus("top", "--host", "--once").stdout
        self.assertIn("GATEWAY", host)
        self.assertRegex(host, r"claude-code\s+1 error\(s\) in 24 h: 530 .* PAUSED until .*\(gateway 530\)")
        self.assertIn("gateway pool claude-code is paused", self.theseus("top", "--once").stdout)
        with store.opened() as conn, store.transaction(conn):
            conn.execute("UPDATE gateway_pools SET paused_until = ?", (time.time() - 1,))
        self.assertIn("assigned   worker-b", self.theseus("queue", "tick").stdout)

    def test_a_fatal_error_wakes_the_coordinator(self):
        (self.state / "screen-%1").write_text("\n".join(screen(CF_403)) + "\n")
        waited = self.theseus("queue", "wait", "--timeout", "20", "--interval", "1")
        self.assertRegex(waited.stdout, r"error\s+worker-a\s+slow-task gateway 403 fatal")

    def test_instance_send_verifies_and_reports_a_stuck_pane(self):
        sent = self.theseus("instance", "send", "worker-a", "Please continue.")
        self.assertIn("%1: sent", sent.stdout)
        self.write("message.txt", "From a file.\n", root=self.tmp)
        self.assertIn("%1: sent", self.theseus("instance", "send", "worker-a", "--file", self.tmp / "message.txt").stdout)
        typed = (self.state / "typed").read_text()
        self.assertIn("-t %1 -l Please continue.", typed)
        self.assertIn("-t %1 -l From a file.", typed)
        (self.state / "stuck-%1").write_text("")
        stuck = self.theseus("instance", "send", "worker-a", "Nobody reads this.", check=False)
        self.assertEqual(1, stuck.returncode)
        self.assertIn("send_failed", self.theseus("queue", "log").stdout)
        self.assertEqual(2, self.theseus("instance", "send", "nobody", "x", check=False).returncode)
