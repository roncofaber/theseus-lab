"""Host registry of theseus projects: every command that loads a project configuration records the project in the
host store's projects table (theseus.store), keyed by project name with the main checkout's root, so `theseus top` can
show every project on the machine. A project is written at most once per minute. When a second root claims a name that
a live root already holds, the first one stays and the conflict is recorded for `theseus doctor`; a root whose
theseus.toml is gone gives its name up to the next root that claims it. Projects are never deleted automatically:
entries() skips dead roots and `theseus db prune` removes them on request.

~/.local/share/theseus/projects.json (THESEUS_REGISTRY overrides the path), the registry before the store, is imported
once by the store migration and left in place.
"""
import os
import sqlite3
import time
from pathlib import Path

from . import store

THROTTLE = 60
CONFIG_NAME = "theseus.toml"


def path():
    if os.environ.get("THESEUS_REGISTRY"):
        return Path(os.environ["THESEUS_REGISTRY"])
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "theseus" / "projects.json"


def read(file):
    import json
    try:
        data = json.loads(Path(file).read_text())
    except (OSError, ValueError):
        return {}
    projects = data.get("projects") if isinstance(data, dict) else None
    return projects if isinstance(projects, dict) else {}


def alive(root):
    return (Path(root) / CONFIG_NAME).is_file() and canonical(root) == str(Path(root))


def canonical(root):
    """The main checkout of a Git worktree, so a worker's worktree never registers as a project of its own."""
    import subprocess
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return str(Path(root))
    common = Path(result.stdout.strip())
    return str(common.parent if common.name == ".git" else Path(root))


def entry(row):
    return {"name": row["name"], "root": row["root"], "data": row["data"], "coordinator": row["coordinator"] or "",
            "last_seen": row["last_seen_epoch"]}


def entries(include_closed=False):
    """Registered projects whose root still has a theseus.toml, sorted by name; a closed project (theseus.closure) only
    with include_closed, and then with its close record under "closed"."""
    from . import closure
    try:
        with store.for_host() as conn:
            rows = store.list_projects(conn)
    except (OSError, sqlite3.Error):
        return []
    found = []
    for row in rows:
        if not (row["root"] and alive(row["root"])):
            continue
        item = entry(row)
        record = closure.closed(item["data"]) if item["data"] else None
        if record and not include_closed:
            continue
        found.append({**item, "closed": record} if record else item)
    return sorted(found, key=lambda item: (item["name"] or "", item["root"]))


def conflicts():
    """Name conflicts whose refused root is still a live project: [{name, root (kept), other, seen}]."""
    with store.for_host() as conn:
        found = store.conflicts(conn)
    return [c for c in found if c["other"] != c["root"] and alive(c["other"])]


def record(conf, now=None):
    """Record conf's project; never raises (except for a store newer than this theseus), so a read-only home never
    breaks a command."""
    try:
        now = time.time() if now is None else now
        root = canonical(conf.root)
        if root != str(conf.root):
            return False
        with store.for_host() as conn:
            known = store.get_project(conn, conf.name)
            if known and known["root"] == root and now - (known["last_seen_epoch"] or 0) < THROTTLE:
                return False
            with store.transaction(conn):
                known = store.get_project(conn, conf.name)
                if known and known["root"] and known["root"] != root and alive(known["root"]):
                    store.add_conflict(conn, conf.name, root, now)
                    return False
                store.drop_conflict(conn, conf.name, root)
                store.upsert_project(conn, conf.name, root=root, data=conf.data,
                                     coordinator=str(conf.section("instances").get("coordinator") or ""), seen=now)
        return True
    except store.StoreError:
        raise
    except Exception:
        return False
