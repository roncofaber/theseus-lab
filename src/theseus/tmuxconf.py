"""The configuration and status line of theseus's private tmux server, generated here and applied to that server only.

The user's own server is never touched and ~/.tmux.conf is never sourced: its session-persistence plugins must not see
theseus sessions. The host's [tmux] extra_conf is sourced last, on the private server only. Applying the file sets
options and bindings and nothing else, so running agents keep their size, process and pane-died hook; every session
then gets its project's color and role as session options (restyle). The status line reads a small JSON cache that
the queue driver rewrites after each tick (write_status), through `theseus tmux status`, so the 5 s status interval
never runs a query.
"""
import hashlib
import json
import os
import re
import shlex
import time
from pathlib import Path

from . import tmux as tm

PALETTE = {"steel": "#5f87af", "olive": "#87af5f", "tan": "#af875f", "rose": "#af5f87", "teal": "#5fafaf",
           "violet": "#875faf", "gold": "#d7af5f", "sea": "#5faf87", "orange": "#d7875f", "lavender": "#8787d7"}
HEX = re.compile(r"#[0-9a-fA-F]{6}")
KEY = re.compile(r"(?:[CMS]-)*(?:Space|F[0-9]{1,2}|[!-&(-~])")
NEUTRAL = "#4e4e4e"
STALE_SECONDS = 180
KINDS = ("coordinator", "agent", "queue")
CONF_NAME = "theseus-tmux.conf"


class TmuxConfError(Exception):
    pass


# colors

def color_value(value):
    """The #rrggbb of a [project] color (a palette name or #rrggbb), or None when it is neither."""
    text = str(value or "").strip()
    if text.lower() in PALETTE:
        return PALETTE[text.lower()]
    return text.lower() if HEX.fullmatch(text) else None


def derived(name):
    """A stable palette color for a project name."""
    colors = list(PALETTE.values())
    return colors[int(hashlib.sha1(str(name).encode()).hexdigest(), 16) % len(colors)]


def project_color(conf):
    return color_value(conf.section("project").get("color")) or derived(conf.name)


def foreground(color):
    """Black or white, whichever reads better on color."""
    red, green, blue = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    def linear(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    luminance = 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)
    return "#000000" if luminance > 0.18 else "#ffffff"


# settings

def settings():
    from . import hostconfig
    return hostconfig.tmux()


def extra_path(value):
    return Path(os.path.expanduser(str(value))) if value else None


def problems(project=None, host=None):
    """(errors, warnings) of a project's [project] color and the host's [tmux] table."""
    from . import hostconfig
    errors, warnings = [], []
    if project is not None and project.get("color") not in (None, "") and color_value(project.get("color")) is None:
        errors.append(f"[project] color {project.get('color')!r} is neither #rrggbb nor a palette color "
                      f"({', '.join(PALETTE)})")
    host = host if host is not None else {}
    for key in host:
        if key not in hostconfig.TMUX_DEFAULTS:
            errors.append(f"[tmux] unknown key {key}; known: {', '.join(hostconfig.TMUX_DEFAULTS)}")
    prefix = host.get("prefix")
    if prefix is not None and not (isinstance(prefix, str) and KEY.fullmatch(prefix)):
        errors.append(f"[tmux] prefix {prefix!r} is not a tmux key such as C-b, C-s or M-a")
    extra = host.get("extra_conf")
    if extra not in (None, ""):
        if not isinstance(extra, str) or "'" in extra or "\n" in extra:
            errors.append("[tmux] extra_conf must be a file path without quotes")
        elif not extra_path(extra).is_file():
            warnings.append(f"[tmux] extra_conf {extra} does not exist; it is skipped")
        else:
            text = extra_path(extra).read_text(errors="replace")
            if re.search(r"resurrect|continuum|tpm", text):
                warnings.append(f"[tmux] extra_conf {extra} mentions a session-persistence plugin (resurrect, continuum, "
                                f"tpm); the private server must never load one")
    return errors, warnings


