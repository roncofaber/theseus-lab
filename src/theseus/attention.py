"""The attention list: everything that needs the coordinator now, derived on demand from the task records, the host
store and the live state of agents, the queue driver and the gateway. Nothing about an item is stored: it disappears
only when its cause is resolved, never because someone saw it.

Kinds and the condition that resolves each:
- delivery: a task in verification, until it is merged, reopened or released;
- merged: a merged task, until it is completed (verified) or unmerged;
- question: a worker's open question (`task note --question`), until `tasks note ID` or an edit answers it;
- agent: an agent at a permission prompt (waiting) or startup dialog (blocked), failed after its restart budget, a
  hand-made pane gone (offline), or given up on by the queue (still idle after its nudge, thrashing in a hand-made
  pane, a gateway error past its resends or fatal), until the agent's live state no longer says so;
- gateway: a gateway pause or cap cut in force on a pool the project's agents use, until it lifts;
- assign: a pending task whose assignment kept failing for longer than queue.assign_backoff_seconds, until it is
  assigned, held or no longer pending;
- driver: a project with agents and no live driving queue loop, until one runs again;
- export: the committed task files edited by hand, or stale behind a task completed after the last export, until
  they are exported again;
- debt: unverified code commits past the pre-push guard's limits (verification.max_unverified_merges or
  max_unverified_hours), until `theseus verify` passes.

The queue driver pushes the list into a registered coordinator pane ([attention] notify = "pane"): one short line
whenever items were added, typed through the checked send, never into a prompt or over typed input, at most once per
tick and once more as a reminder after remind_minutes. Items open past escalate_minutes, and a coordinator pane gone
or stuck, run the host's [attention] escalate_command once with the summary on stdin. The push bookkeeping (first
sight of an item without a time of its own, what was pushed and escalated) is the only state, in
<data>/queue/attention.json.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time

from . import agents as ag
from . import config as cfg
from . import store
from . import tasks as t
from . import workqueue as wq

KINDS = ("delivery", "merged", "question", "agent", "gateway", "assign", "driver", "export", "debt")
PLURAL = {"delivery": "deliveries", "merge to verify": "merges to verify", "open question": "open questions",
          "stalled agent": "stalled agents", "agent at a prompt": "agents at a prompt",
          "agent at a startup dialog": "agents at a startup dialog", "failed agent": "failed agents",
          "offline agent": "offline agents", "gateway pause": "gateway pauses", "gateway cap cut": "gateway cap cuts",
          "assignment error": "assignment errors", "queue driver down": "queue drivers down",
          "task export to fix": "task exports to fix", "verification debt": "verification debts"}
TASK_KINDS = ("error", "assigned", "released", "unclaimed", "reopened", "unmerged", "delivered", "partial", "blocked")
LOOKBACK = 14 * 86400


class AttentionError(Exception):
    pass


def settings(conf):
    from . import hostconfig
    section = conf.section("attention")
    try:
        command = hostconfig.attention().get("escalate_command") or ""
    except hostconfig.ConfigError:
        command = ""
    return {"notify": str(section.get("notify") or "").strip(), "remind_minutes": float(section.get("remind_minutes") or 0),
            "escalate_minutes": float(section.get("escalate_minutes") or 0), "escalate_command": command}


def iso(epoch):
    return store.iso(epoch)


def epoch(value):
    try:
        return store.epoch_of(value)
    except (TypeError, ValueError):
        return None


def entry(conf, key, kind, label, since, summary, command, **extra):
    return {"key": key, "kind": kind, "project": conf.name, "since": since, "label": label, "summary": summary,
            "command": command, **{k: v for k, v in extra.items() if v is not None}}


def book_path(conf):
    return wq.queue_dir(conf) / "attention.json"


def bookkeeping(conf):
    try:
        data = json.loads(book_path(conf).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_book(conf, book):
    cfg.write_atomic(book_path(conf), json.dumps(book, indent=2, sort_keys=True) + "\n")


def history(conf, now):
    """This project's recent events of the kinds the list reads, oldest first, from a read-only connection."""
    kinds = sorted(set(TASK_KINDS) | {"stalled", "gateway_paused", "gateway_capped"})
    try:
        with store.opened(readonly=True) as conn:
            return store.events(conn, conf.name, since=now - LOOKBACK, kinds=kinds)
    except Exception:
        return []


