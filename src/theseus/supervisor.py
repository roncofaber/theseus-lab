"""Supervised persistent agents: theseus starts each one in a hidden pane of its own private tmux server, records every
run in the host store, restarts crashed agents with backoff and stops them gracefully.

There is no daemon. `theseus agents up` reconciles a project toward its [agents] configuration ([agents.counts] live
agents per profile, plus [[agents.extra]]); every queue tick of a managed project runs the same reconcile without waiting
for slow starts, so the first theseus command after a reboot brings the agents back. The pane runs
`theseus agents _exec PROJECT AGENT GEN`, which applies the profile environment in its own process and execs the
agent, so the pane's start command never carries a secret and the pane pid is the agent's. A pane-died hook runs
`theseus agents _died`, which records the exit and kicks a one-agent reconcile.

Restart policy: an agent that holds an in-progress task resumes its conversation after a crash, a reboot or a lost
server; an agent without a task starts fresh. Crashes within [supervisor.restart] window_minutes wait backoff_seconds
before the next start, and after max_crashes the agent is failed until `theseus agents restart NAME`. Startup dialogs
(the folder trust question, a new MCP server) are surfaced as `blocked`, never answered.
"""
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import time
import uuid
from pathlib import Path

from . import config as cfg
from . import hostconfig
from . import profiles
from . import store
from . import tmux as tm

ENV_ALLOW = ("HOME", "USER", "LOGNAME", "SHELL", "PATH", "LANG", "TMPDIR", "TMUX_TMPDIR")
ENV_ALLOW_PREFIX = ("LC_", "XDG_", "THESEUS_")
RESUME_MODES = ("task", "always", "never")
RESET_INTENTS = ("start", "restart", "migrate")
STATUS_TTL = 2.0
_status_cache = {}


class SupervisorError(Exception):
    pass


def settings():
    return hostconfig.supervisor()


def size():
    text = str(settings().get("size") or "220x50")
    match = re.fullmatch(r"(\d+)x(\d+)", text)
    if not match:
        raise SupervisorError(f"[supervisor] size {text!r} is not WIDTHxHEIGHT")
    return int(match.group(1)), int(match.group(2))


def section(conf):
    return conf.section("agents")


def managed(conf):
    return bool(section(conf).get("managed"))


def home():
    """Host files of the supervisor (tmux.conf, the pane-died hook) beside the host store."""
    path = store.path().parent / "supervisor"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def agent_dir(conf, name):
    path = conf.data / "agents" / name
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def theseus_argv():
    """The command that runs this same theseus: this interpreter with this package's import root pinned, without
    exporting PYTHONPATH to the agents."""
    root = str(Path(__file__).resolve().parents[1])
    boot = f"import sys; sys.path.insert(0, {root!r}); from theseus.cli import main; main(sys.argv[1:])"
    return [sys.executable, "-c", boot]


def server_env():
    """The allowlisted environment the private server starts with, so variables of whatever process started it
    never reach every agent."""
    extra = [str(item) for item in settings().get("env_passthrough") or []]
    env = {}
    for key, value in os.environ.items():
        if key in ENV_ALLOW or key.startswith(ENV_ALLOW_PREFIX) or any(fnmatch.fnmatchcase(key, p) for p in extra):
            env[key] = value
    env.pop("THESEUS_TMUX_GUARD", None)
    return env


def session_env():
    """THESEUS_* of the caller, set on each agent session, so _exec uses the caller's store and host config."""
    return {key: value for key, value in os.environ.items() if key.startswith("THESEUS_") and key != "THESEUS_TMUX_GUARD"}


def hook_script():
    path = home() / "pane-died"
    text = "#!/bin/sh\nexec " + shlex.join(theseus_argv()) + ' agents _died "$@"\n'
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
    path.chmod(0o700)
    return path


def tmux_conf():
    """The private server's configuration, loaded with -f so the user's ~/.tmux.conf never applies. Global
    window-size manual crashes tmux 3.4 at the first new-session, so the fixed agent size comes from default-size
    and a per-window resize-window instead."""
    s = settings()
    width, height = size()
    hook = str(hook_script())
    if "'" in hook or '"' in hook:
        raise SupervisorError(f"the host data path {hook} contains a quote; tmux hooks cannot run it")
    detach = str(s.get("detach_key") or "C-q")
    lines = [
        "set -g exit-empty on",
        "set -g remain-on-exit on",
        f"set -g history-limit {int(s.get('history_limit') or 50000)}",
        f"set -g default-size {width}x{height}",
        'set -g default-terminal "tmux-256color"',
        "set -g escape-time 10",
        "set -g focus-events on",
        "set -g status on",
        "set -g status-left-length 60",
        'set -g status-left "[theseus #{@theseus_project}] "',
        f'set -g status-right "#{{@theseus_agent}}  {detach} detach "',
        "set -g prefix None",
        "set -g prefix2 None",
        f"bind -n {detach} detach-client",
        f"set-hook -g pane-died \"run-shell -b '{hook} #{{socket_path}} #{{session_name}} #{{pane_pid}} s#{{pane_dead_status}} g#{{pane_dead_signal}}'\"",
        'set-hook -g client-attached "resize-window -A"',
        f'set-hook -g client-detached "resize-window -x {width} -y {height}"',
    ]
    path = home() / "tmux.conf"
    text = "\n".join(lines) + "\n"
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
    return path


def server(name=None):
    """The private server (or the one a store row names), started on demand with tmux_conf() and server_env()."""
    if name is not None and name != tm.Server().name:
        found = tm.Server.from_name(name)
        found.conf_file, found.env = tmux_conf(), server_env()
        return found
    return tm.Server(conf_file=tmux_conf(), env=server_env())


def server_of(agent):
    """The server an agent's pane lives on: the private one for managed agents, the user's for the rest."""
    if agent.get("managed"):
        return server(agent.get("server"))
    return tm.Server.legacy()


# configuration

def template(conf):
    return str(section(conf).get("name") or "{project}-{profile}-{n}")


def counts(conf):
    """[agents.counts] of the project's own files, or the host's [defaults] agents.counts when they set none: the
    host value is a fallback, never merged into a project's counts."""
    own = {}
    found = False
    for label, _, data in conf.layers:
        if label in ("project", "project-local", "env"):
            present, value = cfg.lookup(data, "agents.counts")
            if present and isinstance(value, dict):
                own.update(value)
                found = True
    if not found:
        own = section(conf).get("counts") or {}
    return {str(k): int(v) for k, v in own.items()}


def desired(conf):
    """The configured names alone, [{name, profile, slot, rooms}]: slots 1..N of [agents.counts] then [[agents.extra]],
    for validation and migration. What reconcile works toward is standing(), which also reads the records."""
    found = []
    pattern = template(conf)
    for profile, count in counts(conf).items():
        for n in range(1, count + 1):
            found.append({"name": pattern.format(project=conf.name, profile=profile, n=n), "profile": profile, "slot": n,
                          "rooms": None})
    for extra in section(conf).get("extra") or []:
        found.append({"name": extra["name"], "profile": extra.get("profile") or profiles.default_name(conf), "slot": None,
                      "rooms": extra.get("rooms")})
    return found


def counted_live(rec):
    return rec.get("desired") not in ("down", "retire") and rec.get("enabled", True)


def name_free(rec):
    """Whether a counted name may go to a new agent: never used or removed. A downed name stays down."""
    return rec is None


