"""Registry of the persistent agents that work a project, and their live state.

The registry lives in the host store's agents table (theseus.store), keyed by project name: panes and sessions are
machine state, not repository content. It starts from [[instances.panes]] in theseus.toml and is then edited while the
project runs: `theseus agents add|remove|enable|disable`. A disabled agent keeps its record but receives no new work
from the queue. A project's <data>/agents.json, the registry before the store, is imported once and left in place.
"""
import contextvars
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config as cfg
from . import store


class AgentError(Exception):
    pass


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
    record = {"name": row["name"], "id": row.get("id"), "pane": row["pane"], "provider": row["profile"], "enabled": row["enabled"],
              "registered": row["registered"]}
    if row["rooms"]:
        record["rooms"] = row["rooms"]
    if row["source"]:
        record["source"] = row["source"]
    if row.get("pane_pid"):
        record["pane_pid"] = row["pane_pid"]
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


RESERVED = ("coordinator", "queue")


def add(conf, name, pane, provider=None, rooms=None):
    if name in RESERVED:
        raise AgentError(f"{name} is reserved for the project's {name} session; choose another name")
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
            store.update_agent(conn, conf.name, name, pane_pid=pane_pid(pane))
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
    """The transcript state (theseus.adapters) of the session in the agent's pane, read by the adapter its profile
    names in interactive.transcript, or None when the profile has none or the session is not found."""
    from . import adapters
    _, adapter, interactive = adapters.resolve(conf, agent.get("provider"))
    if adapter is None:
        return None
    try:
        return adapter.session_state(conf, agent, interactive)
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


def pane_pid(pane, server=None):
    """The pid of the process a pane runs (its shell or agent), or None when the pane does not exist."""
    server, pane = where(pane, server)
    value = (server.display(pane, "#{pane_pid}") if not server.is_legacy else
             tmux("display", "-p", "-t", pane, "#{pane_pid}").stdout).strip()
    return int(value) if value.isdigit() else None


def stale_pane(agent):
    """True when a hand-made agent's pane id now names another process than the one registered (tmux reuses ids)."""
    recorded = agent.get("pane_pid")
    return bool(recorded) and not agent.get("managed") and pane_pid(agent["pane"]) != recorded


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


PROMPT_LINES = 15


def pane_prompt(conf, pane, profile=None, screen=None, server=None):
    """The screen line of a question that needs a human (a permission prompt or a startup dialog of the profile:
    interactive.prompt_patterns, interactive.startup_dialogs) among the last lines of the pane, or None. Text typed
    into such a pane would answer it, so nothing is typed while one shows."""
    from . import profiles
    settings = profiles.interactive(conf, profile)
    patterns = [re.compile(p) for p in settings.get("prompt_patterns") or []]
    patterns += [re.compile(re.escape(text)) for text in settings.get("startup_dialogs") or []]
    if not patterns:
        return None
    lines = (pane_screen(pane, server) if screen is None else screen)[-PROMPT_LINES:]
    return next((line.strip() for line in lines if any(p.search(line) for p in patterns)), None)


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


_BATCH = contextvars.ContextVar("theseus_send_batch", default=None)
ENTER_PAUSE = 0.3
DEFERRED = "typed; verifying"


class Clock:
    """Wall time, monotonic time and sleep for verified sends; tests swap in a fake clock (agents.clock)."""
    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)
    time = staticmethod(time.time)


clock = Clock()


def enter_pause():
    return float(os.environ.get("THESEUS_ENTER_PAUSE") or ENTER_PAUSE)


class Batch:
    """The verified sends of one queue tick: each is typed at once and recorded here, and the tick verifies them all
    together (workqueue.verify_sends) instead of waiting on each in turn."""

    def __init__(self, budget):
        self.items = []
        self.budget = max(0.0, float(budget))
        self.deadline = None

    def start(self):
        """Start the budget: when the tick turns to verifying, not while it still reconciles, resets or relaunches."""
        if self.deadline is None:
            self.deadline = clock.monotonic() + self.budget

    def remaining(self):
        return self.budget if self.deadline is None else max(0.0, self.deadline - clock.monotonic())

    def agents(self):
        return {item["agent"] for item in self.items}


