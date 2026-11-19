"""Table rows and panel texts for the dashboard: (key, cells, search text) and Rich text built from snapshots, with no
widget code."""
import time
from datetime import datetime, timezone

from rich.text import Text

from .. import tasks as t
from .. import top
from ..agents import LIMIT_SOURCES
from . import charts, theme

AGENT_COLUMNS = ("agent", "state", "cap", "task", "context", "room", "on task", "quiet", "up", "restarts", "model", "pool",
                 "CPU", "RAM", "note")
PROJECT_COLUMNS = ("project", "milestone", "coordinator", "watcher", "ready", "working on", "review", "attention", "agents",
                   "pool", "gateway", "today")
# Columns that drop out, first to last, when a table is too narrow for all of them.
OPTIONAL = {"agents": ("cap", "model", "pool", "note", "restarts", "up", "room", "quiet", "on task", "context"),
            "projects": ("watcher", "today", "gateway", "pool", "working on", "ready", "attention"),
            "gateway": ("pool", "attempt", "outcome"), "waiting": ("min cap", "posted", "room")}
# Stable column widths: an int is fixed, a float shares the remaining width in proportion.
WIDTHS = {
    "agents": {"agent": 14, "state": 10, "cap": 4, "task": 1.0, "context": 14, "room": 6, "on task": 8, "quiet": 6,
               "up": 6, "restarts": 8, "model": 0.8, "pool": 8, "CPU": 5, "RAM": 6, "note": 0.5},
    "waiting": {"status": 10, "pri": 5, "task": 1.0, "min cap": 8, "room": 6, "for": 8, "posted": 12, "reason": 1.2},
    "events": {"time": 8, "kind": 11, "agent": 16, "task": 0.7, "detail": 1.6},
    "host-events": {"time": 8, "project": 12, "kind": 11, "agent": 24, "task": 0.7, "detail": 1.6},
    "pools": {"pool": 12, "working": 1.0, "holders": 1.5, "claims": 1.0},
    "gateway": {"time": 8, "project": 12, "agent": 24, "task": 0.8, "error": 1.0, "attempt": 8, "outcome": 0.8, "pool": 1.0},
    "projects": {"project": 14, "milestone": 1.0, "coordinator": 13, "watcher": 9, "ready": 6, "working on": 10,
                 "review": 13, "attention": 1.0, "agents": 1.0, "pool": 10, "gateway": 9, "today": 11},
}
WAITING_COLUMNS = ("status", "pri", "task", "min cap", "room", "for", "posted", "reason")
EVENT_COLUMNS = ("time", "kind", "agent", "task", "detail")
HOST_EVENT_COLUMNS = ("time", "project", "kind", "agent", "task", "detail")
POOL_COLUMNS = ("pool", "working", "holders", "claims")
GATEWAY_COLUMNS = ("time", "project", "agent", "task", "error", "attempt", "outcome", "pool")
MEM_WARN, MEM_ALARM = 0.10, 0.25
CTX_WARN, CTX_ALARM = 0.5, 0.8
CAP_HELP = ("cap and min cap: an agent's capability and a task's minimum (its difficulty), two levels per dot: "
            "◐○○ 1, ●○○ 2, ●◐○ 3, ●●○ 4, ●●◐ 5, ●●● 6")
CPU_HELP = "CPU: the agent's processes in percent of one core; 250% is two and a half cores busy"
MEM_HELP = (f"RAM: the agent's resident memory; yellow from {MEM_WARN:.0%} of system RAM, red from {MEM_ALARM:.0%} "
            f"(the popup shows the share)")
CTX_HELP = (f"context: tokens in the agent's context after its last answer (its transcript, else the pane's /clear "
            f"hint) of the window of the answer's model: the agent CLI's own setting, else [models] context, else the "
            f"profile's context_limit (marked *); yellow from {CTX_WARN:.0%}, red from {CTX_ALARM:.0%}")


def cell(text, style=""):
    return Text(str(text), style=style)


def marked(text, marker_style, text_style="bold", marker="●"):
    shown = Text(f"{marker} ", style=marker_style)
    shown.append(str(text), style=text_style)
    return shown


def row(key, cells, extra=""):
    cells = [c if isinstance(c, Text) else cell(c) for c in cells]
    return key, cells, " ".join([c.plain for c in cells] + [extra]).lower()


def memory(rss, total):
    """An agent's resident memory, colored by its share of system RAM from MEM_WARN and MEM_ALARM."""
    share = rss / total if total else 0
    style = theme.RED if share >= MEM_ALARM else theme.YELLOW if share >= MEM_WARN else ""
    return Text(compact_size(rss), style=style, justify="right")


