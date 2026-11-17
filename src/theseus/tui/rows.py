"""Table rows and panel texts for the dashboard: (key, cells, search text) and Rich text built from snapshots, with no
widget code."""
import time
from datetime import datetime, timezone

from rich.text import Text

from .. import host, top
from . import charts, theme

AGENT_COLUMNS = ("agent", "pane", "state", "task", "room", "on task", "quiet", "cpu", "mem", "note")
WAITING_COLUMNS = ("status", "task", "room", "for", "posted", "reason")
EVENT_COLUMNS = ("time", "kind", "agent", "task", "detail")
HOST_EVENT_COLUMNS = ("time", "project", "kind", "agent", "task", "detail")
POOL_COLUMNS = ("pool", "working", "holders", "claims")
GATEWAY_COLUMNS = ("pool", "recent codes", "last error", "last success", "paused until")
MEM_WARN, MEM_ALARM = 0.10, 0.25
CPU_HELP = "cpu: % of one core, the bar fills at one core"
USAGE_LEGEND = f"cpu: % of a core · mem: share of RAM, bar full at {MEM_ALARM:.0%}"
MEM_HELP = (f"mem: resident memory and its share of system RAM, the bar fills at {MEM_ALARM:.0%};"
            f" yellow from {MEM_WARN:.0%}, red from {MEM_ALARM:.0%}")


def cell(text, style=""):
    return Text(str(text), style=style)


def marked(text, marker_style, text_style="bold", marker="●"):
    shown = Text(f"{marker} ", style=marker_style)
    shown.append(str(text), style=text_style)
    return shown


def row(key, cells, extra=""):
    cells = [c if isinstance(c, Text) else cell(c) for c in cells]
    return key, cells, " ".join([c.plain for c in cells] + [extra]).lower()


def gauge(fraction, label, width=5, style=None):
    shown = charts.meter(fraction, width, style=style)
    shown.append(f" {label}")
    return shown


def memory(rss, total):
    """An agent's resident memory as a bar of its share of system RAM (full at MEM_ALARM), its size and percent."""
    if not total:
        return gauge(0, top.size(rss), style=theme.NEUTRAL)
    share = rss / total
    style = theme.RED if share >= MEM_ALARM else theme.YELLOW if share >= MEM_WARN else theme.NEUTRAL
    return gauge(share / MEM_ALARM, f"{top.size(rss)} {share:>3.0%}", style=style)


def agent_rows(state):
    times = state.get("times", {})
    total = (state.get("host") or {}).get("memTotal") or 0
    rows = []
    for agent in state["agents"]:
        color = theme.STATE.get(agent["state"], "")
        note = "dirty" if agent.get("tainted") else ""
        glow = next(((r, e) for r, e in top.recent(state, top.GLOW_LABEL) if r.get("agent") == agent["name"]), None)
        if glow:
            note = f"{note} {top.GLOW_LABEL[glow[0]['kind']]}".strip()
        quiet = None if agent.get("idleMinutes") is None else agent["idleMinutes"] * 60
        on_task = top.age(times.get(agent.get("task"), (None, None))[1]) if agent.get("task") else None
        cpu = "-" if agent.get("cpu") is None else gauge(agent["cpu"] / 100, f"{agent['cpu']:>3.0f}%", style=theme.NEUTRAL)
        mem = "-" if agent.get("rss") is None else memory(agent["rss"], total)
        glow_style = f"bold {theme.KIND.get(glow[0]['kind'], '')}" if glow else ""
        rows.append(row(f"agent:{agent['name']}:{agent['pane']}", [
            marked(agent["name"], color), agent["pane"], cell(agent["state"], color),
            cell(agent.get("task") or "-", "bold"), cell(agent.get("room") or "-", theme.ROOM.get(agent.get("room"), "")),
            cell(top.duration(on_task), theme.aged(on_task, top.SLOW, top.VERY_SLOW)),
            cell(top.duration(quiet), theme.aged(quiet, top.QUIET_WARN, 2 * top.QUIET_WARN)),
            cpu, mem, cell(note, theme.RED if agent.get("tainted") else glow_style)]))
    for worker in state.get("workers", []):
        on_task = top.age(times.get(worker["task"], (None, None))[1])
        rows.append(row(f"worker:{worker['name']}:{worker['task']}", [
            marked(worker["name"], theme.CYAN, marker="◆"), "-", cell(worker["kind"], theme.CYAN), cell(worker["task"], "bold"),
            cell(worker.get("room") or "-", theme.ROOM.get(worker.get("room"), "")),
            cell(top.duration(on_task), theme.aged(on_task, top.SLOW, top.VERY_SLOW)), "-", "-", "-",
            cell(worker.get("model") or "", "dim")]))
    return rows


