"""Registry of the persistent agents that work a project, and their live state.

The registry lives in the host store's agents table (theseus.store), keyed by project name: panes and sessions are
machine state, not repository content. It starts from [[instances.panes]] in theseus.toml and is then edited while the
project runs: `theseus agents add|remove|enable|disable`. A disabled agent keeps its record but receives no new work
from the queue. A project's <data>/agents.json, the registry before the store, is imported once and left in place.
"""
import os
import re
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
    if row.get("managed"):
        record.update({"managed": True, "server": row.get("server"), "target": row.get("target"),
                       "session_id": row.get("session_id"), "slot": row.get("slot"), "desired": row.get("desired"),
                       "phase": row.get("phase"), "profile": row["profile"]})
    return record


def display_name(conf, agent):
    """A short name for the console: a supervised slot is <profile>-<n>; otherwise the name without the project prefix."""
    if agent.get("managed") and agent.get("slot") and agent.get("profile"):
        return f"{agent['profile']}-{agent['slot']}"
    name = agent["name"]
    for prefix in (f"{conf.name}-", f"{conf.prefix}-"):
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix):]
    return name


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


def where(agent_or_pane, server=None):
    """(server, target) of an agent record or pane: managed agents on their private server, the rest on the user's."""
    from . import supervisor, tmux as tm
    if server is not None:
        return server, agent_or_pane["pane"] if isinstance(agent_or_pane, dict) else agent_or_pane
    if isinstance(agent_or_pane, dict):
        return supervisor.server_of(agent_or_pane), agent_or_pane["pane"]
    return tm.resolve(agent_or_pane)


def tmux(*args):
    """tmux on the user's own server, for hand-made panes."""
    from . import tmux as tm
    return tm.Server.legacy().run(*args)


def on(server, *args):
    return tmux(*args) if server is None or server.is_legacy else server.run(*args)


def session_transcript(conf, agent):
    """The Claude Code transcript state (theseus.transcripts.read) of the session in the agent's pane, or None when
    the profile has no sessions directory or no live client names the pane. A supervised agent's stored session id
    (kept current from the agent CLI's status) names its transcript directly."""
    from . import profiles
    from . import transcripts
    try:
        directory = (conf.section("instances").get("sessions_dir")
                     or profiles.interactive(conf, agent.get("provider")).get("sessions_dir"))
    except profiles.ProfileError:
        return None
    if not directory:
        return None
    server, pane = where(agent)
    if agent.get("managed") and agent.get("session_id"):
        from . import supervisor
        path = transcripts.transcript_path(directory, agent["session_id"], str(supervisor.cwd(conf)))
        if path.exists():
            try:
                return transcripts.read(path)
            except OSError:
                return None
    pids, exclude = None, ()
    if server.is_legacy:
        address = tmux("display", "-p", "-t", pane, "#{window_id}.#{pane_id}").stdout.strip()
        exclude = supervised_pids()
    else:
        from . import top, tmux as tm
        text = server.display(pane, "#{window_id}.#{pane_id}" + tm.SEP + "#{pane_pid}") or ""
        address, _, pid = text.partition(tm.SEP)
        pids = set(top.descendants(top.process_table(), int(pid))) | {int(pid)} if pid.isdigit() else set()
    try:
        return transcripts.pane_transcript(directory, address, pids, exclude)
    except OSError:
        return None


_SUPERVISED_PIDS = {}


def supervised_pids(ttl=5.0):
    """Process ids in the private server's panes (cached briefly), so a hand-made pane whose window and pane ids
    happen to equal a supervised pane's never borrows that agent's transcript."""
    from . import supervisor, top, tmux as tm
    cached = _SUPERVISED_PIDS.get("pids")
    if cached and time.monotonic() - cached[0] < ttl:
        return cached[1]
    found = set()
    try:
        panes = supervisor.server().list_panes(("pane_pid",))
    except (tm.TmuxError, supervisor.SupervisorError, OSError):
        panes = []
    if panes:
        table = top.process_table()
        for pane in panes:
            if pane["pane_pid"].isdigit():
                found |= set(top.descendants(table, int(pane["pane_pid"])))
    _SUPERVISED_PIDS["pids"] = (time.monotonic(), found)
    return found


def context_use(conf, profile, transcript, hint):
    """{"tokens", "limit", "model", "source"} of the agent's context: the transcript's last answer, else the pane's
    context hint, else None. The limit is the transcript model's window from [models] context, else the profile's
    context_limit."""
    from . import profiles
    try:
        limit = int(profiles.resolve(conf, profile).get("context_limit") or profiles.CONTEXT_LIMIT)
    except (profiles.ProfileError, ValueError, TypeError):
        limit = profiles.CONTEXT_LIMIT
    model_limit = profiles.model_context_limit(conf, (transcript or {}).get("model"))
    if model_limit:
        limit = model_limit
    if transcript and transcript.get("tokens"):
        return {"tokens": transcript["tokens"], "limit": limit, "model": transcript.get("model"), "source": "transcript"}
    if hint:
        return {"tokens": hint, "limit": limit, "model": None, "source": "pane hint"}
    return None