def compact_size(value):
    """A size in at most five characters: 608M, 1.2G."""
    return f"{value / 2**20:.0f}M" if value < 2**30 else f"{value / 2**30:.1f}G"


def tokens(count):
    if count >= 1000000:
        return f"{count / 1000000:.1f}".rstrip("0").rstrip(".") + "M"
    return f"{count / 1000:.0f}k" if count >= 1000 else str(count)


def context(use):
    """Context in use of its limit (`87k/200k`) after a small meter colored by fullness, or '-'."""
    if not use or not use.get("tokens"):
        return cell("-", "dim")
    limit = use.get("limit") or 0
    fraction = use["tokens"] / limit if limit else 0
    style = theme.RED if fraction >= CTX_ALARM else theme.YELLOW if fraction >= CTX_WARN else theme.GREEN
    shown = charts.meter(fraction, 4, style=style)
    shown.append(f" {tokens(use['tokens'])}/{tokens(limit) if limit else '?'}")
    if use.get("limitSource") == "profile":
        shown.append("*", style="dim")
    if use.get("source") == "pane hint":
        shown.append(" ~", style="dim")
    return shown


def context_detail(use):
    """The context cell plus where its limit comes from, for a popup."""
    shown = context(use)
    if use and use.get("tokens") and use.get("limitSource"):
        shown.append(f"  limit: {LIMIT_SOURCES.get(use['limitSource'], use['limitSource'])}", style="dim")
    return shown




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
        cpu = "-" if agent.get("cpu") is None else Text(f"{agent['cpu']:.0f}%", justify="right")
        mem = "-" if agent.get("rss") is None else memory(agent["rss"], total)
        glow_style = f"bold {theme.KIND.get(glow[0]['kind'], '')}" if glow else ""
        restarts = agent.get("restarts")
        rows.append(row(f"agent:{agent['name']}:{agent['pane']}", [
            marked(agent.get("display") or agent["name"], color), cell(agent["state"], color),
            level_cell(agent.get("capability")), task_cell(agent.get("task"), state), context(agent.get("context")),
            cell(agent.get("room") or "-", theme.ROOM.get(agent.get("room"), "")),
            cell(top.duration(on_task), theme.aged(on_task, top.SLOW, top.VERY_SLOW)),
            cell(top.duration(quiet), theme.aged(quiet, top.QUIET_WARN, 2 * top.QUIET_WARN)),
            cell(top.duration(agent.get("uptime")), "dim"),
            cell("-" if restarts is None else str(restarts), theme.YELLOW if restarts else "dim"),
            cell(agent.get("model") or "-", "dim"), cell(agent.get("pool") or "-", "dim"), cpu, mem,
            cell(note, theme.RED if agent.get("tainted") else glow_style)], agent["name"]))
    for worker in state.get("workers", []):
        on_task = top.age(times.get(worker["task"], (None, None))[1])
        rows.append(row(f"worker:{worker['name']}:{worker['task']}", [
            marked(worker["name"], theme.CYAN, marker="◆"), cell(worker["kind"], theme.CYAN), "", task_cell(worker["task"], state),
            "-", cell(worker.get("room") or "-", theme.ROOM.get(worker.get("room"), "")),
            cell(top.duration(on_task), theme.aged(on_task, top.SLOW, top.VERY_SLOW)), "-", "-", "-",
            cell(worker.get("model") or "-", "dim"), "-", "-", "-", ""]))
    return rows


LEVEL_STYLE = {1: "", 2: "CYAN", 3: "MAGENTA"}


def level_style(level):
    name = LEVEL_STYLE.get(level, "MAGENTA" if level and level > 3 else "")
    return f"bold {getattr(theme, name)}" if name else ""


def task_cell(task_id, state):
    """The task an agent works on, after its difficulty (minimum capability) as dots."""
    if not task_id:
        return cell("-", "bold")
    level = (state.get("capabilities") or {}).get(task_id) or t.LOWEST_CAPABILITY
    shown = Text(pips(level), style=level_style(int(level)))
    shown.append(f" {task_id}", style="bold")
    return shown


LEVEL_DOTS = 3


def pips(level):
    """A capability level on LEVEL_DOTS dots of two levels each: 1 ◐○○, 2 ●○○, 3 ●◐○, 6 ●●●; above that ●●●+."""
    level = int(level)
    full, half = min(level, 2 * LEVEL_DOTS) // 2, min(level, 2 * LEVEL_DOTS) % 2
    return "●" * full + "◐" * half + "○" * (LEVEL_DOTS - full - half) + ("+" if level > 2 * LEVEL_DOTS else "")


