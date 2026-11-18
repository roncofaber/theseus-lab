"""Task queue: docs/project/tasks.json holds policy plus open tasks, docs/project/tasks-archive.jsonl the completed ones.

Lifecycle: pending -> in_progress (assigned to a worker) -> verification (worker delivered) -> merged (the coordinator
integrated the code; dependents may start) -> completed (verified against acceptance; archivable). Runtime observation
and user testing are recorded separately under `evidence`, because merged, observed at runtime and user-tested are
different claims.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import config as cfg

OPEN = ("pending", "in_progress", "verification", "merged")
STATUSES = OPEN + ("completed",)
SATISFIES_DEPENDENCY = ("merged", "completed")
ACTIVE = ("in_progress",)
TASK_ACCEPTANCE_LIMIT = 6
TASK_RECORD_BUDGET = 6000
TOLERANCE = 0.15
TASK_RECORD_LIMIT = int(TASK_RECORD_BUDGET * (1 + TOLERANCE))
TASK_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
REPORT_PREFIX = cfg.REPORTS + "/"
EVIDENCE_LEVELS = ("observed", "userTested")


class TaskError(Exception):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def agent_of(task):
    """The registered agent for persistent-instance owners (profile:agent:worktree), else the worker name."""
    owner = (task or {}).get("owner") or ""
    parts = owner.split(":")
    if len(parts) >= 3:
        return parts[1] if (task.get("execution") or {}).get("model") == "persistent-instance" else parts[-1]
    return owner or None


def owner_id_of(conf, task):
    """The ID of the agent registered now under the task's owner name, or None for a worker no registry lists."""
    from . import store
    try:
        with store.for_project(conf) as conn:
            return store.agent_id(conn, conf.name, agent_of(task))
    except (OSError, sqlite3.Error):
        return None


def record(queue, kind, task, detail=""):
    """Append the status change to the queue's event history, so `theseus top` shows it however it was made."""
    from . import workqueue
    try:
        workqueue.event(queue.conf, kind, agent_of(task), task["id"], detail, agent_id=task.get("ownerId"))
    except (OSError, sqlite3.Error):
        pass


def record_delivery(queue, task, handoff, result):
    from . import store
    try:
        with store.for_project(queue.conf) as conn:
            store.add_delivery(conn, queue.conf.name, task["id"], agent=agent_of(task), handoff=handoff, result=result,
                               agent_id=task.get("ownerId"))
    except (OSError, sqlite3.Error):
        pass


class Queue:
    def __init__(self, conf):
        self.conf = conf
        self.root = conf.root
        self.path = conf.tasks_file
        self.archive_path = conf.archive_file

    def load(self):
        return json.loads(self.path.read_text())

    def save(self, document):
        self.path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")

    def archived(self):
        if not self.archive_path.exists():
            return []
        return [json.loads(line) for line in self.archive_path.read_text().splitlines() if line.strip()]

    def all(self):
        return self.load()["tasks"] + self.archived()

    def index(self):
        return {task["id"]: task for task in self.all()}

    def get(self, task_id):
        task = self.index().get(task_id)
        if task is None:
            raise TaskError(f"unknown task {task_id}")
        return task

    def open_task(self, document, task_id):
        task = next((t for t in document["tasks"] if t["id"] == task_id), None)
        if task is None:
            raise TaskError(f"{task_id} is not an open task")
        return task

    def milestone(self):
        return self.load().get("currentMilestone")


def in_milestone(task, current):
    return not current or task.get("milestone") in (None, "", current)


def blocking(task, index, current=None):
    reasons = [key for key in ("externalBlocker", "researchRequired", "pause") if task.get(key)]
    if not in_milestone(task, current):
        reasons.append(f"milestone:{task.get('milestone')}")
    for dependency in task.get("dependencies", []):
        record = index.get(dependency)
        if record is None:
            reasons.append(f"missing:{dependency}")
        elif record["status"] not in SATISFIES_DEPENDENCY:
            reasons.append(f"waits:{dependency}")
    return reasons


def dependency_ready(task, index, current=None):
    return task["status"] == "pending" and not blocking(task, index, current)


def evidence_level(task):
    evidence = task.get("evidence") or {}
    if evidence.get("userTested"):
        return "user-tested"
    if evidence.get("observed"):
        return "observed"
    if task["status"] in ("merged", "completed"):
        return "merged"
    return "-"