# the generated file

def wrapper():
    """A script that runs this same theseus, for the key bindings and the status line."""
    from . import supervisor
    path = supervisor.home() / "theseus"
    text = "#!/bin/sh\nexec " + shlex.join(supervisor.theseus_argv()) + ' "$@"\n'
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
    path.chmod(0o700)
    return path


def quoted(text):
    if "'" in text:
        raise TmuxConfError(f"{text} contains a quote; tmux cannot run it from a key binding")
    return f"'{text}'"


def conf_text():
    """(text, digest) of the private server's configuration. Global window-size manual crashes tmux 3.4 at the first
    new-session, so the fixed agent size comes from default-size and a per-window resize-window instead. Nothing here
    changes the number of rows a pane gets (no pane-border-status, one status line), since that would resize every
    running agent."""
    from . import supervisor
    from . import hostconfig
    s = supervisor.settings()
    host = dict(settings())
    errors, _ = problems(host=host)
    if errors:
        # A bad key must not stop supervision: it falls back to its default and `theseus config check` names it.
        hostconfig.warn_once(f"{hostconfig.path()}: " + "; ".join(errors) + "; using the defaults for those keys")
        if not (isinstance(host.get("prefix"), str) and KEY.fullmatch(host["prefix"])):
            host["prefix"] = hostconfig.TMUX_DEFAULTS["prefix"]
        if not isinstance(host.get("extra_conf"), str) or "'" in host["extra_conf"] or "\n" in host["extra_conf"]:
            host["extra_conf"] = ""
    width, height = supervisor.size()
    hook = str(supervisor.hook_script())
    if "'" in hook or '"' in hook:
        raise TmuxConfError(f"the host data path {hook} contains a quote; tmux hooks cannot run it")
    me = quoted(str(wrapper()))
    detach = str(s.get("detach_key") or "C-q")
    prefix = str(host.get("prefix") or "C-b")
    status = f"{me} tmux status --file '#{{@theseus_status}}'"
    lines = [
        "# Generated by theseus for its private tmux server only; it is rewritten, so put personal additions in the",
        "# host config's [tmux] extra_conf. Never load session-persistence plugins (resurrect, continuum) here.",
        "set -g exit-empty on",
        "set -g remain-on-exit on",
        f"set -g history-limit {int(s.get('history_limit') or 50000)}",
        f"set -g default-size {width}x{height}",
        'set -g default-terminal "tmux-256color"',
        "set -s terminal-features[90] '*:RGB:hyperlinks:extkeys'",
        "set -g allow-passthrough on",
        "set -s extended-keys on",
        "set -s escape-time 0",
        "set -s set-clipboard on",
        "set -g focus-events on",
        "set -g renumber-windows on",
        "set -g base-index 1",
        "setw -g pane-base-index 1",
        "setw -g window-size latest",
        "set -g mouse on",
        "bind -n WheelUpPane if -F -t = '#{||:#{mouse_any_flag},#{pane_in_mode}}' 'send -M -t =' 'copy-mode -e -t ='",
        "setw -g mode-keys vi",
        "bind -T copy-mode-vi v send -X begin-selection",
        # Leaving copy mode after a copy matters: keys typed into a pane in copy mode never reach the agent.
        "bind -T copy-mode-vi y send -X copy-selection-and-cancel",
        f"set -g prefix {quoted(prefix)}",
        "set -g prefix2 None",
        f"bind {quoted(prefix)} send-prefix",
        f"bind -n {quoted(detach)} detach-client",
        "bind d detach-client",
        f"bind r run-shell -b \"{me} tmux apply --socket '#{{socket_path}}' --client '#{{client_name}}'\"",
        f"bind a display-popup -E -w 90% -h 80% -d '#{{@theseus_root}}' -T ' attention ' \"{me} tmux _popup attention\"",
        f"bind T display-popup -E -w 95% -h 90% -d '#{{@theseus_root}}' \"{me} tmux _popup console\"",
        f"bind C run-shell -b \"{me} tmux _goto coordinator --socket '#{{socket_path}}' --client '#{{client_name}}' "
        f"--session '#{{session_name}}'\"",
        f"bind n run-shell -b \"{me} tmux _goto next --socket '#{{socket_path}}' --client '#{{client_name}}' "
        f"--session '#{{session_name}}'\"",
        f"bind p run-shell -b \"{me} tmux _goto previous --socket '#{{socket_path}}' --client '#{{client_name}}' "
        f"--session '#{{session_name}}'\"",
        "bind w choose-tree -Zs -O name -F '#{?@theseus_role,#{@theseus_role},}#{?@theseus_task,  #{@theseus_task},}'",
        "set -g status on",
        "set -g status-interval 5",
        "set -g status-position bottom",
        f"set -g status-style 'bg={NEUTRAL},fg=#ffffff'",
        "set -g status-left-length 60",
        "set -g status-right-length 90",
        "set -g status-left \"#[bold] #{?@theseus_project,#{@theseus_project},theseus} #[nobold]| #{@theseus_role} "
        f"#{{?@theseus_agent,#({status} --left --agent '#{{@theseus_agent}}'),}}\"",
        f"set -g status-right \"#({status} --right)| %H:%M \"",
        "set -g window-status-format ''",
        "set -g window-status-current-format ''",
        "set -g message-style 'bg=#d7af5f,fg=#000000'",
        "set -g pane-border-style 'fg=#444444'",
        "set -g pane-border-format ' #{@theseus_role}#{?@theseus_task, #{@theseus_task},} '",
        "set -g set-titles on",
        "set -g set-titles-string 'theseus #{@theseus_project} #{@theseus_role}#{?@theseus_task, #{@theseus_task},}'",
        f"set-hook -g pane-died \"run-shell -b '{hook} #{{socket_path}} #{{session_name}} #{{pane_pid}} s#{{pane_dead_status}} g#{{pane_dead_signal}}'\"",
        'set-hook -g client-attached "resize-window -A"',
        f'set-hook -g client-detached "resize-window -x {width} -y {height}"',
    ]
    extra = extra_path(host.get("extra_conf"))
    seed = "\n".join(lines)
    if extra and extra.is_file():
        seed += "\n" + hashlib.sha256(extra.read_bytes()).hexdigest()
    digest = hashlib.sha256(seed.encode()).hexdigest()[:16]
    lines.append(f"set -g @theseus_conf {digest}")
    if extra and extra.is_file():
        lines.append(f"source-file {quoted(str(extra))}")
    return "\n".join(lines) + "\n", digest


