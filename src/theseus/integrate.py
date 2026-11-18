"""Worker worktrees and the single integration path.

assign creates a worktree on a work/<name> branch at the configured base and records the assignment. merge brings one
delivery into the main checkout: ownership check, refusal when the worker committed, a three-way apply of the worktree
diff, the worker's outside-path patches, regenerated indexes, and a commit of exactly the touched paths. Any conflict
restores main and commits nothing.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config as cfg
from . import docs
from . import gitutil as g
from . import tasks as t

EXIT_CONFLICT = 3


class IntegrationError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def base_commit(conf):
    git = conf.section("git")
    ref = git.get("base") or "HEAD"
    remote = git.get("remote") or "origin"
    if ref.startswith(remote + "/") and g.has_remote(conf.root, remote):
        g.git(conf.root, "fetch", "-q", remote)
    return g.out(conf.root, "rev-parse", ref)


def commit_record(conf, message):
    if g.git(conf.root, "diff", "--quiet", "HEAD", "--", cfg.TASKS, cfg.ARCHIVE, check=False).returncode == 0:
        return False
    g.git(conf.root, "commit", "-q", "-m", message, "--", cfg.TASKS, cfg.ARCHIVE)
    return True


def record_committed(conf, task_id, base):
    """True when the task record in the working tree equals the one at base, so the worker sees it."""
    shown = g.git(conf.root, "show", f"{base}:{cfg.TASKS}", check=False)
    if shown.returncode != 0:
        return False
    pick = lambda document: next((x for x in document["tasks"] if x["id"] == task_id), None)
    return pick(json.loads(shown.stdout)) == pick(json.loads(conf.tasks_file.read_text()))


def base_mismatch(conf, task_id, base):
    """What to do when the task record at the assignment base is not the one in the working tree."""
    git = conf.section("git")
    ref, remote, branch = git.get("base") or "HEAD", git.get("remote") or "origin", git.get("branch") or "main"
    where = f"the assignment base {ref} ({base[:12]})"
    if record_committed(conf, task_id, "HEAD"):
        if ref.startswith(remote + "/"):
            return (f"the {task_id} record is committed locally but not at {where}: push it (`git push {remote} "
                    f"HEAD:{branch}`), or set [git] base = \"HEAD\" to assign from local commits")
        return f"the {task_id} record is committed but not at {where}; move {ref} to a commit that has it"
    return f"the {task_id} record is not committed at {where}: commit {cfg.TASKS} and make {ref} include it"


REBASE_PENDING = "rebase-pending"


def read_bytes(path):
    path = Path(path)
    if path.is_symlink():
        return os.readlink(path).encode()
    return path.read_bytes() if path.is_file() else None


def write_bytes(path, data):
    path = Path(path)
    if data is None:
        if path.is_file() or path.is_symlink():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def blob(root, rev, path):
    shown = subprocess.run(["git", "-C", str(root), "cat-file", "blob", f"{rev}:{path}"], capture_output=True)
    return shown.stdout if shown.returncode == 0 else None


def three_way(base, ours, theirs, labels):
    """Merge theirs' change from base into ours. Returns (merged, conflicted); merged None and not conflicted is a
    deletion. A missing or binary version merges only when one side left the file as it was at base."""
    if theirs == base or ours == theirs:
        return ours, False
    if ours == base:
        return theirs, False
    if None in (base, ours, theirs) or any(b"\0" in x for x in (base, ours, theirs)):
        return None, True
    with tempfile.TemporaryDirectory() as scratch:
        names = []
        for name, data in zip(("ours", "base", "theirs"), (ours, base, theirs)):
            (Path(scratch) / name).write_bytes(data)
            names.append(str(Path(scratch) / name))
        named = [arg for label in labels for arg in ("-L", label)]
        merged = subprocess.run(["git", "merge-file", "-p", "-q", *named, *names], capture_output=True)
    if merged.returncode > 127:
        return None, True
    return merged.stdout, merged.returncode != 0


def stale_paths(conf, worktree, base, head):
    """Paths the commits base..head changed whose worktree copy does not hold that change, as when HEAD moved without
    them. An edit right beside main's change also lands here; merging against the base then refuses safely."""
    moved = set(g.out(conf.root, "diff", "--name-only", "--no-renames", base, head).splitlines())
    stale = []
    for path in sorted({p for _, p in t.worktree_changes(worktree)} & moved):
        current = read_bytes(Path(worktree) / path)
        merged, conflicted = three_way(blob(conf.root, base, path), current, blob(conf.root, head, path),
                                       ("worktree", "base", "head"))
        if conflicted or merged != current:
            stale.append(path)
    return stale


def commits_since(root, base, tip, paths):
    if not paths:
        return []
    return g.out(root, "log", "--format=%h %s", f"{base}..{tip}", "--", *paths).splitlines()


def conflict_hunks(text, limit=80):
    """The conflict regions in text, from each <<<<<<< line to its >>>>>>> line."""
    hunks, inside = [], False
    for line in text.splitlines():
        inside = inside or line.startswith("<<<<<<< ")
        if inside:
            hunks.append(line)
        inside = inside and not line.startswith(">>>>>>> ")
    return hunks if len(hunks) <= limit else hunks[:limit] + [f"... {len(hunks) - limit} more lines"]