def list_tasks(queue, status=None, include_archive=False, show_evidence=False):
    index = queue.index()
    current = queue.milestone()
    tasks = queue.all() if include_archive else queue.load()["tasks"]
    lines = []
    for task in tasks:
        if status and task["status"] != status:
            continue
        flags = blocking(task, index, current) + (["ready"] if dependency_ready(task, index, current) else [])
        line = f"{task['status']:<12} {task['id']}  owner={task.get('owner') or '-'}  {','.join(flags)}"
        if show_evidence:
            line += f"  evidence={evidence_level(task)}"
        lines.append(line.rstrip())
    return lines


def ready(queue, include_paused=False, any_milestone=False):
    index = queue.index()
    current = None if any_milestone else queue.milestone()
    result = []
    for task in queue.load()["tasks"]:
        candidate = {**task, "pause": None} if include_paused else task
        if dependency_ready(candidate, index, current):
            result.append(task["id"])
    return result


def record_size(record):
    return len(json.dumps(record))


def check_profile(queue, profile):
    from . import profiles
    try:
        profiles.resolve(queue.conf, profile)
    except profiles.ProfileError as error:
        raise TaskError(str(error)) from error


def add(queue, task_id, role, owned, objective, contract, accept, depends=(), milestone=None, room=None, priority=0,
        profile=None, min_capability=0):
    if not TASK_ID.fullmatch(task_id):
        raise TaskError(f"{task_id} is not a kebab-case task id")
    known = queue.index()
    if task_id in known:
        raise TaskError(f"{task_id} already exists in the queue or the archive")
    if not any(path.startswith(REPORT_PREFIX) and path.endswith(".md") for path in owned):
        raise TaskError(f"ownedPaths must include a report under {REPORT_PREFIX} ending in .md")
    blank = [name for name, value in (("--role", role), ("--objective", objective), ("--contract", contract))
             if not value.strip()]
    if any(not item.strip() for item in accept):
        blank.append("--accept")
    if blank:
        raise TaskError(f"empty value for {', '.join(blank)}")
    unknown = [dependency for dependency in depends if dependency not in known]
    if unknown:
        raise TaskError(f"unknown dependencies: {', '.join(unknown)}")
    if not accept:
        raise TaskError("at least one acceptance item is required")
    if len(accept) > TASK_ACCEPTANCE_LIMIT:
        raise TaskError(f"{len(accept)} acceptance items; the limit is {TASK_ACCEPTANCE_LIMIT}")
    document = queue.load()
    milestone = milestone if milestone is not None else document.get("currentMilestone")
    record = {
        "id": task_id, "role": role, "status": "pending", "owner": None, "createdAt": now(), "dependencies": list(depends),
        "ownedPaths": list(owned), "objective": objective, "generalContract": contract, "acceptance": list(accept),
        "sharedFilesReadOnly": True, "result": None,
    }
    if milestone:
        record["milestone"] = milestone
    if priority:
        record["priority"] = int(priority)
    if room:
        if room not in ("clean", "dirty"):
            raise TaskError(f"room must be clean or dirty, not {room}")
        record["room"] = room
    if profile:
        check_profile(queue, profile)
        record["profile"] = profile
    if int(min_capability or 0) < 0:
        raise TaskError("--min-capability must be 0 or more")
    if min_capability:
        record["minCapability"] = int(min_capability)
    size = record_size(record)
    if size > TASK_RECORD_LIMIT:
        raise TaskError(f"record is {size} bytes; the limit is {TASK_RECORD_LIMIT}")
    document["tasks"].append(record)
    queue.save(document)
    return f"{task_id} added as pending ({len(accept)} acceptance items, {size} bytes)"


def provider_capacity(conf, provider):
    from . import profiles
    try:
        return profiles.resolve(conf, provider).get("capacity")
    except profiles.ProfileError:
        return None


def capacity_use(conf, tasks=None):
    """profile -> (active tasks, capacity or None) for every profile with active tasks or a configured capacity."""
    tasks = tasks if tasks is not None else Queue(conf).load()["tasks"]
    active = active_by_provider(tasks)
    from . import profiles
    names = set(active) | set(profiles.names(conf))
    return {n: (active.get(n, 0), provider_capacity(conf, n)) for n in names if n}


