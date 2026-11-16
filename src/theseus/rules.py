"""Workflow rules the scaffolded Supervisor tests enforce. Each function returns problems (empty when the rule holds);
the project's tests in tools/Supervisor choose the budgets, allowlists and scanned directories."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import config as cfg
from . import tasks as t

TOLERANCE = t.TOLERANCE
SKIPPED_DIRS = {"bin", "obj", ".git", cfg.WORKER_LOG, "__pycache__", "node_modules", ".venv", "build", "dist"}
SHELL_SHEBANG = re.compile(rb"^#!\s*(?:/usr/bin/env\s+)?(?:\S*/)?(?:bash|sh)\b")
LOCAL_FILES = {"env.local.sh"}


def hard(budget):
    return int(budget * (1 + TOLERANCE))


def size(root, relative):
    return len((Path(root) / relative).read_bytes())


def skill_files(root):
    return sorted(p.relative_to(root).as_posix() for p in (Path(root) / cfg.SKILLS).glob("*/SKILL.md"))


def budget_rows(root, file_budgets, default_skill_budget, read_sets):
    """(name, bytes, budget) for every budgeted file, every skill (project skills get the default) and each read set."""
    budgets = dict(file_budgets)
    for skill in skill_files(root):
        budgets.setdefault(skill, default_skill_budget)
    rows = [(name, size(root, name), budget) for name, budget in budgets.items() if (Path(root) / name).exists()]
    missing = [name for name in file_budgets if not (Path(root) / name).exists()]
    for name, (files, budget) in read_sets.items():
        rows.append((name, sum(size(root, f) for f in files if (Path(root) / f).exists()), budget))
    return rows, missing


def budget_problems(rows):
    return [f"{name} is {value} bytes, over its {budget}-byte budget by more than {TOLERANCE:.0%}; prune before adding"
            for name, value, budget in rows if value > hard(budget)]


def budget_warnings(rows):
    return [f"budget warning: {name} is {value} bytes, over its {budget}-byte budget (fails above {hard(budget)})"
            for name, value, budget in rows if budget < value <= hard(budget)]


def _acyclic(by_id):
    problems, state = [], {}

    def visit(task_id, trail):
        if state.get(task_id) == "done":
            return
        if state.get(task_id) == "visiting":
            problems.append("dependency cycle: " + " -> ".join(trail + [task_id]))
            return
        state[task_id] = "visiting"
        for dependency in by_id.get(task_id, {}).get("dependencies", []):
            if dependency in by_id:
                visit(dependency, trail + [task_id])
        state[task_id] = "done"

    for task_id in by_id:
        visit(task_id, [])
    return problems


def task_record_problems(conf):
    queue = t.Queue(conf)
    document = queue.load()
    open_tasks, archived = document["tasks"], queue.archived()
    everything = open_tasks + archived
    by_id = {}
    problems = []
    if document.get("objectiveComplete"):
        problems.append("objectiveComplete is true; only the user declares the objective complete")
    for task in everything:
        task_id = task.get("id", "<missing id>")
        if task_id in by_id:
            problems.append(f"{task_id}: duplicate task id")
        by_id[task_id] = task
        status = task.get("status")
        if status not in t.STATUSES:
            problems.append(f"{task_id}: unknown status {status!r}")
        for field in ("objective", "ownedPaths", "acceptance"):
            if not task.get(field):
                problems.append(f"{task_id}: empty {field}")
        if len(task.get("acceptance") or []) > t.TASK_ACCEPTANCE_LIMIT:
            problems.append(f"{task_id}: more than {t.TASK_ACCEPTANCE_LIMIT} acceptance items")
        if status == "completed" and (task.get("owner") is None or task.get("result") is None):
            problems.append(f"{task_id}: completed without owner or result")
        if status in ("in_progress", "verification", "merged") and task.get("owner") is None:
            problems.append(f"{task_id}: {status} without owner")
        if status == "merged" and not (task.get("merge") or {}).get("commit"):
            problems.append(f"{task_id}: merged without a merge commit")
        if task.get("result") is not None and status != "completed":
            problems.append(f"{task_id}: has a result but is {status}")
        for key in ("externalBlocker", "researchRequired"):
            record = task.get(key)
            if record is None:
                continue
            if status != "pending" or task.get("owner") is not None:
                problems.append(f"{task_id}: {key} on a task that is not pending and unowned")
            if not isinstance(record, dict) or not record.get("evidence") or not record.get("unblockCondition"):
                problems.append(f"{task_id}: {key} needs evidence and unblockCondition")
        if task.get("externalBlocker") and task.get("researchRequired"):
            problems.append(f"{task_id}: researchRequired is assignable work, not an external blocker; keep one")
        for dependency in task.get("dependencies", []):
            if dependency == task_id:
                problems.append(f"{task_id}: depends on itself")
            elif dependency not in {x.get("id") for x in everything}:
                problems.append(f"{task_id}: unknown dependency {dependency}")
    problems += _acyclic(by_id)
    from . import rooms
    problems += rooms.rule_problems(conf)
    for task in open_tasks:
        if task.get("status") not in t.OPEN:
            problems.append(f"{task['id']}: {task.get('status')} task left in the queue; run `theseus tasks archive`")
        if t.record_size(task) > t.TASK_RECORD_LIMIT:
            problems.append(f"{task['id']}: record is {t.record_size(task)} bytes, over {t.TASK_RECORD_LIMIT}")
        if task.get("status") in ("pending", "in_progress") and not any(
                p.startswith(t.REPORT_PREFIX) for p in task.get("ownedPaths", [])):
            problems.append(f"{task['id']}: owns no report under {t.REPORT_PREFIX}; workers always write one")
    for task in archived:
        if task.get("status") != "completed":
            problems.append(f"{task['id']}: archived but {task.get('status')}")
    current = document.get("currentMilestone")
    active = t.active_by_provider(open_tasks)
    for provider, count in active.items():
        capacity = t.provider_capacity(conf, provider)
        if capacity is None:
            problems.append(f"{count} active task(s) use unknown agent profile {provider!r}")
        elif count > int(capacity or 0):
            problems.append(f"{count} active {provider} tasks exceed workers.{provider}.capacity={capacity}")
    running = [x for x in open_tasks if x.get("status") == "in_progress"]
    for index, task in enumerate(running):
        for other in running[index + 1:]:
            if t.ownership_overlaps(task.get("ownedPaths", []), other.get("ownedPaths", [])):
                problems.append(f"{task['id']} and {other['id']} are both in progress with overlapping ownedPaths")
    for task in open_tasks:
        if task.get("status") == "in_progress":
            if not t.in_milestone(task, current):
                problems.append(f"{task['id']}: in progress outside the current milestone {current!r}; work one milestone at a time")
            for dependency in task.get("dependencies", []):
                record = by_id.get(dependency)
                if record and record.get("status") not in t.SATISFIES_DEPENDENCY:
                    problems.append(f"{task['id']}: in progress while dependency {dependency} is {record.get('status')}")
    return problems


def developer_needles(conf):
    """Strings that must not appear in committed tooling: the home directory, the original's path, the data root."""
    needles = {str(Path.home()) + "/"}
    needles.update(str(p) for p in conf.original if str(p) not in ("", "/"))
    needles.add(str(conf.data))
    return sorted(n for n in needles if len(n) > 3)


