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


class UpgradeTests(Sandbox):
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
        reference = self.project / ".agents/skills/theseus-coordinating/references/events.md"
        reference.write_text("stale\n")
        self.assertIn("update   .agents/skills/theseus-coordinating/references/events.md", self.theseus("upgrade").stdout)
        self.assertNotEqual("stale\n", reference.read_text())
        self.assertEqual("# mine\n", (self.project / "AGENTS.md").read_text())
        self.assertIn("theseus.toml lacks verification.max_unverified_hours", self.theseus("upgrade").stdout)


if __name__ == "__main__":
    unittest.main()
