"""Reclaim disk from retired worker worktrees and old scratch while preserving evidence.

Dry run by default. Anything an open task mentions is kept. Before removal, small evidence-like files (logs, manifests,
evidence JSON, prompts, reports) are copied to <data>/archive/; worktrees keep their .theseus/ worker records, and
dirty worktrees are saved as a status listing plus a tarball of changed files. Only entries inside the configured
scratch root and worktrees of this repository are ever considered.
"""
import os
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

from . import config as cfg

PRESERVE_SUFFIXES = {".log", ".json", ".jsonl", ".sha256", ".md", ".txt", ".tsv", ".csv", ".patch", ".diff", ".pcapng", ".pcap"}
PRESERVE_MAX_BYTES = 64 << 20
SKIP_DIRS = {"bin", "obj", "node_modules", ".git", "__pycache__", ".venv"}


def referenced_strings(conf):
    from . import tasks as t
    document = t.Queue(conf).load()
    found = set()

    def walk(value):
        if isinstance(value, str):
            found.add(value)
            found.update(token for token in re.split(r"[\s,;:\"'()\[\]]+", value) if token)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(document.get("tasks", []))
    return found


def is_referenced(path, referenced):
    prefix = str(path) + "/"
    return str(path) in referenced or path.name in referenced or any(item.startswith(prefix) for item in referenced)


def tree_bytes_and_mtime(path):
    total, newest = 0, path.lstat().st_mtime
    if path.is_file() or path.is_symlink():
        return path.lstat().st_size, newest
    for base, _, files in os.walk(path):
        for name in files:
            try:
                info = os.lstat(os.path.join(base, name))
            except OSError:
                continue
            total += info.st_size
            newest = max(newest, info.st_mtime)
    return total, newest


def preservable(path):
    candidates = [path] if path.is_file() else []
    if path.is_dir():
        for base, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            candidates += [Path(base) / name for name in files]
    for candidate in candidates:
        try:
            if candidate.suffix in PRESERVE_SUFFIXES and not candidate.is_symlink() and candidate.stat().st_size <= PRESERVE_MAX_BYTES:
                yield candidate
        except OSError:
            continue


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def scratch(conf, apply, cutoff, referenced, lines):
    root = conf.scratch
    if not root.is_dir() or root in (Path("/"), Path.home(), Path("/tmp")):
        return 0
    freed = 0
    for entry in sorted(root.iterdir()):
        if is_referenced(entry, referenced):
            lines.append(f"keep     {entry.name} (referenced by an open task)")
            continue
        size, newest = tree_bytes_and_mtime(entry)
        if newest > cutoff:
            lines.append(f"keep     {entry.name} (modified recently)")
            continue
        preserved = 0
        for source in preservable(entry):
            preserved += source.stat().st_size
            if apply:
                target = conf.data / "archive/scratch" / source.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        lines.append(f"{'remove' if apply else 'would rm'} {entry.name} ({human(size)}; preserve {human(preserved)})")
        if apply:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        freed += size
    return freed


def worktrees(conf, apply, cutoff, referenced, lines):
    listing = subprocess.run(["git", "-C", str(conf.root), "worktree", "list", "--porcelain"], capture_output=True, text=True, check=True).stdout
    paths, locked = [], set()
    for line in listing.splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line.split(" ", 1)[1]))
        elif line.startswith("locked") and paths:
            locked.add(paths[-1])
    freed = 0
    for path in paths:
        if path == conf.root or not path.exists():
            continue
        if path in locked:
            lines.append(f"keep     worktree {path.name} (locked)")
            continue
        if conf.worktrees not in path.parents:
            lines.append(f"keep     worktree {path.name} (outside paths.worktrees)")
            continue
        if is_referenced(path, referenced):
            lines.append(f"keep     worktree {path.name} (referenced by an open task)")
            continue
        size, newest = tree_bytes_and_mtime(path)
        if newest > cutoff:
            lines.append(f"keep     worktree {path.name} (modified recently)")
            continue
        status = subprocess.run(["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"], capture_output=True, text=True).stdout
        changed = [line[3:].split(" -> ")[-1] for line in status.splitlines() if line]
        lines.append(f"{'remove' if apply else 'would rm'} worktree {path.name} ({human(size)}; {'dirty: ' + str(len(changed)) + ' file(s) saved' if changed else 'clean'})")
        if apply:
            save = conf.data / "archive/worktrees" / path.name
            if (path / cfg.WORKER_LOG).is_dir():
                shutil.copytree(path / cfg.WORKER_LOG, save / cfg.WORKER_LOG, dirs_exist_ok=True)
            if changed:
                save.mkdir(parents=True, exist_ok=True)
                head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
                (save / "status.txt").write_text(f"HEAD {head}{status}")
                with tarfile.open(save / "changed-files.tar.gz", "w:gz") as tar:
                    for relative in changed:
                        if (path / relative).exists():
                            tar.add(path / relative, arcname=relative)
            subprocess.run(["git", "-C", str(conf.root), "worktree", "remove", "--force", str(path)], check=True)
        freed += size
    if apply:
        subprocess.run(["git", "-C", str(conf.root), "worktree", "prune"], check=True)
    return freed


def run(conf, apply=False, max_age_hours=24, skip_worktrees=False, skip_scratch=False):
    referenced = referenced_strings(conf)
    cutoff = time.time() - max_age_hours * 3600
    lines, freed = [], 0
    if not skip_worktrees:
        freed += worktrees(conf, apply, cutoff, referenced, lines)
    if not skip_scratch:
        freed += scratch(conf, apply, cutoff, referenced, lines)
    lines.append(f"{'freed' if apply else 'would free'} {human(freed)}" + ("" if apply else " (dry run; pass --apply to act)"))
    return lines