def plan(count, members, holds=lambda n: False, started=lambda n: None, taken=lambda n: False, keep=(),
         stopping=lambda n: False):
    """One profile's plan over members {n: record} of its counted names: (kept, starts, victims), lists of n. Live
    members beyond count become victims: those already stopping, then free ones before busy ones and the most recently
    started first (keep last). Below count, retiring members come back first, then the lowest free names."""
    live = [n for n, rec in members.items() if counted_live(rec)]
    if len(live) > count:
        order = sorted(live, key=lambda n: (n in keep, not stopping(n), bool(holds(n)), -(started(n) or 0), -n))
        victims = order[:len(live) - count]
        return sorted(set(live) - set(victims)), [], sorted(victims)
    need = count - len(live)
    starts = sorted(n for n, rec in members.items() if rec.get("desired") == "retire")[:need]
    n = 0
    while len(starts) < need:
        n += 1
        if n in starts or (n not in members and taken(n)):
            continue
        if name_free(members.get(n)):
            starts.append(n)
    return sorted(live), sorted(starts), []


def standing(conf, regs, keep=()):
    """What reconcile keeps or starts, [{name, profile, slot, rooms, revive}]: per profile the live counted agents
    within the count, the names to start or revive and the agents down or disabled (left as they are), then
    [[agents.extra]]. Live agents beyond the count are left out, so reconcile retires them."""
    pattern = template(conf)
    if "{n}" not in pattern:
        raise SupervisorError(f"[agents] name {pattern!r} must contain {{n}}")
    extras = [{"name": e["name"], "profile": e.get("profile") or profiles.default_name(conf), "slot": None,
               "rooms": e.get("rooms")} for e in section(conf).get("extra") or []]
    used = {e["name"] for e in extras}
    wanted = counts(conf)
    names = list(wanted) + sorted({r["profile"] for r in regs.values()
                                   if r.get("managed") and r.get("slot") and r.get("profile") and r["profile"] not in wanted})
    found = []
    for profile in names:
        def name(n, profile=profile):
            return pattern.format(project=conf.name, profile=profile, n=n)
        members = {r["slot"]: r for r in regs.values() if r.get("managed") and r.get("slot") and
                   r.get("profile") == profile and r["name"] == name(r["slot"]) and r["name"] not in used}
        for n in range(1, wanted.get(profile, 0) + len(regs) + 1):
            rec = regs.get(name(n))
            if rec and not rec.get("managed") and name(n) not in used:
                members.setdefault(n, rec)
        kept, starts, _ = plan(wanted.get(profile, 0), members,
                               holds=lambda n: bool(current_task(conf, name(n))),
                               started=lambda n: (last_run(conf, name(n)) or {}).get("started_epoch"),
                               taken=lambda n: name(n) in used or name(n) in regs,
                               keep={n for n in members if name(n) in keep},
                               stopping=lambda n: (last_run(conf, name(n)) or {}).get("stop_reason") == "retire")
        parked = [n for n, rec in members.items() if not counted_live(rec) and rec.get("desired") != "retire"]
        for n in sorted({*kept, *starts, *parked}):
            used.add(name(n))
            found.append({"name": name(n), "profile": profile, "slot": n, "rooms": None,
                          "revive": n in starts and n in members})
    return found + extras


def set_count(conf, profile, count):
    """Write [agents.counts] <profile> = count to the project's theseus.local.toml, keeping the rest of the file."""
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_-]+", profile):
        raise SupervisorError(f"profile name {profile!r} cannot be a TOML key")
    if count < 0:
        raise SupervisorError("a count cannot be negative")
    path = conf.root / cfg.LOCAL_NAME
    text = path.read_text() if path.exists() else ""
    line = f"{profile} = {count}"
    found = _re.search(r"^\[agents\.counts\][ \t]*$", text, _re.MULTILINE)
    if found:
        start = found.end()
        end = text.find("\n[", start)
        end = len(text) if end == -1 else end
        section = text[start:end]
        pattern = _re.compile(rf"^{_re.escape(profile)}\s*=.*$", _re.MULTILINE)
        section = pattern.sub(line, section) if pattern.search(section) else section.rstrip("\n") + "\n" + line + "\n"
        text = text[:start] + section + text[end:]
    else:
        text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + "[agents.counts]\n" + line + "\n"
    import tomllib
    try:
        written = (tomllib.loads(text).get("agents") or {}).get("counts") or {}
    except tomllib.TOMLDecodeError:
        written = {}
    if written.get(profile) != count:
        raise SupervisorError(f"cannot set the count in {path} automatically (its [agents] counts are written in another "
                              f"form); set {profile} = {count} under [agents.counts] by hand")
    path.write_text(text)
    return f"[agents.counts] {profile} = {count} in {cfg.LOCAL_NAME}"


def problems(conf):
    """(errors, warnings) of a project's [agents] table."""
    errors, warnings = [], []
    agents = section(conf)
    known = set(cfg.DEFAULTS["agents"])
    errors += [f"[agents] unknown key {key}; known: {', '.join(sorted(known))}" for key in agents if key not in known]
    pattern = template(conf)
    if "{n}" not in pattern:
        errors.append(f"[agents] name {pattern!r} must contain {{n}}")
    try:
        pattern.format(project="p", profile="q", n=1)
    except (KeyError, IndexError, ValueError) as error:
        errors.append(f"[agents] name {pattern!r}: only {{project}}, {{profile}} and {{n}} are allowed ({error})")
        return errors, warnings
    if str(agents.get("resume") or "task") not in RESUME_MODES:
        errors.append(f"[agents] resume must be one of {', '.join(RESUME_MODES)}")
    names = []
    for item in agents.get("extra") or []:
        if not isinstance(item, dict) or not item.get("name"):
            errors.append("each [[agents.extra]] needs a name")
    if errors:
        return errors, warnings
    for item in desired(conf):
        names.append(item["name"])
        try:
            profile = profiles.resolve(conf, item["profile"])
        except profiles.ProfileError as error:
            errors.append(f"[agents] {item['name']}: {error}")
            continue
        if not profile.get("interactive"):
            errors.append(f"[agents] profile {item['profile']} has no persistent-instance (interactive) settings")
            continue
        command = profile.get("command") or ""
        if command and not shutil.which(command, path=server_env().get("PATH")):
            warnings.append(f"[agents] profile {item['profile']}: command {command!r} is not on PATH; supervised agents "
                            f"exec it directly (no shell aliases or functions; [supervisor] launch_shell = \"login\" "
                            f"runs it through a login shell)")
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        errors.append(f"[agents] duplicate agent names: {', '.join(duplicates)}")
    for profile, count in counts(conf).items():
        try:
            capacity = profiles.resolve(conf, profile).get("capacity")
        except profiles.ProfileError:
            continue
        if capacity is not None and count > int(capacity):
            warnings.append(f"[agents.counts] {profile} = {count} exceeds its capacity {capacity}: the extra sessions stay idle")
    panes = {p.get("name") for p in conf.section("instances").get("panes") or [] if isinstance(p, dict)}
    for name in sorted(panes & set(names)):
        warnings.append(f"{name} is both an [[instances.panes]] entry and a supervised agent; remove the pane entry once "
                        f"`theseus agents migrate {name}` has moved it")
    return errors, warnings


# store helpers

def record(conf, name):
    with store.for_project(conf) as conn:
        return next((a for a in store.list_agents(conn, conf.name) if a["name"] == name), None)