def streak_start(conf, name, state, now):
    """Epoch of the first of the agent's latest consecutive samples in state, or None."""
    try:
        with store.opened(readonly=True) as conn:
            rows = store.samples(conn, conf.name, agent=name, since=now - 86400)
    except Exception:
        return None
    start = None
    for row in reversed(rows):
        if row["state"] != state:
            break
        start = row["epoch"]
    return start


def task_items(conf, queue, tasks, events, now):
    found = []
    results = {record["task"]: record["kind"] for record in events
               if record.get("task") and record["kind"] in ("delivered", "partial", "blocked")}
    for task in tasks:
        task_id, status = task["id"], task["status"]
        if status == "verification":
            execution = task.get("execution") or {}
            result = results.get(task_id)
            found.append(entry(conf, f"delivery:{task_id}", "delivery", "delivery", epoch(execution.get("deliveredAt")),
                               f"{task_id}: handoff {result or 'delivered'}, waiting for review and merge",
                               f"theseus tasks check-delivery {task_id}, then theseus merge {task_id}", task=task_id))
        elif status == "merged":
            found.append(entry(conf, f"merged:{task_id}", "merged", "merge to verify",
                               epoch((task.get("merge") or {}).get("mergedAt")), f"{task_id} merged, not yet verified",
                               f"theseus verify, then theseus tasks complete {task_id} --from-log VERIFY_LOG", task=task_id))
    try:
        questions = queue.service.questions(t.acting_principal())
    except t.TaskError:
        questions = []
    for question in questions:
        found.append(entry(conf, f"question:{question['task']}:{question['time']}", "question", "open question",
                           epoch(question["time"]), f"{question['task']} asks a question",
                           f"theseus tasks show {question['task']}; answer with theseus tasks note {question['task']} TEXT",
                           task=question["task"]))
    backoff = float(conf.section("queue").get("assign_backoff_seconds", 300) or 0)
    by_task = {}
    for record in events:
        if record.get("task") and record["kind"] in TASK_KINDS:
            by_task.setdefault(record["task"], []).append(record)
    for task in tasks:
        if task["status"] != "pending" or task.get("hold"):
            continue
        streak = []
        for record in reversed(by_task.get(task["id"], [])):
            if record["kind"] != "error":
                break
            streak.insert(0, record)
        if streak and now - streak[0]["epoch"] >= backoff:
            found.append(entry(conf, f"assign:{task['id']}", "assign", "assignment error", streak[0]["epoch"],
                               f"{task['id']} could not be assigned ({len(streak)} error(s)): {streak[-1]['detail']}",
                               f"theseus queue log; fix the cause, or theseus tasks hold {task['id']}", task=task["id"]))
    return found


def gateway_phase(conf, name, task_id):
    """The phase of the agent's gateway retry episode for this task (exhausted, fatal, ...), or None."""
    try:
        with store.opened(readonly=True) as conn:
            row = store.gateway_retry(conn, conf.name, name)
    except Exception:
        return None
    return row["phase"] if row and row.get("task") == task_id else None


def stall(conf, info, runtime):
    """Why the queue gave up on an agent's task and handed it to the coordinator, from its live state and the queue's
    own markers: still idle after the nudge, thrashing, or a gateway error past its resends or fatal. None otherwise."""
    task_id, state = info.get("task"), info["state"]
    marks = runtime.get(info["name"]) or {}
    if state == "stalled" and marks.get("nudged") == task_id:
        return f"idle {info.get('idleMinutes')} min after a nudge"
    if state == "thrashing" and marks.get("thrashing") == task_id and not info.get("managed"):
        return f"thrashing: the pane shows {info.get('stall')!r}"
    if state == "gateway_error" and gateway_phase(conf, info["name"], task_id) in ("exhausted", "fatal"):
        return "a gateway error the queue no longer resends"
    return None


