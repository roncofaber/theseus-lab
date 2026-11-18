"""The project's coordinator as a supervised session of the private tmux server, `{project}--coordinator`.

`theseus coordinator up` starts the agent of [coordinator] profile (project or host [defaults]; no built-in default)
in the project root. Like a supervised agent its pane runs a theseus entry point (`coordinator _exec`) that becomes
the agent, every run is a row of agent_runs under the reserved name "coordinator" and a coordinator_sessions row,
and the pane-died hook restarts it after a crash with the [supervisor.restart] backoff. Unlike an agent it is never
in the agents table (the queue never assigns it work), it gets no run credentials and none of the worker variables,
and its theseus MCP server runs with --role coordinator, so every theseus command it runs acts as the coordinator.
It always resumes its conversation after a crash or a reboot; only `restart --fresh`, or `up` after `down`, starts a
new one, which gets the project's start prompt once it is ready. Each start registers the session for the attention
push. The queue driver checks it after each tick (coordinator.tick), which also covers a lost server and a missed
hook.
"""
import json
import os
import shlex
import shutil
import sys
import time
from importlib import resources

from . import store
from . import tmux as tm

NAME = "coordinator"
WORKER_ENV = ("THESEUS_AGENT", "THESEUS_AGENT_ID", "THESEUS_CREDENTIALS", "THESEUS_WORKER_LOG")


class CoordinatorError(Exception):
    pass


def section(conf):
    return conf.section("coordinator")


def autostart(conf):
    return section(conf).get("autostart") in (True, "true", "on", 1)


def session(conf):
    return tm.session_name(conf.name, NAME)


def address(conf):
    return tm.address(session(conf))


def home(conf):
    path = conf.data / NAME
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def meta_key(conf):
    return f"coordinator.session:{conf.name}"


def state(conf):
    """{desired, profile, session_id, server, phase, gen, record, prompt_owed} of the supervised coordinator, or {}."""
    with store.for_project(conf) as conn:
        value = store.get_meta(conn, meta_key(conf))
    try:
        return json.loads(value) if value else {}
    except ValueError:
        return {}


def save(conf, **fields):
    with store.for_project(conf) as conn:
        with store.transaction(conn):
            current = store.get_meta(conn, meta_key(conf))
            data = json.loads(current) if current else {}
            data.update(fields)
            store.set_meta(conn, meta_key(conf), json.dumps(data, sort_keys=True))
    return data


def project_of(session_name):
    """The project whose coordinator runs in this session, or None."""
    with store.for_host() as conn:
        names = [row["name"] for row in store.list_projects(conn)]
    return next((name for name in names if tm.session_name(name, NAME) == session_name), None)


def profile_name(conf, given=None):
    """The coordinator's profile: given, else [coordinator] profile. There is no default; the user chooses."""
    from . import profiles
    name = given or str(section(conf).get("profile") or "")
    if not name:
        raise CoordinatorError("no coordinator profile: set [coordinator] profile = \"NAME\" in theseus.local.toml (or "
                               "[defaults] coordinator.profile in the host config.toml) to an agent profile with "
                               "interactive settings (`theseus agents profiles`), or pass --profile")
    try:
        profile = profiles.resolve(conf, name)
    except profiles.ProfileError as error:
        raise CoordinatorError(f"[coordinator] profile: {error}") from error
    if not profile.get("interactive"):
        raise CoordinatorError(f"[coordinator] profile {name} has no persistent-instance (interactive) settings")
    return name


def launch(conf, profile_name, session_id=None, resume=False):
    """(argv, env) of one coordinator run: the profile's interactive launch named {project}-coordinator, with the
    coordinator's own MCP config and never a worker's credentials."""
    from . import profiles
    profile = profiles.resolve(conf, profile_name)
    agent = f"{conf.name}-{NAME}"
    parts = {"launch": shlex.split(profiles.launch_template(conf, profile, agent)),
             "mcp": profiles.mcp_args(conf, profile, NAME, role="coordinator"),
             "extra": profiles.extra_args(profile, agent=agent),
             "env": {**profiles.env(profile), **profiles.mcp_environment(conf, profile, NAME, role="coordinator")},
             "interactive_env": profiles.interactive_env(profile)}
    if not parts["launch"]:
        raise CoordinatorError(f"profile {profile_name} has no launch command")
    try:
        return profiles.interactive_argv(conf, profile, agent, session_id, resume, parts=parts)
    except profiles.ProfileError as error:
        raise CoordinatorError(str(error)) from error