def records(conf):
    from . import agents
    with store.for_project(conf) as conn:
        agents.seed(conf, conn)
        return {a["name"]: a for a in store.list_agents(conn, conf.name)}


def update(conf, name, **fields):
    with store.for_project(conf) as conn:
        store.update_agent(conn, conf.name, name, **fields)


def event(conf, kind, agent=None, task=None, detail=""):
    from . import workqueue
    return workqueue.event(conf, kind, agent, task, detail)


def last_run(conf, name):
    with store.for_project(conf) as conn:
        return store.last_run(conn, conf.name, name)


def agent_entry(rec):
    from . import agents
    return agents.entry(rec)


def current_task(conf, name):
    from . import agents
    return agents.current_task(conf, name)


# launching

def launch(conf, profile_name, name, session=None, resume=False):
    """(argv, env, cwd, hash) of one agent run; the hash covers argv without session arguments, env names and cwd."""
    profile = profiles.resolve(conf, profile_name)
    argv, env = profiles.interactive_argv(conf, profile, name, session, resume)
    base, _ = profiles.interactive_argv(conf, profile, name)
    where = cwd(conf)
    digest = hashlib.sha256(json.dumps([base, sorted(env), str(where)]).encode()).hexdigest()[:16]
    return argv, env, where, digest


def cwd(conf):
    value = str(section(conf).get("cwd") or "root")
    if value == "root":
        return conf.root
    if value == "data":
        return conf.data
    return Path(cfg.expand(value, conf.values))


def pane_info(srv, session):
    fields = ("pane_id", "pane_pid", "pane_dead", "pane_dead_status", "pane_dead_signal", "@theseus_gen")
    text = srv.display(tm.address(session), tm.SEP.join(f"#{{{f}}}" for f in fields))
    if text is None:
        return None
    return dict(zip(fields, text.split(tm.SEP)))


def resume_session(conf, name, intent):
    """The conversation to resume for a restart, or None to start fresh: an explicit restart, a relaunch or a migration
    resumes; a crash or reboot resumes only an agent that holds a task ([agents] resume = "task")."""
    rec = record(conf, name) or {}
    session = rec.get("session_id")
    if not session:
        return None
    mode = str(section(conf).get("resume") or "task")
    if intent in ("relaunch", "migrate", "restart"):
        return session
    if mode == "never":
        return None
    if mode == "always":
        return session
    return session if current_task(conf, name) else None


def spawn(conf, slot, intent="start", session=None, profile=None, srv=None):
    """Start (or respawn) one agent's pane. session resumes that conversation; otherwise a new id is preassigned."""
    name = slot["name"]
    srv = srv or server()
    rec = record(conf, name) or {}
    profile = profile or rec.get("profile") or slot["profile"]
    resume = bool(session)
    session = session or str(uuid.uuid4())
    argv, _, where, digest = launch(conf, profile, name, session, resume)
    width, height = size()
    target = tm.session_name(conf.name, name)
    with store.for_project(conf) as conn:
        if not rec:
            store.upsert_agent(conn, conf.name, name, pane=tm.address(target), profile=slot["profile"],
                               rooms=slot.get("rooms"), source="supervisor")
        run = store.add_run(conn, conf.name, name, intent, profile=profile, launch_hash=digest, session_id=session,
                            resume=resume)
        store.update_agent(conn, conf.name, name, managed=True, server=srv.name, target=target, pane=tm.address(target),
                           session_id=session, slot=slot.get("slot"), desired="up", phase="starting")
    command = [*theseus_argv(), "agents", "_exec", conf.name, name, str(run["gen"])]
    info = pane_info(srv, target)
    try:
        if info is None:
            srv.new_session(target, command, where, width, height, env=session_env())
        else:
            srv.respawn(tm.address(target), command, cwd=where, env=session_env())
    except tm.TmuxError as error:
        with store.for_project(conf) as conn:
            store.close_run(conn, run["id"], "spawn-failed")
            store.update_agent(conn, conf.name, name, phase="failed")
        raise SupervisorError(f"{name}: {error}") from error
    pane = tm.address(target)
    for key, value in (("@theseus_project", conf.name), ("@theseus_agent", name), ("@theseus_gen", str(run["gen"])),
                       ("@theseus_profile", profile)):
        srv.set_option(pane, key, value)
    srv.run("resize-window", "-x", str(width), "-y", str(height), "-t", pane)
    info = pane_info(srv, target) or {}
    if info.get("pane_pid", "").isdigit():
        with store.for_project(conf) as conn:
            store.update_run(conn, run["id"], pid=int(info["pane_pid"]))
    how = f"resuming {session}" if resume else f"session {session}"
    event(conf, "spawned" if intent == "start" else "restarted", name, None, f"gen {run['gen']} {profile} ({intent}, {how})")
    return run


# observing

def status_entries(conf, profile_name):
    """`claude agents --json` (interactive.status_command) as a list, cached for a few seconds; None when the profile
    has no status command or it failed."""
    command = profiles.interactive(conf, profile_name).get("status_command")
    if not command:
        return None
    key = tuple(command)
    cached = _status_cache.get(key)
    if cached and time.monotonic() - cached[0] < STATUS_TTL:
        return cached[1]
    import subprocess
    try:
        result = subprocess.run(list(command), capture_output=True, text=True, timeout=10)
        entries = json.loads(result.stdout) if result.returncode == 0 else None
        if not isinstance(entries, list):
            entries = None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        entries = None
    _status_cache[key] = (time.monotonic(), entries)
    return entries


_process_cache = {}


def process_table(ttl=1.0):
    """top.process_table(), reused for a second: one status pass looks up several agents."""
    from . import top
    cached = _process_cache.get("table")
    if cached and time.monotonic() - cached[0] < ttl:
        return cached[1]
    table = top.process_table()
    _process_cache["table"] = (time.monotonic(), table)
    return table


def status_of(conf, profile_name, pane_pid):
    """The status entry of the agent whose pid is pane_pid or one of its descendants, or None."""
    entries = status_entries(conf, profile_name)
    if not entries or not pane_pid:
        return None
    from . import top
    table = process_table()
    tree = set(top.descendants(table, int(pane_pid))) | {int(pane_pid)}
    return next((e for e in entries if isinstance(e, dict) and e.get("pid") in tree), None)


def observe(conf, rec, info=None, srv=None):
    """(phase, detail) of a managed agent's live pane: dead, blocked, resume-missing, ready or starting."""
    srv = srv or server_of(rec)
    info = info or pane_info(srv, rec["target"])
    if info is None:
        return "gone", "no session"
    profile = profiles.interactive(conf, (last_run(conf, rec["name"]) or {}).get("profile") or rec.get("profile"))
    # tmux 3.4 scrolls a dead pane's screen into its history, so the last words of an exited agent are read from there.
    screen = (srv.capture(rec["pane"], ansi=False, history=100 if info.get("pane_dead") == "1" else 0) or "")
    run = last_run(conf, rec["name"]) or {}
    missing = profile.get("resume_missing")
    if missing and run.get("resume") and missing in screen and (run.get("session_id") or "") in screen:
        return "resume-missing", f"session {run.get('session_id')} no longer exists"
    if info.get("pane_dead") == "1":
        return "dead", f"exit {info.get('pane_dead_status') or '?'}"
    for pattern in profile.get("startup_dialogs") or []:
        if pattern in screen:
            return "blocked", f"startup dialog ({pattern!r}): run `theseus agents attach {rec['name']}` and answer it"
    run_profile = run.get("profile") or rec.get("profile")
    entries = status_entries(conf, run_profile)
    entry = status_of(conf, run_profile, info.get("pane_pid")) if entries else None
    if entry:
        if entry.get("status") == "waiting" and "dialog" in str(entry.get("waitingFor") or ""):
            return "blocked", f"waiting for {entry.get('waitingFor')}: run `theseus agents attach {rec['name']}`"
        return "ready", entry.get("sessionId") or ""
    busy = profile.get("busy_pattern") or ""
    lines = [line for line in screen.splitlines() if line.strip()]
    ready_pattern = profile.get("ready_pattern")
    started = run.get("started_epoch") or 0
    if lines and not (busy and any(busy in line for line in lines[-3:])) \
            and (not ready_pattern or re.search(ready_pattern, screen)) \
            and time.time() - started >= float(os.environ.get("THESEUS_READY_SETTLE") or 3) \
            and entries is None:
        return "ready", ""
    return "starting", ""


