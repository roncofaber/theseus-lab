"""Worker launch and supervision with stand-ins: a fake claude-cborg launcher and a fake tmux on PATH, private data
roots and no systemd slices, so the real gateway, the real tmux server and the user's running sessions stay untouched."""
import json
import os
import select
import signal
import subprocess
import sys
import time
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

LOOPING_WORKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" > "$FAKE_ARGS"
trap 'printf "{\\\\"status\\\\":\\\\"interrupted\\\\"}\\\\n"; exit 130' INT
while :; do sleep 0.1; done
"""

FAKE_TMUX = """#!/usr/bin/env bash
case "$1" in
    capture-pane) cat "$FAKE_TMUX_SCREEN" 2>/dev/null ;;
    send-keys) shift; printf '%s\\n' "$*" >> "$FAKE_TMUX_LOG" ;;
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
        self.git("commit", "-q", "-am", "queue")
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

    def test_dry_run_changes_nothing_and_claude_provider_needs_a_model(self):
        before = self.git("rev-parse", "HEAD").stdout
        out = self.theseus("dispatch", TASK, "--dry-run").stdout
        self.assertIn("--- commands ---", out)
        self.assertEqual(before, self.git("rev-parse", "HEAD").stdout)
        self.assertFalse((self.tmp / "worktrees" / TASK).exists())
        refused = self.theseus("dispatch", TASK, "--provider", "claude-subagent", check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("set workers.claude-subagent.capacity", refused.stderr)
        self.set_config('[workers.claude-subagent]\nmodel = "sonnet"\ncapacity = 1\n')
        self.git("commit", "-q", "-am", "enable subagents")
        prepared = self.theseus("dispatch", TASK, "--provider", "claude-subagent").stdout
        self.assertIn("Launch a subagent (model sonnet)", prepared)
        self.assertTrue((self.tmp / f"data/workers/{TASK}/prompt.md").exists())
        self.assertIn("subagent", self.theseus("status").stdout)

    def test_capacity_is_enforced(self):
        self.replace_config("capacity = 2", "capacity = 0")
        self.git("commit", "-q", "-am", "no capacity")
        self.fake("claude", FAKE_WORKER)
        refused = self.theseus("dispatch", TASK, check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("capacity", refused.stderr)
        self.assertFalse((self.tmp / "worktrees" / TASK).exists())

    def test_followup_resumes_the_session_and_stop_interrupts_it(self):
        self.fake("claude", LOOPING_WORKER)
        worktree = self.tmp / "worktrees/sample"
        worktree.mkdir(parents=True)
        record = self.tmp / "data/workers/sample"
        record.mkdir(parents=True)
        (record / "launch.json").write_text(json.dumps({
            "worktree": str(worktree), "model": "stand-in", "provider": "claude-code", "session": "3f0c-session", "env": {},
            "resume": ["claude", "-p", "{message}", "--resume", "3f0c-session", "--output-format", "json"]}))
        (record / "result.json").write_text("{}")
        (record / "pid").write_text(f"{2**22 + 7}\n")
        self.theseus("followup", "sample", "finish the report")
        pid = int((record / "pid").read_text())
        try:
            for _ in range(50):
                if (self.tmp / "args.txt").exists():
                    break
                time.sleep(0.1)
            arguments = (self.tmp / "args.txt").read_text()
            self.assertIn("--resume 3f0c-session", arguments)
            self.assertIn("finish the report", arguments)
            self.assertTrue(any(record.glob("result-*.json")))
            self.assertIn("finish the report", (record / "followups.log").read_text())
            ignored = int(next(line for line in Path(f"/proc/{pid}/status").read_text().splitlines()
                               if line.startswith("SigIgn:")).split()[1], 16)
            self.assertEqual(0, ignored & (1 << 1), "SIGINT must not be ignored by the recorded process")
            stop = self.theseus("stop", "sample", timeout=60)
            self.assertIn("session=3f0c-session", stop.stdout)
        finally:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass


class InstanceTests(WorkerSandbox):
    def setUp(self):
        super().setUp()
        self.fake("tmux", FAKE_TMUX)
        self.screen = self.tmp / "screen.txt"
        self.screen.write_text("> \n")
        self.log = self.tmp / "tmux.log"
        self.env.update({"FAKE_TMUX_SCREEN": str(self.screen), "FAKE_TMUX_LOG": str(self.log), "THESEUS_COMPACT_POLL": "0.1"})

    def test_send_types_the_task_message_and_compact_types_the_command(self):
        worktree = self.theseus("instance", "assign", TASK, "inst-1", "a").stdout.split("worktree ")[1].split()[0]
        self.theseus("instance", "send", "%9", TASK, worktree, "in two hours")
        typed = self.log.read_text()
        self.assertIn("-t %9 -l Read and follow", typed)
        self.assertIn("instance-brief.md first. New task decode-status-frame", typed)
        self.assertIn("Owned paths: src/status.py", typed)
        self.assertIn("Deadline: in two hours", typed)
        self.assertIn("Log directory (handoff.md, changed.sha256):", typed)
        self.assertNotIn("In-place assignment", typed)
        self.assertIn("-t %9 Enter", typed)
        self.assertNotIn("SendMessage", typed)
        self.replace_config('coordinator = ""', 'coordinator = "lead-session"')
        self.theseus("instance", "send", "%9", TASK, worktree)
        self.assertIn("sent to the coordinator session lead-session with SendMessage", self.log.read_text())
        self.theseus("instance", "compact", "%9")
        self.assertIn("/compact Keep only", self.log.read_text())

    def test_watch_reports_idle_instances_and_new_handoffs(self):
        self.theseus("instance", "assign", TASK, "inst-1", "a")
        env = dict(self.env, THESEUS_WATCH_ONCE="1", THESEUS_WATCH_POLL="60", THESEUS_IDLE_MINUTES="1")
        self.assertIn("IDLE-NO-HANDOFF inst-1", self.theseus("instance", "watch", "inst-1:%9", env=env).stdout)
        env = dict(self.env, THESEUS_WATCH_POLL="1")
        watcher = subprocess.Popen([sys.executable, "-m", "theseus", "instance", "watch", "inst-1:%9"], cwd=self.project,
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        try:
            time.sleep(1.5)
            self.write(".theseus/handoff.md", "Task: x\n", root=self.tmp / "worktrees/inst-1-a")
            ready, _, _ = select.select([watcher.stdout], [], [], 20)
            self.assertTrue(ready, "the watcher printed nothing within 20 s")
            self.assertEqual("HANDOFF inst-1-a", watcher.stdout.readline().strip())
            self.write(".theseus/some-task/handoff.md", "Task: some-task\n")
            ready, _, _ = select.select([watcher.stdout], [], [], 20)
            self.assertTrue(ready, "the watcher missed an in-place handoff")
            self.assertEqual("HANDOFF in-place:some-task", watcher.stdout.readline().strip())
        finally:
            os.killpg(watcher.pid, signal.SIGKILL)
            watcher.wait()
            watcher.stdout.close()

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