def prompt_text(conf):
    """The first prompt of a fresh coordinator: the project's template ([coordinator] prompt), else the packaged one,
    with {project}, {root} and {session} filled."""
    path = conf.root / str(section(conf).get("prompt") or "")
    if path.is_file():
        text = path.read_text()
    else:
        text = (resources.files("theseus") / "templates/project/dot-agents/skills/theseus-coordinating/"
                "session-prompt.md").read_text()
    from . import profiles
    values = {"project": conf.name, "root": str(conf.root), "session": session(conf)}
    return " ".join(profiles.fill(text, values).split())


def session_env():
    from . import supervisor
    env = supervisor.session_env()
    for key in WORKER_ENV:
        env.pop(key, None)
    return env


def agent_entry(conf, st=None):
    """The coordinator as the record the checked send and the observers take."""
    st = st or state(conf)
    return {"name": NAME, "pane": address(conf), "target": session(conf), "provider": st.get("profile"),
            "profile": st.get("profile"), "managed": True, "server": st.get("server")}


def server(conf=None):
    from . import supervisor
    return supervisor.server()


def pane(srv, conf):
    from . import supervisor
    return supervisor.pane_info(srv, session(conf)) if srv.alive() else None


def event(conf, kind, detail):
    from . import workqueue
    return workqueue.event(conf, kind, None, None, f"coordinator {detail}")


# starting

def spawn(conf, profile, intent, session_id=None, srv=None):
    """Start (or respawn) the coordinator's pane; session_id resumes that conversation, otherwise a new one starts
    and owes the start prompt."""
    from . import profiles, supervisor, tmuxconf
    srv = srv or server()
    supervisor.ensure_conf(srv)
    resume = bool(session_id)
    session_id = session_id or profiles.new_session(profiles.resolve(conf, profile))
    launch(conf, profile, session_id, resume)
    target = session(conf)
    with store.for_project(conf) as conn:
        run = store.add_run(conn, conf.name, NAME, intent, profile=profile, session_id=session_id, resume=resume)
        record = store.start_session(conn, conf.name, pane=address(conf), profile=profile,
                                     detail=f"supervised gen {run['gen']} {'resuming' if resume else 'session'} {session_id}")
    fields = {"desired": "up", "profile": profile, "session_id": session_id, "server": srv.name, "phase": "starting",
              "gen": run["gen"], "record": record}
    if not resume:
        fields["prompt_owed"] = True
    save(conf, **fields)
    command = [*supervisor.theseus_argv(), "coordinator", "_exec", conf.name, str(run["gen"])]
    width, height = supervisor.size()
    try:
        if supervisor.pane_info(srv, target) is None:
            srv.new_session(target, command, conf.root, width, height, env=session_env())
        else:
            srv.respawn(tm.address(target), command, cwd=conf.root, env=session_env())
    except tm.TmuxError as error:
        with store.for_project(conf) as conn:
            store.close_run(conn, run["id"], "spawn-failed")
            store.end_session(conn, record)
        save(conf, phase="failed")
        raise CoordinatorError(f"coordinator: {error}") from error
    srv.set_option(tm.address(target), "@theseus_project", conf.name)
    srv.set_option(tm.address(target), "@theseus_gen", str(run["gen"]))
    srv.run("resize-window", "-x", str(width), "-y", str(height), "-t", tm.address(target))
    tmuxconf.tag(srv, conf, target, NAME)
    info = supervisor.pane_info(srv, target) or {}
    if info.get("pane_pid", "").isdigit():
        with store.for_project(conf) as conn:
            store.update_run(conn, run["id"], pid=int(info["pane_pid"]))
    register(conf, srv, profile)
    event(conf, "spawned" if intent == "start" else "restarted",
          f"gen {run['gen']} {profile} ({intent}, {'resuming' if resume else 'session'} {session_id})")
    return run


