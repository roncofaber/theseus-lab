"""Work queue for registered agents.

A tick looks at every registered agent and acts on its state:
- delivered (handoff present): the task is marked delivered and a `delivered` event asks the coordinator to review and
  merge it; the agent is reset before its next task;
- stalled (idle past instances.idle_minutes with no handoff): one `continue` nudge, then a `stalled` event;
- thrashing (the pane shows a stall pattern of its profile, such as Claude Code's autocompact thrashing notice): one
  `stalled` event at once and no nudge, since typing more into that context only refills it;
- gateway_error (an idle pane shows a gateway error of its profile): retried with backoff, a fresh context when needed
  and a host-wide circuit breaker (theseus.gateway); a `stalled` event once the resends run out, an `error` event at
  once for a fatal error; while the agent's profile is on fallback (theseus.health) its task is released instead, with
  the worktree kept, for an agent of the fallback to take;
- free (enabled, not stopped by `theseus agents down`, no open task, its gateway pool not paused): if a ready task fits its room, the agent is reset (a full
  /clear when it carries dirty context into clean work, a /compact otherwise), the task is assigned on a fresh
  worktree, and its message is sent. A task message that cannot be typed (the pane is gone) rolls the assignment back:
  the task is released and its untouched worktree removed. A task whose assignment fails gets an `error` event and is
  skipped for queue.assign_backoff_seconds (default 300) while the queue keeps driving other work.
A task with "minCapability" goes only to agents whose profile capability ([workers.<name>] capability, default 1) is at
least that, unless they are registered with exactly the task's profile.
A task with a "profile" goes only to agents registered with that profile, or to agents whose profile differs from it only in
MCP servers or environment: those are relaunched onto it in their pane first (session resumed, a `relaunched` event),
and back onto their registered profile before their next task that does not need it. The profile an agent currently
runs is kept in the runtime state (`launched`).
Each tick first runs the host's model health checks (theseus.health: probes that are due, switches onto or back from a
fallback, one `fallback` event per switch in each project that uses the profile). Agents whose pool holds their turns
(theseus.pools.turn_plan: the newest working agents beyond a cut working cap or the pool's token budget) get nothing
typed into them and the pool starts no new task meanwhile.
Tasks with "hold": true are never assigned by the queue; `theseus queue pause` stops all assignment. Events go to the
host store (theseus.store); `theseus queue wait` blocks until one needs the coordinator. Every message the queue types
(task messages, nudges, gateway resends, restart prompts) goes through agents.send_verified, which confirms the agent
started working and records `send_failed` when it did not. A tick types its messages at once and verifies them together
in a bounded pool of threads within queue.send_budget_seconds, so agents that do not respond cost one verification
window per tick, not one each; a send still unresolved is finished by the next tick.
An agent restarted on a fresh context while it holds a task (a fresh supervised restart, `theseus agents restart
--fresh --with-task`, a gateway or idle-stop recovery that clears the context) is owed exactly one task-aware restart
prompt: until it is confirmed received nothing else is typed into the agent (no nudge, gateway resend or new task),
and after [gateway] attempts unconfirmed tries a `stalled` event wakes the coordinator instead.
Each tick also writes one state sample per registered agent and, at most once a day across the host, runs the store's
maintenance and backup.
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
from . import upgrade

ATTENTION = {"delivered", "partial", "blocked", "stalled", "error", "fallback"}


def queue_dir(conf):
    path = conf.data / "queue"
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime(conf):
    path = queue_dir(conf) / "agents-state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_runtime(conf, data):
    cfg.write_atomic(queue_dir(conf) / "agents-state.json", json.dumps(data, indent=2) + "\n")


def assign_failures(conf, now=None):
    """Task ID -> epoch until which the queue skips it after a failed assignment (<data>/queue/assign-failures.json)."""
    now = time.time() if now is None else now
    try:
        failures = json.loads((queue_dir(conf) / "assign-failures.json").read_text())
    except (OSError, ValueError):
        return {}
    return {task_id: until for task_id, until in failures.items() if float(until) > now}


def back_off(conf, task_id, now=None):
    now = time.time() if now is None else now
    failures = assign_failures(conf, now)
    failures[task_id] = now + float(conf.section("queue").get("assign_backoff_seconds", 300))
    cfg.write_atomic(queue_dir(conf) / "assign-failures.json", json.dumps(failures, indent=2) + "\n")


def paused(conf):
    return (queue_dir(conf) / "paused").exists()


def set_paused(conf, value):
    flag = queue_dir(conf) / "paused"
    if value:
        flag.write_text(ag.now() + "\n")
        return "queue paused: no new assignments (deliveries and stalls are still reported)"
    flag.unlink(missing_ok=True)
    return "queue resumed"


def event(conf, kind, agent=None, task=None, detail="", agent_id=None):
    with store.for_project(conf) as conn:
        record = store.add_event(conn, conf.name, kind, agent=agent, task=task, detail=detail, agent_id=agent_id)
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
def locked(conf, announce=False):
    """Hold the project's queue lock, which serializes queue ticks with the commands that change tasks.json, the
    agents or the main checkout. announce says on stderr when the lock is busy before waiting for it."""
    with open(queue_dir(conf) / "lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if announce:
                print("theseus: waiting for the queue lock (a queue tick or another command holds it)", file=sys.stderr)
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
    from . import health
    from . import profiles
    required = health.substitute(task.get("profile"))
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
    skipped = assign_failures(conf)
    for task_id in t.ready(queue):
        task = queue.get(task_id)
        if task.get("hold") or task_id in claimed or task_id in skipped:
            continue
        ok, reset = fits(conf, agent, task, info)
        if ok:
            candidates.append((-(task.get("priority") or 0), order.get(task_id, 0), task, reset))
    candidates.sort(key=lambda item: item[:2])
    return (candidates[0][2], candidates[0][3]) if candidates else (None, None)


def reset(conf, agent, kind):
    prompt = ag.pane_prompt(conf, agent, launched(conf, agent))
    if prompt:
        return False, f"not typed: the pane shows a prompt that needs a human ({prompt[:80]})"
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
    suffix = task["id"][:24].rstrip("-.")
    name = f"{agent['name']}-{suffix}"
    worktree, base, _ = integrate.assign(conf, task["id"], name, provider, "persistent-instance",
                                         owner=f"{provider}:{agent['name']}:{name}", instance=agent["name"],
                                         owner_id=agent.get("id"))
    try:
        record = (f"Owned paths: {', '.join(task['ownedPaths'])}. Objective: {task['objective']} "
                  f"Contract: {task.get('generalContract') or 'not recorded'} Acceptance: {' | '.join(task['acceptance'])}.")
        record += f" Log directory (handoff.md, changed.sha256): {worktree}/{cfg.WORKER_LOG}."
        if task.get("assignmentNote"):
            record += f" Coordinator notes: {task['assignmentNote']}"
        record += kept_work_note(task)
        record += rooms.message(conf, task)
        deadline = conf.section("queue").get("deadline") or ""
        text = task_message(conf, task["id"], worktree, record, deadline, launched(conf, agent))
        ok, detail = ag.send_verified(conf, agent, text, task["id"], kind="task")
    except Exception as error:
        ok, detail = False, str(error)
    if not ok:
        return False, roll_back(conf, task["id"], name, worktree, base, f"task message not submitted: {detail}")
    state = runtime(conf)
    state.setdefault(agent["name"], {}).update({"last_task": task["id"], "assigned": ag.now(), "nudged": None,
                                                "thrashing": None})
    save_runtime(conf, state)
    return True, str(worktree)


FALLBACK_RELEASE = "fallback:"


def kept_work_note(task):
    """A pointer to the worktree of an attempt released because its profile went down, when it still exists."""
    from pathlib import Path
    last = (task.get("previousAttempts") or [{}])[-1]
    kept = last.get("worktree")
    if not str(last.get("reason") or "").startswith(FALLBACK_RELEASE) or not kept or not Path(kept).is_dir():
        return ""
    return (f" A previous attempt stopped by a model outage left its work in {kept}; check its git status and diff and "
            f"carry over what is useful before redoing it.")


def release_for_fallback(conf, agent, task_id, profile, fallback):
    """Release the task of an agent stuck on a gateway error while its profile is on fallback: the worktree stays
    (the next attempt is pointed at it), and the retry episode and any owed prompt are dropped."""
    reason = f"{FALLBACK_RELEASE} profile {profile} is down; the task moves to an agent of {fallback}"
    start = last_event_id(conf)
    try:
        t.release(t.Queue(conf), task_id, reason)
    except t.TaskError as error:
        return [event(conf, "error", agent["name"], task_id, f"release after {profile} went down failed: {error}")]
    with store.for_project(conf) as conn:
        store.drop_gateway_retry(conn, conf.name, agent["name"])
    drop_owed(conf, agent["name"])
    return events(conf, after_id=start)


def roll_back(conf, task_id, name, worktree, base, reason):
    """Undo an assignment whose task message never reached the agent: release the task, remove its worktree and
    work/<name> branch when they hold nothing beyond what assignment wrote. Returns the detail for the error event."""
    from . import gitutil as g
    from . import integrate
    t.release(t.Queue(conf), task_id, reason)
    status = g.git(worktree, "status", "--porcelain", check=False)
    changed = [line for line in status.stdout.splitlines()
               if line[3:].rstrip("/") not in (cfg.WORKER_LOG, cfg.LOCAL_NAME)]
    head = g.out(worktree, "rev-parse", "HEAD", check=False)
    if status.returncode == 0 and not changed and head.startswith(base[:12]):
        integrate.remove_worktree(conf, worktree, name)
        what = "its worktree removed"
    else:
        what = f"its worktree kept at {worktree} (it holds changes)"
    return f"{reason}; {task_id} released, {what}"


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
    with locked(conf), ag.batched(send_budget(conf)) as sends:
        try:
            from . import gateway
            from . import health
            from . import pools
            recorded.extend(check_health(conf))
            active = health.fallbacks()
            recorded.extend(supervise(conf))
            registered = ag.agents(conf)
            recorded.extend(resume_sends(conf, registered, sends))
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
            working = {}
            for agent in registered:
                press_enter(sends, sends.items, wait=False)
                info = ag.state(conf, agent)
                name, task_id = agent["name"], info["task"]
                if info["state"] == "delivered":
                    task = t.Queue(conf).get(task_id)
                    t.deliver(t.Queue(conf), task_id, ag.handoff_path(task))
                    state = runtime(conf)
                    state.setdefault(name, {})["needs_reset"] = True
                    save_runtime(conf, state)
                    recorded.append(events(conf, 1)[-1])
                    drop_owed(conf, name)
                    info = ag.state(conf, agent)
                pool_name = gateway.pool_key(conf, launched(conf, agent))
                if info["task"]:
                    working[name] = info
                    recorded.extend(note_turn(conf, name, info["task"], pools.turn_held(conf.name, pool_name, name)))
                elif runtime(conf).get(name, {}).get("turn_held"):
                    state = runtime(conf)
                    state[name].pop("turn_held", None)
                    save_runtime(conf, state)
                if name in sends.agents() or owed(conf, name):
                    if name not in sends.agents() and not pools.turn_held(conf.name, pool_name, name):
                        recorded.extend(try_owed(conf, agent, info))
                    samples.append((name, info["state"], info["task"]))
                    continue
                fallback = active.get(launched(conf, agent))
                if info["state"] == "gateway_error" and fallback and agent.get("managed"):
                    recorded.extend(release_for_fallback(conf, agent, task_id, launched(conf, agent), fallback))
                    working.pop(name, None)
                    samples.append((name, info["state"], None))
                    continue
                if info["state"] == "gateway_error":
                    recorded.extend(gateway.handle(conf, agent, info))
                elif info["state"] == "stalled":
                    if gateway.paused_until(pool_name) or pools.turn_held(conf.name, pool_name, name):
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
                if agent.get("managed") and agent.get("desired") in ("down", "retire"):
                    continue
                if gateway.paused_until(pool_name):
                    continue
                if active.get(registered_profile(conf, agent)):
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
                    allowed, why = pools.may_add_turn(pool, (info.get("context") or {}).get("tokens"))
                    if not allowed:
                        state = runtime(conf)
                        state.setdefault(name, {})["pool_wait"] = why
                        save_runtime(conf, state)
                        continue
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
                    target = health.substitute(task.get("profile"), active) or registered_profile(conf, agent)
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
                    from . import integrate
                    held = False
                    try:
                        ok, detail = assign_and_send(conf, agent, task)
                    except Exception as error:
                        ok, detail, held = False, str(error), isinstance(error, integrate.DependencyHeld)
                    if not ok:
                        back_off(conf, task["id"])
                    recorded.append(events(conf, 1)[-1] if ok else
                                    event(conf, "held" if held else "error", name, task["id"], detail))
                finally:
                    if pool and not ok:
                        pools.release(conf.name, pool, name)
            recorded.extend(note_holds(conf))
            record_turns(conf, pool_names, working)
        finally:
            recorded.extend(verify_sends(conf, sends))
        for pool in set(filter(None, pool_names.values())) - waiting_on_pool:
            pools.drop_claim(conf, pool)
        if samples:
            with store.for_project(conf) as conn:
                store.add_samples(conn, conf.name, samples)
    housekeeping()
    return recorded


def check_health(conf):
    """The host's model health checks (probes due, fallback switches) and this project's `fallback` events. A failure
    is reported on stderr and never stops the tick."""
    from . import health
    try:
        health.host_tick()
        return health.notify(conf)
    except Exception as error:
        print(f"theseus health: {error}", file=sys.stderr)
        return []


def note_turn(conf, name, task_id, held):
    """A turn_held or turn_resumed event (neither wakes the coordinator) when the pool starts or stops holding the
    agent's turns."""
    state = runtime(conf)
    entry = state.setdefault(name, {})
    if bool(entry.get("turn_held")) == bool(held):
        return []
    entry["turn_held"] = bool(held)
    save_runtime(conf, state)
    if held:
        return [event(conf, "turn_held", name, task_id, "the pool is over its working cap or token budget; nothing is typed "
                                                        "into this agent until older tasks make room")]
    return [event(conf, "turn_resumed", name, task_id, "the pool has room again")]