def pending_rebase(conf, task, head):
    """Refuse to adopt HEAD while a conflicted `theseus tasks rebase` onto it is unresolved. True when the worktree
    content derives from HEAD because theseus moved it there."""
    worktree = Path(task["worktree"])
    marker = worktree / cfg.WORKER_LOG / REBASE_PENDING
    if not marker.is_file():
        return False
    pending = json.loads(marker.read_text())
    if pending.get("head") != head:
        return False
    unresolved = [p for p in pending.get("conflicts", []) if conflict_hunks(read_text(worktree / p))]
    if unresolved:
        raise IntegrationError(f"{task['id']}: the worktree still holds the conflict markers `theseus tasks rebase` left "
                               f"in {', '.join(unresolved)}; have the worker resolve them first", EXIT_CONFLICT)
    stash = pending.get("stash")
    if pending.get("hold") and stash in g.out(worktree, "stash", "list", "--format=%H").splitlines():
        raise IntegrationError(f"{task['id']}: part of the worker's edits exist only in stash {stash[:12]}, which "
                               f"`theseus tasks rebase` could not replay ({hold_reason(pending)}). Have the worker fold "
                               "them in from the stash, then drop that stash entry.", EXIT_CONFLICT)
    marker.unlink()
    if stash:
        drop_stash(worktree, stash)
    return True


def hold_reason(pending):
    parts = [f"no conflict markers in {', '.join(pending['markerless'])}"] if pending.get("markerless") else []
    parts += [f"untracked {', '.join(pending['unrestored'])} not restored"] if pending.get("unrestored") else []
    return "; ".join(parts) or "git stash apply failed"


def read_text(path):
    return Path(path).read_text(errors="replace") if Path(path).is_file() else ""


def adopt_rebase(conf, queue, task):
    """Record the worktree HEAD as the new base when it moved only onto commits of the configured base branch, as a
    worker rebase does. Returns (message or None, stale paths). Refuses when the worker committed. When HEAD moved
    without some files (stale), nothing is recorded: merged against the new HEAD they would revert main's changes."""
    worktree = Path(task["worktree"])
    head = g.out(worktree, "rev-parse", "HEAD")
    base = g.out(conf.root, "rev-parse", task["baseCommit"])
    if head == base:
        return None, []
    ref = conf.section("git").get("base") or "HEAD"
    # Resolve in the main checkout: HEAD inside the worktree is the worker's own.
    tip = g.out(conf.root, "rev-parse", ref)
    own = int(g.out(conf.root, "rev-list", "--count", f"{tip}..{head}"))
    if own:
        raise IntegrationError(f"{task['id']}: the worker committed in its worktree (HEAD {head[:12]}, base {base[:12]}, "
                               f"{own} commit(s) not on {ref}); workers never commit. Inspect the commits, then reset "
                               "them into the working tree before merging.", EXIT_CONFLICT)
    if not pending_rebase(conf, task, head):
        stale = stale_paths(conf, worktree, base, head)
        if stale:
            return None, stale
    return t.rebase(queue, task["id"], head[:12]), []


def rebase(conf, task_id):
    """Adopt a worker rebase, or move the worktree onto the current base ref, replaying uncommitted edits. Records the
    new base only when everything replayed cleanly; conflicts stay in the worktree as conflict markers."""
    queue = t.Queue(conf)
    task = queue.get(task_id)
    if not task.get("worktree") or not Path(task["worktree"]).is_dir():
        raise IntegrationError(f"{task_id} has no live worktree")
    worktree = Path(task["worktree"])
    if worktree.resolve() == Path(conf.root).resolve():
        raise IntegrationError(f"{task_id} works in place in the main checkout; there is no worktree to rebase")
    adopted, stale = adopt_rebase(conf, queue, task)
    if adopted:
        return adopted
    ref = conf.section("git").get("base") or "HEAD"
    ref = "main's HEAD" if ref == "HEAD" else ref
    head = g.out(worktree, "rev-parse", "HEAD")
    messages = []
    if stale:
        messages.append(rebase_stale(conf, queue, task, head, stale, ref))
        task = queue.get(task_id)
    tip = base_commit(conf)
    if tip == head:
        return "\n".join(messages) or f"{task_id}: worktree HEAD is still the base {task['baseCommit']}, which is {ref}"
    stash = None
    if t.worktree_changes(worktree):
        top = lambda: g.git(worktree, "rev-parse", "-q", "--verify", "refs/stash", check=False).stdout.strip()
        before = top()
        # Excluding an ignored path makes stash fail, and the ignored ones are left alone anyway.
        excludes = [f":(exclude,literal){p}" for p in (cfg.WORKER_LOG, cfg.LOCAL_NAME)
                    if g.git(worktree, "check-ignore", "-q", p, check=False).returncode != 0]
        g.git(worktree, "stash", "push", "-q", "--include-untracked", "-m", f"theseus rebase {task_id}", "--", ".",
              *excludes)
        stash = top() if top() != before else None
    moved = g.git(worktree, "reset", "-q", "--keep", tip, check=False)
    if moved.returncode != 0:
        if stash:
            g.git(worktree, "stash", "apply", "-q", "--index", stash)
            drop_stash(worktree, stash)
        raise IntegrationError(f"{task_id}: could not move the worktree to {ref} {tip[:12]}, left as it was: "
                               f"{moved.stderr.strip()}")
    if stash:
        applied = g.git(worktree, "stash", "apply", stash, check=False)
        conflicts = g.out(worktree, "diff", "--name-only", "--diff-filter=U").splitlines()
        untracked = g.git(worktree, "ls-tree", "-r", "--name-only", f"{stash}^3", check=False).stdout.splitlines()
        unrestored = [p for p in untracked if read_bytes(worktree / p) != blob(worktree, f"{stash}^3", p)]
        if applied.returncode != 0 or conflicts or unrestored:
            g.git(worktree, "reset", "-q")
            # Conflicts without markers (binary, modify/delete) leave the worker's side only in the stash.
            markerless = [p for p in conflicts if not conflict_hunks(read_text(worktree / p))]
            pending = {"head": tip, "stash": stash, "conflicts": conflicts, "unrestored": unrestored,
                       "markerless": markerless, "hold": bool(markerless or unrestored or not conflicts)}
            (worktree / cfg.WORKER_LOG).mkdir(exist_ok=True)
            (worktree / cfg.WORKER_LOG / REBASE_PENDING).write_text(json.dumps(pending, indent=1) + "\n")
            lines = [f"{task_id}: moved the worktree onto {ref} {tip[:12]}, but the worker's uncommitted edits did not "
                     "replay cleanly; nothing is lost:"]
            marked = [p for p in conflicts if p not in markerless]
            if marked:
                lines.append(f"- {', '.join(marked)} hold conflict markers (<<<<<<< Updated upstream is {ref}, "
                             ">>>>>>> Stashed changes is the worker's)")
            if pending["hold"]:
                lines.append(f"- {hold_reason(pending)}: the worker's side is only in the stash (`git -C {worktree} "
                             f"show {stash[:12]}:PATH`, or {stash[:12]}^3:PATH for untracked files), and merge waits "
                             "until that stash entry is dropped")
            lines.append(f"- a full copy of the edits stays in stash {stash[:12]} "
                         f"(`git -C {worktree} stash show -p --include-untracked {stash[:12]}`)")
            lines.append(f"Have the worker resolve them; the base stays {task['baseCommit']} until then, and merge or "
                         "`theseus tasks rebase` records the new one once they are resolved.")
            raise IntegrationError("\n".join(messages + lines), EXIT_CONFLICT)
        drop_stash(worktree, stash)
    replayed = ", uncommitted edits replayed cleanly" if stash else ""
    messages.append(f"{task_id}: moved the worktree onto {ref} {tip[:12]}{replayed}\n{t.rebase(queue, task_id, tip[:12])}")
    return "\n".join(messages)


