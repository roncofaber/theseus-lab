"""Table rows for the dashboard: (key, cells, search text) built from snapshots, with no widget code."""
from rich.text import Text

from .. import top
from . import theme

AGENT_COLUMNS = ("agent", "pane", "state", "task", "room", "on task", "quiet", "cpu", "mem", "note")
WAITING_COLUMNS = ("status", "task", "room", "for", "posted", "reason")
EVENT_COLUMNS = ("time", "kind", "agent", "task", "detail")
PROJECT_COLUMNS = ("project", "milestone", "agents", "waiting", "last event", "state")
POOL_COLUMNS = ("pool", "working", "holders", "claims")


def cell(text, style=""):
    return Text(str(text), style=style)


def row(key, cells, extra=""):
    cells = [c if isinstance(c, Text) else cell(c) for c in cells]
    return key, cells, " ".join([c.plain for c in cells] + [extra]).lower()


def agent_rows(state):
    times = state.get("times", {})
    rows = []
    for agent in state["agents"]:
        note = "dirty" if agent.get("tainted") else ""
        glow = next(((r, e) for r, e in top.recent(state, top.GLOW_LABEL) if r.get("agent") == agent["name"]), None)
        if glow:
            note = f"{note} {top.GLOW_LABEL[glow[0]['kind']]}".strip()
        quiet = None if agent.get("idleMinutes") is None else agent["idleMinutes"] * 60
        on_task = top.age(times.get(agent.get("task"), (None, None))[1]) if agent.get("task") else None
        cpu = "-" if agent.get("cpu") is None else f"{agent['cpu']:.0f}%"
        rows.append(row(f"agent:{agent['name']}:{agent['pane']}", [
            cell(agent["name"], "bold"), agent["pane"], cell(agent["state"], theme.STATE.get(agent["state"], "")),
            cell(agent.get("task") or "-", "bold"), cell(agent.get("room") or "-", theme.ROOM.get(agent.get("room"), "")),
            cell(top.duration(on_task), theme.aged(on_task, top.SLOW, top.VERY_SLOW)),
            cell(top.duration(quiet), theme.aged(quiet, top.QUIET_WARN, 2 * top.QUIET_WARN)),
            cpu, top.size(agent.get("rss")), cell(note, "red" if agent.get("tainted") else "")]))
    for worker in state.get("workers", []):
        on_task = top.age(times.get(worker["task"], (None, None))[1])
        rows.append(row(f"worker:{worker['name']}:{worker['task']}", [
            cell(worker["name"], "bold"), "-", cell(worker["kind"], "cyan"), cell(worker["task"], "bold"),
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
        rows.append(row(f"wait:{number}:{entry['task']}", [
            cell(label, theme.QUEUE.get(label, "green")), cell(entry["shown_id"], "bold"),
            cell(room, theme.ROOM.get(room, "")), shown, top.posted_label(entry["posted"]), reason]))
    return rows


def event_rows(events, project=None):
    rows = []
    for number, record in enumerate(reversed(events)):
        cells = [top.local_clock(record["time"]), cell(record["kind"], theme.KIND.get(record["kind"], "")),
                 record.get("agent") or "-", record.get("task") or "-", cell(top.event_detail(record), "dim")]
        if project is not None:
            cells.insert(1, cell(record.get("project") or "-", "bold"))
        rows.append(row(f"event:{number}", cells, record.get("room") or ""))
    return rows


def project_rows(projects):
    from .. import host
    rows = []
    for item in projects:
        if not item["available"]:
            rows.append(row(f"project:{item['root']}", [
                cell(item["name"], "bold"), cell(f"unavailable: {top.cut(item['error'] or '', 70)}", "red"), "", "", "",
                cell("unavailable", "red")]))
            continue
        counts = host.summary_counts(item)
        progress = counts["progress"]
        milestone = f"{counts['milestone'] or '-'}" + (f" {progress[0]}/{progress[1]}" if progress else "")
        waiting = Text()
        styles = {"review": "magenta", "merged": "cyan", "ready": "yellow", "blocked": "dim", "held": "dim"}
        for name, count in counts["waiting"].items():
            if count:
                waiting.append(f"{name} {count}  ", style=styles[name])
        last = top.local_clock(item["lastEvent"]) if item["lastEvent"] else "-"
        state = cell("paused", "yellow") if counts["paused"] else cell("running", "green")
        rows.append(row(f"project:{item['root']}", [cell(item["name"], "bold"), milestone,
                                                    f"{counts['working']}/{counts['registered']}", waiting, last, state]))
    return rows


def pool_rows(state_pools):
    rows = []
    for pool in state_pools:
        full = pool["max"] and pool["active"] >= pool["max"]
        holders = ", ".join(f"{p} {n}" for p, n in sorted(pool["projects"].items())) or "-"
        rows.append(row(f"pool:{pool['pool']}", [
            cell(pool["pool"], "bold"), cell(f"{pool['active']}/{pool['max']}", "red" if full else "green"), holders,
            cell(", ".join(pool["claims"]) or "-", "yellow" if pool["claims"] else "")]))
    return rows


def matches(rows, text):
    text = text.strip().lower()
    return [r for r in rows if text in r[2]] if text else rows