def record_turns(conf, pool_names, working):
    """This project's working agents in each pool's turn record: when their task was assigned and their context."""
    from . import pools
    queue = t.Queue(conf)
    index = queue.index()
    for pool in set(filter(None, pool_names.values())):
        entries = {}
        for name, info in working.items():
            if pool_names.get(name) != pool:
                continue
            assigned = ((index.get(info["task"]) or {}).get("execution") or {}).get("assignedAt")
            entries[name] = {"since": store.epoch_of(assigned) if assigned else None,
                             "tokens": (info.get("context") or {}).get("tokens") or info.get("contextTokens")}
        try:
            pools.sync_turns(conf, pool, entries)
        except Exception as error:
            print(f"theseus pools: {error}", file=sys.stderr)


def note_holds(conf):
    """Record a held event, which does not wake the coordinator, when a pending task starts waiting for a dependency's
    merge to reach the assignment base."""
    queue = t.Queue(conf)
    index = queue.index()
    reach = t.merge_reach(conf)
    recorded = []
    for task in queue.load()["tasks"]:
        detail = "; ".join(t.unreached(task, index, reach)) if task["status"] == "pending" else ""
        if not detail:
            continue
        last = events(conf, 1, kinds=["held"], task=task["id"])
        if not last or last[-1]["detail"] != detail:
            recorded.append(event(conf, "held", None, task["id"], detail))
    return recorded


