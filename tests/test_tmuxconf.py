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


if __name__ == "__main__":
    unittest.main()
