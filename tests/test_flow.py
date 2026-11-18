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

    def test_adopt_overwrites_nothing_and_parks_differing_template_files(self):
        target = self.tmp / "existing"
        self.write("docs/project/STATE.md", "# Live state\n", root=target)
        self.write("tools/Supervisor/test_context_budget.py", "BUDGETS = {'mine': 1}\n", root=target)
        self.git("init", "-q", cwd=target)
        out = self.theseus("scaffold", str(target), "--name", "live", "--adopt", "--force", cwd=self.tmp).stdout
        self.assertEqual("# Live state\n", (target / "docs/project/STATE.md").read_text())
        self.assertEqual("BUDGETS = {'mine': 1}\n", (target / "tools/Supervisor/test_context_budget.py").read_text())
        self.assertTrue((target / "theseus-adopt/docs/project/STATE.md").exists())
        self.assertTrue((target / "theseus-adopt/tools/Supervisor/test_context_budget.py").exists())
        self.assertTrue((target / "theseus.toml").exists())
        self.assertIn("push hook unchanged", out)
        hooks = self.git("config", "--get", "core.hooksPath", cwd=target, check=False).stdout.strip()
        self.assertEqual("", hooks)

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

    def test_a_second_delivery_of_the_same_handoff_is_a_no_op(self):
        self.write("theseus.local.toml", "[paths]\noriginal = []\n")
        self.add_task()
        self.theseus("instance", "assign", TASK, "worker-1", "a")
        worktree = self.tmp / "worktrees/worker-1-a"
        self.deliver(worktree)
        handoff = worktree / ".theseus/handoff.md"
        from theseus import config as cfg, tasks as t
        queue = t.Queue(cfg.load(root=self.project))
        t.deliver(queue, TASK, handoff)
        self.assertIn("already delivered", t.deliver(queue, TASK, handoff))
        self.assertEqual("verification", self.record()["status"])

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

    def test_merge_adopts_a_rebase_onto_main_but_refuses_worker_commits(self):
        self.add_task()
        self.theseus("instance", "assign", TASK, "worker-1", "a")
        worktree = self.tmp / "worktrees/worker-1-a"
        self.git("rebase", "-q", "main", cwd=worktree)
        head = self.git("rev-parse", "--short=12", "HEAD", cwd=worktree).stdout.strip()
        self.assertNotEqual(head, self.record()["baseCommit"])
        self.write("notes.txt", "a worker commit\n", root=worktree)
        self.git("add", "notes.txt", cwd=worktree)
        self.git("commit", "-q", "-m", "worker commit", cwd=worktree)
        for command in (("tasks", "rebase", TASK), ("merge", TASK)):
            refused = self.theseus(*command, check=False)
            self.assertEqual(3, refused.returncode, refused.stdout + refused.stderr)
            self.assertIn("workers never commit", refused.stderr)
        self.git("reset", "-q", "--hard", "HEAD~1", cwd=worktree)
        self.assertIn(head, self.theseus("tasks", "rebase", TASK).stdout)
        self.assertEqual(head, self.record()["baseCommit"])
        from theseus import config as cfg, workqueue
        events = workqueue.events(cfg.load(root=self.project), task=TASK, kinds=["rebased"])
        self.assertEqual(1, len(events))
        self.git("commit", "-q", "-am", "record the rebase")
        self.git("rebase", "-q", "main", cwd=worktree)
        self.deliver(worktree)
        merge = self.theseus("merge", TASK).stdout
        self.assertIn("rebased", merge)
        self.assertEqual("merged", self.record()["status"])

    def test_a_patch_also_applied_in_the_worktree_merges_once(self):
        self.add_task()
        self.theseus("instance", "assign", TASK, "worker-1", "a")
        worktree = self.tmp / "worktrees/worker-1-a"
        self.deliver(worktree)
        self.write("README.md", "# demo-port, edited after the patch\n", root=worktree)
        refused = self.theseus("merge", TASK, check=False)
        self.assertEqual(3, refused.returncode, refused.stdout + refused.stderr)
        self.assertIn("01-readme.patch", refused.stderr)
        self.assertIn("README.md", refused.stderr)
        self.write("README.md", "# demo-port\n", root=worktree)
        merge = self.theseus("merge", TASK).stdout
        self.assertIn("01-readme.patch", merge)
        self.assertEqual("# demo-port\n", (self.project / "README.md").read_text())
        commit = self.record()["merge"]["commit"]
        self.assertIn("README.md", self.git("show", "--name-only", "--format=", commit).stdout.split())
        self.assertEqual("", self.git("status", "--porcelain").stdout)


if __name__ == "__main__":
    unittest.main()