def supervise(conf):
    """For a project whose agents theseus supervises: reconcile them (never waiting for a slow start) and refresh their
    session ids. Returns the events."""
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
    return events(conf, after_id=start)


OWED_WAKE = 5.0
OWED_WAKE_FOR = 900
SEND_WORKERS = 16


def send_budget(conf):
    """Seconds one tick may spend waiting for its verified sends together (queue.send_budget_seconds)."""
    value = os.environ.get("THESEUS_SEND_BUDGET") or conf.section("queue").get("send_budget_seconds")
    return 25.0 if value in (None, "") else float(value)


def last_event_id(conf):
    latest = events(conf, 1)
    return latest[-1]["id"] if latest else 0


def owed(conf, name):
    """The restart prompt the agent is owed ({"task", "reason", "text", "attempts"}), or None."""
    entry = runtime(conf).get(name, {}).get("restart_prompt")
    if isinstance(entry, str):
        return {"task": entry, "reason": "a restart on a fresh session", "attempts": 0}
    return entry or None


def owe_prompt(conf, name, task_id, reason, text=None):
    """Owe the agent one task-aware restart prompt (text, or one built from the task). Until it is confirmed received,
    nothing else is typed into the agent: no nudge, gateway resend or new task; a gateway resend already scheduled is
    called off."""
    state = runtime(conf)
    state.setdefault(name, {})["restart_prompt"] = {"task": task_id, "reason": reason, "text": text, "attempts": 0,
                                                    "since": time.time()}
    save_runtime(conf, state)
    with store.for_project(conf) as conn:
        row = store.gateway_retry(conn, conf.name, name)
        if row and row["phase"] == "waiting":
            store.put_gateway_retry(conn, conf.name, name, phase="resent", resent_epoch=time.time())