def waiting_rows(state):
    rows = []
    for number, entry in enumerate(top.waiting_entries(state)):
        label = entry["label"]
        room = entry["room"] or "-"
        if label == "started":
            reason = cell(f"-> {entry['agent'] or '?'}", theme.GREEN)
            shown = cell(f"{int(entry['elapsed'])}s", "dim")
        else:
            reason = cell(entry["reason"], theme.REASON[entry["reason_kind"]])
            shown = cell(top.duration(entry["since"]),
                         theme.aged(entry["since"], top.SLOW, top.VERY_SLOW) if entry["key"] in ("review", "merged") else "")
        style = theme.QUEUE.get(label, theme.GREEN)
        strong = label in ("review", "merged", "ready")
        rows.append(row(f"wait:{entry['label']}:{entry['task']}", [
            marked(label, style, f"bold {style}" if strong else style), cell(entry["shown_id"], "bold" if strong else ""),
            cell(room, theme.ROOM.get(room, "")), shown, cell(top.posted_label(entry["posted"]), "dim"), reason]))
    return rows


def event_key(record, index):
    return f"event:{record.get('project') or ''}:{record.get('id', f'#{index}')}"


EVENT_SORT = {"time": lambda r: r.get("time") or "", "project": lambda r: (r.get("project") or "").lower(),
              "kind": lambda r: r.get("kind") or "", "agent": lambda r: (r.get("agent") or "").lower(),
              "task": lambda r: (r.get("task") or "").lower(), "detail": lambda r: top.event_detail(r).lower()}


EVENT_CACHE = {}


def event_row(record, number, project):
    """One event's row, cached by everything it shows, so a refresh rebuilds only new events."""
    token = (theme.GENERATION, project is not None, number, *(record.get(name) for name in
                                                              ("project", "id", "time", "kind", "agent", "task",
                                                               "detail", "reset", "room")))
    found = EVENT_CACHE.get(token)
    if found is None:
        strong = "bold " if record["kind"] in theme.STRONG_KINDS else ""
        cells = [cell(top.local_clock(record["time"]), "dim"),
                 cell(record["kind"], strong + theme.KIND.get(record["kind"], "")),
                 record.get("agent") or "-", cell(record.get("task") or "-", strong.strip()),
                 cell(top.event_detail(record), "dim")]
        if project is not None:
            name = record.get("project") or "-"
            cells.insert(1, cell(name, f"bold {theme.project_color(name)}"))
        if len(EVENT_CACHE) > 20000:
            EVENT_CACHE.clear()
        found = EVENT_CACHE[token] = row(event_key(record, number), cells, record.get("room") or "")
    return found


def event_rows(events, project=None, sort=("time", True)):
    """Rows of `events` (oldest first) sorted by the column `sort[0]`, descending when `sort[1]`; ties keep time order.
    The row key is event:<project>:<event id>."""
    column, descending = sort
    order = sorted(enumerate(events), key=lambda pair: (EVENT_SORT[column](pair[1]), pair[0]), reverse=descending)
    return [event_row(record, number if "id" not in record else None, project) for number, record in order]


def pool_rows(state_pools):
    rows = []
    for pool in state_pools:
        full = pool["max"] and pool["active"] >= pool["max"]
        holders = Text()
        for name, count in sorted(pool["projects"].items()):
            if holders:
                holders.append(", ")
            holders.append(name, style=f"bold {theme.project_color(name)}")
            holders.append(f" {count}")
        rows.append(row(f"pool:{pool['pool']}", [
            cell(pool["pool"], "bold"), cell(f"{pool['active']}/{pool['max']}", f"bold {theme.RED}" if full else f"bold {theme.GREEN}"),
            holders if holders else "-", cell(", ".join(pool["claims"]) or "-", theme.YELLOW if pool["claims"] else "")]))
    return rows


def gateway_rows(entries, now=None):
    now = time.time() if now is None else now
    rows = []
    for entry in entries or []:
        codes = " ".join(str(code or "?") for code in entry.get("codes") or []) or "-"
        last_error = f"{top.duration(now - entry['lastError'])} ago" if entry.get("lastError") else "-"
        last_ok = f"{top.duration(now - entry['lastSuccess'])} ago" if entry.get("lastSuccess") else "never"
        until = entry.get("pausedUntil")
        paused = (top.local_clock(datetime.fromtimestamp(until, timezone.utc).isoformat()) + (f" {entry['reason']}" if entry.get("reason") else "")
                  if until else "-")
        rows.append(row(f"gateway:{entry['pool']}", [cell(entry["pool"], "bold"), cell(codes, theme.YELLOW), cell(last_error),
                                                      cell(last_ok, theme.GREEN if entry.get("lastSuccess") else "dim"),
                                                      cell(paused, f"bold {theme.RED}" if until else "")]))
    return rows


