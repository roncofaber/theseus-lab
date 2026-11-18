import subprocess
import time


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