def active_by_provider(tasks):
    counts = {}
    for task in tasks:
        if task["status"] in ACTIVE:
            provider = (task.get("execution") or {}).get("provider")
            counts[provider] = counts.get(provider, 0) + 1
    return counts


def attempt_of(task, keys):
    """A snapshot of the current attempt; the owner's ID is kept as agentId."""
    attempt = {key: task.get(key) for key in keys}
    if task.get("ownerId"):
        attempt["agentId"] = task["ownerId"]
    return attempt


def assign(queue, task_id, owner, provider, model, worktree, base, log_dir, force=False, owner_id=None):
    document = queue.load()
    task = queue.open_task(document, task_id)
    if task.get("pause") and not force:
        raise TaskError(f"{task_id} is paused with preserved work ({task['pause'].get('state')}); pass --force to start fresh")
    if task["status"] != "pending" and not force:
        raise TaskError(f"{task_id} is {task['status']}; pass --force to reassign")
    index = queue.index()
    current = document.get("currentMilestone")
    if not dependency_ready(task, index, current) and not force:
        raise TaskError(f"{task_id} is not ready: {', '.join(blocking(task, index, current))}; pass --force to override")
    capacity = provider_capacity(queue.conf, provider)
    if capacity is not None and not force and active_by_provider(document["tasks"]).get(provider, 0) >= int(capacity):
        raise TaskError(f"provider {provider} is at its capacity of {capacity} active tasks (workers.{provider}.capacity)")
    if task.get("worktree") or task.get("execution") or task.get("pause"):
        attempt = {key: value for key, value in attempt_of(task, ("owner", "worktree", "baseCommit", "execution", "pause")).items()
                   if value is not None}
        attempt.update({"supersededAt": now(), "reason": "reassigned"})
        task.setdefault("previousAttempts", []).append(attempt)
        task.pop("pause", None)
    task.update({"status": "in_progress", "owner": owner, "worktree": str(worktree), "baseCommit": base,
                 "execution": {"provider": provider, "model": model, "logDir": str(log_dir), "assignedAt": now()}})
    task.pop("ownerId", None)
    owner_id = owner_id or owner_id_of(queue.conf, task)
    if owner_id:
        task["ownerId"] = owner_id
    queue.save(document)
    record(queue, "assigned", task, f"{provider}: {worktree}")
    return f"{task_id} -> in_progress ({owner})"


def migrate_ids(queue):
    """Record ownerId on open tasks and agentId on their earlier attempts where the agent registered under the owner
    name now was already registered when the attempt was assigned (so it is the same agent). Idempotent."""
    from . import store
    with store.for_project(queue.conf) as conn:
        registered = {a["name"]: a for a in store.list_agents(conn, queue.conf.name)}

    def same_agent(record):
        agent = registered.get(agent_of(record)) or {}
        assigned = (record.get("execution") or {}).get("assignedAt")
        if not (agent.get("id") and agent.get("registered") and assigned):
            return None
        return agent["id"] if store.epoch_of(agent["registered"]) <= store.epoch_of(assigned) else None

    document = queue.load()
    owners = attempts = 0
    for task in document["tasks"]:
        if task.get("owner") and not task.get("ownerId") and same_agent(task):
            task["ownerId"] = same_agent(task)
            owners += 1
        for attempt in task.get("previousAttempts") or []:
            if attempt.get("owner") and not attempt.get("agentId") and same_agent(attempt):
                attempt["agentId"] = same_agent(attempt)
                attempts += 1
    if owners or attempts:
        queue.save(document)
    return f"ownerId recorded on {owners} task(s), agentId on {attempts} attempt(s)"


def kept_worktree(queue, task_id, worktree):
    """Where an attempt's worktree stays and what the next assignment of the task does with it."""
    path = Path(worktree) if worktree else None
    if path is None or not path.is_dir() or path.resolve() == Path(queue.root).resolve():
        return ""
    n = 1
    while Path(f"{path}-attempt{n}").exists():
        n += 1
    return (f"\nthe worktree stays at {path}; the next assignment of {task_id} to the same name moves it to "
            f"{path}-attempt{n}")