def drop_owed(conf, name):
    state = runtime(conf)
    if state.get(name, {}).pop("restart_prompt", None) is not None:
        save_runtime(conf, state)


def deliver_owed(conf, agent):
    """Type the agent's owed restart prompt. Returns (status, detail): sent, typed (verified later in this tick),
    failed (retried by a later tick), dropped (the task is no longer the agent's) or none."""
    from . import gateway
    name = agent["name"]
    entry = owed(conf, name)
    if not entry:
        return "none", ""
    task = t.Queue(conf).index().get(entry["task"])
    current = ag.current_task(conf, name)
    if not task or task.get("status") != "in_progress" or not current or current["id"] != task["id"]:
        drop_owed(conf, name)
        return "dropped", f"task {entry['task']} is no longer assigned to {name}"
    reason = entry.get("reason") or "a restart"
    text = entry.get("text") or gateway.restart_prompt(conf, task, reason)
    ok, detail = ag.send_verified(conf, agent, text, task["id"], kind="prompt", reason=reason)
    if ok and detail == ag.DEFERRED:
        return "typed", detail
    if ok:
        prompt_confirmed(conf, name, task["id"], reason)
        return "sent", detail
    prompt_failed(conf, name, task["id"], detail)
    return "failed", detail


def prompt_confirmed(conf, name, task_id, reason):
    drop_owed(conf, name)
    return event(conf, "nudged", name, task_id, f"restart prompt after {reason}")


