"""Registry of the persistent agents that work a project, and their live state.

The registry lives in the host store's agents table (theseus.store), keyed by project name: panes and sessions are
machine state, not repository content. It starts from [[instances.panes]] in theseus.toml and is then edited while the
project runs: `theseus agents add|remove|enable|disable`. A disabled agent keeps its record but receives no new work
from the queue. A project's <data>/agents.json, the registry before the store, is imported once and left in place.
"""
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config as cfg
from . import store


class AgentError(Exception):
    pass


def registry_path(conf):
    """The pre-store agents.json, read once by the store migration."""
    return conf.data / "agents.json"


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def marker(conf):
    return f"agents:{conf.name}"


def seed(conf, conn):
    """Fill an untouched registry from [[instances.panes]] once; after that the registry is edited only by commands,
    so removing every agent never brings the configured panes back."""
    if store.get_meta(conn, marker(conf)) is not None:
        return
    panes = conf.section("instances").get("panes") or []
    if not panes:
        return
    with store.transaction(conn):
        if store.get_meta(conn, marker(conf)) is not None:
            return
        default = conf.section("workers").get("default", "claude-code")
        for pane in panes:
            store.upsert_agent(conn, conf.name, pane["name"], pane=pane["pane"], profile=default, source=cfg.CONFIG_NAME)
        store.set_meta(conn, marker(conf), now())


def entry(row):
    record = {"name": row["name"], "pane": row["pane"], "provider": row["profile"], "enabled": row["enabled"],
              "registered": row["registered"]}
    if row["rooms"]:
        record["rooms"] = row["rooms"]
    if row["source"]:
        record["source"] = row["source"]
    return record


def load(conf):
    with store.for_project(conf) as conn:
        seed(conf, conn)
        return {"agents": [entry(row) for row in store.list_agents(conn, conf.name)]}


def registered(conf):
    """Whether the registry was seeded or edited, so it, not [[instances.panes]], lists this project's agents."""
    with store.for_project(conf) as conn:
        return store.get_meta(conn, marker(conf)) is not None


def agents(conf, include_disabled=True):
    return [a for a in load(conf)["agents"] if include_disabled or a.get("enabled", True)]


def get(conf, name):
    for agent in agents(conf):
        if agent["name"] == name:
            return agent
    raise AgentError(f"no registered agent {name}; `theseus agents list`")


def add(conf, name, pane, provider=None, rooms=None):
    with store.for_project(conf) as conn:
        seed(conf, conn)
        with store.transaction(conn):
            existing = store.list_agents(conn, conf.name)
            if any(a["name"] == name for a in existing):
                raise AgentError(f"{name} is already registered")
            if any(a["pane"] == pane for a in existing):
                raise AgentError(f"pane {pane} already belongs to a registered agent")
            store.upsert_agent(conn, conf.name, name, pane=pane,
                               profile=provider or conf.section("workers").get("default", "claude-code"),
                               rooms=list(rooms) if rooms else None, registered=now())
            store.set_meta(conn, marker(conf), now())
    return f"registered {name} on pane {pane}"


def remove(conf, name):
    with store.for_project(conf) as conn:
        seed(conf, conn)
        with store.transaction(conn):
            if not store.remove_agent(conn, conf.name, name):
                raise AgentError(f"no registered agent {name}")
            store.set_meta(conn, marker(conf), now())
    return f"removed {name}; its open task, if any, stays assigned until you release it (theseus tasks release ID)"


def set_enabled(conf, name, enabled):
    with store.for_project(conf) as conn:
        seed(conf, conn)
        with store.transaction(conn):
            if not store.set_enabled(conn, conf.name, name, enabled):
                raise AgentError(f"no registered agent {name}")
            store.set_meta(conn, marker(conf), now())
    return f"{name} {'enabled' if enabled else 'disabled: it finishes its current task and gets no new work'}"