def release(queue, task_id, reason):
    document = queue.load()
    task = queue.open_task(document, task_id)
    if task["status"] not in ("in_progress", "verification"):
        raise TaskError(f"{task_id} is {task['status']}, not assigned")
    attempt = attempt_of(task, ("owner", "worktree", "baseCommit", "execution"))
    attempt.update({"releasedAt": now(), "reason": reason})
    task.setdefault("previousAttempts", []).append(attempt)
    task["status"], task["owner"] = "pending", None
    for key in ("worktree", "baseCommit", "execution", "ownerId"):
        task.pop(key, None)
    queue.save(document)
    record(queue, "released", {**task, "owner": attempt.get("owner"), "ownerId": attempt.get("agentId"),
                               "execution": attempt.get("execution")}, reason)
    return f"{task_id} -> pending (attempt recorded in previousAttempts)" + kept_worktree(queue, task_id, attempt.get("worktree"))


HANDOFF_RESULT = re.compile(r"^\W*Result\W*:?\W*(delivered|partial|blocked)\b", re.IGNORECASE | re.MULTILINE)


def handoff_result(path):
    """delivered, partial or blocked from a handoff's Result line, or None when it states none."""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return None
    match = HANDOFF_RESULT.search(text)
    return match.group(1).lower() if match else None


def deliver(queue, task_id, handoff):
    document = queue.load()
    task = queue.open_task(document, task_id)
    if task["status"] == "verification":
        # A queue tick can record the delivery between merge's first read and this call.
        return f"{task_id} is already delivered"
    if task["status"] != "in_progress":
        raise TaskError(f"{task_id} is {task['status']}, not in_progress")
    task["status"] = "verification"
    task.setdefault("execution", {}).update({"handoff": str(handoff), "deliveredAt": now()})
    queue.save(document)
    result = handoff_result(handoff)
    kind = "delivered" if result in ("delivered", None) else result
    record(queue, kind, task, f"handoff result {result or 'not stated'}")
    record_delivery(queue, task, handoff, kind)
    return f"{task_id} -> verification; the worker slot is free, dependents wait for the merge"


def reopen(queue, task_id, reason):
    """Undo a premature delivery: verification back to in_progress, keeping the note in previousAttempts."""
    document = queue.load()
    task = queue.open_task(document, task_id)
    if task["status"] == "merged":
        raise TaskError(f"{task_id} is merged; take it back with `theseus tasks unmerge {task_id}`")
    if task["status"] != "verification":
        raise TaskError(f"{task_id} is {task['status']}, not verification")
    task["status"] = "in_progress"
    handoff = (task.get("execution") or {}).pop("handoff", None)
    task.setdefault("previousAttempts", []).append({"reopenedAt": now(), "reason": reason, "handoff": handoff})
    queue.save(document)
    record(queue, "reopened", task, reason)
    return f"{task_id} -> in_progress again ({reason})"


def unmerge(queue, task_id, reason, revert_commit=None):
    """Merged back to in_progress after the merge was reverted; owner and worktree stay for a redelivery."""
    document = queue.load()
    task = queue.open_task(document, task_id)
    if task["status"] != "merged":
        raise TaskError(f"{task_id} is {task['status']}, not merged")
    attempt = {"merge": task.pop("merge", None), "unmergedAt": now(), "reason": reason,
               "handoff": (task.get("execution") or {}).pop("handoff", None)}
    if revert_commit:
        attempt["revertCommit"] = revert_commit
    if task.get("evidence"):
        attempt["evidence"] = task.pop("evidence")
    task.setdefault("previousAttempts", []).append(attempt)
    task["status"] = "in_progress"
    queue.save(document)
    record(queue, "unmerged", task, f"{reason} (revert {revert_commit or 'by hand'})")
    return (f"{task_id} -> in_progress again; redeliver in {task.get('worktree')} and merge as usual, or release it"
            + kept_worktree(queue, task_id, task.get("worktree")))


def rebase(queue, task_id, base):
    """Record a new base after the worker moved its worktree HEAD along the base branch."""
    document = queue.load()
    task = queue.open_task(document, task_id)
    if task["status"] not in ("in_progress", "verification"):
        raise TaskError(f"{task_id} is {task['status']}; only an assigned or delivered task can be rebased")
    old = task.get("baseCommit")
    task["baseCommit"] = base
    queue.save(document)
    record(queue, "rebased", task, f"base {old} -> {base}")
    return f"{task_id}: worktree rebased, base {old} -> {base}"