def mark_ready(conf, rec, detail):
    run = last_run(conf, rec["name"]) or {}
    fields = {"phase": "ready"}
    if detail and detail != rec.get("session_id"):
        fields["session_id"] = detail
    update(conf, rec["name"], **fields)
    with store.for_project(conf) as conn:
        if run and not run.get("ready_epoch"):
            store.update_run(conn, run["id"], ready_epoch=time.time(), **({"session_id": detail} if detail else {}))
    if rec.get("phase") != "ready":
        event(conf, "ready", rec["name"], None, f"gen {run.get('gen')}")


def wait_ready(conf, name, timeout=None, poll=None):
    """Poll a starting agent until it is ready, blocked, dead or out of time. Returns (phase, detail)."""
    timeout = float(timeout if timeout is not None else settings().get("ready_seconds") or 90)
    poll = float(poll if poll is not None else os.environ.get("THESEUS_READY_POLL") or 0.5)
    deadline = time.monotonic() + timeout
    while True:
        rec = record(conf, name)
        phase, detail = observe(conf, rec)
        if phase == "ready":
            mark_ready(conf, rec, detail)
            return phase, detail
        if phase in ("blocked", "dead", "gone", "resume-missing"):
            return phase, detail
        if time.monotonic() >= deadline:
            return "timeout", f"not ready after {timeout:g}s"
        time.sleep(poll)


def settle(conf, rec, phase, detail, wait=False):
    """Act on what observe() saw for an agent in its starting or blocked phase. Returns an action label or None."""
    name = rec["name"]
    if phase == "ready":
        mark_ready(conf, rec, detail)
        return "ready"
    if phase == "blocked":
        if rec.get("phase") != "blocked":
            update(conf, name, phase="blocked")
            event(conf, "error", name, None, detail)
        return "blocked"
    if phase == "resume-missing":
        run = last_run(conf, name)
        if run and not run.get("ended"):
            with store.for_project(conf) as conn:
                store.close_run(conn, run["id"], "resume-missing")
        task = current_task(conf, name)
        event(conf, "restarted", name, task["id"] if task else None, f"{detail}; started fresh")
        spawn(conf, {"name": name, "profile": rec["profile"], "slot": rec.get("slot")}, intent="fresh")
        if task:
            want_prompt(conf, name, task["id"])
        if wait:
            return settle(conf, record(conf, name), *wait_ready(conf, name))
        return "restarted fresh"
    if phase in ("dead", "gone"):
        return "exited"
    if phase == "timeout":
        if rec.get("phase") != "blocked":
            update(conf, name, phase="blocked")
            event(conf, "error", name, None, f"{detail}: run `theseus agents attach {name}` to look")
        return "blocked"
    return None


def want_prompt(conf, name, task_id):
    """Owe the freshly started agent its task's restart prompt; the queue sends it once the agent is ready."""
    from . import workqueue
    workqueue.owe_prompt(conf, name, task_id, "a restart on a fresh session")


# crash handling and restart policy

def classify(run, status):
    if run.get("stop_reason"):
        return run["stop_reason"]
    return "exit" if str(status) == "0" else "crash"


def record_exit(conf, name, gen, status=None, signal=None, srv=None):
    """Close the run that just ended (once) and capture its last screen for a crash. Returns the stop reason or None
    when the run had already been closed."""
    with store.for_project(conf) as conn:
        run = store.run_of(conn, conf.name, name, int(gen)) if gen else store.last_run(conn, conf.name, name)
    if not run or run.get("ended"):
        return None
    reason = classify(run, status)
    rec = record(conf, name) or {}
    if reason in ("crash", "exit") and rec.get("target"):
        screen = (srv or server_of(rec)).capture(rec["pane"], history=200, ansi=False, join=True)
        if screen:
            save_screen(agent_dir(conf, name) / f"crash-{run['gen']}.txt", screen)
    status_value = int(status) if str(status or "").lstrip("-").isdigit() else None
    with store.for_project(conf) as conn:
        if not store.close_run(conn, run["id"], reason, status_value, signal or None):
            return None
    if reason in ("crash", "exit"):
        task = current_task(conf, name)
        how = f"signal {signal}" if signal else f"status {status if status not in (None, '') else '?'}"
        event(conf, "exited", name, task["id"] if task else None, f"gen {run['gen']} exited ({how})")
    else:
        event(conf, "stopped", name, None, f"gen {run['gen']} ({reason})")
    return reason


def save_screen(path, screen):
    """Write a captured screen to a private file, redacted like the gateway excerpts. Every screen theseus keeps on
    disk goes through here."""
    from . import redaction
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
        handle.write(redaction.redact(screen))
    path.chmod(0o600)


def crash_history(conf, name, now=None):
    """(count, last end epoch, unplanned clean exits) of crashes within [supervisor.restart] window_minutes."""
    now = time.time() if now is None else now
    window = float(settings()["restart"]["window_minutes"]) * 60
    with store.for_project(conf) as conn:
        recent = store.runs(conn, conf.name, name, since=now - window)
        reset = max([r["gen"] for r in recent if r["intent"] in RESET_INTENTS] or [0])
        rows = [r for r in recent if r["gen"] >= reset and r.get("stop_reason") in ("crash", "exit") and r.get("ended_epoch")]
    if not rows:
        return 0, None, 0
    return len(rows), max(r["ended_epoch"] for r in rows), sum(1 for r in rows if r["stop_reason"] == "exit")


def next_start(conf, name, now=None):
    """(epoch, give_up) for the next automatic start of a crashed agent."""
    now = time.time() if now is None else now
    count, last, exits = crash_history(conf, name, now)
    policy = settings()["restart"]
    if count == 0:
        return now, False
    if count >= int(policy["max_crashes"]):
        return None, True
    if count == 1 and exits == 1:
        return now, False
    backoff = [float(x) for x in policy["backoff_seconds"]] or [5.0]
    return last + backoff[min(count - 1, len(backoff) - 1)], False


def wake_times(conf, now=None):
    """Epochs when a crashed agent of this project becomes due for its restart (for `theseus queue wait`)."""
    if not managed(conf) or paused(conf):
        return []
    now = time.time() if now is None else now
    found = []
    for name, rec in records(conf).items():
        if not rec.get("managed") or rec.get("desired") == "down" or rec.get("phase") == "failed":
            continue
        run = last_run(conf, name)
        if not run or run.get("stop_reason") not in ("crash", "exit"):
            continue
        due, give_up = next_start(conf, name, now)
        if due and not give_up and due > now:
            found.append(due)
    return found