@contextmanager
def batched(budget):
    """Collect the verified sends made inside the block into a Batch instead of waiting for each."""
    batch = Batch(budget)
    token = _BATCH.set(batch)
    try:
        yield batch
    finally:
        _BATCH.reset(token)


def batch():
    return _BATCH.get()


def send_context(conf, agent, profile=None):
    """What a verified send to this agent needs: its pane and server and the profile's busy marker, input line, paste
    placeholder and clear key (all from the profile, so any agent CLI with these settings is verified)."""
    from . import profiles
    from . import workqueue
    server, pane = where(agent)
    profile = profile or workqueue.launched(conf, agent)
    settings = profiles.interactive(conf, profile)
    try:
        busy_pattern = profiles.instance_env(conf, profile)["THESEUS_BUSY_PATTERN"]
    except profiles.ProfileError:
        busy_pattern = conf.section("instances").get("busy_pattern") or ""
    timeout, poll = send_settings(conf)
    return {"server": server, "pane": pane, "profile": profile, "busy": busy_pattern, "timeout": timeout, "poll": poll,
            "input": settings.get("input_pattern"), "placeholder": settings.get("paste_placeholder"),
            "clear": settings.get("clear_input")}


def type_text(ctx, text, sleep=None, enter=True):
    """Empty the input line, type text and (enter) press Enter after a short pause. Returns '' or the tmux error."""
    server, pane = ctx["server"], ctx["pane"]
    # A pane left in copy mode (a mouse wheel scroll) takes typed keys as copy-mode commands.
    if (on(server, "display-message", "-p", "-t", pane, "#{pane_in_mode}").stdout or "").strip() == "1":
        on(server, "send-keys", "-t", pane, "-X", "cancel")
    if ctx["clear"]:
        if server.is_legacy:
            tmux("send-keys", "-t", pane, ctx["clear"])
        else:
            clear_input(server, pane, ctx["clear"], ctx["input"])
    typed = tmux("send-keys", "-t", pane, "-l", text) if server.is_legacy else server.send_literal(pane, text)
    if typed.returncode != 0:
        return f"tmux send-keys failed: {(typed.stderr or '').strip()[:120]}"
    if enter:
        (sleep or clock.sleep)(enter_pause())
        on(server, "send-keys", "-t", pane, "Enter")
    return ""


def advance(ctx, pending, now=None):
    """One look at a typed text: ("ok", detail) once the pane shows the busy marker (expect_busy) or, for slash
    commands, no longer holds the text; ("wait", None) before the deadline, or after pressing Enter a second time when
    the input line still holds the text; ("failed", detail) after that. A send carried over from an earlier tick
    (pending["carried"]) whose text has left the input line counts as submitted: the agent may have answered and gone
    idle between ticks, so a missed busy marker proves nothing."""
    now = clock.time() if now is None else now
    screen = pane_screen(ctx["pane"], ctx["server"])
    text, timeout = pending["text"], ctx["timeout"]
    holds = input_holds(screen, text, ctx["input"], placeholder=ctx["placeholder"])
    if pending.get("expect_busy", True):
        if any(ctx["busy"] in line for line in screen[-3:]):
            return "ok", "sent" if pending["enters"] < 2 else "sent after a second Enter"
    elif not holds:
        return "ok", "sent" if pending["enters"] < 2 else "sent after a second Enter"
    if pending.get("carried") and not holds:
        return "ok", "sent (seen after the tick)"
    if now < pending["deadline"]:
        return "wait", None
    if pending["enters"] < 2 and holds:
        on(ctx["server"], "send-keys", "-t", ctx["pane"], "Enter")
        pending.update(enters=2, deadline=now + timeout)
        return "wait", None
    if pending["enters"] >= 2:
        return "failed", f"the input line still holds the text after a second Enter ({timeout:g}s each)"
    return "failed", f"not busy after Enter and the text is not in the input line ({timeout:g}s each)"


