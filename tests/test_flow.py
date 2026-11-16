"""End to end on a fresh scaffold: the scaffold's own Supervisor suite passes, then a task goes through add, assign,
a simulated worker handoff, merge, observation, full verification, complete, archive and retire."""
import json
import unittest

from helpers import Sandbox

TASK = "parse-capture-header"


class ScaffoldTests(Sandbox):
    def test_scaffold_into_empty_directory_passes_its_supervisor_suite(self):
        self.theseus("scaffold", self.project, "--name", "demo-port", cwd=self.tmp)
        for relative in ("AGENTS.md", "docs/project/STATE.md", "docs/project/ROADMAP.md", "docs/project/MANIFEST.md",
                         "docs/project/tasks.json", "docs/project/tasks-archive.jsonl", "docs/investigations/TEMPLATE.md",
                         "docs/investigations/README.md", "docs/topics/README.md", "docs/re/citation-index.md",
                         ".agents/skills/theseus-coordinating/SKILL.md", ".agents/skills/theseus-worker/SKILL.md",
                         ".agents/skills/theseus-native-re/SKILL.md", ".agents/skills/theseus-reference-runs/SKILL.md",
                         "tools/Supervisor/test_project_tasks.py", "tools/git-hooks/pre-push", "tools/lib/xvfb.sh",
                         "theseus.toml", ".gitignore"):
            self.assertTrue((self.project / relative).exists(), relative)
        self.assertTrue((self.project / ".claude/skills").is_symlink())
        self.assertTrue((self.project / ".claude/skills/theseus-worker/SKILL.md").exists())
        self.assertIn("demo-port", (self.project / "AGENTS.md").read_text())
        result = self.theseus("check", "-v")
        self.assertIn("OK", result.stderr)
        self.theseus("docs", "check")
        self.assertEqual("tools/git-hooks", self.git("config", "core.hooksPath").stdout.strip())

    def test_scaffold_keeps_existing_files_without_force(self):
        self.project.mkdir()
        self.write("AGENTS.md", "mine\n")
        self.write(".gitignore", "node_modules/\n")
        result = self.theseus("scaffold", self.project, cwd=self.tmp)
        self.assertIn("kept     AGENTS.md", result.stdout)
        self.assertEqual("mine\n", (self.project / "AGENTS.md").read_text())
        ignored = (self.project / ".gitignore").read_text().splitlines()
        self.assertEqual("node_modules/", ignored[0])
        self.assertIn("theseus.local.toml", ignored)
        self.assertIn(".theseus/", ignored)
        self.theseus("scaffold", self.project, "--force", cwd=self.tmp)
        self.assertNotEqual("mine\n", (self.project / "AGENTS.md").read_text())

    def test_commands_outside_a_project_refuse_instead_of_guessing(self):
        self.tmp.joinpath("empty").mkdir()
        result = self.theseus("tasks", "list", cwd=self.tmp / "empty", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("no theseus.toml", result.stderr)


class TaskFlowTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        self.replace_config("verify = []", "verify = [\"python3 -c 'print(\\\"verified\\\")'\"]")
        self.replace_config("build = []", "build = [\"test -f theseus.toml\"]")
        self.theseus("tasks", "milestone", "M0")
        self.git("commit", "-q", "-am", "configure")

    def add_task(self, task_id=TASK, owned=("src/capture/header.py", f"docs/investigations/{TASK}.md")):
        self.theseus("tasks", "add", task_id, "--role", "implementation", "--owned", *owned,
                     "--objective", "Parse the capture file header.",
                     "--contract", "Every header variant the original writes, with unknown fields preserved.",
                     "--accept", "Two header variants parse.", "Unknown versions return an explicit unsupported result.")

    def deliver(self, worktree, task_id=TASK):
        self.write("src/capture/header.py", "def parse(data):\n    return data[:4]\n", root=worktree)
        self.write(f"docs/investigations/{task_id}.md",
                   f"# Capture header\n\nTask `{task_id}` on base x. Topic: `new`.\n\n## Result\n\n- **Recovered**: magic at 0x00401a20.\n",
                   root=worktree)
        self.write("README.md", "# demo-port\n", root=worktree)
        self.git("add", "-N", "README.md", cwd=worktree)
        patch = self.git("diff", "--", "README.md", cwd=worktree).stdout
        self.git("reset", "-q", cwd=worktree)
        (worktree / "README.md").unlink()
        self.write(".theseus/patches/01-readme.patch", patch, root=worktree)
        self.write(".theseus/handoff.md", f"Task: {task_id}\nResult: delivered\nChecks: none\n", root=worktree)

    def record(self, task_id=TASK):
        return json.loads(self.theseus("tasks", "show", task_id).stdout)

    def test_full_task_flow(self):
        self.write("theseus.local.toml", "[paths]\noriginal = []\n")
        self.add_task()
        self.assertEqual(TASK, self.theseus("tasks", "ready").stdout.strip())
        out = self.theseus("instance", "assign", TASK, "worker-1", "a").stdout
        worktree = self.tmp / "worktrees/worker-1-a"
        self.assertIn(str(worktree), out)
        self.assertEqual("in_progress", self.record()["status"])
        self.assertEqual("", self.git("status", "--porcelain", "--untracked-files=no").stdout)
        self.assertTrue((worktree / "theseus.local.toml").exists())
        self.deliver(worktree)
        check = self.theseus("tasks", "check-delivery", TASK).stdout
        self.assertIn("0 outside owned paths", check)

        merge = self.theseus("merge", TASK).stdout
        self.assertIn("committed", merge)
        record = self.record()
        self.assertEqual("merged", record["status"])
        files = self.git("show", "--name-only", "--format=", record["merge"]["commit"]).stdout.split()
        self.assertEqual(sorted(["README.md", "docs/investigations/README.md", "docs/re/citation-index.md",
                                 "src/capture/header.py", f"docs/investigations/{TASK}.md"]), sorted(files))
        self.assertIn("0x00401a20", (self.project / "docs/re/citation-index.md").read_text())
        self.assertEqual("", self.git("status", "--porcelain").stdout)
        self.assertTrue((self.tmp / "data/workers/worker-1-a/.theseus/handoff.md").exists())

        listing = self.theseus("tasks", "list", "--evidence").stdout
        self.assertIn("evidence=merged", listing)
        refused = self.theseus("tasks", "complete", TASK, "--summary", "x", check=False)
        self.assertNotEqual(0, refused.returncode)
        self.theseus("tasks", "observe", TASK, "--how", "parsed two recorded captures with the new reader")
        self.assertIn("evidence=observed", self.theseus("tasks", "list", "--evidence").stdout)
        self.assertIn("build: pass", self.theseus("build").stdout)

        later = self.project / "src/capture/header.py"
        later.write_text(later.read_text() + "# changed by a later task\n")
        self.git("commit", "-q", "-am", "a later task edits the same file")
        verify = self.theseus("verify")
        log = next(line.split(": ", 1)[1] for line in verify.stdout.splitlines() if line.startswith("verify log: "))
        self.assertEqual("0 code commits", self.theseus("verify", "--debt").stdout.split(" since")[0])
        self.theseus("tasks", "complete", TASK, "--from-log", log, "--summary", "header reader merged and verified")
        self.assertEqual("completed", self.record()["status"])
        self.assertIn("archived 1", self.theseus("tasks", "archive").stdout)
        self.assertEqual([], json.loads((self.project / "docs/project/tasks.json").read_text())["tasks"])
        self.assertEqual("completed", self.record()["status"])
        self.theseus("tasks", "retire", TASK)
        self.assertFalse(worktree.exists())
        self.assertNotIn("work/worker-1-a", self.git("branch").stdout)
        self.git("commit", "-q", "-am", "archive")
        self.theseus("check")
        self.theseus("docs", "check")

    def test_conflicting_delivery_restores_main_and_commits_nothing(self):
        self.add_task()
        self.theseus("instance", "assign", TASK, "worker-1", "a")
        worktree = self.tmp / "worktrees/worker-1-a"
        self.deliver(worktree)
        self.write("src/capture/header.py", "MAIN = 1\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "main moved")
        head = self.git("rev-parse", "HEAD").stdout
        result = self.theseus("merge", TASK, check=False)
        self.assertEqual(3, result.returncode, result.stdout + result.stderr)
        self.assertEqual(head, self.git("rev-parse", "HEAD").stdout)
        self.assertEqual("", self.git("status", "--porcelain").stdout)
        self.assertEqual("MAIN = 1\n", (self.project / "src/capture/header.py").read_text())
        self.assertFalse((self.project / "README.md").exists())
        self.assertEqual("in_progress", self.record()["status"])

    def test_worker_commit_and_outside_paths_are_refused(self):
        self.add_task()
        self.theseus("instance", "assign", TASK, "worker-1", "a")
        worktree = self.tmp / "worktrees/worker-1-a"
        self.write("src/other.py", "x = 1\n", root=worktree)
        outside = self.theseus("merge", TASK, check=False)
        self.assertEqual(3, outside.returncode)
        self.assertIn("OUTSIDE ?? src/other.py", outside.stderr)
        (worktree / "src/other.py").unlink()
        self.deliver(worktree)
        self.git("add", "-A", cwd=worktree)
        self.git("commit", "-q", "-m", "worker commit", cwd=worktree)
        committed = self.theseus("merge", TASK, check=False)
        self.assertEqual(3, committed.returncode)
        self.assertIn("workers never commit", committed.stderr)

    def test_dependencies_wait_for_merge_and_milestones_gate_readiness(self):
        self.add_task()
        self.add_task("use-capture-header", ("src/capture/use.py", "docs/investigations/use-capture-header.md"))
        tasks = json.loads((self.project / "docs/project/tasks.json").read_text())
        tasks["tasks"][1]["dependencies"] = [TASK]
        (self.project / "docs/project/tasks.json").write_text(json.dumps(tasks, indent=2) + "\n")
        self.assertEqual([TASK], self.theseus("tasks", "ready").stdout.split())
        self.theseus("tasks", "add", "later-work", "--role", "research", "--owned", "docs/investigations/later-work.md",
                     "--objective", "Later.", "--contract", "Later.", "--accept", "Later.", "--milestone", "M1")
        self.assertNotIn("later-work", self.theseus("tasks", "ready").stdout)
        self.assertIn("later-work", self.theseus("tasks", "ready", "--any-milestone").stdout)
        self.theseus("instance", "assign", TASK, "worker-1", "a")
        self.deliver(self.tmp / "worktrees/worker-1-a")
        self.theseus("merge", TASK)
        self.assertIn("use-capture-header", self.theseus("tasks", "ready").stdout)


class InPlaceTests(Sandbox):
    def test_in_place_assignment_needs_no_commit_and_disjoint_ownership(self):
        self.theseus("scaffold", self.project, cwd=self.tmp)
        for task_id, owned in (("survey-a", "docs/investigations/survey-a.md"), ("survey-b", "docs/investigations/survey-b.md"),
                               ("survey-c", "docs/investigations/")):
            self.theseus("tasks", "add", task_id, "--role", "research", "--owned", owned, "docs/investigations/x-" + task_id + ".md",
                         "--objective", "Survey.", "--contract", "Sources.", "--accept", "Report written.")
        first = self.theseus("instance", "assign", "survey-a", "worker-1", "a", "--in-place").stdout
        self.assertIn("base uncommitted", first)
        self.theseus("instance", "assign", "survey-b", "worker-2", "a", "--in-place")
        refused = self.theseus("instance", "assign", "survey-c", "worker-3", "a", "--in-place", check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("overlap", refused.stderr)
        record = json.loads(self.theseus("tasks", "show", "survey-a").stdout)
        self.assertEqual(str(self.project), record["worktree"])
        self.assertEqual(str(self.project / ".theseus/survey-a"), record["execution"]["logDir"])
        self.assertTrue((self.project / ".theseus/survey-a").is_dir())
        self.assertNotEqual(0, self.git("rev-parse", "HEAD", check=False).returncode, "nothing may be committed")
        self.theseus("check")


if __name__ == "__main__":
    unittest.main()
