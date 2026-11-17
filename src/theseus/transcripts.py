"""Claude Code session transcripts of persistent agents: which session a tmux pane runs, read from the client's
sessions directory (one <pid>.json per running client, naming its session id, working directory and tmux pane), and an
incremental tail reader of the session's JSONL transcript for the context in use and the API errors it recorded.

The transcript of a session lives at <sessions dir>/../projects/<cwd with every character other than a letter, digit
or hyphen replaced by a hyphen>/<session id>.jsonl. Reads start at most TAIL_BYTES before the end of the file and then
continue from the previous offset, so a refresh reads only what was appended."""
import json
import os
import re
from pathlib import Path

TAIL_BYTES = 512 * 1024
KEEP_ERRORS = 20
READS = {}
SESSIONS = {}


def sessions(directory):
    """[(session id, cwd, tmux pane address, pid, updated)] of the session files in directory; a file is parsed again
    only when its mtime or size changed (clients rewrite their file in place)."""
    path = Path(os.path.expanduser(str(directory or "")))
    cache = SESSIONS.setdefault(str(path), {})
    seen = {}
    try:
        items = list(os.scandir(path))
    except OSError:
        return []
    for item in items:
        if not item.name.endswith(".json"):
            continue
        try:
            info = item.stat()
        except OSError:
            continue
        stamp = (info.st_mtime_ns, info.st_size)
        cached = cache.get(item.name)
        if cached is None or cached[0] != stamp:
            try:
                data = json.loads(Path(item.path).read_text())
            except (OSError, ValueError):
                data = None
            entry = None
            if isinstance(data, dict) and data.get("sessionId") and data.get("cwd"):
                entry = (data["sessionId"], data["cwd"], str(data.get("tmux") or ""), data.get("pid"),
                         data.get("updatedAt") or data.get("startedAt") or 0)
            cached = (stamp, entry)
        seen[item.name] = cached
    SESSIONS[str(path)] = seen
    return [entry for _, entry in seen.values() if entry]


def alive(pid):
    if not pid or not Path("/proc").is_dir():
        return True
    return Path(f"/proc/{pid}").exists()


def session_of(directory, address):
    """(session id, cwd) of the newest live client whose tmux field ends with address ("@<window>.%<pane>"), or None.
    The window id makes a pane id from another tmux server (or a test's fake one) unlikely to match."""
    if not address or "@" not in address:
        return None
    matches = [entry for entry in sessions(directory) if entry[2].endswith(address) and alive(entry[3])]
    if not matches:
        return None
    best = max(matches, key=lambda entry: entry[4])
    return best[0], best[1]


def transcript_path(directory, session, cwd):
    root = Path(os.path.expanduser(str(directory))).parent / "projects"
    return root / re.sub(r"[^A-Za-z0-9-]", "-", cwd) / f"{session}.jsonl"


def content_text(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    return " ".join(part.get("text", "") for part in content or [] if isinstance(part, dict))


def consume(state, record):
    if record.get("type") != "assistant":
        return
    message = record.get("message") or {}
    if record.get("isApiErrorMessage"):
        state["errors"] = (state["errors"] + [{"time": record.get("timestamp"), "error": record.get("error"),
                                               "text": content_text(message)}])[-KEEP_ERRORS:]
        state["last"] = "error"
        return
    usage = message.get("usage") or {}
    total = sum(int(usage.get(name) or 0) for name in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    if total:
        state["tokens"] = total
        state["model"] = message.get("model")
    state["last"] = "answer"


def read(path):
    """The transcript's state after reading what was appended since the last call: tokens (context in use after the
    last answer), model, the recent API errors and whether the newest assistant entry is an error ("last")."""
    path = Path(path)
    try:
        info = path.stat()
    except OSError:
        return None
    state = READS.get(str(path))
    if state is None or state["inode"] != info.st_ino or info.st_size < state["offset"]:
        start = max(0, info.st_size - TAIL_BYTES)
        state = {"inode": info.st_ino, "offset": start, "partial": start > 0, "tokens": None, "model": None,
                 "errors": [], "last": None}
    if info.st_size > state["offset"]:
        try:
            with open(path, "rb") as handle:
                handle.seek(state["offset"])
                data = handle.read(info.st_size - state["offset"])
        except OSError:
            return None
        end = data.rfind(b"\n")
        if end >= 0:
            lines = data[:end + 1].splitlines()
            state["offset"] += end + 1
            if state.pop("partial", False):
                lines = lines[1:]
            for line in lines:
                try:
                    consume(state, json.loads(line))
                except (ValueError, AttributeError, TypeError):
                    continue
    READS[str(path)] = state
    return state


def pane_transcript(directory, address):
    """read() of the transcript of the session running in the tmux pane at address, or None."""
    found = session_of(directory, address)
    return read(transcript_path(directory, *found)) if found else None