def died(socket, session, pid, status=None, signal=None):
    """The pane-died hook: record the exit of the run whose process pid ended in that session's pane, then reconcile
    the agent when its restart is due soon. The agent is found by its session name and the run by its pid, since an
    agent that exits at once dies before spawn() has set its pane options or stored its pid."""
    srv = tm.Server("-S", socket) if socket else server()
    status = (status or "")[1:] if (status or "").startswith("s") else status
    signal = (signal or "")[1:] if (signal or "").startswith("g") else signal
    with store.for_host() as conn:
        row = conn.execute("SELECT project, name FROM agents WHERE target = ? AND managed", (session,)).fetchone()
    if row is None or not str(pid).isdigit():
        return 0
    conf = project_conf(row["project"])
    if conf is None:
        return 1
    name = row["name"]
    run = None
    deadline = time.monotonic() + 5
    while run is None and time.monotonic() < deadline:
        with store.for_project(conf) as conn:
            run = next((r for r in store.runs(conn, conf.name, name, limit=5) if r.get("pid") == int(pid)), None)
        if run is None:
            time.sleep(0.2)
    if run is None:
        return 0
    reason = record_exit(conf, name, run["gen"], status, signal, srv)
    if reason not in ("crash", "exit"):
        return 0
    due, give_up = next_start(conf, name)
    if give_up:
        reconcile(conf, [name])
        return 0
    delay = due - time.time()
    if delay > 15:
        return 0
    if delay > 0:
        time.sleep(delay)
    from . import workqueue
    with workqueue.locked(conf):
        reconcile(conf, [name])
    return 0


def project_conf(name):
    with store.for_host() as conn:
        row = store.get_project(conn, name)
    if not row or not row.get("root"):
        return None
    try:
        return cfg.load(root=row["root"])
    except cfg.ConfigError:
        return None


# reconcile

def reconcile(conf, names=None, wait=False, dry_run=False, reset=False):
    """One pass toward the project's [agents] configuration. Returns [(name, action, detail)]."""
    if not managed(conf):
        return []
    if paused(conf):
        return [("-", "paused", "supervision paused by `theseus agents down --all`; `theseus agents up` resumes it")]
    srv = server()
    regs = records(conf)
    wanted = standing(conf, regs, keep=names or ())
    if names:
        unknown = set(names) - {w["name"] for w in wanted} - {n for n, r in regs.items() if r.get("managed")}
        if unknown:
            raise SupervisorError(f"not a configured agent of {conf.name}: {', '.join(sorted(unknown))} "
                                  f"(see [agents] in {cfg.CONFIG_NAME} or {cfg.LOCAL_NAME})")
    lost = close_lost(conf, srv, regs, dry_run)
    actions = []
    running = len([p for p in srv.list_panes(("@theseus_agent",)) if p.get("@theseus_agent")])
    limit = int(settings().get("max_agents") or 0)
    for slot in wanted:
        name = slot["name"]
        if names and name not in names:
            continue
        rec = regs.get(name)
        if slot.get("revive") and rec and rec.get("desired") == "retire":
            fields = {"desired": "up", "enabled": True}
            if not dry_run:
                update(conf, name, **fields)
            rec = {**rec, **fields}
        try:
            action = reconcile_one(conf, srv, slot, rec, wait, dry_run, reset, names, lost, running, limit)
        except (SupervisorError, tm.TmuxError, profiles.ProfileError) as error:
            event(conf, "error", name, None, f"supervisor: {error}") if not dry_run else None
            action = ("error", str(error))
        if action[0] in ("started", "restarted", "would start", "would restart"):
            running += 1
        actions.append((name, *action))
    known = {w["name"] for w in wanted}
    if names:
        actions += [(name, "surplus", "beyond [agents.counts]; the next full reconcile stops it")
                    for name in names if name not in known]
    else:
        extra = [rec for name, rec in regs.items() if rec.get("managed") and name not in known]
        current = configured_now(conf) if extra else set()
        for rec in extra:
            if current is None or rec["name"] in current:
                actions.append((rec["name"], "kept", "configured in the current files"))
            else:
                actions.append((rec["name"], *retire(conf, srv, rec, dry_run)))
    return actions


def configured_now(conf):
    """The agent names the project's files configure right now, or None when they cannot be read: a caller holding an
    older configuration (a long-running queue) must never retire an agent that another command has since added."""
    try:
        return {w["name"] for w in standing(cfg.load(root=conf.root), records(conf))}
    except (cfg.ConfigError, SupervisorError):
        return None


def close_lost(conf, srv, regs, dry_run):
    """Close open runs whose session is gone (server lost, session killed); returns the names affected."""
    lost = set()
    with store.for_project(conf) as conn:
        open_runs = store.runs(conn, conf.name, open_only=True)
    alive = srv.alive()
    for run in open_runs:
        rec = regs.get(run["agent"])
        if not rec or not rec.get("target"):
            continue
        if alive and srv.has_session(rec["target"]):
            continue
        lost.add(run["agent"])
        if not dry_run:
            with store.for_project(conf) as conn:
                store.close_run(conn, run["id"], "server-lost" if not alive else "session-lost")
    return lost


def reconcile_one(conf, srv, slot, rec, wait, dry_run, reset, names, lost, running, limit):
    name = slot["name"]
    if rec and not rec.get("managed"):
        from . import agents
        if rec.get("pane") and agents.pane_alive(rec["pane"]):
            return "legacy", f"a hand-made pane {rec['pane']} runs it; `theseus agents migrate {name}`"
    if rec and rec.get("desired") == "down" and not names:
        return "down", "stopped by `theseus agents down`, not counted; `theseus agents up NAME` starts it"
    if rec and rec.get("phase") == "failed" and not reset:
        return "failed", f"restart budget exhausted; `theseus agents restart {name}`"
    info = pane_info(srv, rec["target"]) if rec and rec.get("managed") and rec.get("target") else None
    if info is None:
        if limit and running >= limit:
            return "deferred", f"[supervisor] max_agents = {limit} sessions already run on this host"
        session = resume_session(conf, name, "crash") if rec and rec.get("managed") else None
        if dry_run:
            return ("would restart" if rec and rec.get("managed") else "would start"), (f"resume {session}" if session else "fresh")
        if not (rec and rec.get("managed")):
            intent = "start"
        else:
            intent = "restart" if reset else ("resume" if session else "fresh")
        from . import workqueue
        spawn(conf, slot, intent=intent, session=session, srv=srv,
              profile=workqueue.launched(conf, agent_entry(rec)) if rec else None)
        if session is None and rec and rec.get("managed"):
            task = current_task(conf, name)
            if task:
                want_prompt(conf, name, task["id"])
        label = "started" if intent == "start" else "restarted"
        detail = f"resuming {session}" if session else "fresh session"
        if wait:
            phase = settle(conf, record(conf, name), *wait_ready(conf, name))
            detail += f"; {phase}"
        return label, detail
    if info.get("pane_dead") == "1":
        if not dry_run:
            record_exit(conf, name, info.get("@theseus_gen"), info.get("pane_dead_status"), info.get("pane_dead_signal"), srv)
        run = last_run(conf, name) or {}
        if run.get("stop_reason") in ("down", "retire") and not names:
            return "down", "stopped"
        due, give_up = next_start(conf, name)
        if give_up and not reset:
            if rec.get("phase") != "failed" and not dry_run:
                update(conf, name, phase="failed")
                count = int(settings()["restart"]["max_crashes"])
                event(conf, "error", name, (current_task(conf, name) or {}).get("id"),
                      f"crashed {count} times within {settings()['restart']['window_minutes']} min; not restarting "
                      f"(`theseus agents restart {name}`)")
            return "failed", "restart budget exhausted"
        if due and due > time.time() and not reset and run.get("stop_reason") in ("crash", "exit"):
            return "backoff", f"next start in {int(due - time.time())}s"
        session = resume_session(conf, name, "crash")
        if dry_run:
            return "would restart", f"resume {session}" if session else "fresh"
        intent = "restart" if reset else ("resume" if session else "fresh")
        spawn(conf, {**slot, "profile": rec.get("profile") or slot["profile"]}, intent=intent, session=session, srv=srv,
              profile=run.get("profile"))
        if not session:
            task = current_task(conf, name)
            if task:
                want_prompt(conf, name, task["id"])
        detail = f"resuming {session}" if session else "fresh session"
        if wait:
            detail += f"; {settle(conf, record(conf, name), *wait_ready(conf, name))}"
        return "restarted", detail
    if rec.get("phase") in ("starting", "blocked", None):
        if dry_run:
            return "starting", rec.get("phase") or ""
        if wait:
            phase, detail = wait_ready(conf, name)
        else:
            phase, detail = observe(conf, rec, info, srv)
            limit = float(settings().get("ready_seconds") or 90)
            started = (last_run(conf, name) or {}).get("started_epoch") or time.time()
            if phase == "starting" and time.time() - started > limit:
                phase, detail = "timeout", f"not ready after {limit:g}s"
        label = settle(conf, rec, phase, detail, wait)
        if label and label != "ready":
            return label, detail
        if label is None:
            return "starting", ""
    return check_drift(conf, rec, slot, dry_run)