def rebase_stale(conf, queue, task, head, stale, ref):
    """Merge what main changed since the base into worktree copies that do not hold it."""
    worktree, base = Path(task["worktree"]), g.out(conf.root, "rev-parse", task["baseCommit"])
    conflicts, unmergeable = [], []
    for path in stale:
        merged, conflicted = three_way(blob(conf.root, base, path), read_bytes(worktree / path),
                                       blob(conf.root, head, path), ("worker", f"base {base[:12]}", f"{ref} {head[:12]}"))
        if merged is None and conflicted:
            unmergeable.append(path)
            continue
        write_bytes(worktree / path, merged)
        if conflicted:
            conflicts.append(path)
    commits = "".join(f"  {c}\n" for c in commits_since(conf.root, base, head, stale))
    intro = (f"{task['id']}: the worktree HEAD moved from the base {base[:12]} to {head[:12]}, but the worktree copies "
             f"of {', '.join(stale)} do not hold what these commits changed there; merged it into them:\n{commits}")
    if unmergeable:
        raise IntegrationError(intro + f"{', '.join(unmergeable)} could not be merged (deleted or binary on one side) "
                               f"and are left as they were; have the worker bring them up to {head[:12]} by hand"
                               + (f", and resolve the conflict markers in {', '.join(conflicts)}" if conflicts else ""),
                               EXIT_CONFLICT)
    if conflicts:
        (worktree / cfg.WORKER_LOG).mkdir(exist_ok=True)
        (worktree / cfg.WORKER_LOG / REBASE_PENDING).write_text(json.dumps({"head": head, "conflicts": conflicts},
                                                                           indent=1) + "\n")
        raise IntegrationError(intro + f"{', '.join(conflicts)} hold conflict markers; have the worker resolve them. "
                               f"The base stays {task['baseCommit']} until then.", EXIT_CONFLICT)
    return intro + t.rebase(queue, task["id"], head[:12])


def drop_stash(worktree, stash):
    # refs/stash is shared by every worktree of the repository, so drop the entry by its commit, not by position.
    entries = g.out(worktree, "stash", "list", "--format=%H").splitlines()
    if stash in entries:
        g.git(worktree, "stash", "drop", "-q", f"stash@{{{entries.index(stash)}}}")


def worktree_branches(conf):
    """Resolved path -> checked-out ref ("" when detached) of every git worktree of the repository."""
    found, path = {}, None
    for line in g.out(conf.root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):]).resolve()
            found[path] = ""
        elif line.startswith("branch ") and path is not None:
            found[path] = line[len("branch "):]
    return found


def attempts_at(task, worktree):
    """The records of task (its earlier attempts and the task itself) whose attempt used worktree."""
    target = Path(worktree).resolve()

    def used(record):
        paths = [record.get("worktree"), (record.get("execution") or {}).get("logDir")]
        return any(p and (Path(p).resolve() == target or target in Path(p).resolve().parents) for p in paths)

    return [r for r in [*(task.get("previousAttempts") or []), task] if used(r)]


def check_previous_attempt(conf, task, worktree):
    """Refuse an existing worktree path unless it is a git worktree a previous attempt of this task left there."""
    if not (attempts_at(task, worktree) and Path(worktree).resolve() in worktree_branches(conf)):
        raise IntegrationError(f"worktree already exists: {worktree} (not a previous attempt of {task['id']}; retire "
                               "the old task or pick another name)")


