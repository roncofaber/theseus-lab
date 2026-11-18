"""Worker worktrees and the single integration path.

assign creates a worktree on a work/<name> branch at the configured base and records the assignment. merge brings one
delivery into the main checkout: ownership check, refusal when the worker committed, a three-way apply of the worktree
diff, the worker's outside-path patches, regenerated indexes, and a commit of exactly the touched paths. Any conflict
restores main and commits nothing.
"""
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
    import json
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


def adopt_rebase(conf, queue, task):
    """Record the worktree HEAD as the new base when it moved only onto commits of the configured base branch, as a
    worker rebase does. Returns a message, or None when HEAD is still the base. Refuses when the worker committed."""
    worktree = Path(task["worktree"])
    head = g.out(worktree, "rev-parse", "HEAD")
    base = g.out(conf.root, "rev-parse", task["baseCommit"])
    if head == base:
        return None
    ref = conf.section("git").get("base") or "HEAD"
    # Resolve in the main checkout: HEAD inside the worktree is the worker's own.
    tip = g.out(conf.root, "rev-parse", ref)
    own = int(g.out(conf.root, "rev-list", "--count", f"{tip}..{head}"))
    if own:
        raise IntegrationError(f"{task['id']}: the worker committed in its worktree (HEAD {head[:12]}, base {base[:12]}, "
                               f"{own} commit(s) not on {ref}); workers never commit. Inspect the commits, then reset "
                               "them into the working tree before merging.", EXIT_CONFLICT)
    return t.rebase(queue, task["id"], head[:12])


def rebase(conf, task_id):
    queue = t.Queue(conf)
    task = queue.get(task_id)
    if not task.get("worktree") or not Path(task["worktree"]).is_dir():
        raise IntegrationError(f"{task_id} has no live worktree")
    return adopt_rebase(conf, queue, task) or f"{task_id}: worktree HEAD is still the base {task['baseCommit']}"


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
    worktree = create_worktree(conf, name, base)
    rooms.exclude_dirty(conf, task, worktree)
    try:
        message = t.assign(queue, task_id, owner or f"{provider}:{model}:{name}", provider, model, worktree, base[:12],
                           worktree / cfg.WORKER_LOG, owner_id=owner_id)
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


def applied_patches(worktree, patches, outside):
    """Shipped patches the worktree also applied: each touches an outside path and leaves every path it touches exactly
    as the worktree has it. Returns (matched patches, the paths they cover, one line per patch that differs)."""
    worktree, outside = Path(worktree), set(outside)
    matched, covered, mismatches = [], set(), []
    for patch in patches:
        paths = sorted(set(patch_paths(patch.read_text(errors="replace"))))
        if not outside.intersection(paths):
            continue
        with tempfile.TemporaryDirectory() as scratch:
            patched = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "patched")}
            actual = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "actual")}
            g.git(worktree, "read-tree", "HEAD", env=patched)
            applied = g.git(worktree, "apply", "--cached", str(patch), env=patched, check=False)
            if applied.returncode != 0:
                mismatches.append(f"{patch.name} does not apply to the worktree base: {applied.stderr.strip()}")
                continue
            g.git(worktree, "read-tree", "HEAD", env=actual)
            present = [p for p in paths if (worktree / p).exists() or (worktree / p).is_symlink()]
            if present:
                g.git(worktree, "--literal-pathspecs", "add", "-f", "--", *present, env=actual)
            gone = [p for p in paths if p not in present]
            if gone:
                g.git(worktree, "--literal-pathspecs", "rm", "-q", "--cached", "--ignore-unmatch", "--", *gone, env=actual)
            entries = lambda env: set(g.out(worktree, "--literal-pathspecs", "ls-files", "-s", "--", *paths, env=env).splitlines())
            differ = sorted({line.split("\t", 1)[1] for line in entries(patched) ^ entries(actual)})
        if differ:
            mismatches.append(f"the worktree changes differ from what {patch.name} applies: {', '.join(differ)}")
        else:
            matched.append(patch)
            covered.update(paths)
    return matched, covered, mismatches


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
    rebased = adopt_rebase(conf, queue, task)
    lines, outside = t.check_delivery(queue, task_id, accept)
    patches = worker_patches(worktree, skip_patches)
    covered = set()
    if outside:
        matched, covered, mismatches = applied_patches(worktree, patches, outside)
        uncovered = [p for p in outside if p not in covered]
        if uncovered or mismatches:
            hint = ["changes outside ownedPaths; reject them, move them to a patch, or pass --accept PATH"] if uncovered else []
            raise IntegrationError("\n".join(lines + mismatches + hint), EXIT_CONFLICT)
        lines, _ = t.check_delivery(queue, task_id, [*accept, *covered])
        lines += [f"the worktree already holds {p.name} ({', '.join(sorted(set(patch_paths(p.read_text(errors='replace')))))}); "
                  "applying it once" for p in matched]
    if rebased:
        lines.insert(0, rebased)
    diff = worktree_diff(worktree, covered)
    touched = set(patch_paths(diff))
    for patch in patches:
        touched.update(patch_paths(patch.read_text(errors="replace")))
    touched = sorted(touched)
    existed = {p for p in touched if (root / p).exists() or (root / p).is_symlink()}
    blocking = [p for p in existed if not g.tracked(root, p)]
    if blocking:
        raise IntegrationError("untracked files in main would be overwritten: " + ", ".join(blocking), EXIT_CONFLICT)
    report = list(lines)
    if diff.strip():
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as handle:
            handle.write(diff)
        try:
            result = g.git(root, "apply", "--3way", "--whitespace=nowarn", handle.name, check=False)
        finally:
            os.unlink(handle.name)
        unmerged = g.out(root, "diff", "--name-only", "--diff-filter=U")
        if result.returncode != 0 or unmerged:
            restore(root, touched, existed)
            raise IntegrationError(f"apply failed; main restored, nothing committed\n{result.stderr.strip()}\n{unmerged}".rstrip(), EXIT_CONFLICT)
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
