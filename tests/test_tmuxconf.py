"""The command that attaches a terminal to a session of the private tmux server, and the status line's cache. No tmux
server runs here: a tmux on PATH refuses every call that names no socket, so nothing can reach the user's server."""
import json
import os
import shutil
import time
import unittest

from helpers import Sandbox

REFUSING_TMUX = """#!/bin/sh
if [ "$1" = -L ] || [ "$1" = -S ]; then exec "{real}" "$@"; fi
echo "test tmux: refused a call without a socket: $*" >&2
exit 1
"""


def refuse_bare_tmux(case, env):
    folder = case.tmp / "bin"
    folder.mkdir(exist_ok=True)
    (folder / "tmux").write_text(REFUSING_TMUX.format(real=shutil.which("tmux") or "/bin/false"))
    (folder / "tmux").chmod(0o755)
    env["PATH"] = f"{folder}{os.pathsep}{env['PATH']}"
    os.environ["PATH"] = env["PATH"]


class AttachTests(unittest.TestCase):
    def test_attach_nests_from_any_terminal_and_switches_inside_the_private_server(self):
        from theseus import supervisor, tmux as tm
        srv = tm.Server("-S", "/tmp/th-x/s")
        nested = supervisor.session_attach_argv(srv, "demo--coordinator", read_only=True,
                                                env={"TMUX": "/tmp/tmux-1000/default,77,0"})
        self.assertEqual(["env", "-u", "TMUX", "tmux", "-S", "/tmp/th-x/s", "attach-session", "-t", "=demo--coordinator",
                          "-r"], nested)
        self.assertEqual(nested[:-1], supervisor.session_attach_argv(srv, "demo--coordinator", env={}))
        private = tm.Server()
        inside = supervisor.session_attach_argv(private, "demo--coordinator",
                                                env={"TMUX": f"{os.environ['THESEUS_TMUX_PATH']},77,0"})
        self.assertEqual(["tmux", "-S", os.environ["THESEUS_TMUX_PATH"], "switch-client", "-t", "=demo--coordinator:"],
                         inside)


class StatusTests(Sandbox):
    def setUp(self):
        super().setUp()
        refuse_bare_tmux(self, self.env)

    def test_tmux_status_reads_the_cache_the_driver_writes(self):
        from theseus import config, tmuxconf
        self.scaffold("demo")
        conf = config.load(root=self.project)
        found = [{"kind": "delivery", "key": "delivery:T-1"}, {"kind": "gateway", "key": "gateway:shared:paused"}]
        states = [{"name": "demo-w-1", "state": "working", "task": "T-1"}, {"name": "demo-w-2", "state": "free"}]
        data = tmuxconf.write_status(conf, found, states)
        self.assertEqual((2, 1, 2, "paused"), (data["attention"], data["working"], data["max"], data["gateway"]))
        path = tmuxconf.status_path("demo")
        right = self.theseus("tmux", "status", "--file", path, "--right").stdout
        self.assertIn("#[bg=#d70000,fg=#ffffff,bold] 2 attention #[default]", right)
        self.assertIn("working 1/2", right)
        self.assertIn("gateway paused", right)
        self.assertNotIn("stale", right)
        self.assertEqual("| T-1", self.theseus("tmux", "status", "--file", path, "--left", "--agent", "demo-w-1").stdout.strip())
        self.assertEqual("", self.theseus("tmux", "status", "--file", path, "--left", "--agent", "demo-w-2").stdout.strip())
        self.assertRegex(self.theseus("tmux", "status").stdout, r"demo: 2 attention, working 1/2, gateway paused; updated")
        self.assertEqual("T-1", json.loads(self.theseus("tmux", "status", "--json").stdout)["tasks"]["demo-w-1"])
        old = {**json.loads(path.read_text()), "attention": 0, "time": time.time() - 3600}
        path.write_text(json.dumps(old))
        stale = tmuxconf.render(path, "right")
        self.assertTrue(stale.startswith("0 attention"))
        self.assertIn("driver stale", stale)
        self.assertIn("no queue driver", tmuxconf.render(self.tmp / "missing.json", "right"))
        path.write_text(json.dumps({**old, "time": time.time(), "attention": 3, "ready": 4,
                                    "kinds": {"delivery": 2, "question": 1}}))
        coordinator = tmuxconf.render(path, "right", kind="coordinator")
        self.assertIn("needs you: 2 deliveries, 1 question", coordinator)
        self.assertIn("ready 4", coordinator)
        self.assertIn("needs you", self.theseus("tmux", "status", "--file", path, "--right", "--kind", "coordinator").stdout)


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class AttachSizeTests(Sandbox):
    """A window pinned to the agent size follows an attached terminal, resizes included, and is pinned again on
    detach: run against the generated configuration on a throwaway server with a real terminal client."""

    def setUp(self):
        super().setUp()
        refuse_bare_tmux(self, self.env)

    def test_an_attached_window_follows_the_terminal_and_is_pinned_again_on_detach(self):
        import fcntl
        import pty
        import select
        import signal
        import struct
        import subprocess
        import termios
        from theseus import supervisor, tmuxconf
        text, _ = tmuxconf.conf_text()
        conf_file = self.tmp / "tmux.conf"
        conf_file.write_text(text)
        socket = self.tmp / "sock"
        width, height = supervisor.size()

        def tmux(*args):
            return subprocess.run(["tmux", "-S", str(socket), *args], capture_output=True, text=True)

        self.addCleanup(tmux, "kill-server")
        tmux("-f", str(conf_file), "new-session", "-d", "-s", "demo--coordinator", "sleep 600")
        tmux("resize-window", "-t", "demo--coordinator", "-x", str(width), "-y", str(height))

        def size():
            return tmux("display", "-p", "-t", "demo--coordinator", "#{window_width}x#{window_height}").stdout.strip()

        pid, fd = pty.fork()
        if pid == 0:
            os.environ.pop("TMUX", None)
            os.environ["TERM"] = "xterm-256color"
            os.execvp("tmux", ["tmux", "-S", str(socket), "attach-session", "-t", "demo--coordinator"])

        def terminal(columns, rows, expect):
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
            os.kill(pid, signal.SIGWINCH)
            deadline = time.time() + 5
            while time.time() < deadline and size() != expect:
                if select.select([fd], [], [], 0.1)[0]:
                    try:
                        os.read(fd, 65536)
                    except OSError:
                        break
            return size()

        try:
            self.assertEqual("120x39", terminal(120, 40, "120x39"), "the window takes the terminal's size")
            self.assertEqual("160x44", terminal(160, 45, "160x44"), "and follows it when it is resized")
        finally:
            tmux("detach-client", "-a")
            tmux("detach-client")
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        deadline = time.time() + 5
        while time.time() < deadline and size() != f"{width}x{height}":
            time.sleep(0.1)
        self.assertEqual(f"{width}x{height}", size(), "detach pins the agent size again")


if __name__ == "__main__":
    unittest.main()