def level_cell(level, minimum=False, gated=False):
    """A capability level as dots (pips): an agent's capability, or a task's minimum (its difficulty); a minimum no
    free agent meets is red."""
    if not level:
        return cell("any" if minimum else "-", "dim")
    return Text(pips(level), style=f"bold {theme.RED}" if gated else level_style(int(level)))


def priority_cell(entry):
    """P1 to P5, with the levels the task gained by waiting (P2+1)."""
    level = entry.get("priority")
    if level is None:
        return cell("", "dim")
    text = Text(f"P{level}", style="bold" if level >= 4 else "dim" if level <= 2 else "")
    if entry.get("aging"):
        text.append(f"+{entry['aging']}", style=theme.YELLOW)
    return text


def waiting_rows(state):
    rows = []
    minimums = state.get("capabilities") or {}
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
            marked(label, style, f"bold {style}" if strong else style), priority_cell(entry),
            cell(entry["shown_id"], "bold" if strong else ""),
            level_cell(minimums.get(entry["task"]) or t.LOWEST_CAPABILITY, minimum=True,
                       gated=entry["task"] in (state.get("gated") or {})),
            cell(room, theme.ROOM.get(room, "")), shown, cell(top.posted_label(entry["posted"]), "dim"), reason]))
    return rows


def event_key(record, index):
    return f"event:{record.get('project') or ''}:{record.get('id', f'#{index}')}"


EVENT_SORT = {"time": lambda r: r.get("time") or "", "project": lambda r: (r.get("project") or "").lower(),
              "kind": lambda r: r.get("kind") or "",
              "agent": lambda r: (r.get("agentLabel") or r.get("agent") or "").lower(),
              "task": lambda r: (r.get("task") or "").lower(), "detail": lambda r: top.event_detail(r).lower()}


EVENT_CACHE = {}