def path_offenders(root, needles, scanned=("tools",), suffixes=None, skip=None):
    suffixes = suffixes or {".py", ".sh", ".json", ".toml", ".cfg", ".ini", ".c", ".h", ".cpp", ".cs", ".java", ".ts", ".js", ".rs", ".go"}
    skip = set(skip or ()) | {"theseus.local.toml"}
    found = set()
    for top in scanned:
        base = Path(root) / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if not path.is_file() or path.suffix not in suffixes or relative in skip or path.name in skip:
                continue
            if SKIPPED_DIRS.intersection(path.relative_to(root).parts):
                continue
            text = path.read_text(errors="replace")
            if any(needle in text for needle in needles):
                found.add(relative)
    return found


def is_shell_script(path):
    if path.suffix == ".sh":
        return True
    if path.suffix:
        return False
    with path.open("rb") as handle:
        return bool(SHELL_SHEBANG.match(handle.readline(256)))


def shell_scripts(base):
    base = Path(base)
    found = []
    if not base.is_dir():
        return found
    for path in sorted(base.rglob("*")):
        if SKIPPED_DIRS.intersection(path.relative_to(base).parts[:-1]) or path.name in LOCAL_FILES:
            continue
        if path.is_file() and not path.is_symlink() and is_shell_script(path):
            found.append(path)
    return found


def syntax_errors(paths):
    failures = []
    for path in paths:
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        if result.returncode != 0:
            failures.append(f"{path}: {result.stderr.strip()}")
    return failures


def shellcheck_available():
    return shutil.which("shellcheck") is not None


def shellcheck_errors(paths, cwd=None):
    if not paths:
        return []
    result = subprocess.run(["shellcheck", "-x", "-S", "warning", "-f", "gcc", *map(str, paths)],
                            capture_output=True, text=True, cwd=cwd)
    if result.returncode == 0:
        return []
    output = (result.stdout + result.stderr).strip()
    return output.splitlines() or [f"shellcheck exited {result.returncode}"]


def governance_files(root):
    root = Path(root)
    files = [root / "AGENTS.md", root / cfg.TASKS]
    files += sorted((root / "docs/project").glob("*.md"))
    files += sorted((root / cfg.SKILLS).rglob("*.md"))
    return [f for f in files if f.is_file()]


def scratch_script_references(conf, files=None):
    """Reusable scripts live in the repository: governance docs, skills and task records must not point at scripts
    under the scratch root or a temporary directory."""
    roots = {"/tmp/", "/var/tmp/", "/dev/shm/", str(conf.scratch).rstrip("/") + "/"}
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        roots.add(tmpdir.rstrip("/") + "/")
    variables = r"\$\{?(?:TMPDIR|THESEUS_SCRATCH)\}?/"
    pattern = re.compile(r"(?:" + "|".join(re.escape(r) for r in sorted(roots)) + "|" + variables + r")[^\s`'\")]*\.(?:sh|bash|py|ps1|js|rb|pl)\b")
    problems = []
    for path in files if files is not None else governance_files(conf.root):
        for number, line in enumerate(Path(path).read_text(errors="replace").splitlines(), 1):
            for match in pattern.finditer(line):
                problems.append(f"{Path(path).relative_to(conf.root)}:{number}: {match.group(0)} is a script outside the repository; commit it under tools/")
    return problems


def scenario_named(root, words, production=("src",), test_markers=("/tests/", "/Tests/", "/test_")):
    if not words:
        return set()
    pattern = re.compile(words, re.IGNORECASE)
    found = set()
    for top in production:
        base = Path(root) / top
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if not path.is_file() or any(marker in "/" + relative for marker in test_markers):
                continue
            if SKIPPED_DIRS.intersection(path.relative_to(root).parts):
                continue
            if pattern.search(path.name):
                found.add(relative)
    return found


def load_json(path):
    return json.loads(Path(path).read_text())