def register(conf, srv, profile):
    """Register the session for the attention push (again after each start, since the pane's pid changed)."""
    from . import attention
    try:
        return attention.register(conf, address(conf), profile, server=srv.name)
    except attention.AttentionError as error:
        print(f"theseus: coordinator push registration failed: {error}", file=sys.stderr)
        return None


def observe(conf, srv, st=None):
    from . import supervisor
    st = st or state(conf)
    rec = agent_entry(conf, st)
    return supervisor.observe(conf, rec, None, srv)


def wait_ready(conf, srv, timeout=None, poll=None):
    from . import supervisor
    timeout = float(timeout if timeout is not None else supervisor.settings().get("ready_seconds") or 90)
    poll = float(poll if poll is not None else os.environ.get("THESEUS_READY_POLL") or 0.5)
    deadline = time.monotonic() + timeout
    while True:
        phase, detail = observe(conf, srv)
        if phase in ("ready", "blocked", "dead", "gone", "resume-missing"):
            break
        if time.monotonic() >= deadline:
            phase, detail = "timeout", f"not ready after {timeout:g}s"
            break
        time.sleep(poll)
    settle(conf, srv, phase, detail)
    return phase, detail


def settle(conf, srv, phase, detail):
    """Record what an observation saw: ready marks the run and sends an owed start prompt; a startup dialog or a
    timeout is blocked (an error event, once); a resumed conversation that no longer exists starts fresh."""
    st = state(conf)
    if phase == "ready":
        if st.get("phase") != "ready":
            with store.for_project(conf) as conn:
                run = store.last_run(conn, conf.name, NAME)
                if run and not run.get("ready_epoch"):
                    store.update_run(conn, run["id"], ready_epoch=time.time())
            fields = {"phase": "ready"}
            if detail and detail != st.get("session_id"):
                fields["session_id"] = detail
            save(conf, **fields)
            event(conf, "ready", f"gen {st.get('gen')}")
        if state(conf).get("prompt_owed"):
            send_prompt(conf)
    elif phase in ("blocked", "timeout"):
        if st.get("phase") != "blocked":
            save(conf, phase="blocked")
            event(conf, "error", f"{detail}: run `theseus coordinator attach` and answer it")
    elif phase == "resume-missing":
        with store.for_project(conf) as conn:
            run = store.last_run(conn, conf.name, NAME)
            if run and not run.get("ended"):
                store.close_run(conn, run["id"], "resume-missing")
        event(conf, "restarted", f"{detail}; started fresh")
        spawn(conf, st.get("profile"), "fresh", None, srv)


def send_prompt(conf):
    """Type the start prompt into a ready coordinator once, through the checked send."""
    from . import agents
    st = state(conf)
    ok, detail = agents.send_verified(conf, agent_entry(conf, st), prompt_text(conf), record=False,
                                      profile=st.get("profile"))
    if ok:
        save(conf, prompt_owed=False)
    return ok, detail


def up(conf, profile=None, wait=True):
    """Start the coordinator unless it runs. It resumes its last conversation when it was meant to be running (lost
    to a reboot or a crash) and starts a new one after `coordinator down`. Returns (action, detail)."""
    from . import supervisor, workqueue
    name = profile_name(conf, profile)
    srv = server()
    supervisor.ensure_conf(srv)
    with workqueue.locked(conf):
        st = state(conf)
        info = pane(srv, conf)
        if info and info.get("pane_dead") == "0":
            save(conf, desired="up")
            register(conf, srv, st.get("profile") or name)
            return "kept", f"session {session(conf)} ({st.get('profile') or name})"
        if info:
            record_exit(conf, srv, info.get("pane_dead_status"), info.get("pane_dead_signal"))
        resume = st.get("session_id") if st.get("desired") == "up" and (st.get("profile") or name) == name else None
        if st.get("phase") == "failed":
            resume = st.get("session_id") if st.get("profile") == name else None
        spawn(conf, name, "resume" if resume else "start", resume, srv)
    detail = f"session {session(conf)}, {'resuming ' + resume if resume else 'new conversation'}"
    if wait:
        phase, _ = wait_ready(conf, srv)
        detail += f"; {phase}"
    return "started", detail