def mark_merged(queue, task_id, commit):
    document = queue.load()
    task = queue.open_task(document, task_id)
    if task["status"] not in ("in_progress", "verification"):
        raise TaskError(f"{task_id} is {task['status']}; only assigned or delivered tasks can be merged")
    task["status"] = "merged"
    task["merge"] = {"commit": commit, "mergedAt": now()}
    queue.save(document)
    record(queue, "merged", task, f"in {commit}; verify, then theseus tasks complete")
    return f"{task_id} -> merged in {commit}; record runtime evidence with `theseus tasks observe` and verify before completing"


def observe(queue, task_id, how, level="observed"):
    if level not in EVIDENCE_LEVELS:
        raise TaskError(f"unknown evidence level {level}")
    document = queue.load()
    task = queue.open_task(document, task_id)
    if task["status"] not in ("merged", "completed"):
        raise TaskError(f"{task_id} is {task['status']}; observe a merged task")
    if not how.strip():
        raise TaskError("say how it was observed: command, build, scenario, tester")
    task.setdefault("evidence", {})[level] = {"at": now(), "how": how}
    queue.save(document)
    return f"{task_id}: {level} recorded"


def verify_log_evidence(path):
    """Fill commit, check and log from a passing `theseus verify` log; refuse anything else."""
    from . import verify
    text = Path(path).read_text(errors="replace")
    commit = verify.parse_log_commit(text)
    if commit is None:
        raise TaskError(f"{path} is not a theseus verify log")
    if not verify.log_passed(text):
        raise TaskError(f"{path} does not record a passing full verification; refusing to complete")
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return commit, f"theseus verify {commit[:12]} -> exit 0", f"{path} {digest}"


def manifest_entries(worktree):
    manifest = Path(worktree) / cfg.WORKER_LOG / "changed.sha256"
    if not manifest.exists():
        return None
    entries = []
    for line in manifest.read_text().splitlines():
        if not line.strip():
            continue
        fields = line.split(maxsplit=1)
        if len(fields) != 2:
            entries.append(("MALFORMED", line.strip()))
            continue
        entries.append((fields[0], fields[1].strip().lstrip("*")))
    return entries


def unintegrated_files(worktree, root, ref="HEAD"):
    """Manifest files whose content the given commit (the task's merge commit, else HEAD) does not hold: removing the
    worktree would lose them. Later commits may legitimately change a file again, so HEAD alone is not the reference."""
    missing = []
    for digest, name in manifest_entries(worktree) or []:
        if digest == "MALFORMED" or name.startswith((cfg.WORKER_LOG, "/")) or name.endswith(".patch"):
            continue
        if digest in ("D", "DELETED"):
            gone = subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{ref}:{name}"], capture_output=True)
            if gone.returncode == 0:
                missing.append(name)
            continue
        path = Path(worktree) / name
        shown = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{name}"], capture_output=True)
        if path.is_symlink():
            # Git stores a symlink as its target text, not the content it points to.
            if shown.returncode != 0 or shown.stdout != os.readlink(path).encode():
                missing.append(name)
            continue
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if shown.returncode != 0 or hashlib.sha256(shown.stdout).hexdigest() != digest:
            if not change_present(worktree, root, name):
                missing.append(name)
    return missing


def change_present(worktree, root, name):
    """True when the worker's change to a file it did not create (its diff against its base, the worktree HEAD)
    reverse-applies on main: main holds the change even though later commits changed the same file again."""
    tracked = subprocess.run(["git", "-C", str(worktree), "cat-file", "-e", f"HEAD:{name}"], capture_output=True)
    if tracked.returncode != 0:
        return False
    diff = subprocess.run(["git", "-C", str(worktree), "diff", "--binary", "HEAD", "--", name], capture_output=True)
    if diff.returncode != 0 or not diff.stdout.strip():
        return False
    check = subprocess.run(["git", "-C", str(root), "apply", "--reverse", "--check", "-"], input=diff.stdout,
                           capture_output=True)
    return check.returncode == 0


