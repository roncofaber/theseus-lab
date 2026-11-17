"""Host-wide view for `theseus top`: one snapshot per registered project, the shared pools and the merged events, plus
a plain text summary. A project that cannot be loaded is reported as unavailable instead of failing the whole view."""
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config as cfg
from . import pools, registry
from . import top

BIN_SECONDS = 15 * 60
BINS = 48
ACTIVITY_GROUPS = (("assigned", ("assigned",)), ("delivered", ("delivered",)), ("partial", ("partial",)),
                   ("blocked", ("blocked",)), ("merged", ("merged", "completed")), ("errors", ("error", "stalled")))


def activity(events, now=None, bins=BINS, width=BIN_SECONDS):
    """Event counts per time bin for each kind group over the last bins * width seconds, oldest bin first."""
    now = time.time() if now is None else now
    found = {name: [0] * bins for name, _ in ACTIVITY_GROUPS}
    for record in events:
        try:
            happened = datetime.fromisoformat(record["time"].replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError, KeyError):
            continue
        ago = now - happened
        slot = int(ago // width)
        if ago < 0 or slot >= bins:
            continue
        for name, kinds in ACTIVITY_GROUPS:
            if record.get("kind") in kinds:
                found[name][bins - 1 - slot] += 1
    return found


def project_snapshot(root, sample_host=None):
    root = str(root)
    base = {"root": root, "name": Path(root).name, "available": False, "error": None, "snapshot": None, "activity": None,
            "lastEvent": None}
    try:
        conf = cfg.load(root=root)
        from . import workqueue as wq
        state = top.snapshot(conf, host=sample_host)
        history = wq.events(conf)
    except Exception as error:
        base["error"] = f"{type(error).__name__}: {error}"
        return base
    from . import gateway
    return {**base, "name": conf.name, "available": True, "snapshot": state, "activity": activity(history),
            "lastEvent": history[-1]["time"] if history else None, "gatewayAttempts": gateway.settings(conf)["attempts"]}


def pool_snapshots():
    try:
        return [pools.status(name) for name in sorted(pools.definitions())]
    except (OSError, ValueError, cfg.ConfigError):
        return []


def snapshot(roots=None):
    """roots: extra project roots to include besides the registry (for example the current project)."""
    found = {entry["root"]: entry for entry in registry.entries()}
    for root in roots or ():
        found.setdefault(str(root), {"root": str(root)})
    host = top.host_state()
    projects = []
    for root in found:
        item = project_snapshot(root, host)
        item["coordinator"] = found[root].get("coordinator") or ""
        if not item["available"] and found[root].get("name"):
            item["name"] = found[root]["name"]
        projects.append(item)
    projects.sort(key=lambda item: (item["name"], item["root"]))
    events = []
    for item in projects:
        if item["available"]:
            events.extend({**record, "project": item["name"]} for record in item["snapshot"]["events"])
    events.sort(key=lambda record: record.get("time") or "")
    return {"time": datetime.now(timezone.utc).isoformat(), "host": host, "projects": projects,
            "pools": pool_snapshots(), "gateway": top.gateway_health(),
            "gatewayErrors": top.gateway_recent({item["name"]: item.get("gatewayAttempts") for item in projects}),
            "events": events[-300:]}


def summary_counts(item):
    state = item["snapshot"]
    queue = state["queue"]
    working = sum(1 for a in state["agents"] if a["state"] == "working") + len(state["workers"])
    waiting = {key: len(queue[key]) for key in ("review", "merged", "ready", "blocked", "held")}
    progress = state.get("progress")
    return {"working": working, "registered": len(state["agents"]), "waiting": waiting, "progress": progress,
            "milestone": state.get("milestone"), "paused": state.get("paused")}


def render_host(state):
    lines = [f"theseus top - host - {len(state['projects'])} project(s)", ""]
    lines.append(f"{'PROJECT':<22} {'MILESTONE':<22} {'AGENTS':<8} {'WAITING':<34} LAST EVENT")
    for item in state["projects"]:
        if not item["available"]:
            lines.append(f"{top.cut(item['name'], 22):<22} unavailable: {top.cut(item['error'] or '', 60)}")
            continue
        counts = summary_counts(item)
        progress = counts["progress"]
        milestone = f"{counts['milestone'] or '-'} {progress[0]}/{progress[1]}" if progress else (counts["milestone"] or "-")
        waiting = " ".join(f"{k} {v}" for k, v in counts["waiting"].items() if v) or "-"
        paused = " (paused)" if counts["paused"] else ""
        last = top.local_clock(item["lastEvent"]) if item["lastEvent"] else "-"
        lines.append(f"{top.cut(item['name'], 22):<22} {top.cut(milestone, 22):<22} "
                     f"{counts['working']}/{counts['registered']:<6} {top.cut(waiting + paused, 34):<34} {last}")
    if not state["projects"]:
        lines.append("no projects registered yet: run any theseus command inside a project")
    lines += ["", "POOLS"]
    for pool in state["pools"]:
        holders = ", ".join(f"{p} {n}" for p, n in sorted(pool["projects"].items())) or "-"
        claims = f"  claims: {', '.join(pool['claims'])}" if pool["claims"] else ""
        lines.append(f"{pool['pool']:<22} {pool['active']}/{pool['max']} working  {holders}{claims}")
    if not state["pools"]:
        lines.append("no pools defined")
    lines += ["", "GATEWAY"]
    for entry in state.get("gateway") or []:
        lines.append(f"{entry['pool']:<22} {top.gateway_text(entry)}")
    if not state.get("gateway"):
        lines.append("no gateway errors in the last 24 h")
    lines += ["", "EVENTS (newest first)"]
    for record in reversed(state["events"][-15:]):
        lines.append(f"{top.local_clock(record['time'])} {top.cut(record['project'], 16):<16} {record['kind']:<10} "
                     f"{top.cut(record.get('agent') or '-', 16):<16} {top.cut(record.get('task') or '-', 30)}")
    if not state["events"]:
        lines.append("no events yet")
    return "\n".join(lines)