def agent_items(conf, states, events, now):
    found = []
    stalled = {}
    for record in events:
        if record["kind"] == "stalled" and record.get("task"):
            stalled[record["task"]] = record
    runtime = wq.runtime(conf)
    for info in states:
        name, state, task_id = info["name"], info["state"], info.get("task")
        managed = info.get("managed")
        attach = f"theseus agents attach {name}" if managed else f"answer it in pane {info.get('pane')}"
        if state == "waiting":
            found.append(entry(conf, f"agent:{name}:waiting", "agent", "agent at a prompt", streak_start(conf, name, state, now),
                               f"{name} waits for {info.get('waiting') or 'input'}", attach, agent=name, task=task_id))
        elif state == "blocked":
            found.append(entry(conf, f"agent:{name}:blocked", "agent", "agent at a startup dialog",
                               streak_start(conf, name, state, now), f"{name} shows a startup dialog", attach, agent=name))
        elif state == "failed":
            found.append(entry(conf, f"agent:{name}:failed", "agent", "failed agent", streak_start(conf, name, state, now),
                               f"{name} failed: its restart budget is spent", f"theseus agents restart {name}", agent=name,
                               task=task_id))
        elif state == "offline" and not managed and info.get("enabled", True):
            found.append(entry(conf, f"agent:{name}:offline", "agent", "offline agent", streak_start(conf, name, state, now),
                               f"{name}: pane {info.get('pane')} is gone",
                               f"start its agent again and theseus agents add, or theseus agents remove {name}", agent=name,
                               task=task_id))
        elif task_id and (why := stall(conf, info, runtime)):
            record = stalled.get(task_id)
            since = record["epoch"] if record else streak_start(conf, name, state, now)
            found.append(entry(conf, f"agent:{name}:stalled:{task_id}", "agent", "stalled agent", since,
                               f"{name} stalled on {task_id}: {why}", f"theseus agents logs {name}", agent=name,
                               task=task_id))
    return found


def gateway_items(conf, states, events, now):
    from . import gateway
    pools = {gateway.pool_key(conf, wq.launched(conf, info)) for info in states}
    if not pools:
        return []
    try:
        health = gateway.health(now)
    except Exception:
        return []
    latest = {}
    for record in events:
        if record["kind"] in ("gateway_paused", "gateway_capped"):
            latest[record["kind"]] = record["epoch"]
    found = []
    for item in health:
        pool = item["pool"]
        if pool not in pools:
            continue
        if item.get("pausedUntil"):
            found.append(entry(conf, f"gateway:{pool}:paused", "gateway", "gateway pause", latest.get("gateway_paused"),
                               f"gateway pool {pool} is paused ({item.get('reason') or 'errors'}): no new assignments or resends",
                               "theseus pool; the pause lifts by itself (theseus console shows when)", pool=pool))
        if item.get("capCut"):
            found.append(entry(conf, f"gateway:{pool}:cap", "gateway", "gateway cap cut", latest.get("gateway_capped"),
                               f"gateway pool {pool} works with its cap cut after errors",
                               "theseus pool; the cap steps back up by itself", pool=pool))
    return found


def driver_item(conf, now):
    beat = wq.watcher(conf, now)
    if beat and beat.get("alive"):
        return []
    since = epoch(beat.get("next")) if beat else None
    return [entry(conf, "driver", "driver", "queue driver down", since,
                  "no queue driver runs: nothing assigns tasks, nudges agents or records deliveries", "theseus agents up")]


def export_items(conf, queue):
    try:
        state = t.export_state(queue)
    except (t.TaskError, OSError, ValueError):
        return []
    if state.get("state") == "edited":
        return [entry(conf, "export:edited", "export", "task export to fix", None, state["detail"],
                      "theseus tasks export (drops the edit) or theseus tasks import --replace (takes it); commit docs/project/")]
    if state.get("state") != "stale" or not state.get("since"):
        return []
    completed = [(task.get("result") or {}).get("completedAt") for task in queue.all() if task.get("status") == "completed"
                 and isinstance(task.get("result"), dict)]
    after = sorted(stamp for stamp in completed if stamp and stamp > state["since"])
    if not after:
        return []
    return [entry(conf, "export:stale", "export", "task export to fix", epoch(after[0]),
                  f"the task export misses {len(after)} task(s) completed since it was written",
                  "theseus tasks export, then commit docs/project/")]


def debt_items(conf):
    from . import gitutil as g
    from . import verify
    try:
        unverified, limit = verify.debt(conf)
        hours = float(conf.section("verification").get("max_unverified_hours") or 0)
        times = []
        if limit and len(unverified) > limit:
            times.append(int(g.out(conf.root, "show", "-s", "--format=%ct", unverified[-(limit + 1)])))
        if hours and unverified:
            oldest = int(g.out(conf.root, "show", "-s", "--format=%ct", unverified[-1]))
            if time.time() - oldest > hours * 3600:
                times.append(oldest + hours * 3600)
    except Exception:
        return []
    if not times:
        return []
    return [entry(conf, "debt", "debt", "verification debt", min(times),
                  f"{len(unverified)} code commit(s) since the last full verification, past the pre-push guard's limits",
                  "theseus verify")]