def pane_alive(pane, server=None):
    server, pane = where(pane, server)
    if server.is_legacy:
        # tmux 3.4 exits 0 with empty output for a pane that does not exist.
        result = tmux("display", "-p", "-t", pane, "#{pane_pid}")
        return result.returncode == 0 and bool(result.stdout.strip())
    return server.display(pane, "#{pane_dead}") == "0"


def pane_screen(pane, server=None):
    """The pane's visible lines without the blank rows below the last output."""
    server, pane = where(pane, server)
    lines = (on(server, "capture-pane", "-p", "-t", pane).stdout or "").splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def pane_busy(conf, pane, profile=None, screen=None, server=None):
    from . import profiles
    try:
        pattern = profiles.instance_env(conf, profile)["THESEUS_BUSY_PATTERN"]
    except profiles.ProfileError:
        pattern = conf.section("instances").get("busy_pattern") or ""
    if not pattern:
        return False
    screen = pane_screen(pane, server) if screen is None else screen
    return any(pattern in line for line in screen[-3:])


def pane_stall(conf, pane, profile=None, screen=None, server=None):
    """The first of the profile's stall patterns anywhere on the visible screen, or None."""
    from . import profiles
    try:
        patterns = profiles.stall_patterns(conf, profile)
    except profiles.ProfileError:
        return None
    if not patterns:
        return None
    text = "\n".join(pane_screen(pane, server) if screen is None else screen)
    return next((pattern for pattern in patterns if pattern in text), None)


def send_settings(conf):
    """(timeout, poll) in seconds for verified sends: instances.send_verify_seconds, THESEUS_SEND_VERIFY and
    THESEUS_SEND_POLL override them (tests)."""
    timeout = os.environ.get("THESEUS_SEND_VERIFY") or conf.section("instances").get("send_verify_seconds")
    poll = os.environ.get("THESEUS_SEND_POLL") or 0.5
    return (10.0 if timeout in (None, "") else float(timeout)), float(poll)


def input_holds(screen, text, pattern=None, lines=8, placeholder=None):
    """Whether the pane's input line still holds the start of text (typed but never submitted). With the profile's
    input_pattern the last matching line in the bottom lines is the input line; without one, any bottom line counts.
    A long text arrives as a paste that the agent may show as a placeholder (Claude Code: [Pasted text #N]); with the
    profile's paste_placeholder such an input line counts as holding the text."""
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
        if placeholder and re.match(placeholder, content):
            return True
        return bool(content) and (wanted.startswith(content[:40]) or content.startswith(wanted))
    if placeholder and any(re.search(placeholder, line) for line in bottom):
        return True
    return any(wanted[:20] in " ".join(line.split()) for line in bottom)


def input_empty(screen, pattern, lines=8):
    """Whether the input line (the profile's input_pattern) shows nothing; None when no input line is found."""
    if not pattern:
        return None
    regex = re.compile(pattern)
    held = [m.group(1) for m in (regex.match(line) for line in screen[-lines:]) if m]
    return None if not held else not held[-1].strip()


def clear_input(server, pane, key, input_pattern, attempts=12):
    """Press the clear key until the input line is empty: one press of Claude Code's C-u clears only the last visual
    line of a wrapped input."""
    for _ in range(attempts):
        on(server, "send-keys", "-t", pane, key)
        if input_empty(pane_screen(pane, server), input_pattern) is not False:
            return


