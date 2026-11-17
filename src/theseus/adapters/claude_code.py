"""The "claude-code-jsonl" transcript adapter (theseus.adapters): Claude Code's session records. Which session a tmux
pane runs comes from the client's sessions directory (one <pid>.json per running client, naming its session id,
working directory and tmux pane); a supervised agent's stored session id names its transcript directly. The JSONL
transcript is read incrementally for the context in use, the API errors it recorded and the conversation (user and
assistant text, tool calls and their results; thinking is left out).

The transcript of a session lives at <sessions dir>/../projects/<cwd with every character other than a letter, digit
or hyphen replaced by a hyphen>/<session id>.jsonl. Reads start at most TAIL_BYTES before the end of the file and then
continue from the previous offset, so a refresh reads only what was appended."""
import json
import os
import re
import threading
from pathlib import Path

TAIL_BYTES = 512 * 1024
KEEP_ERRORS = 20
KEEP_ENTRIES = 500
ENTRY_CHARS = 8000
READS = {}
SESSIONS = {}
LOCK = threading.Lock()


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


def session_of(directory, address, pids=None, exclude=()):
    """(session id, cwd) of the newest live client whose tmux field ends with address ("@<window>.%<pane>"), or None.
    The window id makes a pane id from another tmux server (or a test's fake one) unlikely to match. The tmux field
    does not name the server, so pids (the pane's process tree) narrows the match to one server and exclude drops
    clients known to run on another one."""
    if not address or "@" not in address:
        return None
    matches = [entry for entry in sessions(directory) if entry[2].endswith(address) and alive(entry[3])
               and (pids is None or entry[3] in pids) and entry[3] not in exclude]
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


def clip(text):
    text = str(text or "")
    return text if len(text) <= ENTRY_CHARS else text[:ENTRY_CHARS] + f"\n... ({len(text) - ENTRY_CHARS} more characters)"


def entries_of(record):
    """The conversation entries of one transcript record: {"kind": "user" | "assistant" | "tool" | "result", "text",
    "time"} plus "name" for a tool call and "error" for a result. Sidechain (subagent) and meta records, thinking and
    images are left out."""
    kind = record.get("type")
    if kind not in ("user", "assistant") or record.get("isSidechain") or record.get("isMeta"):
        return []
    content = (record.get("message") or {}).get("content")
    time = record.get("timestamp")
    if isinstance(content, str):
        return [{"kind": kind, "text": clip(content), "time": time}] if content.strip() else []
    found = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and str(part.get("text") or "").strip():
            found.append({"kind": kind, "text": clip(part["text"]), "time": time})
        elif part.get("type") == "tool_use":
            found.append({"kind": "tool", "name": str(part.get("name") or "tool"), "time": time,
                          "text": clip(json.dumps(part.get("input"), indent=1, ensure_ascii=False))})
        elif part.get("type") == "tool_result":
            result = part.get("content")
            text = result if isinstance(result, str) else content_text({"content": result})
            found.append({"kind": "result", "error": bool(part.get("is_error")), "text": clip(text), "time": time})
    return found


def consume(state, record):
    found = entries_of(record)
    if found:
        state["entries"] = (state["entries"] + found)[-KEEP_ENTRIES:]
        state["total"] += len(found)
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
    last answer), model, the recent API errors, whether the newest assistant entry is an error ("last"), the newest
    KEEP_ENTRIES conversation entries and how many were ever read ("total"). Safe to call from several threads."""
    with LOCK:
        return read_locked(Path(path))


def read_locked(path):
    try:
        info = path.stat()
    except OSError:
        return None
    state = READS.get(str(path))
    if state is None or state["inode"] != info.st_ino or info.st_size < state["offset"]:
        start = max(0, info.st_size - TAIL_BYTES)
        state = {"inode": info.st_ino, "offset": start, "partial": start > 0, "tokens": None, "model": None,
                 "errors": [], "last": None, "entries": [], "total": 0}
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


def pane_transcript(directory, address, pids=None, exclude=()):
    """read() of the transcript of the session running in the tmux pane at address, or None."""
    found = session_of(directory, address, pids, exclude)
    return read(transcript_path(directory, *found)) if found else None


def sessions_dir(conf, interactive):
    return conf.section("instances").get("sessions_dir") or (interactive or {}).get("sessions_dir") or ""


def session_file(conf, interactive, session, cwd):
    directory = sessions_dir(conf, interactive)
    return transcript_path(directory, session, cwd) if directory and session else None


def session_state(conf, agent, interactive):
    """The transcript state of the session in the agent's pane: the stored session id of a supervised agent (kept
    current from the CLI's status) first, else the newest live client the sessions directory names for the pane."""
    from .. import agents, supervisor, top, tmux as tm
    directory = sessions_dir(conf, interactive)
    if not directory:
        return None
    if agent.get("managed") and agent.get("session_id"):
        path = transcript_path(directory, agent["session_id"], str(supervisor.cwd(conf)))
        if path.exists():
            return read(path)
    server, pane = agents.where(agent)
    pids, exclude = None, ()
    if server.is_legacy:
        address = agents.tmux("display", "-p", "-t", pane, "#{window_id}.#{pane_id}").stdout.strip()
        exclude = agents.supervised_pids()
    else:
        text = server.display(pane, "#{window_id}.#{pane_id}" + tm.SEP + "#{pane_pid}") or ""
        address, _, pid = text.partition(tm.SEP)
        pids = set(top.descendants(top.process_table(), int(pid))) | {int(pid)} if pid.isdigit() else set()
    return pane_transcript(directory, address, pids, exclude)
