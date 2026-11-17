"""Registry of the persistent agents that work a project, and their live state.

The registry lives in the host store's agents table (theseus.store), keyed by project name: panes and sessions are
machine state, not repository content. It starts from [[instances.panes]] in theseus.toml and is then edited while the
project runs: `theseus agents add|remove|enable|disable`. A disabled agent keeps its record but receives no new work
from the queue. A project's <data>/agents.json, the registry before the store, is imported once and left in place.
"""
import os
import re
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


def session_transcript(conf, agent):
    """The Claude Code transcript state (theseus.transcripts.read) of the session in the agent's pane, or None when
    the profile has no sessions directory or no live client names the pane."""
    from . import profiles
    from . import transcripts
    try:
        directory = (conf.section("instances").get("sessions_dir")
                     or profiles.interactive(conf, agent.get("provider")).get("sessions_dir"))
    except profiles.ProfileError:
        return None
    if not directory:
        return None
    address = tmux("display", "-p", "-t", agent["pane"], "#{window_id}.#{pane_id}").stdout.strip()
    try:
        return transcripts.pane_transcript(directory, address)
    except OSError:
        return None


def context_use(conf, profile, transcript, hint):
    """{"tokens", "limit", "model", "source"} of the agent's context: the transcript's last answer, else the pane's
    context hint, else None. The limit is the profile's context_limit."""
    from . import profiles
    try:
        limit = int(profiles.resolve(conf, profile).get("context_limit") or profiles.CONTEXT_LIMIT)
    except (profiles.ProfileError, ValueError, TypeError):
        limit = profiles.CONTEXT_LIMIT
    if transcript and transcript.get("tokens"):
        return {"tokens": transcript["tokens"], "limit": limit, "model": transcript.get("model"), "source": "transcript"}
    if hint:
        return {"tokens": hint, "limit": limit, "model": None, "source": "pane hint"}
    return None


def pane_screen(pane):
    return tmux("capture-pane", "-p", "-t", pane).stdout.splitlines()


def pane_busy(conf, pane, profile=None, screen=None):
    from . import profiles
    try:
        pattern = profiles.instance_env(conf, profile)["THESEUS_BUSY_PATTERN"]
    except profiles.ProfileError:
        pattern = conf.section("instances").get("busy_pattern") or ""
    if not pattern:
        return False
    screen = pane_screen(pane) if screen is None else screen
    return any(pattern in line for line in screen[-3:])


def pane_stall(conf, pane, profile=None, screen=None):
    """The first of the profile's stall patterns anywhere on the visible screen, or None."""
    from . import profiles
    try:
        patterns = profiles.stall_patterns(conf, profile)
    except profiles.ProfileError:
        return None
    if not patterns:
        return None
    text = "\n".join(pane_screen(pane) if screen is None else screen)
    return next((pattern for pattern in patterns if pattern in text), None)


def send_settings(conf):
    """(timeout, poll) in seconds for verified sends: instances.send_verify_seconds, THESEUS_SEND_VERIFY and
    THESEUS_SEND_POLL override them (tests)."""
    timeout = os.environ.get("THESEUS_SEND_VERIFY") or conf.section("instances").get("send_verify_seconds")
    poll = os.environ.get("THESEUS_SEND_POLL") or 0.5
    return (10.0 if timeout in (None, "") else float(timeout)), float(poll)


def input_holds(screen, text, pattern=None, lines=8):
    """Whether the pane's input line still holds the start of text (typed but never submitted). With the profile's
    input_pattern the last matching line in the bottom lines is the input line; without one, any bottom line counts."""
    wanted = " ".join(text.split())[:40]
    if not wanted:
        return False
    bottom = screen[-lines:]
    if pattern:
        regex = re.compile(pattern)
        held = [m.group(1) for m in (regex.match(line) for line in bottom) if m]
        if not held:
            return False
        content = " ".join(held[-1].split())
        return bool(content) and (wanted.startswith(content[:40]) or content.startswith(wanted))
    return any(wanted[:20] in " ".join(line.split()) for line in bottom)


