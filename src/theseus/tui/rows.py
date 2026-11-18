"""Table rows and panel texts for the dashboard: (key, cells, search text) and Rich text built from snapshots, with no
widget code."""
import time
from datetime import datetime, timezone

from rich.text import Text

from .. import host, top
from . import charts, theme

AGENT_COLUMNS = ("agent", "state", "cap", "task", "CPU (core %)", "MEM (RAM)", "context", "room", "on task", "quiet",
                 "up", "restarts", "session", "model", "pool", "note")
PROJECT_COLUMNS = ("project", "milestone", "coordinator", "watcher", "ready", "working on", "review", "attention", "agents",
                   "pool", "gateway", "today")
# Columns that drop out, first to last, when a table is too narrow for all of them.
OPTIONAL = {"agents": ("cap", "model", "pool", "session", "note", "restarts", "up", "room", "quiet", "on task", "context"),
            "projects": ("watcher", "today", "gateway", "pool", "working on", "ready", "attention"),
            "gateway": ("pool", "attempt", "outcome"), "waiting": ("min", "posted", "room")}
# Stable column widths: an int is fixed, a float shares the remaining width in proportion.
WIDTHS = {
    "agents": {"agent": 14, "state": 10, "cap": 3, "task": 1.0, "CPU (core %)": 12, "MEM (RAM)": 11, "context": 14,
               "room": 6, "on task": 8, "quiet": 6, "up": 6, "restarts": 8, "session": 8, "model": 0.8, "pool": 8,
               "note": 0.5},
    "waiting": {"status": 10, "task": 1.0, "min": 3, "room": 6, "for": 8, "posted": 12, "reason": 1.2},
    "events": {"time": 8, "kind": 11, "agent": 16, "task": 0.7, "detail": 1.6},
    "host-events": {"time": 8, "project": 12, "kind": 11, "agent": 24, "task": 0.7, "detail": 1.6},
    "pools": {"pool": 12, "working": 1.0, "holders": 1.5, "claims": 1.0},
    "gateway": {"time": 8, "project": 12, "agent": 24, "task": 0.8, "error": 1.0, "attempt": 8, "outcome": 0.8, "pool": 1.0},
    "projects": {"project": 14, "milestone": 1.0, "coordinator": 13, "watcher": 9, "ready": 6, "working on": 10,
                 "review": 13, "attention": 1.0, "agents": 1.0, "pool": 10, "gateway": 9, "today": 11},
}
WAITING_COLUMNS = ("status", "task", "min", "room", "for", "posted", "reason")
EVENT_COLUMNS = ("time", "kind", "agent", "task", "detail")
HOST_EVENT_COLUMNS = ("time", "project", "kind", "agent", "task", "detail")
POOL_COLUMNS = ("pool", "working", "holders", "claims")
GATEWAY_COLUMNS = ("time", "project", "agent", "task", "error", "attempt", "outcome", "pool")
MEM_WARN, MEM_ALARM = 0.10, 0.25
CTX_WARN, CTX_ALARM = 0.5, 0.8
CPU_HELP = "CPU (core %): the agent's processes in percent of one core; 250% is two and a half cores busy"
MEM_HELP = (f"MEM (RAM): resident memory and its share of system RAM; yellow from {MEM_WARN:.0%}, red from {MEM_ALARM:.0%}")
CTX_HELP = (f"context: tokens in the agent's context after its last answer (Claude Code transcript, else the pane's "
            f"/clear hint) of the profile's context_limit; yellow from {CTX_WARN:.0%}, red from {CTX_ALARM:.0%}")


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
    """An agent's resident memory and its share of system RAM, colored from MEM_WARN and MEM_ALARM."""
    if not total:
        return Text(top.size(rss), justify="right")
    share = rss / total
    style = theme.RED if share >= MEM_ALARM else theme.YELLOW if share >= MEM_WARN else ""
    return Text(f"{top.size(rss)} {share:>3.0%}", style=style, justify="right")


