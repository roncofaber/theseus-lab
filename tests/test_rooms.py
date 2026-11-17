"""Clean-room separation and project upgrades."""
import json
import unittest

from helpers import Sandbox

ROOMS = """
[rooms]
enabled = true
clean_instances = ["clean-1"]
dirty_instances = ["dirty-1", "clean-1-later"]
dirty_paths = ["docs/investigations/re-", "tools/inventory/"]
dirty_data = ["re", "extracted"]
spec_paths = ["docs/protocol/"]
"""


class RoomTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        path = self.project / "theseus.toml"
        text = path.read_text()
        start, end = text.index("[rooms]\n"), text.index("[git]\n")
        path.write_text(text[:start] + ROOMS.lstrip() + "\n" + text[end:])
        self.git("commit", "-q", "-am", "rooms")

    def add(self, task_id, room, owned):
        self.theseus("tasks", "add", task_id, "--role", "research", "--room", room, "--owned", *owned,
                     "--objective", "Do it.", "--contract", "All variants.", "--accept", "Done.")

    def test_rooms_gate_instance_assignment_and_taint_is_permanent(self):
        self.add("re-transfer-layer", "dirty", ["docs/investigations/re-transfer-layer.md"])
        self.add("driver-skeleton", "clean", ["src/", "docs/investigations/driver-skeleton.md"])
        refused = self.theseus("instance", "assign", "re-transfer-layer", "clean-1", "a", check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("dirty-room task but clean-1 is a clean-room instance", refused.stderr)
        self.theseus("instance", "assign", "re-transfer-layer", "dirty-1", "a")
        self.assertEqual("re-transfer-layer", json.loads((self.tmp / "data/rooms.json").read_text())["tainted"]["dirty-1"])
        self.write("tools/inventory/secret.py", "x = 1\n")
        self.write("docs/investigations/re-old.md", "# Old\n")
        self.theseus("docs", "index")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "dirty material")
        self.theseus("instance", "assign", "driver-skeleton", "clean-1", "a")
        clean = self.tmp / "worktrees/clean-1-a"
        self.assertFalse((clean / "tools/inventory/secret.py").exists())
        self.assertFalse((clean / "docs/investigations/re-old.md").exists())
        self.assertTrue((clean / "AGENTS.md").exists())
        self.write("src/driver.c", "int x;\n", root=clean)
        self.write("docs/investigations/driver-skeleton.md", "# Skeleton\n\nTask `driver-skeleton` on base x.\n", root=clean)
        self.write(".theseus/handoff.md", "Task: driver-skeleton\nResult: delivered\n", root=clean)
        self.theseus("merge", "driver-skeleton")
        self.assertTrue((self.project / "tools/inventory/secret.py").exists(), "merging a sparse worktree must not delete dirty files")

    def test_dispatched_clean_work_gets_the_sparse_checkout_and_room_rules(self):
        self.write("tools/inventory/secret.py", "x = 1\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "dirty material")
        self.add("subagent-clean", "clean", ["src/", "docs/investigations/subagent-clean.md"])
        self.git("commit", "-q", "-am", "queue")
        self.set_config('[workers.claude-subagent]\ncapacity = 1\nmodel = "sonnet"\n')
        self.git("commit", "-q", "-am", "subagents")
        out = self.theseus("dispatch", "subagent-clean", "--provider", "claude-subagent").stdout
        self.assertIn("Launch a subagent (model sonnet)", out)
        worktree = self.tmp / "worktrees/subagent-clean"
        self.assertFalse((worktree / "tools/inventory/secret.py").exists())
        self.assertTrue((worktree / "AGENTS.md").exists())
        self.assertIn("CLEAN ROOM", (self.tmp / "data/workers/subagent-clean/prompt.md").read_text())
        flat = " ".join(self.theseus("top", "--once").stdout.split())
        self.assertIn("subagent-clean - subagent subagent-clean clean", flat)

    def test_clearing_context_lifts_the_taint_and_permanent_mode_keeps_it(self):
        self.replace_config('clean_instances = ["clean-1"]', 'clean_instances = []')
        self.replace_config('dirty_instances = ["dirty-1", "clean-1-later"]', 'dirty_instances = []')
        self.replace_config("panes = []", 'panes = [{ name = "worker-9", pane = "%9" }]')
        self.git("commit", "-q", "-am", "any instance")
        self.add("re-a", "dirty", ["docs/investigations/re-a.md"])
        self.add("clean-b", "clean", ["src/b.c", "docs/investigations/clean-b.md"])
        self.theseus("instance", "assign", "re-a", "worker-9", "a")
        refused = self.theseus("instance", "assign", "clean-b", "worker-9", "b", check=False)
        self.assertIn("clear its context first", refused.stderr)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "tmux").write_text('#!/usr/bin/env bash\n[ "$1" = capture-pane ] && echo "> "\nexit 0\n')
        (bin_dir / "tmux").chmod(0o755)
        env = dict(self.env, PATH=f"{bin_dir}:{self.env['PATH']}", THESEUS_COMPACT_POLL="0.01")
        summarized = self.theseus("instance", "compact", "%9", env=env).stdout
        self.assertNotIn("taint", summarized)
        self.assertIn("re-a", (self.tmp / "data/rooms.json").read_text())
        cleared = self.theseus("instance", "compact", "%9", "--clear", env=env).stdout
        self.assertIn("worker-9: context cleared, taint from re-a lifted", cleared)
        self.theseus("instance", "assign", "clean-b", "worker-9", "b")

    def test_a_tainted_instance_is_refused_clean_work_even_if_reconfigured(self):
        self.replace_config('[rooms]\nenabled = true', '[rooms]\nenabled = true\ntaint = "permanent"')
        self.add("re-a", "dirty", ["docs/investigations/re-a.md"])
        self.theseus("instance", "assign", "re-a", "clean-1-later", "a")
        self.replace_config('clean_instances = ["clean-1"]', 'clean_instances = ["clean-1", "clean-1-later"]')
        self.replace_config('dirty_instances = ["dirty-1", "clean-1-later"]', 'dirty_instances = ["dirty-1"]')
        self.git("commit", "-q", "-am", "reconfigure")
        self.add("clean-b", "clean", ["src/b.c", "docs/investigations/clean-b.md"])
        refused = self.theseus("instance", "assign", "clean-b", "clean-1-later", "a", check=False)
        self.assertIn("may never take clean work", refused.stderr)

    def test_supervisor_rejects_roomless_tasks_and_clean_tasks_owning_dirty_paths(self):
        self.add("clean-bad", "clean", ["tools/inventory/x.py", "docs/investigations/clean-bad.md"])
        self.theseus("tasks", "add", "no-room", "--role", "research", "--owned", "docs/investigations/no-room.md",
                     "--objective", "x", "--contract", "x", "--accept", "x")
        result = self.theseus("check", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("clean-room task owns dirty paths", result.stderr)
        self.assertIn("no room (clean or dirty)", result.stderr)

    def test_task_messages_carry_the_room_rules(self):
        self.add("driver-skeleton", "clean", ["src/", "docs/investigations/driver-skeleton.md"])
        self.add("re-x", "dirty", ["docs/investigations/re-x.md"])
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "tmux").write_text('#!/usr/bin/env bash\n[ "$1" = send-keys ] && printf "%s\\n" "$*" >> "$TMUX_LOG"\nexit 0\n')
        (bin_dir / "tmux").chmod(0o755)
        env = dict(self.env, PATH=f"{bin_dir}:{self.env['PATH']}", TMUX_LOG=str(self.tmp / "tmux.log"))
        self.theseus("instance", "send", "%1", "driver-skeleton", "/w", env=env)
        self.theseus("instance", "send", "%2", "re-x", "/w", env=env)
        log = (self.tmp / "tmux.log").read_text()
        self.assertIn("CLEAN ROOM: never open, search or summarize docs/investigations/re-, tools/inventory/", log)
        self.assertIn(f"{self.tmp}/data/re", log)
        self.assertIn("DIRTY ROOM: record what the original does", log)
        self.assertIn("internal addresses, offsets, function names and call structure stay in your report", log)


