"""The one place theseus runs tmux.

Managed agents live on a private tmux server (`tmux -L theseus` by default, [supervisor] socket in the host config;
THESEUS_TMUX_SOCKET names another socket and THESEUS_TMUX_PATH gives a full socket path, for tests). Every command for
that server passes the socket explicitly and never relies on $TMUX. Server.legacy() is the user's own server, used only
for agents registered by hand ([[instances.panes]], `theseus agents add`) and for the explicit open-in-a-window action;
its argv is plain `tmux ...`, unchanged from before the supervisor. When $TMUX points at the private server (inside a
managed agent's pane or one of its hooks), legacy calls drop it, so a hand-made pane id never resolves on the private
server.

A managed agent's address is its session target `=<session>:`, which is unique on the host and names no pane on the
user's server, so a caller that forgets the socket fails instead of typing into an unrelated pane. resolve() maps an
address to (server, target).

The isolated-server test approach (one throwaway server per test on its own socket, killed afterwards) follows
libtmux's pytest plugin (MIT); no libtmux code is used.
"""
import os
import re
import subprocess
from pathlib import Path

DEFAULT_SOCKET = "theseus"
# tmux prints control characters in formats as octal escapes, so fields are joined with printable text.
SEP = "<|>"
SESSION_SAFE = re.compile(r"[^A-Za-z0-9_-]")


class TmuxError(Exception):
    pass


def session_name(project, agent):
    """The tmux session of one managed agent: tmux allows neither '.' nor ':' in session names."""
    return SESSION_SAFE.sub("_", f"{project}--{agent}")


def address(session):
    return f"={session}:"


def is_address(target):
    return bool(target) and target.startswith("=") and target.endswith(":") and "--" in target


def configured():
    """(flag, value) of the private server: ('-S', path) or ('-L', name)."""
    if os.environ.get("THESEUS_TMUX_PATH"):
        return "-S", os.environ["THESEUS_TMUX_PATH"]
    if os.environ.get("THESEUS_TMUX_SOCKET"):
        return "-L", os.environ["THESEUS_TMUX_SOCKET"]
    from . import hostconfig
    try:
        name = (hostconfig.read().get("supervisor") or {}).get("socket") or DEFAULT_SOCKET
    except hostconfig.ConfigError:
        name = DEFAULT_SOCKET
    return "-L", name


def socket_path(flag, value):
    if flag == "-S":
        return Path(value)
    base = os.environ.get("TMUX_TMPDIR") or "/tmp"
    return Path(base) / f"tmux-{os.getuid()}" / value


