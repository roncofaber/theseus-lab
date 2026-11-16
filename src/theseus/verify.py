"""Build, smoke and full verification commands, the heavy-run semaphore and the pre-push guard.

Configured commands run through `bash -c` in the project root with the THESEUS_* environment. A command kind with
nothing configured is reported as NOT CONFIGURED with exit status 4: neither a silent pass nor a hard failure.

Full verification writes a log under <data>/logs/verify-<sha>.log whose first line names the commit and whose last line
is the exit status. The guard refuses pushes of code that no passing full log covers; THESEUS_PUSH_UNVERIFIED="reason"
overrides it (logged), but only while the code-only commits since the last full pass stay within
verification.max_unverified_merges and the oldest of them is younger than verification.max_unverified_hours. Code-only
rounds therefore cannot postpone the full pass indefinitely.
"""
import contextlib
import fcntl
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import gitutil as g

NOT_CONFIGURED = 4
HOOK_GIT_VARIABLES = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_OBJECT_DIRECTORY",
                      "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_QUARANTINE_PATH"}
LOG_HEADER = "theseus full verification of "
ZERO = "0" * 40


@contextlib.contextmanager
def heavy_slot(conf, wait=True):
    """Hold one of limits.heavy_runs slots (flock files under <data>/locks) for the duration of the block."""
    slots = max(1, int(conf.section("limits").get("heavy_runs") or 1))
    directory = conf.data / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    handles = [open(directory / f"heavy-{n}.lock", "w") for n in range(slots)]
    held = None
    try:
        while held is None:
            for handle in handles:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = handle
                    break
                except BlockingIOError:
                    continue
            if held is None:
                if not wait:
                    raise TimeoutError("all heavy-run slots are busy")
                time.sleep(2)
        yield
    finally:
        for handle in handles:
            handle.close()


def run_command(conf, command, log=None, timeout=None):
    env = {**os.environ, **conf.environment()}
    with heavy_slot(conf):
        try:
            result = subprocess.run(["bash", "-c", command], cwd=conf.root, env=env, capture_output=True, text=True, timeout=timeout)
            output, code = result.stdout + result.stderr, result.returncode
        except subprocess.TimeoutExpired as expired:
            output = (expired.stdout or b"").decode(errors="replace") if isinstance(expired.stdout, bytes) else (expired.stdout or "")
            output += f"\ntimed out after {timeout}s"
            code = 124
    if log:
        with open(log, "a") as handle:
            handle.write(f"$ {command}\n{output}\nexit status {code}\n")
    return code, output


def run_kind(conf, kind, timeout=None):
    """Run every command of one kind; prints one summary line per command. Returns the exit status."""
    commands = conf.commands(kind)
    if not commands:
        print(f"{kind}: NOT CONFIGURED (commands.{kind} is empty in theseus.toml); nothing ran")
        return NOT_CONFIGURED
    conf.logs.mkdir(parents=True, exist_ok=True)
    log = conf.logs / f"{kind}-{time.strftime('%Y%m%dT%H%M%S')}.log"
    status = 0
    for command in commands:
        code, output = run_command(conf, command, log, timeout)
        tail = output.strip().splitlines()[-1] if output.strip() else ""
        print(f"{kind}: {'pass' if code == 0 else 'FAIL'} (exit {code}) {command}" + (f"  | {tail[:160]}" if code else ""))
        status = status or (code if code else 0)
    print(f"{kind} log: {log}")
    return 1 if status else 0


def smoke(conf, push=False):
    status = run_kind(conf, "smoke", timeout=int(conf.section("commands").get("smoke_timeout") or 300))
    if status != 0 or not push:
        return status
    branch = conf.section("git").get("branch") or "main"
    remote = conf.section("git").get("remote") or "origin"
    result = subprocess.run(["git", "-C", str(conf.root), "push", remote, branch], text=True)
    return result.returncode


def parse_log_commit(text):
    first = text.splitlines()[0] if text else ""
    return first[len(LOG_HEADER):].split()[0] if first.startswith(LOG_HEADER) else None


def log_passed(text):
    statuses = [line.strip() for line in text.splitlines() if re.fullmatch(r"final status \d+", line.strip())]
    return bool(statuses) and statuses[-1] == "final status 0"


def full(conf, revision="HEAD"):
    """Run commands.verify on a clean detached worktree of the revision and write the verification log."""
    commands = conf.commands("verify")
    if not commands:
        print("verify: NOT CONFIGURED (commands.verify is empty in theseus.toml); nothing ran and no log was written")
        return NOT_CONFIGURED, None
    sha = g.out(conf.root, "rev-parse", revision)
    conf.logs.mkdir(parents=True, exist_ok=True)
    log = conf.logs / f"verify-{sha[:12]}-{time.strftime('%Y%m%dT%H%M%S')}.log"
    log.write_text(f"{LOG_HEADER}{sha}\n")
    conf.scratch.mkdir(parents=True, exist_ok=True)
    tree = Path(tempfile.mkdtemp(prefix="verify-", dir=conf.scratch)) / "tree"
    g.git(conf.root, "worktree", "add", "--detach", "--quiet", str(tree), sha)
    status = 0
    try:
        local = conf.root / "theseus.local.toml"
        if local.is_file():
            shutil.copy2(local, tree / "theseus.local.toml")
        env = {**os.environ, **conf.environment(), "THESEUS_ROOT": str(tree), "THESEUS_VERIFY_TREE": str(tree)}
        for command in commands:
            with heavy_slot(conf):
                result = subprocess.run(["bash", "-c", command], cwd=tree, env=env, capture_output=True, text=True)
            with open(log, "a") as handle:
                handle.write(f"$ {command}\n{result.stdout}{result.stderr}\nexit status {result.returncode}\n")
            print(f"verify: {'pass' if result.returncode == 0 else 'FAIL'} (exit {result.returncode}) {command}")
            status = status or result.returncode
    finally:
        g.git(conf.root, "worktree", "remove", "--force", str(tree), check=False)
        shutil.rmtree(tree.parent, ignore_errors=True)
    with open(log, "a") as handle:
        handle.write(f"final status {1 if status else 0}\n")
    print(f"verify log: {log}")
    return (1 if status else 0), log


