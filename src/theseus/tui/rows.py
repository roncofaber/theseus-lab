"""Table rows and panel texts for the dashboard: (key, cells, search text) and Rich text built from snapshots, with no
widget code."""
from rich.text import Text

from .. import host, top
from . import charts, theme

AGENT_COLUMNS = ("agent", "pane", "state", "task", "room", "on task", "quiet", "cpu", "mem", "note")
WAITING_COLUMNS = ("status", "task", "room", "for", "posted", "reason")
EVENT_COLUMNS = ("time", "kind", "agent", "task", "detail")
HOST_EVENT_COLUMNS = ("time", "project", "kind", "agent", "task", "detail")
POOL_COLUMNS = ("pool", "working", "holders", "claims")
WAITING_STYLE = {"review": "magenta", "merged": "cyan", "ready": "yellow", "blocked": "dim", "held": "dim"}


def cell(text, style=""):
    return Text(str(text), style=style)


def marked(text, marker_style, text_style="bold", marker="●"):
    shown = Text(f"{marker} ", style=marker_style)
    shown.append(str(text), style=text_style)
    return shown


def row(key, cells, extra=""):
    cells = [c if isinstance(c, Text) else cell(c) for c in cells]
    return key, cells, " ".join([c.plain for c in cells] + [extra]).lower()


def gauge(fraction, label, width=5):
    shown = charts.meter(fraction, width)
    shown.append(f" {label}")
    return shown


def agent_rows(state):
    times = state.get("times", {})
    heaviest = max([a.get("rss") or 0 for a in state["agents"]] + [1])
    rows = []
    for agent in state["agents"]:
        color = theme.STATE.get(agent["state"], "")
        note = "dirty" if agent.get("tainted") else ""
        glow = next(((r, e) for r, e in top.recent(state, top.GLOW_LABEL) if r.get("agent") == agent["name"]), None)
        if glow:
            note = f"{note} {top.GLOW_LABEL[glow[0]['kind']]}".strip()
        quiet = None if agent.get("idleMinutes") is None else agent["idleMinutes"] * 60
        on_task = top.age(times.get(agent.get("task"), (None, None))[1]) if agent.get("task") else None
        cpu = "-" if agent.get("cpu") is None else gauge(agent["cpu"] / 100, f"{agent['cpu']:>3.0f}%")
        mem = "-" if agent.get("rss") is None else gauge(agent["rss"] / heaviest, top.size(agent["rss"]))
        glow_style = f"bold {theme.KIND.get(glow[0]['kind'], '')}" if glow else ""
        rows.append(row(f"agent:{agent['name']}:{agent['pane']}", [
            marked(agent["name"], color), agent["pane"], cell(agent["state"], color),
            cell(agent.get("task") or "-", "bold"), cell(agent.get("room") or "-", theme.ROOM.get(agent.get("room"), "")),
            cell(top.duration(on_task), theme.aged(on_task, top.SLOW, top.VERY_SLOW)),
            cell(top.duration(quiet), theme.aged(quiet, top.QUIET_WARN, 2 * top.QUIET_WARN)),
            cpu, mem, cell(note, "red" if agent.get("tainted") else glow_style)]))
    for worker in state.get("workers", []):
        on_task = top.age(times.get(worker["task"], (None, None))[1])
        rows.append(row(f"worker:{worker['name']}:{worker['task']}", [
            marked(worker["name"], "cyan", marker="◆"), "-", cell(worker["kind"], "cyan"), cell(worker["task"], "bold"),
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
            reason = cell(f"-> {entry['agent'] or '?'}", "green")
            shown = cell(f"{int(entry['elapsed'])}s", "dim")
        else:
            reason = cell(entry["reason"], theme.REASON[entry["reason_kind"]])
            shown = cell(top.duration(entry["since"]),
                         theme.aged(entry["since"], top.SLOW, top.VERY_SLOW) if entry["key"] in ("review", "merged") else "")
        style = theme.QUEUE.get(label, "green")
        strong = label in ("review", "merged", "ready")
        rows.append(row(f"wait:{number}:{entry['task']}", [
            marked(label, style, f"bold {style}" if strong else style), cell(entry["shown_id"], "bold" if strong else ""),
            cell(room, theme.ROOM.get(room, "")), shown, cell(top.posted_label(entry["posted"]), "dim"), reason]))
    return rows


def event_rows(events, project=None):
    rows = []
    for number, record in enumerate(reversed(events)):
        strong = "bold " if record["kind"] in theme.STRONG_KINDS else ""
        cells = [cell(top.local_clock(record["time"]), "dim"),
                 cell(record["kind"], strong + theme.KIND.get(record["kind"], "")),
                 record.get("agent") or "-", cell(record.get("task") or "-", strong.strip()),
                 cell(top.event_detail(record), "dim")]
        if project is not None:
            name = record.get("project") or "-"
            cells.insert(1, cell(name, f"bold {theme.project_color(name)}"))
        rows.append(row(f"event:{number}", cells, record.get("room") or ""))
    return rows


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
            cell(pool["pool"], "bold"), cell(f"{pool['active']}/{pool['max']}", "bold red" if full else "bold green"),
            holders if holders else "-", cell(", ".join(pool["claims"]) or "-", "yellow" if pool["claims"] else "")]))
    return rows


