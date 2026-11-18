"""Task queue: the tasks live in the host store (theseus.taskservice); docs/project/tasks.json, tasks-archive.jsonl
and tasks-history.jsonl are their export, written by `theseus tasks export` and committed by the coordinator. This
module keeps the task commands' wording and the helpers that read worktrees and files.

Lifecycle: pending -> in_progress (assigned to a worker) -> verification (worker delivered) -> merged (the coordinator
integrated the code; dependents may start once the merge commit is in their assignment base) -> completed (verified
against acceptance; archivable). Runtime observation and user testing are recorded separately under `evidence`, because
merged, observed at runtime and user-tested are different claims.
"""
import contextlib
import contextvars
import datetime
import functools
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
from . import gitutil as g

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


def service(conf):
    from . import taskservice
    return taskservice.open(conf)


ACTING = contextvars.ContextVar("theseus_acting_principal", default=None)


def acting_principal(principal=None):
    """principal, else the one a command resolved (acting), else the coordinator (in-process callers on the host)."""
    from . import taskservice
    return principal or ACTING.get() or taskservice.COORDINATOR


@contextlib.contextmanager
def acting(principal):
    """Run the task calls inside as principal, the caller the CLI identified."""
    token = ACTING.set(principal)
    try:
        yield principal
    finally:
        ACTING.reset(token)


def refreshes_inbox(function):
    """After a change to an in-progress task (function(queue, task_id, ...)), rewrite its worker's copy."""
    @functools.wraps(function)
    def wrapper(queue, task_id, *args, **kwargs):
        result = function(queue, task_id, *args, **kwargs)
        write_inbox(queue, task_id)
        return result
    return wrapper


class Queue:
    """The project's tasks as the legacy tasks.json document and archive records, read from the host store. Changes
    go through the functions below, which call theseus.taskservice."""

    def __init__(self, conf):
        self.conf = conf
        self.root = conf.root

    @property
    def service(self):
        return service(self.conf)

    def load(self):
        return self.service.document(acting_principal(None))

    def archived(self):
        return [task for task in self.all() if task["status"] == "completed"]

    def all(self):
        return self.service.list(acting_principal(None), include_completed=True)

    def index(self):
        return {task["id"]: task for task in self.all()}

    def get(self, task_id):
        return self.service.get(acting_principal(None), task_id)["task"]

    def milestone(self):
        return self.load().get("currentMilestone")

    def holds(self):
        """Pending task id -> soft-ownership reasons it waits (theseus.taskservice.conflict)."""
        return self.service.holds(acting_principal())


def in_milestone(task, current):
    return not current or task.get("milestone") in (None, "", current)


def merge_reach(conf, base=None, label=None):
    """A function taking a dependency's recorded merge commit and returning the name of the base a worktree would
    start from when that base lacks the commit, else None. base defaults to the configured [git] base ref, resolved
    without fetching. A commit that no longer resolves (rewritten history, old archive records) counts as reached."""
    ref = base or queue_base_ref(conf)
    label = label or ref
    resolved, ancestry = {}, {}

    def resolve(rev):
        if rev not in resolved:
            shown = g.git(conf.root, "rev-parse", "--verify", "-q", f"{rev}^{{commit}}", check=False)
            resolved[rev] = shown.stdout.strip() if shown.returncode == 0 else None
        return resolved[rev]

    def missing(commit):
        merge, tip = resolve(commit), resolve(ref)
        if merge is None or tip is None:
            return None
        if merge not in ancestry:
            ancestry[merge] = g.git(conf.root, "merge-base", "--is-ancestor", merge, tip, check=False).returncode == 0
        return None if ancestry[merge] else label

    return missing


def queue_base_ref(conf):
    return conf.section("git").get("base") or "HEAD"


def unreached(task, index, reach):
    """A reason per satisfied dependency whose merge commit is not yet in the base a worktree would start from."""
    reasons = []
    for dependency in task.get("dependencies", []):
        record = index.get(dependency)
        commit = ((record or {}).get("merge") or {}).get("commit")
        if record and record["status"] in SATISFIES_DEPENDENCY and commit:
            where = reach(commit)
            if where:
                reasons.append(f"waiting for {dependency}'s merge {commit[:7]} to reach {where}")
    return reasons