def prompt_failed(conf, name, task_id, detail):
    """Count a restart prompt that was not confirmed; after [gateway] attempts of them, wake the coordinator instead of
    ever falling back to a plain continue."""
    from . import gateway
    state = runtime(conf)
    entry = state.get(name, {}).get("restart_prompt")
    if not isinstance(entry, dict):
        return []
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
    entry["tried"] = time.time()
    limit = gateway.settings(conf)["attempts"]
    if entry["attempts"] < limit:
        save_runtime(conf, state)
        return []
    state[name].pop("restart_prompt", None)
    state[name]["nudged"] = task_id
    save_runtime(conf, state)
    return [event(conf, "stalled", name, task_id, f"restart prompt not confirmed after {limit} attempts: {detail}")]


def try_owed(conf, agent, info):
    """Send the owed restart prompt once the agent is ready, idle and not still verifying another send. After an
    unconfirmed try, the agent seen busy or its worktree changed counts as the prompt received: nothing else was typed
    into it."""
    if agent.get("managed") and agent.get("phase") != "ready":
        return []
    if info["state"] in ("offline", "starting", "blocked", "failed"):
        return []
    entry = owed(conf, agent["name"]) or {}
    if entry.get("attempts") and (info.get("busy") or (info.get("lastChange") or 0) > (entry.get("tried") or 0)):
        return [prompt_confirmed(conf, agent["name"], entry["task"], entry.get("reason") or "a restart")]
    if info.get("busy"):
        return []
    start = last_event_id(conf)
    deliver_owed(conf, agent)
    return events(conf, after_id=start)