def matches(rows, text):
    text = text.strip().lower()
    return [r for r in rows if text in r[2]] if text else rows


def progress(done, total, width=10):
    line = charts.meter(done / total if total else 0, width, style=theme.GREEN)
    line.append(f" {done}/{total}")
    return line


def card_text(item):
    """The body of a project card on the Host tab."""
    if not item["available"]:
        return Text(f"unavailable: {top.cut(item['error'] or '', 200)}", style=theme.RED)
    state = item["snapshot"]
    counts = host.summary_counts(item)
    line = Text()
    line.append(counts["milestone"] or "no milestone", style="bold")
    if counts["progress"] and counts["progress"][1]:
        line.append("  ")
        line.append_text(progress(*counts["progress"]))
    line.append("\n")
    working, registered = counts["working"], counts["registered"]
    line.append("● ", style=theme.GREEN if working else theme.YELLOW if registered else "dim")
    line.append(f"{working}", style="bold")
    line.append(f" working of {registered} agent(s)")
    stalled = sum(1 for a in state["agents"] if a["state"] in ("stalled", "offline"))
    if stalled:
        line.append(f"  {stalled} stalled/offline", style=f"bold {theme.RED}")
    line.append("\n")
    waiting = [(name, count) for name, count in counts["waiting"].items() if count]
    for name, count in waiting:
        line.append(f"{name} ", style=theme.QUEUE[name])
        line.append(f"{count}  ", style=f"bold {theme.QUEUE[name]}")
    if not waiting:
        line.append("nothing waiting", style="dim")
    line.append("\n")
    pools = state.get("pools") or []
    for pool in pools:
        full = pool["max"] and pool["active"] >= pool["max"]
        line.append(f"pool {pool['pool']} ")
        line.append(f"{pool['active']}/{pool['max']}", style=f"bold {theme.RED}" if full else f"bold {theme.GREEN}")
        line.append(f" (this {pool['projects'].get(state['project'], 0)})  ", style="dim")
    if not pools:
        line.append("no pool", style="dim")
    line.append("\n")
    last = state["events"][-1] if state["events"] else None
    if last:
        line.append(f"{top.local_clock(last['time'])} ", style="dim")
        line.append(last["kind"], style=theme.KIND.get(last["kind"], ""))
        line.append(f" {last.get('task') or ''}".rstrip())
    else:
        line.append("no events yet", style="dim")
    line.append("   ")
    line.append("PAUSED" if counts["paused"] else "running", style=f"bold {theme.YELLOW}" if counts["paused"] else theme.GREEN)
    return line


def attention(state):
    """The coordinator's to-do lines of the plain dashboard, as Rich text."""
    found = top.issues(state)
    if not found:
        return Text.assemble(("● ", theme.GREEN), ("nothing needs the coordinator", "dim"))
    text = Text("\n").join(Text.from_ansi(line) for line in found)
    text.stylize("bold")
    return text


def project_status(state):
    """Milestone, repository, verification, pools and today's throughput for a project tab."""
    line = Text()
    line.append(state["milestone"] or "no milestone", style="bold")
    done = state.get("progress")
    if done and done[1]:
        line.append("  ")
        line.append_text(progress(*done, width=20))
        line.append(" done")
    if state["paused"]:
        line.append("   QUEUE PAUSED", style=f"bold {theme.YELLOW}")
    git = state["git"]
    line.append("\n")
    line.append(git["branch"], style="bold")
    line.append(f" {top.cut(git['head'], 60)}   ")
    line.append(f"{git['dirty']} dirty" if git["dirty"] else "clean", style=theme.YELLOW if git["dirty"] else theme.GREEN)
    ahead = "no remote" if git["ahead"] is None else f"{git['ahead']} ahead of remote"
    line.append(f" · {git['worktrees']} worktrees · {ahead}", style="dim")
    verification = state["verification"]
    last = verification["last"]
    line.append("\nverification ")
    if last:
        line.append("pass" if last["passed"] else "FAIL", style=f"bold {theme.GREEN}" if last["passed"] else f"bold {theme.RED}")
        line.append(f" on {last['commit']} {top.duration(last['age'])} ago")
    else:
        line.append("never", style=theme.YELLOW)
    over = verification["limit"] and (verification["unverified"] or 0) > verification["limit"]
    line.append(f"   {verification['unverified']} unverified code commits", style=f"bold {theme.RED}" if over else "")
    line.append(f" (limit {verification['limit'] or '-'})", style="dim")
    for pool in state.get("pools", []):
        full = pool["max"] and pool["active"] >= pool["max"]
        line.append(f"\npool {pool['pool']} ", style="bold")
        line.append(f"{pool['active']}/{pool['max']}", style=f"bold {theme.RED}" if full else f"bold {theme.GREEN}")
        line.append(f" working · this project {pool['projects'].get(state['project'], 0)}")
        if pool["claims"]:
            line.append(f" · claims: {', '.join(pool['claims'])}", style=theme.YELLOW)
    today = state.get("today") or {}
    median = today.get("medianHandoff")
    line.append("\ntoday ")
    line.append(str(today.get("delivered", 0)), style="bold")
    line.append(" delivered · ")
    line.append(str(today.get("completed", 0)), style="bold")
    line.append(" completed · median time to handoff ")
    line.append(top.duration(median) if median is not None else "-", style="bold")
    return line