def conf_path():
    from . import supervisor
    return supervisor.home() / CONF_NAME


def write_conf():
    """Write the configuration file; returns (path, digest)."""
    text, digest = conf_text()
    path = conf_path()
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
    return path, digest


def applied(srv):
    result = srv.run("show-options", "-gqv", "@theseus_conf")
    return result.stdout.strip() if result.returncode == 0 else ""


def ensure(srv):
    """Apply the configuration to a running server that has another (or none, when an older theseus started it).
    Returns whether it applied it."""
    if srv.is_legacy or not srv.alive():
        return False
    _, digest = write_conf()
    if applied(srv) == digest:
        return False
    apply(srv)
    return True


def apply(srv):
    """Source the configuration into the running private server and restyle every session. Returns (path, sessions)."""
    if srv.is_legacy:
        raise TmuxConfError("the theseus tmux configuration never applies to your own tmux server")
    path, _ = write_conf()
    srv.run("source-file", str(path), check=True)
    return path, restyle(srv)


# sessions

def status_path(project):
    from . import supervisor
    folder = supervisor.home() / "status"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder / f"{tm.SESSION_SAFE.sub('_', project)}.json"


def role_label(conf, kind, agent=None):
    if kind == "agent" and agent:
        for prefix in (f"{conf.name}-", f"{conf.prefix}-"):
            if agent.startswith(prefix) and len(agent) > len(prefix):
                return agent[len(prefix):]
        return agent
    return "queue driver" if kind == "queue" else kind