def items(conf, now=None, states=None, driver=True, book=None):
    """The attention list, oldest first. states reuses agent states already read; driver=False leaves out the
    driver check (the driver asking about itself)."""
    now = time.time() if now is None else now
    queue = t.Queue(conf)
    tasks = queue.load()["tasks"]
    states = ag.states(conf) if states is None else states
    events = history(conf, now)
    found = task_items(conf, queue, tasks, events, now) + agent_items(conf, states, events, now)
    found += gateway_items(conf, states, events, now)
    if driver and states:
        found += driver_item(conf, now)
    found += export_items(conf, queue) + debt_items(conf)
    seen = (bookkeeping(conf) if book is None else book).get("seen") or {}
    for item in found:
        if item["since"] is None:
            item["since"] = seen.get(item["key"], now)
        item["since"] = iso(item["since"])
    return sorted(found, key=lambda item: (item["since"], KINDS.index(item["kind"]), item["key"]))


def summary(found):
    """'3 items need you (2 deliveries, 1 stalled agent)', or 'nothing needs you'."""
    if not found:
        return "nothing needs you"
    counts = {}
    for item in sorted(found, key=lambda item: KINDS.index(item["kind"])):
        counts[item["label"]] = counts.get(item["label"], 0) + 1
    parts = ", ".join(f"{n} {PLURAL.get(label, label) if n > 1 else label}" for label, n in counts.items())
    return f"{len(found)} item{'s' if len(found) > 1 else ''} need{'' if len(found) > 1 else 's'} you ({parts})"


def push_line(found, reminder=False):
    text = summary(found)
    if reminder:
        text = text.replace(" need you", " still need you").replace(" needs you", " still needs you")
        return f"theseus: reminder: {text}; run theseus attention"
    return f"theseus: {text}; run theseus attention"


# the coordinator pane

def meta_key(conf):
    return f"coordinator.pane:{conf.name}"


def coordinator(conf):
    """The registered coordinator pane ({pane, pane_pid, server, profile, registered}), or None."""
    try:
        with store.opened(readonly=True) as conn:
            value = store.get_meta(conn, meta_key(conf))
    except Exception:
        return None
    try:
        return json.loads(value) if value else None
    except ValueError:
        return None


def register(conf, pane=None, profile=None, server=None):
    """Record the coordinator pane; server names the private server a pane address lives on (the supervised
    coordinator session registers itself this way)."""
    from . import profiles
    from . import tmux as tm
    pane = pane or os.environ.get("TMUX_PANE")
    if not pane:
        raise AttentionError("no pane given and $TMUX_PANE is not set: run this in the coordinator's tmux pane, or name "
                             "the pane (theseus coordinator register %N)")
    if server is None and pane == os.environ.get("TMUX_PANE") and tm.private_tmux(os.environ.get("TMUX")):
        server = tm.Server().name
    holder = next((a for a in ag.agents(conf) if a["pane"] == pane), None)
    if holder is not None:
        raise AttentionError(f"pane {pane} belongs to the registered agent {holder['name']}")
    record = {"pane": pane, "server": server, "registered": ag.now(),
              "profile": profiles.resolve(conf, profile)["name"]}
    pid = ag.pane_pid(pane, where(record)[0])
    if pid is None:
        raise AttentionError(f"pane {pane} does not exist")
    record["pane_pid"] = pid
    with store.for_project(conf) as conn:
        store.set_meta(conn, meta_key(conf), json.dumps(record, sort_keys=True))
    note = "" if settings(conf)["notify"] == "pane" else (
        "; push is off: set [attention] notify = \"pane\" in theseus.local.toml for the queue driver to type into it")
    return f"registered the coordinator of {conf.name} on pane {pane} (pid {pid}, profile {record['profile']}){note}"


def unregister(conf):
    with store.for_project(conf) as conn:
        found = store.get_meta(conn, meta_key(conf))
        store.delete_meta(conn, meta_key(conf))
    return "coordinator pane unregistered" if found else "no coordinator pane was registered"


def where(record):
    from . import tmux as tm
    server = tm.Server.from_name(record["server"]) if record.get("server") else tm.Server.legacy()
    return server, record["pane"]


def pane_problem(record):
    """Why the registered pane cannot take a line ('gone', or 'stale' when its pane id now runs another process), or
    None."""
    server, pane = where(record)
    try:
        if not ag.pane_alive(pane, server):
            return "gone"
        if record.get("pane_pid") and ag.pane_pid(pane, server) != record["pane_pid"]:
            return "stale"
    except Exception as error:
        return f"unreadable ({error})"
    return None