# stopping and restarting

def stop(conf, srv, reason, graceful=True, grace=10):
    """End the coordinator's process: its exit command at an idle prompt, else SIGTERM then SIGKILL; the pane stays
    for a respawn. Returns the stop reason recorded."""
    from . import agents, profiles, supervisor
    st = state(conf)
    info = pane(srv, conf)
    if info is None:
        return None
    with store.for_project(conf) as conn:
        run = store.last_run(conn, conf.name, NAME)
        if run and not run.get("ended"):
            store.update_run(conn, run["id"], stop_reason=reason)
    rec = agent_entry(conf, st)
    alive = info.get("pane_dead") != "1"
    exit_text = profiles.interactive(conf, st.get("profile")).get("exit")
    if alive and graceful and exit_text and st.get("phase") == "ready" \
            and not agents.pane_busy(conf, rec["pane"], st.get("profile"), server=srv) \
            and not agents.pane_prompt(conf, rec["pane"], st.get("profile"), server=srv):
        srv.send_literal(rec["pane"], exit_text)
        time.sleep(0.3)
        srv.send_key(rec["pane"], "Enter")
        if not supervisor.wait_dead(srv, rec, timeout=float(os.environ.get("THESEUS_STOP_SECONDS") or 30)):
            supervisor.kill_pane(srv, rec, grace)
    elif alive:
        supervisor.kill_pane(srv, rec, grace)
    info = pane(srv, conf) or {}
    return record_exit(conf, srv, info.get("pane_dead_status"), info.get("pane_dead_signal")) or reason


def down(conf):
    """Stop the coordinator and keep it stopped (the hook and the driver leave it alone); unregister the push."""
    from . import attention, workqueue
    srv = server()
    with workqueue.locked(conf):
        save(conf, desired="down")
        info = pane(srv, conf)
        if info is None:
            stopped = False
        else:
            stop(conf, srv, "down")
            srv.kill_session(session(conf))
            stopped = True
        save(conf, phase=None)
    record = attention.coordinator(conf)
    if record and record.get("pane") == address(conf):
        attention.unregister(conf)
    return ("stopped", f"session {session(conf)}") if stopped else ("none", "no coordinator session ran")


def restart(conf, fresh=False, profile=None, wait=True):
    """Restart the coordinator, resuming its conversation unless fresh (or the profile changed). Returns (ok, detail)."""
    from . import workqueue
    name = profile_name(conf, profile or (None if section(conf).get("profile") else state(conf).get("profile")))
    srv = server()
    with workqueue.locked(conf):
        st = state(conf)
        launch(conf, name)
        session_id = None if fresh or st.get("profile") not in (None, name) else (live_session(conf, srv) or st.get("session_id"))
        if pane(srv, conf) is not None:
            stop(conf, srv, "restart")
        spawn(conf, name, "restart", session_id, srv)
    if not wait:
        return True, "started"
    phase, detail = wait_ready(conf, srv)
    how = f"resumed {session_id}" if session_id else "new conversation"
    return phase in ("ready", "resume-missing"), f"{how}; {phase}" + (f" ({detail})" if phase != "ready" and detail else "")


def live_session(conf, srv):
    """The conversation id the coordinator's CLI reports now (it changes on /clear), or None."""
    from . import supervisor
    st = state(conf)
    info = pane(srv, conf)
    if not info or not st.get("profile"):
        return None
    entry = supervisor.status_of(conf, st["profile"], info.get("pane_pid"))
    return entry.get("sessionId") if entry else None


# exits, the hook and the driver's check

