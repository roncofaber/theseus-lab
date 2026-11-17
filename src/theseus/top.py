"""theseus top: a live dashboard of the host, the agents, the queue, the repository and recent events.

`theseus top [SECONDS]` refreshes in place; `--once` prints one frame (colors stripped when not a terminal); `--json`
prints the snapshot. Sections are sized to their content and the events list takes the remaining height. Keys: q quit,
p pause or resume the queue, space freeze, 1-9 switch tmux to that agent's pane.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import agents as ag
from . import gitutil as g
from . import tasks as t
from . import workqueue as wq

R, B, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = (f"\033[{c}m" for c in (31, 32, 33, 34, 35, 36))
ANSI = re.compile(r"\033\[[0-9;]*m")
STATE_COLOR = {"working": GREEN, "quiet": YELLOW, "busy": CYAN, "free": YELLOW, "delivered": MAGENTA, "stalled": RED, "offline": RED,
               "disabled": DIM}
KIND_COLOR = {"delivered": MAGENTA, "partial": YELLOW, "blocked": RED, "stalled": RED, "error": RED, "assigned": GREEN,
              "nudged": YELLOW, "compacted": BLUE, "cleared": CYAN, "merged": CYAN, "completed": GREEN,
              "reopened": YELLOW, "released": YELLOW}
QUEUE_COLOR = {"review": MAGENTA, "merged": CYAN, "working": GREEN, "ready": YELLOW, "blocked": DIM, "held": DIM}
ROWS = {"events": 12}
SLOW, VERY_SLOW, QUIET_WARN = 3600, 7200, 20 * 60
_CPU_PREVIOUS = {}


# data

def read(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def host_state():
    meminfo = {}
    for line in read("/proc/meminfo").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            meminfo[parts[0].rstrip(":")] = int(parts[1]) * 1024
    try:
        load = list(os.getloadavg())
    except OSError:
        load = [0.0, 0.0, 0.0]
    gpus = []
    if shutil.which("nvidia-smi"):
        try:
            result = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                                     "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
            lines = result.stdout.strip().splitlines()
        except (OSError, subprocess.TimeoutExpired):
            lines = []
        for line in lines:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 5:
                try:
                    gpus.append({"name": parts[0], "util": float(parts[1]), "memUsed": float(parts[2]) * 2**20,
                                 "memTotal": float(parts[3]) * 2**20, "temp": float(parts[4])})
                except ValueError:
                    continue
    return {"cpus": os.cpu_count() or 1, "load": load, "memTotal": meminfo.get("MemTotal", 0),
            "memAvailable": meminfo.get("MemAvailable", 0), "swapTotal": meminfo.get("SwapTotal", 0),
            "swapFree": meminfo.get("SwapFree", 0), "gpus": gpus}


def process_table():
    """pid -> (ppid, rss bytes, cpu ticks, start ticks) for every process."""
    table = {}
    page = os.sysconf("SC_PAGE_SIZE")
    if not os.path.isdir("/proc/self"):
        return table
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        stat = read(f"/proc/{entry}/stat")
        if not stat:
            continue
        fields = stat[stat.rfind(")") + 2:].split()
        try:
            table[int(entry)] = (int(fields[1]), int(fields[21]) * page, int(fields[11]) + int(fields[12]), int(fields[19]))
        except (IndexError, ValueError):
            continue
    return table


def descendants(table, root):
    children = {}
    for pid, values in table.items():
        children.setdefault(values[0], []).append(pid)
    found, stack = [], [root]
    while stack:
        pid = stack.pop()
        if pid in table:
            found.append(pid)
            stack.extend(children.get(pid, []))
    return found


def agent_processes(states):
    """Memory, CPU percent since the previous frame, uptime and process count of each agent's process tree."""
    table = process_table()
    ticks = os.sysconf("SC_CLK_TCK")
    boot = next((float(line.split()[1]) for line in read("/proc/stat").splitlines() if line.startswith("btime")), 0.0)
    now = time.time()
    for info in states:
        info.update({"rss": None, "cpu": None, "uptime": None, "procs": 0})
        result = subprocess.run(["tmux", "display", "-p", "-t", info["pane"], "#{pane_pid}"], capture_output=True, text=True)
        if result.returncode != 0 or not result.stdout.strip().isdigit():
            continue
        shell = int(result.stdout.strip())
        agent_pids = [pid for pid, values in table.items() if values[0] == shell]
        if not agent_pids:
            continue
        pids = [pid for agent in agent_pids for pid in descendants(table, agent)]
        info["rss"] = sum(table[p][1] for p in pids)
        info["procs"] = len(pids)
        info["uptime"] = now - (boot + table[agent_pids[0]][3] / ticks)
        total = sum(table[p][2] for p in pids)
        previous = _CPU_PREVIOUS.get(info["name"])
        if previous and now > previous[1]:
            info["cpu"] = max(0.0, (total - previous[0]) / ticks / (now - previous[1]) * 100)
        _CPU_PREVIOUS[info["name"]] = (total, now)
    return states