def blocking(task, index, current=None, reach=None, holds=None):
    reasons = [key for key in ("externalBlocker", "researchRequired", "pause") if task.get(key)]
    if not in_milestone(task, current):
        reasons.append(f"milestone:{task.get('milestone')}")
    for dependency in task.get("dependencies", []):
        record = index.get(dependency)
        if record is None:
            reasons.append(f"missing:{dependency}")
        elif record["status"] not in SATISFIES_DEPENDENCY:
            reasons.append(f"waits:{dependency}")
    if reach and task["status"] == "pending":
        reasons += unreached(task, index, reach)
    if holds and task["status"] == "pending":
        reasons += holds.get(task["id"], [])
    return reasons


def dependency_ready(task, index, current=None, reach=None, holds=None):
    return task["status"] == "pending" and not blocking(task, index, current, reach, holds)


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
    reach = merge_reach(queue.conf)
    holds = queue.holds()
    tasks = queue.all() if include_archive else queue.load()["tasks"]
    lines = []
    for task in tasks:
        if status and task["status"] != status:
            continue
        flags = blocking(task, index, current, reach, holds)
        flags += ["ready"] if task["status"] == "pending" and not flags else []
        line = f"{task['status']:<12} {task['id']}  owner={task.get('owner') or '-'}  {','.join(flags)}"
        if show_evidence:
            line += f"  evidence={evidence_level(task)}"
        lines.append(line.rstrip())
    return lines


def ready(queue, include_paused=False, any_milestone=False):
    return queue.service.ready(acting_principal(None), include_paused=include_paused, any_milestone=any_milestone,
                               reach=merge_reach(queue.conf))


def record_size(record):
    return len(json.dumps(record))


def add(queue, task_id, role, owned, objective, contract, accept, depends=(), milestone=None, room=None, priority=0,
        profile=None, min_capability=0, touches=()):
    if int(min_capability or 0) < 0:
        raise TaskError("--min-capability must be 0 or more")
    added = queue.service.add(acting_principal(None), {
        "id": task_id, "role": role, "owned": list(owned), "objective": objective, "contract": contract,
        "accept": list(accept), "depends": list(depends), "milestone": milestone, "room": room, "priority": priority,
        "profile": profile, "min_capability": min_capability, "touches": list(touches)})
    lines = [f"{task_id} added as pending ({len(accept)} acceptance items, {added['size']} bytes)"]
    return "\n".join(lines + [f"warning: {warning}" for warning in added["warnings"]])


def provider_capacity(conf, provider):
    """The profile's capacity, plus that of the profiles on fallback onto it (theseus.health)."""
    from . import health
    from . import profiles
    try:
        capacity = profiles.resolve(conf, provider).get("capacity")
    except profiles.ProfileError:
        return None
    if capacity is None:
        return None
    extra = health.extra_capacity(conf, provider)
    return None if extra is None else int(capacity) + extra


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


def assign(queue, task_id, owner, provider, model, worktree, base, log_dir, force=False, owner_id=None, principal=None):
    """Record an assignment to a worktree that already exists (claim, then activate at once)."""
    svc, principal = queue.service, acting_principal(principal)
    owner_id = owner_id or owner_id_of(queue.conf, {"owner": owner, "execution": {"model": model}})
    claim = svc.claim(principal, task_id, owner_id, owner=owner, provider=provider, model=model, force=force,
                      capacity=provider_capacity(queue.conf, provider),
                      reach=merge_reach(queue.conf, base) if base != "uncommitted" else None)
    svc.activate(principal, claim["attempt"], worktree=worktree, base=base, log_dir=log_dir)
    write_inbox(queue, task_id)
    return f"{task_id} -> in_progress ({owner})"


def migrate_ids(queue):
    """Deprecated: ownerId and agentId live on the attempts in the host store."""
    return "agent ids live on the attempts in the host store; nothing to migrate"


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


def release(queue, task_id, reason, principal=None):
    released = queue.service.release(acting_principal(principal), task_id, reason)
    return (f"{task_id} -> pending (attempt recorded in previousAttempts)"
            + kept_worktree(queue, task_id, released["worktree"]))


HANDOFF_RESULT = re.compile(r"^\W*Result\W*:?\W*(delivered|partial|blocked)\b", re.IGNORECASE | re.MULTILINE)


def handoff_result(path):
    """delivered, partial or blocked from a handoff's Result line, or None when it states none."""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return None
    match = HANDOFF_RESULT.search(text)
    return match.group(1).lower() if match else None