def record_exit(conf, srv, status=None, signal=None, run=None):
    """Close the coordinator's open run once, keeping the last screen of a crash. Returns the stop reason, or None
    when it was already closed."""
    from . import supervisor
    with store.for_project(conf) as conn:
        run = run or store.last_run(conn, conf.name, NAME)
    if not run or run.get("ended"):
        return None
    reason = supervisor.classify(run, status)
    if reason in ("crash", "exit"):
        screen = srv.capture(address(conf), history=200, ansi=False, join=True)
        if screen:
            supervisor.save_screen(home(conf) / f"crash-{run['gen']}.txt", screen)
    value = int(status) if str(status or "").lstrip("-").isdigit() else None
    with store.for_project(conf) as conn:
        if not store.close_run(conn, run["id"], reason, value, signal or None):
            return None
    record = state(conf).get("record")
    if record:
        with store.for_project(conf) as conn:
            store.end_session(conn, record)
    if reason in ("crash", "exit"):
        how = f"signal {signal}" if signal else f"status {status if status not in (None, '') else '?'}"
        event(conf, "exited", f"gen {run['gen']} exited ({how})")
    else:
        event(conf, "stopped", f"gen {run['gen']} ({reason})")
    return reason


def recover(conf, srv, now=None):
    """Restart a crashed coordinator that is due, resuming its conversation; mark it failed past the restart budget.
    Returns an action label or None."""
    from . import supervisor
    st = state(conf)
    if st.get("desired") != "up" or st.get("phase") == "failed":
        return None
    due, give_up = supervisor.next_start(conf, NAME, now)
    if give_up:
        save(conf, phase="failed")
        policy = supervisor.settings()["restart"]
        event(conf, "error", f"crashed {policy['max_crashes']} times within {policy['window_minutes']} min; not "
                             f"restarting (`theseus coordinator restart`)")
        return "failed"
    if due and due > (time.time() if now is None else now):
        return "backoff"
    spawn(conf, st.get("profile"), "resume" if st.get("session_id") else "fresh", st.get("session_id"), srv)
    return "restarted"


def died(conf, srv, pid=None, status=None, signal=None):
    """The pane-died hook for the coordinator's session: record the exit of the run whose pid ended, then restart it
    when its backoff allows (within 15 s; later the driver's check does). Returns 0."""
    from . import supervisor, workqueue
    run = None
    if str(pid or "").isdigit():
        deadline = time.monotonic() + 5
        while run is None and time.monotonic() < deadline:
            with store.for_project(conf) as conn:
                run = next((r for r in store.runs(conn, conf.name, NAME, limit=5) if r.get("pid") == int(pid)), None)
            if run is None:
                time.sleep(0.2)
    if run is None:
        return 0
    reason = record_exit(conf, srv, status, signal, run)
    if reason not in ("crash", "exit"):
        return 0
    due, give_up = supervisor.next_start(conf, NAME)
    delay = 0 if give_up else due - time.time()
    if delay > 15:
        return 0
    if delay > 0:
        time.sleep(delay)
    action = None
    with workqueue.locked(conf):
        info = pane(srv, conf)
        if info is None or info.get("pane_dead") == "1":
            try:
                action = recover(conf, srv)
            except (CoordinatorError, tm.TmuxError) as error:
                event(conf, "error", f"restart failed: {error}")
                return 0
    if action == "restarted":
        wait_ready(conf, srv)
    return 0


def tick(conf, now=None):
    """The driver's check after each tick, never blocking: restart a dead or lost coordinator that is due, settle a
    starting one, send an owed start prompt, keep the conversation id and the push registration current. Returns
    the lines to log."""
    st = state(conf)
    if st.get("desired") != "up":
        return []
    srv = server()
    info = pane(srv, conf)
    lines = []
    if info is None or info.get("pane_dead") == "1":
        from . import workqueue
        with workqueue.locked(conf):
            info = pane(srv, conf)
            if info is not None and info.get("pane_dead") == "0":
                return []
            if info is not None:
                record_exit(conf, srv, info.get("pane_dead_status"), info.get("pane_dead_signal"))
            else:
                with store.for_project(conf) as conn:
                    run = store.last_run(conn, conf.name, NAME)
                    if run and not run.get("ended"):
                        store.close_run(conn, run["id"], "server-lost" if not srv.alive() else "session-lost")
            action = recover(conf, srv, now)
        return [action] if action and action != "backoff" else []
    if st.get("phase") in ("starting", "blocked", None) or st.get("prompt_owed"):
        phase, detail = observe(conf, srv, st)
        if phase == "starting":
            from . import supervisor
            with store.for_project(conf) as conn:
                started = (store.last_run(conn, conf.name, NAME) or {}).get("started_epoch") or time.time()
            limit = float(supervisor.settings().get("ready_seconds") or 90)
            if time.time() - started > limit:
                phase, detail = "timeout", f"not ready after {limit:g}s"
        if phase != "starting":
            settle(conf, srv, phase, detail)
            lines.append(phase)
    if state(conf).get("phase") == "ready":
        current = live_session(conf, srv)
        if current and current != st.get("session_id"):
            save(conf, session_id=current)
    from . import attention
    record = attention.coordinator(conf)
    if record is None or record.get("pane") != address(conf) or attention.pane_problem(record):
        register(conf, srv, st.get("profile"))
    return lines