class UpgradeTests(Sandbox):
    def test_upgrade_leaves_files_the_project_keeps(self):
        self.scaffold()
        readme = self.project / "tools/Supervisor/README.md"
        readme.write_text("# Our own checks\n")
        self.replace_config("keep = []", 'keep = ["tools/Supervisor/README.md"]')
        out = self.theseus("upgrade").stdout
        self.assertNotIn("tools/Supervisor/README.md", out)
        self.assertEqual("# Our own checks\n", readme.read_text())

    def test_upgrade_refreshes_owned_files_adds_missing_ones_and_keeps_project_files(self):
        self.scaffold()
        skill = self.project / ".agents/skills/theseus-worker/SKILL.md"
        original = skill.read_text()
        skill.write_text("stale\n")
        (self.project / "AGENTS.md").write_text("# mine\n")
        (self.project / "tools/lib/xvfb.sh").unlink()
        self.replace_config("max_unverified_hours = 24\n", "")
        dry = self.theseus("upgrade", "--dry-run").stdout
        self.assertIn("would update   .agents/skills/theseus-worker/SKILL.md", dry)
        self.assertEqual("stale\n", skill.read_text())
        out = self.theseus("upgrade").stdout
        self.assertIn("add   tools/lib/xvfb.sh", out)
        self.assertIn("theseus.toml lacks verification.max_unverified_hours", out)
        self.assertEqual(original, skill.read_text())
        self.assertEqual("# mine\n", (self.project / "AGENTS.md").read_text())
        self.assertIn("theseus.toml lacks verification.max_unverified_hours", self.theseus("upgrade").stdout)


if __name__ == "__main__":
    unittest.main()