def own_line(screen, pattern):
    """Whether the input line holds an earlier line of ours that was typed but never submitted, which the next send
    clears, unlike text the user is typing."""
    held = [m.group(1) for m in (re.match(pattern, line) for line in screen[-8:]) if m] if pattern else []
    return bool(held) and held[-1].strip().startswith("theseus:")


def type_line(conf, record, text):
    """Type one line into the coordinator pane through the checked send. Returns (ok, detail); nothing is typed while
    the pane shows a prompt for a human or its input line holds text (the user is typing)."""
    from . import profiles
    problem = pane_problem(record)
    if problem:
        return False, f"the coordinator pane {record['pane']} is {problem}"
    server, pane = where(record)
    profile = record.get("profile")
    try:
        screen = ag.pane_screen(pane, server)
        prompt = ag.pane_prompt(conf, pane, profile, screen, server)
        pattern = profiles.interactive(conf, profile).get("input_pattern")
    except Exception as error:
        return False, f"the coordinator pane could not be read: {error}"
    if prompt:
        return False, f"the coordinator pane shows a prompt ({prompt[:60]})"
    if ag.input_empty(screen, pattern) is False and not own_line(screen, pattern):
        return False, "the coordinator pane's input line holds text"
    agent = {"name": "coordinator", "pane": pane, "provider": profile}
    if record.get("server"):
        agent.update(managed=True, server=record["server"])
    # The draft check above already let only an empty line or an unsent line of the push's own through.
    ok, detail = ag.send_verified(conf, agent, text, record=False, profile=profile, keep_draft=False)
    if ok or detail.startswith(("not typed", "tmux send-keys failed")):
        return ok, detail
    try:
        unsent = own_line(ag.pane_screen(pane, server), pattern)
    except Exception:
        unsent = True
    # Submitted but no busy marker seen (a harness whose marker differs from the profile's): typing it again every
    # tick would break "once per change", so it counts as delivered and the reminder covers a miss.
    return (False, detail) if unsent else (True, f"typed, not confirmed busy ({detail})")


# push and escalation (the queue driver, after each tick)

def push(conf, found, now=None):
    """Bring the coordinator up to date with the attention list: type one line when items were added since the last
    push, one reminder after remind_minutes for items still open, and escalate what stays open too long. Returns the
    lines typed."""
    now = time.time() if now is None else now
    s = settings(conf)
    book = bookkeeping(conf)
    keys = [item["key"] for item in found]
    seen = book.get("seen") or {}
    book["seen"] = {key: seen.get(key, now) for key in keys}
    typed = []
    record = coordinator(conf) if s["notify"] == "pane" else None
    if record is not None:
        pushed = set(book.get("pushed") or [])
        added = [key for key in keys if key not in pushed]
        if added:
            book.setdefault("owed", now)
            ok, detail = type_line(conf, record, push_line(found))
            book["last"] = detail
            if ok:
                typed.append(push_line(found))
                book.update(pushed=keys, pushed_at=now, reminded=False)
                book.pop("owed", None)
        else:
            book["pushed"] = [key for key in keys if key in pushed]
            book.pop("owed", None)
            due = s["remind_minutes"] and book.get("pushed_at") and now - book["pushed_at"] >= s["remind_minutes"] * 60
            if book["pushed"] and due and not book.get("reminded"):
                ok, detail = type_line(conf, record, push_line(found, reminder=True))
                book["last"] = detail
                if ok:
                    typed.append(push_line(found, reminder=True))
                    book["reminded"] = True
    else:
        for key in ("pushed", "pushed_at", "reminded", "owed"):
            book.pop(key, None)
    escalate(conf, found, book, now, s, record)
    save_book(conf, book)
    return typed


def coordinator_problem(conf, book, now, s, record=None):
    """'gone', 'stale' or 'stuck' (a push owed for longer than escalate_minutes) for a registered coordinator pane
    while push is on; None otherwise."""
    if s["notify"] != "pane":
        return None
    record = coordinator(conf) if record is None else record
    if record is None:
        return None
    problem = pane_problem(record)
    if problem:
        return problem
    if s["escalate_minutes"] and book.get("owed") and now - book["owed"] >= s["escalate_minutes"] * 60:
        return "stuck"
    return None


def overdue(found, now, s):
    if not s["escalate_minutes"]:
        return []
    return [item for item in found if now - epoch(item["since"]) >= s["escalate_minutes"] * 60]


