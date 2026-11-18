"""Worker launch and supervision with stand-ins: a fake claude-cborg launcher and a fake tmux on PATH, private data
roots and no systemd slices, so the real gateway, the real tmux server and the user's running sessions stay untouched."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import Sandbox

TASK = "decode-status-frame"

FAKE_WORKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" > "$FAKE_ARGS"
sleep "${FAKE_SLEEP:-2}"
mkdir -p src .theseus
"$FAKE_PYTHON" -m theseus config get root > .theseus/root.txt
"$FAKE_PYTHON" -m theseus config get paths.original > .theseus/original.txt
echo 'def decode(frame): return frame[0]' > src/status.py
printf '# Status frame\\n\\nTask `decode-status-frame` on base x.\\n' > docs/investigations/decode-status-frame.md
printf 'Task: decode-status-frame\\nResult: delivered\\n' > .theseus/handoff.md
printf '{"is_error": false, "result": "done"}\\n'
"""

FAKE_TMUX = """#!/usr/bin/env bash
case "$1" in
    capture-pane) cat "$FAKE_TMUX_SCREEN" 2>/dev/null
        if [ -e "$FAKE_TMUX_SCREEN.busy" ]; then echo "working (esc to interrupt)"; rm -f "$FAKE_TMUX_SCREEN.busy"; fi ;;
    send-keys) shift; printf '%s\\n' "$*" >> "$FAKE_TMUX_LOG"
        if [ "${!#}" = Enter ]; then touch "$FAKE_TMUX_SCREEN.busy"; fi ;;
    *) exit 1 ;;
esac
"""


class WorkerSandbox(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        self.theseus("tasks", "milestone", "M0")
        self.theseus("tasks", "add", TASK, "--role", "implementation", "--owned", "src/status.py",
                     f"docs/investigations/{TASK}.md", "--objective", "Decode the device status frame.",
                     "--contract", "Every status frame layout the original decodes.", "--accept", "Recorded frames decode.")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.env.update({"PATH": f"{self.bin}{os.pathsep}{self.env['PATH']}", "THESEUS_NO_SLICE": "1",
                         "FAKE_ARGS": str(self.tmp / "args.txt"), "FAKE_PYTHON": sys.executable, "THESEUS_WAIT_POLL": "0.2", "THESEUS_STOP_WAIT": "20"})

    def fake(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)
        return path


class DispatchTests(WorkerSandbox):
    def test_cborg_dispatch_launches_the_worker_and_wait_reports_its_handoff(self):
        self.fake("claude", FAKE_WORKER)
        self.env["FAKE_SLEEP"] = "0.5"
        self.write("theseus.local.toml", '[paths]\noriginal = ["/srv/original-app"]\n')
        out = self.theseus("dispatch", TASK).stdout
        self.assertIn("launched", out)
        record = json.loads(self.theseus("tasks", "show", TASK).stdout)
        self.assertEqual("in_progress", record["status"])
        self.assertEqual("claude-code", record["execution"]["provider"])
        waited = self.theseus("wait", "--timeout", "30")
        self.assertIn(f"{TASK}: exited with a handoff", waited.stdout)
        worker_log = self.tmp / "worktrees" / TASK / ".theseus"
        self.assertEqual(str(self.tmp / "worktrees" / TASK), (worker_log / "root.txt").read_text().strip())
        self.assertEqual('["/srv/original-app"]', (worker_log / "original.txt").read_text().strip())
        args = (self.tmp / "args.txt").read_text()
        self.assertIn("--session-id", args)
        self.assertIn("never spawn subagents", args)
        self.assertIn(f"task `{TASK}`", args)
        status = self.theseus("status").stdout
        self.assertRegex(status, rf"{TASK}\s+\d+\s+exited\s+\d+\s+\S+\s+yes")
        self.assertIn('"is_error": false', (self.tmp / f"data/workers/{TASK}/result.json").read_text())
        self.theseus("merge", TASK)
        self.assertEqual("merged", json.loads(self.theseus("tasks", "show", TASK).stdout)["status"])


class InstanceTests(WorkerSandbox):
    def setUp(self):
        super().setUp()
        self.fake("tmux", FAKE_TMUX)
        self.screen = self.tmp / "screen.txt"
        self.screen.write_text("> \n")
        self.log = self.tmp / "tmux.log"
        self.env.update({"FAKE_TMUX_SCREEN": str(self.screen), "FAKE_TMUX_LOG": str(self.log), "THESEUS_COMPACT_POLL": "0.1"})

    def test_busy_pane_is_not_sent_to(self):
        self.screen.write_text("working... (esc to interrupt)\n")
        lib = Path(__file__).resolve().parents[1] / "src/theseus/scripts/lib.sh"
        result = subprocess.run(["bash", "-c", f"source {lib}; pane_busy %9"], env=dict(self.env, THESEUS_PROJECT="x",
                                THESEUS_BUSY_PATTERN="esc to interrupt"), capture_output=True)
        self.assertEqual(0, result.returncode)
        self.screen.write_text("> \n")
        result = subprocess.run(["bash", "-c", f"source {lib}; pane_busy %9"], env=dict(self.env, THESEUS_PROJECT="x",
                                THESEUS_BUSY_PATTERN="esc to interrupt"), capture_output=True)
        self.assertEqual(1, result.returncode)


if __name__ == "__main__":
    unittest.main()
