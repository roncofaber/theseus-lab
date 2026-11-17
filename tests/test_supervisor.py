"""Supervised agents on a real, throwaway private tmux server (one short socket per test, killed afterwards) with a fake
agent CLI: start, reconcile, crash recovery, startup dialogs, drift, scale-down, server loss, verified sends, migration
from hand-made panes and the guard that no supervised path calls tmux without a socket."""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from helpers import Sandbox, drop_tmux, short_dir

REAL_TMUX = shutil.which("tmux")

FAKE_AGENT = r"""#!/usr/bin/env bash
# A stand-in for an interactive agent CLI: a banner, a "> " prompt, the busy marker while it works on a line, a
# handoff for a task message, /exit, and modes read from $FAKE_STATE/mode-<name> (normal, trust, crash, exit-clean,
# resume-missing).
name="" session="" resume=""
while [ $# -gt 0 ]; do
    case "$1" in
        --name) name=$2; shift ;;
        --session-id) session=$2; shift ;;
        --resume) resume=$2; shift ;;
    esac
    shift
done
mode=$(cat "$FAKE_STATE/mode-$name" 2>/dev/null || echo normal)
echo "start name=$name session=$session resume=$resume pid=$$ secret=${FAKE_SECRET:-unset} tmux=${TMUX:+set}" >> "$FAKE_STATE/agent.log"
case "$mode" in
    trust)
        echo "Quick safety check: Is this a project you created or one you trust?"
        echo " > 1. Yes, I trust this folder"
        while IFS= read -r answer; do echo "answer $name: $answer" >> "$FAKE_STATE/agent.log"; done
        exit 0 ;;
    crash) echo "fake agent $name crashing"; sleep 0.3; exit 3 ;;
    exit-clean) exit 0 ;;
    resume-missing)
        if [ -n "$resume" ]; then echo "No conversation found with session ID: $resume"; exit 1; fi ;;
esac
echo "fake agent $name ready"
while true; do
    printf '> '
    IFS= read -r line || exit 0
    echo "input $name: $line" >> "$FAKE_STATE/agent.log"
    case "$line" in
        /exit) echo bye; exit 0 ;;
        /*) echo "ran $line" ;;
        *thrash*) echo "Autocompact is thrashing: the context refills right after compacting" ;;
        *)
            echo "working (esc to interrupt)"
            log=$(printf '%s' "$line" | sed -n 's/.*Log directory (handoff.md, changed.sha256): \([^ ]*\)\. .*/\1/p')
            if [ -n "$log" ]; then mkdir -p "$log"; printf 'Task: x\nResult: delivered\n' > "$log/handoff.md"; touch -d '-2 minutes' "$log/handoff.md"; fi
            sleep "${FAKE_BUSY:-1}"
            printf '\033[1A\033[2K'
            echo "done" ;;
    esac
done
"""

FAKE_STATUS = r"""#!/usr/bin/env python3
# A stand-in for `claude agents --json`: one entry per running fake agent, with status, waitingFor and sessionId
# overridable through $FAKE_STATE/{status,waiting,session,hidden}-<name>.
import glob, json, os
state = os.environ["FAKE_STATE"]
found = []
for folder in glob.glob("/proc/[0-9]*"):
    try:
        args = [a.decode(errors="replace") for a in open(folder + "/cmdline", "rb").read().split(b"\0") if a]
    except OSError:
        continue
    if len(args) < 2 or not args[1].endswith("fake-agent"):
        continue
    rest = args[2:]
    def arg(flag):
        return rest[rest.index(flag) + 1] if flag in rest else ""
    name = arg("--name")
    def read(key, default=None):
        path = os.path.join(state, f"{key}-{name}")
        return open(path).read().strip() if os.path.exists(path) else default
    if read("hidden"):
        continue
    found.append({"pid": int(folder[6:]), "name": name, "sessionId": read("session", arg("--session-id") or arg("--resume")),
                  "status": read("status", "idle"), "waitingFor": read("waiting")})
print(json.dumps(found))
"""

WRAPPER = """#!/usr/bin/env bash
# Real tmux for calls that name a socket; a call without one is logged and sent to a throwaway "user" server.
if [ "$1" = -L ] || [ "$1" = -S ]; then exec "{real}" "$@"; fi
printf '%s\\n' "$*" >> "$FAKE_STATE/legacy-calls"
exec "{real}" -S "$LEGACY_SOCKET" "$@"
"""

