"""Long-running queue commands outliving an upgrade: when `theseus queue run` or `queue wait` finds the host store
migrated by a newer theseus, it becomes the installed `theseus` (same arguments) if that one knows the new schema, and
otherwise waits for an upgrade instead of exiting. When the theseus code on disk changes under it, it restarts itself
the same way it was started, so a release never runs beside logic still loaded from the old one."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_told = set()


def installed():
    """The installed console script: THESEUS_INSTALLED, else `theseus` on PATH."""
    return os.environ.get("THESEUS_INSTALLED") or shutil.which("theseus")


def schema_of(script):
    """The host store schema version the theseus at script knows, or None: `--schema-version` where supported, else
    the store module imported by the script's own interpreter."""
    try:
        result = subprocess.run([script, "--schema-version"], capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip().isdigit():
            return int(result.stdout.strip())
        with open(script, "rb") as handle:
            first = handle.readline().decode(errors="replace").strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not first.startswith("#!") or "python" not in first:
        return None
    interpreter = first[2:].split()
    try:
        result = subprocess.run([*interpreter, "-c", "from theseus import store; print(store.SCHEMA_VERSION)"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return int(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip().isdigit() else None


class CodeChanged(Exception):
    """The theseus package on disk changed since this process loaded it."""


def code_stamp():
    """(file count, newest mtime) of the theseus package's Python sources, which any reinstall or edit changes."""
    files = list(Path(__file__).resolve().parent.rglob("*.py"))
    return len(files), max((f.stat().st_mtime_ns for f in files), default=0)


def restart(argv, execv=None):
    """Become a fresh process of the same entry point (`python -m theseus` or the console script) with argv."""
    entry = ["-m", "theseus"] if Path(sys.argv[0]).name == "__main__.py" else [sys.argv[0]]
    print("theseus: the installed theseus code changed; restarting with the same arguments", file=sys.stderr, flush=True)
    (execv or os.execv)(sys.executable, [sys.executable, *entry, *argv])


def newer_store(error, argv, interval=30, sleep=time.sleep, execv=os.execv):
    """Re-exec the installed theseus with argv when it knows the store's schema; otherwise print one line per
    situation and sleep interval seconds, so the caller retries."""
    script = installed()
    known = schema_of(script) if script else None
    if known is not None and known >= error.version:
        print(f"theseus: the host store is at schema {error.version}; continuing as {script}", file=sys.stderr, flush=True)
        execv(script, [script, *argv])
        return
    key = (error.version, script, known)
    if key not in _told:
        _told.add(key)
        where = f"the installed {script} knows schema {known}" if script else "no installed theseus is on PATH"
        print(f"theseus: the host store is at schema {error.version}, newer than this theseus, and {where}; retrying every "
              f"{interval:g}s until it is upgraded", file=sys.stderr, flush=True)
    sleep(interval)
