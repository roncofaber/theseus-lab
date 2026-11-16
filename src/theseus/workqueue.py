"""Work queue for registered agents.

A tick looks at every registered agent and acts on its state:
- delivered (handoff present): the task is marked delivered and a `delivered` event asks the coordinator to review and
  merge it; the agent is reset before its next task;
- stalled (idle past instances.idle_minutes with no handoff): one `continue` nudge, then a `stalled` event;
- free (enabled, no open task): if a ready task fits its room, the agent is reset (a full /clear when it carries dirty
  context into clean work, a /compact otherwise), the task is assigned on a fresh worktree, and its message is sent.
Tasks with "hold": true are never assigned by the queue; `theseus queue pause` stops all assignment. Events go to
<data>/queue/events.jsonl; `theseus queue wait` blocks until one needs the coordinator.
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
    record = {"time": ag.now(), "kind": kind, "agent": agent, "task": task, "detail": detail}
    with open(queue_dir(conf) / "events.jsonl", "a") as handle:
        handle.write(json.dumps(record) + "\n")
    return record


def events(conf, limit=None):
    path = queue_dir(conf) / "events.jsonl"
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    return [json.loads(line) for line in (lines[-limit:] if limit else lines) if line.strip()]


@contextmanager
def locked(conf):
    with open(queue_dir(conf) / "lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def script(name):
    return str(resources.files("theseus") / "scripts" / name)


def run_script(conf, name, args, agent=None):
    from . import profiles
    env = {**conf.environment(), **os.environ}
    if agent is not None:
        env.update(profiles.instance_env(conf, agent.get("provider")))
    return subprocess.run(["bash", script(name), *args], cwd=conf.root, env=env, capture_output=True, text=True)


def fits(conf, agent, task, info):
    """Whether this agent may take this task now, and the reset it needs first (None, 'compact' or 'clear')."""
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
    result = run_script(conf, "instance-send.sh", [agent["pane"], task["id"], str(worktree), record, deadline], agent)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    state = runtime(conf)
    state.setdefault(agent["name"], {}).update({"last_task": task["id"], "assigned": ag.now(), "nudged": None})
    save_runtime(conf, state)
    return True, str(worktree)


def nudge(conf, agent, task):
    text = conf.section("queue").get("nudge") or (
        "Continue the current task from the state of your worktree; if an error stopped you, retry the last step, "
        "and finish with the handoff and your done line.")
    result = run_script(conf, "instance-type.sh", [agent["pane"], text], agent)
    state = runtime(conf)
    state.setdefault(agent["name"], {})["nudged"] = task["id"]
    save_runtime(conf, state)
    return result.returncode == 0


def tick(conf, assign=True):
    """One pass over every registered agent. Returns the events it recorded."""
    recorded = []
    claimed = set()
    with locked(conf):
        for agent in ag.agents(conf):
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
            elif info["state"] == "stalled":
                if runtime(conf).get(name, {}).get("nudged") != task_id:
                    if nudge(conf, agent, t.Queue(conf).get(task_id)):
                        recorded.append(event(conf, "nudged", name, task_id, f"idle {info['idleMinutes']} min without a handoff"))
                else:
                    already = [e for e in events(conf) if e["kind"] == "stalled" and e["task"] == task_id]
                    if not already:
                        recorded.append(event(conf, "stalled", name, task_id, f"still idle after a nudge ({info['idleMinutes']} min)"))
            if info["state"] != "free" or not assign or paused(conf):
                continue
            task, needed = next_task(conf, agent, info, claimed)
            if task is None:
                continue
            claimed.add(task["id"])
            if needed:
                ok, detail = reset(conf, agent, needed)
                if not ok:
                    recorded.append(event(conf, "error", name, task["id"], f"{needed} failed: {detail}"))
                    continue
                recorded.append(event(conf, "cleared" if needed == "clear" else "compacted", name, None, detail))
            try:
                ok, detail = assign_and_send(conf, agent, task)
            except Exception as error:
                ok, detail = False, str(error)
            recorded.append(events(conf, 1)[-1] if ok else event(conf, "error", name, task["id"], detail))
    return recorded


def run(conf, interval=30, once=False):
    while True:
        for record in tick(conf):
            print(f"{record['time']} {record['kind']:<10} {record['agent'] or '-':<18} {record['task'] or '-'} {record['detail']}")
            sys.stdout.flush()
        if once:
            return
        time.sleep(interval)


def wait(conf, timeout=10800, interval=30, drive=True):
    """Block until an event that needs the coordinator appears after the call started; print it and return 0.
    With drive, also run ticks meanwhile so the queue makes progress without a separate runner."""
    start = len(events(conf))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if drive:
            tick(conf)
        new = events(conf)[start:]
        hits = [e for e in new if e["kind"] in ATTENTION]
        if hits:
            for record in new:
                print(f"{record['time']} {record['kind']:<10} {record['agent'] or '-':<18} {record['task'] or '-'} {record['detail']}")
            return 0
        time.sleep(interval)
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