def settle_send(conf, item, result, detail):
    """Act on a verified send of a tick once it resolved: a failure records send_failed and, for a task message, the
    error event; a restart prompt is confirmed or counted; a failed clear command drops the prompt that was to follow."""
    name, task, kind = item["agent"], item.get("task"), item.get("kind")
    if result == "ok":
        return [prompt_confirmed(conf, name, task, item.get("reason") or "a restart")] if kind == "prompt" else []
    found = [ag.record_send_failed(conf, name, task, item["text"], detail)] if item.get("record", True) else []
    if kind == "task":
        found.append(event(conf, "error", name, task, f"task message not submitted: {detail}"))
    elif kind == "prompt":
        found += prompt_failed(conf, name, task, detail)
    elif kind == "clear":
        drop_owed(conf, name)
    return found


def resume_sends(conf, registered, sends):
    """Pick up the sends an earlier tick typed but could not finish verifying: resolve them now, or (after one more
    Enter for a text still in the input line) verify them in this tick."""
    by_name = {agent["name"]: agent for agent in registered}
    state = runtime(conf)
    carried = {name: entry.pop("verify") for name, entry in state.items() if isinstance(entry, dict) and entry.get("verify")}
    if not carried:
        return []
    save_runtime(conf, state)
    found = []
    for name, pending in carried.items():
        agent = by_name.get(name)
        if agent is None:
            continue
        try:
            ctx = ag.send_context(conf, agent, pending.get("profile"))
            result, detail = ag.advance(ctx, pending)
        except Exception as error:
            result, detail = "failed", f"verification failed: {error}"
        if result == "wait":
            sends.items.append({**pending, "ctx": ctx, "entry": agent})
        else:
            found += settle_send(conf, pending, result, detail)
    return found


def poll_send(item, sends):
    ctx = item["ctx"]
    while True:
        try:
            result, detail = ag.advance(ctx, item)
        except Exception as error:
            return "failed", f"verification failed: {error}"
        if result != "wait":
            return result, detail
        left = sends.remaining()
        if left <= 0:
            return "wait", None
        ag.clock.sleep(min(ctx["poll"], left))


def press_enter(sends, items, wait=True):
    """Press Enter in every pane typed into, after one shared pause (wait) or only where the pause is over, and start
    their verification windows."""
    due = [item for item in items if item.get("enter_at") and (wait or item["enter_at"] <= ag.clock.monotonic())]
    if not due:
        return
    ag.clock.sleep(max(0.0, max(item["enter_at"] for item in due) - ag.clock.monotonic()))
    for item in due:
        ctx = item.pop("enter_at") and item["ctx"]
        try:
            ag.on(ctx["server"], "send-keys", "-t", ctx["pane"], "Enter")
        except Exception:
            pass
        item["deadline"] = ag.clock.time() + ctx["timeout"]