def git_state(conf):
    root = conf.root
    head = g.git(root, "log", "-1", "--format=%h %s", check=False).stdout.strip() or "no commits"
    branch = g.git(root, "branch", "--show-current", check=False).stdout.strip() or "-"
    dirty = len([l for l in g.git(root, "status", "--porcelain", check=False).stdout.splitlines() if l.strip()])
    remote = conf.section("git").get("remote") or "origin"
    ahead = None
    if g.has_remote(root, remote):
        counted = g.git(root, "rev-list", "--count", f"{remote}/{branch}..HEAD", check=False)
        ahead = int(counted.stdout.strip()) if counted.stdout.strip().isdigit() else None
    worktrees = len([l for l in g.git(root, "worktree", "list", "--porcelain", check=False).stdout.splitlines()
                     if l.startswith("worktree ")]) - 1
    return {"head": head, "branch": branch, "dirty": dirty, "ahead": ahead, "worktrees": max(worktrees, 0)}


def verification_state(conf):
    from . import verify
    try:
        unverified, limit = verify.debt(conf)
    except Exception:
        return {"unverified": None, "limit": None, "last": None}
    logs = sorted(conf.logs.glob("verify-*.log"), key=lambda p: p.stat().st_mtime) if conf.logs.exists() else []
    last = None
    if logs:
        text = logs[-1].read_text(errors="replace")
        last = {"passed": verify.log_passed(text), "commit": (verify.parse_log_commit(text) or "")[:9],
                "age": time.time() - logs[-1].stat().st_mtime}
    return {"unverified": len(unverified), "limit": limit, "last": last}


POSTED_CACHE = {}


def posted_at(conf, task):
    """When the task was added: its createdAt, or for older records the first commit that mentions it in the task file."""
    if task.get("createdAt"):
        return task["createdAt"]
    if task["id"] not in POSTED_CACHE:
        result = subprocess.run(["git", "-C", str(conf.root), "log", "--reverse", "--format=%cI", "-S", f'"id": "{task["id"]}"',
                                 "--", str(conf.tasks_file.relative_to(conf.root))], capture_output=True, text=True)
        POSTED_CACHE[task["id"]] = (result.stdout.split() or [None])[0]
    return POSTED_CACHE[task["id"]]


def task_times(conf):
    """task id -> (posted, entered its current status) as ISO stamps."""
    times = {}
    for task in t.Queue(conf).load().get("tasks", []):
        execution = task.get("execution") or {}
        posted = posted_at(conf, task)
        since = {"in_progress": execution.get("assignedAt"), "verification": execution.get("deliveredAt"),
                 "merged": (task.get("merge") or {}).get("mergedAt")}.get(task.get("status")) or posted
        times[task["id"]] = (posted, since)
    return times


def age(stamp):
    try:
        return time.time() - datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def posted_label(stamp):
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError):
        return "-"
    return moment.strftime("%H:%M") if moment.date() == datetime.now().astimezone().date() else moment.strftime("%b %d")


def milestone_progress(conf):
    """(done, total) for the current milestone: merged or completed tasks over all tasks of that milestone."""
    queue = t.Queue(conf)
    current = queue.milestone()
    if not current:
        return None
    in_scope = [x for x in queue.all() if x.get("milestone") in (None, "", current)
                and (x.get("milestone") == current or x.get("status") != "completed")]
    done = sum(1 for x in in_scope if x.get("status") in ("merged", "completed"))
    return done, len(in_scope)