class Server:
    """One tmux server. Server() is the private server; Server.legacy() the user's."""

    def __init__(self, flag=None, value=None, conf_file=None, env=None):
        if flag is None:
            flag, value = configured()
        if flag not in ("-L", "-S", None):
            raise TmuxError(f"bad socket flag {flag!r}")
        if flag and not value:
            raise TmuxError("a private tmux server needs a socket name or path")
        if flag == "-L" and value == DEFAULT_SOCKET and os.environ.get("THESEUS_TMUX_GUARD"):
            raise TmuxError("refusing the default private tmux socket: THESEUS_TMUX_GUARD is set (a test harness) and "
                            "neither THESEUS_TMUX_PATH nor THESEUS_TMUX_SOCKET names a throwaway server")
        self.flag, self.value = flag, value
        self.conf_file = conf_file
        self.env = env

    @classmethod
    def legacy(cls):
        server = cls.__new__(cls)
        server.flag = server.value = server.conf_file = server.env = None
        return server

    @property
    def is_legacy(self):
        return self.flag is None

    @property
    def name(self):
        """What the store records for this server: the socket name, or 'path:<socket path>'."""
        if self.is_legacy:
            return None
        return self.value if self.flag == "-L" else f"path:{self.value}"

    @classmethod
    def from_name(cls, name):
        if name is None:
            return cls.legacy()
        if name.startswith("path:"):
            return cls("-S", name[5:])
        return cls("-L", name)

    @property
    def path(self):
        return None if self.is_legacy else socket_path(self.flag, self.value)

    def argv(self, *args):
        if self.is_legacy:
            return ["tmux", *args]
        base = ["tmux", self.flag, self.value]
        if self.conf_file:
            base += ["-f", str(self.conf_file)]
        return base + list(args)

    def child_env(self):
        if self.is_legacy:
            env = dict(os.environ)
            if private_tmux(env.get("TMUX")):
                env.pop("TMUX", None)
                env.pop("TMUX_PANE", None)
            return env
        env = dict(self.env) if self.env is not None else dict(os.environ)
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        return env

    def run(self, *args, input=None, check=False, timeout=30):
        try:
            result = subprocess.run(self.argv(*args), capture_output=True, text=True, input=input, env=self.child_env(),
                                    timeout=timeout)
        except FileNotFoundError as error:
            raise TmuxError("tmux is not installed; theseus needs it for persistent agents") from error
        if check and result.returncode != 0:
            raise TmuxError(f"tmux {args[0]}: {(result.stderr or '').strip()[:200]}")
        return result

    def alive(self):
        """Whether the server runs (it has at least one session)."""
        return self.run("list-sessions", "-F", "#{session_name}").returncode == 0

    def has_session(self, session):
        return self.run("has-session", "-t", f"={session}").returncode == 0

    def list_panes(self, fields):
        """One dict per pane on the server with the given format fields (names without #{})."""
        fmt = SEP.join(f"#{{{name}}}" for name in fields)
        result = self.run("list-panes", "-a", "-F", fmt)
        if result.returncode != 0:
            return []
        return [dict(zip(fields, line.split(SEP))) for line in result.stdout.splitlines() if line]

    def display(self, target, fmt):
        """A format expanded for target, or None when the target does not exist: tmux 3.4 display-message exits 0
        with empty output for a missing pane or session, so the pane id is checked as well."""
        result = self.run("display-message", "-p", "-t", target, "#{pane_id}" + SEP + fmt)
        if result.returncode != 0 or SEP not in result.stdout:
            return None
        pane, _, value = result.stdout.rstrip("\n").partition(SEP)
        return value if pane.startswith("%") else None

    def new_session(self, session, argv, cwd, width, height, env=None):
        args = ["new-session", "-d", "-s", session, "-x", str(width), "-y", str(height), "-c", str(cwd)]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        return self.run(*args, "--", *argv, check=True)

    def respawn(self, target, argv, cwd=None, env=None):
        args = ["respawn-pane", "-k", "-t", target]
        if cwd:
            args += ["-c", str(cwd)]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        return self.run(*args, "--", *argv, check=True)

    def kill_session(self, session):
        return self.run("kill-session", "-t", f"={session}")

    def capture(self, target, history=0, ansi=True, join=False):
        args = ["capture-pane", "-p", "-t", target]
        if ansi:
            args.append("-e")
        if join:
            args.append("-J")
        if history:
            args += ["-S", f"-{int(history)}"]
        result = self.run(*args)
        return result.stdout if result.returncode == 0 else None

    def send_literal(self, target, text):
        return self.run("send-keys", "-t", target, "-l", "--", text)

    def paste(self, target, text, buffer="theseus-send"):
        loaded = self.run("load-buffer", "-b", buffer, "-", input=text)
        if loaded.returncode != 0:
            return loaded
        return self.run("paste-buffer", "-p", "-d", "-b", buffer, "-t", target)

    def send_key(self, target, *keys):
        return self.run("send-keys", "-t", target, *keys)

    def set_option(self, target, name, value, scope="-p"):
        return self.run("set-option", scope, "-t", target, name, value)


def private_tmux(value):
    """Whether a $TMUX value points at theseus's private server."""
    if not value:
        return False
    socket = value.split(",")[0]
    try:
        flag, name = configured()
    except TmuxError:
        return False
    return socket == str(socket_path(flag, name))


def resolve(target, server=None):
    """(server, target) for a pane address: a managed agent's `=session:` goes to the private server, anything else
    to the user's server unless server is given."""
    if server is not None:
        return server, target
    if is_address(target):
        return Server(), target
    return Server.legacy(), target