def verify_sends(conf, sends):
    """Verify every send this tick typed at once, in a bounded pool of threads that only read panes and press Enter,
    within the tick's send budget. Results are applied here, on the queue's own thread; a restart prompt owed after a
    confirmed clear command is typed and verified in a second round. Sends still unresolved when the budget runs out
    are carried to the next tick. Returns the events."""
    from concurrent.futures import ThreadPoolExecutor
    found = []
    sends.start()
    while True:
        waiting = [item for item in sends.items if "result" not in item]
        if not waiting:
            break
        press_enter(sends, waiting)
        with ThreadPoolExecutor(max_workers=min(SEND_WORKERS, len(waiting))) as pool:
            outcomes = list(pool.map(lambda item: poll_send(item, sends), waiting))
        follow = []
        for item, (result, detail) in zip(waiting, outcomes):
            if result == "wait":
                item["result"] = None
                continue
            item["result"] = result
            found += settle_send(conf, item, result, detail)
            if result == "ok" and item.get("kind") == "clear" and owed(conf, item["agent"]):
                follow.append(item)
        typed = 0
        for item in follow:
            ctx = item["ctx"]
            if sends.remaining() <= 0 or ag.pane_busy(conf, ctx["pane"], ctx["profile"], server=ctx["server"]):
                continue
            before, start = len(sends.items), last_event_id(conf)
            deliver_owed(conf, item["entry"])
            found += events(conf, after_id=start)
            typed += len(sends.items) - before
        if not typed:
            break
    leftovers = [item for item in sends.items if item.get("result") is None]
    if leftovers:
        state = runtime(conf)
        for item in leftovers:
            pending = {key: value for key, value in item.items() if key not in ("ctx", "entry", "result")}
            state.setdefault(item["agent"], {})["verify"] = {**pending, "carried": True}
        save_runtime(conf, state)
    return found


def wake_times(conf, now=None):
    """Epochs strictly after now when something in this project becomes due: gateway resends, supervised agent
    restarts after their backoff, and soon for a send still being verified or a restart prompt still owed."""
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
    try:
        from . import health, pools
        found += health.wake_times(now) + pools.wake_times(conf, now)
    except Exception:
        pass
    for entry in runtime(conf).values():
        if not isinstance(entry, dict):
            continue
        prompt = entry.get("restart_prompt")
        fresh = isinstance(prompt, dict) and now - float(prompt.get("since") or 0) < OWED_WAKE_FOR
        if entry.get("verify") or fresh:
            found.append(now + OWED_WAKE)
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


def config_signature(conf):
    """Modification times of the files a project's configuration is read from: theseus.toml, theseus.local.toml and
    the host files."""
    from . import hostconfig
    files = (conf.root / cfg.CONFIG_NAME, conf.root / cfg.LOCAL_NAME, hostconfig.path(), hostconfig.pools_path(),
             hostconfig.store_path())
    found = []
    for path in files:
        try:
            found.append((str(path), path.stat().st_mtime_ns))
        except OSError:
            found.append((str(path), None))
    return tuple(found)


def reloaded(conf, seen):
    """(conf, signature): the project configuration read again when one of its files changed since seen, so a
    long-running queue never acts on counts or profiles another command has since rewritten. A file that no longer
    parses keeps the configuration in use."""
    current = config_signature(conf)
    if seen is None or current == seen:
        return conf, current
    try:
        return cfg.load(root=conf.root), current
    except cfg.ConfigError as error:
        print(f"theseus queue: keeping the configuration in use: {error}", file=sys.stderr)
        return conf, current


def run(conf, interval=30, once=False):
    started = time.time()
    seen = None
    code = upgrade.CodeWatch()
    while True:
        code.check()
        conf, seen = reloaded(conf, seen)
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
        cfg.write_atomic(queue_dir(conf) / "watcher.json", json.dumps(beat) + "\n")
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
    seen = None
    code = upgrade.CodeWatch()
    while clock() < deadline:
        code.check()
        conf, seen = reloaded(conf, seen)
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
    reach = t.merge_reach(conf)
    for task in open_tasks:
        status = task["status"]
        if status == "pending":
            if task.get("hold"):
                groups["held"].append(task)
            elif task["id"] in ready:
                groups["ready"].append(task)
            else:
                groups["blocked"].append({**task, "reasons": t.blocking(task, index, current, reach)})
        elif status == "in_progress":
            groups["in_progress"].append(task)
        elif status == "verification":
            groups["review"].append(task)
        elif status == "merged":
            groups["merged"].append(task)
    return groups