TASK_FIELDS = ("id", "status", "role", "room", "priority", "profile", "owner", "dependencies", "ownedPaths", "objective",
               "generalContract", "acceptance")
TASK_LABELS = {"ownedPaths": "owned paths", "generalContract": "contract", "acceptance": "acceptance"}


def field(line, label, value):
    """One labelled field of a detail popup: lists as bullet lines, long or multi-line text on its own lines."""
    line.append(f"{label:<14}", style="bold")
    if isinstance(value, (list, tuple)):
        if not value:
            line.append("-", style="dim")
        for item in value:
            line.append(f"\n  - {item}")
    elif isinstance(value, dict):
        for key, item in value.items():
            line.append(f"\n  {key}: {item}")
    else:
        text = "-" if value in (None, "") else str(value)
        line.append(f"\n  {text}" if "\n" in text or len(text) > 90 else text)
    line.append("\n")


def task_text(task, entry=None, project=None):
    """Every field of a task record for its popup: the main fields in a fixed order, then any others (timestamps)."""
    line = Text()
    if project:
        field(line, "project", project)
    if entry:
        field(line, "waiting", f"{entry['label']}  {entry.get('reason') or ''}".strip())
    if not task:
        line.append("the task record is not in this snapshot", style="dim")
        return line
    for name in TASK_FIELDS:
        if name in task:
            field(line, TASK_LABELS.get(name, name), task[name])
    for name, value in task.items():
        if name not in TASK_FIELDS and value not in (None, "", [], {}):
            field(line, name, value)
    line.rstrip()
    return line


def task_summary(task):
    line = Text()
    for name in ("id", "status", "role", "room", "owner"):
        if task.get(name):
            field(line, name, task[name])
    objective = str(task.get("objective") or "")
    field(line, "objective", objective if len(objective) <= 400 else objective[:400] + " ...")
    line.rstrip()
    return line


def event_text(record, task=None):
    """An event for its popup: every field, the detail untruncated, then a summary of the linked task."""
    line = Text()
    field(line, "time", f"{top.local_clock(record.get('time'))}  ({record.get('time')})")
    for name in ("project", "kind", "agent", "task", "room"):
        if record.get(name):
            field(line, name, record[name])
    field(line, "detail", record.get("detail"))
    for name, value in record.items():
        if name not in ("time", "project", "kind", "agent", "task", "room", "detail", "epoch") and value not in (None, ""):
            field(line, name, value)
    if task:
        line.append("\nlinked task\n", style="bold underline")
        line.append_text(task_summary(task))
    line.rstrip()
    return line


def agent_text(agent, task=None, total=0):
    """An agent for its popup: state, pane, profile, CPU and memory, taint, then its current task."""
    line = Text()
    field(line, "state", agent.get("state"))
    field(line, "pane", agent.get("pane"))
    field(line, "profile", agent.get("profile") or agent.get("provider") or "-")
    field(line, "cpu", "-" if agent.get("cpu") is None else f"{agent['cpu']:.0f}% of one core")
    rss = agent.get("rss")
    field(line, "memory", "-" if rss is None else top.size(rss) + (f" ({rss / total:.1%} of RAM)" if total else ""))
    field(line, "taint", "dirty worktree" if agent.get("tainted") else "clean")
    if agent.get("task"):
        line.append("\ncurrent task\n", style="bold underline")
        line.append_text(task_text(task) if task else Text(agent["task"]))
    line.rstrip()
    return line