def check_drift(conf, rec, slot, dry_run):
    from . import workqueue
    run = last_run(conf, rec["name"]) or {}
    launched = workqueue.runtime(conf).get(rec["name"], {}).get("launched")
    if launched and launched != rec.get("profile"):
        return "kept", f"relaunched onto {launched} for a task"
    profile = run.get("profile") or rec.get("profile")
    try:
        _, _, _, digest = launch(conf, profile, rec["name"])
    except profiles.ProfileError as error:
        return "drift", str(error)
    if run.get("launch_hash") and digest != run["launch_hash"]:
        if not dry_run and not drift_reported(conf, rec["name"], run["gen"]):
            event(conf, "drift", rec["name"], None, f"gen {run['gen']}: the launch configuration changed; "
                                                    f"`theseus agents restart {rec['name']}` applies it")
        return "drift", "launch configuration changed; not restarted"
    return "kept", ""


def drift_reported(conf, name, gen):
    from . import workqueue
    return any(e["agent"] == name and e["detail"].startswith(f"gen {gen}:") for e in workqueue.events(conf, 50, kinds=["drift"]))


def drifted(conf, rec):
    run = last_run(conf, rec["name"]) or {}
    try:
        return bool(run.get("launch_hash")) and launch(conf, run.get("profile") or rec["profile"], rec["name"])[3] != run["launch_hash"]
    except profiles.ProfileError:
        return True


def retire(conf, srv, rec, dry_run):
    """An agent no longer configured: disable it while it holds a task, else stop it and drop its record."""
    name = rec["name"]
    task = current_task(conf, name)
    if task:
        if not dry_run and (rec.get("enabled") or rec.get("desired") != "retire"):
            update(conf, name, enabled=False, desired="retire")
        return "deferred", f"task {task['id']} in progress; stopping it once free"
    if dry_run:
        return "would stop", "no longer configured"
    if rec.get("target") and srv.has_session(rec["target"]):
        stop(conf, rec, graceful=True, wait=False, reason="retire")
        info = pane_info(srv, rec["target"])
        if info and info.get("pane_dead") != "1":
            return "stopping", "exit command sent"
        srv.kill_session(rec["target"])
    with store.for_project(conf) as conn:
        store.remove_agent(conn, conf.name, name)
    return "removed", "no longer configured"


# stopping, restarting, relaunching

def busy(conf, rec, profile=None):
    from . import agents, workqueue
    return agents.pane_busy(conf, rec["pane"], profile or workqueue.launched(conf, rec), server=server_of(rec))


def wait_idle(conf, rec, timeout=180, poll=5):
    deadline = time.monotonic() + timeout
    while busy(conf, rec):
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)
    return True


def wait_dead(srv, rec, timeout=30, poll=None):
    poll = float(poll if poll is not None else os.environ.get("THESEUS_DEAD_POLL") or 0.5)
    deadline = time.monotonic() + timeout
    while True:
        info = pane_info(srv, rec["target"])
        if info is None or info.get("pane_dead") == "1":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def kill_pane(srv, rec, grace=10):
    """End the pane's process with SIGTERM, then SIGKILL after grace seconds; the pane stays (remain-on-exit) for the
    next start."""
    import signal
    info = pane_info(srv, rec["target"]) or {}
    pid = info.get("pane_pid", "")
    if not pid.isdigit() or info.get("pane_dead") == "1":
        return
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5)):
        try:
            os.kill(int(pid), sig)
        except OSError:
            return
        if wait_dead(srv, rec, timeout=wait, poll=0.2):
            return


def at_prompt(conf, rec, info):
    """Whether typing the exit command reaches the agent's prompt: the profile has one, the agent is ready and its CLI
    does not say it waits for an answer. A startup dialog or a permission prompt would take the keystrokes as an answer, so those are killed."""
    from . import workqueue
    if (record(conf, rec["name"]) or rec).get("phase") != "ready":
        return False
    if not profiles.interactive(conf, workqueue.launched(conf, rec)).get("exit"):
        return False
    entry = status_of(conf, workqueue.launched(conf, rec), info.get("pane_pid"))
    return not (entry and entry.get("status") == "waiting")


def plan_stop(conf, name, reason):
    run = last_run(conf, name)
    if run and not run.get("ended"):
        with store.for_project(conf) as conn:
            store.update_run(conn, run["id"], stop_reason=reason)


def stop(conf, rec, graceful=True, wait=True, reason="down", idle_timeout=180, grace=10):
    """Type the profile's exit command at an idle prompt (graceful), wait for the pane to die, then close the run.
    Returns (ok, detail); without wait it returns once the command is typed."""
    from . import workqueue
    srv = server_of(rec)
    name = rec["name"]
    info = pane_info(srv, rec["target"])
    if info is None:
        return True, "not running"
    plan_stop(conf, name, reason)
    if graceful and not at_prompt(conf, rec, info):
        graceful = False
    if info.get("pane_dead") != "1" and graceful:
        if wait and not wait_idle(conf, rec, idle_timeout, poll=float(os.environ.get("THESEUS_STOP_POLL") or 5)):
            return False, f"still busy after {idle_timeout}s"
        exit_text = profiles.interactive(conf, workqueue.launched(conf, rec))["exit"]
        srv.send_literal(rec["pane"], exit_text)
        time.sleep(0.3)
        srv.send_key(rec["pane"], "Enter")
        if not wait:
            return True, "exit command sent"
        if not wait_dead(srv, rec, timeout=float(os.environ.get("THESEUS_STOP_SECONDS") or 30)):
            kill_pane(srv, rec, grace)
    elif info.get("pane_dead") != "1":
        kill_pane(srv, rec, grace)
    info = pane_info(srv, rec["target"]) or {}
    record_exit(conf, name, info.get("@theseus_gen"), info.get("pane_dead_status"), info.get("pane_dead_signal"), srv)
    run = last_run(conf, name)
    if run and not run.get("ended"):
        with store.for_project(conf) as conn:
            store.close_run(conn, run["id"], reason)
    return True, "stopped"