def record_send_failed(conf, name, task, text, detail):
    from . import workqueue
    short = " ".join(text.split())
    return workqueue.event(conf, "send_failed", name, task, f"{detail}: {short[:60]!r}")


def send_verified(conf, agent, text, task=None, expect_busy=True, profile=None, record=True, sleep=None,
                  kind="plain", into_prompt=False, **extra):
    """Type text into an agent's pane and confirm it was submitted: within instances.send_verify_seconds the pane must
    show the profile's busy marker (expect_busy) or, for slash commands, no longer hold the text in its input line.
    If the text is still in the input line, Enter is pressed once more and the pane checked again. Returns (ok, detail);
    a failure records a send_failed event unless record is False. Profiles without a busy marker are sent unverified.
    Inside a queue tick (batched) the text is typed at once and Enter and the verification are left to the tick, which
    does them for all its sends together: the result is (True, "typed; verifying") and kind with extra says what to do
    once it resolves (workqueue.settle_send). Nothing is typed into a pane that shows a prompt for a human
    (pane_prompt) unless into_prompt says a human is answering it."""
    ctx = send_context(conf, agent, profile)
    prompt = None if into_prompt else pane_prompt(conf, ctx["pane"], ctx["profile"], server=ctx["server"])
    if prompt:
        detail = f"not typed: the pane shows a prompt that needs a human ({prompt[:80]})"
        if record:
            record_send_failed(conf, agent["name"], task, text, detail)
        return False, detail
    current = batch()
    verified = not expect_busy or bool(ctx["busy"])
    error = type_text(ctx, text, sleep, enter=current is None or not verified)
    if not error and not verified:
        return True, "sent (unverified: the profile has no busy marker)"
    sleep = sleep or clock.sleep
    now = clock.time()
    pending = {"agent": agent["name"], "text": text, "task": task, "expect_busy": expect_busy, "profile": ctx["profile"],
               "kind": kind, "enters": 1, "typed": now, "deadline": now + ctx["timeout"], "record": record, **extra}
    if not error and current is not None:
        current.items.append({**pending, "ctx": ctx, "entry": agent, "enter_at": clock.monotonic() + enter_pause()})
        return True, DEFERRED
    if error:
        result, detail = "failed", error
    else:
        while True:
            result, detail = advance(ctx, pending)
            if result != "wait":
                break
            sleep(ctx["poll"])
    if result == "ok":
        return True, detail
    if record:
        record_send_failed(conf, agent["name"], task, text, detail)
    return False, detail


def current_task(conf, name):
    """The open task assigned to this agent (owner provider:<name>:...), if any. A task that records its owner's ID
    belongs to that agent only, not to a later one registered under the same name."""
    from . import tasks as t
    current = None
    for task in t.Queue(conf).load()["tasks"]:
        owner = task.get("owner") or ""
        if task["status"] == "in_progress" and owner.split(":")[1:2] == [name]:
            if task.get("ownerId"):
                if current is None:
                    with store.for_project(conf) as conn:
                        current = store.agent_id(conn, conf.name, name) or ""
                if task["ownerId"] != current:
                    continue
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
    from . import adapters
    from . import gateway
    from . import rooms
    from . import tasks as t
    pane = agent["pane"]
    task = current_task(conf, agent["name"])
    info = {"name": agent["name"], "id": agent.get("id"), "display": display_name(conf, agent), "pane": pane, "provider": agent.get("provider", ""),
            "enabled": agent.get("enabled", True),
            "task": task["id"] if task else None, "room": (task or {}).get("room"), "idleMinutes": None,
            "tainted": rooms.ledger(conf)["tainted"].get(agent["name"]) if rooms.enabled(conf) else None}
    info["transcript"] = adapters.resolve(conf, agent.get("provider"))[0]
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
    if not waiting and (prompt := pane_prompt(conf, pane, agent.get("provider"), screen)):
        waiting, busy = f"a prompt on screen: {prompt[:80]}", False
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