def tokens(count):
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
    if use.get("source") == "pane hint":
        shown.append(" ~", style="dim")
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
            cell("" if agent.get("capability") is None else str(agent["capability"]), "dim"),
            cell(agent.get("task") or "-", "bold"), cpu, mem, context(agent.get("context")),
            cell(agent.get("room") or "-", theme.ROOM.get(agent.get("room"), "")),
            cell(top.duration(on_task), theme.aged(on_task, top.SLOW, top.VERY_SLOW)),
            cell(top.duration(quiet), theme.aged(quiet, top.QUIET_WARN, 2 * top.QUIET_WARN)),
            cell(top.duration(agent.get("uptime")), "dim"),
            cell("-" if restarts is None else str(restarts), theme.YELLOW if restarts else "dim"),
            cell((agent.get("session") or "-")[:8], "dim"), cell(agent.get("model") or "-", "dim"),
            cell(agent.get("pool") or "-", "dim"),
            cell(note, theme.RED if agent.get("tainted") else glow_style)], agent["name"]))
    for worker in state.get("workers", []):
        on_task = top.age(times.get(worker["task"], (None, None))[1])
        rows.append(row(f"worker:{worker['name']}:{worker['task']}", [
            marked(worker["name"], theme.CYAN, marker="◆"), cell(worker["kind"], theme.CYAN), "", cell(worker["task"], "bold"),
            "-", "-", "-", cell(worker.get("room") or "-", theme.ROOM.get(worker.get("room"), "")),
            cell(top.duration(on_task), theme.aged(on_task, top.SLOW, top.VERY_SLOW)), "-", "-", "-", "-",
            cell(worker.get("model") or "-", "dim"), "-", ""]))
    return rows


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
            marked(label, style, f"bold {style}" if strong else style), cell(entry["shown_id"], "bold" if strong else ""),
            cell(str(minimums.get(entry["task"]) or ""), theme.YELLOW if entry["task"] in (state.get("gated") or {}) else ""),
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


def coordinator_lines(state):
    coordinator = state.get("coordinator")
    watcher = state.get("watcher")
    if coordinator:
        style = {"busy": theme.GREEN, "idle": theme.YELLOW, "waiting": theme.RED, "offline": theme.RED}.get(
            coordinator["state"], "")
        shown = coordinator["state"] + (f" ({coordinator['waiting']})" if coordinator.get("waiting") else "")
        lines = [("coordinator", Text.assemble((f"{coordinator['name']} ", "bold"), (shown, f"bold {style}")), ""),
                 ("active", ago(coordinator.get("lastActivity")), ""), ("context", context(coordinator.get("context")), "")]
    else:
        lines = [("coordinator", "-", "dim")]
    if watcher and watcher.get("alive"):
        wake = max(0, (watcher.get("next") or 0) - time.time())
        lines.append(("watcher", f"queue {watcher.get('command')} · tick {ago(watcher.get('tick'))} · next {int(wake)}s",
                      theme.GREEN))
    else:
        lines.append(("watcher", "none", theme.YELLOW))
    return lines


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
        parts.append(f"{pool['pool']} {pool['active']}/{pool['max']}{cut}")
    return " · ".join(parts) or "no pool"


def gateway_line(state):
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
               ("queue", "PAUSED" if state.get("paused") else "running", f"bold {theme.YELLOW}" if state.get("paused") else "")]
    review = oldest_review(state)
    issues = top.issues(state)
    work = [("ready", f"{len(queue['ready'])} · held {len(queue['held'])} · blocked {len(queue['blocked'])}", ""),
            ("working on", str(len(queue["in_progress"])), ""),
            ("review", f"{len(queue['review'])}" + (f" · oldest {review['task']} {top.duration(review['since'])}"
                                                    if review else ""), theme.MAGENTA if queue["review"] else ""),
            ("attention", str(len(issues)), f"bold {theme.RED}" if issues else theme.GREEN)]
    counts = agent_counts(state)
    busy = counts.get("working", 0) + counts.get("busy", 0)
    trouble = {k: v for k, v in counts.items() if k in ("blocked", "failed", "waiting", "offline", "stalled", "thrashing")}
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
    groups = [labeled(lines) for lines in (project, coordinator_lines(state), work, agents, outcomes)]
    per_row = 5 if width >= 150 else 3 if width >= 96 else 2
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
        trouble = sum(v for k, v in counts.items() if k in ("blocked", "failed", "waiting", "offline", "stalled", "thrashing"))
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
    detail.add("active", ago(coordinator.get("lastActivity")))
    detail.add("context", context(coordinator.get("context")))
    return detail.render()


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
        if name in task:
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
    detail.add("context", context(agent.get("context")))
    detail.add("worktree", agent.get("worktree") or "-")
    detail.add("room", "dirty" if agent.get("tainted") else "clean", theme.RED if agent.get("tainted") else theme.GREEN)
    if agent.get("task"):
        if task:
            task_detail(detail, task, title=f"current task {task['id']}")
        else:
            detail.section("current task").add("id", agent["task"], "bold")
    return detail.render()