def complete(queue, task_id, summary, commit=None, checks=(), log=None, limits=(), from_log=None, force=False):
    document = queue.load()
    task = queue.open_task(document, task_id)
    if task["status"] not in ("verification", "merged") and not force:
        raise TaskError(f"{task_id} is {task['status']}; merge it first or pass --force")
    checks = list(checks)
    if from_log:
        verified, check, log = verify_log_evidence(from_log)
        checks.append(check)
        commit = commit or verified[:12]
    commit = commit or (task.get("merge") or {}).get("commit")
    if not commit or not log or not checks:
        raise TaskError("complete needs --from-log, or --log and at least one --check (the commit defaults to the merge commit)")
    worktree = task.get("worktree")
    if worktree and Path(worktree).is_dir() and not force:
        missing = unintegrated_files(worktree, queue.root, (task.get("merge") or {}).get("commit") or "HEAD")
        if missing:
            raise TaskError(f"{task_id} delivered files that main never committed: {', '.join(missing)}; integrate them or pass --force")
    task["status"] = "completed"
    task["result"] = {"integratedCommit": commit, "completedAt": now(), "summary": summary, "checks": checks,
                      "log": log, "limits": list(limits)}
    queue.save(document)
    record(queue, "completed", task, summary)
    return f"{task_id} -> completed; run `theseus tasks archive`"


def owned_pattern(pattern):
    parts = []
    for token in re.split(r"(\*\*/?|\*)", pattern):
        if token in ("**", "**/"):
            parts.append(".*")
        elif token == "*":
            parts.append("[^/]*")
        else:
            parts.append(re.escape(token))
    body = "".join(parts)
    if pattern.endswith("/"):
        body += ".*"
    elif "*" not in pattern:
        # An owned directory written without a trailing slash still owns everything under it.
        body += "(?:/.*)?"
    return re.compile(body + "$")


def ownership_overlaps(first, second):
    """True when any owned pattern of one task could match a path owned by the other."""
    for a in first:
        for b in second:
            stem_a, stem_b = a.split("*")[0], b.split("*")[0]
            if stem_a.startswith(stem_b) or stem_b.startswith(stem_a):
                return True
    return False


def is_owned(path, patterns):
    return any(owned_pattern(p).match(path) for p in patterns)


def worktree_changes(worktree):
    status = subprocess.run(["git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=all"],
                            capture_output=True, text=True, check=True).stdout
    changes = []
    for line in status.splitlines():
        code, path = line[:2], line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        if path in (cfg.WORKER_LOG, cfg.LOCAL_NAME) or path.startswith(cfg.WORKER_LOG + "/"):
            continue
        changes.append((code, path))
    return changes


def manifest_mismatches(worktree):
    stale = []
    for digest, name in manifest_entries(worktree) or []:
        if digest == "MALFORMED":
            stale.append(f"malformed line '{name}'")
            continue
        target = Path(worktree) / name
        if digest == "DELETED":
            if target.exists() or target.is_symlink():
                stale.append(name)
            continue
        if target.is_symlink() or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            stale.append(name)
    return stale


def check_delivery(queue, task_id, accepted=()):
    """Lines describing each change, plus the paths outside ownedPaths that were not explicitly accepted."""
    task = queue.get(task_id)
    worktree = task.get("worktree")
    if not worktree or not Path(worktree).is_dir():
        raise TaskError(f"{task_id} has no live worktree")
    changes = worktree_changes(worktree)
    accepted = set(accepted)
    outside = [path for _, path in changes if not is_owned(path, task["ownedPaths"]) and path not in accepted]
    lines = []
    for code, path in changes:
        label = "OUTSIDE " if path in outside else ("accepted" if path in accepted else "owned   ")
        lines.append(f"{label}{code} {path}")
    entries = manifest_entries(worktree)
    stale = manifest_mismatches(worktree)
    manifest = "missing" if entries is None else ("matches" if not stale else f"differs for {', '.join(stale)}")
    lines.append(f"{len(changes)} changed, {len(outside)} outside owned paths, manifest: {manifest}")
    return lines, outside