def move_previous_attempt(conf, queue, task_id, worktree):
    """Move the worktree a previous attempt of task_id left at worktree, and its work/<name> branch, aside to
    <worktree>-attemptN and work/<name>-attemptN and record both on that attempt. Returns the new path, or None when
    worktree is free."""
    worktree = Path(worktree)
    if not worktree.exists():
        return None
    document = queue.load()
    task = queue.open_task(document, task_id)
    check_previous_attempt(conf, task, worktree)
    records = attempts_at(task, worktree)
    branch = worktree_branches(conf)[worktree.resolve()]
    own = f"refs/heads/work/{worktree.name}"
    n = 1
    while Path(f"{worktree}-attempt{n}").exists() or (
            branch == own and g.git(conf.root, "rev-parse", "--verify", "-q", f"{own}-attempt{n}", check=False).returncode == 0):
        n += 1
    aside = Path(f"{worktree}-attempt{n}")
    g.git(conf.root, "worktree", "move", str(worktree), str(aside))
    if branch == own:
        g.git(conf.root, "branch", "-m", f"work/{worktree.name}", f"work/{worktree.name}-attempt{n}")
    old = str(worktree)
    for record in records:
        record["worktree"] = str(aside)
        if branch == own:
            record["branch"] = f"work/{worktree.name}-attempt{n}"
        execution = record.get("execution") or {}
        for key in ("logDir", "handoff"):
            if isinstance(execution.get(key), str) and execution[key].startswith(old + os.sep):
                execution[key] = str(aside) + execution[key][len(old):]
    queue.save(document)
    return aside


def create_worktree(conf, name, base):
    worktree = conf.worktrees / name
    if worktree.exists():
        raise IntegrationError(f"worktree already exists: {worktree} (retire the old task or pick another name)")
    conf.worktrees.mkdir(parents=True, exist_ok=True)
    g.git(conf.root, "worktree", "add", "-q", "-b", f"work/{name}", str(worktree), base)
    (worktree / cfg.WORKER_LOG).mkdir(exist_ok=True)
    copy_local_overlay(conf, worktree)
    return worktree


def copy_local_overlay(conf, tree):
    """Give a worktree the machine-specific theseus.local.toml so `theseus` run inside it resolves the same paths."""
    local = conf.root / cfg.LOCAL_NAME
    if local.is_file():
        shutil.copy2(local, Path(tree) / cfg.LOCAL_NAME)


def worker_environment(conf, tree):
    """Environment for anything that runs inside a worktree: THESEUS_ROOT points at the worktree, not main."""
    return {**os.environ, **conf.environment(), "THESEUS_ROOT": str(tree)}


def assign(conf, task_id, name, provider, model, owner=None, commit=True, instance=None, owner_id=None):
    """Create the worktree, record the assignment and (by default) commit only the task file."""
    from . import rooms
    queue = t.Queue(conf)
    task = queue.get(task_id)
    if instance:
        try:
            rooms.check_assignment(conf, task, instance)
        except rooms.RoomError as error:
            raise IntegrationError(str(error)) from error
    if commit and not record_committed(conf, task_id, "HEAD"):
        commit_record(conf, f"chore: queue {task_id}")
    base = base_commit(conf)
    if not record_committed(conf, task_id, base):
        raise IntegrationError(base_mismatch(conf, task_id, base))
    aside = move_previous_attempt(conf, queue, task_id, conf.worktrees / name)
    worktree = create_worktree(conf, name, base)
    rooms.exclude_dirty(conf, task, worktree)
    try:
        message = t.assign(queue, task_id, owner or f"{provider}:{model}:{name}", provider, model, worktree, base[:12],
                           worktree / cfg.WORKER_LOG, owner_id=owner_id)
        if aside:
            message += f"; the previous attempt's worktree moved to {aside}"
    except t.TaskError:
        g.git(conf.root, "worktree", "remove", "--force", str(worktree), check=False)
        g.git(conf.root, "branch", "-D", f"work/{name}", check=False)
        raise
    if instance:
        rooms.record_assignment(conf, task, instance)
    if commit:
        commit_record(conf, f"chore: assign {task_id} to {name}")
    return worktree, base, message


def assign_in_place(conf, task_id, name, provider, model, owner=None, instance=None, owner_id=None):
    """Assign without a worktree or commit: the worker edits its owned paths in the main checkout and writes its
    records to .theseus/<task>/. For docs-only rounds, or before the repository has a first commit. Owned paths must
    not overlap any other active task, since nothing isolates the workers from each other."""
    from . import rooms
    queue = t.Queue(conf)
    task = queue.get(task_id)
    if instance:
        try:
            rooms.check_assignment(conf, task, instance)
        except rooms.RoomError as error:
            raise IntegrationError(str(error)) from error
    active = [x for x in queue.load()["tasks"] if x["status"] == "in_progress" and x["id"] != task_id]
    overlap = [x["id"] for x in active if t.ownership_overlaps(task["ownedPaths"], x["ownedPaths"])]
    if overlap:
        raise IntegrationError(f"{task_id} owns paths that overlap active task(s) {', '.join(overlap)}; in-place work needs disjoint ownership")
    head = g.git(conf.root, "rev-parse", "--short=12", "HEAD", check=False)
    base = head.stdout.strip() if head.returncode == 0 else "uncommitted"
    log_dir = conf.root / cfg.WORKER_LOG / task_id
    log_dir.mkdir(parents=True, exist_ok=True)
    message = t.assign(queue, task_id, owner or f"{provider}:{model}:{name}", provider, model, conf.root, base, log_dir,
                       owner_id=owner_id)
    if instance:
        rooms.record_assignment(conf, task, instance)
    return conf.root, base, message