HOST = """
[supervisor]
env_passthrough = ["FAKE_*"]
ready_seconds = 15

[supervisor.restart]
backoff_seconds = [1, 1]
max_crashes = 3
window_minutes = 10
"""

PROFILE = """
[workers.fake]
preset = "claude-code"
command = "fake-agent"
capacity = 3
env = { FAKE_SECRET = "s3cret-value" }

[workers.fake.interactive]
status_command = []

[agents]
managed = true

[agents.counts]
fake = 2
"""


@unittest.skipUnless(REAL_TMUX, "tmux is not installed")
class SupervisorTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold("demo")
        self.state = self.tmp / "state"
        self.state.mkdir()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "fake-agent").write_text(FAKE_AGENT)
        (bin_dir / "fake-agent").chmod(0o755)
        (bin_dir / "fake-status").write_text(FAKE_STATUS)
        (bin_dir / "fake-status").chmod(0o755)
        (bin_dir / "tmux").write_text(WRAPPER.format(real=REAL_TMUX))
        (bin_dir / "tmux").chmod(0o755)
        legacy = short_dir() / "u"
        self.addCleanup(drop_tmux, legacy)
        self.legacy = legacy
        (self.tmp / "config.toml").write_text(HOST)
        self.env.update({"PATH": f"{bin_dir}{os.pathsep}{self.env['PATH']}", "FAKE_STATE": str(self.state),
                         "LEGACY_SOCKET": str(legacy), "THESEUS_READY_SETTLE": "0.5", "THESEUS_STOP_POLL": "0.2",
                         "THESEUS_STOP_SECONDS": "5", "THESEUS_SEND_VERIFY": "5", "THESEUS_SEND_POLL": "0.2",
                         "THESEUS_COMPACT_POLL": "0.2", "TMUX": "/nonexistent/bogus,1,0"})
        self.set_config(PROFILE)
        self.git("commit", "-q", "-am", "agents")
        self.socket = self.env["THESEUS_TMUX_PATH"]
        os.environ.update({k: self.env[k] for k in ("PATH", "FAKE_STATE", "LEGACY_SOCKET", "THESEUS_TMUX_PATH",
                                                      "THESEUS_READY_SETTLE", "THESEUS_STOP_POLL", "THESEUS_STOP_SECONDS",
                                                      "THESEUS_SEND_VERIFY", "THESEUS_SEND_POLL")})

    def tmux(self, *args):
        return subprocess.run([REAL_TMUX, "-S", self.socket, *args], capture_output=True, text=True)

    def conf(self):
        from theseus import config
        return config.load(root=self.project)

    def log(self):
        path = self.state / "agent.log"
        return path.read_text() if path.exists() else ""

    def wait_for(self, check, timeout=15, message="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.2)
        self.fail(f"timed out waiting for {message}")

    def runs(self, name):
        from theseus import store
        with store.opened() as conn:
            return store.runs(conn, "demo", name)

    def events(self, kind=None):
        from theseus import store
        with store.opened() as conn:
            return [e for e in store.events(conn, "demo") if kind is None or e["kind"] == kind]

    def legacy_calls(self):
        path = self.state / "legacy-calls"
        return path.read_text() if path.exists() else ""

    def up(self, *names, check=True):
        return self.theseus("agents", "up", *names, check=check, timeout=60)

    def test_up_starts_each_slot_once_and_a_second_up_keeps_them(self):
        out = self.up().stdout
        self.assertRegex(out, r"demo-fake-1\s+started\s+fresh session; ready")
        self.assertRegex(out, r"demo-fake-2\s+started")
        again = self.up().stdout
        self.assertEqual(2, again.count("kept"), again)
        self.assertNotIn("started", again)
        starts = [line for line in self.log().splitlines() if line.startswith("start ")]
        self.assertEqual(2, len(starts))
        self.assertRegex(starts[0], r"name=demo-fake-1 session=[0-9a-f-]{36} resume= pid=\d+ secret=s3cret-value tmux=set")
        sessions = self.tmux("list-sessions", "-F", "#{session_name}").stdout.split()
        self.assertEqual(["demo--demo-fake-1", "demo--demo-fake-2"], sorted(sessions))
        start_command = self.tmux("display", "-p", "-t", "=demo--demo-fake-1:", "#{pane_start_command}").stdout
        self.assertIn("agents _exec demo demo-fake-1 1", start_command)
        self.assertNotIn("s3cret", start_command)
        environment = self.tmux("show-environment", "-g").stdout + self.tmux("show-environment", "-t", "=demo--demo-fake-1").stdout
        self.assertNotIn("s3cret", environment)
        runs_log = (self.tmp / "data/agents/demo-fake-1/runs.log").read_text()
        self.assertIn("env FAKE_SECRET", runs_log)
        self.assertNotIn("s3cret", runs_log)
        self.assertEqual(0o600, (self.tmp / "data/agents/demo-fake-1/runs.log").stat().st_mode & 0o777)
        listed = self.theseus("agents", "ls").stdout
        self.assertRegex(listed, r"demo-fake-1\s+free\s+fake\s+gen 1")
        self.assertRegex(self.theseus("agents").stdout, r"demo-fake-1\s+=demo--demo-fake-1:\s+free")
        self.assertEqual("", self.legacy_calls())

    def test_a_killed_agent_is_restarted_alone_with_its_exit_recorded(self):
        self.up()
        pid = int(self.tmux("display", "-p", "-t", "=demo--demo-fake-1:", "#{pane_pid}").stdout)
        os.kill(pid, 9)
        self.wait_for(lambda: self.runs("demo-fake-1")[-1].get("ended"), message="the pane-died hook to close the run")
        crashed = self.runs("demo-fake-1")[-1]
        self.assertEqual("crash", crashed["stop_reason"])
        self.assertTrue(crashed["exit_signal"] or crashed["exit_status"] is not None)
        self.wait_for(lambda: len(self.runs("demo-fake-1")) == 2, message="the hook to restart the agent")
        self.assertEqual(1, len(self.runs("demo-fake-2")))
        out = self.up().stdout
        self.assertRegex(out, r"demo-fake-1\s+kept")
        self.assertTrue((self.tmp / "data/agents/demo-fake-1/crash-1.txt").exists())
        self.assertTrue(self.events("exited"))

    def test_a_crash_loop_gives_up_after_max_crashes_with_one_error(self):
        (self.state / "mode-demo-fake-1").write_text("crash")
        self.up("demo-fake-1", check=False)
        self.wait_for(lambda: (self.record("demo-fake-1") or {}).get("phase") == "failed", timeout=30,
                      message="the agent to be marked failed")
        errors = [e for e in self.events("error") if "crashed 3 times" in e["detail"]]
        self.assertEqual(1, len(errors))
        self.assertRegex(self.up("demo-fake-1").stdout, r"demo-fake-1\s+failed")
        (self.state / "mode-demo-fake-1").write_text("normal")
        self.assertRegex(self.theseus("agents", "restart", "demo-fake-1", timeout=60).stdout, r"restarted")
        self.assertEqual("ready", self.record("demo-fake-1")["phase"])

    def record(self, name):
        from theseus import store
        with store.opened() as conn:
            return next((a for a in store.list_agents(conn, "demo") if a["name"] == name), None)

    def test_a_startup_dialog_blocks_the_agent_and_is_never_answered(self):
        (self.state / "mode-demo-fake-1").write_text("trust")
        out = self.up("demo-fake-1").stdout
        self.assertRegex(out, r"demo-fake-1\s+started\s+fresh session; blocked")
        self.assertTrue(any("startup dialog" in e["detail"] for e in self.events("error")))
        self.assertNotIn("answer demo-fake-1", self.log())
        self.assertRegex(self.theseus("agents").stdout, r"demo-fake-1\s+\S+\s+blocked")
        self.assertRegex(self.theseus("agents", "down", "demo-fake-1", timeout=60).stdout, r"demo-fake-1\s+stopped")
        self.assertNotIn("answer demo-fake-1", self.log())

    def test_config_drift_is_reported_not_applied(self):
        self.up()
        self.replace_config('env = { FAKE_SECRET = "s3cret-value" }', 'env = { FAKE_SECRET = "s3cret-value", FAKE_MORE = "1" }')
        out = self.up().stdout
        self.assertRegex(out, r"demo-fake-1\s+drift")
        self.assertEqual(1, len(self.runs("demo-fake-1")))
        self.assertEqual(2, len(self.events("drift")))
        self.up()
        self.assertEqual(2, len(self.events("drift")))

    def test_lowering_a_count_stops_a_free_agent(self):
        self.up()
        self.replace_config("fake = 2", "fake = 1")
        out = self.up().stdout
        self.assertRegex(out, r"demo-fake-2\s+(removed|stopping)")
        self.wait_for(lambda: self.tmux("has-session", "-t", "=demo--demo-fake-2").returncode != 0 or
                      self.up() and False, message="the agent to exit")
        self.up()
        self.assertIsNone(self.record("demo-fake-2"))
        self.assertIn("input demo-fake-2: /exit", self.log())

    def test_down_keeps_an_agent_stopped_until_up_names_it(self):
        self.up()
        self.assertRegex(self.theseus("agents", "down", "demo-fake-2", timeout=60).stdout, r"demo-fake-2\s+stopped")
        self.assertIn("input demo-fake-2: /exit", self.log())
        self.assertNotEqual(0, self.tmux("has-session", "-t", "=demo--demo-fake-2").returncode)
        self.assertRegex(self.up().stdout, r"demo-fake-2\s+down")
        self.assertRegex(self.up("demo-fake-2").stdout, r"demo-fake-2\s+(started|restarted)")

    def test_a_lost_server_resumes_only_the_agent_that_holds_a_task(self):
        self.up()
        first = self.record("demo-fake-1")["session_id"]
        self.add_task("t-one")
        from theseus import integrate
        integrate.assign(self.conf(), "t-one", "demo-fake-1-t-one", "fake", "persistent-instance",
                         owner="fake:demo-fake-1:demo-fake-1-t-one", instance="demo-fake-1")
        self.tmux("kill-server")
        out = self.up().stdout
        self.assertRegex(out, rf"demo-fake-1\s+restarted\s+resuming {first}")
        self.assertRegex(out, r"demo-fake-2\s+restarted\s+fresh session")
        self.assertIn(f"resume={first}", self.log().splitlines()[-1] + self.log())
        self.assertEqual("server-lost", self.runs("demo-fake-1")[1]["stop_reason"])

    def use_status(self):
        self.replace_config("status_command = []", 'status_command = ["fake-status"]')
        self.git("commit", "-q", "-am", "status command")

    def assign(self, task_id, agent="demo-fake-1"):
        self.add_task(task_id)
        from theseus import integrate
        integrate.assign(self.conf(), task_id, f"{agent}-{task_id}", "fake", "persistent-instance",
                         owner=f"fake:{agent}:{agent}-{task_id}", instance=agent)

    def test_the_status_command_marks_ready_and_its_session_changes_are_stored(self):
        self.use_status()
        self.assertRegex(self.up().stdout, r"demo-fake-1\s+started\s+fresh session; ready")
        fresh = "99999999-8888-7777-6666-555555555555"
        (self.state / "session-demo-fake-1").write_text(fresh)
        self.theseus("queue", "tick", "--no-assign", timeout=60)
        self.assertEqual(fresh, self.record("demo-fake-1")["session_id"])

    def test_an_agent_missing_from_the_status_list_times_out_as_blocked(self):
        self.use_status()
        (self.tmp / "config.toml").write_text(HOST.replace("ready_seconds = 15", "ready_seconds = 2"))
        (self.state / "hidden-demo-fake-1").write_text("1")
        self.assertRegex(self.up("demo-fake-1").stdout, r"demo-fake-1\s+started\s+fresh session; blocked")
        self.assertTrue(any("not ready after 2s" in e["detail"] for e in self.events("error")))

    def test_waiting_for_input_with_a_task_raises_one_stalled_event(self):
        self.use_status()
        self.up()
        self.assign("t-wait")
        (self.state / "status-demo-fake-1").write_text("waiting")
        (self.state / "waiting-demo-fake-1").write_text("permission prompt")
        self.theseus("queue", "tick", timeout=60)
        self.theseus("queue", "tick", timeout=60)
        stalled = [e for e in self.events("stalled") if "waiting for permission prompt" in e["detail"]]
        self.assertEqual(1, len(stalled))
        self.assertRegex(self.theseus("agents").stdout, r"demo-fake-1\s+\S+\s+waiting")

    def test_the_queue_relaunches_a_supervised_agent_onto_a_compatible_profile(self):
        self.set_config('\n[workers.fake2]\npreset = "claude-code"\ncommand = "fake-agent"\ncapacity = 3\n'
                        'env = { FAKE_SECRET = "other-value" }\n\n[workers.fake2.interactive]\nstatus_command = []\n')
        self.git("commit", "-q", "-am", "fake2")
        self.up()
        session = self.record("demo-fake-1")["session_id"]
        self.add_task("t-rea")
        self.theseus("tasks", "profile", "t-rea", "fake2")
        self.git("commit", "-q", "-am", "profile")
        out = self.theseus("queue", "tick", timeout=120).stdout
        self.assertIn("relaunched", out)
        self.assertIn("assigned", out)
        self.assertRegex(self.log(), rf"start name=demo-fake-1 session= resume={session} pid=\d+ secret=other-value")
        self.assertEqual("fake2", self.runs("demo-fake-1")[0]["profile"])
        self.assertIn("input demo-fake-1: /exit", self.log())

    def test_a_thrashing_agent_restarts_fresh_and_gets_its_restart_prompt(self):
        self.up()
        self.assign("t-thrash")
        from theseus import supervisor
        supervisor.send(self.conf(), "demo-fake-1", "please thrash", expect_busy=False)
        out = self.theseus("queue", "tick", "--no-assign", timeout=60).stdout
        self.assertRegex(out, r"restarted\s+demo-fake-1\s+t-thrash thrashing: restarted on a fresh session")
        self.wait_for(lambda: self.theseus("queue", "tick", "--no-assign", timeout=60) and
                      "Your context was cleared" in self.log(), timeout=30, message="the restart prompt")
        starts = [line for line in self.log().splitlines() if line.startswith("start name=demo-fake-1")]
        self.assertEqual(2, len(starts))
        self.assertIn("resume= ", starts[-1])

    def test_the_transcript_of_a_supervised_agent_comes_from_its_stored_session_id(self):
        sessions = self.tmp / "claude/sessions"
        sessions.mkdir(parents=True)
        self.replace_config("status_command = []", f'status_command = []\nsessions_dir = "{sessions}"')
        self.git("commit", "-q", "-am", "sessions dir")
        self.up("demo-fake-1")
        from theseus import agents, transcripts
        rec = agents.get(self.conf(), "demo-fake-1")
        path = transcripts.transcript_path(sessions, rec["session_id"], str(self.project))
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "assistant", "message": {"model": "m", "usage": {"input_tokens": 1234}}}) + "\n")
        self.assertEqual(1234, agents.session_transcript(self.conf(), rec)["tokens"])
        self.assertEqual(1234, agents.state(self.conf(), rec)["context"]["tokens"])

    def test_scale_writes_the_local_count_and_reconciles(self):
        self.up()
        out = self.theseus("agents", "scale", "fake", "3", timeout=60).stdout
        self.assertIn("[agents.counts] fake = 3 in theseus.local.toml", out)
        self.assertRegex(out, r"demo-fake-3\s+started")
        self.assertIn("[agents.counts]\nfake = 3", (self.project / "theseus.local.toml").read_text())
        self.theseus("agents", "scale", "fake", "2", timeout=60)
        self.assertIn("fake = 2", (self.project / "theseus.local.toml").read_text())
        self.assertEqual(1, (self.project / "theseus.local.toml").read_text().count("fake ="))

    def test_scale_refuses_counts_written_inline(self):
        self.up()
        (self.project / "theseus.local.toml").write_text("[agents]\ncounts = { fake = 2 }\n")
        result = self.theseus("agents", "scale", "fake", "3", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("by hand", result.stderr)
        self.assertEqual("[agents]\ncounts = { fake = 2 }\n", (self.project / "theseus.local.toml").read_text())

    def test_attach_from_the_console_returns_on_the_detach_key(self):
        import pty
        import select
        self.up("demo-fake-1")
        code = ("import sys; from theseus.tui.app import attach_supervised; "
                "print('result', repr(attach_supervised(sys.argv[1], sys.argv[2])))")
        pid, fd = pty.fork()
        if pid == 0:
            os.environ.clear()
            os.environ.update({**self.env, "TERM": "xterm-256color"})
            os.execvp(sys.executable, [sys.executable, "-c", code, str(self.project), "demo-fake-1"])
        output = b""

        def drain(seconds):
            nonlocal output
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                ready, _, _ = select.select([fd], [], [], 0.1)
                if ready:
                    try:
                        output += os.read(fd, 65536)
                    except OSError:
                        return
        drain(2.0)
        self.assertEqual("1", self.tmux("display", "-p", "-t", "=demo--demo-fake-1:", "#{session_attached}").stdout.strip())
        os.write(fd, b"\x11")
        drain(2.0)
        self.assertEqual(pid, os.waitpid(pid, 0)[0])
        self.assertIn(b"result ''", output)
        self.assertEqual("", self.legacy_calls())

    def test_the_console_mirrors_and_restarts_a_supervised_agent(self):
        import asyncio
        self.up()
        from theseus.tui.app import TopApp
        from theseus.tui.popups import AgentScreen, Confirm
        os.environ["PYTHONPATH"] = self.env["PYTHONPATH"]

        async def drive():
            app = TopApp(interval=3600, roots=[str(self.project)], current=str(self.project), color_system="truecolor",
                         mirror_seconds=0.1)
            async with app.run_test(size=(170, 60)) as pilot:
                async def until(check, seconds=20):
                    for _ in range(int(seconds / 0.1)):
                        await pilot.pause(0.1)
                        if check():
                            return
                    self.fail("console did not settle")
                await until(lambda: app.active_item() is not None and "fake-1" in self.screen(app))
                await pilot.press("a", "enter")
                await until(lambda: isinstance(app.screen, AgentScreen))
                popup = app.screen
                await until(lambda: "fake agent demo-fake-1 ready" in popup.query_one("#mirror-text").render().plain)
                await pilot.click("#send")
                await pilot.press(*"hi there", "enter")
                await until(lambda: "input demo-fake-1: hi there" in self.log())
                await pilot.press("escape")
                await until(lambda: len(app.screen_stack) == 1)
                await pilot.press("a", "enter")
                await until(lambda: isinstance(app.screen, AgentScreen))
                await pilot.press("R")
                await until(lambda: isinstance(app.screen, Confirm))
                await pilot.press("y")
                await until(lambda: len(self.runs("demo-fake-1")) == 2, 60)
        asyncio.run(drive())
        self.assertEqual("", self.legacy_calls())

    def screen(self, app):
        return "\n".join(strip.text for strip in app.screen._compositor.render_strips())

    def add_task(self, task_id):
        self.theseus("tasks", "milestone", "M1")
        self.theseus("tasks", "add", task_id, "--role", "research", "--owned", f"docs/investigations/{task_id}.md",
                     "--objective", "Do it.", "--contract", "All variants.", "--accept", "Done.")
        self.git("commit", "-q", "-am", f"queue {task_id}")

    def test_a_missing_conversation_restarts_fresh(self):
        self.up()
        self.add_task("t-two")
        from theseus import integrate
        integrate.assign(self.conf(), "t-two", "demo-fake-1-t-two", "fake", "persistent-instance",
                         owner="fake:demo-fake-1:demo-fake-1-t-two", instance="demo-fake-1")
        (self.state / "mode-demo-fake-1").write_text("resume-missing")
        self.tmux("kill-server")
        out = self.up("demo-fake-1").stdout
        self.assertIn("restarted fresh", out)
        state = json.loads((self.tmp / "data/queue/agents-state.json").read_text())
        self.assertEqual("t-two", state["demo-fake-1"]["restart_prompt"])

    def test_verified_send_reaches_a_supervised_agent(self):
        self.up()
        from theseus import supervisor
        ok, detail = supervisor.send(self.conf(), "demo-fake-1", "hello there")
        self.assertTrue(ok, detail)
        self.assertIn("input demo-fake-1: hello there", self.log())
        screen = supervisor.capture(supervisor.record(self.conf(), "demo-fake-1"), ansi=False)
        self.assertIn("fake agent demo-fake-1 ready", screen)
        names = [item["name"] for item in supervisor.snapshot(self.conf())]
        self.assertEqual(["demo-fake-1", "demo-fake-2"], names)
        from theseus import top
        self.assertIn("ready", top.capture("=demo--demo-fake-1:"))
        self.assertEqual("", top.send_keys("=demo--demo-fake-1:", "Escape"))
        top.agent_processes([{"name": "demo-fake-1", "pane": "=demo--demo-fake-1:"}])
        self.assertEqual("", self.legacy_calls())

    def test_a_queue_cycle_on_supervised_agents(self):
        self.up()
        self.add_task("t-cycle")
        out = self.theseus("queue", "tick", timeout=60).stdout
        self.assertIn("assigned", out)
        self.wait_for(lambda: "New task t-cycle" in self.log(), message="the task message")
        time.sleep(1.5)
        out = self.theseus("queue", "tick", timeout=60).stdout
        self.assertIn("delivered", out)
        self.assertEqual("", self.legacy_calls())

    def test_the_hand_made_pane_migrates_with_its_conversation(self):
        self.tmux("kill-server")
        session = "11111111-2222-3333-4444-555555555555"
        sessions = self.tmp / "sessions"
        sessions.mkdir()
        env = {**self.env}
        subprocess.run([REAL_TMUX, "-S", str(self.legacy), "-f", "/dev/null", "new-session", "-d", "-s", "user", "-x", "200",
                        "-y", "50", "bash", "--norc", "--noprofile"], env=env, check=True)
        subprocess.run([REAL_TMUX, "-S", str(self.legacy), "send-keys", "-t", "user:", "-l",
                        f"fake-agent --name demo-fake-1 --session-id {session}"], check=True)
        subprocess.run([REAL_TMUX, "-S", str(self.legacy), "send-keys", "-t", "user:", "Enter"], check=True)
        self.wait_for(lambda: "start name=demo-fake-1" in self.log(), message="the hand-made agent")
        pid = int(re.search(r"start name=demo-fake-1 .* pid=(\d+)", self.log()).group(1))
        (sessions / f"{pid}.json").write_text(json.dumps({"sessionId": session, "name": "demo-fake-1"}))
        pane = subprocess.run([REAL_TMUX, "-S", str(self.legacy), "display", "-p", "-t", "user:", "#{pane_id}"],
                              capture_output=True, text=True).stdout.strip()
        self.replace_config("status_command = []", f'status_command = []\nsessions_dir = "{sessions}"')
        self.replace_config("panes = []", f'panes = [{{ name = "demo-fake-1", pane = "{pane}" }}]')
        self.replace_config('# default = "claude-code"', 'default = "fake"')
        self.git("commit", "-q", "-am", "hand-made pane")
        self.assertRegex(self.theseus("agents", "up", "--dry-run").stdout, r"demo-fake-1\s+legacy")
        out = self.theseus("agents", "migrate", "demo-fake-1", timeout=90).stdout
        self.assertRegex(out, rf"demo-fake-1\s+migrated\s+resumed {session}")
        self.assertIn(f"resume={session}", self.log())
        self.assertTrue(self.record("demo-fake-1")["managed"])
        self.assertIn("input demo-fake-1: /exit", self.log())
        self.assertTrue(self.tmux("has-session", "-t", "=demo--demo-fake-1").returncode == 0)


@unittest.skipUnless(REAL_TMUX, "tmux is not installed")
class TmuxSequenceTests(unittest.TestCase):
    """The production sequence on tmux itself: spawn, respawn, attach-size hooks and kill, which must never crash the
    server (a global window-size manual does on tmux 3.4)."""

    def setUp(self):
        self.socket = short_dir() / "s"
        self.addCleanup(drop_tmux, self.socket)

    def test_attach_resizes_to_the_client_and_the_detach_key_restores_the_agent_size(self):
        import pty
        import select
        import struct
        import fcntl
        import termios
        from theseus import supervisor, tmux as tm
        server = tm.Server("-S", str(self.socket), conf_file=supervisor.tmux_conf(), env=supervisor.server_env())
        server.new_session("a--x", ["sleep", "60"], "/", 220, 50)
        server.run("resize-window", "-x", "220", "-y", "50", "-t", "=a--x:")
        argv = supervisor.attach_argv({"name": "x", "managed": True, "server": f"path:{self.socket}", "target": "a--x"})
        pid, fd = pty.fork()
        if pid == 0:
            os.environ["TERM"] = "xterm-256color"
            os.execvp(argv[0], argv)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))

        def drain(seconds):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                ready, _, _ = select.select([fd], [], [], 0.1)
                if ready:
                    try:
                        os.read(fd, 65536)
                    except OSError:
                        return
        drain(1.5)
        self.assertEqual("100x29 1", server.display("=a--x:", "#{window_width}x#{window_height} #{session_attached}"))
        os.write(fd, b"\x11")
        drain(1.0)
        self.assertEqual(pid, os.waitpid(pid, 0)[0])
        self.assertEqual("220x50 0", server.display("=a--x:", "#{window_width}x#{window_height} #{session_attached}"))

    def test_spawn_respawn_and_kill_keep_the_server_alive(self):
        from theseus import supervisor, tmux as tm
        server = tm.Server("-S", str(self.socket), conf_file=supervisor.tmux_conf(), env=supervisor.server_env())
        server.new_session("a--x", ["sleep", "60"], "/", 220, 50)
        server.new_session("a--y", ["sleep", "60"], "/", 220, 50)
        server.run("resize-window", "-x", "220", "-y", "50", "-t", "=a--x:")
        self.assertEqual("220x50", server.display("=a--x:", "#{window_width}x#{window_height}"))
        server.respawn("=a--x:", ["sleep", "61"])
        self.assertIsNotNone(server.display("=a--x:", "#{pane_pid}"))
        server.kill_session("a--x")
        self.assertTrue(server.alive())
        self.assertTrue(server.has_session("a--y"))
        self.assertEqual("on", server.run("show-options", "-gv", "remain-on-exit").stdout.strip())