def escalation_text(conf, late, problem, s):
    lines = [f"theseus {conf.name}: {summary(late)} for more than {s['escalate_minutes']:g} minutes" if late else
             f"theseus {conf.name}: the coordinator needs attention"]
    lines += [f"- {item['summary']} (since {item['since']}; {item['command']})" for item in late]
    if problem:
        lines.append(f"- the coordinator pane is {problem}: pushed lines do not reach it")
    return "\n".join(lines) + "\n"


def escalate(conf, found, book, now, s, record=None):
    """Run the escalate_command once for each item newly open past escalate_minutes and for a coordinator pane newly
    gone or stuck. Returns the newly escalated keys."""
    late = overdue(found, now, s)
    problem = coordinator_problem(conf, book, now, s, record)
    current = [item["key"] for item in late] + ([f"coordinator:{problem}"] if problem else [])
    before = set(book.get("escalated") or [])
    book["escalated"] = current
    fresh = [key for key in current if key not in before]
    if not fresh:
        return []
    text = escalation_text(conf, [item for item in late if item["key"] in fresh],
                           problem if f"coordinator:{problem}" in fresh else None, s)
    wq.event(conf, "escalated", None, None, " ".join(fresh)[:200])
    command = s["escalate_command"]
    if command:
        argv = command if isinstance(command, list) else ["sh", "-c", command]
        try:
            result = subprocess.run(argv, input=text, capture_output=True, text=True, timeout=60, cwd=conf.root)
            if result.returncode != 0:
                print(f"theseus attention: escalate_command exited {result.returncode}: {result.stderr.strip()[:200]}",
                      file=sys.stderr)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"theseus attention: escalate_command {shlex.join(argv)[:80]}: {error}", file=sys.stderr)
    return fresh


def drive(conf, now=None):
    """The driver's step after a tick: compute the list, write the tmux status cache from it and push it; a failure
    is reported and never stops the queue."""
    try:
        states = ag.states(conf)
        found = items(conf, now=now, states=states, driver=False)
        try:
            from . import tmuxconf
            tmuxconf.write_status(conf, found, states, now)
        except Exception as error:
            print(f"theseus attention: tmux status: {error}", file=sys.stderr)
        return push(conf, found, now)
    except Exception as error:
        print(f"theseus attention: {error}", file=sys.stderr)
        return []


def console(conf, states):
    """{items, escalated, coordinator} for the console: the list, the items open past escalate_minutes and the
    coordinator pane's problem, computed here so a stopped driver changes nothing."""
    now = time.time()
    book = bookkeeping(conf)
    s = settings(conf)
    found = items(conf, now=now, states=states, book=book)
    return {"items": found, "escalated": overdue(found, now, s),
            "coordinator": coordinator_problem(conf, book, now, s), "notify": s["notify"],
            "push": {"at": book.get("pushed_at"), "last": book.get("last"), "owed": book.get("owed")}}


# output

def age_text(item, now):
    seconds = max(0, now - epoch(item["since"]))
    return f"{int(seconds // 60)} min" if seconds < 7200 else f"{seconds / 3600:.1f} h"


def render(conf, found, now=None):
    now = time.time() if now is None else now
    lines = [summary(found)]
    for item in found:
        lines.append(f"{item['kind']:<9} {age_text(item, now):>8}  {item['summary']}")
        lines.append(f"{'':<19}run: {item['command']}")
    s = settings(conf)
    if s["notify"] == "pane" and coordinator(conf) is None:
        lines.append("push is on but no coordinator pane is registered: theseus coordinator register")
    return "\n".join(lines)


def follow(conf, interval=10.0, clock=time.time, sleep=time.sleep, out=None, limit=None):
    """Print one line whenever the list changes (and the list once at the start); never returns unless limit (a
    number of checks, for tests) runs out."""
    out = out or sys.stdout
    previous = None
    seen = None
    checks = 0
    while limit is None or checks < limit:
        checks += 1
        conf, seen = wq.reloaded(conf, seen)
        try:
            found = items(conf, now=clock())
        except Exception as error:
            print(f"theseus attention: {error}", file=sys.stderr)
            found = None
        if found is not None:
            keys = [item["key"] for item in found]
            if previous is None or set(keys) != set(previous):
                line = f"{iso(clock())} {summary(found)}"
                added = [key for key in keys if previous is not None and key not in previous]
                gone = [key for key in previous or [] if key not in keys]
                if added:
                    line += f"; new: {', '.join(added)}"
                if gone:
                    line += f"; resolved: {', '.join(gone)}"
                print(line, file=out, flush=True)
                previous = keys
        sleep(interval)
