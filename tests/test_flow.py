"""End to end on a fresh scaffold: the scaffold's own Supervisor suite passes, then a task goes through add, assign,
a simulated worker handoff, merge, observation, full verification, complete, archive and retire."""
import hashlib
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
        moved = self.git("log", "-1", "--format=%h main moved").stdout.strip()
        for expected in ("src/capture/header.py: changed on main since the worktree base", moved, "<<<<<<< main",
                         "MAIN = 1", "return data[:4]", ">>>>>>> worker", f"theseus tasks rebase {TASK}"):
            self.assertIn(expected, result.stderr)

    def test_an_owned_file_main_changed_after_the_base_merges_three_way(self):
        lines = "".join(f"line {n}\n" for n in range(1, 13))
        mine = lines.replace("line 10\n", "line 10 from the worker\n")
        both = lines.replace("line 1\n", "line 1 from main\n").replace("line 10\n", "line 10 from the worker\n")
        for task_id, name in ((TASK, "a"), ("parse-capture-footer", "b")):
            self.write("src/capture/header.py", lines)
            self.git("add", "-A")
            self.git("commit", "-q", "-m", "header")
            self.add_task(task_id, owned=("src/capture/header.py", f"docs/investigations/{task_id}.md"))
            self.theseus("instance", "assign", task_id, "worker-1", name)
            worktree = self.tmp / f"worktrees/worker-1-{name}"
            self.write("src/capture/header.py", lines.replace("line 1\n", "line 1 from main\n"))
            self.git("commit", "-q", "-am", "main edits line 1")
            self.write(".theseus/handoff.md", f"Task: {task_id}\nResult: delivered\n", root=worktree)
            if name == "b":
                # The worker moved HEAD to main and copied its whole file back from the old base.
                self.git("reset", "-q", "--hard", "main", cwd=worktree)
            self.write("src/capture/header.py", mine, root=worktree)
            merge = self.theseus("merge", task_id).stdout
            self.assertEqual(both, (self.project / "src/capture/header.py").read_text())
            self.assertEqual("", self.git("status", "--porcelain").stdout)
        self.assertIn("moved from the base", merge)

    def test_rebase_replays_uncommitted_edits_onto_main(self):
        lines = "".join(f"line {n}\n" for n in range(1, 13))
        self.write("src/capture/header.py", lines)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "header")
        self.add_task()
        self.theseus("instance", "assign", TASK, "worker-1", "a")
        worktree = self.tmp / "worktrees/worker-1-a"
        self.write("src/capture/header.py", lines.replace("line 1\n", "line 1 from main\n"))
        self.git("commit", "-q", "-am", "main edits line 1")
        self.deliver(worktree)
        self.write("src/capture/header.py", lines.replace("line 10\n", "line 10 from the worker\n"), root=worktree)
        self.theseus("tasks", "rebase", TASK)
        head = self.git("rev-parse", "--short=12", "HEAD").stdout.strip()
        self.assertEqual(head, self.git("rev-parse", "--short=12", "HEAD", cwd=worktree).stdout.strip())
        self.assertEqual(head, self.record()["baseCommit"])
        self.assertEqual(lines.replace("line 1\n", "line 1 from main\n").replace("line 10\n", "line 10 from the worker\n"),
                         (worktree / "src/capture/header.py").read_text())

        self.git("commit", "-q", "-am", "record the rebase")
        self.write("src/capture/header.py", lines.replace("line 10\n", "line 10 from main\n"))
        self.git("commit", "-q", "-am", "main edits line 10")
        refused = self.theseus("tasks", "rebase", TASK, check=False)
        self.assertEqual(3, refused.returncode, refused.stdout + refused.stderr)
        self.assertIn("src/capture/header.py hold conflict markers", refused.stderr)
        text = (worktree / "src/capture/header.py").read_text()
        for expected in ("<<<<<<< Updated upstream", "line 10 from main", "line 10 from the worker", ">>>>>>> Stashed changes"):
            self.assertIn(expected, text)
        self.assertTrue((worktree / f"docs/investigations/{TASK}.md").exists())
        self.assertEqual(head, self.record()["baseCommit"])
        refused = self.theseus("merge", TASK, check=False)
        self.assertIn("still holds the conflict markers", refused.stderr)
        self.write("src/capture/header.py", lines.replace("line 10\n", "line 10 from main and the worker\n"), root=worktree)
        self.theseus("merge", TASK)
        self.assertIn("line 10 from main and the worker", (self.project / "src/capture/header.py").read_text())
        self.assertEqual("", self.git("stash", "list").stdout)

    def test_a_deleted_file_is_in_the_merge_commit_and_completes(self):
        self.write("src/capture/old.py", "OLD = 1\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "old module")
        self.add_task(owned=("src/capture/", f"docs/investigations/{TASK}.md"))
        self.git("commit", "-q", "-am", "queue")
        self.theseus("instance", "assign", TASK, "worker-1", "a")
        worktree = self.tmp / "worktrees/worker-1-a"
        (worktree / "src/capture/old.py").unlink()
        self.write("src/capture/new.py", "def parse(data):\n    return data[:4]\n", root=worktree)
        self.write(f"docs/investigations/{TASK}.md", f"# Replace\n\nTask `{TASK}` on base x. Topic: `new`.\n",
                   root=worktree)
        self.write(".theseus/handoff.md", f"Task: {TASK}\nResult: delivered\n", root=worktree)
        digest = hashlib.sha256((worktree / "src/capture/new.py").read_bytes()).hexdigest()
        self.write(".theseus/changed.sha256", f"{digest}  src/capture/new.py\nD src/capture/old.py\n", root=worktree)
        self.theseus("merge", TASK)
        self.assertEqual("", self.git("status", "--porcelain").stdout)
        merge = self.record()["merge"]["commit"]
        self.assertNotEqual(0, self.git("cat-file", "-e", f"{merge}:src/capture/old.py", check=False).returncode,
                            "the deletion must be in the merge commit itself")
        self.theseus("tasks", "complete", TASK, "--summary", "replaced", "--check", "manual", "--log",
                     str(worktree / ".theseus/handoff.md"))

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

    def test_unmerge_reverts_the_merge_and_a_redelivery_merges_again(self):
        self.add_task()
        self.theseus("instance", "assign", TASK, "worker-1", "a")
        worktree = self.tmp / "worktrees/worker-1-a"
        self.deliver(worktree)
        self.theseus("merge", TASK)
        first = self.record()["merge"]["commit"]
        self.theseus("tasks", "observe", TASK, "--how", "smoke run before the failure")
        self.write("docs/investigations/later-report.md",
                   "# Later\n\nTask `later-report` on base x. Topic: `new`.\n\n## Result\n\n- **Recovered**: magic at 0x00402b30.\n")
        self.theseus("docs", "index")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "a later report moves the generated indexes")
        refused = self.theseus("tasks", "reopen", TASK, "--reason", "smoke failed", check=False)
        self.assertIn("tasks unmerge", refused.stderr)
        refused = self.theseus("tasks", "unmerge", TASK, "--no-revert", check=False)
        self.assertEqual(3, refused.returncode, refused.stdout + refused.stderr)
        self.assertIn("src/capture/header.py", refused.stderr)
        self.write("src/capture/header.py", "def parse(data):\n    return data[:8]\n")
        self.git("commit", "-q", "-am", "main edits the delivered file")
        head = self.git("rev-parse", "HEAD").stdout
        refused = self.theseus("tasks", "unmerge", TASK, check=False)
        self.assertEqual(3, refused.returncode, refused.stdout + refused.stderr)
        self.assertIn("src/capture/header.py", refused.stderr)
        self.assertEqual((head, ""), (self.git("rev-parse", "HEAD").stdout, self.git("status", "--porcelain").stdout))
        self.assertEqual("merged", self.record()["status"])
        self.git("revert", "--no-edit", "HEAD")

        out = self.theseus("tasks", "unmerge", TASK, "--reason", "verify HEAD failed the smoke").stdout
        self.assertEqual("", self.git("status", "--porcelain").stdout)
        self.assertFalse((self.project / "src/capture/header.py").exists())
        self.assertFalse((self.project / "README.md").exists())
        index = (self.project / "docs/re/citation-index.md").read_text()
        self.assertIn("0x00402b30", index)
        self.assertNotIn("0x00401a20", index)
        self.theseus("docs", "check")
        self.theseus("check")
        record = self.record()
        self.assertEqual(("in_progress", str(worktree)), (record["status"], record["worktree"]))
        self.assertNotIn("merge", record)
        self.assertNotIn("evidence", record)
        attempt = record["previousAttempts"][-1]
        self.assertEqual(first, attempt["merge"]["commit"])
        self.assertIn(attempt["revertCommit"], out)
        self.assertIn(f"Revert the {TASK} merge {first}", self.git("log", "-1", "--format=%B", attempt["revertCommit"]).stdout)
        from theseus import config as cfg, workqueue
        self.assertEqual(1, len(workqueue.events(cfg.load(root=self.project), task=TASK, kinds=["unmerged"])))

        self.write("src/capture/header.py", "def parse(data):\n    return bytes(data[:4])\n", root=worktree)
        self.theseus("merge", TASK)
        second = self.record()["merge"]["commit"]
        self.assertNotEqual(first, second)
        self.assertIn("bytes(data[:4])", (self.project / "src/capture/header.py").read_text())

        self.git("revert", "--no-edit", second)
        refused = self.theseus("tasks", "unmerge", TASK, check=False)
        self.assertIn("--no-revert", refused.stderr)
        self.assertEqual("", self.git("status", "--porcelain").stdout)
        self.theseus("tasks", "unmerge", TASK, "--no-revert", "--reason", "reverted by hand")
        self.assertEqual("in_progress", self.record()["status"])
        self.assertEqual("", self.git("status", "--porcelain").stdout)

    def test_reassigning_to_the_same_agent_moves_the_previous_worktree_aside(self):
        self.add_task()
        self.theseus("instance", "assign", TASK, "worker-1", "a")
        worktree = self.tmp / "worktrees/worker-1-a"
        self.deliver(worktree)
        self.theseus("merge", TASK)
        self.git("revert", "--no-edit", self.record()["merge"]["commit"])
        self.theseus("tasks", "unmerge", TASK, "--no-revert", "--reason", "reverted by hand")
        released = self.theseus("tasks", "release", TASK, "--reason", "start over").stdout
        aside = self.tmp / "worktrees/worker-1-a-attempt1"
        self.assertIn(f"the worktree stays at {worktree}", released)
        self.assertIn(str(aside), released)

        out = self.theseus("instance", "assign", TASK, "worker-1", "a").stdout
        self.assertIn(f"previous attempt's worktree moved to {aside}", out)
        record = self.record()
        self.assertEqual(("in_progress", str(worktree)), (record["status"], record["worktree"]))
        self.assertEqual(str(aside), record["previousAttempts"][-1]["worktree"])
        self.assertEqual(str(aside / ".theseus"), record["previousAttempts"][-1]["execution"]["logDir"])
        self.assertEqual("work/worker-1-a", self.git("branch", "--show-current", cwd=worktree).stdout.strip())
        self.assertEqual("work/worker-1-a-attempt1", self.git("branch", "--show-current", cwd=aside).stdout.strip())
        self.assertTrue((aside / "src/capture/header.py").exists(), "the earlier work is kept")
        self.assertEqual("", self.git("status", "--porcelain").stdout)

        self.theseus("tasks", "release", TASK, "--reason", "elsewhere")
        (self.tmp / "worktrees/worker-2-a").mkdir()
        refused = self.theseus("instance", "assign", TASK, "worker-2", "a", check=False)
        self.assertIn("worktree already exists", refused.stderr)
        self.assertIn("not a previous attempt", refused.stderr)

    def test_in_place_copies_of_several_patches_merge_once_but_a_stale_copy_is_refused(self):
        lines = "".join(f"line {n}\n" for n in range(1, 13))
        self.write("shared.txt", lines)
        self.write("config.txt", lines)
        self.git("add", "shared.txt", "config.txt")
        self.git("commit", "-q", "-m", "shared files")
        self.add_task()
        self.theseus("instance", "assign", TASK, "worker-1", "a")
        worktree = self.tmp / "worktrees/worker-1-a"
        self.write("config.txt", lines.replace("line 1\n", "line 1 from a newer merge\n"))
        self.git("commit", "-q", "-am", "newer merge")
        self.deliver(worktree)
        text = lines
        for number, line in enumerate(("line 3", "line 6", "line 10"), 2):
            text = text.replace(line + "\n", line + " patched\n")
            self.write("shared.txt", text, root=worktree)
            self.write(f".theseus/patches/0{number}-shared.patch", self.git("diff", "--", "shared.txt", cwd=worktree).stdout, root=worktree)
            self.git("add", "shared.txt", cwd=worktree)
        self.git("reset", "-q", cwd=worktree)
        self.write("config.txt", lines.replace("line 10\n", "line 10 patched\n"), root=worktree)
        self.write(".theseus/patches/05-config.patch", self.git("diff", "--", "config.txt", cwd=worktree).stdout, root=worktree)
        self.write("config.txt", lines.replace("line 1\n", "line 1 from an older base\n").replace("line 10\n", "line 10 patched\n"), root=worktree)
        refused = self.theseus("merge", TASK, check=False)
        self.assertEqual(3, refused.returncode, refused.stdout + refused.stderr)
        self.assertIn(f"git -C {worktree} checkout -- config.txt", refused.stderr)
        self.assertNotIn("copies of shared.txt", refused.stderr)
        self.git("checkout", "--", "config.txt", cwd=worktree)
        merge = self.theseus("merge", TASK).stdout
        self.assertIn("applying the patches once", merge)
        self.assertEqual(text, (self.project / "shared.txt").read_text())
        self.assertEqual(lines.replace("line 1\n", "line 1 from a newer merge\n").replace("line 10\n", "line 10 patched\n"),
                         (self.project / "config.txt").read_text())
        self.assertEqual("", self.git("status", "--porcelain").stdout)


if __name__ == "__main__":
    unittest.main()
