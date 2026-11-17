"""Work queue for registered agents.

A tick looks at every registered agent and acts on its state:
- delivered (handoff present): the task is marked delivered and a `delivered` event asks the coordinator to review and
  merge it; the agent is reset before its next task;
- stalled (idle past instances.idle_minutes with no handoff): one `continue` nudge, then a `stalled` event;
- thrashing (the pane shows a stall pattern of its profile, such as Claude Code's autocompact thrashing notice): one
  `stalled` event at once and no nudge, since typing more into that context only refills it;
- gateway_error (an idle pane shows a gateway error of its profile): retried with backoff, a fresh context when needed
  and a host-wide circuit breaker (theseus.gateway); a `stalled` event once the resends run out, an `error` event at
  once for a fatal error;
- free (enabled, no open task, its gateway pool not paused): if a ready task fits its room, the agent is reset (a full
  /clear when it carries dirty context into clean work, a /compact otherwise), the task is assigned on a fresh
  worktree, and its message is sent.
A task with "minCapability" goes only to agents whose profile capability ([workers.<name>] capability, default 1) is at
least that, unless they are registered with exactly the task's profile.
A task with a "profile" goes only to agents registered with that profile, or to agents whose profile differs from it only in
MCP servers or environment: those are relaunched onto it in their pane first (session resumed, a `relaunched` event),
and back onto their registered profile before their next task that does not need it. The profile an agent currently
runs is kept in the runtime state (`launched`).
Tasks with "hold": true are never assigned by the queue; `theseus queue pause` stops all assignment. Events go to the
host store (theseus.store); `theseus queue wait` blocks until one needs the coordinator. Every message the queue types
(task messages, nudges, gateway resends, restart prompts) goes through agents.send_verified, which confirms the agent
started working and records `send_failed` when it did not. Each tick also writes one state sample per registered agent
and, at most once a day across the host, runs the store's maintenance and backup.
<data>/queue/events.jsonl still receives a plain-text copy of every event as an audit trail; it is deprecated and
nothing reads it after the one-time import.
"""
import fcntl
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from importlib import resources

from . import agents as ag
from . import config as cfg
from . import rooms
from . import store
from . import tasks as t

ATTENTION = {"delivered", "partial", "blocked", "stalled", "error"}


def queue_dir(conf):
    path = conf.data / "queue"
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime(conf):
    path = queue_dir(conf) / "agents-state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_runtime(conf, data):
    (queue_dir(conf) / "agents-state.json").write_text(json.dumps(data, indent=2) + "\n")


def paused(conf):
    return (queue_dir(conf) / "paused").exists()


def set_paused(conf, value):
    flag = queue_dir(conf) / "paused"
    if value:
        flag.write_text(ag.now() + "\n")
        return "queue paused: no new assignments (deliveries and stalls are still reported)"
    flag.unlink(missing_ok=True)
    return "queue resumed"


def event(conf, kind, agent=None, task=None, detail=""):
    with store.for_project(conf) as conn:
        record = store.add_event(conn, conf.name, kind, agent=agent, task=task, detail=detail)
    try:
        with open(queue_dir(conf) / "events.jsonl", "a") as handle:
            handle.write(json.dumps({key: record[key] for key in ("time", "kind", "agent", "task", "detail")}) + "\n")
    except OSError:
        pass
    return record


def events(conf, limit=None, since=None, after_id=None, kinds=None, task=None):
    """This project's events oldest first; with limit, the latest limit of them."""
    with store.for_project(conf) as conn:
        return store.events(conn, conf.name, since=since, limit=limit, after_id=after_id, kinds=kinds, task=task)