# attaching, status and the pane wrapper

def inside(conf, env=None):
    """Whether this process runs in the coordinator's own pane (stopping it from there would end the command)."""
    env = os.environ if env is None else env
    if not env.get("TMUX_PANE") or not tm.private_tmux(env.get("TMUX")):
        return False
    srv = server()
    return srv.display(address(conf), "#{pane_id}") == env["TMUX_PANE"]


def attach_argv(conf, read_only=False, env=None):
    from . import supervisor
    return supervisor.session_attach_argv(server(), session(conf), read_only, env)


def status_lines(conf):
    from . import attention, supervisor
    st = state(conf)
    srv = server()
    info = pane(srv, conf)
    if not st and info is None:
        return [f"no supervised coordinator for {conf.name} (theseus coordinator up)"]
    live = "not running" if info is None else ("dead" if info.get("pane_dead") == "1" else "running")
    with store.for_project(conf) as conn:
        run = store.last_run(conn, conf.name, NAME) or {}
    count, _, _ = supervisor.crash_history(conf, NAME)
    lines = [f"session {session(conf)}: {live}, phase {st.get('phase') or '-'}, wanted {st.get('desired') or '-'}",
             f"profile {st.get('profile') or '-'}, gen {run.get('gen') or '-'}, pid "
             f"{(info or {}).get('pane_pid') or '-'}, conversation {st.get('session_id') or '-'}, crashes in the window "
             f"{count}"]
    if st.get("prompt_owed"):
        lines.append("start prompt owed: sent once the agent is ready")
    record = attention.coordinator(conf)
    s = attention.settings(conf)
    push = "on" if s["notify"] == "pane" else "off ([attention] notify = \"pane\" turns it on)"
    if record is None:
        lines.append(f"push {push}; no coordinator pane registered")
    else:
        problem = attention.pane_problem(record)
        lines.append(f"push {push}; registered pane {record['pane']} ({problem or 'live'})")
    book = attention.bookkeeping(conf)
    if book.get("last"):
        lines.append(f"last push: {book['last']}")
    return lines


def exec_coordinator(project, gen):
    """`theseus coordinator _exec`: become the coordinator of one run, with none of the worker variables. Never
    returns on success."""
    from . import supervisor
    conf = supervisor.project_conf(project)
    if conf is None:
        print(f"theseus: project {project} is not registered on this host", file=sys.stderr)
        return 2
    with store.for_project(conf) as conn:
        run = store.run_of(conn, conf.name, NAME, int(gen))
    if run is None:
        print(f"theseus: no coordinator run {gen}", file=sys.stderr)
        return 2
    argv, env = launch(conf, run["profile"], run["session_id"], bool(run["resume"]))
    log = home(conf) / "runs.log"
    with open(log, "a") as handle:
        handle.write(f"{store.iso()} gen {gen} {run['intent']} {run['profile']} env {' '.join(sorted(env))} "
                     f"argv {shlex.join(argv)}\n")
    os.chmod(log, 0o600)
    os.environ.update(env)
    for key in WORKER_ENV:
        os.environ.pop(key, None)
    if str(supervisor.settings().get("launch_shell") or "exec") == "login":
        shell = os.environ.get("SHELL") or "/bin/sh"
        os.execvp(shell, [shell, "-lc", "exec " + shlex.join(argv)])
    if shutil.which(argv[0]) is None:
        print(f"theseus: {argv[0]} is not on PATH (the coordinator execs it directly; see [supervisor] launch_shell)",
              file=sys.stderr)
        return 127
    os.execvp(argv[0], argv)
