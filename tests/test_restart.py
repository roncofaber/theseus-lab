"""theseus instance restart with stand-ins: a fake tmux whose pane shell is a small loop process, and a fake agent
named claude that writes a per-process session record, so the real panes and sessions are never touched."""
import json
import os
import signal
import subprocess
import time
import unittest

from helpers import Sandbox

FAKE_AGENT = """#!/usr/bin/env bash
session=${2:-first-session}
printf '{"pid": %s, "sessionId": "%s", "name": "worker-x"}' "$$" "$session" > "$FAKE_SESSIONS/$$.json"
trap 'exit 0' TERM
while :; do sleep 0.1; done
"""

FAKE_SHELL = """#!/usr/bin/env bash
cd "$FAKE_STATE"
while :; do
    if [ -f command ]; then read -ra c < command; rm -f command; "${c[@]}" & fi
    sleep 0.1
done
"""

FAKE_TMUX = """#!/usr/bin/env bash
case "$1" in
    display) cat "$FAKE_STATE/shell.pid" ;;
    capture-pane) echo "> " ;;
    send-keys)
        if [ "$4" = "-l" ]; then printf '%s' "$5" > "$FAKE_STATE/buffer"; exit 0; fi
        if [ "$4" = "Enter" ]; then
            b=$(cat "$FAKE_STATE/buffer")
            if [ "$b" = "/exit" ]; then for p in $(pgrep -P "$(cat "$FAKE_STATE/shell.pid")"); do [ -f "$FAKE_SESSIONS/$p.json" ] && kill -TERM "$p"; done; else printf '%s' "$b" > "$FAKE_STATE/command"; fi
        fi ;;
esac
"""


class RestartTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        self.state = self.tmp / "state"
        self.sessions = self.tmp / "sessions"
        bin_dir = self.tmp / "bin"
        for directory in (self.state, self.sessions, bin_dir):
            directory.mkdir()
        for name, body in (("claude", FAKE_AGENT), ("fake-shell", FAKE_SHELL), ("tmux", FAKE_TMUX)):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.env.update({"PATH": f"{bin_dir}{os.pathsep}{self.env['PATH']}", "FAKE_STATE": str(self.state),
                         "FAKE_SESSIONS": str(self.sessions), "THESEUS_AGENT_SESSIONS": str(self.sessions),
                         "THESEUS_AGENT_LAUNCH": str(bin_dir / "claude")})
        self.shell = subprocess.Popen([str(bin_dir / "fake-shell")], env=self.env, start_new_session=True)
        self.addCleanup(self.stop_shell)
        (self.state / "shell.pid").write_text(str(self.shell.pid))
        (self.state / "command").write_text(f"{bin_dir / 'claude'} --resume first-session")
        self.wait_for_agent()

    def stop_shell(self):
        os.killpg(self.shell.pid, signal.SIGKILL)
        self.shell.wait()

    def agent_pids(self):
        out = subprocess.run(["pgrep", "-P", str(self.shell.pid)], capture_output=True, text=True).stdout
        return [int(p) for p in out.split()]

    def wait_for_agent(self, exclude=()):
        for _ in range(100):
            pids = [p for p in self.agent_pids() if p not in exclude and (self.sessions / f"{p}.json").exists()]
            if pids:
                return pids[0]
            time.sleep(0.1)
        self.fail("fake agent did not start")

    def test_dry_run_names_the_session(self):
        out = self.theseus("instance", "restart", "%7", "--dry-run").stdout
        self.assertIn("would restart worker-x", out)
        self.assertIn("--resume first-session", out)

    def test_restart_exits_and_resumes_the_same_session(self):
        before = self.wait_for_agent()
        out = self.theseus("instance", "restart", "%7", timeout=120).stdout
        after = self.wait_for_agent(exclude=(before,))
        self.assertNotEqual(before, after)
        self.assertIn(f"restarted worker-x as pid {after}, session first-session", out)
        self.assertEqual("first-session", json.loads((self.sessions / f"{after}.json").read_text())["sessionId"])


if __name__ == "__main__":
    unittest.main()