def patch_paths(text):
    paths = []
    for line in text.splitlines():
        if line.startswith("diff --git a/") and " b/" in line:
            left, right = line[len("diff --git a/"):].split(" b/", 1)
            paths += [left.strip(), right.strip()]
        elif line.startswith("+++ b/"):
            paths.append(line[6:].strip())
        elif line.startswith("--- a/"):
            paths.append(line[6:].strip())
        elif line.startswith("rename to ") or line.startswith("rename from "):
            paths.append(line.split(" ", 2)[2].strip())
    return [p for p in paths if p and p != "/dev/null"]


def worktree_diff(worktree, exclude=()):
    excludes = [f":!{cfg.WORKER_LOG}", f":!{cfg.LOCAL_NAME}"] + [f":(exclude,literal){p}" for p in exclude]
    g.git(worktree, "add", "-A", "--intent-to-add", "--", ".", *excludes, check=False)
    try:
        return g.git(worktree, "diff", "--binary", "-M", "HEAD", "--", ".", *excludes).stdout
    finally:
        g.git(worktree, "reset", "-q", "--", ".", check=False)


def worker_patches(worktree, skip=()):
    directory = Path(worktree) / cfg.WORKER_LOG / "patches"
    return [p for p in sorted(directory.glob("*.patch")) if p.name not in skip]


def applied_patches(root, worktree, patches, foreign):
    """Compare the worktree with the cumulative result of the shipped patches applied in order to its base, on the
    outside-owned paths (foreign) that both the worktree and a patch change. Returns (the paths that equal that result,
    one line per refusal). Patches applied to main are authoritative, so differing in-place copies are refused."""
    worktree = Path(worktree)
    touched = {patch: set(patch_paths(patch.read_text(errors="replace"))) for patch in patches}
    paths = sorted(set(foreign) & set().union(*touched.values()))
    if not paths:
        return set(), []
    with tempfile.TemporaryDirectory() as scratch:
        patched = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "patched")}
        actual = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "actual")}
        g.git(worktree, "read-tree", "HEAD", env=patched)
        includes = [f"--include={p}" for p in paths]
        for patch in (patch for patch in patches if touched[patch].intersection(paths)):
            applied = g.git(worktree, "apply", "--cached", *includes, str(patch), env=patched, check=False)
            if applied.returncode != 0:
                return set(), [f"{patch.name} does not apply to the worktree base after the patches before it: {applied.stderr.strip()}"]
        g.git(worktree, "read-tree", "HEAD", env=actual)
        present = [p for p in paths if (worktree / p).exists() or (worktree / p).is_symlink()]
        if present:
            g.git(worktree, "--literal-pathspecs", "add", "-f", "--", *present, env=actual)
        gone = [p for p in paths if p not in present]
        if gone:
            g.git(worktree, "--literal-pathspecs", "rm", "-q", "--cached", "--ignore-unmatch", "--", *gone, env=actual)
        entries = lambda env: set(g.out(worktree, "--literal-pathspecs", "ls-files", "-s", "--", *paths, env=env).splitlines())
        differ = sorted({line.split("\t", 1)[1] for line in entries(patched) ^ entries(actual)})
    if not differ:
        return set(paths), []
    names = ", ".join(patch.name for patch in patches if touched[patch].intersection(differ))
    refusal = [f"the worktree copies of {', '.join(differ)} differ from what the shipped patches ({names}) apply in order "
               "to the worktree base"]
    base = g.out(worktree, "rev-parse", "HEAD")
    if int(g.out(root, "rev-list", "--count", f"{base}..HEAD")):
        tracked = [p for p in differ if g.git(worktree, "cat-file", "-e", f"HEAD:{p}", check=False).returncode == 0]
        new = [p for p in differ if p not in tracked]
        steps = [f"`git -C {worktree} checkout -- {' '.join(tracked)}`"] if tracked else []
        steps += [f"delete {', '.join(new)} from the worktree"] if new else []
        refusal.append(f"the worktree base {base[:12]} is older than HEAD, so in-place copies may carry stale content: "
                       f"rely on the patches, which merge applies to current main, revert the in-place copies "
                       f"({' and '.join(steps)}) and merge again")
    return set(), refusal


