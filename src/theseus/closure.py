"""Closing a project and reopening it later (`theseus close`, `theseus reopen`).

close writes the task export, stops the supervised agents, the queue driver and the coordinator, and leaves a marker,
<data>/closed.json, that keeps them stopped: `agents up` and `coordinator up` refuse a closed project, `agents up
--all-projects` and the console skip it. Nothing is deleted: tasks, worktrees, agent records and conversations stay,
so reopen brings the agents back (an agent that held a task resumes it) and the coordinator if it was running."""
import json
import subprocess
import time
from pathlib import Path

from . import config as cfg

MARKER = "closed.json"


class ClosedError(Exception):
    pass


def marker(data):
    return Path(data) / MARKER


def closed(conf_or_data):
    """The close record ({closed, reason, coordinator}) of a project (a Config or its data directory), or None."""
    data = getattr(conf_or_data, "data", conf_or_data)
    try:
        return json.loads(marker(data).read_text())
    except (OSError, ValueError, TypeError):
        return None


def refuse_if_closed(conf, what):
    record = closed(conf)
    if record:
        raise ClosedError(f"{conf.name} is closed (since {record.get('closed') or '?'}); `theseus reopen` brings it back "
                          f"before {what}")


def holders(conf):
    """[(agent, task)] for the supervised agents that hold a task."""
    from . import supervisor
    found = []
    for name, rec in supervisor.records(conf).items():
        task = supervisor.current_task(conf, name) if rec.get("managed") else None
        if task:
            found.append((name, task["id"]))
    return found


def git_lines(root):
    """What is left to commit or push in the checkout, as lines."""
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    lines = []
    dirty = [line[3:] for line in git("status", "--porcelain", "--untracked-files=no").stdout.splitlines()]
    if dirty:
        lines.append(f"uncommitted: {', '.join(dirty[:6])}{' ...' if len(dirty) > 6 else ''}")
    ahead = git("rev-list", "--count", "@{upstream}..HEAD")
    if ahead.returncode == 0 and ahead.stdout.strip() not in ("", "0"):
        lines.append(f"unpushed: {ahead.stdout.strip()} commit(s)")
    return lines


def close(conf, reason="", force=False, commit=False):
    """Close the project. Refuses while a supervised agent holds a task unless force (the task stays assigned and its
    agent resumes it on reopen). The coordinator is left to stop_coordinator, called last, since close may run inside
    its session. Returns (action rows, notes)."""
    from . import coordinator, supervisor, taskrecords
    from . import tasks as t
    busy = holders(conf)
    if busy and not force:
        names = ", ".join(f"{agent} ({task})" for agent, task in busy)
        raise ClosedError(f"agents hold tasks: {names}. Let them deliver, or pass --force: they stop, the tasks stay "
                          f"assigned and resume on reopen")
    rows, notes = [], []
    try:
        notes.append(t.export(t.Queue(conf)).split(";")[0])
    except Exception as error:
        notes.append(f"warning: no task export written ({error})")
    if commit:
        exported = [cfg.TASKS, cfg.ARCHIVE, taskrecords.HISTORY]
        subprocess.run(["git", "-C", str(conf.root), "add", "--", *exported], capture_output=True)
        made = subprocess.run(["git", "-C", str(conf.root), "commit", "-q", "-m", "Task export at project close", "--",
                               *exported], capture_output=True, text=True)
        notes.append("committed the task export" if made.returncode == 0 else "the task export was already committed")
    running = []
    if supervisor.managed(conf):
        running = [name for name, rec in supervisor.records(conf).items()
                   if rec.get("managed") and rec.get("desired") != "down"]
        rows += supervisor.down(conf, None, force=True)
    rows.append(supervisor.stop_driver(conf))
    st = coordinator.state(conf)
    was_up = st.get("desired") == "up"
    record = {"closed": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "reason": reason, "coordinator": was_up,
              "agents": running, "held": [{"agent": agent, "task": task} for agent, task in busy]}
    conf.data.mkdir(parents=True, exist_ok=True)
    cfg.write_atomic(marker(conf.data), json.dumps(record, indent=2) + "\n")
    notes += git_lines(conf.root)
    notes.append(f"closed; `theseus reopen` brings it back{' with the coordinator' if was_up else ''}")
    return rows, notes


def stop_coordinator(conf):
    """The coordinator's row after stopping it (desired down, so nothing restarts it), or None when it has none."""
    from . import coordinator
    if not coordinator.state(conf):
        return None
    return (coordinator.NAME, *coordinator.down(conf))


def reopen(conf, wait=True):
    """Reopen a closed project: drop the marker, bring back the supervised agents that ran at close under their own
    names (each resumes its conversation, and its task if it held one), the queue driver, and the coordinator when it
    ran at close or [coordinator] autostart is on. Returns action rows."""
    from . import coordinator, supervisor, workqueue
    record = closed(conf) or {}
    marker(conf.data).unlink(missing_ok=True)
    rows = []
    if supervisor.managed(conf):
        with workqueue.locked(conf):
            supervisor.set_paused(conf, False)
            known = supervisor.records(conf)
            for name in record.get("agents") or []:
                if name in known:
                    supervisor.update(conf, name, desired="up")
            rows += supervisor.reconcile(conf, wait=wait) or []
    rows.append(supervisor.start_driver(conf))
    if record.get("coordinator") or coordinator.autostart(conf):
        try:
            rows.append((coordinator.NAME, *coordinator.up(conf, wait=wait)))
        except coordinator.CoordinatorError as error:
            rows.append((coordinator.NAME, "error", str(error)))
    return rows