def paused(conf):
    with store.for_project(conf) as conn:
        return store.get_meta(conn, f"supervision.paused:{conf.name}") is not None


def set_paused(conf, value):
    with store.for_project(conf) as conn:
        if value:
            store.set_meta(conn, f"supervision.paused:{conf.name}", store.iso())
        else:
            store.delete_meta(conn, f"supervision.paused:{conf.name}")


def down(conf, names=None, force=False, now=False):
    """Stop managed agents and keep them stopped (desired = down); without names every agent stops and supervision
    pauses until `theseus agents up`. Returns [(name, action, detail)]."""
    from . import workqueue
    regs = records(conf)
    chosen = names or [n for n, r in regs.items() if r.get("managed")]
    results = []
    if not names:
        set_paused(conf, True)
        results.append(("-", "paused", "supervision paused; `theseus agents up` resumes it"))
    for name in chosen:
        rec = regs.get(name)
        if not rec or not rec.get("managed"):
            results.append((name, "skipped", "not a supervised agent"))
            continue
        # Marked down under the queue lock before the pane stops, so no tick assigns to it in between.
        with workqueue.locked(conf):
            task = current_task(conf, name)
            if task and not force:
                results.append((name, "refused", f"holds task {task['id']}; --force stops it anyway (the task stays assigned)"))
                continue
            update(conf, name, desired="down")
        graceful = not now and not (force and busy(conf, rec))
        ok, detail = stop(conf, rec, graceful=graceful, reason="down", grace=5 if force else 10)
        if not ok:
            update(conf, name, desired=rec.get("desired"))
            results.append((name, "refused", detail))
            continue
        server_of(rec).kill_session(rec["target"])
        update(conf, name, desired="down", phase=None)
        results.append((name, "stopped", detail))
    return results


def restart(conf, name, fresh=False, intent="restart", profile=None, wait=True, graceful=True):
    """Gracefully restart one agent, resuming its conversation unless fresh. Clears a failed agent. Returns
    (ok, detail)."""
    rec = record(conf, name)
    if not rec or not rec.get("managed"):
        raise SupervisorError(f"{name} is not a supervised agent")
    slot = next((w for w in desired(conf) if w["name"] == name),
                {"name": name, "profile": rec["profile"], "slot": rec.get("slot")})
    srv = server_of(rec)
    session = None if fresh else (live_session(conf, rec) or rec.get("session_id"))
    if pane_info(srv, rec["target"]) is not None:
        ok, detail = stop(conf, rec, graceful=graceful, reason="relaunch" if intent == "relaunch" else "restart")
        if not ok:
            return False, detail
    spawn(conf, slot, intent=intent, session=session, profile=profile, srv=srv)
    if not wait:
        return True, "started"
    phase, detail = wait_ready(conf, name)
    label = settle(conf, record(conf, name), phase, detail, wait=True)
    return label == "ready" or label == "restarted fresh", f"{'resumed ' + session if session else 'fresh session'}; {label or phase}"


def live_session(conf, rec):
    """The agent's current conversation id from its status entry (it changes on /clear), or None."""
    from . import workqueue
    info = pane_info(server_of(rec), rec["target"]) if rec.get("target") else None
    if not info:
        return None
    entry = status_of(conf, workqueue.launched(conf, rec), info.get("pane_pid"))
    return entry.get("sessionId") if entry else None


def refresh_sessions(conf):
    """Store each ready Claude agent's current session id (after /clear it changes)."""
    for name, rec in records(conf).items():
        if not rec.get("managed") or rec.get("phase") != "ready":
            continue
        session = live_session(conf, rec)
        if session and session != rec.get("session_id"):
            update(conf, name, session_id=session)
            run = last_run(conf, name)
            if run and not run.get("ended"):
                with store.for_project(conf) as conn:
                    store.update_run(conn, run["id"], session_id=session)


def relaunch(conf, agent, profile):
    """Move a managed agent onto another profile in its own pane, resuming its conversation (queue relaunch)."""
    rec = record(conf, agent["name"])
    if not wait_idle(conf, rec):
        return False, "still busy after 3 minutes; not relaunched"
    ok, detail = restart(conf, agent["name"], intent="relaunch", profile=profile)
    return ok, f"{agent['name']} {detail}"


def thrashing_restart(conf, agent, task_id):
    """A thrashing agent restarts on a fresh session; the queue sends its task's restart prompt once it is ready."""
    rec = record(conf, agent["name"])
    ok, detail = restart(conf, agent["name"], fresh=True, intent="fresh", wait=False, graceful=False)
    want_prompt(conf, agent["name"], task_id)
    return ok, f"thrashing: restarted on a fresh session ({detail}); the task stays assigned"


# migration from hand-made panes

def migrate(conf, names=None, with_task=False, timeout=60):
    """Move agents from hand-made panes on the user's tmux server onto the private server under the same names,
    resuming their conversations. Returns [(name, action, detail)]."""
    from . import agents, workqueue
    if not managed(conf):
        raise SupervisorError(f"set [agents] managed = true in {cfg.LOCAL_NAME} (with [agents.counts]) first")
    wanted = {w["name"]: w for w in desired(conf)}
    regs = records(conf)
    chosen = names or [n for n, r in regs.items() if not r.get("managed")]
    results = []
    legacy = tm.Server.legacy()
    for name in chosen:
        rec = regs.get(name)
        if not rec:
            results.append((name, "skipped", "not registered"))
            continue
        if rec.get("managed"):
            results.append((name, "skipped", "already supervised"))
            continue
        if name not in wanted:
            results.append((name, "skipped", f"not a configured agent; its name must match [agents] name "
                                             f"{template(conf)!r} and counts, or an [[agents.extra]] entry"))
            continue
        task = agents.current_task(conf, name)
        if task and not with_task:
            results.append((name, "deferred", f"holds task {task['id']}; --with-task moves it anyway (its conversation resumes)"))
            continue
        pane = rec["pane"]
        session = None
        if agents.pane_alive(pane):
            pid = legacy.display(pane, "#{pane_pid}")
            entry = status_of(conf, workqueue.launched(conf, rec), pid) if pid and pid.isdigit() else None
            if entry:
                session, agent_pid = entry.get("sessionId"), entry.get("pid")
            else:
                session, agent_pid = legacy_session(conf, rec, pid)
            if agents.pane_busy(conf, pane, workqueue.launched(conf, rec)):
                results.append((name, "deferred", "busy; migrate it once idle"))
                continue
            if session is None or agent_pid is None:
                results.append((name, "skipped", f"no agent session found in pane {pane}"))
                continue
            exit_text = profiles.interactive(conf, workqueue.launched(conf, rec)).get("exit")
            if not exit_text:
                results.append((name, "skipped", "its profile has no interactive.exit command to end the agent in your pane"))
                continue
            legacy.run("send-keys", "-t", pane, "-l", exit_text)
            time.sleep(0.3)
            legacy.run("send-keys", "-t", pane, "Enter")
            deadline = time.monotonic() + timeout
            while agent_pid and pid_alive(agent_pid) and time.monotonic() < deadline:
                time.sleep(0.5)
            if agent_pid and pid_alive(agent_pid):
                results.append((name, "failed", f"the agent in {pane} did not exit within {timeout}s"))
                continue
            legacy.run("send-keys", "-t", pane, "-l",
                       f"# {name} now runs under theseus: theseus agents attach {name}")
        update(conf, name, session_id=session)
        slot = wanted[name]
        spawn(conf, slot, intent="migrate", session=session, profile=workqueue.launched(conf, rec))
        phase, detail = wait_ready(conf, name)
        label = settle(conf, record(conf, name), phase, detail, wait=True)
        results.append((name, "migrated", f"{'resumed ' + session if session else 'fresh session'}; {label or phase}"))
    return results


