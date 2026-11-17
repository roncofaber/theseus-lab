"""Host registry of theseus projects: every command that loads a project configuration records the project in
~/.local/share/theseus/projects.json (THESEUS_REGISTRY overrides the path), so `theseus top` can show every project on
the machine. A project is written at most once per minute; entries whose root lost its theseus.toml are dropped.
"""
import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

THROTTLE = 60
CONFIG_NAME = "theseus.toml"


def path():
    if os.environ.get("THESEUS_REGISTRY"):
        return Path(os.environ["THESEUS_REGISTRY"])
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "theseus" / "projects.json"


def read(file):
    try:
        data = json.loads(file.read_text())
    except (OSError, ValueError):
        return {}
    projects = data.get("projects") if isinstance(data, dict) else None
    return projects if isinstance(projects, dict) else {}


def alive(root):
    return (Path(root) / CONFIG_NAME).is_file()


def entries():
    """Registered projects whose root still has a theseus.toml, sorted by name."""
    found = [dict(entry, root=root) for root, entry in read(path()).items() if alive(root)]
    return sorted(found, key=lambda entry: (entry.get("name") or "", entry["root"]))


@contextlib.contextmanager
def locked(file):
    file.parent.mkdir(parents=True, exist_ok=True)
    with open(file.with_name(file.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def record(conf, now=None):
    """Record conf's project; never raises, so a read-only home never breaks a command."""
    try:
        now = time.time() if now is None else now
        file = path()
        root = str(conf.root)
        previous = read(file).get(root)
        if previous and now - previous.get("last_seen", 0) < THROTTLE:
            return False
        with locked(file):
            projects = {r: e for r, e in read(file).items() if alive(r)}
            projects[root] = {"name": conf.name, "data": str(conf.data),
                              "coordinator": str(conf.section("instances").get("coordinator") or ""), "last_seen": now}
            temporary = file.with_name(f"{file.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps({"projects": projects}, indent=2) + "\n")
            os.replace(temporary, file)
        return True
    except Exception:
        return False