class ConfigAndEntryTests(Sandbox):
    def test_config_check_warns_about_pane_ids_in_the_tracked_file(self):
        self.scaffold("demo")
        self.replace_config("panes = []", 'panes = [{ name = "w-1", pane = "%3" }]')
        out = self.theseus("config", "check").stdout
        self.assertIn("[instances] panes lists tmux pane ids", out)
        self.replace_config('panes = [{ name = "w-1", pane = "%3" }]', "panes = []")
        (self.project / "theseus.local.toml").write_text('[instances]\npanes = [{ name = "w-1", pane = "%3" }]\n')
        self.assertNotIn("pane ids", self.theseus("config", "check").stdout)

    def test_theseus_alone_on_a_terminal_opens_the_console(self):
        from unittest import mock
        from theseus import cli
        opened = []
        with mock.patch.object(cli, "cmd_top", lambda args: opened.append(args.command)), \
                mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True):
            cli.main([])
        self.assertEqual(["console"], opened)


class SeamTests(unittest.TestCase):
    def test_every_private_argv_names_its_socket_and_legacy_argv_is_plain(self):
        from theseus import tmux as tm
        self.assertEqual(["tmux", "-S", "/x/s", "list-panes"], tm.Server("-S", "/x/s").argv("list-panes"))
        self.assertEqual(["tmux", "-L", "other", "-f", "/c", "ls"], tm.Server("-L", "other", conf_file="/c").argv("ls"))
        self.assertEqual(["tmux", "display", "-p"], tm.Server.legacy().argv("display", "-p"))

    def test_the_default_private_socket_is_refused_under_the_test_guard(self):
        from theseus import tmux as tm
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ.pop("THESEUS_TMUX_PATH", None)
        os.environ.pop("THESEUS_TMUX_SOCKET", None)
        os.environ["THESEUS_TMUX_GUARD"] = "1"
        with self.assertRaises(tm.TmuxError):
            tm.Server()

    def test_a_private_tmux_variable_is_dropped_for_legacy_calls(self):
        from theseus import tmux as tm
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ["TMUX"] = f"{os.environ['THESEUS_TMUX_PATH']},123,0"
        self.assertNotIn("TMUX", tm.Server.legacy().child_env())
        os.environ["TMUX"] = "/tmp/tmux-1000/default,123,0"
        self.assertEqual("/tmp/tmux-1000/default,123,0", tm.Server.legacy().child_env()["TMUX"])
        self.assertNotIn("TMUX", tm.Server("-S", "/x/s").child_env())

    def test_only_the_seam_builds_tmux_commands(self):
        source = Path(__file__).resolve().parents[1] / "src/theseus"
        offenders = []
        for path in source.rglob("*.py"):
            if path.name == "tmux.py":
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"""\[\s*["']tmux["']""", line):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual([], offenders)
        for path in (source / "scripts").glob("*.sh"):
            if path.name in ("instance-restart.sh",):
                continue
            bare = [line for line in path.read_text().splitlines()
                    if re.search(r"(^|[;&|(]\s*|\s)tmux\s+(capture-pane|send-keys|display|list|new|kill)", line)
                    and "tmux_at" not in line and "tmux \"$@\"" not in line and not line.lstrip().startswith("#")]
            self.assertEqual([], bare, path.name)


if __name__ == "__main__":
    unittest.main()