def legacy_session(conf, rec, pane_pid):
    """(session id, agent pid) from the Claude Code session record of a process in the pane, the pre-supervisor way;
    (None, None) when there is none."""
    from . import workqueue
    folder = os.path.expanduser(profiles.interactive(conf, workqueue.launched(conf, rec)).get("sessions_dir") or "")
    if not folder or not pane_pid or not pane_pid.isdigit():
        return None, None
    from . import top
    for pid in top.descendants(top.process_table(), int(pane_pid)):
        path = Path(folder) / f"{pid}.json"
        if path.is_file():
            try:
                return json.loads(path.read_text()).get("sessionId"), pid
            except ValueError:
                return None, None
    return None, None


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


# the pane wrapper

def exec_agent(project, name, gen):
    """`theseus agents _exec`: become the agent of one run. Never returns on success."""
    conf = project_conf(project)
    if conf is None:
        print(f"theseus: project {project} is not registered on this host", file=sys.stderr)
        return 2
    with store.for_project(conf) as conn:
        run = store.run_of(conn, conf.name, name, int(gen))
    if run is None:
        print(f"theseus: no run {gen} of {name}", file=sys.stderr)
        return 2
    argv, env, where, _ = launch(conf, run["profile"], name, run["session_id"], bool(run["resume"]))
    with open(agent_dir(conf, name) / "runs.log", "a") as handle:
        handle.write(f"{store.iso()} gen {gen} {run['intent']} {run['profile']} cwd {where} env {' '.join(sorted(env))} "
                     f"argv {shlex.join(argv)}\n")
    os.chmod(agent_dir(conf, name) / "runs.log", 0o600)
    os.environ.update(env)
    os.environ["THESEUS_AGENT"] = name
    if str(settings().get("launch_shell") or "exec") == "login":
        shell = os.environ.get("SHELL") or "/bin/sh"
        os.execvp(shell, [shell, "-lc", "exec " + shlex.join(argv)])
    if shutil.which(argv[0]) is None:
        print(f"theseus: {argv[0]} is not on PATH (supervised agents exec it directly; aliases and shell functions do not "
              f"apply, see [supervisor] launch_shell)", file=sys.stderr)
        return 127
    os.execvp(argv[0], argv)


# the dashboard's snapshot API

def snapshot(conf):
    """Every supervised agent of a project with its live pane facts: name, profile, phase, gen, pid, pane id,
    session id, dead, restarts in the window, address (the pane argument for capture, send and attach)."""
    srv = server()
    found = []
    for name, rec in records(conf).items():
        if not rec.get("managed"):
            continue
        info = pane_info(server_of(rec), rec["target"]) if rec.get("target") else None
        run = last_run(conf, name) or {}
        count, _, _ = crash_history(conf, name)
        found.append({"name": name, "id": rec.get("id"), "profile": rec.get("profile"), "phase": rec.get("phase"), "desired": rec.get("desired"),
                      "gen": run.get("gen"), "runProfile": run.get("profile"), "sessionId": rec.get("session_id"),
                      "pid": int(info["pane_pid"]) if info and info.get("pane_pid", "").isdigit() else None,
                      "paneId": (info or {}).get("pane_id"), "dead": bool(info and info.get("pane_dead") == "1"),
                      "running": info is not None, "restarts": count, "address": rec.get("pane"),
                      "server": rec.get("server") or srv.name, "started": run.get("started_epoch")})
    return found


def capture(agent, history=0, ansi=True):
    """The screen (and history lines) of a managed agent's pane; agent is a record or an address. '' when it fails."""
    if isinstance(agent, dict):
        srv, target = server_of(agent), agent["pane"]
    else:
        srv, target = tm.resolve(agent)
    return srv.capture(target, history=history, ansi=ansi) or ""


def send(conf, name, text, expect_busy=True):
    """Type a line into a managed agent through the queue's verified send. Returns (ok, detail)."""
    from . import agents
    rec = record(conf, name)
    if not rec:
        raise SupervisorError(f"no agent {name}")
    return agents.send_verified(conf, agents.entry(rec), text, expect_busy=expect_busy)


MIN_FIT = (20, 5)


def fit(rec, columns=None, rows=None):
    """Size a supervised agent's window on the private server to columns x rows (what the console's pane mirror
    shows), or back to the pinned [supervisor] size when columns is None. Agents on the user's own server are never
    resized, nor is a window while a client is attached to it: the attach hooks size it to the client and restore the
    pinned size on detach, and the mirror fits it again on its next poll. Returns True when it resized the window."""
    if not rec or not rec.get("managed") or not rec.get("target"):
        return False
    srv = server_of(rec)
    if srv.is_legacy:
        return False
    if columns is None:
        want = size()
    elif columns < MIN_FIT[0] or rows < MIN_FIT[1]:
        return False
    else:
        want = (int(columns), int(rows))
    target = tm.address(rec["target"])
    state = srv.display(target, "#{session_attached} #{window_width} #{window_height}")
    if not state:
        return False
    attached, width, height = state.split()
    if attached != "0" or (int(width), int(height)) == want:
        return False
    return srv.resize_window(target, *want).returncode == 0


def attach_argv(rec, read_only=False):
    srv = server_of(rec)
    argv = ["env", "-u", "TMUX", *srv.argv("attach-session", "-t", f"={rec['target']}")]
    return argv + (["-r"] if read_only else [])


def open_window(rec, read_only=False):
    """Open the agent's pane in a new window of the user's own tmux (the one explicit action on that server)."""
    if not os.environ.get("TMUX") or tm.private_tmux(os.environ.get("TMUX")):
        return False, "open needs a terminal inside your own tmux session; `theseus agents attach NAME` works anywhere"
    result = tm.Server.legacy().run("new-window", "-n", rec["name"][:30], shlex.join(attach_argv(rec, read_only)))
    if result.returncode != 0:
        return False, f"tmux: {(result.stderr or '').strip()[:80]}"
    return True, f"opened {rec['name']} in a new window (detach with {settings().get('detach_key') or 'C-q'})"


def transcript(conf, rec):
    """The record of the agent's current session from its profile's transcript adapter (theseus.adapters), when it
    exists, or None."""
    from . import adapters
    _, adapter, interactive = adapters.resolve(conf, rec.get("profile"))
    path = adapter.session_file(conf, interactive, rec.get("session_id"), str(cwd(conf))) if adapter else None
    return path if path and path.exists() else None