def apply_patches(root, patches, skip=()):
    """Apply outside-path patches with a three-way merge, staging touched files in a temporary copy of the index so
    the coordinator's real index is never changed. Paths in skip (the generated indexes, which merge rewrites anyway)
    are left out of every patch so a stale copy cannot conflict. Returns (ok, messages)."""
    excludes = [f"--exclude={p}" for p in skip]
    messages, conflicts, incomplete = [], [], False
    with tempfile.TemporaryDirectory() as index_dir:
        real = Path(g.out(root, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        if real.exists():
            shutil.copy(real, Path(index_dir) / "index")
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(index_dir) / "index")}
        for patch in patches:
            text = Path(patch).read_text(errors="replace")
            if "diff --git " not in text:
                applied = False
                for strip in ("1", "0"):
                    dry = subprocess.run(["patch", f"-p{strip}", "--dry-run", "--silent", "-i", str(patch)], cwd=root, capture_output=True)
                    if dry.returncode == 0:
                        subprocess.run(["patch", f"-p{strip}", "--silent", "--no-backup-if-mismatch", "-i", str(patch)], cwd=root, check=True)
                        messages.append(f"applied {patch} (plain diff, patch -p{strip})")
                        applied = True
                        break
                if not applied:
                    messages.append(f"incomplete {patch}: plain diff does not apply with patch -p1 or -p0")
                    incomplete = True
                continue
            numstat = g.git(root, "apply", "--numstat", *excludes, str(patch), env=env, check=False).stdout
            files = [line.split("\t", 2)[2] for line in numstat.splitlines() if line.count("\t") >= 2]
            files = [f.split(" => ")[-1].replace("{", "").replace("}", "") for f in files]
            existing = [f for f in files if (Path(root) / f).exists()]
            if existing:
                g.git(root, "add", "--", *existing, env=env)
            result = g.git(root, "apply", "--3way", *excludes, str(patch), env=env, check=False)
            if result.returncode == 0:
                messages.append(f"applied {patch}")
            else:
                messages.append(f"failed {patch}: {result.stderr.strip()}")
            found = [f for f in files if (Path(root) / f).is_file() and "<<<<<<< " in (Path(root) / f).read_text(errors="replace")]
            conflicts += found
            if result.returncode != 0 and not found:
                incomplete = True
    if conflicts:
        messages += [f"conflict {c}" for c in conflicts]
    return not conflicts and not incomplete, messages


def repoint_folded_links(root, touched):
    """Point links at reports the fold archived (possibly after the worker's base) to their archived copy, in every
    Markdown file under docs/ and the delivered files. Returns the repository-relative paths changed."""
    root = Path(root)
    link = re.compile(r"(\]\(|`)((?:\.\./|\./)*(?:docs/)?(?:[\w.-]+/)*investigations/([A-Za-z0-9_.-]+)\.md)")
    candidates = {p for p in touched if p.endswith(".md")}
    if (root / "docs").is_dir():
        candidates.update(p.relative_to(root).as_posix() for p in (root / "docs").rglob("*.md"))
    changed = []
    for relative in sorted(candidates):
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")

        def replace(match):
            target = match.group(2)
            base = root if target.startswith("docs/") else path.parent
            if (base / target).exists():
                return match.group(0)
            archived = root / cfg.FOLDED / f"{match.group(3)}.md"
            if not archived.exists():
                return match.group(0)
            new = archived.relative_to(root).as_posix() if target.startswith("docs/") else os.path.relpath(archived, path.parent)
            return match.group(1) + new

        updated = link.sub(replace, text)
        if updated != text:
            path.write_text(updated)
            changed.append(relative)
    return changed


def rebase_hint(task_id):
    return (f"Main moved on under the worker: have it rebase (`theseus tasks rebase {task_id}` brings its worktree up to "
            "the current base and replays its edits, leaving conflict markers to resolve), then merge again.")


def conflict_report(root, worktree, task_id, touched, unmerged, stderr):
    """Why the worktree diff did not apply: per file, the commits main gained since the worktree base and the
    conflicting hunks, read before main is restored."""
    base = g.out(worktree, "rev-parse", "HEAD")
    since = {p: commits_since(root, base, "HEAD", [p]) for p in touched}
    labels = {"<<<<<<< ours": "<<<<<<< main", ">>>>>>> theirs": ">>>>>>> worker"}
    lines = ["apply failed; main restored, nothing committed"]
    if stderr.strip():
        lines.append(stderr.strip())
    for path in unmerged or [p for p in touched if since[p]]:
        commits = since.get(path) or commits_since(root, base, "HEAD", [path])
        lines.append(f"{path}: changed on main since the worktree base {base[:12]} by:" if commits
                     else f"{path}: unchanged on main since the worktree base {base[:12]}")
        lines += [f"  {c}" for c in commits]
        hunks = conflict_hunks(read_text(Path(root) / path)) if path in unmerged else []
        if hunks:
            lines.append("  conflicting hunks:")
            lines += [f"    {labels.get(h, h)}" for h in hunks]
    if any(since.values()):
        lines.append(rebase_hint(task_id))
    return lines


def merge_stale(root, worktree, task_id, base, stale):
    """Three-way merge the worker's changes to files HEAD moved without (git merge-file of the base, main and worktree
    versions). Returns {path: merged content, None to delete}; refuses on any conflict before main is touched."""
    results, refused = {}, []
    for path in stale:
        ours = blob(root, "HEAD", path)
        merged, conflicted = three_way(blob(root, base, path), ours, read_bytes(Path(worktree) / path),
                                       ("main", f"base {base[:12]}", "worker"))
        if conflicted or ((Path(worktree) / path).is_symlink() and merged != ours):
            refused.append((path, merged))
        else:
            results[path] = merged
    if not refused:
        return results
    head = g.out(worktree, "rev-parse", "--short=12", "HEAD")
    lines = [f"the worktree HEAD moved from the base {base[:12]} to {head}, but the worktree copies of "
             f"{', '.join(stale)} do not hold what main changed there since the base, and merging them against the "
             "base conflicts with main; main untouched, nothing committed"]
    for path, merged in refused:
        lines.append(f"{path}: changed on main since the base {base[:12]} by:")
        lines += [f"  {c}" for c in commits_since(root, base, "HEAD", [path])]
        hunks = conflict_hunks(merged.decode(errors="replace")) if merged else []
        lines += ["  conflicting hunks:"] + [f"    {h}" for h in hunks] if hunks else ["  deleted or binary on one side"]
    lines.append(rebase_hint(task_id))
    raise IntegrationError("\n".join(lines), EXIT_CONFLICT)