@contextmanager
def locked(conf):
    with open(queue_dir(conf) / "lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def script(name):
    return str(resources.files("theseus") / "scripts" / name)


def registered_profile(conf, agent):
    from . import profiles
    return agent.get("provider") or agent.get("profile") or profiles.default_name(conf)


def launched(conf, agent):
    """The profile the agent's pane currently runs: its registered profile unless the queue relaunched it."""
    return runtime(conf).get(agent["name"], {}).get("launched") or registered_profile(conf, agent)


def run_script(conf, name, args, agent=None, profile=None):
    from . import profiles
    env = {**conf.environment(), **os.environ}
    if agent is not None:
        env.update(profiles.instance_env(conf, profile or launched(conf, agent), agent["name"]))
    return subprocess.run(["bash", script(name), *args], cwd=conf.root, env=env, capture_output=True, text=True)


def fits(conf, agent, task, info, gate=True):
    """Whether this agent may take this task now, and the reset it needs first (None, 'compact' or 'clear'). A task's
    minCapability is met by the agent's registered profile and, when the task names another profile it is relaunched
    onto, by that profile too; an agent registered with exactly the task's profile always qualifies. gate=False skips
    the capability check (the console uses it to name capability as the reason a task waits)."""
    from . import profiles
    required = task.get("profile")
    registered = registered_profile(conf, agent)
    if required and not profiles.relaunch_compatible(conf, registered, required):
        return False, None
    need = int(task.get("minCapability") or 0)
    if gate and need and required != registered:
        if min(profiles.capability(conf, registered), profiles.capability(conf, required or registered)) < need:
            return False, None
    allowed = agent.get("rooms")
    room = task.get("room")
    if allowed and room and room not in allowed:
        return False, None
    if rooms.enabled(conf) and room:
        configured = rooms.configured_room(conf, agent["name"])
        lists = rooms.section(conf).get("clean_instances") or rooms.section(conf).get("dirty_instances")
        if lists and configured != room:
            return False, None
        if room == "clean" and info.get("tainted"):
            if rooms.taint_mode(conf) == "permanent":
                return False, None
            return True, "clear"
    state = runtime(conf).get(agent["name"], {})
    finished_since = bool(state.get("last_task"))
    return True, ("compact" if state.get("needs_reset", True) or finished_since else None)


def next_task(conf, agent, info, claimed):
    queue = t.Queue(conf)
    order = {task["id"]: n for n, task in enumerate(queue.load()["tasks"])}
    candidates = []
    for task_id in t.ready(queue):
        task = queue.get(task_id)
        if task.get("hold") or task_id in claimed:
            continue
        ok, reset = fits(conf, agent, task, info)
        if ok:
            candidates.append((-(task.get("priority") or 0), order.get(task_id, 0), task, reset))
    candidates.sort(key=lambda item: item[:2])
    return (candidates[0][2], candidates[0][3]) if candidates else (None, None)


def reset(conf, agent, kind):
    args = [agent["pane"], "--clear"] if kind == "clear" else [agent["pane"]]
    result = run_script(conf, "instance-compact.sh", args, agent)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    detail = ""
    if kind == "clear":
        detail = rooms.clear_taint(conf, agent["name"])
    state = runtime(conf)
    state.setdefault(agent["name"], {}).update({"needs_reset": False, "last_task": None})
    save_runtime(conf, state)
    return True, detail


def relaunch(conf, agent, profile):
    """Restart the agent's pane onto another profile, resuming its session, and record the profile it now runs."""
    if agent.get("managed"):
        from . import supervisor
        ok, detail = supervisor.relaunch(conf, agent, profile)
        if not ok:
            return False, detail
        output = detail
    else:
        result = run_script(conf, "instance-restart.sh", [agent["pane"]], agent, profile=profile)
        if result.returncode != 0:
            return False, (result.stderr or result.stdout).strip()
        output = result.stdout.strip()
    registered = registered_profile(conf, agent)
    state = runtime(conf)
    entry = state.setdefault(agent["name"], {})
    if profile == registered:
        entry.pop("launched", None)
    else:
        entry["launched"] = profile
    save_runtime(conf, state)
    where = f"back onto its profile {profile}" if profile == registered else f"onto profile {profile}"
    return True, f"{where}: {output}"


def assign_and_send(conf, agent, task):
    from . import integrate
    from . import profiles
    provider = agent.get("provider") or profiles.default_name(conf)
    suffix = task["id"][:24]
    name = f"{agent['name']}-{suffix}"
    worktree, base, _ = integrate.assign(conf, task["id"], name, provider, "persistent-instance",
                                         owner=f"{provider}:{agent['name']}:{name}", instance=agent["name"])
    record = (f"Owned paths: {', '.join(task['ownedPaths'])}. Objective: {task['objective']} "
              f"Contract: {task.get('generalContract') or 'not recorded'} Acceptance: {' | '.join(task['acceptance'])}.")
    record += f" Log directory (handoff.md, changed.sha256): {worktree}/{cfg.WORKER_LOG}."
    if task.get("assignmentNote"):
        record += f" Coordinator notes: {task['assignmentNote']}"
    record += rooms.message(conf, task)
    deadline = conf.section("queue").get("deadline") or ""
    text = task_message(conf, task["id"], worktree, record, deadline, launched(conf, agent))
    ok, detail = ag.send_verified(conf, agent, text, task["id"])
    if not ok:
        return False, f"task message not submitted: {detail}"
    state = runtime(conf)
    state.setdefault(agent["name"], {}).update({"last_task": task["id"], "assigned": ag.now(), "nudged": None,
                                                "thrashing": None})
    save_runtime(conf, state)
    return True, str(worktree)


def task_message(conf, task_id, worktree, record, deadline="", profile=None):
    """The message that hands a persistent agent its task: the standing brief to re-read, the worktree, the task record,
    an optional deadline and how to report back."""
    from . import profiles
    brief = os.environ.get("THESEUS_BRIEF") or str(conf.root / (conf.section("instances").get("brief") or ""))
    coordinator = os.environ.get("THESEUS_COORDINATOR") or str(conf.section("instances").get("coordinator") or "")
    try:
        messaging = profiles.instance_env(conf, profile)["THESEUS_MESSAGING"] == "1"
    except profiles.ProfileError:
        messaging = True
    message = f"Read and follow {brief} first. New task {task_id} in worktree {worktree}. {record}"
    if deadline:
        message += f" Deadline: {deadline}; if the work cannot be finished by then, deliver a partial handoff with what works."
    message += " Run only the checks named here. Finish with the handoff and a done line"
    message += f" sent to the coordinator session {coordinator} with SendMessage." if coordinator and messaging else "."
    return message


def nudge(conf, agent, task, info=None):
    """Type the continue nudge (queue.nudge) through the verified send; when the pane's context hint is at least
    gateway.clear_tokens, clear the context and send a restart prompt instead."""
    from . import gateway
    text = conf.section("queue").get("nudge") or (
        "Continue the current task from the state of your worktree; if an error stopped you, retry the last step, "
        "and finish with the handoff and your done line.")
    limit = gateway.settings(conf)["clear_tokens"]
    tokens = (info or {}).get("contextTokens") or 0
    fresh = bool(limit and tokens >= limit)
    ok, _ = gateway.recover(conf, agent, task, fresh, f"an idle stop at {tokens} tokens of context", text=text)
    state = runtime(conf)
    state.setdefault(agent["name"], {})["nudged"] = task["id"]
    save_runtime(conf, state)
    return ok


def tick(conf, assign=True):
    """One pass over every registered agent. Returns the events it recorded."""
    recorded = []
    claimed = set()
    samples = []
    with locked(conf):
        from . import gateway
        from . import pools
        recorded.extend(supervise(conf))
        registered = ag.agents(conf)
        pool_names = {a["name"]: pools.pool_of(conf, a.get("provider")) for a in registered}
        for pool in set(filter(None, pool_names.values())):
            held = {}
            for a in registered:
                if pool_names[a["name"]] == pool:
                    current = ag.current_task(conf, a["name"])
                    if current:
                        held[a["name"]] = current["id"]
            pools.sync(conf, pool, held)
        waiting_on_pool = set()
        for agent in registered:
            info = ag.state(conf, agent)
            name, task_id = agent["name"], info["task"]
            if info["state"] == "delivered":
                task = t.Queue(conf).get(task_id)
                t.deliver(t.Queue(conf), task_id, ag.handoff_path(task))
                state = runtime(conf)
                state.setdefault(name, {})["needs_reset"] = True
                save_runtime(conf, state)
                recorded.append(events(conf, 1)[-1])
                info = ag.state(conf, agent)
            elif info["state"] == "gateway_error":
                recorded.extend(gateway.handle(conf, agent, info))
            elif info["state"] == "stalled":
                if gateway.paused_until(gateway.pool_key(conf, launched(conf, agent))):
                    pass
                elif runtime(conf).get(name, {}).get("nudged") != task_id:
                    if nudge(conf, agent, t.Queue(conf).get(task_id), info):
                        recorded.append(event(conf, "nudged", name, task_id, f"idle {info['idleMinutes']} min without a handoff"))
                elif not events(conf, 1, kinds=["stalled"], task=task_id):
                    recorded.append(event(conf, "stalled", name, task_id, f"still idle after a nudge ({info['idleMinutes']} min)"))
            elif info["state"] == "thrashing":
                state = runtime(conf)
                if state.get(name, {}).get("thrashing") != task_id:
                    state.setdefault(name, {}).update({"thrashing": task_id, "nudged": task_id})
                    save_runtime(conf, state)
                    if agent.get("managed"):
                        from . import supervisor
                        try:
                            ok, detail = supervisor.thrashing_restart(conf, agent, task_id)
                        except Exception as error:
                            ok, detail = False, str(error)
                        recorded.append(event(conf, "restarted" if ok else "error", name, task_id, detail))
                    else:
                        recorded.append(event(conf, "stalled", name, task_id, f"thrashing: the pane shows {info['stall']!r}; not nudged"))
            elif info["state"] in ("blocked", "failed", "waiting") and info["task"]:
                kind = f"{info['state']}:{info.get('waiting') or ''}"
                state = runtime(conf)
                if state.get(name, {}).get("attention") != f"{task_id}:{kind}":
                    state.setdefault(name, {})["attention"] = f"{task_id}:{kind}"
                    save_runtime(conf, state)
                    detail = (f"waiting for {info['waiting']}" if info["state"] == "waiting"
                              else f"the agent is {info['state']}; theseus agents ls")
                    recorded.append(event(conf, "stalled", name, task_id, detail))
            if info.get("busy") and info["state"] in ("working", "busy"):
                gateway.observe(conf, agent, info)
            samples.append((name, info["state"], info["task"]))
            if info["state"] != "free" or not assign or paused(conf):
                continue
            if gateway.paused_until(gateway.pool_key(conf, launched(conf, agent))):
                continue
            from . import profiles as pf
            used, capacity = t.capacity_use(conf).get(agent.get("provider") or pf.default_name(conf), (0, None))
            if capacity is not None and used >= int(capacity):
                continue
            task, needed = next_task(conf, agent, info, claimed)
            if task is None:
                continue
            pool = pool_names.get(name)
            if pool:
                allowed, why = pools.acquire(conf, pool, name, task["id"])
                if not allowed:
                    waiting_on_pool.add(pool)
                    state = runtime(conf)
                    state.setdefault(name, {})["pool_wait"] = why
                    save_runtime(conf, state)
                    continue
            claimed.add(task["id"])
            ok = False
            try:
                target = task.get("profile") or registered_profile(conf, agent)
                if target != launched(conf, agent):
                    try:
                        ok, detail = relaunch(conf, agent, target)
                    except Exception as error:
                        ok, detail = False, str(error)
                    if not ok:
                        recorded.append(event(conf, "error", name, task["id"], f"relaunch onto {target} failed: {detail}"))
                        continue
                    recorded.append(event(conf, "relaunched", name, task["id"], detail))
                if needed:
                    try:
                        ok, detail = reset(conf, agent, needed)
                    except Exception as error:
                        ok, detail = False, str(error)
                    if not ok:
                        recorded.append(event(conf, "error", name, task["id"], f"{needed} failed: {detail}"))
                        continue
                    recorded.append(event(conf, "cleared" if needed == "clear" else "compacted", name, None, detail))
                try:
                    ok, detail = assign_and_send(conf, agent, task)
                except Exception as error:
                    ok, detail = False, str(error)
                recorded.append(events(conf, 1)[-1] if ok else event(conf, "error", name, task["id"], detail))
            finally:
                if pool and not ok:
                    pools.release(conf.name, pool, name)
        for pool in set(filter(None, pool_names.values())) - waiting_on_pool:
            pools.drop_claim(conf, pool)
        if samples:
            with store.for_project(conf) as conn:
                store.add_samples(conn, conf.name, samples)
    housekeeping()
    return recorded


def supervise(conf):
    """For a project whose agents theseus supervises: reconcile them (never waiting for a slow start), refresh their
    session ids and send the restart prompt an agent restarted fresh with a task is owed. Returns the events."""
    from . import supervisor
    if not supervisor.managed(conf):
        return []
    before = events(conf, 1)
    start = before[-1]["id"] if before else 0
    try:
        supervisor.reconcile(conf)
        supervisor.refresh_sessions(conf)
    except Exception as error:
        event(conf, "error", None, None, f"supervisor: {error}")
    state = runtime(conf)
    for name, entry in list(state.items()):
        task_id = entry.get("restart_prompt") if isinstance(entry, dict) else None
        if not task_id:
            continue
        rec = supervisor.record(conf, name)
        if not rec or rec.get("phase") != "ready":
            continue
        task = t.Queue(conf).index().get(task_id)
        entry.pop("restart_prompt", None)
        save_runtime(conf, state)
        if not task or task.get("status") != "in_progress":
            continue
        from . import gateway
        ok, detail = ag.send_verified(conf, ag.entry(rec), gateway.restart_prompt(conf, task, "a restart on a fresh session"),
                                      task_id)
        event(conf, "nudged" if ok else "error", name, task_id,
              "restart prompt after a fresh restart" + ("" if ok else f" not submitted: {detail}"))
    return events(conf, after_id=start)


def wake_times(conf, now=None):
    """Epochs strictly after now when something in this project becomes due: gateway resends and supervised agent
    restarts after their backoff."""
    from . import supervisor
    now = time.time() if now is None else now
    found = []
    with store.for_project(conf) as conn:
        found += [row["next_epoch"] for row in conn.execute(
            "SELECT next_epoch FROM gateway_retries WHERE project = ? AND phase = 'waiting' AND next_epoch IS NOT NULL",
            (conf.name,))]
    try:
        found += supervisor.wake_times(conf, now)
    except Exception:
        pass
    return sorted(epoch for epoch in found if epoch > now)


def housekeeping():
    """The store's daily maintenance and backup; a failure is recorded in the store and never stops the queue."""
    try:
        with store.for_host() as conn:
            for line in store.daily(conn):
                if "failed" in line:
                    print(f"theseus store: {line}", file=sys.stderr)
    except Exception as error:
        print(f"theseus store: housekeeping failed: {error}", file=sys.stderr)


def run(conf, interval=30, once=False):
    started = time.time()
    while True:
        for record in tick(conf):
            print(f"{record['time']} {record['kind']:<10} {record['agent'] or '-':<18} {record['task'] or '-'} {record['detail']}")
            sys.stdout.flush()
        if once:
            return
        pause = next_sleep(conf, interval, time.time())
        heartbeat(conf, "run", started, pause)
        time.sleep(pause)


def still_open(conf, record):
    """A delivery the coordinator already merged (theseus merge records the delivery itself) needs no wake-up."""
    if record["kind"] not in ("delivered", "partial", "blocked") or not record.get("task"):
        return True
    task = t.Queue(conf).index().get(record["task"])
    return bool(task) and task.get("status") == "verification"


WAKE_SLACK = 0.5


def heartbeat(conf, command, started, sleep_seconds, now=None):
    """Record the queue loop of this process (pid, command, last tick, next wake) for the console."""
    now = time.time() if now is None else now
    beat = {"pid": os.getpid(), "command": command, "started": started, "tick": now, "next": now + sleep_seconds}
    try:
        (queue_dir(conf) / "watcher.json").write_text(json.dumps(beat) + "\n")
    except OSError:
        pass


def next_sleep(conf, interval, now):
    """Seconds until the next tick: the interval, or less when a gateway resend or an agent restart falls due
    sooner (plus a little slack so the tick finds it due)."""
    pending = wake_times(conf, now)
    if pending:
        return max(0.0, min(float(interval), pending[0] - now + WAKE_SLACK))
    return float(interval)


def wait(conf, timeout=10800, interval=30, drive=True, clock=time.time, sleep=time.sleep):
    """Block until an event that needs the coordinator appears after the call started; print it and return 0.
    With drive, also run ticks meanwhile so the queue makes progress without a separate runner; it sleeps until the
    next interval or the earliest pending gateway resend or agent restart, whichever comes first."""
    latest = events(conf, 1)
    start = latest[-1]["id"] if latest else 0
    started = clock()
    deadline = started + timeout
    while clock() < deadline:
        if drive:
            tick(conf)
        new = events(conf, after_id=start)
        hits = [e for e in new if e["kind"] in ATTENTION and still_open(conf, e)]
        if hits:
            for record in new:
                print(f"{record['time']} {record['kind']:<10} {record['agent'] or '-':<18} {record['task'] or '-'} {record['detail']}")
            return 0
        now = clock()
        pause = min(next_sleep(conf, interval, now) if drive else float(interval), max(0.0, deadline - now))
        if drive:
            heartbeat(conf, "wait", started, pause, now)
        sleep(pause)
    print(f"queue wait: nothing needed the coordinator within {timeout}s")
    return 1


def summary(conf):
    queue = t.Queue(conf)
    open_tasks = queue.load()["tasks"]
    ready = set(t.ready(queue))
    groups = {"ready": [], "held": [], "blocked": [], "in_progress": [], "review": [], "merged": []}
    index = queue.index()
    current = queue.milestone()
    for task in open_tasks:
        status = task["status"]
        if status == "pending":
            if task.get("hold"):
                groups["held"].append(task)
            elif task["id"] in ready:
                groups["ready"].append(task)
            else:
                groups["blocked"].append({**task, "reasons": t.blocking(task, index, current)})
        elif status == "in_progress":
            groups["in_progress"].append(task)
        elif status == "verification":
            groups["review"].append(task)
        elif status == "merged":
            groups["merged"].append(task)
    return groups