def tmux(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def pane_alive(pane):
    return tmux("display", "-p", "-t", pane, "#{pane_pid}").returncode == 0


def pane_busy(conf, pane, profile=None):
    from . import profiles
    try:
        pattern = profiles.instance_env(conf, profile)["THESEUS_BUSY_PATTERN"]
    except profiles.ProfileError:
        pattern = conf.section("instances").get("busy_pattern") or ""
    if not pattern:
        return False
    screen = tmux("capture-pane", "-p", "-t", pane).stdout.splitlines()
    return any(pattern in line for line in screen[-3:])


def current_task(conf, name):
    """The open task assigned to this agent (owner provider:<name>:...), if any."""
    from . import tasks as t
    for task in t.Queue(conf).load()["tasks"]:
        owner = task.get("owner") or ""
        if task["status"] == "in_progress" and owner.split(":")[1:2] == [name]:
            return task
    return None


def handoff_path(task):
    log_dir = (task.get("execution") or {}).get("logDir")
    return Path(log_dir) / "handoff.md" if log_dir else None


def newest_change(paths):
    newest = 0
    for base in paths:
        base = Path(base)
        if not base.exists():
            continue
        candidates = [base] if base.is_file() else base.rglob("*")
        for path in candidates:
            if ".git" in path.parts:
                continue
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
    return newest or None


def idle_limit(conf):
    value = conf.section("instances").get("idle_minutes")
    return 5 if value is None else int(value)


def busy_detectable(conf, profile=None):
    from . import profiles
    try:
        return bool(profiles.instance_env(conf, profile)["THESEUS_BUSY_PATTERN"])
    except profiles.ProfileError:
        return bool(conf.section("instances").get("busy_pattern"))


def handoff_final(conf, handoff):
    """A handoff counts once it states a Result and has not changed for instances.handoff_settle_seconds: workers
    sometimes write an interim handoff and keep going, or stop to ask a question before finishing it."""
    from . import tasks as t
    settle = conf.section("instances").get("handoff_settle_seconds")
    settle = 45 if settle is None else float(settle)
    try:
        age = time.time() - handoff.stat().st_mtime
    except OSError:
        return False
    return t.handoff_result(handoff) is not None and age >= settle


def state(conf, agent):
    """Live state: offline, busy, working, delivered, stalled, quiet, free (plus disabled). A handoff counts as delivered
    only once the pane is idle and the handoff is final (handoff_final). Without a busy marker for the agent's profile a
    long silence is reported as quiet, never stalled, so the queue does not type nudges into a working agent."""
    from . import rooms
    from . import tasks as t
    pane = agent["pane"]
    task = current_task(conf, agent["name"])
    info = {"name": agent["name"], "pane": pane, "provider": agent.get("provider", ""), "enabled": agent.get("enabled", True),
            "task": task["id"] if task else None, "room": (task or {}).get("room"), "idleMinutes": None,
            "tainted": rooms.ledger(conf)["tainted"].get(agent["name"]) if rooms.enabled(conf) else None}
    if not pane_alive(pane):
        info["state"] = "offline"
        return info
    busy = pane_busy(conf, pane, agent.get("provider"))
    if task:
        handoff = handoff_path(task)
        worktree = task.get("worktree")
        newest = newest_change([p for p in (worktree, handoff.parent if handoff else None) if p])
        info["idleMinutes"] = int((time.time() - newest) / 60) if newest else None
        if handoff and handoff.exists() and not busy and handoff_final(conf, handoff):
            info["state"] = "delivered"
            info["result"] = t.handoff_result(handoff)
        elif busy:
            info["state"] = "working"
        elif info["idleMinutes"] is not None and info["idleMinutes"] >= idle_limit(conf):
            info["state"] = "stalled" if busy_detectable(conf, agent.get("provider")) else "quiet"
        else:
            info["state"] = "working"
    else:
        info["state"] = "busy" if busy else ("free" if info["enabled"] else "disabled")
    return info


def states(conf):
    return [state(conf, agent) for agent in agents(conf)]


def instance_entries(conf):
    """Name and pane of every registered agent, for the instance scripts."""
    return [{"name": a["name"], "pane": a["pane"]} for a in agents(conf)]


def ensure_known(conf, name):
    if not any(a["name"] == name for a in agents(conf)):
        raise cfg.ConfigError(f"{name} is not a registered agent")
