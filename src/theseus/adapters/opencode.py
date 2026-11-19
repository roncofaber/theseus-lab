"""The "opencode-sqlite" transcript adapter (theseus.adapters): OpenCode's own session store. OpenCode 2 keeps every
session in one SQLite database (<data>/opencode/opencode.db: OPENCODE_DB, else $XDG_DATA_HOME/opencode, else
~/.local/share/opencode); a session is a `session_v2` row and its conversation the `session_message` rows of that
session in `seq` order, each a JSON document of type user, assistant, compaction or idle (the end of a turn, with
its outcome and error).

The database names no process or tmux pane, so only a supervised agent's session is found, by the id theseus
preassigned. The database is opened read-only, and each read asks only for the rows after the last one consumed. An
assistant or compaction row is rewritten while it streams, so reading stops before the first unfinished one until it
completes or a later idle row shows the turn ended.

The context limit is the one OpenCode itself uses for the answer's model: providers.<provider>.models.<model>.limit.context
(provider in OpenCode 1) in the user's global configuration, so the meter follows a model switch made inside OpenCode."""
import json
import os
import sqlite3
import threading
import time
import urllib.parse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .claude_code import clip, key_argument, one_line, tool_input

FIRST_ROWS = 400
KEEP_ERRORS = 20
KEEP_ENTRIES = 500
READS = {}
LOCK = threading.Lock()
CONFIG_SECONDS = 10
CONFIG = {}
ERROR_KINDS = {"provider.rate-limit": "rate_limit", "provider.internal": "server_error",
               "provider.timeout": "server_error", "provider.transport": "connection_lost"}


class SessionPath(type(Path())):
    """The database path, carrying the session id: what session_file returns and read takes. Its stat is the newer of
    the database and its write-ahead log, where OpenCode's writes land until a checkpoint."""
    session = None

    def stat(self, *, follow_symlinks=True):
        found = super().stat(follow_symlinks=follow_symlinks)
        try:
            log = Path(f"{self}-wal").stat()
        except OSError:
            return found
        return log if log.st_mtime > found.st_mtime else found


def session_path(database, session):
    path = SessionPath(database)
    path.session = session
    return path


def database(interactive=None):
    variables = {**os.environ, **((interactive or {}).get("env") or {})}
    if variables.get("OPENCODE_DB"):
        return Path(os.path.expanduser(variables["OPENCODE_DB"]))
    data = variables.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(data) / "opencode" / "opencode.db"