def send_verified(conf, agent, text, task=None, expect_busy=True, profile=None, record=True, sleep=time.sleep):
    """Type text into an agent's pane and confirm it was submitted: within instances.send_verify_seconds the pane must
    show the profile's busy marker (expect_busy) or, for slash commands, no longer hold the text in its input line.
    If the text is still in the input line, Enter is pressed once more and the pane checked again. Returns (ok, detail);
    a failure records a send_failed event unless record is False. Profiles without a busy marker are sent unverified."""
    from . import profiles
    from . import workqueue
    pane = agent["pane"]
    profile = profile or workqueue.launched(conf, agent)
    settings = profiles.interactive(conf, profile)
    try:
        busy_pattern = profiles.instance_env(conf, profile)["THESEUS_BUSY_PATTERN"]
    except profiles.ProfileError:
        busy_pattern = conf.section("instances").get("busy_pattern") or ""
    timeout, poll = send_settings(conf)
    input_pattern = settings.get("input_pattern")

    def submitted():
        screen = pane_screen(pane)
        if expect_busy:
            return any(busy_pattern in line for line in screen[-3:]), screen
        return not input_holds(screen, text, input_pattern), screen

    def wait():
        deadline = time.monotonic() + timeout
        while True:
            ok, screen = submitted()
            if ok or time.monotonic() >= deadline:
                return ok, screen
            sleep(poll)

    if settings.get("clear_input"):
        tmux("send-keys", "-t", pane, settings["clear_input"])
    typed = tmux("send-keys", "-t", pane, "-l", text)
    if typed.returncode != 0:
        detail = f"tmux send-keys failed: {(typed.stderr or '').strip()[:120]}"
    else:
        sleep(0.3)
        tmux("send-keys", "-t", pane, "Enter")
        if expect_busy and not busy_pattern:
            return True, "sent (unverified: the profile has no busy marker)"
        ok, screen = wait()
        if ok:
            return True, "sent"
        if input_holds(screen, text, input_pattern):
            tmux("send-keys", "-t", pane, "Enter")
            ok, screen = wait()
            if ok:
                return True, "sent after a second Enter"
            detail = "the input line still holds the text after a second Enter"
        else:
            detail = "not busy after Enter and the text is not in the input line"
        detail += f" ({timeout:g}s each)"
    if record:
        short = " ".join(text.split())
        workqueue.event(conf, "send_failed", agent["name"], task, f"{detail}: {short[:60]!r}")
    return False, detail


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
    """Live state: offline, busy, working, delivered, thrashing, gateway_error, stalled, quiet, free (plus disabled).
    An idle pane whose bottom lines show a gateway error of its profile while it holds a task is gateway_error, with the
    parsed error in "gateway" (theseus.gateway); "contextTokens" is the profile's context hint when shown. A handoff counts
    as delivered only once the pane is idle and the handoff is final (handoff_final). A pane that shows one of the profile's
    stall patterns (interactive.stall_patterns) while it holds a task is thrashing at once, busy or not, with the
    pattern in "stall". Without a busy marker for the agent's profile a long silence is reported as quiet, never stalled, so the
    queue does not type nudges into a working agent."""
    from . import gateway
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
    screen = pane_screen(pane)
    busy = pane_busy(conf, pane, agent.get("provider"), screen)
    info["busy"] = busy
    info["contextTokens"] = gateway.context_tokens(conf, agent.get("provider"), screen)
    transcript = session_transcript(conf, agent)
    info["context"] = context_use(conf, agent.get("provider"), transcript, info["contextTokens"])
    transcript_error = (transcript["errors"][-1] if transcript and transcript.get("last") == "error" and transcript["errors"]
                        else None)
    if task:
        handoff = handoff_path(task)
        worktree = task.get("worktree")
        newest = newest_change([p for p in (worktree, handoff.parent if handoff else None) if p])
        info["idleMinutes"] = int((time.time() - newest) / 60) if newest else None
        info["lastChange"] = newest
        if handoff and handoff.exists() and not busy and handoff_final(conf, handoff):
            info["state"] = "delivered"
            info["result"] = t.handoff_result(handoff)
        elif stall := pane_stall(conf, pane, agent.get("provider"), screen):
            info["state"] = "thrashing"
            info["stall"] = stall
        elif not busy and (error := gateway.detect(conf, agent.get("provider"), screen, transcript_error)) \
                and not gateway.stale(conf, agent["name"], error):
            info["state"] = "gateway_error"
            info["gateway"] = error
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