def tag(srv, conf, session, kind, agent=None, task=None):
    """Mark one session with its project, role, color and status cache, as session options (and its window's active
    border), so the status line, the bindings and choose-tree read them. Only options change."""
    color = project_color(conf)
    target = tm.address(session)
    role = role_label(conf, kind, agent)
    values = [("@theseus_project", conf.name), ("@theseus_root", str(conf.root)), ("@theseus_kind", kind),
              ("@theseus_role", role), ("@theseus_coordinator", tm.session_name(conf.name, "coordinator")),
              ("@theseus_color", color), ("@theseus_status", str(status_path(conf.name))),
              ("status-style", f"bg={color},fg={foreground(color)}")]
    args = []
    for key, value in values:
        args += ["set-option", "-t", target, key, value, ";"]
    args += ["set-option", "-w", "-t", target, "pane-active-border-style", f"fg={color}", ";"]
    args += ["set-option", "-p", "-t", target, "@theseus_task", task or "", ";"]
    args += ["select-pane", "-t", target, "-T", f"{role} {task}" if task else role]
    return srv.run(*args).returncode == 0


def restyle(srv):
    """Tag every session on the server from what it already says about itself. Returns the sessions tagged."""
    from . import supervisor
    fields = ("session_name", "@theseus_project", "@theseus_agent", "@theseus_driver", "@theseus_kind", "@theseus_task")
    result = srv.run("list-sessions", "-F", tm.SEP.join(f"#{{{f}}}" for f in fields))
    if result.returncode != 0:
        return []
    confs, done = {}, []
    for line in result.stdout.splitlines():
        info = dict(zip(fields, line.split(tm.SEP)))
        project = info.get("@theseus_project")
        if not project:
            continue
        if project not in confs:
            confs[project] = supervisor.project_conf(project)
        conf = confs[project]
        if conf is None:
            continue
        name = info["session_name"]
        if info.get("@theseus_driver") == "1" or name == tm.session_name(project, "queue"):
            kind = "queue"
        elif info.get("@theseus_agent"):
            kind = "agent"
        elif name == tm.session_name(project, "coordinator"):
            kind = "coordinator"
        else:
            kind = info.get("@theseus_kind") or "agent"
        if tag(srv, conf, name, kind, info.get("@theseus_agent") or None, info.get("@theseus_task") or None):
            done.append(name)
    return done


# the status cache

def write_status(conf, found, states, now=None):
    """The status cache of a project, rewritten by the queue driver after each tick from what it computed anyway:
    the attention count, the pool's working agents over its slots, gateway pauses and cap cuts, each agent's task.
    A task that changed also retitles the agent's pane on the private server."""
    from . import pools, workqueue
    now = time.time() if now is None else now
    path = status_path(conf.name)
    try:
        before = json.loads(path.read_text()).get("tasks") or {}
    except (OSError, ValueError):
        before = {}
    tasks = {info["name"]: info.get("task") for info in states if info.get("task")}
    working, slots = None, None
    try:
        names = pools.project_pools(conf)
        if names:
            found_pools = [pools.status(name) for name in names]
            working = sum(int(p.get("active") or 0) for p in found_pools)
            slots = sum(int(p.get("max") or 0) for p in found_pools) or None
    except Exception:
        names = []
    if working is None:
        working = sum(1 for info in states if info.get("state") == "working")
        slots = len(states)
    gateway = ""
    for item in found:
        if item.get("kind") == "gateway":
            gateway = "paused" if item["key"].endswith(":paused") else (gateway or "cut")
    data = {"project": conf.name, "time": now, "attention": len(found), "working": working, "max": slots,
            "pools": names, "gateway": gateway, "paused": workqueue.paused(conf), "tasks": tasks}
    from . import config as cfg
    cfg.write_atomic(path, json.dumps(data, sort_keys=True) + "\n")
    changed = [info for info in states if info.get("managed") and tasks.get(info["name"]) != before.get(info["name"])]
    if changed:
        from . import supervisor
        srv = supervisor.server()
        for info in changed:
            session = (info.get("pane") or "")[1:-1]
            if session and srv.has_session(session):
                tag(srv, conf, session, "agent", info["name"], tasks.get(info["name"]))
    return data