def stamp(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def context_tokens(tokens):
    """What OpenCode's context meter shows for an answer: its input, output and reasoning plus cached input."""
    tokens = tokens or {}
    cache = tokens.get("cache") or {}
    return sum(int(value or 0) for value in (tokens.get("input"), tokens.get("output"), tokens.get("reasoning"),
                                             cache.get("read"), cache.get("write")))


def model_limit(provider, model):
    """The context limit OpenCode's global configuration sets for a provider's model, or None."""
    if not provider or not model:
        return None
    cached = CONFIG.get("configs")
    if not cached or time.monotonic() - cached[0] > CONFIG_SECONDS:
        from .. import profiles
        cached = (time.monotonic(), profiles.opencode_configs())
        CONFIG["configs"] = cached
    found = None
    for data in cached[1]:
        for key in ("providers", "provider"):
            entry = (((data.get(key) or {}).get(provider) or {}).get("models") or {}).get(model) or {}
            limit = (entry.get("limit") or {}).get("context") if isinstance(entry, dict) else None
            if isinstance(limit, (int, float)) and limit > 0:
                found = int(limit)
    return found


def error_text(error):
    status = error.get("status")
    head = f"API Error: {status} " if isinstance(status, int) else ""
    body = (error.get("response") or {}).get("body") or ""
    return clip(f"{head}{error.get('message') or error.get('type') or 'error'}" + (f"\n{body}" if body else ""))


def tool_text(content):
    parts = []
    for part in content or []:
        if isinstance(part, dict):
            parts.append(str(part.get("text") or "") if part.get("type") == "text" else f"[{part.get('type') or 'part'}]")
    return "\n".join(parts)


def entries_of(kind, data, time, cwd=None):
    """The conversation entries of one message row (see theseus.adapters): user text, assistant text as Markdown, each
    tool call and its finished result, and the error that ended a failed turn. Reasoning and compaction summaries are
    left out."""
    if kind == "user":
        text = str(data.get("text") or "").strip()
        return [{"kind": "user", "text": clip(text), "time": time}] if text else []
    if kind == "idle":
        error = data.get("error")
        if data.get("outcome") == "failed" and isinstance(error, dict):
            return [{"kind": "assistant", "text": clip(f"Error: {error.get('message') or error.get('type')}"), "time": time}]
        return []
    if kind != "assistant":
        return []
    found = []
    for part in data.get("content") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and str(part.get("text") or "").strip():
            found.append({"kind": "assistant", "text": clip(str(part["text"]).strip()), "time": time, "format": "markdown"})
        elif part.get("type") == "tool":
            state = part.get("state") or {}
            arguments = state.get("input")
            found.append({"kind": "tool", "name": str(part.get("name") or "tool"), "time": time,
                          "summary": key_argument(arguments, cwd), "text": clip(tool_input(arguments))})
            if state.get("status") in ("completed", "error"):
                error = state.get("error")
                text = tool_text(state.get("content")) or str((error.get("message") if isinstance(error, dict) else error) or "")
                first = next((line for line in text.splitlines() if line.strip()), "")
                found.append({"kind": "result", "error": state.get("status") == "error", "summary": one_line(first),
                              "text": clip(text), "time": time})
    return found


def finished(kind, data):
    if kind == "assistant":
        return bool((data.get("time") or {}).get("completed") or data.get("error"))
    if kind == "compaction":
        return data.get("status") in ("completed", "failed")
    return True


def consume(state, kind, data, time):
    found = entries_of(kind, data, time, state.get("cwd"))
    if found:
        for number, entry in enumerate(found, state["total"] + 1):
            entry["n"] = number
        state["entries"] = (state["entries"] + found)[-KEEP_ENTRIES:]
        state["total"] += len(found)
    if time:
        state["updated"] = max(state.get("updated") or 0, created_epoch(time))
    if kind == "assistant" and context_tokens(data.get("tokens")):
        state["tokens"] = context_tokens(data["tokens"])
        model = data.get("model") or {}
        if model.get("id"):
            state["model"], state["provider"] = model["id"], model.get("providerID")
        if not data.get("error"):
            state["last"] = "answer"
    elif kind == "compaction" and data.get("status") == "completed":
        state["tokens"] = None
    elif kind == "idle" and data.get("outcome") == "failed" and isinstance(data.get("error"), dict):
        error = data["error"]
        state["errors"] = (state["errors"] + [{"time": time, "error": ERROR_KINDS.get(str(error.get("type"))),
                                               "text": error_text(error)}])[-KEEP_ERRORS:]
        state["last"] = "error"


def created_epoch(stamp_text):
    try:
        return datetime.fromisoformat(stamp_text.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0


def connect(path):
    return sqlite3.connect(f"file:{urllib.parse.quote(str(Path(path).resolve()))}?mode=ro", uri=True, timeout=2)


def read(path, session=None):
    """The state of one session after reading the message rows added since the last call (see theseus.adapters).
    session defaults to the id a SessionPath carries. Safe to call from several threads."""
    session = session or getattr(path, "session", None)
    if not session:
        return None
    with LOCK:
        try:
            return read_locked(Path(path), session)
        except (sqlite3.Error, OSError):
            return None


def read_locked(path, session):
    if not path.is_file():
        return None
    with closing(connect(path)) as conn:
        row = conn.execute("SELECT time_created, directory FROM session_v2 WHERE id = ?", (session,)).fetchone()
        if row is None:
            return None
        key = f"{path}:{session}:{row[0]}"
        state = READS.get(key)
        if state is None:
            state = {"seq": None, "tokens": None, "model": None, "provider": None, "limit": None, "updated": None,
                     "errors": [], "last": None, "entries": [], "total": 0, "key": key, "cwd": row[1]}
            rows = conn.execute("SELECT seq, type, time_created, data FROM session_message WHERE session_id = ? "
                                "ORDER BY seq DESC LIMIT ?", (session, FIRST_ROWS)).fetchall()[::-1]
        else:
            rows = conn.execute("SELECT seq, type, time_created, data FROM session_message WHERE session_id = ? AND seq > ? "
                                "ORDER BY seq", (session, state["seq"])).fetchall()
    parsed = []
    for seq, kind, created, text in rows:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        parsed.append((seq, kind, created, data if isinstance(data, dict) else {}))
    pending = next((n for n, (_, kind, _, data) in enumerate(parsed) if not finished(kind, data)), None)
    if pending is not None and not any(kind == "idle" for _, kind, _, _ in parsed[pending + 1:]):
        parsed = parsed[:pending]
    for seq, kind, created, data in parsed:
        consume(state, kind, data, stamp(created))
        state["seq"] = seq
    if state["seq"] is None:
        state["seq"] = 0 if not rows else rows[0][0] - 1
    state["limit"] = model_limit(state.get("provider"), state.get("model"))
    READS[key] = state
    return state


def session_file(conf, interactive, session, cwd):
    """The database holding a known session id, carrying that id (cwd is not needed: ids are unique), or None."""
    path = database(interactive)
    return session_path(path, session) if session and path.is_file() else None


def session_state(conf, agent, interactive):
    """The state of a supervised agent's session from its stored id; None for a hand-made pane (OpenCode records no
    pane or process for a session)."""
    if not (agent.get("managed") and agent.get("session_id")):
        return None
    return read(database(interactive), agent["session_id"])