def send_verified(conf, agent, text, task=None, expect_busy=True, profile=None, record=True, sleep=time.sleep):
    """Type text into an agent's pane and confirm it was submitted: within instances.send_verify_seconds the pane must
    show the profile's busy marker (expect_busy) or, for slash commands, no longer hold the text in its input line.
    If the text is still in the input line, Enter is pressed once more and the pane checked again. Returns (ok, detail);
    a failure records a send_failed event unless record is False. Profiles without a busy marker are sent unverified."""
    from . import profiles
    from . import workqueue
    server, pane = where(agent)
    profile = profile or workqueue.launched(conf, agent)
    settings = profiles.interactive(conf, profile)
    placeholder = settings.get("paste_placeholder")
    try:
        busy_pattern = profiles.instance_env(conf, profile)["THESEUS_BUSY_PATTERN"]
    except profiles.ProfileError:
        busy_pattern = conf.section("instances").get("busy_pattern") or ""
    timeout, poll = send_settings(conf)
    input_pattern = settings.get("input_pattern")

    def submitted():
        screen = pane_screen(pane, server)
        if expect_busy:
            return any(busy_pattern in line for line in screen[-3:]), screen
        return not input_holds(screen, text, input_pattern, placeholder=placeholder), screen

    def wait():
        deadline = time.monotonic() + timeout
        while True:
            ok, screen = submitted()
            if ok or time.monotonic() >= deadline:
                return ok, screen
            sleep(poll)

    if settings.get("clear_input"):
        if server.is_legacy:
            tmux("send-keys", "-t", pane, settings["clear_input"])
        else:
            clear_input(server, pane, settings["clear_input"], input_pattern)
    typed = tmux("send-keys", "-t", pane, "-l", text) if server.is_legacy else server.send_literal(pane, text)
    if typed.returncode != 0:
        detail = f"tmux send-keys failed: {(typed.stderr or '').strip()[:120]}"
    else:
        sleep(0.3)
        on(server, "send-keys", "-t", pane, "Enter")
        if expect_busy and not busy_pattern:
            return True, "sent (unverified: the profile has no busy marker)"
        ok, screen = wait()
        if ok:
            return True, "sent"
        if input_holds(screen, text, input_pattern, placeholder=placeholder):
            on(server, "send-keys", "-t", pane, "Enter")
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
    """Live state: offline, busy, working, delivered, thrashing, gateway_error, stalled, quiet, free (plus disabled);
    supervised agents (theseus.supervisor) also starting, blocked (a startup dialog needs a human), failed (restart
    budget exhausted) and waiting (the agent CLI's own status says it waits for input, such as a permission prompt).
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
    info = {"name": agent["name"], "display": display_name(conf, agent), "pane": pane, "provider": agent.get("provider", ""),
            "enabled": agent.get("enabled", True),
            "task": task["id"] if task else None, "room": (task or {}).get("room"), "idleMinutes": None,
            "tainted": rooms.ledger(conf)["tainted"].get(agent["name"]) if rooms.enabled(conf) else None}
    server, _ = where(agent)
    if agent.get("managed"):
        info.update({"managed": True, "phase": agent.get("phase"), "session": agent.get("session_id")})
        if agent.get("phase") == "failed":
            info["state"] = "failed"
            return info
    if not pane_alive(pane, server):
        info["state"] = "offline"
        return info
    if agent.get("managed") and agent.get("phase") in ("starting", "blocked"):
        info["state"] = agent["phase"]
        return info
    screen = pane_screen(pane, server)
    busy = pane_busy(conf, pane, agent.get("provider"), screen)
    waiting = None
    if agent.get("managed"):
        status = managed_status(conf, agent, server)
        if status:
            busy = status.get("status") == "busy" or busy
            if status.get("status") == "waiting":
                waiting = status.get("waitingFor") or "input"
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
        elif waiting and not busy:
            info["state"] = "waiting"
            info["waiting"] = waiting
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
    elif waiting and not busy:
        info["state"] = "waiting"
        info["waiting"] = waiting
    else:
        info["state"] = "busy" if busy else ("free" if info["enabled"] else "disabled")
    return info


def managed_status(conf, agent, server):
    """The agent CLI's own status entry (Claude Code: `claude agents --json`) for a managed agent, or None."""
    from . import supervisor, workqueue
    pid = server.display(agent["pane"], "#{pane_pid}")
    if not pid or not pid.isdigit():
        return None
    return supervisor.status_of(conf, workqueue.launched(conf, agent), pid)


def states(conf):
    found = [state(conf, agent) for agent in agents(conf)]
    shorten(found)
    return found


def shorten(infos):
    """Drop leading name segments every hand-made agent shares (`team-gw-1`, `team-gw-2` show as `gw-1`, `gw-2`),
    keeping at least two segments, so short names stay distinct."""
    plain = [info for info in infos if not info.get("managed") and info.get("display") == info["name"]]
    if len(plain) < 2:
        return
    parts = [info["name"].split("-") for info in plain]
    shared = 0
    while all(len(p) > shared + 2 for p in parts) and len({p[shared] for p in parts}) == 1:
        shared += 1
    if shared:
        for info, segments in zip(plain, parts):
            info["display"] = "-".join(segments[shared:])


def instance_entries(conf):
    """Name and pane of every registered agent, for the instance scripts."""
    return [{"name": a["name"], "pane": a["pane"]} for a in agents(conf)]


def ensure_known(conf, name):
    if not any(a["name"] == name for a in agents(conf)):
        raise cfg.ConfigError(f"{name} is not a registered agent")
