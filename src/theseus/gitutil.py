import os
import subprocess
import tempfile
import time
from pathlib import Path


class GitError(Exception):
    pass


LOCK_RETRIES = (0.5, 1.0, 2.0, 4.0)


def git(root, *args, check=True, env=None, input=None):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, env=env, input=input)
    # Another theseus process (a merge, a queue tick) may hold the index for a moment.
    for delay in LOCK_RETRIES:
        if result.returncode == 0 or "index.lock" not in (result.stderr or ""):
            break
        time.sleep(delay)
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, env=env, input=input)
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(result.stderr or result.stdout).strip()}")
    return result


def out(root, *args, **kwargs):
    return git(root, *args, **kwargs).stdout.strip()


def has_remote(root, remote):
    return remote in out(root, "remote").split()


def tracked(root, path):
    return git(root, "ls-files", "--error-unmatch", "--", path, check=False).returncode == 0


def in_head(root, path):
    return git(root, "cat-file", "-e", f"HEAD:{path}", check=False).returncode == 0


def dirty_tracked(root):
    return git(root, "status", "--porcelain", "--untracked-files=no").stdout.rstrip("\n")


def commit_paths(root, message, paths):
    """Commit the working-tree state of paths on top of HEAD through a temporary index, so nothing else the user has
    staged is committed or unstaged; the real index entries of paths then match the new HEAD. False when paths hold
    no change."""
    with tempfile.TemporaryDirectory() as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        git(root, "read-tree", "HEAD", env=env)
        known = set(git(root, "--literal-pathspecs", "ls-files", "-z", "--", *paths, env=env).stdout.split("\0"))
        present = [p for p in paths if p in known or os.path.lexists(Path(root) / p)]
        if present:
            git(root, "--literal-pathspecs", "add", "-A", "--", *present, env=env)
        if git(root, "diff", "--cached", "--quiet", "HEAD", env=env, check=False).returncode == 0:
            return False
        git(root, "commit", "-q", "-m", message, env=env)
    # The commit stands even when the index cannot be synced; the paths then merely show as staged reversions.
    git(root, "--literal-pathspecs", "reset", "-q", "--", *present, check=False)
    return True