def outside_consumers(queue, task):
    """Types declared in owned files and the files outside ownedPaths that mention them."""
    patterns = task.get("ownedPaths", [])
    declarations = queue.conf.consumer_patterns()
    tracked = subprocess.run(["git", "-C", str(queue.root), "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    tracked = [path for path in tracked if Path(path).suffix in declarations]
    names = set()
    for path in tracked:
        if is_owned(path, patterns):
            text = (queue.root / path).read_text(errors="replace")
            names.update(declarations[Path(path).suffix].findall(text))
    names = sorted(name for name in names if len(name) > 2)
    consumers = {}
    if not names:
        return names, consumers
    globs = [f"*{ext}" for ext in declarations]
    for start in range(0, len(names), 200):
        chunk = names[start:start + 200]
        found = subprocess.run(["git", "-C", str(queue.root), "grep", "-ownE", r"\b(" + "|".join(map(re.escape, chunk)) + r")\b",
                                "--", *globs], capture_output=True, text=True).stdout
        for line in found.splitlines():
            path, _, name = line.split(":", 2)
            if not is_owned(path, patterns):
                consumers.setdefault(path, set()).add(name)
    return names, consumers


def retire(queue, task_id, force=False):
    task = queue.get(task_id)
    worktree = task.get("worktree")
    released = False
    if not worktree and task["status"] == "pending":
        # A released task keeps its old worktree in previousAttempts; it would block the next assignment's worktree.
        worktree = next((a.get("worktree") for a in reversed(task.get("previousAttempts") or []) if a.get("worktree")), None)
        released = True
    if not worktree or not Path(worktree).is_dir():
        raise TaskError(f"{task_id} has no live worktree")
    if task["status"] != "completed" and not released and not force:
        raise TaskError(f"refusing to retire {task_id}: it is {task['status']}, not completed (complete it first, or pass --force)")
    missing = unintegrated_files(worktree, queue.root, (task.get("merge") or {}).get("commit") or "HEAD")
    if missing and not force:
        raise TaskError(f"refusing to remove {worktree}: main never committed {', '.join(missing)}; integrate them or pass --force")
    target = queue.conf.workers_dir / Path(worktree).name
    target.mkdir(parents=True, exist_ok=True)
    if (Path(worktree) / cfg.WORKER_LOG).is_dir():
        shutil.copytree(Path(worktree) / cfg.WORKER_LOG, target / cfg.WORKER_LOG, dirs_exist_ok=True)
    branch = subprocess.run(["git", "-C", str(worktree), "symbolic-ref", "--quiet", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(queue.root), "worktree", "remove", "--force", str(worktree)], check=True)
    note = ""
    if branch.startswith("work/") and (task["status"] in SATISFIES_DEPENDENCY or released):
        subprocess.run(["git", "-C", str(queue.root), "branch", "-D", branch], capture_output=True)
        note = f", deleted branch {branch}"
    return f"archived {worktree}/{cfg.WORKER_LOG} to {target} and removed the worktree{note}"


def worker_runs(conf):
    runs = []
    for launch in sorted(conf.workers_dir.glob("*/launch.json")):
        record = json.loads(launch.read_text())
        started = record.get("started")
        if not started:
            continue
        handoff = None
        for candidate in (launch.parent / cfg.WORKER_LOG / "handoff.md",
                          Path(record.get("worktree") or "/nonexistent") / cfg.WORKER_LOG / "handoff.md"):
            if candidate.exists():
                handoff = candidate
                break
        result, minutes = "none", None
        if handoff is not None:
            match = re.search(r"Result:\s*\**\s*([A-Za-z]+)", handoff.read_text(errors="replace"))
            result = match.group(1).lower() if match else "unparsed"
            begin = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
            end = datetime.datetime.fromtimestamp(handoff.stat().st_mtime, datetime.timezone.utc)
            minutes = max(0.0, (end - begin).total_seconds() / 60)
        followups = launch.parent / "followups.log"
        runs.append({"name": launch.parent.name, "task": record.get("task", ""), "provider": record.get("provider", ""),
                     "model": record.get("model", ""), "started": started, "minutes": minutes, "result": result,
                     "followups": len(followups.read_text().splitlines()) if followups.exists() else 0})
    return runs


def stats(conf, since=None):
    runs = [run for run in worker_runs(conf) if not since or run["started"] >= since]
    groups = {}
    for run in runs:
        groups.setdefault(f"{run['provider']}:{run['model']}", []).append(run)
    lines = [f"{'provider:model':38} {'runs':>4} {'delivered':>9} {'partial':>7} {'blocked':>7} {'none':>5} {'median min':>10} {'follow-ups':>10}"]
    for key, items in sorted(groups.items()):
        count = lambda word: sum(1 for item in items if item["result"] == word)
        durations = sorted(item["minutes"] for item in items if item["minutes"] is not None)
        median = durations[len(durations) // 2] if durations else float("nan")
        lines.append(f"{key:38} {len(items):>4} {count('delivered'):>9} {count('partial'):>7} {count('blocked'):>7} "
                     f"{count('none'):>5} {median:>10.0f} {sum(i['followups'] for i in items):>10}")
    return lines


def archive(queue):
    document = queue.load()
    keep, move = [], []
    for task in document["tasks"]:
        (move if task["status"] == "completed" else keep).append(task)
    if not move:
        return "nothing to archive"
    archived = {task["id"] for task in queue.archived()}
    duplicates = [task["id"] for task in move if task["id"] in archived]
    if duplicates or len({t["id"] for t in move}) != len(move):
        raise TaskError(f"duplicate task IDs between the queue and the archive: {', '.join(duplicates)}; resolve by hand")
    with open(queue.archive_path, "a") as handle:
        for task in move:
            handle.write(json.dumps(task, ensure_ascii=False, separators=(",", ":")) + "\n")
    document["tasks"] = keep
    queue.save(document)
    return f"archived {len(move)} completed task(s): {', '.join(t['id'] for t in move)}"


def note(queue, task_id, text):
    """Set the coordinator's assignment note, which task messages and prompts carry to the worker."""
    document = queue.load()
    task = queue.open_task(document, task_id)
    task["assignmentNote"] = text
    queue.save(document)
    return f"{task_id}: note set"


def set_priority(queue, task_id, priority):
    """Higher priority is assigned first among ready tasks; equal priorities keep task-file order."""
    document = queue.load()
    task = queue.open_task(document, task_id)
    if int(priority):
        task["priority"] = int(priority)
    else:
        task.pop("priority", None)
    queue.save(document)
    return f"{task_id}: priority {int(priority)}"


def set_profile(queue, task_id, profile):
    """The agent profile a task needs (only its agents, or ones the queue can relaunch onto it, take it); empty: any."""
    if profile:
        check_profile(queue, profile)
    document = queue.load()
    task = queue.open_task(document, task_id)
    if profile:
        task["profile"] = profile
    else:
        task.pop("profile", None)
    queue.save(document)
    return f"{task_id}: {f'needs profile {profile}' if profile else 'any profile'}"


def set_min_capability(queue, task_id, level):
    """The lowest profile capability an agent needs to take the task ([workers.<name>] capability); 0 removes it."""
    level = int(level)
    if level < 0:
        raise TaskError("the minimum capability must be 0 or more")
    document = queue.load()
    task = queue.open_task(document, task_id)
    if level:
        task["minCapability"] = level
    else:
        task.pop("minCapability", None)
    queue.save(document)
    return f"{task_id}: {f'needs capability >= {level}' if level else 'any capability'}"


def hold(queue, task_id, held):
    document = queue.load()
    task = queue.open_task(document, task_id)
    if held:
        task["hold"] = True
    else:
        task.pop("hold", None)
    queue.save(document)
    return f"{task_id}: {'held, the queue will not assign it' if held else 'released to the queue'}"


def set_milestone(queue, milestone):
    document = queue.load()
    document["currentMilestone"] = milestone or None
    queue.save(document)
    return f"current milestone: {milestone or 'none'}"


def render_prompt(queue, task_id, base, worktree, log_dir):
    task = queue.get(task_id)
    template = (queue.root / cfg.SKILLS / "theseus-worker/prompt-template.md").read_text()
    contract = task.get("generalContract")
    if isinstance(contract, (dict, list)):
        contract = json.dumps(contract, ensure_ascii=False)
    replacements = {
        "{{PROJECT}}": queue.conf.name,
        "{{TASK_ID}}": task["id"],
        "{{BASE_COMMIT}}": base,
        "{{WORKTREE}}": str(worktree),
        "{{LOG_DIR}}": str(log_dir),
        "{{OBJECTIVE}}": task["objective"],
        "{{GENERAL_CONTRACT}}": contract or "Not recorded; state the general contract you implement in the report.",
        "{{OWNED_PATHS}}": "\n".join(f"- `{path}`" for path in task["ownedPaths"]),
        "{{ACCEPTANCE}}": "\n".join(f"{n}. {item}" for n, item in enumerate(task["acceptance"], 1)),
        "{{DEPENDENCIES}}": ", ".join(task.get("dependencies", [])) or "none",
        "{{NOTES}}": task.get("assignmentNote") or "none",
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def fail(error):
    sys.exit(str(error))