def restore(root, touched, existed):
    """Undo a failed merge: reset the touched paths to HEAD and delete files the merge created."""
    for path in touched:
        g.git(root, "reset", "-q", "--", path, check=False)
        if g.in_head(root, path):
            g.git(root, "checkout", "-q", "HEAD", "--", path, check=False)
        elif path not in existed:
            target = Path(root) / path
            if target.is_file() or target.is_symlink():
                target.unlink()


def merge(conf, task_id, subject=None, accept=(), skip_patches=(), commit=True, regenerate=True):
    queue = t.Queue(conf)
    task = queue.get(task_id)
    if task["status"] not in ("in_progress", "verification"):
        raise IntegrationError(f"{task_id} is {task['status']}; only an assigned or delivered task can be merged")
    worktree = Path(task.get("worktree") or "")
    if not task.get("worktree") or not worktree.is_dir():
        raise IntegrationError(f"{task_id} has no live worktree")
    root = conf.root
    dirty = [line[3:] for line in g.dirty_tracked(root).splitlines() if line[3:] not in (cfg.TASKS, cfg.ARCHIVE)]
    if dirty:
        raise IntegrationError("the main checkout has uncommitted changes to tracked files (" + ", ".join(dirty)
                               + "); commit or set them aside first", 2)
    base = g.out(root, "rev-parse", task["baseCommit"])
    rebased, stale = adopt_rebase(conf, queue, task)
    lines, outside = t.check_delivery(queue, task_id, accept)
    patches = worker_patches(worktree, skip_patches)
    shipped = {p for patch in patches for p in patch_paths(patch.read_text(errors="replace"))}
    foreign = [p for _, p in t.worktree_changes(worktree) if not t.is_owned(p, task["ownedPaths"])]
    covered, mismatches = applied_patches(root, worktree, patches, foreign)
    uncovered = [p for p in outside if p not in shipped]
    if uncovered or mismatches:
        hint = ["changes outside ownedPaths; reject them, move them to a patch, or pass --accept PATH"] if uncovered else []
        raise IntegrationError("\n".join(lines + mismatches + hint), EXIT_CONFLICT)
    if covered:
        lines, _ = t.check_delivery(queue, task_id, [*accept, *covered])
        lines.append(f"the worktree already holds what the shipped patches apply to {', '.join(sorted(covered))}; "
                     "applying the patches once")
    if rebased:
        lines.insert(0, rebased)
    diff = worktree_diff(worktree, sorted({p for p in shipped if not t.is_owned(p, task["ownedPaths"])} | set(stale)))
    touched = set(patch_paths(diff)) | set(stale)
    for patch in patches:
        touched.update(patch_paths(patch.read_text(errors="replace")))
    touched = sorted(touched)
    existed = {p for p in touched if (root / p).exists() or (root / p).is_symlink()}
    blocking = [p for p in existed if not g.tracked(root, p)]
    if blocking:
        raise IntegrationError("untracked files in main would be overwritten: " + ", ".join(blocking), EXIT_CONFLICT)
    merged = merge_stale(root, worktree, task_id, base, stale)
    report = list(lines)
    if stale:
        report.insert(0, f"the worktree HEAD moved from the base {base[:12]}, but the copies of {', '.join(stale)} do "
                         "not hold what main changed since; merged them three-way against the base")
    if diff.strip():
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as handle:
            handle.write(diff)
        try:
            result = g.git(root, "apply", "--3way", "--whitespace=nowarn", handle.name, check=False)
        finally:
            os.unlink(handle.name)
        unmerged = g.out(root, "diff", "--name-only", "--diff-filter=U").splitlines()
        if result.returncode != 0 or unmerged:
            explained = conflict_report(root, worktree, task_id, sorted(set(patch_paths(diff))), unmerged, result.stderr)
            restore(root, touched, existed)
            raise IntegrationError("\n".join(explained), EXIT_CONFLICT)
    for path, content in merged.items():
        write_bytes(root / path, content)
    if patches:
        ok, messages = apply_patches(root, patches, docs.generated_paths(conf) if regenerate else ())
        report += messages
        if not ok:
            restore(root, touched, existed)
            raise IntegrationError("\n".join(messages + ["a worker patch failed; main restored, nothing committed"]), EXIT_CONFLICT)
    repointed = repoint_folded_links(root, touched)
    generated = docs.regenerate(conf) if regenerate else []
    in_head = set(g.out(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines())
    paths = sorted({p for p in touched + repointed + generated if (root / p).exists() or g.tracked(root, p)})
    deleted = sorted({p for p in touched if p in in_head and p not in paths})
    handoff = worktree / cfg.WORKER_LOG / "handoff.md"
    if task["status"] == "in_progress" and handoff.exists():
        t.deliver(queue, task_id, handoff)
    if not commit:
        report.append(f"applied {len(paths)} path(s) without committing; commit them, then `theseus tasks merged {task_id} --commit SHA`")
        return None, report
    if not paths:
        raise IntegrationError("nothing to merge: the delivery changes no files")
    g.git(root, "add", "-A", "--", *paths)
    if g.git(root, "diff", "--cached", "--quiet", "--", *paths, check=False).returncode == 0:
        raise IntegrationError("nothing to merge: main already holds this delivery")
    staged = set(g.out(root, "diff", "--cached", "--name-only").split())
    stray = sorted(staged - set(paths) - set(deleted))
    if stray:
        g.git(root, "reset", "-q", "--", *stray)
    # A pathspec commit takes paths from the working tree and drops deleted files, so commit the staged index instead.
    g.git(root, "commit", "-q", "-m", f"{subject or task['objective']}\n\nDelivered by {task_id}.")
    sha = g.out(root, "rev-parse", "--short=12", "HEAD")
    report.append(t.mark_merged(queue, task_id, sha))
    commit_record(conf, f"chore: {task_id} merged in {sha}")
    archive = conf.workers_dir / worktree.name
    archive.mkdir(parents=True, exist_ok=True)
    if (worktree / cfg.WORKER_LOG).is_dir():
        shutil.copytree(worktree / cfg.WORKER_LOG, archive / cfg.WORKER_LOG, dirs_exist_ok=True)
    report.append(f"committed {sha}")
    return sha, report


def still_present(conf, commit):
    """Paths whose change in commit (generated indexes aside) still reverse-applies on HEAD. Checked per file, since a
    whole-patch check fails as soon as any one file is reverted."""
    root, skip = conf.root, set(docs.generated_paths(conf))
    names = g.out(root, "diff-tree", "--root", "--no-commit-id", "-r", "--name-only", "--no-renames", commit).splitlines()
    present = []
    with tempfile.TemporaryDirectory() as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        g.git(root, "read-tree", "HEAD", env=env)
        for name in names:
            if name in skip:
                continue
            diff = g.git(root, "diff-tree", "--root", "--no-commit-id", "-p", "--binary", "--no-renames", commit, "--",
                         name).stdout
            if g.git(root, "apply", "--cached", "--reverse", "--check", "-", env=env, input=diff, check=False).returncode == 0:
                present.append(name)
    return present


def unmerge(conf, task_id, reason, revert=True):
    """Take a merged task back to in_progress: revert its merge commit on main (or, with revert=False, check that it is
    already reverted) and keep the worktree, so the worker can redeliver and `theseus merge` runs as usual."""
    queue = t.Queue(conf)
    task = queue.get(task_id)
    if task["status"] != "merged":
        raise IntegrationError(f"{task_id} is {task['status']}; only a merged task can be unmerged")
    recorded = (task.get("merge") or {}).get("commit")
    if not recorded:
        raise IntegrationError(f"{task_id} records no merge commit")
    root = conf.root
    commit = g.git(root, "rev-parse", "--verify", "-q", f"{recorded}^{{commit}}", check=False).stdout.strip()
    if not commit or g.git(root, "merge-base", "--is-ancestor", commit, "HEAD", check=False).returncode != 0:
        raise IntegrationError(f"the {task_id} merge commit {recorded} is not on HEAD")
    if not revert:
        present = still_present(conf, commit)
        if present:
            raise IntegrationError(f"HEAD still holds the {task_id} merge {recorded} in: {', '.join(present)}; revert it "
                                   "first or drop --no-revert", EXIT_CONFLICT)
        message = t.unmerge(queue, task_id, reason)
        commit_record(conf, f"chore: {task_id} unmerged")
        return [message]
    dirty = [line[3:] for line in g.dirty_tracked(root).splitlines() if line[3:] not in (cfg.TASKS, cfg.ARCHIVE)]
    if dirty:
        raise IntegrationError("the main checkout has uncommitted changes to tracked files (" + ", ".join(dirty)
                               + "); commit or set them aside first", 2)
    g.git(root, "reset", "-q", "--", cfg.TASKS, cfg.ARCHIVE, check=False)
    generated = docs.generated_paths(conf)
    names = g.out(root, "diff-tree", "--root", "--no-commit-id", "-r", "--name-only", "--no-renames", commit).splitlines()
    touched = sorted(set(names) | set(generated))
    existed = {p for p in touched if (root / p).exists() or (root / p).is_symlink()}

    def refuse(text):
        g.git(root, "revert", "--quit", check=False)
        restore(root, touched, existed)
        raise IntegrationError(f"{text}; main restored, nothing committed", EXIT_CONFLICT)

    result = g.git(root, "revert", "--no-commit", commit, check=False)
    unmerged = g.out(root, "diff", "--name-only", "--diff-filter=U").splitlines()
    other = [p for p in unmerged if p not in generated]
    if other or (result.returncode != 0 and not unmerged):
        refuse(f"reverting {recorded} conflicts in {', '.join(other)}" if other
               else f"git revert {recorded} failed: {(result.stderr or result.stdout).strip()}")
    try:
        written = docs.regenerate(conf)
    except RuntimeError as error:
        refuse(str(error))
    paths = [p for p in sorted(set(written) | set(unmerged)) if (root / p).exists() or g.tracked(root, p)]
    if paths:
        g.git(root, "add", "-A", "--", *paths)
    if g.out(root, "diff", "--name-only", "--diff-filter=U"):
        refuse("unresolved paths remain after regenerating the indexes")
    if g.git(root, "diff", "--cached", "--quiet", check=False).returncode == 0:
        refuse(f"HEAD no longer holds {recorded}; if it was reverted by hand, pass --no-revert")
    g.git(root, "commit", "-q", "-m", f"Revert the {task_id} merge {recorded}\n\n{reason}")
    g.git(root, "revert", "--quit", check=False)
    sha = g.out(root, "rev-parse", "--short=12", "HEAD")
    message = t.unmerge(queue, task_id, reason, sha)
    commit_record(conf, f"chore: {task_id} unmerged in {sha}")
    return [f"reverted {recorded} in {sha}", message]