def verified_commits(conf):
    verified = set()
    for log in conf.logs.glob("verify-*.log"):
        try:
            text = log.read_text(errors="replace")
        except OSError:
            continue
        commit = parse_log_commit(text)
        if commit and log_passed(text):
            verified.add(commit)
    return verified


def code_pathspecs(conf):
    section = conf.section("verification")
    return list(section.get("code_paths") or ["."]) + [f":(exclude){p}" for p in section.get("doc_paths") or []]


def code_commits_since_verified(conf, sha):
    """Commits reachable from sha that change code and that no passing full verification of a descendant covers."""
    known = [c for c in verified_commits(conf)
             if g.git(conf.root, "cat-file", "-e", f"{c}^{{commit}}", check=False).returncode == 0]
    exclude = ["--not", *known] if known else []
    return g.out(conf.root, "rev-list", sha, *exclude, "--", *code_pathspecs(conf)).split()


def unverified_age_hours(conf, commits):
    if not commits:
        return 0.0
    stamp = int(g.out(conf.root, "show", "-s", "--format=%ct", commits[-1]))
    return max(0.0, (time.time() - stamp) / 3600)


def debt(conf, revision="HEAD"):
    sha = g.out(conf.root, "rev-parse", revision)
    unverified = code_commits_since_verified(conf, sha)
    limit = int(conf.section("verification").get("max_unverified_merges") or 0)
    return unverified, limit


def fast_checks(conf):
    exe = sys.executable
    return [
        [exe, "-m", "theseus", "check"],
        [exe, "-m", "theseus", "docs", "check"],
    ]


def run_fast_checks(conf, sha):
    """Run the supervisor suite and docs checks on the pushed commit itself, never on the working tree."""
    conf.scratch.mkdir(parents=True, exist_ok=True)
    snapshot = Path(tempfile.mkdtemp(prefix="prepush-", dir=conf.scratch))
    tree = snapshot / "tree"
    g.git(conf.root, "worktree", "add", "--detach", "--quiet", str(tree), sha)
    try:
        for command in fast_checks(conf):
            env = {key: value for key, value in os.environ.items() if key not in HOOK_GIT_VARIABLES}
            env.update({"THESEUS_ROOT": str(tree), "THESEUS_DATA": str(conf.data),
                        "THESEUS_WORKTREES": str(conf.worktrees), "THESEUS_SCRATCH": str(conf.scratch)})
            local = conf.root / "theseus.local.toml"
            if local.exists():
                shutil.copy(local, tree / "theseus.local.toml")
            result = subprocess.run(command, cwd=tree, capture_output=True, text=True, env=env)
            if result.returncode != 0:
                return f"{' '.join(command[2:])} failed on {sha[:12]}:\n{result.stdout[-2000:]}{result.stderr[-2000:]}"
    finally:
        g.git(conf.root, "worktree", "remove", "--force", str(tree), check=False)
        shutil.rmtree(snapshot, ignore_errors=True)
    return None


def guard(conf, remote_name, lines, override=None):
    """Pre-push decision. Returns (exit status, messages)."""
    override = (override if override is not None else os.environ.get("THESEUS_PUSH_UNVERIFIED", "")).strip()
    heads = []
    for line in lines:
        parts = line.split()
        if len(parts) == 4 and parts[1] != ZERO and parts[1] not in heads:
            heads.append(parts[1])
    messages = []
    for head in heads:
        problem = run_fast_checks(conf, head)
        if problem:
            return 1, [f"pre-push: {problem}"]
    refused = []
    for head in heads:
        unverified, limit = debt(conf, head)
        if unverified:
            refused.append((head, unverified, limit))
    if not refused:
        return 0, messages
    if override:
        over = [(h, u, l) for h, u, l in refused if l and len(u) > l]
        for head, unverified, limit in over:
            messages.append(f"pre-push: {len(unverified)} code commits since the last full verification exceed "
                            f"verification.max_unverified_merges={limit}; run `theseus verify` before pushing (override refused)")
        hours = float(conf.section("verification").get("max_unverified_hours") or 0)
        for head, unverified, limit in refused:
            age = unverified_age_hours(conf, unverified)
            if hours and age > hours:
                messages.append(f"pre-push: the oldest unverified code commit is {age:.0f} h old, over "
                                f"verification.max_unverified_hours={hours:g}; run `theseus verify` before pushing (override refused)")
        if messages:
            return 1, messages
        conf.logs.mkdir(parents=True, exist_ok=True)
        with open(conf.logs / "unverified-pushes.log", "a") as handle:
            for head, unverified, limit in refused:
                handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} remote={remote_name} head={head} "
                             f"unverified={len(unverified)} newest={unverified[0]} reason={override}\n")
        messages.append("pre-push: pushing unverified code under THESEUS_PUSH_UNVERIFIED (logged)")
        return 0, messages
    for head, unverified, limit in refused:
        messages.append(f"pre-push: {unverified[0][:12]} changes code and no passing full verification covers it "
                        f"({len(unverified)} code commits since the last one). Run `theseus verify` and push after it passes, "
                        "or set THESEUS_PUSH_UNVERIFIED=\"reason\" for a logged code-only push.")
    return 1, messages