def event_row(record, number, project):
    """One event's row, cached by everything it shows, so a refresh rebuilds only new events."""
    token = (theme.GENERATION, project is not None, number, *(record.get(name) for name in
                                                              ("project", "id", "time", "kind", "agent", "agentLabel",
                                                               "task", "detail", "reset", "room")))
    found = EVENT_CACHE.get(token)
    if found is None:
        strong = "bold " if record["kind"] in theme.STRONG_KINDS else ""
        cells = [cell(top.local_clock(record["time"]), "dim"),
                 cell(record["kind"], strong + theme.KIND.get(record["kind"], "")),
                 record.get("agentLabel") or record.get("agent") or "-", cell(record.get("task") or "-", strong.strip()),
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
        leases = sorted((pool.get("leases") or {}).values(), key=lambda lease: lease.get("agentLabel") or "")
        for lease in leases:
            if holders:
                holders.append(", ")
            holders.append(lease.get("agentLabel") or lease.get("agent") or "?",
                           style=f"bold {theme.project_color(lease.get('project') or '')}")
        for name, count in ([] if leases else sorted(pool["projects"].items())):
            if holders:
                holders.append(", ")
            holders.append(name, style=f"bold {theme.project_color(name)}")
            holders.append(f" {count}")
        rows.append(row(f"pool:{pool['pool']}", [
            cell(pool["pool"], "bold"),
            Text.assemble((f"{pool['active']}/{pool['max']}", f"bold {theme.RED}" if full else f"bold {theme.GREEN}"),
                          (f" cap -{pool['capCut']} until {clock(pool['capUntil'])}" if pool.get("capCut") else "",
                           theme.YELLOW)),
            holders if holders else "-", cell(", ".join(pool["claims"]) or "-", theme.YELLOW if pool["claims"] else "")]))
    return rows


def clock(epoch):
    return top.local_clock(datetime.fromtimestamp(epoch, timezone.utc).isoformat()) if epoch else "-"


def pool_state(entry):
    """(text, style) of a gateway pool: paused until, cap cut until, or ok."""
    if entry.get("pausedUntil"):
        return f"paused until {clock(entry['pausedUntil'])}", f"bold {theme.RED}"
    if entry.get("capCut"):
        return f"cap -{entry['capCut']} until {clock(entry['capUntil'])}", theme.YELLOW
    return "ok", theme.GREEN


OUTCOME_STYLE = {"recovered": "GREEN", "resent": "CYAN", "retry": "YELLOW", "stalled": "RED", "fatal": "RED"}


def outcome(error):
    name = error.get("outcome") or "-"
    text = f"retry at {clock(error['next'])}" if name == "retry" and error.get("next") else name
    style = getattr(theme, OUTCOME_STYLE[name]) if name in OUTCOME_STYLE else "dim"
    return cell(text, f"bold {style}" if name in ("stalled", "fatal") else style)


def gateway_rows(errors, now=None):
    """One row per recent gateway error (theseus.gateway.recent), newest first; key gateway:<id>."""
    rows = []
    for error in errors or []:
        project = error.get("project") or "-"
        state, style = pool_state(error)
        rows.append(row(f"gateway:{error['id']}", [
            cell(top.local_clock(error.get("time")), "dim"), cell(project, f"bold {theme.project_color(project)}"),
            error.get("agentLabel") or error.get("agent") or "-", cell(error.get("task") or "-", "bold"),
            cell(error.get("label") or "?", theme.gateway_style(error)),
            cell(f"{error.get('attempt') or '-'}/{error.get('attempts') or '-'}", "dim"), outcome(error),
            Text.assemble((f"{error['pool']} ", "bold"), (state, style))], error.get("excerpt") or ""))
    return rows


def gateway_title(health):
    """The GATEWAY panel title: every pool with errors, a pause or a cap cut, and its state."""
    parts = []
    for entry in health or []:
        state, _ = pool_state(entry)
        parts.append(f"{entry['pool']} {state} · {entry['errors']} err 24 h")
    return "GATEWAY " + (" · ".join(parts) if parts else "24 h ok")


def matches(rows, text):
    text = text.strip().lower()
    return [r for r in rows if text in r[2]] if text else rows


def progress(done, total, width=10):
    line = charts.meter(done / total if total else 0, width, style=theme.GREEN)
    line.append(f" {done}/{total}")
    return line


def labeled(lines):
    """Text of (label, value, style) lines, labels dim."""
    text = Text()
    for number, (label, value, style) in enumerate(lines):
        if number:
            text.append("\n")
        text.append(f"{label} ", style="dim")
        text.append_text(value if isinstance(value, Text) else Text(str(value), style=style or ""))
    return text


def ago(epoch):
    return "-" if not epoch else top.duration(time.time() - epoch) + " ago"


def driver_line(state):
    watcher = state.get("watcher")
    if watcher and watcher.get("alive"):
        wake = max(0, (watcher.get("next") or 0) - time.time())
        return ("driver", f"queue {watcher.get('command')} · tick {ago(watcher.get('tick'))} · next {int(wake)}s",
                theme.GREEN)
    return ("driver", "none: nothing assigns tasks (theseus agents up)", f"bold {theme.YELLOW}")


COORDINATOR_STYLE = {"busy": theme.GREEN, "idle": theme.YELLOW, "waiting": theme.RED, "offline": theme.RED,
                     "failed": theme.RED, "starting": theme.YELLOW}


def push_line(attention):
    """The attention push as the coordinator panel shows it: off, or on with the last line's age and result."""
    if (attention or {}).get("notify") != "pane":
        return "off", "dim"
    problem = attention.get("coordinator")
    if problem:
        return f"on · pane {problem}", f"bold {theme.RED}"
    push = attention.get("push") or {}
    if push.get("owed"):
        return f"on · owed {top.duration(time.time() - push['owed'])}", theme.YELLOW
    if push.get("at"):
        return f"on · last {ago(push['at'])}", theme.GREEN
    return "on · nothing pushed yet", ""


def coordinator_panel(state, width=80, limit=6):
    """The coordinator half of a project's top panel: who it is and how it is doing, then what needs it (the
    attention list, which is its to-do list)."""
    from rich.console import Group
    from rich.table import Table as Grid
    from .. import attention as att
    coordinator = state.get("coordinator")
    attention = state.get("attention") or {}
    if not coordinator:
        head = labeled([("coordinator", "none", theme.YELLOW),
                        ("start", "theseus coordinator up", "dim")])
    else:
        shown = Text(coordinator["state"], style=f"bold {COORDINATOR_STYLE.get(coordinator['state'], '')}")
        if coordinator.get("waiting"):
            shown.append(f" ({coordinator['waiting']})", style=theme.RED)
        who = coordinator.get("profile") or "-"
        if coordinator.get("model"):
            who += f" · {coordinator['model']}"
        lines = [("state", shown, ""), ("profile", who, "")]
        if coordinator.get("supervised"):
            crashes = coordinator.get("crashes") or 0
            lines.append(("session", f"{(coordinator.get('session') or '-')[:8]}"
                          + (f" · {crashes} crash{'es' if crashes != 1 else ''}" if crashes else ""),
                          theme.RED if crashes else "dim"))
        lines += [("active", ago(coordinator.get("lastActivity")), ""),
                  ("context", context(coordinator.get("context")), ""),
                  ("push", *push_line(attention))]
        head = labeled(lines)
    items = attention.get("items") or []
    late = {item["key"] for item in attention.get("escalated") or []}
    todo = Grid.grid(padding=(0, 1), expand=True)
    todo.add_column(no_wrap=True)
    todo.add_column(no_wrap=True, justify="right")
    todo.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    if not items:
        todo.add_row(Text("●", style=theme.GREEN), "", Text("nothing needs the coordinator", style="dim"))
    for item in items[:limit]:
        since = att.epoch(item.get("since")) if item.get("since") else None
        age = top.duration(time.time() - since) if since else "-"
        style = f"bold {theme.RED}" if item["key"] in late else theme.MAGENTA if item["kind"] == "delivery" else ""
        todo.add_row(Text(item["kind"], style=style), Text(age, style="dim"), Text(item.get("summary") or "", style=style))
    if len(items) > limit:
        todo.add_row("", "", Text(f"and {len(items) - limit} more: theseus attention", style="dim"))
    if width >= 90:
        grid = Grid.grid(padding=(0, 2), expand=True)
        grid.add_column(no_wrap=True, overflow="ellipsis", ratio=2)
        grid.add_column(ratio=3)
        grid.add_row(head, todo)
        return grid
    return Group(head, Text(""), todo)


def oldest_review(state):
    entries = [e for e in top.waiting_entries(state) if e["key"] == "review" and e["since"] is not None]
    return max(entries, key=lambda e: e["since"]) if entries else None


def agent_counts(state):
    counts = {}
    for agent in state["agents"]:
        counts[agent["state"]] = counts.get(agent["state"], 0) + 1
    return counts


def pool_line(state):
    parts = []
    for pool in state.get("pools") or []:
        cut = f" -{pool['capCut']}" if pool.get("capCut") else ""
        held = f" held {len(pool['held'])}" if pool.get("held") else ""
        parts.append(f"{pool['pool']} {pool['active']}/{pool['max']}{cut}{held}")
    return " · ".join(parts) or "no pool"


def gateway_line(state):
    for entry in state.get("health") or []:
        if entry.get("onFallback"):
            return f"{entry['profile']} on fallback {entry['fallback']}", theme.RED
    for entry in state.get("gateway") or []:
        text, style = pool_state(entry)
        if text != "ok":
            return text, style
    return "ok", theme.GREEN


def overview(state, width=200):
    """The first panel of a project tab: project, coordinator, queue, agents and today, in aligned labeled columns
    (five side by side on a wide panel, fewer per row on a narrow one)."""
    from rich.console import Group
    from rich.table import Table as Grid
    queue = state["queue"]
    git, verification = state["git"], state["verification"]
    done = state.get("progress")
    milestone = Text(state.get("milestone") or "no milestone", style="bold")
    if done and done[1]:
        milestone.append(" ")
        milestone.append_text(progress(*done, width=8))
    last = verification.get("last")
    verified = (Text("pass", style=theme.GREEN) if last and last["passed"] else Text("FAIL" if last else "never",
                style=f"bold {theme.RED}" if last else theme.YELLOW))
    if last:
        verified.append(f" {top.duration(last['age'])} ago · {verification.get('unverified') or 0} unverified", style="dim")
    project = [("milestone", milestone, ""),
               ("branch", Text.assemble((git["branch"], "bold"), (f" {git['dirty']} dirty" if git["dirty"] else " clean",
                                                                  theme.YELLOW if git["dirty"] else theme.GREEN)), ""),
               ("verified", verified, ""),
               ("export", (state.get("export") or {}).get("state") or "-",
                {"current": theme.GREEN, "stale": theme.YELLOW, "unexported": theme.YELLOW,
                 "edited": f"bold {theme.RED}"}.get(
                    (state.get("export") or {}).get("state"), "dim")),
               ("queue", "PAUSED" if state.get("paused") else "running", f"bold {theme.YELLOW}" if state.get("paused") else ""),
               driver_line(state)]
    review = oldest_review(state)
    issues = top.issues(state)
    work = [("ready", f"{len(queue['ready'])} · held {len(queue['held'])} · blocked {len(queue['blocked'])}", ""),
            ("working on", str(len(queue["in_progress"])), ""),
            ("review", f"{len(queue['review'])}" + (f" · oldest {review['task']} {top.duration(review['since'])}"
                                                    if review else ""), theme.MAGENTA if queue["review"] else ""),
            ("attention", str(len(issues)), f"bold {theme.RED}" if issues else theme.GREEN)]
    counts = agent_counts(state)
    busy = counts.get("working", 0) + counts.get("busy", 0)
    trouble = {k: v for k, v in counts.items() if k in ("blocked", "failed", "waiting", "stalled", "thrashing")}
    text, style = gateway_line(state)
    agents = [("agents", f"{busy} working · {counts.get('free', 0)} free of {len(state['agents'])}", ""),
              ("trouble", ", ".join(f"{v} {k}" for k, v in trouble.items()) or "none",
               f"bold {theme.RED}" if trouble else theme.GREEN),
              ("pool", pool_line(state), ""), ("gateway", text, style)]
    if state.get("supervisionPaused"):
        agents.insert(1, ("supervision", "paused", f"bold {theme.YELLOW}"))
    today = state.get("today") or {}
    last_merge = top.age(today["lastMerge"]) if today.get("lastMerge") else None
    outcomes = [("merged", str(today.get("merged", 0)), "bold"), ("completed", str(today.get("completed", 0)), "bold"),
                ("delivered", str(today.get("delivered", 0)), "bold"),
                ("last merge", "-" if last_merge is None else top.duration(last_merge) + " ago", "")]
    groups = [labeled(lines) for lines in (project, work, agents, outcomes)]
    per_row = 4 if width >= 120 else 2
    grid = Grid.grid(padding=(0, 2), expand=True)
    for _ in range(per_row):
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    for start in range(0, len(groups), per_row):
        chunk = groups[start:start + per_row]
        grid.add_row(*chunk, *[""] * (per_row - len(chunk)))
    if not issues:
        return grid
    shown = [Text.from_ansi(line, no_wrap=True, overflow="ellipsis") for line in issues[:3]]
    return Group(grid, *shown)


def project_rows(projects):
    """One row per project for the Host tab: the key figures of its overview."""
    rows = []
    for item in projects:
        name = item["name"]
        accent = f"bold {theme.project_color(name)}"
        if not item["available"]:
            rows.append(row(f"project:{item['root']}", [cell(name, accent), cell(f"unavailable: {item['error']}", theme.RED),
                                                        *["-"] * (len(PROJECT_COLUMNS) - 2)]))
            continue
        state = item["snapshot"]
        queue = state["queue"]
        done = state.get("progress")
        milestone = Text(state.get("milestone") or "-", style="bold")
        if done and done[1]:
            milestone.append(" ")
            milestone.append_text(progress(*done, width=6))
        coordinator = state.get("coordinator")
        coord = (cell(coordinator["state"], {"busy": theme.GREEN, "idle": theme.YELLOW}.get(coordinator["state"], theme.RED))
                 if coordinator else cell("-", "dim"))
        watcher = state.get("watcher")
        watching = cell(watcher["command"], theme.GREEN) if watcher and watcher.get("alive") else cell("none", theme.YELLOW)
        review = oldest_review(state)
        issues = top.issues(state)
        counts = agent_counts(state)
        busy = counts.get("working", 0) + counts.get("busy", 0)
        trouble = sum(v for k, v in counts.items() if k in ("blocked", "failed", "waiting", "stalled", "thrashing"))
        text, style = gateway_line(state)
        today = state.get("today") or {}
        rows.append(row(f"project:{item['root']}", [
            cell(name, accent), milestone, coord, watching, str(len(queue["ready"])), str(len(queue["in_progress"])),
            cell(f"{len(queue['review'])}" + (f" · {top.duration(review['since'])}" if review else ""),
                 theme.MAGENTA if queue["review"] else ""),
            cell(Text.from_ansi(issues[0]).plain if issues else "none", f"bold {theme.RED}" if issues else theme.GREEN),
            cell(f"{busy}/{len(state['agents'])} working" + (f" · {trouble} trouble" if trouble else ""),
                 f"bold {theme.RED}" if trouble else ""),
            pool_line(state), cell(text, style), f"{today.get('merged', 0)} merged"]))
    return rows


def agent_events(events, name):
    """The events of one agent (None: events without an agent), newest first, one line each."""
    lines = Text()
    for record in reversed(events):
        if record.get("agent") != name:
            continue
        if lines:
            lines.append("\n")
        strong = "bold " if record["kind"] in theme.STRONG_KINDS else ""
        lines.append(f"{top.local_clock(record['time'])} ", style="dim")
        lines.append(f"{record['kind']:<11}", style=strong + theme.KIND.get(record["kind"], ""))
        lines.append(f" {record.get('task') or '-'} ", style="bold")
        lines.append(top.event_detail(record), style="dim")
    return lines or Text("-", style="dim")


def coordinator_text(coordinator):
    detail = Detail().section(f"coordinator {coordinator['name']}")
    detail.add("state", coordinator["state"] + (f" ({coordinator['waiting']})" if coordinator.get("waiting") else ""), "bold")
    detail.add("pane", coordinator.get("pane") or "-")
    detail.add("session", coordinator.get("session") or "-")
    detail.add("profile", coordinator.get("profile") or "-")
    detail.add("model", coordinator.get("model") or "-")
    detail.add("active", ago(coordinator.get("lastActivity")))
    detail.add("context", context_detail(coordinator.get("context")))
    return detail.render()


def attention(state):
    """The coordinator's to-do lines of the plain dashboard, as Rich text."""
    found = top.issues(state)
    if not found:
        return Text.assemble(("● ", theme.GREEN), ("nothing needs the coordinator", "dim"))
    text = Text("\n").join(Text.from_ansi(line) for line in found)
    text.stylize("bold")
    return text


TASK_FIELDS = ("id", "status", "role", "room", "priority", "profile", "minCapability", "owner", "dependencies", "ownedPaths")
TASK_TEXTS = (("objective", "objective"), ("generalContract", "contract"), ("acceptance", "acceptance"))
TASK_LABELS = {"ownedPaths": "owned paths", "dependencies": "depends on", "minCapability": "min capability"}
TASK_STATUS = {"pending": "YELLOW", "in_progress": "GREEN", "completed": "CYAN", "released": "MAGENTA", "failed": "RED",
               "blocked": "RED"}


def task_status_style(status):
    return f"bold {getattr(theme, TASK_STATUS[status])}" if status in TASK_STATUS else "bold"


def value_text(value, style=""):
    """A popup value: lists as one bullet per line, mappings as key: value lines, '-' for nothing."""
    if isinstance(value, (list, tuple)):
        if not value:
            return Text("-", style="dim")
        return Text("\n").join(Text.assemble(("▸ ", theme.CYAN), (str(item), style)) for item in value)
    if isinstance(value, dict):
        return Text("\n").join(Text.assemble((f"{key}: ", "dim"), (str(item), style)) for key, item in value.items())
    if value in (None, ""):
        return Text("-", style="dim")
    return value if isinstance(value, Text) else Text(str(value), style=style)


class Detail:
    """A popup body: sections, each a bold accent rule over a grid of dim keys and bright values or over a block of
    wrapped text."""

    def __init__(self):
        self.parts = []
        self.grid = None

    def section(self, title):
        from rich.rule import Rule
        if self.parts:
            self.parts.append(Text(""))
        heading = Text.assemble(("── ", theme.CYAN), (title, f"bold {theme.CYAN}"))
        self.parts.append(Rule(heading, align="left", style=theme.CYAN, characters="─"))
        self.grid = None
        return self

    def add(self, key, value, style=""):
        from rich.table import Table
        if self.grid is None:
            self.grid = Table.grid(padding=(0, 2), expand=True)
            self.grid.add_column(style="dim", no_wrap=True, min_width=12)
            self.grid.add_column(ratio=1)
            self.parts.append(self.grid)
        self.grid.add_row(key, value_text(value, style))
        return self

    def block(self, value, style=""):
        from rich.padding import Padding
        self.parts.append(Padding(value_text(value, style), (0, 0, 0, 2)))
        self.grid = None
        return self

    def render(self):
        from rich.console import Group
        return Group(*self.parts)


def task_detail(detail, task, entry=None, project=None, title=None):
    detail.section(title or (f"task {task['id']}" if task else "task"))
    if project:
        detail.add("project", project, f"bold {theme.project_color(project)}")
    if entry:
        detail.add("waiting", entry["label"], f"bold {theme.QUEUE.get(entry['label'], theme.GREEN)}")
        if entry.get("reason"):
            detail.add("reason", entry["reason"], theme.REASON.get(entry.get("reason_kind"), ""))
    if not task:
        detail.add("record", "not in this snapshot", "dim")
        return detail
    for name in TASK_FIELDS:
        if name == "minCapability":
            level = t.min_capability(task)
            detail.add(TASK_LABELS[name], f"{pips(level)} {level}" + ("" if task.get(name) else " (default)"), "bold")
        elif name in task:
            detail.add(TASK_LABELS.get(name, name), task[name], task_status_style(task[name]) if name == "status" else "bold")
    extra = [(name, value) for name, value in task.items()
             if name not in TASK_FIELDS and name not in dict(TASK_TEXTS) and value not in (None, "", [], {})]
    for name, value in extra:
        detail.add(name, value if not isinstance(value, dict) else value)
    for name, title in TASK_TEXTS:
        if task.get(name):
            detail.section(title).block(task[name])
    return detail


def task_text(task, entry=None, project=None):
    """Every field of a task record for its popup: the main fields, the other fields (timestamps) and the objective,
    contract and acceptance as wrapped sections."""
    return task_detail(Detail(), task, entry, project).render()


def event_text(record, task=None):
    """An event for its popup: its fields, the detail untruncated and a summary of the linked task."""
    detail = Detail().section(f"event {record.get('kind')}")
    detail.add("time", f"{top.local_clock(record.get('time'))}  ({record.get('time')})")
    if record.get("project"):
        detail.add("project", record["project"], f"bold {theme.project_color(record['project'])}")
    detail.add("kind", record.get("kind"), f"bold {theme.KIND.get(record.get('kind'), '')}")
    for name in ("agent", "task", "room"):
        if record.get(name):
            detail.add(name, record[name], "bold")
    for name, value in record.items():
        if name not in ("time", "project", "kind", "agent", "task", "room", "detail", "epoch") and value not in (None, ""):
            detail.add(name, value)
    detail.section("detail").block(record.get("detail"))
    if task:
        detail.section(f"linked task {task.get('id')}")
        for name in ("status", "role", "room", "owner"):
            if task.get(name):
                detail.add(name, task[name], task_status_style(task[name]) if name == "status" else "bold")
        objective = str(task.get("objective") or "")
        detail.add("objective", objective if len(objective) <= 400 else objective[:400] + " ...")
    return detail.render()


def gateway_text(error):
    """A gateway error for its popup: who, what (typed label, status, kind, subtype), the retry state, the pool and the
    redacted excerpt of the error text."""
    state, style = pool_state(error)
    detail = Detail().section(f"gateway error {error.get('label') or '?'}")
    detail.add("time", f"{top.local_clock(error.get('time'))}  ({error.get('time')})")
    detail.add("project", error.get("project"), f"bold {theme.project_color(error.get('project') or '-')}")
    detail.add("agent", error.get("agent"), "bold").add("task", error.get("task"), "bold")
    detail.add("error", error.get("label"), f"bold {theme.gateway_style(error)}")
    detail.add("status", error.get("code") or "none (no status in the error text)")
    detail.add("kind", error.get("kind")).add("subtype", error.get("subtype"))
    detail.add("retryable", "yes" if error.get("retryable") else "no, fatal",
               theme.GREEN if error.get("retryable") else f"bold {theme.RED}")
    detail.add("attempt", f"{error.get('attempt') or '-'} of {error.get('attempts') or '-'}")
    detail.add("outcome", outcome(error))
    detail.add("pool", Text.assemble((f"{error.get('pool')} ", "bold"), (state, style)))
    detail.section("error text (redacted)").block(error.get("excerpt") or "not recorded (an error from before typed errors)",
                                                  "" if error.get("excerpt") else "dim")
    return detail.render()


def agent_text(agent, task=None, total=0):
    """An agent for its popup: state, supervision facts, profile, CPU, memory, context and taint, then its task."""
    detail = Detail().section(f"agent {agent.get('display') or agent.get('name')}")
    detail.add("state", agent.get("state"), f"bold {theme.STATE.get(agent.get('state'), '')}")
    detail.add("name", agent.get("name"))
    detail.add("profile", agent.get("profile") or agent.get("provider"))
    detail.add("model", agent.get("model") or "-")
    detail.add("launcher", agent.get("command") or "-")
    detail.add("session", (agent.get("session") or "-")[:8])
    detail.add("pool", agent.get("pool") or "-")
    detail.add("pane", agent.get("pane"), "bold")
    if agent.get("managed"):
        detail.add("run", f"gen {agent.get('gen') or '-'} · restarts {agent.get('restarts') or 0} · up "
                          f"{top.duration(agent.get('uptime'))}")
    else:
        detail.add("run", f"up {top.duration(agent.get('uptime'))}")
    detail.add("CPU", "-" if agent.get("cpu") is None else f"{agent['cpu']:.0f}% of one core")
    rss = agent.get("rss")
    detail.add("memory", "-" if rss is None else top.size(rss) + (f"  ({rss / total:.1%} of RAM)" if total else ""))
    detail.add("context", context_detail(agent.get("context")))
    detail.add("worktree", agent.get("worktree") or "-")
    detail.add("room", "dirty" if agent.get("tainted") else "clean", theme.RED if agent.get("tainted") else theme.GREEN)
    if agent.get("task"):
        if task:
            task_detail(detail, task, title=f"current task {task['id']}")
        else:
            detail.section("current task").add("id", agent["task"], "bold")
    return detail.render()