def today_stats(history):
    """Deliveries and completions since local midnight, and the median minutes from assignment to delivery."""
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    assigned, handoffs, delivered, completed = {}, [], 0, 0
    for record in history:
        try:
            moment = datetime.fromisoformat(record["time"].replace("Z", "+00:00"))
        except (ValueError, AttributeError, KeyError):
            continue
        if record["kind"] == "assigned":
            assigned[record.get("task")] = moment
        elif record["kind"] in ("delivered", "partial", "blocked") and moment >= midnight:
            delivered += 1
            start = assigned.get(record.get("task"))
            if start:
                handoffs.append((moment - start).total_seconds())
        elif record["kind"] == "completed" and moment >= midnight:
            completed += 1
    handoffs.sort()
    median = handoffs[len(handoffs) // 2] if handoffs else None
    return {"delivered": delivered, "completed": completed, "medianHandoff": median}


def merge_resets(history):
    """Fold a compacted or cleared event into the assignment that follows it for the same agent."""
    out = []
    for record in history:
        previous = out[-1] if out else None
        if (record["kind"] == "assigned" and previous and previous["kind"] in ("compacted", "cleared")
                and previous.get("agent") == record.get("agent")):
            out[-1] = {**record, "reset": "compact" if previous["kind"] == "compacted" else "clear"}
        else:
            out.append(record)
    return out


def other_workers(conf, states):
    """In-progress tasks no registered agent holds: one-shot headless workers and coordinator-launched subagents."""
    from . import profiles
    held = {a.get("task") for a in states}
    workers = []
    for task in t.Queue(conf).load().get("tasks", []):
        if task.get("status") != "in_progress" or task["id"] in held:
            continue
        execution = task.get("execution") or {}
        provider = execution.get("provider") or ""
        try:
            kind = "subagent" if profiles.resolve(conf, provider).get("headless") is None else "headless"
        except profiles.ProfileError:
            kind = "worker"
        workers.append({"name": t.agent_of(task) or task["id"], "task": task["id"], "room": task.get("room"),
                        "kind": kind, "model": execution.get("model") or provider})
    return workers


def snapshot(conf, sample=False):
    states = ag.states(conf)
    if sample:
        agent_processes(states)
        time.sleep(0.5)
    agent_processes(states)
    summary = wq.summary(conf)
    return {
        "project": conf.name, "milestone": t.Queue(conf).milestone(), "time": datetime.now(timezone.utc).isoformat(),
        "host": host_state(), "agents": states, "workers": other_workers(conf, states),
        "queue": {k: [x["id"] for x in v] for k, v in summary.items()},
        "rooms": {x["id"]: x.get("room") for v in summary.values() for x in v},
        "blocked": {x["id"]: x["reasons"] for x in summary["blocked"]},
        "times": task_times(conf),
        "paused": wq.paused(conf), "git": git_state(conf), "verification": verification_state(conf),
        "events": merge_resets(wq.events(conf, 200)),
        "today": today_stats(wq.events(conf)), "progress": milestone_progress(conf),
    }


# presentation

def visible(text):
    return len(ANSI.sub("", text))


def cut(text, width):
    text = str(text)
    return text if len(text) <= width else text[: max(width - 1, 0)] + "~"


def pad(text, width):
    return text + " " * max(0, width - visible(text))


def size(value):
    if value is None:
        return "-"
    for unit, scale in (("G", 2**30), ("M", 2**20), ("K", 2**10)):
        if value >= scale:
            return f"{value / scale:.1f}{unit}"
    return f"{int(value)}B"


def duration(seconds):
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d{seconds % 86400 // 3600:02d}h"


def local_clock(stamp):
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except (ValueError, AttributeError):
        return str(stamp)[11:19]


def bar(fraction, width=18):
    fraction = min(max(fraction, 0.0), 1.0)
    color = RED if fraction > 0.9 else YELLOW if fraction > 0.7 else GREEN
    filled = round(fraction * width)
    return f"{color}{'█' * filled}{DIM}{'░' * (width - filled)}{R}"


class Frame:
    def __init__(self, width):
        self.width = width
        self.lines = []

    def top(self, left, right):
        fill = self.width - 4 - visible(left) - visible(right)
        self.lines.append(f"{DIM}┌─{R}{left}{DIM}{'─' * max(fill, 1)}{R}{right}{DIM}─┐{R}")

    def rule(self, title, detail=""):
        self.row()
        head = f" {B}{BLUE}{title}{R} " + (f"{detail} " if detail else "")
        fill = self.width - 3 - visible(head)
        self.lines.append(f"{DIM}├─{R}{head}{DIM}{'─' * max(fill, 0)}┤{R}")
        self.row()

    def row(self, text=""):
        inner = self.width - 4
        if visible(text) > inner:
            text = ANSI.sub("", text)[: inner - 1] + "~"
        self.lines.append(f"{DIM}│{R} {pad(text, inner)} {DIM}│{R}")

    def header(self, text):
        self.row(f"{DIM}{B}{text}{R}")

    def fixed(self, rows, count, empty=""):
        if len(rows) > count:
            rows = rows[: count - 1] + [f"{DIM}+{len(rows) - count + 1} more{R}"]
        if not rows and empty:
            rows = [f"{DIM}{empty}{R}"]
        for line in rows + [""] * (count - len(rows)):
            self.row(line)

    def bottom(self):
        self.row()
        self.lines.append(f"{DIM}└{'─' * (self.width - 2)}┘{R}")


def issues(state):
    found = []
    for agent in state["agents"]:
        if agent["state"] in ("stalled", "offline"):
            found.append(f"{RED}●{R} {agent['name']} is {agent['state']}")
    review = state["queue"]["review"]
    if review:
        found.append(f"{MAGENTA}●{R} {len(review)} delivery(ies) to review and merge: {', '.join(review[:3])}")
    merged = state["queue"]["merged"]
    if merged:
        found.append(f"{CYAN}●{R} {len(merged)} merged task(s) to verify and complete: {', '.join(merged[:3])}")
    verification = state["verification"]
    if verification["limit"] and verification["unverified"] and verification["unverified"] > verification["limit"]:
        found.append(f"{YELLOW}●{R} {verification['unverified']} code commits since the last full verification")
    free = [a for a in state["agents"] if a["state"] == "free"]
    if free and not state["queue"]["ready"]:
        found.append(f"{YELLOW}●{R} {len(free)} free agent(s) and no ready task: write task records")
    return found


def aged(seconds, warn, alarm):
    text = duration(seconds)
    if seconds is None:
        return text
    color = RED if seconds >= alarm else YELLOW if seconds >= warn else ""
    return f"{color}{text}{R if color else ''}"


def column(text, width):
    return text + " " * max(0, width - visible(text))


def progress_text(progress):
    if not progress or not progress[1]:
        return ""
    done, total = progress
    filled = round(10 * done / total)
    return f" {GREEN}{'█' * filled}{DIM}{'░' * (10 - filled)}{R} {done}/{total} done"


def day_label(stamp):
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%a %b %d")
    except (ValueError, AttributeError):
        return None


def render(state, width=120, height=None, ui=None):
    ui = ui or {}
    width = max(80, min(width, 180))
    frame = Frame(width)
    found = issues(state)
    wide, medium, roomy = width >= 125, width >= 110, width >= 100

    status = f" {GREEN}{B}all clear{R} " if not found else f" {YELLOW}{B}{len(found)} to act on{R} "
    if state["paused"]:
        status = f" {YELLOW}{B}QUEUE PAUSED{R} {DIM}│{R}" + status
    if ui.get("frozen"):
        status = f" {CYAN}{B}FROZEN{R} {DIM}│{R}" + status
    left = (f" {B}{MAGENTA}theseus top{R} {DIM}·{R} {B}{state['project']}{R} {DIM}·{R} "
            f"{B}{state['milestone'] or 'no milestone'}{R}{progress_text(state.get('progress'))} ")
    clock = f" {DIM}{socket.gethostname()} {datetime.now():%a %H:%M:%S}{R} {DIM}│{R}" if medium else ""
    frame.top(left, clock + status)

    if found:
        frame.rule("ACTION NEEDED")
        for line in found:
            frame.row(line)

    host = state["host"]
    mem_used = host["memTotal"] - host["memAvailable"]
    swap_used = host["swapTotal"] - host["swapFree"]
    load_fraction = host["load"][0] / host["cpus"] if host["cpus"] else 0
    frame.rule("HOST")
    frame.row(f"{B}CPU {R} {bar(load_fraction)} {load_fraction * 100:5.1f}%   load {host['load'][0]:.2f} "
              f"{host['load'][1]:.2f} {host['load'][2]:.2f} on {host['cpus']} threads")
    frame.row(f"{B}MEM {R} {bar(mem_used / host['memTotal'] if host['memTotal'] else 0)} {size(mem_used):>6}   of "
              f"{size(host['memTotal'])}, swap {size(swap_used)} of {size(host['swapTotal'])}")
    if host["gpus"]:
        gpu = host["gpus"][0]
        frame.row(f"{B}GPU {R} {bar(gpu['util'] / 100)} {gpu['util']:5.1f}%   {size(gpu['memUsed'])} of {size(gpu['memTotal'])}, "
                  f"{gpu['temp']:.0f} C  {DIM}{gpu['name']}{R}")

    gstate, verification = state["git"], state["verification"]
    frame.rule("PROJECT")
    ahead = "no remote" if gstate["ahead"] is None else f"{gstate['ahead']} ahead of remote"
    dirty = f"{YELLOW}{gstate['dirty']} dirty{R}" if gstate["dirty"] else f"{GREEN}clean{R}"
    frame.row(f"{B}{gstate['branch']}{R} {cut(gstate['head'], width - 70)}   {dirty} {DIM}·{R} {gstate['worktrees']} worktrees "
              f"{DIM}·{R} {ahead}")
    last = verification["last"]
    if last:
        result = f"{GREEN}pass{R}" if last["passed"] else f"{RED}FAIL{R}"
        last_text = f"{result} on {last['commit']} {duration(last['age'])} ago"
    else:
        last_text = f"{YELLOW}never{R}"
    over = verification["limit"] and (verification["unverified"] or 0) > verification["limit"]
    frame.row(f"verification {last_text}   {RED if over else ''}{verification['unverified']} unverified code commits"
              f"{R if over else ''} {DIM}(limit {verification['limit'] or '-'}){R}")
    today = state.get("today") or {}
    median = today.get("medianHandoff")
    frame.row(f"today {B}{today.get('delivered', 0)}{R} delivered {DIM}·{R} {B}{today.get('completed', 0)}{R} completed "
              f"{DIM}·{R} median time to handoff {B}{duration(median) if median is not None else '-'}{R}")

    total = sum(a.get("rss") or 0 for a in state["agents"])
    times = state.get("times", {})
    frame.rule("AGENTS", f"{DIM}{len(state['agents'])} registered · {size(total)} resident in total{R}")
    task_w = max(12, min(28, width - 4 - 73 - (14 if roomy else 0) - (14 if wide else 0)))
    head = f"  {'#':<1} {'AGENT':<16} {'PANE':<6} {'STATE':<10} {'TASK':<{task_w}} {'ROOM':<6} {'ON TASK':<8} {'QUIET':<6} "
    if wide:
        head += f"{'UP':<7} "
    if roomy:
        head += f"{'CPU':<5} {'MEM':<7} "
    if wide:
        head += f"{'PROCS':<5} "
    frame.header(head + "NOTE")
    rows = []
    for number, agent in enumerate(state["agents"], 1):
        color = STATE_COLOR.get(agent["state"], R)
        note = f"{RED}dirty{R}" if agent.get("tainted") else ""
        cpu = "-" if agent.get("cpu") is None else f"{agent['cpu']:.0f}%"
        quiet_seconds = None if agent.get("idleMinutes") is None else agent["idleMinutes"] * 60
        on_task = age(times.get(agent.get("task"), (None, None))[1]) if agent.get("task") else None
        key = str(number) if number <= 9 else " "
        line = (f"{color}●{R} {DIM}{key}{R} {B}{cut(agent['name'], 16):<16}{R} {agent['pane']:<6} {color}{agent['state']:<10}{R} "
                f"{cut(agent.get('task') or '-', task_w):<{task_w}} {agent.get('room') or '-':<6} "
                f"{column(aged(on_task, SLOW, VERY_SLOW), 8)} {column(aged(quiet_seconds, QUIET_WARN, 2 * QUIET_WARN), 6)} ")
        if wide:
            line += f"{duration(agent.get('uptime')):<7} "
        if roomy:
            line += f"{cpu:<5} {size(agent.get('rss')):<7} "
        if wide:
            line += f"{str(agent.get('procs') or '-'):<5} "
        rows.append(line + note)
    for worker in state.get("workers", []):
        on_task = age(times.get(worker["task"], (None, None))[1])
        rows.append(f"{CYAN}◆{R}   {B}{cut(worker['name'], 16):<16}{R} {'-':<6} {CYAN}{worker['kind']:<10}{R} "
                    f"{cut(worker['task'], task_w):<{task_w}} {worker.get('room') or '-':<6} "
                    f"{column(aged(on_task, SLOW, VERY_SLOW), 8)} {DIM}{cut(worker.get('model') or '', 20)}{R}")
    frame.fixed(rows, max(len(rows), 1), "no registered agents: theseus agents add NAME PANE")

    queue = state["queue"]
    counts = (f"{GREEN}working {len(queue['in_progress'])}{R} {DIM}·{R} {MAGENTA}review {len(queue['review'])}{R} {DIM}·{R} "
              f"{CYAN}merged {len(queue['merged'])}{R} {DIM}·{R} {YELLOW}ready {len(queue['ready'])}{R} {DIM}·{R} "
              f"{DIM}blocked {len(queue['blocked'])} · held {len(queue['held'])}{R}")
    frame.rule("WAITING", counts)
    frame.header(f"{'STATUS':<8} {'TASK':<34} {'ROOM':<6} {'FOR':<7} " + (f"{'POSTED':<7} " if medium else "") + "NEXT STEP")
    rows = []
    for key, label in (("review", "review"), ("merged", "merged"), ("ready", "ready"), ("blocked", "blocked"), ("held", "held")):
        color = QUEUE_COLOR[label]
        for task_id in queue[key]:
            detail = ", ".join(state["blocked"].get(task_id, [])) if key == "blocked" else {
                "review": "theseus merge", "merged": "verify, then theseus tasks complete", "held": "hold set",
                "ready": "next free agent"}[key]
            posted, since = times.get(task_id, (None, None))
            waiting = age(since)
            shown = aged(waiting, SLOW, VERY_SLOW) if key in ("review", "merged") else duration(waiting)
            rows.append(f"{color}{label:<8}{R} {cut(task_id, 34):<34} {state['rooms'].get(task_id) or '-':<6} {column(shown, 7)} "
                        + (f"{posted_label(posted):<7} " if medium else "") + f"{DIM}{detail}{R}")
    if rows:
        frame.fixed(rows, len(rows))
    elif queue["in_progress"]:
        frame.row(f"{DIM}nothing waiting; {len(queue['in_progress'])} task(s) in progress (see AGENTS){R}")
    else:
        frame.row(f"{DIM}queue empty: write task records (theseus tasks add){R}")

    footer = 2
    events_rows = ROWS["events"] if height is None else max(4, height - len(frame.lines) - 4 - footer)
    frame.rule("EVENTS", f"{DIM}newest first{R}")
    frame.header(f"{'TIME':<8} {'KIND':<10} {'AGENT':<18} {'TASK':<30} DETAIL")
    rows, last_day = [], day_label(datetime.now(timezone.utc).isoformat())
    for record in reversed(state["events"]):
        if len(rows) >= events_rows:
            break
        day = day_label(record["time"])
        if day and day != last_day:
            rows.append(f"{DIM}── {day} {'─' * 20}{R}")
            last_day = day
        color = KIND_COLOR.get(record["kind"], R)
        detail = str(record.get("detail") or "")
        if record["kind"] == "assigned" and ":" in detail and "/" in detail:
            detail = "worktree " + Path(detail.split(":", 1)[1].strip()).name
        elif detail.startswith("/"):
            detail = "worktree " + Path(detail).name
        if record["kind"] in ("delivered", "partial", "blocked"):
            detail = re.sub(r";?\s*review and merge: theseus merge \S+", "", detail).strip() or "ready to merge"
        if record.get("reset"):
            detail = f"after {record['reset']} · {detail}"
        rows.append(f"{local_clock(record['time'])} {color}{record['kind']:<10}{R} {cut(record['agent'] or '-', 18):<18} "
                    f"{cut(record['task'] or '-', 30):<30} {DIM}{cut(detail, width - 75)}{R}")
    frame.fixed(rows[:events_rows], events_rows, "no events yet")
    frame.row()
    keys = "q quit · p pause/resume queue · space freeze"
    if state["agents"]:
        keys += f" · 1-{min(len(state['agents']), 9)} jump to agent pane"
    message = ui.get("message")
    frame.row(f"{DIM}{keys}{R}" + (f"   {YELLOW}{message}{R}" if message else ""))
    frame.lines.append(f"{DIM}└{'─' * (width - 2)}┘{R}")
    return "\n".join(frame.lines)


def own_client():
    """The tmux client displaying this dashboard's pane, or None; never another terminal's client."""
    pane = os.environ.get("TMUX_PANE")
    if not os.environ.get("TMUX") or not pane:
        return None
    session = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#{session_id}"], capture_output=True, text=True)
    if session.returncode != 0 or not session.stdout.strip():
        return None
    clients = subprocess.run(["tmux", "list-clients", "-t", session.stdout.strip(), "-F", "#{client_name}"],
                             capture_output=True, text=True).stdout.split()
    return clients[0] if len(clients) == 1 else None


def jump_to(pane):
    client = own_client()
    if client is None:
        return "jump needs theseus top in a tmux session with exactly one attached client"
    result = subprocess.run(["tmux", "switch-client", "-c", client, "-t", pane], capture_output=True, text=True)
    if result.returncode != 0:
        return f"tmux: {result.stderr.strip()[:80]}"
    return f"switched to {pane}"


def handle_key(conf, key, state, ui):
    if key in ("q", "Q", "\x03"):
        return False
    if key in ("p", "P"):
        ui["message"] = wq.set_paused(conf, not wq.paused(conf))
    elif key == " ":
        ui["frozen"] = not ui.get("frozen")
        ui["message"] = "frozen; space to resume" if ui["frozen"] else ""
    elif key.isdigit() and key != "0" and state:
        index = int(key) - 1
        if index < len(state["agents"]):
            ui["message"] = jump_to(state["agents"][index]["pane"])
    return True


def main(conf, interval=5, once=False, as_json=False):
    if as_json:
        print(json.dumps(snapshot(conf, sample=True), indent=2, default=str))
        return 0
    if once or not sys.stdout.isatty():
        frame = render(snapshot(conf, sample=True), shutil.get_terminal_size((120, 50)).columns)
        print(frame if sys.stdout.isatty() else ANSI.sub("", frame))
        return 0
    import select
    import termios
    import tty
    stdin = sys.stdin.fileno()
    interactive = sys.stdin.isatty()
    saved = termios.tcgetattr(stdin) if interactive else None
    previous, size_seen, state, ui, next_refresh = [], None, None, {}, 0.0
    try:
        if interactive:
            tty.setcbreak(stdin)
        sys.stdout.write("\033[?1049h\033[?25l\033[2J")
        while True:
            terminal = shutil.get_terminal_size((120, 50))
            if state is None or (not ui.get("frozen") and time.monotonic() >= next_refresh):
                state = snapshot(conf)
                next_refresh = time.monotonic() + interval
            lines = render(state, terminal.columns, terminal.lines, ui).splitlines()[: terminal.lines]
            if terminal != size_seen:
                previous, size_seen = [], terminal
                sys.stdout.write("\033[2J")
            sys.stdout.write(repaint(previous, lines))
            sys.stdout.flush()
            previous = lines
            wait = max(0.1, next_refresh - time.monotonic()) if not ui.get("frozen") else 1.0
            ready = select.select([stdin], [], [], wait)[0] if interactive else time.sleep(wait)
            if ready:
                key = os.read(stdin, 1).decode(errors="ignore")
                if not handle_key(conf, key, state, ui):
                    return 0
                if key in ("p", "P"):
                    next_refresh = 0.0
    except KeyboardInterrupt:
        return 0
    finally:
        if saved is not None:
            termios.tcsetattr(stdin, termios.TCSADRAIN, saved)
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()


def repaint(previous, lines):
    """Escape sequence that rewrites only the changed lines, inside one synchronized update (DEC mode 2026) so the
    terminal shows the new frame at once instead of drawing it line by line."""
    out = ["\033[?2026h"]
    for number, line in enumerate(lines):
        if number >= len(previous) or previous[number] != line:
            out.append(f"\033[{number + 1};1H{line}\033[K")
    if len(previous) > len(lines):
        out.append(f"\033[{len(lines) + 1};1H\033[J")
    out.append("\033[?2026l")
    return "".join(out)
