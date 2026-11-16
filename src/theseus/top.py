"""theseus top: a live dashboard of the host, the agents, the queue, the repository and recent events.

`theseus top [SECONDS]` refreshes in place; `--once` prints one frame (colors stripped when not a terminal); `--json`
prints the snapshot. Every section has a fixed height, so rows never move between frames.
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
ROWS = {"issues": 2, "agents": 6, "queue": 8, "events": 12}
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


def snapshot(conf, sample=False):
    states = ag.states(conf)
    if sample:
        agent_processes(states)
        time.sleep(0.5)
    agent_processes(states)
    summary = wq.summary(conf)
    return {
        "project": conf.name, "milestone": t.Queue(conf).milestone(), "time": datetime.now(timezone.utc).isoformat(),
        "host": host_state(), "agents": states,
        "queue": {k: [x["id"] for x in v] for k, v in summary.items()},
        "rooms": {x["id"]: x.get("room") for v in summary.values() for x in v},
        "blocked": {x["id"]: x["reasons"] for x in summary["blocked"]},
        "times": task_times(conf),
        "paused": wq.paused(conf), "git": git_state(conf), "verification": verification_state(conf),
        "events": wq.events(conf, ROWS["events"]),
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


def render(state, width=120):
    width = max(100, min(width, 180))
    frame = Frame(width)
    found = issues(state)
    left = (f" {B}{MAGENTA}theseus top{R} {DIM}·{R} {B}{state['project']}{R} {DIM}· milestone{R} "
            f"{B}{state['milestone'] or '-'}{R} ")
    status = f" {GREEN}{B}all clear{R} " if not found else f" {YELLOW}{B}{len(found)} to act on{R} "
    frame.top(left, f" {DIM}{socket.gethostname()} {datetime.now():%a %H:%M:%S}{R} {DIM}│{R}{status}")
    frame.row()
    frame.fixed(found, ROWS["issues"], "nothing needs the coordinator")

    host = state["host"]
    mem_used = host["memTotal"] - host["memAvailable"]
    swap_used = host["swapTotal"] - host["swapFree"]
    load_fraction = host["load"][0] / host["cpus"]
    frame.rule("HOST")
    frame.row(f"{B}CPU {R} {bar(load_fraction)} {load_fraction * 100:5.1f}%   load {host['load'][0]:.2f} "
              f"{host['load'][1]:.2f} {host['load'][2]:.2f} on {host['cpus']} threads")
    frame.row(f"{B}MEM {R} {bar(mem_used / host['memTotal'] if host['memTotal'] else 0)} {size(mem_used):>6}   of "
              f"{size(host['memTotal'])}, swap {size(swap_used)} of {size(host['swapTotal'])}")
    if host["gpus"]:
        gpu = host["gpus"][0]
        frame.row(f"{B}GPU {R} {bar(gpu['util'] / 100)} {gpu['util']:5.1f}%   {size(gpu['memUsed'])} of {size(gpu['memTotal'])}, "
                  f"{gpu['temp']:.0f} C  {DIM}{gpu['name']}{R}")
    else:
        frame.row(f"{B}GPU {R} {DIM}no NVIDIA GPU reported{R}")

    gstate, verification = state["git"], state["verification"]
    frame.rule("REPOSITORY AND VERIFICATION")
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
    frame.row(f"last full verification {last_text}   {RED if over else ''}{verification['unverified']} unverified code commits"
              f"{R} {DIM}(limit {verification['limit'] or '-'}){R}")

    total = sum(a.get("rss") or 0 for a in state["agents"])
    frame.rule("AGENTS", f"{DIM}{len(state['agents'])} registered · {size(total)} resident in total{R}")
    frame.header(f"  {'AGENT':<16}  {'PANE':<6} {'STATE':<10} {'TASK':<30} {'ROOM':<6} {'IDLE':<6} {'UP':<7} "
                 f"{'CPU':<6} {'MEM':<7} {'PROCS':<5}  NOTE")
    rows = []
    for agent in state["agents"]:
        color = STATE_COLOR.get(agent["state"], R)
        note = f"{RED}dirty context{R}" if agent.get("tainted") else ""
        cpu = "-" if agent.get("cpu") is None else f"{agent['cpu']:.0f}%"
        idle = "-" if agent.get("idleMinutes") is None else f"{agent['idleMinutes']}m"
        rows.append(f"{color}●{R} {B}{cut(agent['name'], 16):<16}{R}  {agent['pane']:<6} {color}{agent['state']:<10}{R} "
                    f"{cut(agent.get('task') or '-', 30):<30} {agent.get('room') or '-':<6} {idle:<6} "
                    f"{duration(agent.get('uptime')):<7} {cpu:<6} {size(agent.get('rss')):<7} {str(agent.get('procs') or '-'):<5}  {note}")
    frame.fixed(rows, ROWS["agents"], "no registered agents: theseus agents add NAME PANE")

    queue = state["queue"]
    counts = (f"{YELLOW}ready {len(queue['ready'])}{R} {DIM}·{R} {GREEN}working {len(queue['in_progress'])}{R} {DIM}·{R} "
              f"{MAGENTA}review {len(queue['review'])}{R} {DIM}·{R} {CYAN}merged {len(queue['merged'])}{R} {DIM}·{R} "
              f"{DIM}blocked {len(queue['blocked'])} · held {len(queue['held'])}{R}")
    frame.rule("QUEUE", counts + (f"  {YELLOW}{B}PAUSED{R}" if state["paused"] else ""))
    frame.header(f"{'STATUS':<9} {'TASK':<36} {'ROOM':<6} {'FOR':<7} {'POSTED':<7} NEXT STEP")
    rows = []
    for key, label in (("review", "review"), ("merged", "merged"), ("in_progress", "working"), ("ready", "ready"),
                       ("blocked", "blocked"), ("held", "held")):
        color = QUEUE_COLOR[label]
        for task_id in queue[key]:
            detail = ", ".join(state["blocked"].get(task_id, [])) if key == "blocked" else {
                "review": "theseus merge", "merged": "verify, then theseus tasks complete", "held": "hold set",
                "ready": "next free agent",
                "in_progress": next((f"with {a['name']}" for a in state["agents"] if a.get("task") == task_id), "")}.get(key, "")
            posted, since = state["times"].get(task_id, (None, None))
            rows.append(f"{color}{label:<9}{R} {cut(task_id, 36):<36} {state['rooms'].get(task_id) or '-':<6} "
                        f"{duration(age(since)):<7} {posted_label(posted):<7} {DIM}{detail}{R}")
    frame.fixed(rows, ROWS["queue"], "queue empty: write task records (theseus tasks add)")

    frame.rule("EVENTS", f"{DIM}newest first, last {ROWS['events']}{R}")
    frame.header(f"{'TIME':<8} {'KIND':<10} {'AGENT':<18} {'TASK':<30} DETAIL")
    rows = []
    for record in reversed(state["events"][-ROWS["events"]:]):
        color = KIND_COLOR.get(record["kind"], R)
        detail = str(record.get("detail") or "")
        if detail.startswith("/"):
            detail = "worktree " + Path(detail).name
        rows.append(f"{local_clock(record['time'])} {color}{record['kind']:<10}{R} {cut(record['agent'] or '-', 18):<18} "
                    f"{cut(record['task'] or '-', 30):<30} {DIM}{cut(detail, width - 75)}{R}")
    frame.fixed(rows, ROWS["events"], "no events yet")
    frame.bottom()
    return "\n".join(frame.lines)


def main(conf, interval=5, once=False, as_json=False):
    if as_json:
        print(json.dumps(snapshot(conf, sample=True), indent=2, default=str))
        return 0
    if once or not sys.stdout.isatty():
        frame = render(snapshot(conf, sample=True), shutil.get_terminal_size((120, 50)).columns)
        print(frame if sys.stdout.isatty() else ANSI.sub("", frame))
        return 0
    previous, size_seen = [], None
    try:
        sys.stdout.write("\033[?1049h\033[?25l\033[2J")
        while True:
            terminal = shutil.get_terminal_size((120, 50))
            lines = render(snapshot(conf), terminal.columns).splitlines()[: terminal.lines]
            if terminal != size_seen:
                previous, size_seen = [], terminal
                sys.stdout.write("\033[2J")
            sys.stdout.write(repaint(previous, lines))
            sys.stdout.flush()
            previous = lines
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
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
