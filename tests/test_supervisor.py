"""Supervised agents on a real, throwaway private tmux server (one short socket per test, killed afterwards) with a fake
agent CLI: start, reconcile, crash recovery, startup dialogs, drift, scale-down, server loss, verified sends, migration
from hand-made panes and the guard that no supervised path calls tmux without a socket."""
import json
import os
import re
import shutil
import signal
import subprocess
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
backoff_seconds = [0.3, 0.3]
max_crashes = 3
window_minutes = 10
"""

# Test-only poll intervals and settle times; the production defaults are unchanged.
FAST = {"THESEUS_READY_SETTLE": "0.1", "THESEUS_READY_POLL": "0.05", "THESEUS_DEAD_POLL": "0.05",
        "THESEUS_STOP_POLL": "0.05", "THESEUS_STOP_SECONDS": "5", "THESEUS_SEND_VERIFY": "5", "THESEUS_SEND_POLL": "0.05",
        "THESEUS_COMPACT_POLL": "0.1", "FAKE_BUSY": "0.5"}

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


def drain(fd, until=None, timeout=10):
    """Read a pty until until() holds or the other side closes it (until=None waits for the close); returns the
    output and whether the condition was met."""
    import select
    output = b""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if until is not None and until():
            return output, True
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                return output, until is None
            output += chunk
    return output, False


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
                         "LEGACY_SOCKET": str(legacy), "TMUX": "/nonexistent/bogus,1,0", **FAST})
        self.set_config(PROFILE)
        self.git("commit", "-q", "-am", "agents")
        self.socket = self.env["THESEUS_TMUX_PATH"]
        os.environ.update({k: self.env[k] for k in ("PATH", "FAKE_STATE", "LEGACY_SOCKET", "THESEUS_TMUX_PATH", *FAST)})

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
            self.reap()
            time.sleep(0.05)
        self.fail(f"timed out waiting for {message}")

    def reap(self):
        """Signal SIGCHLD to the private server. tmux 3.4 sometimes misses the SIGCHLD of a pane's process (and of its
        own run-shell jobs), leaving it a zombie and pane-died unfired until the next one arrives."""
        pid = self.tmux("display", "-p", "#{pid}").stdout.strip()
        if pid.isdigit():
            try:
                os.kill(int(pid), signal.SIGCHLD)
            except ProcessLookupError:
                pass

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

    def test_up_reconciles_restarts_a_killed_agent_and_names_its_transcript(self):
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
        refused = self.theseus("agents", "remove", "demo-fake-2", check=False)
        self.assertIn("is running", refused.stderr)
        self.theseus("agents", "down", "demo-fake-2")
        self.wait_for(lambda: self.runs("demo-fake-2")[-1].get("ended"), message="demo-fake-2 to stop")
        self.assertIn("removed demo-fake-2", self.theseus("agents", "remove", "demo-fake-2").stdout)
        sessions = self.tmp / "claude/sessions"
        sessions.mkdir(parents=True)
        self.replace_config("status_command = []", f'status_command = []\nsessions_dir = "{sessions}"')
        self.git("commit", "-q", "-am", "sessions dir")
        self.up("demo-fake-1")
        from theseus import agents
        from theseus.adapters import claude_code as transcripts
        rec = agents.get(self.conf(), "demo-fake-1")
        path = transcripts.transcript_path(sessions, rec["session_id"], str(self.project))
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "assistant", "message": {"model": "m", "usage": {"input_tokens": 1234}}}) + "\n")
        self.assertEqual(1234, agents.session_transcript(self.conf(), rec)["tokens"])
        self.assertEqual(str(path), self.theseus("agents", "logs", "demo-fake-1", "--transcript").stdout.strip())
        self.assertEqual(1234, agents.state(self.conf(), rec)["context"]["tokens"])

    def assign(self, task_id, agent="demo-fake-1"):
        self.add_task(task_id)
        from theseus import integrate
        integrate.assign(self.conf(), task_id, f"{agent}-{task_id}", "fake", "persistent-instance",
                         owner=f"fake:{agent}:{agent}-{task_id}", instance=agent)

    def screen(self, app):
        return "\n".join(strip.text for strip in app.screen._compositor.render_strips())

    def add_task(self, task_id):
        self.theseus("tasks", "milestone", "M1")
        self.theseus("tasks", "add", task_id, "--role", "research", "--owned", f"docs/investigations/{task_id}.md",
                     "--objective", "Do it.", "--contract", "All variants.", "--accept", "Done.")
        self.git("commit", "-q", "-am", f"queue {task_id}")


@unittest.skipUnless(REAL_TMUX, "tmux is not installed")


class TmuxSequenceTests(unittest.TestCase):
    """The production sequence on tmux itself: spawn, respawn, attach-size hooks and kill, which must never crash the
    server (a global window-size manual does on tmux 3.4)."""

    @classmethod
    def setUpClass(cls):
        cls.socket = short_dir() / "s"
        cls.addClassCleanup(drop_tmux, cls.socket)

    def test_attach_resizes_to_the_client_and_the_detach_key_restores_the_agent_size(self):
        import pty
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
        state = lambda: server.display("=a--x:", "#{window_width}x#{window_height} #{session_attached}")
        drain(fd, lambda: state() == "100x29 1")
        self.assertEqual("100x29 1", state())
        os.write(fd, b"\x11")
        self.assertTrue(drain(fd)[1])
        self.assertEqual(pid, os.waitpid(pid, 0)[0])
        self.assertEqual("220x50 0", server.display("=a--x:", "#{window_width}x#{window_height} #{session_attached}"))

    def test_the_mirror_size_fits_the_window_except_while_attached_and_the_pinned_size_comes_back(self):
        import fcntl
        import pty
        import struct
        import termios
        from unittest import mock
        from theseus import supervisor, tmux as tm
        server = tm.Server("-S", str(self.socket), conf_file=supervisor.tmux_conf(), env=supervisor.server_env())
        server.new_session("c--x", ["sleep", "60"], "/", 220, 50)
        server.run("resize-window", "-x", "220", "-y", "50", "-t", "=c--x:")
        rec = {"name": "x", "managed": True, "server": f"path:{self.socket}", "target": "c--x"}
        state = lambda: server.display("=c--x:", "#{window_width}x#{window_height} #{session_attached}")
        self.assertTrue(supervisor.fit(rec, 120, 33))
        self.assertEqual("120x33 0", state())
        self.assertEqual("120x33", server.display("=c--x:", "#{pane_width}x#{pane_height}"))
        self.assertFalse(supervisor.fit(rec, 120, 33), "an unchanged size is not sent again")
        self.assertFalse(supervisor.fit(rec, 8, 2))
        self.assertEqual("120x33 0", state())
        argv = supervisor.attach_argv(rec)
        pid, fd = pty.fork()
        if pid == 0:
            os.environ["TERM"] = "xterm-256color"
            os.execvp(argv[0], argv)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        drain(fd, lambda: state() == "100x29 1")
        self.assertEqual("100x29 1", state())
        self.assertFalse(supervisor.fit(rec, 120, 33), "an attached client keeps its size")
        self.assertEqual("100x29 1", state())
        os.write(fd, b"\x11")
        self.assertTrue(drain(fd)[1])
        self.assertEqual(pid, os.waitpid(pid, 0)[0])
        self.assertEqual("220x50 0", state())
        self.assertTrue(supervisor.fit(rec, 120, 33), "the mirror fits the window again after a detach")
        self.assertEqual("120x33 0", state())
        self.assertTrue(supervisor.fit(rec, None, None))
        self.assertEqual("220x50 0", state())
        with mock.patch.object(tm.Server, "run", side_effect=AssertionError("tmux was called")):
            self.assertFalse(supervisor.fit({**rec, "managed": False, "server": None}, 120, 33))
            self.assertFalse(supervisor.fit(None, 120, 33))

    def test_spawn_respawn_and_kill_keep_the_server_alive(self):
        from theseus import supervisor, tmux as tm
        server = tm.Server("-S", str(self.socket), conf_file=supervisor.tmux_conf(), env=supervisor.server_env())
        server.new_session("b--x", ["sleep", "60"], "/", 220, 50)
        server.new_session("b--y", ["sleep", "60"], "/", 220, 50)
        server.run("resize-window", "-x", "220", "-y", "50", "-t", "=b--x:")
        self.assertEqual("220x50", server.display("=b--x:", "#{window_width}x#{window_height}"))
        server.respawn("=b--x:", ["sleep", "61"])
        self.assertIsNotNone(server.display("=b--x:", "#{pane_pid}"))
        server.kill_session("b--x")
        self.assertTrue(server.alive())
        self.assertTrue(server.has_session("b--y"))
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