def matches(rows, text):
    text = text.strip().lower()
    return [r for r in rows if text in r[2]] if text else rows


def progress(done, total, width=10):
    line = charts.meter(done / total if total else 0, width, style="green")
    line.append(f" {done}/{total}")
    return line


def card_text(item):
    """The body of a project card on the Host tab."""
    if not item["available"]:
        return Text(f"unavailable: {top.cut(item['error'] or '', 200)}", style="red")
    state = item["snapshot"]
    counts = host.summary_counts(item)
    line = Text()
    line.append(counts["milestone"] or "no milestone", style="bold")
    if counts["progress"] and counts["progress"][1]:
        line.append("  ")
        line.append_text(progress(*counts["progress"]))
    line.append("\n")
    working, registered = counts["working"], counts["registered"]
    line.append("● ", style="green" if working else "yellow" if registered else "dim")
    line.append(f"{working}", style="bold")
    line.append(f" working of {registered} agent(s)")
    stalled = sum(1 for a in state["agents"] if a["state"] in ("stalled", "offline"))
    if stalled:
        line.append(f"  {stalled} stalled/offline", style="bold red")
    line.append("\n")
    waiting = [(name, count) for name, count in counts["waiting"].items() if count]
    for name, count in waiting:
        line.append(f"{name} ", style=WAITING_STYLE[name])
        line.append(f"{count}  ", style=f"bold {WAITING_STYLE[name]}")
    if not waiting:
        line.append("nothing waiting", style="dim")
    line.append("\n")
    pools = state.get("pools") or []
    for pool in pools:
        full = pool["max"] and pool["active"] >= pool["max"]
        line.append(f"pool {pool['pool']} ")
        line.append(f"{pool['active']}/{pool['max']}", style="bold red" if full else "bold green")
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
    line.append("PAUSED" if counts["paused"] else "running", style="bold yellow" if counts["paused"] else "green")
    return line


def attention(state):
    """The coordinator's to-do lines of the plain dashboard, as Rich text."""
    found = top.issues(state)
    if not found:
        return Text.assemble(("● ", "green"), ("nothing needs the coordinator", "dim"))
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
        line.append("   QUEUE PAUSED", style="bold yellow")
    git = state["git"]
    line.append("\n")
    line.append(git["branch"], style="bold")
    line.append(f" {top.cut(git['head'], 60)}   ")
    line.append(f"{git['dirty']} dirty" if git["dirty"] else "clean", style="yellow" if git["dirty"] else "green")
    ahead = "no remote" if git["ahead"] is None else f"{git['ahead']} ahead of remote"
    line.append(f" · {git['worktrees']} worktrees · {ahead}", style="dim")
    verification = state["verification"]
    last = verification["last"]
    line.append("\nverification ")
    if last:
        line.append("pass" if last["passed"] else "FAIL", style="bold green" if last["passed"] else "bold red")
        line.append(f" on {last['commit']} {top.duration(last['age'])} ago")
    else:
        line.append("never", style="yellow")
    over = verification["limit"] and (verification["unverified"] or 0) > verification["limit"]
    line.append(f"   {verification['unverified']} unverified code commits", style="bold red" if over else "")
    line.append(f" (limit {verification['limit'] or '-'})", style="dim")
    for pool in state.get("pools", []):
        full = pool["max"] and pool["active"] >= pool["max"]
        line.append(f"\npool {pool['pool']} ", style="bold")
        line.append(f"{pool['active']}/{pool['max']}", style="bold red" if full else "bold green")
        line.append(f" working · this project {pool['projects'].get(state['project'], 0)}")
        if pool["claims"]:
            line.append(f" · claims: {', '.join(pool['claims'])}", style="yellow")
    today = state.get("today") or {}
    median = today.get("medianHandoff")
    line.append("\ntoday ")
    line.append(str(today.get("delivered", 0)), style="bold")
    line.append(" delivered · ")
    line.append(str(today.get("completed", 0)), style="bold")
    line.append(" completed · median time to handoff ")
    line.append(top.duration(median) if median is not None else "-", style="bold")
    return line
