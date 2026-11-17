"""Long-running queue commands outliving a store upgrade: when `theseus queue run` or `queue wait` finds the host store
migrated by a newer theseus, it becomes the installed `theseus` (same arguments) if that one knows the new schema, and
otherwise waits for an upgrade instead of exiting."""
import os
import shutil
import subprocess
import sys
import time

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