def deliver(queue, task_id, handoff, principal=None):
    """Record a delivery whose handoff the queue or the coordinator found (a worker reports with `theseus task done`)."""
    try:
        text = Path(handoff).read_text(errors="replace")
    except OSError:
        text = None
    worktree = Path(handoff).parent.parent if Path(handoff).parent.name == cfg.WORKER_LOG else None
    delivered = queue.service.deliver(acting_principal(principal), task_id, handoff_path=str(handoff),
                                      result=handoff_result(handoff), handoff_text=text,
                                      observed_touches=patch_targets(worktree) if worktree else ())
    if delivered.get("already"):
        return f"{task_id} is already delivered"
    return f"{task_id} -> verification; the worker slot is free, dependents wait for the merge"


@refreshes_inbox
def reopen(queue, task_id, reason):
    """Undo a premature delivery: verification back to in_progress, keeping the note in previousAttempts."""
    task = queue.get(task_id)
    aside = set_aside_handoff(task) if task["status"] == "verification" else None
    reopened = queue.service.reopen(acting_principal(None), task_id, reason, handoff=aside)
    return busy_suffix(task, reopened, f"{task_id} -> in_progress again ({reason})")


def busy_suffix(task, changed, message):
    """The message after reopen or unmerge, saying so when the task went to pending because its agent already works
    on another task (an agent works one task at a time)."""
    if not changed.get("busy_with"):
        return message
    return (f"{message}; {agent_of(task)} already works on {changed['busy_with']}, so {task['id']} went back to pending "
            f"for the next free agent (its worktree moves aside on assignment)")


def patch_targets(worktree):
    """The files a worker's patches under .theseus/patches/ change: shared files it touches outside its ownership."""
    from . import integrate
    found = set()
    for patch in sorted((Path(worktree) / cfg.WORKER_LOG / "patches").glob("*.patch")):
        found.update(integrate.patch_paths(patch.read_text(errors="replace")))
    return sorted(found)


def set_aside_handoff(task):
    """Move the delivered handoff.md to handoff.attemptN.md, so the queue does not take it for a new delivery once the
    task is back in progress. Returns where the handoff now is."""
    execution = task.get("execution") or {}
    recorded = execution.pop("handoff", None)
    path = Path(recorded) if recorded else (Path(execution["logDir"]) / "handoff.md" if execution.get("logDir") else None)
    if not path or not path.is_file():
        return recorded
    aside = path.with_name(f"handoff.attempt{len(task.get('previousAttempts') or []) + 1}.md")
    os.replace(path, aside)
    return str(aside)


@refreshes_inbox
def unmerge(queue, task_id, reason, revert_commit=None):
    """Merged back to in_progress after the merge was reverted; owner and worktree stay for a redelivery."""
    task = queue.get(task_id)
    aside = set_aside_handoff(task) if task["status"] == "merged" else None
    unmerged = queue.service.unmerge(acting_principal(None), task_id, reason, revert_commit, handoff=aside)
    if unmerged.get("busy_with"):
        return busy_suffix(task, unmerged, f"{task_id} unmerged")
    return (f"{task_id} -> in_progress again; redeliver in {task.get('worktree')} and merge as usual, or release it"
            + kept_worktree(queue, task_id, task.get("worktree")))


@refreshes_inbox
def rebase(queue, task_id, base):
    """Record a new base after the worker moved its worktree HEAD along the base branch."""
    rebased = queue.service.rebase(acting_principal(None), task_id, base)
    return f"{task_id}: worktree rebased, base {rebased['previous']} -> {base}"


def mark_merged(queue, task_id, commit):
    queue.service.mark_merged(acting_principal(None), task_id, commit)
    return f"{task_id} -> merged in {commit}; record runtime evidence with `theseus tasks observe` and verify before completing"


def observe(queue, task_id, how, level="observed"):
    queue.service.observe(acting_principal(None), task_id, how, level)
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
    task = queue.get(task_id)
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
    queue.service.complete(acting_principal(None), task_id, summary, commit, checks, log, limits, force=force)
    return f"{task_id} -> completed; commit the record with `theseus tasks export`"


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
    """True when some path is owned by a pattern of each task (patterns_intersect)."""
    return any(patterns_intersect(a, b) for a in first for b in second)


def pattern_segments(pattern):
    """An owned pattern as path segments. A trailing slash, or a directory name without wildcards, also owns
    everything below (a final `**`); a wildcard-free last segment with a dot is taken for a file."""
    segments = tuple(part for part in pattern.split("/") if part)
    if pattern.endswith("/") or ("*" not in pattern and "." not in (segments[-1] if segments else "")):
        segments += ("**",)
    return segments


