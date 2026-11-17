"""Host-wide worker pools: several projects share one provider limit, such as a model gateway that allows a fixed
number of concurrent agents.

~/.config/theseus/pools.toml (THESEUS_POOLS overrides the path) declares each pool:

    [pools.gateway]
    max = 4                               # agents working a task at once, across every project
    priority = ["laser-port", "game-port"] # earlier projects win when the pool is full

A profile joins a pool with `pool = "<name>"` in its [workers.<profile>] section. Only agents working a task hold a
slot; idle sessions cost nothing. Every queue tick refreshes its project's slots in
~/.local/share/theseus/pools/<name>.json (THESEUS_POOL_STATE overrides the directory). A project that has ready work
but finds the pool full records a claim; while a fresh claim from a higher-priority project exists, lower-priority
projects start no new tasks, so their running tasks finish and the slots return. Slots a project stopped refreshing
expire, so a stopped queue never holds the pool forever.
"""
import contextlib
import fcntl
import json
import os
import time
import tomllib
from pathlib import Path

CLAIM_TTL = 15 * 60
LEASE_TTL = 30 * 60


def config_path():
    if os.environ.get("THESEUS_POOLS"):
        return Path(os.environ["THESEUS_POOLS"])
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "theseus" / "pools.toml"


def state_dir():
    if os.environ.get("THESEUS_POOL_STATE"):
        return Path(os.environ["THESEUS_POOL_STATE"])
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "theseus" / "pools"


def definitions():
    path = config_path()
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text()).get("pools", {})


def pool_of(conf, profile_name):
    from . import profiles
    try:
        name = profiles.resolve(conf, profile_name).get("pool")
    except profiles.ProfileError:
        return None
    return name if name and name in definitions() else None


@contextlib.contextmanager
def locked(pool):
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pool}.json"
    with open(directory / f"{pool}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            state = {}
        state.setdefault("leases", {})
        state.setdefault("claims", {})
        now = time.time()
        state["leases"] = {k: v for k, v in state["leases"].items()
                           if (v.get("until") or v.get("seen", 0) + LEASE_TTL) > now}
        state["claims"] = {k: v for k, v in state["claims"].items() if now - v < CLAIM_TTL}
        yield state
        path.write_text(json.dumps(state, indent=2) + "\n")


def key(conf, agent):
    return f"{conf.name}/{agent}"


def sync(conf, pool, held):
    """Replace this project's slots with the agents now working a task: {agent name: task id}."""
    now = time.time()
    with locked(pool) as state:
        prefix = f"{conf.name}/"
        state["leases"] = {k: v for k, v in state["leases"].items() if not k.startswith(prefix)}
        for agent, task in held.items():
            state["leases"][key(conf, agent)] = {"project": conf.name, "agent": agent, "task": task, "seen": now}


def may_start(conf, pool):
    """(True, "") when this project may start one more task in the pool, else (False, reason); a full pool records
    this project's claim."""
    spec = definitions().get(pool, {})
    limit = int(spec.get("max") or 0)
    priority = list(spec.get("priority") or [])
    with locked(pool) as state:
        active = len(state["leases"])
        if conf.name in priority:
            ahead = priority[: priority.index(conf.name)]
        else:
            ahead = priority
        waiting = [p for p in ahead if p in state["claims"]]
        if waiting:
            return False, f"yielding to {waiting[0]} in pool {pool}"
        if limit and active >= limit:
            state["claims"][conf.name] = time.time()
            return False, f"pool {pool} full ({active}/{limit})"
        return True, ""


def take(conf, pool, agent, task):
    with locked(pool) as state:
        state["leases"][key(conf, agent)] = {"project": conf.name, "agent": agent, "task": task, "seen": time.time()}


def hold(project, pool, agent, task, hours=4.0):
    """Hold a slot by hand for a worker no theseus queue tracks (it expires after hours unless released). Refused when
    the pool is full or a higher-priority project claims it; returns (ok, reason)."""
    spec = definitions().get(pool, {})
    limit = int(spec.get("max") or 0)
    priority = list(spec.get("priority") or [])
    with locked(pool) as state:
        ahead = priority[: priority.index(project)] if project in priority else priority
        waiting = [p for p in ahead if p in state["claims"]]
        if waiting:
            return False, f"yielding to {waiting[0]} in pool {pool}"
        if f"{project}/{agent}" not in state["leases"] and limit and len(state["leases"]) >= limit:
            return False, f"pool {pool} full ({len(state['leases'])}/{limit})"
        now = time.time()
        state["leases"][f"{project}/{agent}"] = {"project": project, "agent": agent, "task": task, "seen": now,
                                                 "until": now + hours * 3600}
        return True, ""


def release(project, pool, agent):
    with locked(pool) as state:
        return state["leases"].pop(f"{project}/{agent}", None) is not None


def drop_claim(conf, pool):
    with locked(pool) as state:
        state["claims"].pop(conf.name, None)


def status(pool):
    spec = definitions().get(pool, {})
    with locked(pool) as state:
        leases = dict(state["leases"])
        claims = dict(state["claims"])
    by_project = {}
    for lease in leases.values():
        by_project[lease["project"]] = by_project.get(lease["project"], 0) + 1
    return {"pool": pool, "max": int(spec.get("max") or 0), "priority": list(spec.get("priority") or []),
            "active": len(leases), "projects": by_project, "claims": sorted(claims), "leases": leases}


def project_pools(conf):
    """The pools this project's registered agents use."""
    from . import agents as ag
    return sorted({p for p in (pool_of(conf, a.get("provider")) for a in ag.agents(conf)) if p})