def render(path, part="right", agent=None, now=None):
    """One part of the status line from the cache file: left is the agent's task, right the attention count (red
    when nonzero), working/max, the gateway state and a stale-driver mark."""
    try:
        data = json.loads(Path(path).read_text()) if path else None
    except (OSError, ValueError):
        data = None
    if part == "left":
        task = ((data or {}).get("tasks") or {}).get(agent or "")
        return f"| {task} " if task else ""
    if data is None:
        return "#[dim]no queue driver #[default]"
    now = time.time() if now is None else now
    count = int(data.get("attention") or 0)
    parts = [f"#[bg=#d70000,fg=#ffffff,bold] {count} attention #[default]" if count else "0 attention"]
    slots = data.get("max")
    parts.append(f"working {data.get('working') or 0}/{slots if slots is not None else '-'}")
    if data.get("gateway"):
        parts.append(f"#[bg=#d7af00,fg=#000000,bold] gateway {data['gateway']} #[default]")
    if data.get("paused"):
        parts.append("queue paused")
    if now - float(data.get("time") or 0) > STALE_SECONDS:
        parts.append("#[bg=#d7af00,fg=#000000] driver stale #[default]")
    return " | ".join(parts) + " "


def describe(path, now=None):
    """A plain line of the cache for a terminal."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return "no status yet: the queue driver writes it after each tick (theseus agents up)"
    now = time.time() if now is None else now
    slots = data.get("max")
    parts = [f"{data.get('attention', 0)} attention", f"working {data.get('working') or 0}/{slots if slots is not None else '-'}"]
    if data.get("gateway"):
        parts.append(f"gateway {data['gateway']}")
    if data.get("paused"):
        parts.append("queue paused")
    return f"{data.get('project')}: {', '.join(parts)}; updated {int(now - float(data.get('time') or 0))} s ago"


# key bindings

def goto(socket, client, session, where):
    """prefix C, n, p: switch the client to the project's coordinator session, or to the next or previous coordinator
    or agent session of the same project. Returns a message for the client, or ''."""
    srv = tm.Server("-S", socket)
    fields = ("session_name", "@theseus_project", "@theseus_kind", "@theseus_coordinator")
    listed = srv.run("list-sessions", "-F", tm.SEP.join(f"#{{{f}}}" for f in fields))
    rows = [dict(zip(fields, line.split(tm.SEP))) for line in listed.stdout.splitlines()] if listed.returncode == 0 else []
    current = next((row for row in rows if row["session_name"] == session), None)
    if current is None or not current.get("@theseus_project"):
        return "this session belongs to no theseus project"
    project = current["@theseus_project"]
    if where == "coordinator":
        target = current.get("@theseus_coordinator") or tm.session_name(project, "coordinator")
        if not any(row["session_name"] == target for row in rows):
            return f"no coordinator session for {project}: theseus coordinator up"
    else:
        ring = sorted((row for row in rows if row.get("@theseus_project") == project
                       and row.get("@theseus_kind") in ("coordinator", "agent")),
                      key=lambda row: (row.get("@theseus_kind") != "coordinator", row["session_name"]))
        names = [row["session_name"] for row in ring]
        if session not in names:
            names.append(session)
        if len(names) < 2:
            return f"no other agent session of {project}"
        step = 1 if where == "next" else -1
        target = names[(names.index(session) + step) % len(names)]
    result = srv.run("switch-client", "-c", client, "-t", tm.address(target))
    return "" if result.returncode == 0 else (result.stderr or "").strip()[:100]