def patterns_intersect(a, b):
    """True when some path matches both owned patterns: compared segment by segment, `**` standing for any number of
    segments and `*` for any text within one."""
    return segments_meet(pattern_segments(a), pattern_segments(b))


@functools.lru_cache(maxsize=65536)
def segments_meet(a, b):
    if not a or not b:
        rest = a or b
        return all(part == "**" for part in rest)
    if a[0] == "**":
        return segments_meet(a[1:], b) or segments_meet(a, b[1:])
    if b[0] == "**":
        return segments_meet(a, b[1:]) or segments_meet(a[1:], b)
    return segment_meets(a[0], b[0]) and segments_meet(a[1:], b[1:])


@functools.lru_cache(maxsize=65536)
def segment_meets(a, b):
    """True when some segment text matches both globs (only `*` is special within a segment)."""
    if not a or not b:
        return set(a or b) <= {"*"}
    if a[0] == "*":
        return segment_meets(a[1:], b) or segment_meets(a, b[1:])
    if b[0] == "*":
        return segment_meets(a, b[1:]) or segment_meets(a[1:], b)
    return a[0] == b[0] and segment_meets(a[1:], b[1:])


def is_owned(path, patterns):
    return any(owned_pattern(p).match(path) for p in patterns)


def worktree_changes(worktree):
    entries = iter(g.git(worktree, "status", "--porcelain", "-z", "--untracked-files=all").stdout.split("\0"))
    changes = []
    for entry in entries:
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            next(entries, None)
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
    branch = g.out(worktree, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    g.git(queue.root, "worktree", "remove", "--force", str(worktree))
    note = ""
    if branch.startswith("work/") and (task["status"] in SATISFIES_DEPENDENCY or released):
        g.git(queue.root, "branch", "-D", branch, check=False)
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


def export(queue, head=None):
    """Write the store's tasks into the working tree as tasks.json, tasks-archive.jsonl and tasks-history.jsonl. Never
    commits."""
    from . import taskrecords
    snap = queue.service.export(acting_principal(None), head=head)
    taskrecords.write(snap, queue.root)
    doc, archived, history = snap.documents()
    return (f"exported {len(doc['tasks'])} open and {len(archived)} archived task(s) and {len(history)} history line(s) "
            f"to {cfg.TASKS}, {cfg.ARCHIVE} and {taskrecords.HISTORY}; commit them with the next change")


def import_(queue, replace=False, new_id=False):
    """Load the project's task files into the host store, after a backup of the store."""
    from . import store, taskrecords
    with store.opened() as conn:
        backup = store.backup(conn, label="pre-import")["path"]
    report = queue.service.import_(acting_principal(None), taskrecords.read(queue.root), replace=replace, new_id=new_id)
    lines = [f"imported {report['open']} open and {report['archived']} archived task(s) of {report['project']} "
             f"(id {report['id']}) in {report['seconds']} s; the store was backed up to {backup}"]
    if report["saved"]:
        lines.append(f"the tasks it replaced were exported to {report['saved']}")
    return "\n".join(lines + [f"warning: {warning}" for warning in report["warnings"]])


@refreshes_inbox
def note(queue, task_id, text):
    """Set the coordinator's assignment note, which task messages and prompts carry to the worker."""
    queue.service.note(acting_principal(None), task_id, text)
    return f"{task_id}: note set"


@refreshes_inbox
def edit(queue, task_id, changes, force=False):
    edited = queue.service.edit(acting_principal(None), task_id, changes, force=force)
    lines = [f"{task_id}: edited ({', '.join(sorted(changes))}), revision {edited['rev']}"]
    return "\n".join(lines + [f"warning: {warning}" for warning in edited["warnings"]])


@refreshes_inbox
def set_priority(queue, task_id, priority):
    """Higher priority is assigned first among ready tasks; equal priorities keep task-file order."""
    queue.service.edit(acting_principal(None), task_id, {"priority": int(priority)}, force=True)
    return f"{task_id}: priority {int(priority)}"


@refreshes_inbox
def set_profile(queue, task_id, profile):
    """The agent profile a task needs (only its agents, or ones the queue can relaunch onto it, take it); empty: any."""
    queue.service.edit(acting_principal(None), task_id, {"profile": profile or None}, force=True)
    return f"{task_id}: {f'needs profile {profile}' if profile else 'any profile'}"


@refreshes_inbox
def set_min_capability(queue, task_id, level):
    """The lowest profile capability an agent needs to take the task ([workers.<name>] capability); 0 removes it."""
    level = int(level)
    if level < 0:
        raise TaskError("the minimum capability must be 0 or more")
    queue.service.edit(acting_principal(None), task_id, {"min_capability": level}, force=True)
    return f"{task_id}: {f'needs capability >= {level}' if level else 'any capability'}"


def hold(queue, task_id, held):
    queue.service.hold(acting_principal(None), task_id, held)
    return f"{task_id}: {'held, the queue will not assign it' if held else 'released to the queue'}"


def set_milestone(queue, milestone):
    queue.service.set_milestone(acting_principal(None), milestone)
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


def task_prompt(queue, task):
    """What a worker reads to start: the standing brief to follow and the task rendered from the worker prompt
    template (the record alone when a project has no template)."""
    execution = task.get("execution") or {}
    worktree, log_dir = task.get("worktree") or "", execution.get("logDir") or ""
    try:
        body = render_prompt(queue, task["id"], task.get("baseCommit") or "", worktree, log_dir)
    except OSError:
        body = json.dumps(task, indent=2, ensure_ascii=False)
    brief = queue.conf.section("instances").get("brief")
    return (f"Read and follow {queue.root / brief} first.\n\n" if brief else "") + body


INBOX = "task.json"
OUTBOX = "outbox"


def write_inbox(queue, task_id):
    """Copy an in-progress task (record, notes, rendered prompt) into its log directory as task.json, which the worker
    reads when it cannot open the host store. Returns the path, or None for a task with no live log directory."""
    shown = queue.service.get(acting_principal(), task_id)
    task = shown["task"]
    log_dir = (task.get("execution") or {}).get("logDir")
    if task["status"] != "in_progress" or not log_dir or not Path(log_dir).is_dir():
        return None
    copy = {"task": task, "notes": shown["notes"], "prompt": task_prompt(queue, task), "written": now()}
    target = Path(log_dir) / INBOX
    try:
        cfg.write_atomic(target, json.dumps(copy, indent=2, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return target


def find_inbox(start, task_id):
    """The task.json copy of task_id in the worktree holding start (its .theseus/, or .theseus/TASK/ for in-place
    work), or None."""
    start = Path(start).resolve()
    for directory in (start, *start.parents):
        for candidate in (directory / cfg.WORKER_LOG / INBOX, directory / cfg.WORKER_LOG / task_id / INBOX):
            if candidate.is_file():
                try:
                    copy = json.loads(candidate.read_text())
                except (OSError, ValueError):
                    continue
                if (copy.get("task") or {}).get("id") == task_id:
                    return candidate
    return None


def write_outbox(inbox, entry):
    """Leave a report the worker could not write to the store (the store is not writable from its sandbox) next to
    its task copy; the queue takes it in on its next tick."""
    outbox = Path(inbox).parent / OUTBOX
    outbox.mkdir(exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = outbox / f"{stamp}-{entry['kind']}.json"
    cfg.write_atomic(target, json.dumps({**entry, "written": now()}, ensure_ascii=False) + "\n")
    return target


def show_own(queue, principal, task_id):
    """A worker's view of its task: the record, notes, attempt and the rendered prompt."""
    shown = queue.service.show(principal, task_id)
    return {"task": shown["task"], "notes": shown["notes"], "attempt": shown["attempt"],
            "prompt": task_prompt(queue, shown["task"])}


def report(queue, principal, task_id, kind, *, text=None, question=False, handoff=None, reason=None):
    """A worker's report (kind note, done or blocked) through the service; the coordinator may report for a subagent
    of its own session. done reads the handoff's Result and the patch targets next to it."""
    svc = queue.service
    if kind == "note":
        return svc.note(principal, task_id, text, question=question, on_behalf=principal.role == "coordinator")
    handoff = Path(handoff).resolve() if handoff else None
    body = handoff.read_text(errors="replace") if handoff and handoff.is_file() else None
    worktree = handoff.parent.parent if handoff and handoff.parent.name == cfg.WORKER_LOG else None
    if kind == "done":
        if body is None:
            raise TaskError(f"no handoff at {handoff}; write it first (see the worker skill)")
        return svc.deliver(principal, task_id, handoff_path=str(handoff), result=handoff_result(handoff),
                           handoff_text=body, observed_touches=patch_targets(worktree) if worktree else (),
                           source="command")
    return svc.blocked(principal, task_id, reason or "", handoff_path=str(handoff) if handoff else None,
                       handoff_text=body, source="command")


def fail(error):
    sys.exit(str(error))
